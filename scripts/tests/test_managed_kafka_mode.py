#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_managed_kafka_mode.py
#  Purpose:      Prove a managed Kafka provider reaches kafka.mode external and
#                the broker endpoint from the cluster secret alone, and that the
#                network policies open the broker ports for that case.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""kafka.mode on a managed broker -- the path with no deploy-repo overlay.

profile-scale.yaml declares kafka.mode cluster, which is right for Strimzi and
wrong for MSK, Confluent Cloud and Redpanda Cloud. A deploy repo can move it,
but a deployment that supplies none has nothing layered after the profile, so
the mode stays cluster: the MSK bootstrap Job never renders and the baseline
default-deny egress keeps every pod off the broker ports.

The facts are already on the cluster secret, so the fix is the appsets reading
them back. Three things are checked here: the profile default is untouched for
an in-cluster broker, the annotation-driven overlay reaches every chart that
resolves a broker, and bootstrap.sh sets the annotation for exactly the three
managed providers. The protocol travels the same way: all three serve TLS only,
so bootstrap.sh derives SASL_SSL for them and the cluster secret carries it.

The apps render through scripts/dfe-weave, which executes layer2-apps' values
block for the cluster secret's annotations; the loader needs the scalo-service
library (_weave.library), the engine's env renders from dfe-extras offline.

    python3 scripts/tests/test_managed_kafka_mode.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _expect import expect, standalone, summary
from _gate import effective_env, golden, object_id
from _weave import render_app, render_extras

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
VALUES = REPO_ROOT / "argocd" / "values"
APPSETS = REPO_ROOT / "argocd" / "appsets"
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
AWS_ROOT = REPO_ROOT / "terraform" / "environments" / "aws"

# The valueFiles order every layer2 appset uses, before any overlay.
BASE_CASCADE = [VALUES / "common.yaml", VALUES / "aws.yaml", VALUES / "profile-scale.yaml"]

# What the appsets render into their `values` block when the cluster secret
# carries a managed broker's facts. A placeholder endpoint (RFC 2606), never a
# real estate address: this repo ships publicly.
MANAGED_BOOTSTRAP = "b-1.example.invalid:9096,b-2.example.invalid:9096"
MANAGED_IAM = "b-1.example.invalid:9098"

MANAGED_OVERLAY = (
    "kafka:\n"
    "  mode: external\n"
    f"  bootstrapServers: {MANAGED_BOOTSTRAP!r}\n"
    "  external:\n"
    "    provider: msk\n"
    f"    bootstrap: {MANAGED_BOOTSTRAP!r}\n"
    "    msk:\n"
    f"      bootstrapIam: {MANAGED_IAM!r}\n"
)

MANAGED_PROVIDERS = ("msk", "confluent-cloud", "redpanda-cloud")
IN_CLUSTER_PROVIDERS = ("strimzi", "redpanda")

# The cluster secret bootstrap.sh writes for an MSK broker, as layer2-apps reads it.
MANAGED_ANNOTATIONS = {
    "dfe.hyperi.io/kafka_mode": "external",
    "dfe.hyperi.io/kafka_provider": "msk",
    "dfe.hyperi.io/kafka_bootstrap": MANAGED_BOOTSTRAP,
    "dfe.hyperi.io/kafka_bootstrap_iam": MANAGED_IAM,
    "dfe.hyperi.io/kafka_security_protocol": "SASL_SSL",
}

# Where the apps render: the scale tier on aws, the cascade BASE_CASCADE names.
APP_CELL = ("scale", "aws")


def render(chart: str, *, values: list[Path] | None = None, sets: tuple[str, ...] = ()) -> list[dict]:
    cmd = ["helm", "template", chart, str(CHARTS / chart)]
    for path in values or BASE_CASCADE:
        cmd += ["-f", str(path)]
    cmd += ["--set", "appNamespace=dfe-aws"]
    for item in sets:
        cmd += ["--set", item]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def with_managed(chart: str) -> list[dict]:
    """The chart as Argo renders it once the annotation-driven block is layered."""
    with tempfile.TemporaryDirectory() as tmp:
        overlay = Path(tmp) / "managed.yaml"
        overlay.write_text(MANAGED_OVERLAY, encoding="utf-8", newline="\n")
        return render(chart, values=[*BASE_CASCADE, overlay])


def kinds(docs: list[dict]) -> set[str]:
    return {d.get("kind", "") for d in docs}


def kafka_egress(docs: list[dict]) -> list[dict]:
    return [
        d
        for d in docs
        if d.get("kind") == "NetworkPolicy"
        and d["metadata"]["name"].startswith("allow-external-kafka-egress-")
    ]


def test_the_profile_default_stays_cluster_for_an_in_cluster_broker() -> None:
    """Nothing here moves a Strimzi deployment: it still gets the Kafka CR."""
    docs = render("kafka")
    expect("the scale profile still renders the Strimzi Kafka CR", "Kafka" in kinds(docs),
           f"got {sorted(kinds(docs))}")
    expect("and no MSK bootstrap Job", "Job" not in kinds(docs), f"got {sorted(kinds(docs))}")
    expect("and the network policies open no broker egress",
           kafka_egress(render("network-policies")) == [])


def test_the_managed_overlay_renders_the_msk_bootstrap_job() -> None:
    """mode external plus provider msk plus an IAM endpoint is the Job's gate."""
    docs = with_managed("kafka")
    expect("a managed broker renders the bootstrap Job", "Job" in kinds(docs),
           f"got {sorted(kinds(docs))}")
    expect("and no in-cluster Kafka CR", "Kafka" not in kinds(docs), f"got {sorted(kinds(docs))}")
    job = next(d for d in docs if d.get("kind") == "Job")
    script = job["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    expect("the Job bootstraps against the IAM endpoint", MANAGED_IAM in script,
           "the IAM endpoint never reached the Job's command")


def test_the_managed_overlay_points_the_apps_at_the_broker() -> None:
    """The profile's bootstrapServers names a Service a managed deploy never creates."""
    rendered = yaml.safe_load((VALUES / "profile-scale.yaml").read_text(encoding="utf-8"))
    expect("the profile still defaults to the in-cluster bootstrap",
           rendered["kafka"]["mode"] == "cluster"
           and rendered["kafka"]["bootstrapServers"].endswith(".svc.cluster.local:9092"),
           f"got {rendered['kafka']}")
    docs = render_app("dfe-loader", *APP_CELL, annotations=MANAGED_ANNOTATIONS)
    env = effective_env(docs, "Deployment/dfe-loader")
    brokers = [v for k, v in env.items() if "BROKER" in k or "BOOTSTRAP" in k]
    expect("the loader dials the managed broker", MANAGED_BOOTSTRAP in brokers,
           f"got {brokers}")
    in_cluster = effective_env(golden("dfe-loader", *APP_CELL), "Deployment/dfe-loader")
    expect("and an in-cluster deploy keeps the profile's broker",
           in_cluster.get("DFE_LOADER_KAFKA_BROKERS") == rendered["kafka"]["bootstrapServers"],
           f"got {in_cluster.get('DFE_LOADER_KAFKA_BROKERS')!r}")


def test_the_managed_overlay_opens_the_broker_egress() -> None:
    """The half the live cycle proved missing: policy, not an AWS control."""
    policies = kafka_egress(with_managed("network-policies"))
    expect("a managed broker opens the broker egress", policies != [], "no policy rendered")
    namespaces = {p["metadata"]["namespace"] for p in policies}
    expect("including the namespace the bootstrap Job runs in", "strimzi" in namespaces,
           f"got {sorted(namespaces)}")
    ports = {
        port["port"]
        for p in policies
        for rule in p["spec"]["egress"]
        for port in rule.get("ports", [])
    }
    expect("on the SCRAM and IAM listeners both", {9096, 9098} <= ports, f"got {sorted(ports)}")


def test_the_engine_reaches_a_managed_broker() -> None:
    """The engine dials the broker for its topic set and holds readiness on the
    dead-letter topics, so an egress list without it leaves every app in Init."""
    env = next(
        d
        for d in render_extras("dfe-engine", *APP_CELL, annotations=MANAGED_ANNOTATIONS)
        if object_id(d) == "ConfigMap/dfe-engine-env"
    )["data"]
    expect("the engine is pointed at the managed broker",
           env.get("DFE_KAFKA_BOOTSTRAP_SERVERS") == MANAGED_BOOTSTRAP,
           f"got {env.get('DFE_KAFKA_BOOTSTRAP_SERVERS')!r}")
    engine = [
        p
        for p in kafka_egress(with_managed("network-policies"))
        if p["spec"]["podSelector"].get("matchLabels", {}).get("app.kubernetes.io/name")
        == "dfe-engine"
    ]
    ports = {port["port"] for p in engine for rule in p["spec"]["egress"] for port in rule["ports"]}
    expect("so the network policies open the SCRAM listener to the engine", 9096 in ports,
           f"engine policies: {[p['metadata']['name'] for p in engine]}")


def test_bootstrap_sets_external_for_exactly_the_managed_providers() -> None:
    """The mode is derived from the provider, never asked for twice."""
    script = BOOTSTRAP.read_text(encoding="utf-8")
    case = re.search(r'case "\$\{DFE_KAFKA_PROVIDER\}" in\n(.*?)\nesac', script, re.S)
    expect("bootstrap.sh derives kafka.mode from the provider", case is not None)
    body = case.group(1) if case else ""
    arm = re.search(r'^\s*([a-z|-]+)\)\s*DFE_KAFKA_MODE="external"', body, re.M)
    named = set(arm.group(1).split("|")) if arm else set()
    expect("exactly the managed providers resolve to external", named == set(MANAGED_PROVIDERS),
           f"got {sorted(named)}")
    for provider in IN_CLUSTER_PROVIDERS:
        expect(f"{provider} keeps the profile's own mode", provider not in named,
               f"named in: {sorted(named)}")


PROTOCOL_ENV = "DFE_KAFKA_SECURITY_PROTOCOL"
PROTOCOL_ANNOTATION = "dfe.hyperi.io/kafka_security_protocol"


def derived(provider: str) -> tuple[str, str] | None:
    """bootstrap.sh's own mode and protocol derivation, run for one provider.

    Executes the script's text from the mode's reset to the protocol's export,
    so a changed case arm or condition is what gets tested. None when either
    end is missing.
    """
    script = BOOTSTRAP.read_text(encoding="utf-8")
    start = script.find('DFE_KAFKA_MODE=""')
    end = script.find(f"export {PROTOCOL_ENV}\n")
    bash = shutil.which("bash")
    if start < 0 or end < start or bash is None:
        return None
    snippet = script[start:end] + f'printf "%s|%s" "$DFE_KAFKA_MODE" "${{{PROTOCOL_ENV}:-}}"\n'
    out = subprocess.run(
        [bash, "-c", snippet],
        env={"DFE_KAFKA_PROVIDER": provider},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if out.returncode != 0:
        raise SystemExit(f"bootstrap.sh's derivation failed for {provider!r}:\n{out.stderr}")
    mode, _, protocol = out.stdout.partition("|")
    return mode, protocol


def test_bootstrap_derives_tls_for_exactly_the_managed_providers() -> None:
    """Every managed provider serves TLS only, and common.yaml names the in-cluster
    brokers' plain listener -- so a managed deploy left on it cannot connect."""
    for provider in MANAGED_PROVIDERS:
        got = derived(provider)
        expect(f"bootstrap.sh derives external + SASL_SSL for {provider}",
               got == ("external", "SASL_SSL"), f"got {got!r}")
    for provider in (*IN_CLUSTER_PROVIDERS, ""):
        got = derived(provider)
        expect(f"{provider or 'no provider'} derives no protocol, so common.yaml's stands",
               got == ("", ""), f"got {got!r}")


def test_the_cluster_secret_carries_the_protocol() -> None:
    template = CLUSTER_SECRET.read_text(encoding="utf-8")
    expect(f"the cluster secret carries {PROTOCOL_ANNOTATION}",
           f'{PROTOCOL_ANNOTATION}: "${{{PROTOCOL_ENV}}}"' in template,
           "annotation missing from the cluster-secret template")


def test_every_appset_reads_the_mode_back() -> None:
    """A fact on the cluster secret that no appset reads is a fact nothing uses."""
    for name in ("layer2-apps.yaml", "layer2-data.yaml", "layer2-platform.yaml"):
        body = (APPSETS / name).read_text(encoding="utf-8")
        expect(f"{name} reads the kafka_mode annotation",
               'dfe.hyperi.io/kafka_mode' in body, f"not found in {name}")
        expect(f"{name} lands it on kafka.mode",
               re.search(r"kafka:\n\s+(\{\{-.*\n\s+)*mode: ", body) is not None,
               f"no kafka.mode block in {name}")
    for name in ("layer2-apps.yaml", "layer2-data.yaml"):
        body = (APPSETS / name).read_text(encoding="utf-8")
        expect(f"{name} reads the broker endpoint too",
               'dfe.hyperi.io/kafka_bootstrap"' in body, f"not found in {name}")


MESSAGE_SIZE_ENV = "DFE_KAFKA_MESSAGE_MAX_BYTES"
MESSAGE_SIZE_ANNOTATION = "dfe.hyperi.io/kafka_message_max_bytes"


def test_the_root_hands_on_the_size_each_managed_body_was_given() -> None:
    """The engine creates every topic at the size the brokers were built for, so the
    output reads the same variable the root passes a body that takes one, never a
    second copy. redpanda-cloud takes none: the output is the only reader there."""
    main_tf = (AWS_ROOT / "main.tf").read_text(encoding="utf-8")
    outputs = (AWS_ROOT / "outputs.tf").read_text(encoding="utf-8")
    block = re.search(rf'output "{MESSAGE_SIZE_ENV}" \{{\n(.*?)\n\}}', outputs, re.S)
    expect(f"the aws root emits {MESSAGE_SIZE_ENV}", block is not None, "no such output")
    body = block.group(1) if block else ""
    for module, source in (
        ("kafka", "var.kafka.msk.message_max_bytes"),
        ("confluent", "var.kafka.message_max_bytes"),
    ):
        module_block = re.search(rf'module "{module}" \{{\n(.*?)\n\}}', main_tf, re.S)
        passed = module_block is not None and re.search(
            rf"^\s*message_max_bytes\s*=\s*{re.escape(source)}\s*$", module_block.group(1), re.M
        )
        expect(f"module {module} is given {source}", bool(passed), "the root passes something else")
        expect(f"and the output reads {source}", source in body, body)
    expect("the output reads var.kafka.message_max_bytes for redpanda-cloud too",
           '"redpanda-cloud"' in body and "var.kafka.message_max_bytes" in body, body)
    expect("an in-cluster broker gets an empty value", re.search(r':\s*""\s*\)', body) is not None, body)


def test_the_size_travels_bootstrap_to_the_engine_chart() -> None:
    """Each hop spells the fact on its own, and a miss renders empty: the engine
    then creates its topics at its own default, above a managed ceiling."""
    script = BOOTSTRAP.read_text(encoding="utf-8")
    expect(f"bootstrap.sh defaults {MESSAGE_SIZE_ENV} empty",
           f'export {MESSAGE_SIZE_ENV}="${{{MESSAGE_SIZE_ENV}:-}}"' in script)
    template = CLUSTER_SECRET.read_text(encoding="utf-8")
    expect(f"the cluster secret carries {MESSAGE_SIZE_ANNOTATION}",
           f'{MESSAGE_SIZE_ANNOTATION}: "${{{MESSAGE_SIZE_ENV}}}"' in template)
    doc = yaml.safe_load((APPSETS / "layer2-apps.yaml").read_text(encoding="utf-8"))
    values_block = doc["spec"]["template"]["spec"]["sources"][0]["helm"]["values"]
    expect("layer2-apps reads the annotation",
           f'$kafkaMessageMaxBytes := index .metadata.annotations "{MESSAGE_SIZE_ANNOTATION}"'
           in values_block, values_block)
    expect("and lands it on kafka.messageMaxBytes under the managed block",
           "messageMaxBytes: {{ $kafkaMessageMaxBytes | quote }}" in values_block
           and len(re.findall(r"^kafka:", values_block, re.M)) == 1, values_block)


def test_the_managed_overlay_gives_the_engine_the_topic_size() -> None:
    """What the appset renders for a managed broker, as the engine's env carries it.

    dfe-extras writes the engine's env into dfe-engine-env, which the engine's
    thin chart reads through envFrom.
    """
    facts = {**MANAGED_ANNOTATIONS, MESSAGE_SIZE_ANNOTATION: "8388608"}
    docs = render_extras("dfe-engine", *APP_CELL, annotations=facts)
    env = next(d for d in docs if object_id(d) == "ConfigMap/dfe-engine-env")["data"]
    expect("the engine creates its topics at the managed size",
           env.get("DFE_KAFKA_TOPIC_MAX_MESSAGE_BYTES") == "8388608",
           f"got {env.get('DFE_KAFKA_TOPIC_MAX_MESSAGE_BYTES')!r}")


RETENTION_ENV = "DFE_KAFKA_RETENTION_MS"
RETENTION_ANNOTATION = "dfe.hyperi.io/kafka_retention_ms"


def test_the_root_hands_on_the_dials_retention() -> None:
    """A SaaS broker's default retention is the vendor's, so the output hands the
    engine the dial's, from the same variable each provider's dial fills."""
    outputs = (AWS_ROOT / "outputs.tf").read_text(encoding="utf-8")
    block = re.search(rf'output "{RETENTION_ENV}" \{{\n(.*?)\n\}}', outputs, re.S)
    expect(f"the aws root emits {RETENTION_ENV}", block is not None, "no such output")
    body = block.group(1) if block else ""
    expect("msk reads its own block", "var.kafka.msk.log_retention_ms" in body, body)
    expect("both SaaS bodies read the top-level value",
           '"confluent-cloud", "redpanda-cloud"' in body and "var.kafka.log_retention_ms" in body,
           body)
    expect("an in-cluster broker gets an empty value", re.search(r':\s*""\s*\)', body) is not None, body)


def test_the_retention_travels_bootstrap_to_the_engine_chart() -> None:
    """Each hop spells the fact on its own, and a miss renders empty: the engine
    then leaves its topics at the broker's default retention."""
    script = BOOTSTRAP.read_text(encoding="utf-8")
    expect(f"bootstrap.sh defaults {RETENTION_ENV} empty",
           f'export {RETENTION_ENV}="${{{RETENTION_ENV}:-}}"' in script)
    template = CLUSTER_SECRET.read_text(encoding="utf-8")
    expect(f"the cluster secret carries {RETENTION_ANNOTATION}",
           f'{RETENTION_ANNOTATION}: "${{{RETENTION_ENV}}}"' in template)
    doc = yaml.safe_load((APPSETS / "layer2-apps.yaml").read_text(encoding="utf-8"))
    values_block = doc["spec"]["template"]["spec"]["sources"][0]["helm"]["values"]
    expect("layer2-apps reads the annotation",
           f'$kafkaRetentionMs := index .metadata.annotations "{RETENTION_ANNOTATION}"'
           in values_block, values_block)
    expect("and lands it on kafka.topicRetentionMs",
           "topicRetentionMs: {{ $kafkaRetentionMs | quote }}" in values_block, values_block)


def test_the_managed_overlay_gives_the_engine_the_topic_retention() -> None:
    """What the appset renders for a managed broker, as the engine's env carries it."""
    facts = {**MANAGED_ANNOTATIONS, RETENTION_ANNOTATION: "259200000"}
    docs = render_extras("dfe-engine", *APP_CELL, annotations=facts)
    env = next(d for d in docs if object_id(d) == "ConfigMap/dfe-engine-env")["data"]
    expect("the engine creates its topics with the dial's retention",
           env.get("DFE_KAFKA_TOPIC_RETENTION_MS") == "259200000",
           f"got {env.get('DFE_KAFKA_TOPIC_RETENTION_MS')!r}")
    in_cluster = effective_env(golden("dfe-engine", *APP_CELL), "Deployment/dfe-engine")
    expect("an in-cluster broker leaves the engine's default",
           "DFE_KAFKA_TOPIC_MAX_MESSAGE_BYTES" not in in_cluster, "the variable was rendered")


TOPIC_RF_ENV = "DFE_KAFKA_TOPIC_REPLICATION_FACTOR"
MANAGED_TOPIC_RF = "3"


def test_a_managed_broker_gets_source_topics_at_three_replicas() -> None:
    """Confluent Cloud and Redpanda Cloud refuse a topic below 3 replicas, and MSK's
    min.insync.replicas of 2 refuses acks=all on fewer, so the engine is handed 3.

    Rendered on the single tier, which hands the engine no factor of its own, so
    the 3 can only have come from the managed block.
    """
    for provider in MANAGED_PROVIDERS:
        facts = {**MANAGED_ANNOTATIONS, "dfe.hyperi.io/kafka_provider": provider}
        docs = render_extras("dfe-engine", "single", "aws", annotations=facts)
        env = next(d for d in docs if object_id(d) == "ConfigMap/dfe-engine-env")["data"]
        expect(f"{provider}: the engine creates source topics at {MANAGED_TOPIC_RF} replicas",
               env.get(TOPIC_RF_ENV) == MANAGED_TOPIC_RF, f"got {env.get(TOPIC_RF_ENV)!r}")
    in_cluster = effective_env(golden("dfe-engine", "single", "aws"), "Deployment/dfe-engine")
    expect("one in-cluster broker leaves the factor to the engine",
           TOPIC_RF_ENV not in in_cluster, f"got {in_cluster.get(TOPIC_RF_ENV)!r}")


def test_the_managed_factor_rides_the_managed_block_alone() -> None:
    """Under kafka_mode external, beside the endpoint, so an in-cluster deploy never gets it."""
    doc = yaml.safe_load((APPSETS / "layer2-apps.yaml").read_text(encoding="utf-8"))
    for index, source in enumerate(doc["spec"]["template"]["spec"]["sources"][:2]):
        block = source["helm"]["values"]
        managed = re.search(
            r'\{\{- if eq \$kafkaMode "external" \}\}\n(?:\s*#.*\n)*\s*topicReplicationFactor: 3\n',
            block,
        )
        expect(f"source {index} sets the factor under kafka_mode external", managed is not None,
               "no topicReplicationFactor under the managed block")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
