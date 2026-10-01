#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_kafka_credential_chain.py
#  Purpose:      The Kafka credential chain end to end in a render: the keys the
#                apps read, the provider table they are derived from, and the
#                external-mode contract.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for dfe-infra #187, #191, #10, #190 and the chart half of #9.

**#187 -- two keys nothing wrote.** The scale profile turns the cross-namespace
credential projection off, because the broker namespace IS the app namespace
there. That also switched off the only template writing `username` and
`sasl.mechanism` into `dfe-kafka-user`, which have nothing to do with
namespaces. Four charts name those keys in a non-optional secretKeyRef, so every
one of their pods sat in CreateContainerConfigError with no container started.

**#191 and #10 -- the provider table.** kafbat asked for SASL_SSL against a
broker the same render served on SASL_PLAINTEXT, because the table had no key
for a DFE-owned broker on its TLS-off listener. The table now lives in
dfe-common, both `-no-tls` keys are in it, and everything that needs a protocol
or a mechanism derives from it instead of carrying a literal.

**#9 -- external mode.** Naming the supplied broker's provider gets the same
credential shape the DFE-owned tiers produce, so a Confluent Cloud target is
expressible: SASL_SSL with PLAIN, the one sanctioned exception to SCRAM-SHA-512.

**#446 -- a managed broker's credential in the app namespace.** On MSK,
Confluent Cloud and Redpanda Cloud the credential reached no namespace an app
reads from: layer2-data never named the provider, so the chart rendered a
password-only secret in its own namespace, keyed on a store path nothing seeds.
These tests render the appset's own values block for each broker and carry it
through the kafka chart, the way Argo does.

    python3 scripts/tests/test_kafka_credential_chain.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
KAFKA = CHARTS / "kafka"
LIBRARY_HARNESS = REPO_ROOT / "helm" / "library" / "dfe-common" / "tests" / "lint-test"
STACK = REPO_ROOT / "helm" / "dfe-stack"
PROFILES = STACK / "profiles"
KAFKA_TPL = REPO_ROOT / "helm" / "library" / "dfe-common" / "templates" / "_kafka.tpl"
LAYER2_DATA = REPO_ROOT / "argocd" / "appsets" / "layer2-data.yaml"
ARGO_VALUES = REPO_ROOT / "argocd" / "values"

_loader = importlib.machinery.SourceFileLoader(
    "deploy_matrix", str(REPO_ROOT / "scripts" / "deploy_matrix.py")
)
_spec = importlib.util.spec_from_loader("deploy_matrix", _loader)
deploy_matrix = importlib.util.module_from_spec(_spec)
sys.modules["deploy_matrix"] = deploy_matrix
_loader.exec_module(deploy_matrix)

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import render_dial  # noqa: E402

# The DFE Kafka credential contract (dfe-engine#98) as this file expects to find
# it rendered: provider identity -> (security.protocol, sasl.mechanism).
CONTRACT_TABLE = {
    "plaintext": ("PLAINTEXT", ""),
    "strimzi-no-tls": ("SASL_PLAINTEXT", "SCRAM-SHA-512"),
    "redpanda-no-tls": ("SASL_PLAINTEXT", "SCRAM-SHA-512"),
    "strimzi": ("SASL_SSL", "SCRAM-SHA-512"),
    "redpanda": ("SASL_SSL", "SCRAM-SHA-512"),
    "msk": ("SASL_SSL", "SCRAM-SHA-512"),
    "redpanda-cloud": ("SASL_SSL", "SCRAM-SHA-512"),
    "confluent-cloud": ("SASL_SSL", "PLAIN"),
    "msk_iam": ("SASL_SSL", "OAUTHBEARER"),
}

# Providers that ship a schema registry, so kafbat only wires the URL where one
# exists to answer it.
WITH_REGISTRY = {"confluent-cloud", "redpanda", "redpanda-no-tls", "redpanda-cloud"}

# The charts naming dfe-kafka-user keys in a non-optional secretKeyRef.
SECRET_CONSUMERS = ("dfe-receiver", "dfe-loader", "kafbat", "dfe-engine")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )


def _helm(
    chart: Path, *sets: str, values: tuple[Path, ...], show: str = ""
) -> subprocess.CompletedProcess[str]:
    cmd = ["helm", "template", chart.name, str(chart)]
    for path in values:
        cmd += ["-f", str(path)]
    if show:
        cmd += ["--show-only", show]
    for s in sets:
        cmd += ["--set", s]
    return _run(cmd)


def render(chart: Path, *sets: str, values: tuple[Path, ...] = (), show: str = "") -> list[dict]:
    """Rendered docs. Raises with helm's own message when the chart refuses."""
    out = _helm(chart, *sets, values=values, show=show)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart.name} {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def render_error(chart: Path, *sets: str, values: tuple[Path, ...] = ()) -> str:
    """helm's stderr when the chart refuses to render, or "" when it rendered."""
    out = _helm(chart, *sets, values=values)
    return out.stderr if out.returncode != 0 else ""


def build_deps(chart: Path) -> None:
    """Vendor a chart's dependencies once, where they are not committed."""
    if (chart / "charts").is_dir():
        return
    out = _run(["helm", "dependency", "build", str(chart)])
    if out.returncode != 0:
        raise SystemExit(f"helm dependency build failed for {chart.name}:\n{out.stderr}")


def build_harness_deps() -> None:
    build_deps(LIBRARY_HARNESS)


def stack(profile: str) -> list[dict]:
    """The umbrella rendered with one profile overlay."""
    build_deps(STACK)
    return render(STACK, values=(PROFILES / profile,))


def derived(provider: str) -> dict[str, str]:
    """What the library table derives for one provider key."""
    build_harness_deps()
    docs = render(
        LIBRARY_HARNESS,
        f"kafka.provider={provider}",
        show="templates/kafka-configmap.yaml",
    )
    return docs[0]["data"]


def secret_key_refs(docs: list[dict], secret: str) -> set[str]:
    """Every key named on `secret` by a container env in these docs."""
    keys: set[str] = set()
    for doc in docs:
        spec = doc.get("spec", {})
        pod = spec.get("template", {}).get("spec") or spec.get("jobTemplate", {})
        for container in (pod or {}).get("containers", []):
            for env in container.get("env", []):
                ref = (env.get("valueFrom") or {}).get("secretKeyRef") or {}
                if ref.get("name") == secret:
                    keys.add(ref["key"])
    return keys


def written_keys(docs: list[dict], secret: str) -> set[str]:
    """Every key an ExternalSecret in these docs puts INTO `secret`."""
    keys: set[str] = set()
    for doc in docs:
        if doc.get("kind") == "ExternalSecret":
            spec = doc["spec"]
        elif doc.get("kind") == "ClusterExternalSecret":
            spec = doc["spec"]["externalSecretSpec"]
        else:
            continue
        if spec.get("target", {}).get("name") != secret:
            continue
        keys |= set(spec.get("target", {}).get("template", {}).get("data", {}))
        keys |= {d["secretKey"] for d in spec.get("data", [])}
    return keys


# --- #187: the keys the apps read ------------------------------------------


def test_the_scale_profile_writes_the_keys_its_apps_read() -> None:
    """The defect: turning the projection off dropped username and the mechanism."""
    docs = stack("scale.yaml")
    written = written_keys(docs, "dfe-kafka-user")
    expect(
        "the scale render writes username into dfe-kafka-user",
        "username" in written,
        f"got {sorted(written)}",
    )
    expect(
        "and sasl.mechanism",
        "sasl.mechanism" in written,
        f"got {sorted(written)}",
    )


def test_every_consumer_key_is_one_the_scale_render_supplies() -> None:
    """A secretKeyRef on a missing KEY starts no container and writes no log."""
    docs = stack("scale.yaml")
    read = secret_key_refs(docs, "dfe-kafka-user")
    expect("the consumers do read that secret", read, "no chart referenced dfe-kafka-user")
    written = written_keys(docs, "dfe-kafka-user") | {"password"}
    expect(
        "every key the apps read is written by the same render",
        read <= written,
        f"unwritten: {sorted(read - written)}",
    )


def test_the_keys_do_not_depend_on_the_projection_flag() -> None:
    """username and a mechanism have nothing to do with a cross-namespace copy."""
    for from_store in ("true", "false"):
        docs = render(
            KAFKA,
            "kafka.mode=cluster",
            "kafka.provider=strimzi",
            "appNamespace=dfe-local",
            f"user.password.fromSecretsStore={from_store}",
        )
        written = written_keys(docs, "dfe-kafka-user")
        expect(
            f"fromSecretsStore={from_store} still writes username",
            "username" in written,
            f"got {sorted(written)}",
        )
        expect(
            f"fromSecretsStore={from_store} still writes sasl.mechanism",
            "sasl.mechanism" in written,
            f"got {sorted(written)}",
        )


def test_the_app_keys_merge_rather_than_claim_the_name() -> None:
    """The User Operator owns dfe-kafka-user; a second owner would fight it."""
    docs = render(
        KAFKA,
        "kafka.mode=cluster",
        "kafka.provider=strimzi",
        "appNamespace=dfe-local",
        "user.password.fromSecretsStore=false",
    )
    appkeys = [
        d for d in docs
        if d.get("kind") == "ExternalSecret"
        and d["metadata"]["name"].endswith("-appkeys")
    ]
    expect("the app-keys ExternalSecret renders", len(appkeys) == 1, f"got {len(appkeys)}")
    policy = appkeys[0]["spec"]["target"]["creationPolicy"]
    expect("it merges into the operator's Secret", policy == "Merge", f"got {policy}")


def test_the_single_tier_still_carries_the_same_shape() -> None:
    """Both tiers present one credential shape, so clients are identical."""
    docs = stack("single.yaml")
    written = written_keys(docs, "dfe-kafka-user")
    expect(
        "the single render writes all three keys",
        {"username", "password", "sasl.mechanism"} <= written,
        f"got {sorted(written)}",
    )


# --- #191 and #10: the provider table ---------------------------------------


def test_the_table_derives_the_contract_pair_for_every_provider() -> None:
    for provider, (protocol, mechanism) in CONTRACT_TABLE.items():
        got = derived(provider)
        expect(
            f"{provider} derives {protocol}",
            got["securityProtocol"] == protocol,
            f"got {got['securityProtocol']}",
        )
        expect(
            f"{provider} derives {mechanism or 'no mechanism'}",
            got["saslMechanism"] == mechanism,
            f"got {got['saslMechanism']}",
        )


def test_plain_never_crosses_a_cleartext_transport() -> None:
    """The one hard floor: a PLAIN password rides SASL_SSL or it does not ride."""
    plain = [k for k, (_, mech) in CONTRACT_TABLE.items() if mech == "PLAIN"]
    expect("confluent-cloud is the only PLAIN target", plain == ["confluent-cloud"], f"got {plain}")
    for provider in plain:
        got = derived(provider)
        expect(
            f"{provider} carries PLAIN over SASL_SSL",
            got["securityProtocol"] == "SASL_SSL",
            f"got {got['securityProtocol']}",
        )


def test_the_registry_flag_follows_the_provider() -> None:
    for provider in CONTRACT_TABLE:
        got = derived(provider)["hasSchemaRegistry"]
        expect(
            f"{provider} schema registry is {provider in WITH_REGISTRY}",
            bool(got) is (provider in WITH_REGISTRY),
            f"got {got!r}",
        )


def test_an_unknown_provider_refuses_to_render() -> None:
    """Guessing a default here is how a deployment ends up in the clear."""
    build_harness_deps()
    err = render_error(LIBRARY_HARNESS, "kafka.provider=kinesis")
    expect("an unknown provider fails the render", "kinesis" in err, f"got {err!r}")
    expect(
        "and the message names the accepted keys",
        "strimzi-no-tls" in err and "confluent-cloud" in err,
        f"got {err!r}",
    )


def test_kafbat_matches_the_broker_in_its_own_render() -> None:
    """The defect: one render carried both the wrong protocol and the right one."""
    for profile in ("single.yaml", "scale.yaml"):
        docs = stack(profile)
        config = [
            d for d in docs
            if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("kafbat-config")
        ]
        expect(f"{profile}: kafbat renders a config", len(config) == 1, f"got {len(config)}")
        cluster = yaml.safe_load(config[0]["data"]["application-local.yml"])["kafka"]["clusters"][0]
        expect(
            f"{profile}: kafbat speaks SASL_PLAINTEXT to a DFE-owned broker",
            cluster["properties"]["security.protocol"] == "SASL_PLAINTEXT",
            f"got {cluster['properties']['security.protocol']}",
        )
        expect(
            f"{profile}: on the mechanism the broker serves",
            cluster["properties"]["sasl.mechanism"] == "SCRAM-SHA-512",
            f"got {cluster['properties']['sasl.mechanism']}",
        )


def test_the_matrix_expects_what_the_chart_derives() -> None:
    """deploy_matrix asserts the derivation, so its table must be the contract."""
    expect(
        "the matrix table is the contract table",
        deploy_matrix.PROVIDER_AUTH == CONTRACT_TABLE,
        f"got {deploy_matrix.PROVIDER_AUTH}",
    )
    for provider, (protocol, mechanism) in CONTRACT_TABLE.items():
        got = derived(provider)
        expect(
            f"the chart agrees with the matrix on {provider}",
            (got["securityProtocol"], got["saslMechanism"]) == (protocol, mechanism),
            f"got {got}",
        )


def test_the_matrix_catches_a_hand_set_mechanism() -> None:
    """A check that passes on anything is what #10 asks this to stop being."""
    cell = deploy_matrix.Cell("kafka", "cluster", "scale", ("kafka.provider=strimzi",))
    clean = "  security.protocol: SASL_PLAINTEXT\n  sasl.mechanism: SCRAM-SHA-512\n"
    expect("a derived render passes", not deploy_matrix.assert_derived_auth(cell, clean))
    handset = "  sasl.mechanism: PLAIN\n"
    expect(
        "a hand-set mechanism fails",
        "PLAIN" in deploy_matrix.assert_derived_auth(cell, handset),
        deploy_matrix.assert_derived_auth(cell, handset),
    )
    broker_own = "    sasl.mechanism.inter.broker.protocol=SCRAM-SHA-512\n"
    expect(
        "the broker's own listener keys are not read as client config",
        not deploy_matrix.assert_derived_auth(cell, broker_own),
        deploy_matrix.assert_derived_auth(cell, broker_own),
    )


# --- #190: the note that was wrong in one direction -------------------------


def test_the_provider_env_note_matches_the_vendored_scalo() -> None:
    """The note told a reader the dial might not work; it half did."""
    note = KAFKA_TPL.read_text(encoding="utf-8")
    expect(
        "the stale claim is gone",
        "does not yet read a PROVIDER suffix" not in note,
        "the template still says scalo cannot read the suffix",
    )
    expect(
        "the reader is told what does consume it",
        "from_env" in note and "dfe-transform-elastic" in note,
        "the note names neither the reader nor the one app that uses it",
    )


# --- #9: the external-mode contract -----------------------------------------


def test_naming_the_external_provider_gives_the_shared_secret_shape() -> None:
    """Confluent Cloud's API key IS the username, so a password alone cannot express it."""
    docs = render(
        KAFKA,
        "kafka.mode=external",
        "kafka.external.bootstrap=pkc-1.ap-southeast-2.aws.confluent.cloud:9092",
        "kafka.external.provider=confluent-cloud",
    )
    written = written_keys(docs, "dfe-kafka-external")
    expect(
        "the external credential carries all three keys",
        {"username", "password", "sasl.mechanism"} <= written,
        f"got {sorted(written)}",
    )
    secrets = [d for d in docs if d.get("kind") == "ExternalSecret"]
    mechanism = secrets[0]["spec"]["target"]["template"]["data"]["sasl.mechanism"]
    expect("with PLAIN derived for Confluent Cloud", mechanism == "PLAIN", f"got {mechanism}")


def test_an_unnamed_external_provider_renders_the_pre_contract_shape() -> None:
    """A deployment that seeded only a password keeps resolving."""
    docs = render(KAFKA, "kafka.mode=external", "kafka.external.bootstrap=broker:9092")
    written = written_keys(docs, "dfe-kafka-external")
    expect("only the password is read", written == {"password"}, f"got {sorted(written)}")


def test_iam_stays_quarantined_at_the_external_seam() -> None:
    """msk_iam mints no static credential, so half-naming it renders a broken deploy."""
    err = render_error(
        KAFKA,
        "kafka.mode=external",
        "kafka.external.provider=msk_iam",
        "kafka.external.auth.type=scram",
    )
    expect("provider msk_iam with a static auth type refuses", "msk_iam" in err, f"got {err!r}")
    err = render_error(
        KAFKA,
        "kafka.mode=external",
        "kafka.external.provider=confluent-cloud",
        "kafka.external.auth.type=msk_iam",
    )
    expect("and the reverse mismatch refuses too", "msk_iam" in err, f"got {err!r}")


# --- #446: a managed broker's credential in the app namespace ---------------

# What bootstrap.sh writes on the Argo cluster secret, per broker. The hosts are
# placeholders (RFC 2606): this repo ships publicly.
CLUSTER = {
    "dfe.hyperi.io/dfe_namespace": "dfe-aws",
    "dfe.hyperi.io/env": "test",
    "dfe.hyperi.io/cloud": "aws",
}
BROKERS = {
    "strimzi": {"dfe.hyperi.io/kafka_provider": "strimzi"},
    "msk": {
        "dfe.hyperi.io/kafka_provider": "msk",
        "dfe.hyperi.io/kafka_mode": "external",
        "dfe.hyperi.io/kafka_bootstrap": "b-1.example.invalid:9096,b-2.example.invalid:9096",
        "dfe.hyperi.io/kafka_bootstrap_iam": "b-1.example.invalid:9098,b-2.example.invalid:9098",
    },
    "confluent-cloud": {
        "dfe.hyperi.io/kafka_provider": "confluent-cloud",
        "dfe.hyperi.io/kafka_mode": "external",
        "dfe.hyperi.io/kafka_bootstrap": "pkc-1.example.invalid:9092",
    },
    "redpanda-cloud": {
        "dfe.hyperi.io/kafka_provider": "redpanda-cloud",
        "dfe.hyperi.io/kafka_mode": "external",
        "dfe.hyperi.io/kafka_bootstrap": "seed-1.example.invalid:9092",
    },
}
MANAGED = ("msk", "confluent-cloud", "redpanda-cloud")

# Where apps read dfe-kafka-user: the app namespace, plus otel for the
# collector's kafka_metrics receiver (common.yaml's extraAppNamespaces).
APP_NAMESPACES = {"dfe-aws", "otel"}


def appset_values(broker: str) -> dict:
    """layer2-data's `values:` block, rendered against one broker's cluster secret.

    helm evaluates it with the text/template engine and sprig functions Argo's
    goTemplate uses, and the block reads nothing but `.metadata.annotations`.
    """
    doc = yaml.safe_load(LAYER2_DATA.read_text(encoding="utf-8"))
    block = doc["spec"]["template"]["spec"]["sources"][0]["helm"]["values"]
    cluster = {"cluster": {"metadata": {"annotations": {**CLUSTER, **BROKERS[broker]}}}}
    with tempfile.TemporaryDirectory() as tmp:
        chart = Path(tmp) / "appset-values"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text(
            "apiVersion: v2\nname: appset-values\nversion: 0.0.0\n", encoding="utf-8", newline="\n"
        )
        template = "{{- with .Values.cluster }}\n" + block + "\n{{- end }}\n"
        (chart / "templates" / "values.yaml").write_text(template, encoding="utf-8", newline="\n")
        facts = Path(tmp) / "cluster.yaml"
        facts.write_text(yaml.safe_dump(cluster), encoding="utf-8", newline="\n")
        docs = render(chart, values=(facts,))
    return docs[0] if docs else {}


def layer2_kafka(broker: str, *sets: str) -> list[dict]:
    """The kafka chart as the layer2-data Application renders it for one broker.

    valueFiles, then the appset's values block, then its parameters: Argo's own
    precedence, which `-f` in order followed by `--set` reproduces.
    """
    facts = {**CLUSTER, **BROKERS[broker]}
    with tempfile.TemporaryDirectory() as tmp:
        overlay = Path(tmp) / "appset.yaml"
        overlay.write_text(yaml.safe_dump(appset_values(broker)), encoding="utf-8", newline="\n")
        cascade = (
            ARGO_VALUES / "common.yaml",
            ARGO_VALUES / "aws.yaml",
            ARGO_VALUES / "profile-scale.yaml",
            overlay,
        )
        return render(
            KAFKA,
            f"appNamespace={facts['dfe.hyperi.io/dfe_namespace']}",
            f"env={facts['dfe.hyperi.io/env']}",
            f"cloud={facts['dfe.hyperi.io/cloud']}",
            f"kafka.provider={facts['dfe.hyperi.io/kafka_provider']}",
            *sets,
            values=cascade,
        )


def app_credential(docs: list[dict]) -> dict:
    """The one ClusterExternalSecret writing dfe-kafka-user, or {} if not exactly one."""
    found = [
        d for d in docs
        if d.get("kind") == "ClusterExternalSecret"
        and d["spec"]["externalSecretSpec"]["target"]["name"] == "dfe-kafka-user"
    ]
    return found[0] if len(found) == 1 else {}


def template_data(doc: dict) -> dict:
    """The keys a ClusterExternalSecret templates into its target Secret, {} for no doc."""
    if not doc:
        return {}
    return doc["spec"]["externalSecretSpec"]["target"]["template"]["data"]


def namespaces(doc: dict) -> set[str]:
    selectors = doc.get("spec", {}).get("namespaceSelectors", [])
    return {s["matchLabels"]["kubernetes.io/metadata.name"] for s in selectors}


def store_reads(docs: list[dict]) -> set[tuple[str, str]]:
    """Every (store key, property) an ExternalSecret or ClusterExternalSecret reads."""
    reads: set[tuple[str, str]] = set()
    for doc in docs:
        if doc.get("kind") == "ExternalSecret":
            spec = doc["spec"]
        elif doc.get("kind") == "ClusterExternalSecret":
            spec = doc["spec"]["externalSecretSpec"]
        else:
            continue
        reads |= {(d["remoteRef"]["key"], d["remoteRef"]["property"]) for d in spec.get("data", [])}
    return reads


def test_the_appset_names_a_managed_provider_to_the_chart() -> None:
    """The fact was on the cluster secret; nothing handed it to kafka.external."""
    for broker in MANAGED:
        got = appset_values(broker).get("kafka", {}).get("external", {}).get("provider")
        expect(f"{broker}: the appset sets kafka.external.provider", got == broker, f"got {got!r}")
    got = appset_values("strimzi")
    expect(
        "an in-cluster broker gets no kafka block from the appset",
        "kafka" not in got,
        f"got {got}",
    )


def test_a_managed_broker_projects_its_credential_into_the_app_namespaces() -> None:
    """Every app chart and the otel gateway name dfe-kafka-user in a non-optional secretKeyRef."""
    for broker in MANAGED:
        docs = layer2_kafka(broker)
        doc = app_credential(docs)
        expect(
            f"{broker}: one ClusterExternalSecret writes dfe-kafka-user",
            bool(doc),
            "none rendered",
        )
        expect(
            f"{broker}: into the app namespace and otel",
            namespaces(doc) == APP_NAMESPACES,
            f"got {sorted(namespaces(doc))}",
        )
        written = written_keys(docs, "dfe-kafka-user")
        expect(
            f"{broker}: with every key the apps read",
            written == {"username", "password", "sasl.mechanism"},
            f"got {sorted(written)}",
        )
        # The detail names the expectation, never a value read out of an ExternalSecret spec.
        want = CONTRACT_TABLE[broker][1]
        expect(
            f"{broker}: on the mechanism the provider table derives",
            template_data(doc).get("sasl.mechanism") == want,
            f"want {want!r}",
        )


def test_a_managed_broker_reads_only_the_path_the_deploy_layer_seeds() -> None:
    """ESO reads <store prefix> + key, and tofu writes <ref>/<project>/<env>/<seed>.

    The store prefix is the ref alone (secrets/aws-sm store_config, asserted in
    that module's tftest), so every key read here must be <project>/<env>/<seed>:
    anything else is an ExternalSecret that never syncs.
    """
    for broker in MANAGED:
        seed = next(name for name in render_dial._seeds(broker) if name.startswith("kafka/"))
        keys = {key for key, _ in store_reads(layer2_kafka(broker))}
        expect(
            f"{broker}: the chart reads dfe/test/{seed} and nothing else",
            keys == {f"dfe/test/{seed}"},
            f"got {sorted(keys)}",
        )


def test_only_confluent_cloud_reads_its_username_from_the_store() -> None:
    """Confluent's API key IS the username; a SCRAM principal is a fixed name."""
    reads = {prop for _, prop in store_reads([app_credential(layer2_kafka("confluent-cloud"))])}
    expect(
        "confluent-cloud reads username and password",
        reads == {"username", "password"},
        f"got {sorted(reads)}",
    )
    for broker in ("msk", "redpanda-cloud"):
        doc = app_credential(layer2_kafka(broker))
        reads = {prop for _, prop in store_reads([doc])}
        expect(f"{broker} reads only the password", reads == {"password"}, f"got {sorted(reads)}")
        expect(
            f"{broker} authenticates as dfe-kafka-user",
            template_data(doc).get("username") == "dfe-kafka-user",
            "the templated username differs",
        )


def test_msk_authenticates_as_the_principal_its_acls_name() -> None:
    """The bootstrap Job writes ACLs for msk.scramUsername; the apps must log in as it."""
    doc = app_credential(layer2_kafka("msk", "kafka.external.msk.scramUsername=dfe-cloud"))
    expect(
        "the projected username follows kafka.external.msk.scramUsername",
        template_data(doc).get("username") == "dfe-cloud",
        "the templated username is not dfe-cloud",
    )


def test_the_in_cluster_credential_is_unchanged() -> None:
    """The Strimzi path already worked; this pins it exactly."""
    doc = app_credential(layer2_kafka("strimzi"))
    expected = {
        "externalSecretName": "dfe-kafka-user",
        "refreshTime": "1h",
        "namespaceSelectors": [
            {"matchLabels": {"kubernetes.io/metadata.name": "dfe-aws"}},
            {"matchLabels": {"kubernetes.io/metadata.name": "otel"}},
        ],
        "externalSecretSpec": {
            "refreshInterval": "1h",
            "secretStoreRef": {"name": "dfe-secret-store", "kind": "ClusterSecretStore"},
            "target": {
                "name": "dfe-kafka-user",
                "creationPolicy": "Owner",
                "template": {
                    "engineVersion": "v2",
                    "data": {
                        "username": "dfe-kafka-user",
                        "password": "{{ .password }}",
                        "sasl.mechanism": "SCRAM-SHA-512",
                    },
                },
            },
            "data": [
                {
                    "secretKey": "password",
                    "remoteRef": {"key": "dfe/test/kafka/strimzi", "property": "password"},
                },
            ],
        },
    }
    expect(
        "the Strimzi credential renders exactly as it did",
        doc.get("spec") == expected,
        f"got {doc.get('spec')}",
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    raise SystemExit(main())
