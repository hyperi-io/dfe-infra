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

    python3 scripts/tests/test_managed_kafka_mode.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

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
    "  provider: msk\n"
    f"  bootstrapServers: {MANAGED_BOOTSTRAP!r}\n"
    "  external:\n"
    f"    bootstrap: {MANAGED_BOOTSTRAP!r}\n"
    "    msk:\n"
    f"      bootstrapIam: {MANAGED_IAM!r}\n"
)

MANAGED_PROVIDERS = ("msk", "confluent-cloud", "redpanda-cloud")
IN_CLUSTER_PROVIDERS = ("strimzi", "redpanda")


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
    docs = with_managed("dfe-loader")
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    env = {e["name"]: e.get("value", "") for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    brokers = [v for k, v in env.items() if "BROKER" in k or "BOOTSTRAP" in k]
    expect("the loader dials the managed broker", MANAGED_BOOTSTRAP in brokers,
           f"got {brokers}")


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
    """The engine's topics must match the landing topics, so the output reads the
    same variable the root passes the selected body, never a second copy."""
    main_tf = (AWS_ROOT / "main.tf").read_text(encoding="utf-8")
    outputs = (AWS_ROOT / "outputs.tf").read_text(encoding="utf-8")
    block = re.search(rf'output "{MESSAGE_SIZE_ENV}" \{{\n(.*?)\n\}}', outputs, re.S)
    expect(f"the aws root emits {MESSAGE_SIZE_ENV}", block is not None, "no such output")
    body = block.group(1) if block else ""
    for module, source in (
        ("kafka", "var.kafka.msk.message_max_bytes"),
        ("confluent", "var.kafka.message_max_bytes"),
        ("redpanda", "var.kafka.message_max_bytes"),
    ):
        module_block = re.search(rf'module "{module}" \{{\n(.*?)\n\}}', main_tf, re.S)
        passed = module_block is not None and re.search(
            rf"message_max_bytes\s*=\s*{re.escape(source)}\n", module_block.group(1)
        )
        expect(f"module {module} is given {source}", bool(passed), "the root passes something else")
        expect(f"and the output reads {source}", source in body, body)
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
           and values_block.count("kafka:") == 1, values_block)


def test_the_managed_overlay_gives_the_engine_the_topic_size() -> None:
    """What the appset renders for a managed broker, as the engine chart reads it."""
    with tempfile.TemporaryDirectory() as tmp:
        overlay = Path(tmp) / "managed.yaml"
        overlay.write_text(MANAGED_OVERLAY + "  messageMaxBytes: '8388608'\n",
                           encoding="utf-8", newline="\n")
        docs = render("dfe-engine", values=[*BASE_CASCADE, overlay])
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    env = {e["name"]: e.get("value") for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    expect("the engine creates its topics at the managed size",
           env.get("DFE_KAFKA_TOPIC_MAX_MESSAGE_BYTES") == "8388608",
           f"got {env.get('DFE_KAFKA_TOPIC_MAX_MESSAGE_BYTES')!r}")
    in_cluster = next(d for d in render("dfe-engine") if d.get("kind") == "Deployment")
    names = {e["name"] for e in in_cluster["spec"]["template"]["spec"]["containers"][0]["env"]}
    expect("an in-cluster broker leaves the engine's default",
           "DFE_KAFKA_TOPIC_MAX_MESSAGE_BYTES" not in names, "the variable was rendered")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
