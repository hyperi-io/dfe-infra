# Storage: the deploy-time matrix

REFERENCE. Look up one cell: a service, a deployment mode and a storage model,
and what that combination does. Where DFE's data physically lives is not one
choice but several independent ones, and this page is the complete list of them
-- which combinations ship, which are refused, and how strong the evidence is
behind each. Every future addition claims a cell here rather than adding a
paragraph somewhere.

The deep dives stay where they are: [clickhouse.md](clickhouse.md) for the
ClickHouse target matrix, [clickhouse-tiering.md](clickhouse-tiering.md) for the
hot/cold walkthrough, [kafka/README.md](kafka/README.md) for the broker
defaults.

## The four dimensions

1. **Service** -- `clickhouse` or `kafka`. Both take a storage model; they do not
   take the same ones.
2. **Deployment mode** -- who deploys the datastore or broker.
   ClickHouse: `cluster` (operator), `single` (plain StatefulSet), `external`
   (bring your own). Kafka: `disabled` (no broker, gRPC transport), `single`
   (non-operator broker), `cluster` (Strimzi or Redpanda operator), `external`.
3. **Storage model** -- where the data lives, and whether it moves or is copied.
4. **Sub-dimensions** -- the dials inside a model: cache placement, object-store
   flavour, request bounds, per-topic granularity.

Modes and models are INDEPENDENT axes. Not every pair is legal, and the illegal
ones fail the render with a named guard rather than deploying something inert.

## The vocabulary

A storage model is named `<family>-<bulk>`, and both halves carry meaning.

| Half | Value | Means |
| --- | --- | --- |
| family | `tiered` | Data MOVES to the bulk store. The bulk copy is the only copy, so both stores must be durable. |
| family | `cached` | Data is COPIED to the bulk store. The local copy is disposable; the bulk store holds the durable one. |
| bulk | `block` | A second volume on a cheaper StorageClass. |
| bulk | `object` | An S3-compatible object store. |

`local` is the fifth value and the default: one volume, no bulk store, nothing
extra rendered.

The charts read the two halves through a pair of helpers
(`storageFamily` / `storageBulk` in each chart's `_storage.tpl`), so every
template conditional is a single comparison and a new model claims a cell
without touching them.

## Status and evidence vocabularies

Every cell below carries one status and one proof level. Both lists are closed.

| Status | Means |
| --- | --- |
| **default** | What a deployment gets at that tier with no override. |
| **opt-in** | Shipped and supported, inert unless the deployer sets it. |
| **refused** | The render fails. The guard is named in the cell. |
| **follow-up** | Understood and written up, deliberately not surfaced as a value. |
| **wired-unverified** | A code path exists but has never run against the real backend. |

| Proof | Means |
| --- | --- |
| **live-proven** | Run on real hardware, with the measurement named. |
| **render-verified** | `helm template` plus an assertion in `scripts/tests/test_storage_model.py`. No runtime. |
| **unverified** | Neither. |

## Dimension 2 x 3: ClickHouse

`clickhouse.storageModel` in `helm/charts/clickhouse-cluster`.

| Mode | `local` | `cached-object` | `tiered-block` |
| --- | --- | --- | --- |
| `cluster` | **default** (scale tier), live-proven | **opt-in**, render-verified; the disk-ordering constraint is live-proven | **opt-in**, **live-proven** |
| `single` | **default** (single tier), live-proven | **opt-in**, render-verified | **opt-in**, render-verified |
| `external` | **opt-in**; the model is forced, the supplied ClickHouse owns its storage | **refused** | **refused** |

**Preference order.** ClickHouse Cloud runs SharedMergeTree by default and is
the reference shape. `cached-object` is our best-effort equivalent of it, and
is the preferred model wherever an object store exists: always on Kubernetes in
a cloud, and on-prem whenever an S3-compatible store such as MinIO is supplied.
`local` is the last resort -- on-prem with no object store, and the slim and
single tiers, where one volume is the point. That order is what the chart
derives: `clickhouse.storageModel` ships EMPTY and
`dfe-clickhouse.storageModel` reads `cached-object` from a set
`clickhouse.objectStore.endpoint` and `local` from nothing, with a value written
into `clickhouse.storageModel` overriding both. Every template reads the derived
answer, so the two cannot disagree; because every model is locked at first
deploy, the derivation runs once and the deployment repo's
`governance/policies/storage-layout.yaml` holds it afterwards. The `cluster` row
above still reads `local` as default because
`cached-object` is render-verified only; it becomes the cluster-tier default
when it is live-proven, and the table changes then, not before.

The two refusals share one guard in `dfe-clickhouse.validateStorageModel`:

```
clickhouse.storageModel=cached-object is meaningless with mode=external --
the supplied ClickHouse owns its own storage
```

**`cached-object` evidence.** The cache disk must sort AFTER the disk it wraps,
because ClickHouse builds a cache only once its backing disk exists and the
operator serialises `extraConfig` as sorted-key JSON. Naming it anything earlier
fails the server at startup with `BAD_ARGUMENTS` "there is no such disk (it
should be initialized before cache disk)" -- proven live 2026-08-30. The disk
names `s3_object` and `s3_object_cache` encode that ordering.

**`tiered-block` evidence.** Demotion triggers on the hot volume's free space as
a fraction of its total. Verified on a cluster whose hot tier was an SSD with
55.7 GiB free of 61 (a free ratio of 0.91) and whose cold tier was a separate
491 GiB spinning disk: `moveFactor` 0.95 demoted new parts to the cold volume,
0.2 left them hot. One node, one dataset, changing nothing but the value.

**Single mode carries the same on-disk layout.** In cluster mode the operator
mounts each additional volume claim at `/var/lib/clickhouse/disks/<name>` and
registers a ClickHouse disk of the same name; single mode runs no operator, so
the chart mounts the same path and declares the disk itself. Both modes then
share one `storage_configuration` fragment.

## Dimension 2 x 3: Kafka

`kafka.storageModel` in `helm/charts/kafka`. `tiered-object` is KIP-405 tiered
storage, which is what the Kafka and Strimzi documentation calls it.

| Mode | `local` | `tiered-object` |
| --- | --- | --- |
| `disabled` | **default** (slim tier) | **refused** -- "needs kafka.mode=cluster" |
| `single` | **default** (single tier) | **refused** -- same guard |
| `cluster` + `strimzi` | **default** (scale tier) | **opt-in**, render-verified |
| `cluster` + `redpanda` | **opt-in** (BSL gate) | **refused** -- "is a Strimzi Kafka CR field" |
| `external` | **opt-in** | **refused** -- needs `mode=cluster` |

Two further guards on the opt-in cell. `kafka.tieredObject.className` is
required, because Strimzi supports `type: custom` only and ships no
RemoteStorageManager -- the broker image must carry a plugin. And
`kafka.operatorVersion` must be at least 0.38.0, where
`spec.kafka.tieredStorage` landed: Strimzi drops unknown CR fields silently, so
an older operator would deploy a broker that looks configured and tiers nothing.

## Dimension 4: cache placement (`cached-object`, ClickHouse)

`clickhouse.objectStore.cache.volume` in `helm/charts/clickhouse-cluster`.

| Placement | Status | Proof | Constraint |
| --- | --- | --- | --- |
| `pvc` -- a subdirectory of the data PVC, bounded by `clickhouse.objectStore.cache.maxSizeRatioToTotalSpace` (0.6), or by `clickhouse.objectStore.cacheSize` when that is set | **default** | render-verified | The cache and the part metadata share `clickhouse.storage.size`, and ClickHouse refuses both bounds at once |
| `instance-store` -- an `emptyDir` on the node's local NVMe, sized to `clickhouse.objectStore.cacheSize` (required) | **opt-in**, cluster mode only | render-verified | Needs `clickhouse.mode: cluster` and a node that actually carries local NVMe |
| Dedicated disposable cache PVC | **follow-up** | unverified | A different mechanism to `instance-store` above -- see below |

**Why `instance-store` over a bigger `pvc`.** A gp3 volume provisioned large
enough to hold a useful cache is the expensive answer, and it is never what
either dial value defaults to. On AWS the Karpenter `clickhouse` pool's shape
(`shapes/compute-shapes.yaml`) already carries local NVMe sized for exactly
this cache: at about 474 GB an r9gd.2xlarge's NVMe costs what gp3 costs
provisioned to the instance's own EBS ceiling, with 14.5x the IOPS.
`instance-store` mounts that NVMe as a plain pod-level `emptyDir`, mounted into
the container at `/var/lib/clickhouse/disks/s3_object_cache` (concatenated with
the operator's own mounts, per the CRD's own comment on that field) with a
`nodeSelector` on `dfe.hyperi.io/workload: clickhouse` -- the label
`build_karpenter_pools` (`scripts/resolve_sizing.py`) gives every pool it
builds, named after the pool's own use case. The data PVC keeps its default
mount and now holds only the `s3_object` metadata_path. The resolver's own
`clickhouse.storage.size` default does not shrink to reflect that yet -- it is
still sized for a PVC-resident cache -- so a deployer running `instance-store`
can safely set it much smaller by hand; teaching the resolver that ratio is a
follow-up.

**The failure mode is the point, not a gap.** An `emptyDir` is destroyed with
its pod: a node replacement -- Karpenter consolidation, an instance
replacement, a rolling restart -- empties the cache, and the server rewarms it
from the object store cold on the next read. The `clickhouse` Karpenter pool is
on-demand only (`STATEFUL_USE_CASES` in `scripts/resolve_sizing.py`), so spot
reclamation is not a cause here, but node churn still is -- which is exactly
why that pool already carries a disruption budget of one node at a time:
losing more than one node's cache at once is the outage the budget buys
against, not something `instance-store` is meant to prevent outright.

The dedicated cache PVC is a different mechanism from `instance-store` above --
it would use `additionalVolumeClaimTemplates`, and the operator registers a
ClickHouse disk per additional volume claim, so a volume named
`s3_object_cache` collides with the cache disk declaration and needs a
distinct name. And the object disk's `metadata_path` sits at
`/var/lib/clickhouse/disks/s3_object/`, which works only while there are no
additional volumes of that kind -- adding one makes that directory the
operator's root-owned mount parent, and the server dies on
`create_directories`. `instance-store` sidesteps this entirely: it is a plain
pod volume, not a PVC template, so the operator never registers it as a disk.

## Dimension 4: object-store flavour

| Flavour | Service | Status | Proof |
| --- | --- | --- | --- |
| S3 | ClickHouse | **opt-in** | render-verified |
| GCS via its S3-compatible endpoint | ClickHouse | **opt-in** | unverified. GCS rejects the batch-delete call, so set `clickhouse.objectStore.supportBatchDelete: false` |
| Azure Blob | ClickHouse | **follow-up** (no value surfaced) | wired-unverified |
| `local_blob_storage` | ClickHouse | **follow-up** (no value surfaced) | live-proven on a rig |
| Whatever the plugin speaks | Kafka | **opt-in** | unverified |

Azure Blob is addressed with `storage_account_url`, `container_name` and
`account_name` / `account_key`. Authentication and addressing were confirmed
against the Azurite emulator; container creation could not be, because Azurite
3.36.0 rejects the create-container call from ClickHouse and from `az` alike.
Provisioning the container ahead of time is the better posture regardless, since
the credential then needs no container-create right.

`local_blob_storage` presents a plain filesystem path through the object-storage
interface, which makes it cacheable -- the route to `cached-block` with no
object store at all. Verified on a rig: the wrapped disk resolved as
`ObjectStorage / Local`, and a warm scan served 3.34 MB from cache against 79 KB
from source.

Kafka's flavour is not a chart dimension. `kafka.tieredObject.config` passes
plugin keys through verbatim, so the plugin owns the addressing that the
ClickHouse chart owns itself.

## Dimension 4: request bounds (`cached-object`, ClickHouse)

`clickhouse.objectStore.retryAttempts`, `.connectTimeoutMs` and
`.requestTimeoutMs` are **opt-in** and unset by default, leaving the server's own
behaviour alone. That behaviour is the reason they exist: **live-proven**, a read
against an unreachable store blocked for over nine minutes before failing, and
`1 / 2000 / 5000` brought the same failure down to 10.4 seconds. Those numbers
are a demonstration, not a recommendation -- one retry is aggressive against real
S3, which returns transient 5xx routinely, so pick production values against the
actual store. What they must not stay is unbounded, because a nine-minute stall
on a dashboard is indistinguishable from an outage.

## Dimension 4: credential binding (both services)

| Cell | Status | Proof |
| --- | --- | --- |
| Derived path `<project>/<env>/clickhouse/s3` and `<project>/<env>/kafka/tiered` | **default** | render-verified |
| Rebind with `objectStore.remoteKey`, `.accessKeyProperty`, `.secretKeyProperty`, `.secretStoreName` | **opt-in** | render-verified |
| A credential literal in a values file | **refused** by construction | render-verified |

Both charts read the credential from the environment, wired from a Secret the
secrets store materialises. Nothing is inline in the CR or the ConfigMap, both of
which live in git.

## Dimension 4: granularity of the opt-in

| Cell | Status | Proof |
| --- | --- | --- |
| Kafka per-topic: the landing topic gets `remote.storage.enable: true`, DLQ topics do not | **default** under `tiered-object` | render-verified |
| ClickHouse per-table | **follow-up** | The policy is server-wide, so no table DDL is needed |

Without the per-topic switch the brokers hold the plugin and move nothing. On the
ClickHouse side, age-based demotion (`TTL ts + INTERVAL 7 DAY TO VOLUME 'slow'`)
is per-table DDL and belongs to dfe-engine's governance, not to this chart.

## Cells the vocabulary names and no chart builds

Both are refused at render time, so asking for one fails loudly rather than
deploying a datastore that ignores the request.

| Cell | Service | What it would be | Blocking work |
| --- | --- | --- | --- |
| `tiered-object` | ClickHouse | Parts MOVED to an object store, matching Kafka's shape | A ranked policy whose cold volume is an object disk; no evidence gathered |
| `cached-block` | ClickHouse | An SSD cache over a cheap local volume | Needs `local_blob_storage`, which is proven on a rig and not surfaced |

## Locked at deploy

Every model is a one-way decision, enforced twice.

The **mechanisms**: the ClickHouse operator takes no new disk on an existing
`ClickHouseCluster`, so a model that adds a volume claim cannot be reached later.
And `volumeClaimTemplates` are immutable, so a size or class change needs every
PVC patched and the StatefulSet recreated. `local-path` cannot resize at all.

The **enforcement**: dfe-engine seeds a `storage-layout` policy holding
`clickhouse.mode`, `clickhouse.storageModel`, `clickhouse.objectStore.*`,
`clickhouse.tieredBlock.*`, `clickhouse.storage.size`,
`clickhouse.storage.storageClass` and the five matching `kafka.*` patterns as
protected vars. The API refuses a post-deploy edit and names the pattern that
blocked it. A holder of `helmvars:override` can still make a deliberate
exception, and a direct git commit still gets through -- the policy governs the
API, not the repo.

The API also reports what syncing a write would do, and the verdict differs by
service: writing `kafka.storageModel` is a `roll`, because Strimzi reconciles
`tieredStorage` onto the running CR, while `clickhouse.storageModel` is a
`recreate`.

## Runtime identity: what a rename is not

Some names in the rendered output are not labels -- they are what the running
server resolves its data through. Renaming one is a rebuild and a restore, not a
values edit, so they stay fixed while the values vocabulary above them changes.

| Identifier | Why it is frozen |
| --- | --- |
| Disks `s3_object`, `s3_object_cache`; policy `s3_cached` | ClickHouse refuses a policy change that drops a volume name it already loaded, and `merge_tree.storage_policy` is the server-wide default every existing table resolved through |
| Policy `default`, volume `default`, the cold volume name | The same refusal, and the cold name is also the PVC template name, which is immutable in both the operator claim template and a StatefulSet |
| Mount path `/var/lib/clickhouse/disks/<name>` | Changing it strands the metadata the disk points at |
| Derived credential paths `<project>/<env>/clickhouse/s3` and `<project>/<env>/kafka/tiered` | A deployment that seeded there and never set `remoteKey` would lose its credential |
| Secret names `dfe-clickhouse-s3`, `dfe-kafka-tiered` | Moving them orphans the materialised Secret and rolls every pod that reads it |

## The docker path is a different axis

`dfe-docker` has no storage models and no size dial. It has ONE storage choice
and it is a LOCATION: set `DFE_DATA_ROOT` and every named volume becomes a bind
onto `${DFE_DATA_ROOT}/<name>`.

State it as asymmetry rather than parity: **Kubernetes locks size and class;
docker locks which disk.** Two consequences carry over from
`dfe-docker/docs/operating.md`: switching an existing deployment does not migrate
data between locations, and `make clean` removes the volume objects while the
bind directories keep their contents, so reclaiming the space is an explicit
delete of `${DFE_DATA_ROOT}`.

## Where the pieces live

- ClickHouse chart: `helm/charts/clickhouse-cluster`, models in
  `templates/_storage.tpl`.
- Kafka chart: `helm/charts/kafka`, models in `templates/_storage.tpl`.
- Assertions: `scripts/tests/test_storage_model.py` -- run it with
  `python3 scripts/tests/test_storage_model.py`.
- The protected-var lock: dfe-engine
  `src/dfe_engine/governance/resources/policies/storage-layout.yaml`.
