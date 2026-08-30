#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_storage_model.py
#  Purpose:      Prove the deploy-time storage model renders what it promises,
#                refuses what the deployment cannot honour, and that a deploy-repo
#                overlay beats the profile file in the appset's values order.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for clickhouse.storageModel, kafka.storageModel and the cascade.

Three things are checked, and the third is the one that is easy to break:

1. `local` is the default and renders NOTHING extra, so today's deployments are
   untouched.
2. `s3backed` / `tiered` render the disks, the policy and the credential
   environment -- and never a credential literal.
3. A deploy-repo overlay layered LAST beats `profile-*.yaml`. That ordering is
   the whole reason a deployer can reach `mode: external` without editing this
   repo, and nothing else asserts it.

    python3 scripts/tests/test_storage_model.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
VALUES = REPO_ROOT / "argocd" / "values"

# The valueFiles order layer2-data and layer2-platform use, minus the deploy-repo
# layers the individual tests append.
BASE_CASCADE = [VALUES / "common.yaml", VALUES / "local.yaml", VALUES / "profile-scale.yaml"]

_failures = 0


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


def _cmd(chart: str, values: list[Path], sets: tuple[str, ...]) -> list[str]:
    cmd = ["helm", "template", chart, str(CHARTS / chart)]
    for v in values:
        cmd += ["-f", str(v)]
    # The appsets pass appNamespace as a helm parameter, not a values file; the
    # kafka chart refuses to render without it.
    cmd += ["--set", "appNamespace=dfe-local"]
    for s in sets:
        cmd += ["--set", s]
    return cmd


def render(chart: str, *sets: str, values: list[Path] | None = None) -> list[dict]:
    out = subprocess.run(
        _cmd(chart, values or BASE_CASCADE, sets), capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def render_error(chart: str, *sets: str) -> str:
    """The stderr of a render that MUST fail. Empty string means it did not."""
    out = subprocess.run(_cmd(chart, BASE_CASCADE, sets), capture_output=True, text=True, check=False)
    return "" if out.returncode == 0 else out.stderr


def one(docs: list[dict], kind: str) -> dict:
    matches = [d for d in docs if d.get("kind") == kind]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one {kind}, got {len(matches)}")
    return matches[0]


def kinds(docs: list[dict]) -> set[str]:
    return {d.get("kind", "") for d in docs}


S3_SETS = (
    "clickhouse.storageModel=s3backed",
    "clickhouse.s3.endpoint=https://dfe-ch.s3.ap-southeast-2.amazonaws.com/parts/",
)
TIERED_SETS = (
    "kafka.storageModel=tiered",
    "kafka.tiered.className=io.aiven.kafka.tieredstorage.RemoteStorageManager",
)


def test_clickhouse_local_adds_nothing() -> None:
    ch = one(render("clickhouse-cluster"), "ClickHouseCluster")
    extra = ch["spec"]["settings"]["extraConfig"]
    expect("local CH declares no storage_configuration", "storage_configuration" not in extra)
    expect("local CH declares no merge_tree default policy", "merge_tree" not in extra)
    expect(
        "local CH mints no object-store secret",
        "dfe-clickhouse-s3" not in yaml.safe_dump(render("clickhouse-cluster")),
    )


def test_clickhouse_s3backed_renders_the_disks() -> None:
    docs = render("clickhouse-cluster", *S3_SETS)
    extra = one(docs, "ClickHouseCluster")["spec"]["settings"]["extraConfig"]
    disks = extra["storage_configuration"]["disks"]
    expect("s3backed defines an object disk and a cache disk", set(disks) == {"s3_object", "s3_cache"},
           f"got {sorted(disks)}")
    expect("the cache fronts the object disk", disks["s3_cache"]["disk"] == "s3_object")
    expect("MergeTree defaults to the cached policy",
           extra["merge_tree"]["storage_policy"] == "s3_cached")


def test_clickhouse_s3backed_keeps_credentials_out_of_git() -> None:
    docs = render("clickhouse-cluster", *S3_SETS)
    disk = one(docs, "ClickHouseCluster")["spec"]["settings"]["extraConfig"][
        "storage_configuration"]["disks"]["s3_object"]
    expect("the disk takes credentials from the environment",
           disk.get("use_environment_credentials") is True)
    expect("the disk carries no inline key",
           not {"access_key_id", "secret_access_key"} & set(disk), f"got {sorted(disk)}")
    env = one(docs, "ClickHouseCluster")["spec"]["containerTemplate"]["env"]
    expect("both credential vars come from a secretKeyRef",
           all("secretKeyRef" in e["valueFrom"] for e in env), f"got {env}")
    expect("an ExternalSecret materialises them", "ExternalSecret" in kinds(docs))


def test_clickhouse_s3backed_reaches_single_mode() -> None:
    docs = render("clickhouse-cluster", "clickhouse.mode=single", *S3_SETS)
    cm = [d for d in docs if d.get("kind") == "ConfigMap"][0]
    expect("single mode ships the same fragment as config.d YAML",
           "dfe-storage.yaml" in cm["data"])
    fragment = yaml.safe_load(cm["data"]["dfe-storage.yaml"])
    expect("the single-mode fragment names the same policy",
           fragment["merge_tree"]["storage_policy"] == "s3_cached")


def test_clickhouse_storage_model_guards() -> None:
    expect("an unknown model is refused",
           "must be local or s3backed" in render_error("clickhouse-cluster",
                                                       "clickhouse.storageModel=glacier"))
    expect("s3backed with no endpoint is refused",
           "needs clickhouse.s3.endpoint" in render_error("clickhouse-cluster",
                                                          "clickhouse.storageModel=s3backed"))
    expect("s3backed on an external ClickHouse is refused",
           "meaningless with mode=external" in render_error("clickhouse-cluster",
                                                            "clickhouse.mode=external", *S3_SETS))


def test_kafka_local_adds_nothing() -> None:
    kafka = one(render("kafka"), "Kafka")
    expect("local Kafka declares no tieredStorage", "tieredStorage" not in kafka["spec"]["kafka"])
    expect("local Kafka leaves the remote-log switch off",
           "remote.log.storage.system.enable" not in kafka["spec"]["kafka"]["config"])
    topics = [d for d in render("kafka") if d.get("kind") == "KafkaTopic"]
    expect("no topic asks for remote storage",
           all("remote.storage.enable" not in (t["spec"].get("config") or {}) for t in topics))


def test_kafka_tiered_renders_the_plugin_and_the_switches() -> None:
    docs = render("kafka", *TIERED_SETS)
    spec = one(docs, "Kafka")["spec"]["kafka"]
    expect("tiered storage is Strimzi's custom type", spec["tieredStorage"]["type"] == "custom")
    expect("the plugin class is passed through",
           spec["tieredStorage"]["remoteStorageManager"]["className"].endswith("RemoteStorageManager"))
    expect("the broker-wide switch is on",
           spec["config"]["remote.log.storage.system.enable"] == "true")
    landing = [d for d in docs if d.get("kind") == "KafkaTopic" and d["spec"]["topicName"].endswith("_land")]
    expect("the landing topic opts in per-topic",
           all(t["spec"]["config"]["remote.storage.enable"] == "true" for t in landing),
           f"got {[t['spec'].get('config') for t in landing]}")
    dlq = [d for d in docs if d.get("kind") == "KafkaTopic" and "dlq" in d["spec"]["topicName"]]
    expect("the DLQ topics do not tier",
           all("remote.storage.enable" not in t["spec"]["config"] for t in dlq))


def test_kafka_tiered_keeps_credentials_out_of_git() -> None:
    docs = render("kafka", *TIERED_SETS)
    env = one(docs, "Kafka")["spec"]["kafka"]["template"]["kafkaContainer"]["env"]
    expect("both credential vars come from a secretKeyRef",
           all("secretKeyRef" in e["valueFrom"] for e in env), f"got {env}")
    names = {d["metadata"]["name"] for d in docs if d.get("kind") == "ExternalSecret"}
    expect("an ExternalSecret materialises them", "dfe-kafka-tiered" in names, f"got {sorted(names)}")


def test_kafka_tiered_version_gate() -> None:
    err = render_error("kafka", *TIERED_SETS, "kafka.operatorVersion=0.37.0")
    expect("an operator too old for tiered storage fails the render",
           "needs Strimzi >= 0.38.0" in err, f"got {err[:200]}")
    expect("the failure names the version in use", "0.37.0" in err, f"got {err[:200]}")
    expect("the pinned operator version passes the gate",
           render_error("kafka", *TIERED_SETS) == "")


def test_kafka_tiered_other_guards() -> None:
    expect("an unknown model is refused",
           "must be local or tiered" in render_error("kafka", "kafka.storageModel=glacier"))
    expect("tiered without a plugin class is refused",
           "needs kafka.tiered.className" in render_error("kafka", "kafka.storageModel=tiered"))
    expect("tiered on the single tier is refused",
           "needs kafka.mode=cluster" in render_error("kafka", *TIERED_SETS, "kafka.mode=single"))
    expect("tiered on redpanda is refused",
           "Strimzi Kafka CR field" in render_error("kafka", *TIERED_SETS, "kafka.provider=redpanda"))


def test_deploy_repo_overlay_beats_the_profile() -> None:
    """The cascade order the appsets use: the deploy repo's infra/ layer is LAST.

    profile-scale.yaml declares clickhouse.mode=cluster. An overlay layered after
    it must flip the derived external-service egress on, or a deployer cannot
    reach mode=external without editing this repo.
    """
    with tempfile.TemporaryDirectory() as tmp:
        overlay = Path(tmp) / "common.yaml"
        overlay.write_text("clickhouse:\n  mode: external\n", encoding="utf-8", newline="\n")
        docs = render("network-policies", values=[*BASE_CASCADE, overlay])
    derived = {
        d["metadata"]["name"]
        for d in docs
        if d.get("kind") == "NetworkPolicy"
        and d["metadata"]["name"].startswith("allow-external-clickhouse-egress-")
    }
    expect("the overlay's mode=external reaches network-policies", derived != set())
    expect("it opens the CH clients, not the internet-egress apps",
           not any(n.endswith(("dfe-fetcher", "dfe-archiver")) for n in derived),
           f"got {sorted(derived)}")
    baseline = render("network-policies")
    expect("the profile alone opens nothing",
           not [d for d in baseline if d.get("kind") == "NetworkPolicy"
                and d["metadata"]["name"].startswith("allow-external-clickhouse-egress-")])


def main() -> int:
    test_clickhouse_local_adds_nothing()
    test_clickhouse_s3backed_renders_the_disks()
    test_clickhouse_s3backed_keeps_credentials_out_of_git()
    test_clickhouse_s3backed_reaches_single_mode()
    test_clickhouse_storage_model_guards()
    test_kafka_local_adds_nothing()
    test_kafka_tiered_renders_the_plugin_and_the_switches()
    test_kafka_tiered_keeps_credentials_out_of_git()
    test_kafka_tiered_version_gate()
    test_kafka_tiered_other_guards()
    test_deploy_repo_overlay_beats_the_profile()
    print(f"\n{_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
