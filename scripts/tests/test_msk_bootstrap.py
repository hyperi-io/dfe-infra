#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_msk_bootstrap.py
#  Purpose:      Prove the MSK bootstrap Job renders only for MSK, writes exactly
#                the on-prem ACL set, creates no topic, and carries no credential
#                of its own.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The in-cluster bootstrap for AWS MSK.

MSK ACLs live in the Kafka data plane, so nothing in a tofu apply can write the
first one. The kafka chart writes them from a Job that authenticates by SASL/IAM
-- and three things about that Job are worth asserting, because none of them
fails loudly:

1. It renders for MSK ALONE. Pointed at Strimzi or Redpanda it would grant a
   principal those brokers have never heard of, and a Job that runs where no
   MSK exists just sits in backoff until the Application reports degraded.
2. The grants match on-prem's KafkaUser grant for grant. An extra cluster
   operation renders green and passes every schema: the only check that bites
   is naming the allowed set and refusing the rest.
3. It creates NO topic. dfe-schemas declares the bootstrap topic set and
   dfe-engine creates it at its own startup, on every tier -- a second creator
   here would be a per-tier difference in who owns a topic, which is exactly
   what engine-only schema control removed.

    python3 scripts/tests/test_msk_bootstrap.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "kafka"
VALUES = REPO_ROOT / "argocd" / "values"
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
LAYER2_DATA = REPO_ROOT / "argocd" / "appsets" / "layer2-data.yaml"

# The valueFiles order a cloud deploy layers, minus the deploy-repo overlay each
# test supplies as its own values dict.
BASE_CASCADE = [VALUES / "common.yaml", VALUES / "profile-scale.yaml"]

# What the deploy carries in from the managed-kafka/msk module's outputs. The
# bootstrap string is a two-broker one because that is the shape MSK emits, and a
# comma in a --set is exactly where a hand-rolled render goes wrong.
MSK_VALUES = {
    "appNamespace": "dfe-aws",
    "kafka": {
        "mode": "external",
        "provider": "msk",
        "external": {
            "bootstrap": "b-1.dfe.example:9096,b-2.dfe.example:9096",
            "msk": {"bootstrapIam": "b-1.dfe.example:9098,b-2.dfe.example:9098"},
        },
        "landingTopics": {"sources": [{"name": "okta"}, {"name": "m365", "partitions": 24}]},
    },
}

# The on-prem grants, read from helm/charts/kafka/templates/kafka-user.yaml.
# Alter raises a topic's partition count; Delete is what deleting a source needs
# to take its _land/_load pair with it.
TOPIC_OPERATIONS = {"Read", "Write", "Create", "Describe", "Alter", "Delete"}
GROUP_OPERATIONS = {"Read", "Describe"}
# What on-prem deliberately withholds. A cluster operation is not in this set
# because the word "Cluster" is checked on its own below.
WITHHELD_OPERATIONS = {"AlterConfigs", "ClusterAction", "IdempotentWrite", "All"}


def merged(*overlays: dict) -> dict:
    """MSK_VALUES with each overlay merged over it, deeply."""

    def deep(base: dict, over: dict) -> dict:
        out = dict(base)
        for k, v in over.items():
            out[k] = deep(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        return out

    out = MSK_VALUES
    for overlay in overlays:
        out = deep(out, overlay)
    return out


def render(values: dict | None = None) -> list[dict]:
    """Every document the chart renders under the cascade plus these values."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(values if values is not None else MSK_VALUES, fh)
        overlay = fh.name
    cmd = ["helm", "template", "kafka", str(CHART)]
    for v in [*BASE_CASCADE, Path(overlay)]:
        cmd += ["-f", str(v)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    Path(overlay).unlink()
    if out.returncode != 0:
        raise SystemExit(f"helm template failed:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def jobs(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d.get("kind") == "Job"]


def bootstrap_job(docs: list[dict]) -> dict:
    matches = [d for d in jobs(docs) if "msk-bootstrap" in d["metadata"]["name"]]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one MSK bootstrap Job, got {len(matches)}")
    return matches[0]


def script(job: dict) -> str:
    return job["spec"]["template"]["spec"]["containers"][0]["command"][2]


def commands(job: dict) -> list[str]:
    """The script's real commands -- a comment naming a flag is not one."""
    return [ln.strip() for ln in script(job).splitlines() if not ln.strip().startswith("#")]


def acl_lines(job: dict) -> list[str]:
    return [ln for ln in commands(job) if "kafka-acls.sh" in ln]


def test_the_job_and_its_account_render_for_msk() -> None:
    docs = render()
    job = bootstrap_job(docs)
    accounts = [d for d in docs if d.get("kind") == "ServiceAccount"]
    expect("one ServiceAccount renders with the Job", len(accounts) == 1, f"got {len(accounts)}")
    expect(
        "the Job runs as that account, which is what the Pod Identity association names",
        job["spec"]["template"]["spec"]["serviceAccountName"] == accounts[0]["metadata"]["name"],
        f"{job['spec']['template']['spec'].get('serviceAccountName')} vs {accounts[0]['metadata']['name']}",
    )
    expect(
        "the account name is a value, not a literal",
        accounts[0]["metadata"]["name"] == "dfe-kafka-bootstrap",
        accounts[0]["metadata"]["name"],
    )


def test_the_job_renders_for_msk_alone() -> None:
    for provider in ("strimzi", "redpanda"):
        # Redpanda refuses to render at all without its BSL acknowledgement, and
        # that refusal is a different check's -- accept it so this one sees the
        # provider gate rather than the licence gate.
        docs = render(merged({"kafka": {"provider": provider, "redpanda": {"acceptLicense": True}}}))
        expect(
            f"external mode on {provider} renders no MSK Job",
            not [d for d in jobs(docs) if "msk-bootstrap" in d["metadata"]["name"]],
        )
        expect(
            f"external mode on {provider} renders no bootstrap ServiceAccount",
            not [d for d in docs if d.get("kind") == "ServiceAccount"],
        )
    for mode in ("cluster", "single", "disabled"):
        docs = render(merged({"kafka": {"mode": mode}}))
        expect(
            f"{mode} mode renders no MSK Job even with provider msk",
            not [d for d in jobs(docs) if "msk-bootstrap" in d["metadata"]["name"]],
        )


def test_the_job_needs_somewhere_to_connect_and_an_off_switch() -> None:
    docs = render(merged({"kafka": {"external": {"msk": {"bootstrapIam": ""}}}}))
    expect("no IAM bootstrap string means no Job", not jobs(docs))
    docs = render(merged({"kafka": {"external": {"msk": {"bootstrap": {"enabled": False}}}}}))
    expect("the off switch bites", not jobs(docs))


def test_the_acls_are_the_on_prem_grants_and_nothing_more() -> None:
    lines = acl_lines(bootstrap_job(render()))
    adds = [ln for ln in lines if "--add" in ln]
    expect("exactly two --add commands", len(adds) == 2, f"got {len(adds)}")
    topic = [ln for ln in adds if "--topic" in ln]
    group = [ln for ln in adds if "--group" in ln]
    expect("one topic grant", len(topic) == 1, f"got {topic}")
    expect("one group grant", len(group) == 1, f"got {group}")

    def operations(line: str) -> set[str]:
        parts = line.split()
        return {parts[i + 1] for i, p in enumerate(parts) if p == "--operation"}

    expect("the topic grant is Read/Write/Create/Describe/Alter/Delete",
           operations(topic[0]) == TOPIC_OPERATIONS, f"got {operations(topic[0])}")
    expect("the group grant is Read/Describe",
           operations(group[0]) == GROUP_OPERATIONS, f"got {operations(group[0])}")
    expect('the topic resource is the literal "*"',
           '--topic "*" --resource-pattern-type literal' in topic[0], topic[0])
    expect('the group resource is the "dfe-" PREFIX, with no glob',
           '--group "dfe-" --resource-pattern-type prefixed' in group[0], group[0])
    expect("both grants name the SCRAM principal",
           all('--allow-principal "User:dfe-kafka-user"' in ln for ln in adds), f"got {adds}")
    granted = operations(topic[0]) | operations(group[0])
    expect("nothing on-prem withholds is granted",
           not granted & WITHHELD_OPERATIONS, f"got {granted & WITHHELD_OPERATIONS}")
    expect("no cluster-scoped grant", "--cluster" not in " ".join(adds), f"got {adds}")
    expect("the resulting ACLs are printed", any("--list" in ln for ln in lines), f"got {lines}")


def test_the_job_grants_exactly_what_the_kafkauser_grants() -> None:
    """Read the on-prem set off the KafkaUser rather than restating it here.

    One principal serves both, so the two drifting apart is a per-cloud
    difference in what dfe-engine may do -- and the comment in each file has
    said "grant for grant" since before either had Alter.
    """
    docs = render(merged({"kafka": {"mode": "cluster", "provider": "strimzi"}}))
    users = [d for d in docs if d.get("kind") == "KafkaUser"]
    expect("the strimzi path renders one KafkaUser", len(users) == 1, f"got {len(users)}")
    acls = {a["resource"]["type"]: set(a["operations"]) for a in users[0]["spec"]["authorization"]["acls"]}
    expect("the KafkaUser topic grant is the set this file names",
           acls["topic"] == TOPIC_OPERATIONS, f"got {acls['topic']}")
    expect("the KafkaUser group grant is the set this file names",
           acls["group"] == GROUP_OPERATIONS, f"got {acls['group']}")


def test_the_principal_is_a_value() -> None:
    job = bootstrap_job(render(merged({"kafka": {"external": {"msk": {"scramUsername": "dfe-cloud"}}}})))
    expect("the renamed principal reaches both grants",
           script(job).count('--allow-principal "User:dfe-cloud"') == 2, script(job))


def test_the_job_creates_no_topic() -> None:
    """The ACL half is the only half. dfe-engine creates every topic, on every tier."""
    job = bootstrap_job(render())
    body = script(job)
    expect("no kafka-topics.sh call", "kafka-topics.sh" not in body, body)
    expect("no --create flag", "--create" not in body, body)
    expect("no topic name reaches the Job",
           "main_land" not in body and "okta_land" not in body and "_dlq" not in body, body)


def test_the_iam_jar_is_pinned_and_verified() -> None:
    job = bootstrap_job(render())
    pod = job["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    fetch = init["command"][2]
    expect("the jar is fetched by an init container", len(pod["initContainers"]) == 1)
    expect("the pinned version is in the URL",
           "releases/download/v2.3.8/aws-msk-iam-auth-2.3.8-all.jar" in fetch, fetch)
    expect("the published checksum is pinned",
           "8c7b14f9fc4c9dc3f78837a970d84f9303b5bf849d272c6c414e5ee49f3e5ac4" in fetch, fetch)
    expect("the checksum is checked, not just carried", "sha256sum" in fetch, fetch)
    expect("a mismatch fails the Job", "exit 1" in fetch, fetch)
    expect("the CLI loads the verified jar off the shared volume",
           any(e["name"] == "CLASSPATH" and e["value"].startswith("/opt/msk-iam/")
               for e in pod["containers"][0]["env"]),
           repr(pod["containers"][0]["env"]))
    mirrored = bootstrap_job(render(merged({"kafka": {"external": {"msk": {"bootstrap": {
        "iamAuth": {"url": "https://mirror.example/aws-msk-iam-auth-2.3.8-all.jar"}}}}}})))
    expect("an air-gapped deploy can point the URL at a mirror",
           "https://mirror.example/aws-msk-iam-auth-2.3.8-all.jar"
           in mirrored["spec"]["template"]["spec"]["initContainers"][0]["command"][2])


def test_the_job_carries_no_credential() -> None:
    job = bootstrap_job(render())
    text = yaml.safe_dump(job)
    expect("no Secret is mounted or projected", "secretKeyRef" not in text, text[:400])
    expect("no static AWS key", "AWS_SECRET_ACCESS_KEY" not in text)
    expect("the login module is the IAM one, with no inline credential",
           "software.amazon.msk.auth.iam.IAMLoginModule required;" in script(job), script(job))
    expect("SASL_SSL over AWS_MSK_IAM",
           "security.protocol=SASL_SSL" in script(job) and "sasl.mechanism=AWS_MSK_IAM" in script(job))


def test_the_job_is_tracked_and_bounded() -> None:
    docs = render()
    job = bootstrap_job(docs)
    account = next(d for d in docs if d.get("kind") == "ServiceAccount")
    annotations = job["metadata"].get("annotations", {})
    expect("the Job is a tracked resource, not an Argo hook",
           "argocd.argoproj.io/hook" not in annotations, repr(annotations))
    expect("the account syncs before the Job that names it",
           int(account["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
           < int(annotations["argocd.argoproj.io/sync-wave"]))
    expect("backoffLimit is a value", job["spec"]["backoffLimit"] == 5, repr(job["spec"]))
    expect("ttlSecondsAfterFinished is a value",
           job["spec"]["ttlSecondsAfterFinished"] == 600, repr(job["spec"]))
    expect("a changed principal is a NEW Job name",
           bootstrap_job(render(merged({"kafka": {"external": {"msk": {"scramUsername": "dfe-cloud"}}}})))
           ["metadata"]["name"] != job["metadata"]["name"])


# --- the pipeline that reaches the chart: bootstrap.sh -> cluster secret -----
# --- -> the layer2-data appset. gate-3-correctness.md P1-5: the Job and its ---
# --- RBAC existed and were unreachable -- nothing set mode=external or -------
# --- carried bootstrapIam this far. --------------------------------------


def test_bootstrap_derives_kafka_mode_for_every_managed_provider() -> None:
    """external only for msk/confluent-cloud/redpanda-cloud -- strimzi and
    redpanda run in-cluster and must keep the profile overlay's own mode."""
    body = BOOTSTRAP.read_text(encoding="utf-8")
    expect(
        "DFE_KAFKA_MODE is derived, not accepted as an input",
        'DFE_KAFKA_MODE=""' in body and "case \"${DFE_KAFKA_PROVIDER}\" in" in body,
        "the derivation is missing",
    )
    expect(
        "all three managed providers flip it to external",
        "msk|confluent-cloud|redpanda-cloud) DFE_KAFKA_MODE=\"external\" ;;" in body,
        "the case arm is missing or lists the wrong providers",
    )
    expect("the derived value is exported", 'export DFE_KAFKA_MODE' in body)


def test_bootstrap_defaults_the_tofu_outputs_it_carries_forward() -> None:
    """Empty by default so the annotations below always render, on every
    provider but msk -- the same pattern DFE_KAFKA_BOOTSTRAP already uses."""
    body = BOOTSTRAP.read_text(encoding="utf-8")
    for key in ("DFE_KAFKA_BOOTSTRAP_IAM", "DFE_KAFKA_BOOTSTRAP_ROLE_ARN", "DFE_KAFKA_CREDENTIAL_REF"):
        expect(f"{key} is defaulted empty", f'export {key}="${{{key}:-}}"' in body, f"{key} missing")


def test_the_cluster_secret_carries_every_new_kafka_annotation() -> None:
    body = CLUSTER_SECRET.read_text(encoding="utf-8")
    for annotation, env_key in (
        ("dfe.hyperi.io/kafka_mode", "DFE_KAFKA_MODE"),
        ("dfe.hyperi.io/kafka_bootstrap_iam", "DFE_KAFKA_BOOTSTRAP_IAM"),
        ("dfe.hyperi.io/kafka_bootstrap_role_arn", "DFE_KAFKA_BOOTSTRAP_ROLE_ARN"),
        ("dfe.hyperi.io/kafka_credential_ref", "DFE_KAFKA_CREDENTIAL_REF"),
    ):
        expect(
            f"{annotation} is set from ${{{env_key}}}",
            f'{annotation}: "${{{env_key}}}"' in body,
            "annotation missing from the template",
        )


def test_the_appset_turns_the_annotations_into_the_chart_values_that_render_the_job() -> None:
    """A `parameters:` entry is a structured list this file's own tests parse
    as plain YAML before Go templating runs, so a per-element conditional
    cannot live there -- this is a `values:` block string instead, the same
    pattern layer2-platform.yaml uses for karpenter-pools."""
    doc = yaml.safe_load(LAYER2_DATA.read_text(encoding="utf-8"))
    kafka_source = doc["spec"]["template"]["spec"]["sources"][0]
    values_block = kafka_source["helm"]["values"]
    expect(
        "the block is gated on a managed broker's own annotations",
        "{{- if or $kafkaMode $bootstrapIam }}" in values_block,
        values_block,
    )
    expect(
        "kafka.mode reads the kafka_mode annotation",
        "mode: {{ $kafkaMode | quote }}" in values_block
        and 'index .metadata.annotations "dfe.hyperi.io/kafka_mode"' in values_block,
        values_block,
    )
    expect(
        "kafka.external.msk.bootstrapIam reads the kafka_bootstrap_iam annotation",
        "bootstrapIam: {{ $bootstrapIam | quote }}" in values_block
        and 'index .metadata.annotations "dfe.hyperi.io/kafka_bootstrap_iam"' in values_block,
        values_block,
    )
    expect(
        "both nest under one kafka: key, so neither overwrites the other in the merged values",
        values_block.count("kafka:") == 1,
        values_block,
    )


def main() -> int:
    with standalone():
        test_the_job_and_its_account_render_for_msk()
        test_the_job_renders_for_msk_alone()
        test_the_job_needs_somewhere_to_connect_and_an_off_switch()
        test_the_acls_are_the_on_prem_grants_and_nothing_more()
        test_the_job_grants_exactly_what_the_kafkauser_grants()
        test_the_principal_is_a_value()
        test_the_job_creates_no_topic()
        test_the_iam_jar_is_pinned_and_verified()
        test_the_job_carries_no_credential()
        test_the_job_is_tracked_and_bounded()
        test_bootstrap_derives_kafka_mode_for_every_managed_provider()
        test_bootstrap_defaults_the_tofu_outputs_it_carries_forward()
        test_the_cluster_secret_carries_every_new_kafka_annotation()
        test_the_appset_turns_the_annotations_into_the_chart_values_that_render_the_job()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
