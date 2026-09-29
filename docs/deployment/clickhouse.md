# ClickHouse: targets, operators, and the swap seam

ClickHouse is the DFE data layer. Which ClickHouse -- and which operator, if
any -- is a per-deployment choice behind the `clickhouse-cluster` chart's
`clickhouse.mode` value, per the documented-fork rule (same genre as
[AUTOSCALING.md](../AUTOSCALING.md)).

## The DFE 2.2 target matrix

| Target | How | Status |
| --- | --- | --- |
| **Official ClickHouse operator** (clickhouse.com CRDs) | `mode: cluster` -- ClickHouseCluster CR + DFE-owned Keeper, ReplicatedMergeTree | Public default for the scale tier |
| **ClickHouse Cloud** (or any BYO endpoint) | `mode: external` -- deploy nothing, connect to the supplied endpoint | Public default for managed deployments |
| Standalone server | `mode: single` -- plain StatefulSet, no operator, MergeTree | Small/test tiers |
| **HyperI internal operator + CH fork** (private cloud) | Retired as of DFE 2.2 | See [Migrating from DFE 2.x before 2.2](upgrades.md#migrating-from-dfe-2x-before-22) |
| Altinity operator | -- | **NOT tested.** Nothing in this stack has run under it |

## History and rationale

Until ClickHouse published an operator of its own, the Kubernetes options were
the Altinity operator or building your own. HyperI built its own: an internal
operator paired with a custom ClickHouse fork whose engine put an SSD cache in
front of bulk storage, run on private-cloud deployments. That pairing beat
upstream ClickHouse in a couple of specific use cases over the two years
HyperI maintained it. The retired operator and fork are described only in
general terms here, never by name.

DFE 2.2 retires the pairing. A small performance loss in those use cases buys
much less code to maintain, lower operational risk, and a storage model held
in common across on-prem and cloud deployments. The official ClickHouse
operator plus the `cached-object` storage model -- an object-store disk with a
local read-through cache in front of it, under the `s3_cached` policy --
replaces it as the supported path, and is now the default wherever an
object-store endpoint exists. A deployment still on the retired pairing
migrates onto DFE 2.2 by following
[Migrating from DFE 2.x before 2.2](upgrades.md#migrating-from-dfe-2x-before-22).

When ClickHouse Inc shipped its official operator, DFE 2.2 adopted it as the
public on-cluster default. The decision record (versions.yaml,
`operators.clickhouse-operator`): greenfield stack, single-shard topology, DFE
owns its own DDL, safe Keeper consensus, and ClickHouse partner support --
none of which needed Altinity's extra machinery. ClickHouse Cloud is the other
main public target via `mode: external`.

The Altinity operator was never adopted and the stack has NOT been tested
against it. Treat it as unsupported: its CRDs, defaults, and upgrade
behaviour differ from the official operator's, and no DFE validation cycle
has ever run on it.

## Storage model: `local`, `cached-object` or `tiered-block`

`clickhouse.storageModel` is a separate axis from `clickhouse.mode` and is
fixed for the life of the deployment. It ships empty and is derived --
`cached-object` when `clickhouse.objectStore.endpoint` is supplied, `local`
otherwise, where the PVC is the capacity ceiling -- and a value written here
overrides the derivation. Models are named `<family>-<bulk>`: the family says
whether parts MOVE to the bulk store or are COPIED to it, the bulk half says
what that store is. Every combination and its evidence:
[storage.md](storage.md).

`cached-object` renders a `storage_configuration` with an `s3` disk, a
`cache` disk in front of it, and a `s3_cached` policy set as the server-wide
`merge_tree.storage_policy` -- so the engine's DDL needs no per-table
`storage_policy`. The cache is a separate volume, chosen by
`clickhouse.objectStore.cache.volume`: the default `pvc` places it on the data
PVC alongside the part metadata, while `instance-store` puts it on an
`emptyDir` over the node's local NVMe instead, sized by
`clickhouse.objectStore.cacheSize` -- see "Dimension 4: cache placement" in
[storage.md](storage.md) for both. The same fragment reaches both modes:
`settings.extraConfig` on the operator CR for cluster, a
`config.d/dfe-storage.yaml` ConfigMap entry for single. `external` mode
refuses it -- the supplied ClickHouse owns its own storage.

Credentials are never in the values. The disk sets
`use_environment_credentials`, and the chart wires `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` from a Secret ESO materialises from
`<project>/<env>/clickhouse/s3` -- the same seam `mode: external` uses for
its password.

`tiered-block` needs no object store at all: it claims a second, cheaper
volume and replaces the default storage policy with a ranked one, so new
parts land on the fast volume and the oldest are demoted once free space
there falls below `clickhouse.tieredBlock.moveFactor`. Data MOVES rather than
being copied, so both volumes must be durable. Full surface, the naming trap
that inverts the tiers, and the measured demotion evidence:
[clickhouse-tiering.md](clickhouse-tiering.md).

Changing the model on a live deployment strands every part already written,
so the engine holds `clickhouse.storageModel`, `clickhouse.objectStore.*` and
`clickhouse.tieredBlock.*` as protected vars and refuses the edit.

## Retention

Retention is time-only. Every DFE time-series table, the `otel_*` tables included, drops rows older than the deployment default TTL: 90 days, set at deploy by `retention.default_ttl_days` in the deployment dial, which reaches the engine as `DFE_CLICKHOUSE_DEFAULT_TTL_DAYS` (0 = no TTL). A changed value there reaches tables created afterwards, not the ones already deployed. On a running deployment, an engine carrying dfe-engine#652 takes `PUT /api/v1/system/retention` from an admin and applies it to every table that follows the default with `ALTER TABLE ... MODIFY TTL`.

The server's own system log tables are a separate knob, `clickhouse.systemLogTTLDays` in the `clickhouse-cluster` chart: 7 days for `metric_log`, `asynchronous_metric_log` and `part_log`, 30 for `query_log`, in both cluster and single mode. `clickhouse.systemLogsOff` switches `text_log` and `trace_log` off by removing their config sections: nothing in DFE reads either, and the server log already reaches `otel_logs`. A table already created stays and stops being written. The server applies a changed value at its next start by renaming the old table to `<table>_0` and creating a new one. The `_0` copy keeps its rows, so on an upgrade from a chart without these TTLs, list and drop them on each server:

```sql
SELECT name, formatReadableSize(total_bytes) FROM system.tables
WHERE database = 'system' AND match(name, '_log_[0-9]+$');
DROP TABLE system.query_log_0;   -- one per name listed
```

The chart does not drop them: nothing in DFE writes to the `system` database. The `_0` tables stop growing the moment they are renamed.

Both knobs, and `clickhouse.serverLog`, are server config, so they apply only where this chart runs the ClickHouse server (`mode: single` or `cluster`). On ClickHouse Cloud or any other `mode: external` server the chart renders no server config and the provider owns it; `SHOW CREATE TABLE system.query_log` shows the TTL in force there. The dfe-docker compose stack configures its own ClickHouse outside this chart.

## Sizing the disk

Nothing bounds the disk by size, so the volume has to hold the TTL window. Per replica:

```text
disk >= (D x T + F) / 0.85
```

- `D` -- bytes a day on disk across the TTL'd tables: the `otel_*` tables plus every source table.
- `T` -- the TTL in days.
- `F` -- about 2.4 GiB: the system log tables at their TTLs plus the server log's 400 MiB cap.
- `0.85` -- 15% free. Merges write the new part before dropping the old ones, and on a node-local volume the kubelet evicts at its own threshold (by default 10% free on the node filesystem, 15% on the image filesystem).

Measured on an idle mesh-tier stack with no traffic, one replica, `D` for the `otel_*` tables alone is about 0.2 GiB a day, and the system log tables add about 0.18 GiB a day until their TTLs reach it. With the 90-day default an idle replica settles near 20 GiB. That figure is from a short window on one deployment shape; measure your own.

| Volume per replica | Idle telemetry at 90 days uses | Most `D` it carries at 90 days | Most `D` at 30 days |
| --- | --- | --- | --- |
| 100Gi | 20% | 0.9 GiB/day | 2.8 GiB/day |
| 300Gi | 7% | 2.8 GiB/day | 8.4 GiB/day |
| 1Ti | 2% | 9.6 GiB/day | 28.9 GiB/day |

To measure `D`, run this on any one server after a full day of normal load. The time-series tables partition by the day a row was loaded, as `toDate` on the `otel_*` tables and `toYYYYMMDD` on `main` and `detection`, so yesterday's partition in either spelling is one day's bytes:

```sql
SELECT database, table, formatReadableSize(sum(bytes_on_disk)) AS per_day
FROM system.parts
WHERE active
  AND partition IN (toString(yesterday()), toString(toYYYYMMDD(yesterday())))
GROUP BY database, table
ORDER BY sum(bytes_on_disk) DESC;
```

Add the rows up for `D`. `F` for your own servers is `sum(bytes_on_disk)` over `system.parts` where `database = 'system'`, once the system tables have run a full TTL window. Under `cached-object` the parts live in the bucket, and the volume holds their metadata and the cache instead.

## Where the pieces live

- Chart: `helm/charts/clickhouse-cluster` (modes above; keeper bundled for
  cluster mode).
- The storage matrix across both data stores: [storage.md](storage.md).
- SSD tiering over a bulk volume, opt-in per deployment:
  [clickhouse-tiering.md](clickhouse-tiering.md).
- Storage-model assertions: `scripts/tests/test_storage_model.py`.
- Server log bounds and system log TTLs: `scripts/tests/test_clickhouse_server_log.py`.
- Operator install: `argocd/appsets/layer-scale.yaml` (wave 3, scale tier
  only -- single mode needs no operator).
- Version pins + the operator decision record: `versions.yaml`
  (`operators.clickhouse-operator`, `services.clickhouse-version`).
