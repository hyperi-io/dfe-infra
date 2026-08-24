# ClickHouse SSD tiering and caching

DFE can put recent data on SSD and bulk data on cheap storage, without changing
a single table definition. It is opt-in per deployment via
`clickhouse.tiering.mode` in the `clickhouse-cluster` chart, and off by default.

## Pick a mode

The choice is made for you by what the customer's cluster actually has.

| Mode | Bulk lives on | Fast tier is | Use when |
| --- | --- | --- | --- |
| `off` | one PVC | nothing | Default. Small deployments, or storage that is uniformly fast |
| `tier` | a second PVC on a cheap class | an SSD PVC holding recent parts | Block storage only, no object store |
| `cache` | an S3-compatible object store (S3, GCS, MinIO, Ceph RGW) | an SSD PVC holding a disposable cache | An object store is available |

Azure Blob is not wired up. ClickHouse addresses it with `storage_account_url`,
`container_name` and `account_name`/`account_key` rather than the S3 keys, so it
needs its own branch in the chart and a test against a real account. The chart
rejects `type: azure_blob_storage` rather than rendering config that cannot work.

The constraint that decides it: **ClickHouse cannot cache one local disk on
another.** The `cache` disk type only wraps object storage, so over plain block
storage the supported shape is a ranked storage policy, not a cache. Attempting
otherwise fails at startup with "cached disk is allowed only on top of object
storage".

```mermaid
flowchart LR
    Q[Query] --> P{default storage policy}
    subgraph tier["mode: tier"]
        P --> H["volume default<br/>SSD PVC"]
        P --> C["volume slow<br/>bulk PVC"]
        H -. "demoted when hot fills<br/>(move_factor)" .-> C
    end
    subgraph cache["mode: cache"]
        P --> W["disk bulk_cached<br/>SSD PVC, disposable"]
        W -- "miss" --> O[("object store<br/>durable copy")]
    end
```

## The difference that matters

In `tier`, data **moves**. A part lives on exactly one volume, so the cold
volume is not disposable and losing it loses those parts. Both volumes need a
durable StorageClass.

In `cache`, data is **copied**. The object store always holds the durable copy
and the cache is write-through, so losing the cache costs a cold cache and
nothing else. The local data volume still must be durable: it holds the map
from ClickHouse paths to object blobs, and that map cannot be rebuilt from the
bucket.

## Turning it on

Both modes are fixed at cluster **creation**. The operator does not accept a new
disk on an existing `ClickHouseCluster`, so switching mode on a live deployment
means recreating the cluster and restoring from a replica.

For `tier`, point the existing data volume at the fast class and size the cold
volume:

```yaml
clickhouse:
  storage:
    storageClass: nvme-fast
    size: 200Gi
  tiering:
    mode: tier
    cold:
      name: slow
      storageClass: nvme-bulk
      size: 4Ti
    moveFactor: 0.2
```

For `cache`, supply the endpoint and a Secret holding the credentials:

```yaml
clickhouse:
  tiering:
    mode: cache
    cache:
      storageClass: nvme-fast
      size: 500Gi
      maxSize: 450Gi
    objectStorage:
      type: s3
      endpoint: https://BUCKET.s3.REGION.amazonaws.com/dfe/
      secretName: dfe-clickhouse-object-storage
```

Credentials reach the server as environment variables and are read back with
ClickHouse's `@from_env`, so they never appear in the CR, the generated
ConfigMap, or the preprocessed config on disk.

## No table DDL is required

`moveFactor` is a property of the storage policy, not of a table. ClickHouse
demotes the oldest parts once free space on the hot volume falls below that
fraction, for every table on the default policy. Nothing in dfe-engine changes.

Age-based demotion (`TTL ts + INTERVAL 7 DAY TO VOLUME 'slow'`) is a separate,
optional refinement. It is per-table DDL, so it belongs to dfe-engine's
governance, not to this chart.

## Two naming traps the chart guards

**Volume order sets tier priority, and the order is alphabetical.** The
Kubernetes API server re-serialises `extraConfig` with sorted keys, so the order
written in the chart is not the order ClickHouse sees. Naming the volumes `hot`
and `cold` puts `cold` first, which makes the bulk disk the hot tier, silently
and with no error. The cold volume name must sort after `default`; the chart
fails the render if it does not.

**The bulk disk's metadata path must be set explicitly.** It defaults to
`/var/lib/clickhouse/disks/<name>`, which the operator owns as the mount parent
for additional volumes and the server cannot write to. The chart pins it to
`/var/lib/clickhouse/object_metadata/<name>/` on the durable data volume, which
is where it belongs anyway.

## Verifying a deployment

```sql
-- Tiers resolved as intended. volume_priority 1 must be the fast one.
SELECT policy_name, volume_name, volume_priority, disks, move_factor
FROM system.storage_policies;

SELECT name, type, object_storage_type, path FROM system.disks;

-- Where parts actually live.
SELECT disk_name, count(), formatReadableSize(sum(bytes_on_disk))
FROM system.parts WHERE active GROUP BY disk_name;

-- Cache mode only: hit ratio.
SELECT event, value FROM system.events WHERE event LIKE 'CachedReadBuffer%';
DESCRIBE FILESYSTEM CACHE 'bulk_cached';
```

In `cache` mode `system.disks` also lists the cache PVC as an unused local disk
named after `tiering.cache.name`. The operator registers a disk for every
additional volume; that one carries no data and is expected.

## Not covered here

Benchmarks, per-platform cost modelling, workload identity in place of static
keys, and fault injection against the cache device are spike work, not settled
configuration. Automatic demotion by `moveFactor` in particular depends on the
two tiers being genuinely separate devices with separate free space.
