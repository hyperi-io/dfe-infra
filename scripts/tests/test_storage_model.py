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
2. Every non-local model renders the disks and the policy it promises --
   ClickHouse `s3backed` and `tiered`, Kafka `tiered` -- and the object-store
   models render the credential environment and never a credential literal.
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
# ClickHouse hot/cold on two local volumes. The cold dials keep their defaults
# except where a test names them, so the defaults stay exercised.
CH_TIERED_SETS = ("clickhouse.storageModel=tiered",)


def test_clickhouse_local_adds_nothing() -> None:
    ch = one(render("clickhouse-cluster"), "ClickHouseCluster")
    extra = ch["spec"]["settings"]["extraConfig"]
    expect("local CH declares no storage_configuration", "storage_configuration" not in extra)
    expect("local CH declares no merge_tree default policy", "merge_tree" not in extra)
    expect("local CH claims one volume and no other",
           "additionalVolumeClaimTemplates" not in ch["spec"])
    expect(
        "local CH mints no object-store secret",
        "dfe-clickhouse-s3" not in yaml.safe_dump(render("clickhouse-cluster")),
    )


def test_clickhouse_s3backed_renders_the_disks() -> None:
    docs = render("clickhouse-cluster", *S3_SETS)
    extra = one(docs, "ClickHouseCluster")["spec"]["settings"]["extraConfig"]
    disks = extra["storage_configuration"]["disks"]
    expect(
        "s3backed defines an object disk and a cache disk",
        set(disks) == {"s3_object", "s3_object_cache"},
        f"got {sorted(disks)}",
    )
    expect("the cache fronts the object disk", disks["s3_object_cache"]["disk"] == "s3_object")
    expect("MergeTree defaults to the cached policy",
           extra["merge_tree"]["storage_policy"] == "s3_cached")


def test_the_cache_disk_sorts_after_the_disk_it_wraps() -> None:
    """The operator serialises extraConfig as sorted-key JSON, and ClickHouse
    builds a cache only after its backing disk. Names carry that ordering, so a
    rename that sorts the cache first crashes the server at startup."""
    docs = render("clickhouse-cluster", *S3_SETS)
    disks = one(docs, "ClickHouseCluster")["spec"]["settings"]["extraConfig"][
        "storage_configuration"
    ]["disks"]
    for name, spec in disks.items():
        backing = spec.get("disk")
        if backing is None:
            continue
        expect(
            f"cache disk {name} sorts after its backing disk {backing}",
            backing < name,
            f"{backing} does not sort before {name}",
        )


def _external_secret(docs: list[dict], name: str) -> dict:
    for doc in docs:
        if doc.get("kind") == "ExternalSecret" and doc["metadata"]["name"] == name:
            return doc
    raise SystemExit(f"no ExternalSecret {name} in the render")


def test_the_credential_binding_defaults_to_the_dfe_seeded_path() -> None:
    """Unset values must keep the path every seeded deployment already uses."""
    for chart, sets, secret, seeded in (
        ("clickhouse-cluster", S3_SETS, "dfe-clickhouse-s3", "dfe/local/clickhouse/s3"),
        ("kafka", TIERED_SETS, "dfe-kafka-tiered", "dfe/local/kafka/tiered"),
    ):
        entries = _external_secret(render(chart, *sets), secret)["spec"]["data"]
        expect(
            f"{chart} defaults the remote key to {seeded}",
            {e["remoteRef"]["key"] for e in entries} == {seeded},
            f"got {[e['remoteRef']['key'] for e in entries]}",
        )
        expect(
            f"{chart} defaults the properties to the DFE field names",
            [e["remoteRef"]["property"] for e in entries] == ["access_key_id", "secret_access_key"],
            f"got {[e['remoteRef']['property'] for e in entries]}",
        )


def test_the_credential_binding_follows_an_existing_store_entry() -> None:
    """A deployment reusing an estate credential cannot re-seed it under a DFE
    path, so the remote key and its field names have to be values."""
    for chart, sets, prefix, secret in (
        ("clickhouse-cluster", S3_SETS, "clickhouse.s3", "dfe-clickhouse-s3"),
        ("kafka", TIERED_SETS, "kafka.tiered", "dfe-kafka-tiered"),
    ):
        entries = _external_secret(
            render(
                chart,
                *sets,
                f"{prefix}.remoteKey=services/minio",
                f"{prefix}.accessKeyProperty=dfe_storage_tests_access_key",
                f"{prefix}.secretKeyProperty=dfe_storage_tests_secret_key",
                f"{prefix}.secretStoreName=openbao",
            ),
            secret,
        )["spec"]
        expect(
            f"{chart} binds the supplied remote key",
            {e["remoteRef"]["key"] for e in entries["data"]} == {"services/minio"},
            f"got {[e['remoteRef']['key'] for e in entries['data']]}",
        )
        expect(
            f"{chart} binds the supplied field names",
            [e["remoteRef"]["property"] for e in entries["data"]]
            == ["dfe_storage_tests_access_key", "dfe_storage_tests_secret_key"],
            f"got {[e['remoteRef']['property'] for e in entries['data']]}",
        )
        expect(
            f"{chart} binds the store that mounts them",
            entries["secretStoreRef"]["name"] == "openbao",
            f"got {entries['secretStoreRef']['name']}",
        )
        expect(
            f"{chart} leaves the local secretKey names alone",
            [e["secretKey"] for e in entries["data"]] == ["access_key_id", "secret_access_key"],
            f"got {[e['secretKey'] for e in entries['data']]}",
        )


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


def test_the_object_store_timeouts_are_unset_by_default_and_settable() -> None:
    """Left at the server defaults a read against an unreachable store blocked
    over nine minutes on the rig; 1 / 2000 / 5000 failed the same read in 10.4s.
    The dials must therefore exist, and must change nothing until they are set."""
    bounded = {"s3_retry_attempts", "s3_connect_timeout_ms", "s3_request_timeout_ms"}
    disk = one(render("clickhouse-cluster", *S3_SETS), "ClickHouseCluster")["spec"]["settings"][
        "extraConfig"]["storage_configuration"]["disks"]["s3_object"]
    expect("the defaults leave the server's own retry behaviour alone",
           not bounded & set(disk), f"got {sorted(disk)}")
    tuned = one(
        render(
            "clickhouse-cluster",
            *S3_SETS,
            "clickhouse.s3.retryAttempts=1",
            "clickhouse.s3.connectTimeoutMs=2000",
            "clickhouse.s3.requestTimeoutMs=5000",
        ),
        "ClickHouseCluster",
    )["spec"]["settings"]["extraConfig"]["storage_configuration"]["disks"]["s3_object"]
    expect("each bound reaches the disk",
           [tuned.get(k) for k in ("s3_retry_attempts", "s3_connect_timeout_ms",
                                   "s3_request_timeout_ms")] == [1, 2000, 5000],
           f"got {tuned}")


def test_clickhouse_tiered_ranks_two_local_volumes() -> None:
    docs = render(
        "clickhouse-cluster",
        *CH_TIERED_SETS,
        "clickhouse.tiered.coldStorageClass=nvme-bulk",
        "clickhouse.tiered.coldSize=4Ti",
    )
    ch = one(docs, "ClickHouseCluster")
    claims = ch["spec"]["additionalVolumeClaimTemplates"]
    expect("tiered claims exactly one extra volume", len(claims) == 1, f"got {claims}")
    expect("the claim is named for the cold disk", claims[0]["metadata"]["name"] == "slow",
           f"got {claims[0]['metadata']['name']}")
    expect("the claim carries the requested class",
           claims[0]["spec"]["storageClassName"] == "nvme-bulk", f"got {claims[0]['spec']}")
    expect("the claim carries the requested size",
           claims[0]["spec"]["resources"]["requests"]["storage"] == "4Ti", f"got {claims[0]['spec']}")
    policy = ch["spec"]["settings"]["extraConfig"]["storage_configuration"]["policies"]["default"]
    expect("the policy replaces the operator's generated JBOD one",
           policy["@replace"] == "1", f"got {policy}")
    expect("moveFactor lands on the policy", policy["move_factor"] == 0.2, f"got {policy}")
    expect("both volumes are ranked in one policy",
           set(policy["volumes"]) == {"default", "slow"}, f"got {sorted(policy['volumes'])}")
    expect("each volume names its own disk",
           [v["disk"] for v in policy["volumes"].values()] == list(policy["volumes"]),
           f"got {policy['volumes']}")


def test_the_cold_volume_sorts_after_the_hot_one() -> None:
    """Volume order IS tier priority and the API server re-serialises the policy
    with sorted keys, so a cold volume sorting before `default` would silently
    make the bulk disk the hot tier. Order has to survive the sort, not the render."""
    policy = one(render("clickhouse-cluster", *CH_TIERED_SETS), "ClickHouseCluster")["spec"][
        "settings"]["extraConfig"]["storage_configuration"]["policies"]["default"]
    volumes = list(policy["volumes"])
    expect("the hot volume is first once sorted", sorted(volumes)[0] == "default", f"got {volumes}")
    expect("the rendered order already matches the sorted order",
           volumes == sorted(volumes), f"got {volumes}")


def test_clickhouse_tiered_needs_no_object_store() -> None:
    docs = render("clickhouse-cluster", *CH_TIERED_SETS)
    ch = one(docs, "ClickHouseCluster")
    expect("tiered mints no object-store secret",
           "dfe-clickhouse-s3" not in yaml.safe_dump(docs))
    expect("tiered wires no credential environment",
           "env" not in ch["spec"]["containerTemplate"], f"got {ch['spec']['containerTemplate']}")
    storage = ch["spec"]["settings"]["extraConfig"]["storage_configuration"]
    expect("cluster mode declares no disk of its own -- the operator registers it",
           "disks" not in storage, f"got {sorted(storage)}")
    expect("tiered sets no server-wide merge_tree policy override",
           "merge_tree" not in ch["spec"]["settings"]["extraConfig"])


def test_clickhouse_tiered_reaches_single_mode() -> None:
    docs = render("clickhouse-cluster", "clickhouse.mode=single", *CH_TIERED_SETS,
                  "clickhouse.tiered.coldStorageClass=nvme-bulk")
    cm = [d for d in docs if d.get("kind") == "ConfigMap"][0]
    fragment = yaml.safe_load(cm["data"]["dfe-storage.yaml"])["storage_configuration"]
    expect("single mode declares the disk the operator would have registered",
           list(fragment["disks"]) == ["slow"], f"got {fragment.get('disks')}")
    expect("the disk points at the mount the StatefulSet gives it",
           fragment["disks"]["slow"]["path"] == "/var/lib/clickhouse/disks/slow/",
           f"got {fragment['disks']['slow']}")
    expect("single mode ranks the same two volumes",
           set(fragment["policies"]["default"]["volumes"]) == {"default", "slow"},
           f"got {sorted(fragment['policies']['default']['volumes'])}")
    sts = one(docs, "StatefulSet")
    claims = [c["metadata"]["name"] for c in sts["spec"]["volumeClaimTemplates"]]
    expect("single mode claims a second volume for the cold tier",
           claims == ["data", "slow"], f"got {claims}")
    cold = sts["spec"]["volumeClaimTemplates"][1]["spec"]
    expect("the cold claim carries the requested class",
           cold["storageClassName"] == "nvme-bulk", f"got {cold}")
    mounts = {m["name"]: m["mountPath"]
              for m in sts["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]}
    expect("the cold volume is mounted where the disk declaration points",
           mounts.get("slow") == "/var/lib/clickhouse/disks/slow", f"got {mounts}")


def test_clickhouse_tiered_guards() -> None:
    expect("a cold name sorting before default is refused",
           "silently inverts the tiers" in render_error("clickhouse-cluster", *CH_TIERED_SETS,
                                                        "clickhouse.tiered.coldName=cold"))
    expect('a cold name of "default" is refused',
           'must not be "default"' in render_error("clickhouse-cluster", *CH_TIERED_SETS,
                                                   "clickhouse.tiered.coldName=default"))
    expect("an empty cold name is refused",
           "needs clickhouse.tiered.coldName" in render_error("clickhouse-cluster", *CH_TIERED_SETS,
                                                              "clickhouse.tiered.coldName="))
    expect("tiered on an external ClickHouse is refused",
           "meaningless with mode=external" in render_error("clickhouse-cluster",
                                                            "clickhouse.mode=external",
                                                            *CH_TIERED_SETS))
    expect("the shipped default cold name passes the guard",
           render_error("clickhouse-cluster", *CH_TIERED_SETS) == "")


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
           "must be local, s3backed or tiered" in render_error("clickhouse-cluster",
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


def test_the_overlay_reaches_the_tiered_dials() -> None:
    """The same cascade, on the dials this change adds. profile-scale.yaml leaves
    the storage model at the chart default, so a deployer with SSD and bulk classes
    must be able to reach `tiered` from the deploy repo without editing this one."""
    with tempfile.TemporaryDirectory() as tmp:
        overlay = Path(tmp) / "common.yaml"
        overlay.write_text(
            "clickhouse:\n"
            "  storageModel: tiered\n"
            "  storage:\n"
            "    storageClass: nvme-fast\n"
            "  tiered:\n"
            "    coldStorageClass: nvme-bulk\n"
            "    moveFactor: 0.15\n",
            encoding="utf-8",
            newline="\n",
        )
        ch = one(render("clickhouse-cluster", values=[*BASE_CASCADE, overlay]), "ClickHouseCluster")
    expect("the overlay's model reaches the CR",
           "storage_configuration" in ch["spec"]["settings"]["extraConfig"])
    expect("the overlay's hot class reaches the data volume",
           ch["spec"]["dataVolumeClaimSpec"]["storageClassName"] == "nvme-fast",
           f"got {ch['spec']['dataVolumeClaimSpec']}")
    expect("the overlay's cold class reaches the additional volume",
           ch["spec"]["additionalVolumeClaimTemplates"][0]["spec"]["storageClassName"] == "nvme-bulk",
           f"got {ch['spec']['additionalVolumeClaimTemplates']}")
    expect("the overlay's moveFactor reaches the policy",
           ch["spec"]["settings"]["extraConfig"]["storage_configuration"]["policies"]["default"][
               "move_factor"] == 0.15)
    baseline = one(render("clickhouse-cluster"), "ClickHouseCluster")
    expect("the profile alone stays on one volume",
           "additionalVolumeClaimTemplates" not in baseline["spec"])


def main() -> int:
    test_clickhouse_local_adds_nothing()
    test_clickhouse_s3backed_renders_the_disks()
    test_the_cache_disk_sorts_after_the_disk_it_wraps()
    test_the_credential_binding_defaults_to_the_dfe_seeded_path()
    test_the_credential_binding_follows_an_existing_store_entry()
    test_clickhouse_s3backed_keeps_credentials_out_of_git()
    test_the_object_store_timeouts_are_unset_by_default_and_settable()
    test_clickhouse_tiered_ranks_two_local_volumes()
    test_the_cold_volume_sorts_after_the_hot_one()
    test_clickhouse_tiered_needs_no_object_store()
    test_clickhouse_tiered_reaches_single_mode()
    test_clickhouse_tiered_guards()
    test_clickhouse_s3backed_reaches_single_mode()
    test_clickhouse_storage_model_guards()
    test_kafka_local_adds_nothing()
    test_kafka_tiered_renders_the_plugin_and_the_switches()
    test_kafka_tiered_keeps_credentials_out_of_git()
    test_kafka_tiered_version_gate()
    test_kafka_tiered_other_guards()
    test_deploy_repo_overlay_beats_the_profile()
    test_the_overlay_reaches_the_tiered_dials()
    print(f"\n{_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
