# ClickHouse SSD tiering

> **Coming from Splunk or Elastic?** Tiering is a ClickHouse storage policy, not table DDL. `moveFactor` triggers demotion when the hot volume's free-space fraction falls below its threshold, for every table on the policy.

DFE can put recent data on SSD and bulk data on cheap storage. It's opt-in per deployment through `clickhouse.storageModel` in the `clickhouse-cluster` chart, and off by default.

This page is the walkthrough for one model. [storage.md](storage.md) has the full matrix across both data stores: every mode, every model, and the evidence behind each.

## Pick a storage model

`clickhouse.storageModel` ships as `auto`, which derives the model from whether an object store is supplied: `clickhouse.objectStore.endpoint` set means `cached-object`, nothing means `local`. An empty value means the same. Setting `local`, `tiered-block` or `cached-object` overrides the derivation. There's no second dial, because the model decides the disks, the policy and the volumes together. The names are `<family>-<bulk>`, where the family says whether parts MOVE to the bulk store or are COPIED to it.

| Model | Bulk data lives on | Fast tier is | Use when |
| --- | --- | --- | --- |
| `local` | one PVC | nothing | Default. Small deployments, or storage that is uniformly fast |
| `tiered-block` | a second PVC on a cheap class | the data PVC on an SSD class, holding recent parts | Block storage, no object store |
| `cached-object` | an S3-compatible object store | a read-through cache -- on the data PVC by default, or on node-local NVMe with `clickhouse.objectStore.cache.volume: instance-store` (see [storage.md](storage.md)) | An object store is available |

The constraint that decides between the two non-default models: **ClickHouse can't cache one local disk on another.** The `cache` disk type wraps object storage alone, so over plain block storage the supported setup is a ranked storage policy, not a cache. Try it any other way and the server fails at startup with "cached disk is allowed only on top of object storage". That's why `cached-block` is a cell the chart doesn't build.

In `tiered-block` a part lives on one of two volumes and moves down as the hot one fills. In `cached-object` the object store holds every part and the cache holds copies.

```mermaid
flowchart LR
    accTitle: L4 Detail -- ClickHouse storage models
    accDescr: A query reads through the storage policy. Under tiered-block a part sits on the hot SSD volume until moveFactor demotes it to the cold bulk volume. Under cached-object the cache disk answers and fetches a miss from the object store, which holds the durable copy.

    query["Query"]

    subgraph tiered["storageModel: tiered-block"]
        direction LR
        hot[("Hot volume<br/>SSD PVC")]
        cold[("Cold volume<br/>bulk PVC")]
        hot -.->|moveFactor| cold
    end

    subgraph cached["storageModel: cached-object"]
        direction LR
        cache[("Cache disk<br/>PVC or NVMe")]
        objstore[("Object store<br/>durable copy")]
        cache -->|miss| objstore
    end

    query --> tiered
    query --> cached

    classDef default fill:#F8FAFC,stroke:#64748B,stroke-width:1px,color:#0F172A
    classDef store fill:#DDF3EC,stroke:#00785A,stroke-width:1px,color:#0B3327
    classDef group fill:none,stroke:#64748B,stroke-width:1px,stroke-dasharray:2 4
    class hot,cold,cache,objstore store
    class tiered,cached group
```

*Figure 1. L4 Detail -- ClickHouse storage models. The dotted arrow is a demotion, not a read.* Up: [Deploying DFE with dfe-infra](README.md).

In `tiered-block`, data **moves**. A part lives on exactly one volume, so the cold volume isn't disposable. Losing it loses those parts. Both volumes need a durable StorageClass.

In `cached-object`, data is **copied**. The object store holds the durable copy, so losing the cache costs a cold cache and nothing else. The data volume still has to be durable. It holds the map from ClickHouse paths to object blobs, and that map can't be rebuilt from the bucket.

## Turning tiering on

The model is fixed at cluster **creation**. The operator doesn't accept a new disk on an existing `ClickHouseCluster`, so switching a live deployment means recreating the cluster and restoring from a replica. That's why the engine holds `clickhouse.storageModel` and `clickhouse.tieredBlock.*` as protected vars and rejects the edit.

The hot tier is the volume the deployment has. Point `clickhouse.storage.storageClass` at the SSD class and size it for the working set. The `tieredBlock` dials describe the cold volume alone.

```yaml
clickhouse:
  storageModel: tiered-block
  storage:
    storageClass: nvme-fast
    size: 200Gi
  tieredBlock:
    coldName: slow
    coldStorageClass: nvme-bulk
    coldSize: 4Ti
    moveFactor: 0.2
```

Both `mode: cluster` and `mode: single` render it. In cluster mode the operator turns the extra claim into a mount at `/var/lib/clickhouse/disks/slow` and registers a ClickHouse disk of the same name. In single mode the StatefulSet mounts the same path and the chart declares the disk, so the two modes end up with an identical on-disk layout. `mode: external` fails the render, because the supplied ClickHouse owns its storage.

`moveFactor` is a property of the storage policy, not of a table. ClickHouse demotes the oldest parts once free space on the hot volume falls below that fraction, for every table on the policy. Nothing in dfe-engine changes.

Age-based demotion (`TTL ts + INTERVAL 7 DAY TO VOLUME 'slow'`) is a separate, optional refinement. It's per-table DDL, so it belongs to dfe-engine's governance rather than to this chart.

**Volume order sets tier priority, and the order is alphabetical.** The Kubernetes API server re-serialises `settings.extraConfig` with sorted keys, so the order written in the chart isn't the order ClickHouse sees. Name the volumes `hot` and `cold` and `cold` comes first, which makes the bulk disk the hot tier. There's no error, and every insert lands on the wrong device. So `clickhouse.tieredBlock.coldName` must sort after `default`, and the chart fails the render if it doesn't:

```console
clickhouse.tieredBlock.coldName "cold" sorts before "default", which silently
inverts the tiers -- the bulk volume would become the hot one. Pick a name
sorting after "default" (e.g. slow, tier2, warm)
```

The same ordering rule is why the `cached-object` cache disk is named `s3_object_cache` behind `s3_object`: ClickHouse builds a cache only after the disk it wraps. Those disk names, the policy names and the cold volume name are runtime identity, not labels. Renaming one on a live deployment is a rebuild. See "Runtime identity" in [storage.md](storage.md).

## moveFactor needs two separate devices

Demotion triggers on the hot volume's free space as a fraction of its total, so the two tiers must be separate devices. We tested it on a cluster whose hot tier was an SSD root with 55.7 GiB free of 61 (a free ratio of 0.91) and whose cold tier was a separate 491 GiB spinning disk. One node, one dataset, changing nothing but the value:

| moveFactor | Where new parts ended up |
| --- | --- |
| 0.95 | `slow`, the spinning tier -- demoted with no TTL and no manual move |
| 0.2 | `default`, the SSD tier -- stayed hot |

Point both StorageClasses at the same filesystem and both tiers report identical free space, so demotion either never fires or fires constantly. That also makes tiering untestable on a single-filesystem provisioner such as `local-path`.

Check a deployment with:

```sql
-- Tiers resolved as intended. volume_priority 1 must be the fast one.
SELECT policy_name, volume_name, volume_priority, disks, move_factor
FROM system.storage_policies;

SELECT name, type, path, free_space, total_space FROM system.disks;

-- Where parts actually live.
SELECT disk_name, count(), formatReadableSize(sum(bytes_on_disk))
FROM system.parts WHERE active GROUP BY disk_name;
```

## The object-store path

`cached-object` covers S3, GCS and any S3-compatible on-prem store through its endpoint. Its dials live under `clickhouse.objectStore` (see [clickhouse.md](clickhouse.md)). The one worth setting deliberately is the request bound.

Left at the server defaults, a read against an unreachable store blocked for over nine minutes before failing. `retryAttempts: 1`, `connectTimeoutMs: 2000` and `requestTimeoutMs: 5000` brought the same failure down to 10.4 seconds. Those numbers are a demonstration, not a recommendation. One retry is aggressive against S3, which returns transient 5xx routinely, so pick production values against the store you run. Don't leave them unbounded, because a nine-minute stall on a dashboard is indistinguishable from an outage.

Against GCS, set `supportBatchDelete: false`. GCS rejects the batch-delete call ClickHouse otherwise makes.

Azure Blob, `local_blob_storage` and a dedicated cache PVC are tested or partly tested and deliberately not surfaced as values. They're follow-up cells with their evidence and their blocking work in [storage.md](storage.md), alongside the unbuilt `tiered-object` and `cached-block` models.
