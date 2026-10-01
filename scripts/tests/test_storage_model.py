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
   ClickHouse `cached-object` and `tiered-block`, Kafka `tiered-object` -- and
   the models with an object bulk store render the credential environment and
   never a credential literal. The cells the vocabulary names but no chart
   builds are refused, not rendered inert.
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

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
VALUES = REPO_ROOT / "argocd" / "values"

# The valueFiles order layer2-data and layer2-platform use, minus the deploy-repo
# layers the individual tests append.
BASE_CASCADE = [VALUES / "common.yaml", VALUES / "local.yaml", VALUES / "profile-scale.yaml"]


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


# The models are named <family>-<bulk>: the family says whether parts MOVE to
# the bulk store or are COPIED to it, the bulk half says what that store is.
CACHED_OBJECT_SETS = (
    "clickhouse.storageModel=cached-object",
    "clickhouse.objectStore.endpoint=https://dfe-ch.s3.ap-southeast-2.amazonaws.com/parts/",
)
KAFKA_TIERED_SETS = (
    "kafka.storageModel=tiered-object",
    "kafka.tieredObject.className=io.aiven.kafka.tieredstorage.RemoteStorageManager",
)
# ClickHouse hot/cold on two local volumes. The cold dials keep their defaults
# except where a test names them, so the defaults stay exercised.
TIERED_BLOCK_SETS = ("clickhouse.storageModel=tiered-block",)


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


def test_clickhouse_cached_object_renders_the_disks() -> None:
    docs = render("clickhouse-cluster", *CACHED_OBJECT_SETS)
    extra = one(docs, "ClickHouseCluster")["spec"]["settings"]["extraConfig"]
    disks = extra["storage_configuration"]["disks"]
    expect(
        "cached-object defines an object disk and a cache disk",
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
    docs = render("clickhouse-cluster", *CACHED_OBJECT_SETS)
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


def _service_account(docs: list[dict], name: str) -> dict:
    matches = [d for d in docs if d.get("kind") == "ServiceAccount" and d["metadata"]["name"] == name]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one ServiceAccount {name}, got {len(matches)}")
    return matches[0]


def test_the_credential_binding_defaults_to_the_dfe_seeded_path() -> None:
    """Unset values must keep the path every seeded deployment already uses."""
    for chart, sets, secret, seeded in (
        ("clickhouse-cluster", CACHED_OBJECT_SETS, "dfe-clickhouse-s3", "dfe/local/clickhouse/s3"),
        ("kafka", KAFKA_TIERED_SETS, "dfe-kafka-tiered", "dfe/local/kafka/tiered"),
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
        ("clickhouse-cluster", CACHED_OBJECT_SETS, "clickhouse.objectStore", "dfe-clickhouse-s3"),
        ("kafka", KAFKA_TIERED_SETS, "kafka.objectStore", "dfe-kafka-tiered"),
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


def test_clickhouse_cached_object_keeps_credentials_out_of_git() -> None:
    docs = render("clickhouse-cluster", *CACHED_OBJECT_SETS)
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


def test_clickhouse_cached_object_pod_identity_skips_the_static_key() -> None:
    """usePodIdentity: true must render no ExternalSecret and no credential env
    vars -- a static AWS_ACCESS_KEY_ID in the pod's environment would shadow the
    EKS Pod Identity Agent's injected credentials, which the AWS SDK's default
    credential chain checks first (helm/charts/clickhouse-cluster/templates/
    _storage.tpl)."""
    docs = render("clickhouse-cluster", *CACHED_OBJECT_SETS, "clickhouse.objectStore.usePodIdentity=true")
    disk = one(docs, "ClickHouseCluster")["spec"]["settings"]["extraConfig"][
        "storage_configuration"]["disks"]["s3_object"]
    expect("the disk still takes credentials from the environment",
           disk.get("use_environment_credentials") is True)
    env = one(docs, "ClickHouseCluster")["spec"]["containerTemplate"].get("env", [])
    expect("no static credential env vars are rendered", env == [], f"got {env}")
    object_store_secrets = [
        d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == "dfe-clickhouse-s3"
    ]
    # admin-secret.yaml still mints its own unrelated ExternalSecret for the
    # cluster's admin password whenever mode != external, so the assertion
    # names the object-store secret specifically rather than the kind.
    expect("no ExternalSecret is minted for the object store", object_store_secrets == [],
           f"got {object_store_secrets}")


def test_clickhouse_cached_object_pod_identity_reaches_single_mode() -> None:
    """clickhouse-single.yaml carries its own copy of the usePodIdentity gate.
    A static AWS_ACCESS_KEY_ID there shadows the Pod Identity Agent's injected
    credential just as surely as in cluster mode, and fails as an auth error
    nobody attributes to Helm."""
    docs = render("clickhouse-cluster", *CACHED_OBJECT_SETS,
                  "clickhouse.mode=single", "clickhouse.objectStore.usePodIdentity=true")
    container = one(docs, "StatefulSet")["spec"]["template"]["spec"]["containers"][0]
    credential_env = [e for e in container.get("env", []) if e["name"].startswith("AWS_")]
    expect("no static credential env vars reach the single-mode pod",
           credential_env == [], f"got {credential_env}")
    object_store_secrets = [
        d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == "dfe-clickhouse-s3"
    ]
    expect("and no object-store ExternalSecret is minted", object_store_secrets == [],
           f"got {object_store_secrets}")


def test_clickhouse_renders_its_own_service_account() -> None:
    """Fix: the AWS Pod Identity association used to bind the release
    namespace's default account, so every other pod in that namespace
    inherited the object-store role's S3 write and delete rights. The chart
    now renders a dedicated ServiceAccount and
    points both the CR and the StatefulSet at it, cluster mode and single
    alike (terraform/modules/kubernetes-cluster/aws/object-store.tf's
    clickhouse_object_store_service_account default must keep matching this
    name -- proven independently by the OpenTofu contract test of the same
    shape)."""
    for label, sets in (("cluster", ()), ("single", ("clickhouse.mode=single",))):
        docs = render("clickhouse-cluster", *sets)
        sa = _service_account(docs, "dfe-clickhouse")
        expect(f"{label}: dfe-clickhouse automounts no token",
               sa.get("automountServiceAccountToken") is False, f"got {sa}")
        pod_spec = (
            one(docs, "ClickHouseCluster")["spec"]["podTemplate"]
            if label == "cluster"
            else one(docs, "StatefulSet")["spec"]["template"]["spec"]
        )
        expect(f"{label}: the workload names that account",
               pod_spec.get("serviceAccountName") == "dfe-clickhouse", f"got {pod_spec}")


def test_clickhouse_external_mode_renders_no_service_account() -> None:
    """mode=external supplies its own ClickHouse -- nothing here runs as a DFE
    account, so no ServiceAccount should render for a Pod Identity association
    to reach even by accident."""
    docs = render("clickhouse-cluster", "clickhouse.mode=external")
    expect("no ServiceAccount for a BYO ClickHouse", "ServiceAccount" not in kinds(docs), f"got {kinds(docs)}")


def test_clickhouse_keeper_gets_its_own_service_account_too() -> None:
    """Keeper never touches S3 -- no Pod Identity association targets it --
    but it must not share the namespace's default account either, or a future
    association or grant aimed at "default" would reach it too. It also must
    not share ClickHouse's own account: two workloads on one identity is the
    same over-sharing fault this whole change fixes, one level down."""
    docs = render("clickhouse-cluster")
    sa = _service_account(docs, "dfe-keeper")
    expect("dfe-keeper automounts no token", sa.get("automountServiceAccountToken") is False, f"got {sa}")
    keeper = one(docs, "KeeperCluster")
    expect("the KeeperCluster CR names its own account",
           keeper["spec"]["podTemplate"].get("serviceAccountName") == "dfe-keeper",
           f"got {keeper['spec'].get('podTemplate')}")
    expect("clickhouse and keeper hold DIFFERENT accounts",
           _service_account(docs, "dfe-clickhouse")["metadata"]["name"] != sa["metadata"]["name"])


def test_the_aws_cascade_is_what_turns_pod_identity_on() -> None:
    """usePodIdentity lives in argocd/values/aws.yaml, not the chart default, so
    the cluster-mode test above passes on a --set nobody sets in production.
    This renders the cascade an AWS deploy actually gets."""
    aws_cascade = [VALUES / "common.yaml", VALUES / "aws.yaml", VALUES / "profile-scale.yaml"]
    docs = render(
        "clickhouse-cluster",
        "clickhouse.objectStore.endpoint=https://dfe-ch.s3.ap-southeast-2.amazonaws.com/dfe/",
        values=aws_cascade,
    )
    env = one(docs, "ClickHouseCluster")["spec"]["containerTemplate"].get("env", [])
    expect("the aws cascade renders no static credential env", env == [], f"got {env}")
    object_store_secrets = [
        d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == "dfe-clickhouse-s3"
    ]
    expect("and no object-store ExternalSecret", object_store_secrets == [],
           f"got {object_store_secrets}")


def test_the_endpoint_alone_derives_cached_object() -> None:
    """Every other test names clickhouse.storageModel outright, so the
    derivation the whole AWS chain rests on -- a non-empty objectStore.endpoint
    turning cached-object on by itself -- is otherwise proven by nothing. Break
    it and an AWS deploy with a good bucket silently stays local, writing every
    bulk part to the PVC."""
    docs = render(
        "clickhouse-cluster",
        "clickhouse.objectStore.endpoint=https://dfe-ch.s3.ap-southeast-2.amazonaws.com/dfe/",
    )
    storage = one(docs, "ClickHouseCluster")["spec"]["settings"]["extraConfig"]["storage_configuration"]
    expect("the endpoint alone declares the s3 disk", "s3_object" in storage["disks"], f"{storage['disks'].keys()}")
    expect("and the cached policy is what MergeTree gets",
           "s3_cached" in storage["policies"], f"{storage['policies'].keys()}")


def test_the_object_store_timeouts_are_unset_by_default_and_settable() -> None:
    """Left at the server defaults a read against an unreachable store blocked
    over nine minutes on the rig; 1 / 2000 / 5000 failed the same read in 10.4s.
    The dials must therefore exist, and must change nothing until they are set."""
    bounded = {"s3_retry_attempts", "s3_connect_timeout_ms", "s3_request_timeout_ms"}
    disk = one(render("clickhouse-cluster", *CACHED_OBJECT_SETS), "ClickHouseCluster")["spec"]["settings"][
        "extraConfig"]["storage_configuration"]["disks"]["s3_object"]
    expect("the defaults leave the server's own retry behaviour alone",
           not bounded & set(disk), f"got {sorted(disk)}")
    tuned = one(
        render(
            "clickhouse-cluster",
            *CACHED_OBJECT_SETS,
            "clickhouse.objectStore.retryAttempts=1",
            "clickhouse.objectStore.connectTimeoutMs=2000",
            "clickhouse.objectStore.requestTimeoutMs=5000",
        ),
        "ClickHouseCluster",
    )["spec"]["settings"]["extraConfig"]["storage_configuration"]["disks"]["s3_object"]
    expect("each bound reaches the disk",
           [tuned.get(k) for k in ("s3_retry_attempts", "s3_connect_timeout_ms",
                                   "s3_request_timeout_ms")] == [1, 2000, 5000],
           f"got {tuned}")


def test_clickhouse_tiered_block_ranks_two_local_volumes() -> None:
    docs = render(
        "clickhouse-cluster",
        *TIERED_BLOCK_SETS,
        "clickhouse.tieredBlock.coldStorageClass=nvme-bulk",
        "clickhouse.tieredBlock.coldSize=4Ti",
    )
    ch = one(docs, "ClickHouseCluster")
    claims = ch["spec"]["additionalVolumeClaimTemplates"]
    expect("tiered-block claims exactly one extra volume", len(claims) == 1, f"got {claims}")
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
    policy = one(render("clickhouse-cluster", *TIERED_BLOCK_SETS), "ClickHouseCluster")["spec"][
        "settings"]["extraConfig"]["storage_configuration"]["policies"]["default"]
    volumes = list(policy["volumes"])
    expect("the hot volume is first once sorted", sorted(volumes)[0] == "default", f"got {volumes}")
    expect("the rendered order already matches the sorted order",
           volumes == sorted(volumes), f"got {volumes}")


def test_clickhouse_tiered_block_needs_no_object_store() -> None:
    docs = render("clickhouse-cluster", *TIERED_BLOCK_SETS)
    ch = one(docs, "ClickHouseCluster")
    expect("a block bulk store mints no object-store secret",
           "dfe-clickhouse-s3" not in yaml.safe_dump(docs))
    expect("a block bulk store wires no credential environment",
           "env" not in ch["spec"]["containerTemplate"], f"got {ch['spec']['containerTemplate']}")
    storage = ch["spec"]["settings"]["extraConfig"]["storage_configuration"]
    expect("cluster mode declares no disk of its own -- the operator registers it",
           "disks" not in storage, f"got {sorted(storage)}")
    expect("tiered-block sets no server-wide merge_tree policy override",
           "merge_tree" not in ch["spec"]["settings"]["extraConfig"])


def test_clickhouse_tiered_block_reaches_single_mode() -> None:
    docs = render("clickhouse-cluster", "clickhouse.mode=single", *TIERED_BLOCK_SETS,
                  "clickhouse.tieredBlock.coldStorageClass=nvme-bulk")
    cm = next(d for d in docs if d.get("kind") == "ConfigMap")
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


def test_clickhouse_tiered_block_guards() -> None:
    expect("a cold name sorting before default is refused",
           "silently inverts the tiers" in render_error("clickhouse-cluster", *TIERED_BLOCK_SETS,
                                                        "clickhouse.tieredBlock.coldName=cold"))
    expect('a cold name of "default" is refused',
           'must not be "default"' in render_error("clickhouse-cluster", *TIERED_BLOCK_SETS,
                                                   "clickhouse.tieredBlock.coldName=default"))
    expect("an empty cold name is refused",
           "needs clickhouse.tieredBlock.coldName" in render_error(
               "clickhouse-cluster", *TIERED_BLOCK_SETS, "clickhouse.tieredBlock.coldName="))
    expect("tiered-block on an external ClickHouse is refused",
           "meaningless with mode=external" in render_error("clickhouse-cluster",
                                                            "clickhouse.mode=external",
                                                            *TIERED_BLOCK_SETS))
    expect("the refusal names the model that was asked for",
           "clickhouse.storageModel=tiered-block" in render_error("clickhouse-cluster",
                                                                  "clickhouse.mode=external",
                                                                  *TIERED_BLOCK_SETS))
    expect("the shipped default cold name passes the guard",
           render_error("clickhouse-cluster", *TIERED_BLOCK_SETS) == "")


def test_clickhouse_cached_object_reaches_single_mode() -> None:
    docs = render("clickhouse-cluster", "clickhouse.mode=single", *CACHED_OBJECT_SETS)
    cm = next(d for d in docs if d.get("kind") == "ConfigMap")
    expect("single mode ships the same fragment as config.d YAML",
           "dfe-storage.yaml" in cm["data"])
    fragment = yaml.safe_load(cm["data"]["dfe-storage.yaml"])
    expect("the single-mode fragment names the same policy",
           fragment["merge_tree"]["storage_policy"] == "s3_cached")


def test_wait_for_async_insert_guard_catches_every_falsy_spelling() -> None:
    """gate-3-correctness.md P3: the guard used to compare toString(value) to
    the literal "0" alone, so a values file writing an unquoted `false` (the
    Go bool, not the string) sailed through -- ClickHouse itself reads either
    as the same fire-and-forget setting."""
    for value in ("false", "no", "off"):
        err = render_error("clickhouse-cluster", f"clickhouse.userProfile.waitForAsyncInsert={value}")
        expect(f"waitForAsyncInsert={value} is refused", "must not be" in err, f"got {err[:200]}")
    expect(
        "the shipped default (1) still passes",
        render_error("clickhouse-cluster") == "",
    )


def test_clickhouse_storage_model_guards() -> None:
    expect("an unknown model is refused",
           "must be local, cached-object or tiered-block" in render_error(
               "clickhouse-cluster", "clickhouse.storageModel=glacier"))
    expect("cached-object with no endpoint is refused",
           "needs clickhouse.objectStore.endpoint" in render_error(
               "clickhouse-cluster", "clickhouse.storageModel=cached-object"))
    expect("cached-object on an external ClickHouse is refused",
           "meaningless with mode=external" in render_error(
               "clickhouse-cluster", "clickhouse.mode=external", *CACHED_OBJECT_SETS))


def test_an_instance_store_cache_with_no_endpoint_names_the_endpoint() -> None:
    """The resolver emits the cache volume with no storageModel beside it, so
    under `auto` a missing endpoint fails here -- and the message has to name
    the endpoint nobody set rather than the model nobody chose. Reachable:
    bootstrap.sh composes the endpoint annotation only when the tofu output is
    non-empty, so a run without --from-terraform renders it blank."""
    err = render_error(
        "clickhouse-cluster",
        "clickhouse.objectStore.cache.volume=instance-store",
        "clickhouse.objectStore.cacheSize=109Gi",
    )
    expect("the failure names the missing endpoint",
           "no clickhouse.objectStore.endpoint is set" in err, err.strip()[-300:])
    expect("and says which way the model derived",
           "derived to local" in err, err.strip()[-300:])
    expect("an explicit local still blames the model the deployer chose",
           "clickhouse.storageModel=local has none" in render_error(
               "clickhouse-cluster",
               "clickhouse.storageModel=local",
               "clickhouse.objectStore.cache.volume=instance-store",
               "clickhouse.objectStore.cacheSize=109Gi",
           ))


STORAGE_MODEL_ENDPOINT_SET = "clickhouse.objectStore.endpoint=https://dfe-ch.s3.ap-southeast-2.amazonaws.com/parts/"


def _renders_local(docs: list[dict]) -> bool:
    extra = one(docs, "ClickHouseCluster")["spec"]["settings"]["extraConfig"]
    return "storage_configuration" not in extra and "merge_tree" not in extra


def _renders_cached_object(docs: list[dict]) -> bool:
    extra = one(docs, "ClickHouseCluster")["spec"]["settings"]["extraConfig"]
    disks = extra.get("storage_configuration", {}).get("disks", {})
    return set(disks) == {"s3_object", "s3_object_cache"} and extra.get(
        "merge_tree", {}
    ).get("storage_policy") == "s3_cached"


def test_storage_model_dial_the_six_cases() -> None:
    """The three-value dial (auto/cached-object/local) against the two states of
    objectStore.endpoint -- the full cross product this fix turns on. `auto`
    derives from the endpoint exactly as an unset value always did; the other two
    are explicit overrides that win outright, in either direction."""
    expect("auto with no endpoint renders local",
           _renders_local(render("clickhouse-cluster", "clickhouse.storageModel=auto")))
    expect("auto with an endpoint derives cached-object",
           _renders_cached_object(render("clickhouse-cluster", "clickhouse.storageModel=auto",
                                          STORAGE_MODEL_ENDPOINT_SET)))
    expect("cached-object with an endpoint renders cached-object",
           _renders_cached_object(render("clickhouse-cluster", *CACHED_OBJECT_SETS)))
    expect("cached-object with no endpoint is refused",
           "needs clickhouse.objectStore.endpoint" in render_error(
               "clickhouse-cluster", "clickhouse.storageModel=cached-object"))
    expect("local with an endpoint still renders local -- the explicit opt-out wins",
           _renders_local(render("clickhouse-cluster", "clickhouse.storageModel=local",
                                  STORAGE_MODEL_ENDPOINT_SET)))
    expect("local with no endpoint renders local",
           _renders_local(render("clickhouse-cluster", "clickhouse.storageModel=local")))


def test_the_unclaimed_cells_are_refused_rather_than_rendered_inert() -> None:
    """tiered-object and cached-block are named in the vocabulary and not built.
    Accepting either would deploy a ClickHouse that silently ignores the model."""
    for model in ("tiered-object", "cached-block"):
        err = render_error("clickhouse-cluster", f"clickhouse.storageModel={model}")
        expect(f"clickhouse refuses the unbuilt {model}",
               "must be local, cached-object or tiered-block" in err, f"got {err[:200]}")
    for model in ("tiered-block", "cached-object", "cached-block"):
        err = render_error("kafka", f"kafka.storageModel={model}")
        expect(f"kafka refuses the unbuilt {model}",
               "must be local or tiered-object" in err, f"got {err[:200]}")


def test_the_object_store_batch_delete_switch_is_tri_state() -> None:
    """GCS rejects the batch-delete call, so a deployment against it needs an
    explicit false -- which an unset-means-empty dial cannot express."""
    disk = one(render("clickhouse-cluster", *CACHED_OBJECT_SETS), "ClickHouseCluster")["spec"][
        "settings"]["extraConfig"]["storage_configuration"]["disks"]["s3_object"]
    expect("unset leaves the server default alone", "support_batch_delete" not in disk,
           f"got {sorted(disk)}")
    for value, rendered in (("false", False), ("true", True)):
        tuned = one(
            render("clickhouse-cluster", *CACHED_OBJECT_SETS,
                   f"clickhouse.objectStore.supportBatchDelete={value}"),
            "ClickHouseCluster",
        )["spec"]["settings"]["extraConfig"]["storage_configuration"]["disks"]["s3_object"]
        expect(f"an explicit {value} reaches the disk",
               tuned.get("support_batch_delete") is rendered, f"got {tuned}")


def test_kafka_local_adds_nothing() -> None:
    kafka = one(render("kafka"), "Kafka")
    expect("local Kafka declares no tieredStorage", "tieredStorage" not in kafka["spec"]["kafka"])
    expect("local Kafka leaves the remote-log switch off",
           "remote.log.storage.system.enable" not in kafka["spec"]["kafka"]["config"])


def test_kafka_tiered_object_renders_the_plugin_and_the_switches() -> None:
    """The BROKER half only. The per-topic remote.storage.enable is a topic
    config, and this chart creates no topic -- dfe-engine applies the dfe-schemas
    topic set on every tier."""
    docs = render("kafka", *KAFKA_TIERED_SETS)
    spec = one(docs, "Kafka")["spec"]["kafka"]
    expect("tiered storage is Strimzi's custom type", spec["tieredStorage"]["type"] == "custom")
    expect("the plugin class is passed through",
           spec["tieredStorage"]["remoteStorageManager"]["className"].endswith("RemoteStorageManager"))
    expect("the broker-wide switch is on",
           spec["config"]["remote.log.storage.system.enable"] == "true")
    expect("and the chart renders no KafkaTopic to carry the per-topic half",
           [d for d in docs if d.get("kind") == "KafkaTopic"] == [],
           f"got {[d['metadata']['name'] for d in docs if d.get('kind') == 'KafkaTopic']}")


def test_kafka_tiered_object_keeps_credentials_out_of_git() -> None:
    docs = render("kafka", *KAFKA_TIERED_SETS)
    env = one(docs, "Kafka")["spec"]["kafka"]["template"]["kafkaContainer"]["env"]
    expect("both credential vars come from a secretKeyRef",
           all("secretKeyRef" in e["valueFrom"] for e in env), f"got {env}")
    names = {d["metadata"]["name"] for d in docs if d.get("kind") == "ExternalSecret"}
    expect("an ExternalSecret materialises them", "dfe-kafka-tiered" in names, f"got {sorted(names)}")


def test_kafka_tiered_object_version_gate() -> None:
    err = render_error("kafka", *KAFKA_TIERED_SETS, "kafka.operatorVersion=0.37.0")
    expect("an operator too old for tiered storage fails the render",
           "needs Strimzi >= 0.38.0" in err, f"got {err[:200]}")
    expect("the failure names the version in use", "0.37.0" in err, f"got {err[:200]}")
    expect("the pinned operator version passes the gate",
           render_error("kafka", *KAFKA_TIERED_SETS) == "")


def test_kafka_tiered_object_other_guards() -> None:
    expect("an unknown model is refused",
           "must be local or tiered-object" in render_error("kafka", "kafka.storageModel=glacier"))
    expect("tiered-object without a plugin class is refused",
           "needs kafka.tieredObject.className" in render_error(
               "kafka", "kafka.storageModel=tiered-object"))
    expect("tiered-object on the single tier is refused",
           "needs kafka.mode=cluster" in render_error("kafka", *KAFKA_TIERED_SETS, "kafka.mode=single"))
    expect("tiered-object on redpanda is refused",
           "Strimzi Kafka CR field" in render_error("kafka", *KAFKA_TIERED_SETS, "kafka.provider=redpanda"))
    expect("each refusal names the model that was asked for",
           "kafka.storageModel=tiered-object" in render_error(
               "kafka", *KAFKA_TIERED_SETS, "kafka.mode=single"))


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


def test_the_overlay_reaches_the_tiered_block_dials() -> None:
    """The same cascade, on the dials this change adds. profile-scale.yaml leaves
    the storage model at the chart default, so a deployer with SSD and bulk classes
    must reach `tiered-block` from the deploy repo without editing this one."""
    with tempfile.TemporaryDirectory() as tmp:
        overlay = Path(tmp) / "common.yaml"
        overlay.write_text(
            "clickhouse:\n"
            "  storageModel: tiered-block\n"
            "  storage:\n"
            "    storageClass: nvme-fast\n"
            "  tieredBlock:\n"
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
    with standalone():
        test_clickhouse_local_adds_nothing()
        test_clickhouse_cached_object_renders_the_disks()
        test_the_cache_disk_sorts_after_the_disk_it_wraps()
        test_the_credential_binding_defaults_to_the_dfe_seeded_path()
        test_the_credential_binding_follows_an_existing_store_entry()
        test_clickhouse_cached_object_keeps_credentials_out_of_git()
        test_clickhouse_cached_object_pod_identity_skips_the_static_key()
        test_clickhouse_cached_object_pod_identity_reaches_single_mode()
        test_clickhouse_renders_its_own_service_account()
        test_clickhouse_external_mode_renders_no_service_account()
        test_clickhouse_keeper_gets_its_own_service_account_too()
        test_the_aws_cascade_is_what_turns_pod_identity_on()
        test_the_endpoint_alone_derives_cached_object()
        test_the_object_store_timeouts_are_unset_by_default_and_settable()
        test_clickhouse_tiered_block_ranks_two_local_volumes()
        test_the_cold_volume_sorts_after_the_hot_one()
        test_clickhouse_tiered_block_needs_no_object_store()
        test_clickhouse_tiered_block_reaches_single_mode()
        test_clickhouse_tiered_block_guards()
        test_clickhouse_cached_object_reaches_single_mode()
        test_wait_for_async_insert_guard_catches_every_falsy_spelling()
        test_clickhouse_storage_model_guards()
        test_storage_model_dial_the_six_cases()
        test_the_unclaimed_cells_are_refused_rather_than_rendered_inert()
        test_the_object_store_batch_delete_switch_is_tri_state()
        test_kafka_local_adds_nothing()
        test_kafka_tiered_object_renders_the_plugin_and_the_switches()
        test_kafka_tiered_object_keeps_credentials_out_of_git()
        test_kafka_tiered_object_version_gate()
        test_kafka_tiered_object_other_guards()
        test_deploy_repo_overlay_beats_the_profile()
        test_the_overlay_reaches_the_tiered_block_dials()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
