---
sidebar_position: 1
---

# ClickHouse

ClickHouse is where DFE stores every event. This page answers which ClickHouse to run, what an existing one must provide, how we deploy it on-prem, and where the fork from DFE 2.0 and 2.1 went.

Which ClickHouse is a per-deployment choice in the `clickhouse-cluster` chart's `clickhouse.mode`: one we deploy on your cluster, or one you run, on your servers or from a cloud provider. An existing ClickHouse must meet the version floor below and provide everything in [What a self-hosted server provides](#what-a-self-hosted-server-provides). On-prem we deploy the official operator, with the `cached-object` storage model wherever an S3-compatible store exists: an object store with an SSD cache in front. It is our nearest equivalent of ClickHouse Cloud's storage, so on-prem and cloud deployments share one storage model.

## The DFE 2.2 target matrix

| Target | How | Status |
| --- | --- | --- |
| **Official ClickHouse operator** (clickhouse.com CRDs) | `mode: cluster` -- ClickHouseCluster CR + DFE-owned Keeper, ReplicatedMergeTree | Public default for the scale tier |
| **ClickHouse Cloud** (or any BYO endpoint) | `mode: external` -- deploy nothing, connect to the supplied endpoint | Public default for managed deployments |
| Standalone server | `mode: single` -- plain StatefulSet, no operator, Keeper embedded, a cluster of one | Small/test tiers |
| **HyperI internal operator + CH fork** (private cloud) | Retired as of DFE 2.2 | See [Migrating from DFE/XDR 2.1.x and earlier](migrating-from-2.1.md) |
| Altinity operator | -- | **NOT tested.** Nothing in this stack has run under it |

An external ClickHouse must run 24.10 or later. The collector's `otel_writer` grant uses a table wildcard (`INSERT ON dfe.otel_*`) that an older server rejects, so the collector can't insert.

## What a self-hosted server provides

This is the checklist for a ClickHouse you run, whether under the Altinity operator or on bare servers.

dfe-engine creates every table as an argumentless `Replicated*` engine inside a Replicated database. So it rejects a self-hosted server that lacks Keeper, the `{cluster}`, `{shard}` and `{replica}` macros, or a `remote_servers` cluster the `{cluster}` macro names. On a cluster of more than one server its SQL users and roles must live in Keeper as well, as the first writable `user_directories` entry. Both modes this chart deploys provide all of it:

| | `mode: cluster` | `mode: single` |
| --- | --- | --- |
| Keeper | The `KeeperCluster` the CR references, under an identity the operator mints | Embedded in the server on loopback 9181, under the identity in the `dfe-clickhouse-keeper-identity` Secret |
| Macros and cluster | Written by the operator: `{cluster}` is `default`, holding every replica | `config.d/dfe-replication.xml`: `{cluster}` is `dfe`, holding the one server |
| Access storage | The operator's base config lists `users_xml` then `replicated` | `dfe-replication.xml` replaces the image's list with `users_xml` then `replicated` |

The `user_directories` list has to replace the image's default rather than add to it. An appended `<replicated>` sits behind `local_directory`, which then takes every `CREATE ROLE`. Keeper's log and snapshots live on the data PVC beside the tables, so the two are never lost apart. The Keeper identity is generated once and never rotated, because the server can read the znodes created under it and no others. Under another value the server exits with code 231 at start. Rationale and the reference config: dfe-schemas `docs/clickhouse-platform.md`. <!-- doc-paths: ignore -->

In single mode `config.d/dfe-listen.xml` replaces the image's `docker_related_config.xml`. Keeper, raft and interserver bind loopback so no other pod reaches them, and 8123, 9000 and 8443 alone listen on the pod's address. Those three listen on IPv4 alone, where the image's file listened on IPv6 as well, so single mode fails its probes on an IPv6-only cluster. With `tcp_port` removed, the replica has `is_local` 0 in `system.clusters`. A hand-run `clusterAllReplicas('dfe', ...)`, which DFE never issues, then dials the server as `default` and fails with `AUTHENTICATION_FAILED`.

In cluster mode the `KeeperCluster` turns on the operator's NetworkPolicy, and `network-policies` leaves the keeper pods out of `data-ingress-policy` (`operatorIngressScoped`). Operator-labelled pods from any namespace ([accepted risk](../threat-model.md#accepted-risks)) and the ClickHouse servers that reference the keeper reach 2181, 2281 and 9123, and the other keepers alone reach raft on 9234. Policies are additive, so when `data-ingress-policy` selected them every DFE, data and operator namespace reached every port on the keepers. Pinned by `scripts/tests/test_keeper_ingress.py`.

## History and rationale

Until ClickHouse published an operator, the Kubernetes options were the Altinity operator or building your own. DFE 2.0 and 2.1 ran HyperI's: an internal operator and a ClickHouse fork, on private-cloud deployments. The Vector-based loader of those releases needed the fork's SSD cache in front of bulk storage.

The ground-up Rust dfe-loader in DFE 2.2 resolved those pain points, which let the fork and the internal operator go. The official ClickHouse operator plus the `cached-object` storage model replaces them as the supported path. That model is an object-store disk with a local read-through cache in front of it, under the `s3_cached` policy, and it's the default wherever an object-store endpoint exists. A deployment still on the retired pairing follows [Migrating from DFE/XDR 2.1.x and earlier](migrating-from-2.1.md).

When ClickHouse Inc shipped its official operator, DFE 2.2 adopted it as the public on-cluster default. The decision record (versions.yaml, `operators.clickhouse-operator`): greenfield stack, single-shard topology, DFE owns its DDL, safe Keeper consensus, and ClickHouse partner support. None of that needed Altinity's extra machinery. ClickHouse Cloud is the other main public target via `mode: external`.

The Altinity operator is unsupported. DFE never adopted it or ran a validation cycle on it, and its CRDs, defaults and upgrade behaviour differ from the official operator's.

## Storage model: `local`, `cached-object` or `tiered-block`

> **Coming from Splunk or Elastic?** The TTL on each table sets retention here, and the storage model sets where parts sit while they wait. The storage policy is server-wide, so the engine's DDL needs no per-table `storage_policy` and queries use the same table and SQL across both tiers.

> **Coming from a lakehouse?** You'd partition by date and let a bucket lifecycle rule expire the old files. Here the table's TTL removes rows past retention inside ClickHouse, and the storage model decides whether parts wait on local disk or in object storage. More in [coming from a lakehouse](../../docs-site/content-root/coming-from-lakehouse.md#doing-it-the-dfe-way).

`clickhouse.storageModel` is a separate axis from `clickhouse.mode` and is fixed for the life of the deployment. It ships as `auto` and is derived: `cached-object` when `clickhouse.objectStore.endpoint` is supplied, `local` otherwise, where the PVC is the capacity ceiling. An empty value means the same, and a model written here overrides the derivation. `tiered-block` is never derived and is reached only by setting it. Models are named `<family>-<bulk>`. The family says whether parts MOVE to the bulk store or are COPIED to it, and the bulk half says what that store is. Every combination and its evidence: [storage.md](storage.md).

`cached-object` renders a `storage_configuration` with an `s3` disk, a `cache` disk in front of it, and a `s3_cached` policy set as the server-wide `merge_tree.storage_policy`. The cache is a separate volume, chosen by `clickhouse.objectStore.cache.volume`. The default `pvc` places it on the data PVC alongside the part metadata. `instance-store` puts it on an `emptyDir` over the node's local NVMe instead, sized by `clickhouse.objectStore.cacheSize`. See "Dimension 4: cache placement" in [storage.md](storage.md) for both.

The same fragment reaches both modes: `settings.extraConfig` on the operator CR for cluster, a `config.d/dfe-storage.yaml` ConfigMap entry for single. `external` mode fails the render on it, because the supplied ClickHouse owns its storage.

Credentials are never in the values. The disk sets `use_environment_credentials`, and the chart wires `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from a Secret ESO materialises from `<project>/<env>/clickhouse/s3`. `mode: external` uses the same route for its password.

`tiered-block` needs no object store at all. It claims a second, cheaper volume and replaces the default storage policy with a ranked one, so new parts land on the fast volume and the oldest are demoted once free space there falls below `clickhouse.tieredBlock.moveFactor`. Data MOVES rather than being copied, so both volumes must be durable. Full surface, the naming trap that inverts the tiers, and the demotion test evidence: [clickhouse-tiering.md](clickhouse-tiering.md).

Changing the model on a live deployment strands every part written. So the engine holds `clickhouse.storageModel`, `clickhouse.objectStore.*` and `clickhouse.tieredBlock.*` as protected vars and rejects the edit.

## Placement and identity

`clickhouse.podSpread` spreads each shard's replicas, and separately the keepers, across nodes with `maxSkew: 1` on `kubernetes.io/hostname`. It is `DoNotSchedule`, so a replica stays Pending rather than land on a node holding more of its shard than the emptiest eligible node. A one-node cluster is one domain and schedules every replica. Nodes a pod doesn't tolerate still count as domains, because Kubernetes defaults `nodeTaintsPolicy` to `Ignore`. `scripts/tests/test_clickhouse_replica_spread.py` pins the selectors, which match the labels the operator writes.

`nodeScheduling.tolerations` tolerates the `clickhouse` and `keeper` pool taints and does nothing where no node has them. An overlay's list replaces the chart's, because Helm replaces a list rather than merging it. `nodeScheduling.dedicatedPools` makes a pool required: servers on `clickhouse`, Keeper on `keeper`. `layer2-data` fills it from the tainted pools on the cluster secret, and an `instance-store` cache always requires the `clickhouse` pool for its NVMe.

The server pods run as `dfe-clickhouse` and Keeper as `dfe-keeper`, never the release namespace's default account. On AWS the Pod Identity role binds the server account alone, so its name must equal the module variable `clickhouse_object_store_service_account` (`scripts/tests/test_pod_identity_accounts.py` pins it). Keeper never calls S3 and has no association, but a later grant aimed at `default` would otherwise reach it. Neither account mounts a token, because the server calls S3 and Keeper and never the Kubernetes API.

The deploy layer owns one credential, the `default` user's password. ESO generates it in-cluster and copies it into `appNamespace`, which is where dfe-engine reads it to create every other user, and into each of `extraCredentialNamespaces`. Only `default` takes its password from a Secret (`defaultUserPassword`). Any other user needs it inline in the CR. `clickhouse.tls.ca.extraNamespaces` get the CA copy and never the admin credential, so a client outside the app namespace, the OTel collector, can verify the server without being able to log in as `default`.

## Server and Keeper settings

Both modes have these.

- **Tenant isolation.** `custom_settings_prefixes` allows `SQL_`, because the per-`_org_id` RESTRICTIVE row policy reads `getSetting('SQL_current_tenant_id')`, pinned on each tenant's user by dfe-engine. A server refusing the prefix silently applies no tenant scoping. Cluster mode also allows `custom_`: the operator injects `custom_operator_reload_revision` into its profile, and without the prefix the server fails to load its users with `CANNOT_LOAD_CONFIG`. The `system` and `information_schema` databases are deny-by-default, and dfe-engine grants back only what the embedded HyperDX reads (`governance/ch/models.py`, `TENANT_SYSTEM_TABLES`).
- **Async inserts.** The `default` profile sets `async_insert` and `wait_for_async_insert` to 1 for every writer, because async insert has no table-level form. Both match ClickHouse's defaults and are written out so a `compatibility` setting or a later re-default can't move them. `wait_for_async_insert: 0` is refused at render: dfe-loader commits its Kafka offset on the ack, so the rows would be lost with no error. The flush size and timeout stay on ClickHouse's adaptive defaults.
- **Server log.** `clickhouse.serverLog` sets `level` (`trace` to `fatal`), `size` and `count` for the log and its `err.log`, so the disk bound is `2 x (count + 1) x size`, 400M per replica as shipped. Unset, cluster mode takes the operator's trace level with 50 files of 1000M on the data PVC and prints the same trace to stdout, which the OTel collector ships into `otel_logs`. Single mode takes the image's trace level with 10 files of 1000M on the node's disk.

Cluster mode alone:

- **Server profile.** `clickhouse.serverProfile` goes in through the operator CR, and single mode runs no operator. Concurrent query threads are capped at 2 per core of the replica's CPU request, where the default 0 is unbounded and hunts, engine queries and merges share a node. `max_server_memory_usage_to_ram_ratio` is 0.8, since 0.9 of a cgroup limit ends in an OOMKill rather than a `MEMORY_LIMIT_EXCEEDED`. The merge pool is the larger of 16 and 2 threads per requested core, so a 24-core node merges with 48 threads. `parts_to_delay_insert`, `parts_to_throw_insert` and `max_parts_in_total` are left alone, because raising them hides a partition-key fault. `max_server_memory_usage` is derived from the cgroup limit by ClickHouse.
- **Keeper.** The memory limit is 3Gi because Keeper's default log caches are 1 GiB and 500 MiB before it serves a request, so 1Gi would OOMKill under load. The liveness probe is `ruok`, replacing the operator's TCP probe, which opens 2181 and sends nothing, so Keeper logs a Warning on every probe. An empty `keeper.livenessProbe` keeps the operator's. `keeper.fourLetterWords` is limited to read-only words: the operator sends `mntr` to read each keeper's state and the probe sends `ruok`. `keeper.log` takes the fields of `serverLog`, and unset, the operator keeps 50 trace-level files of up to 1000M on the data PVC.

## Retention

Retention is time-only. Every DFE time-series table, the `otel_*` tables included, drops rows older than the deployment default TTL. The default is 90 days, set at deploy by `retention.default_ttl_days` in the deployment dial, which reaches the engine as `DFE_ENGINE_CLICKHOUSE_DEFAULT_TTL_DAYS` (0 = no TTL). A changed value there reaches every table at the engine's next start. On a running deployment, `PUT /api/v1/system/retention` (admin) stores a new default TTL and returns before any table changes. A background pass then runs `ALTER TABLE ... MODIFY TTL` on every table that follows the default, and retries until each one has it, so a ClickHouse that is down at the time takes the value once it's back.

The server's system log tables are a separate dial, `clickhouse.systemLogTTLDays` in the `clickhouse-cluster` chart. It's 7 days for `metric_log`, `asynchronous_metric_log`, `text_log` (which the dfe-hyperdx ClickHouse log source searches) and `part_log`, and 30 for `query_log`, in both cluster and single mode.

`clickhouse.systemLogsOff` switches `trace_log` off by removing its config section. Nothing in DFE reads it, and single mode's image alone turns it on. A created table stays and stops being written.

The server applies a changed TTL at its next start by renaming the old table to `<table>_0` and creating a new one. The `_0` copy keeps its rows, so on an upgrade from a chart without these TTLs, list and drop them on each server:

```sql
SELECT name, formatReadableSize(total_bytes) FROM system.tables
WHERE database = 'system' AND match(name, '_log_[0-9]+$');
DROP TABLE system.query_log_0;   -- one per name listed
```

The chart doesn't drop them, because nothing in DFE writes to the `system` database, and they stop growing once renamed.

Both dials, and `clickhouse.serverLog`, are server config, so they apply where this chart runs the ClickHouse server and nowhere else (`mode: single` or `cluster`). On ClickHouse Cloud or any other `mode: external` server the chart renders no server config and the provider owns it. `SHOW CREATE TABLE system.query_log` shows the TTL in force there. The dfe-docker compose stack configures its ClickHouse outside this chart.

## Sizing the disk

Nothing bounds the disk by size, so the volume has to hold the TTL window. Per replica:

```text
disk >= (D x T + F) / 0.85
```

- `D` -- bytes a day on disk across the TTL'd tables: the `otel_*` tables plus every source table.
- `T` -- the TTL in days.
- `F` -- about 2.4 GiB: the system log tables at their TTLs plus the server log's 400 MiB cap.
- `0.85` -- 15% free. Merges write the new part before dropping the old ones, and on a node-local volume the kubelet evicts at its threshold (by default 10% free on the node filesystem, 15% on the image filesystem).

On one idle stack with no traffic, one replica, `D` for the `otel_*` tables alone is about 0.2 GiB a day, and the system log tables add about 0.18 GiB a day until their TTLs reach it. With the 90-day default an idle replica settles near 20 GiB. That figure is from a short window on one deployment, so measure yours.

| Volume per replica | Idle telemetry at 90 days uses | Most `D` it holds at 90 days | Most `D` at 30 days |
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

Add the rows up for `D`. `F` for your servers is `sum(bytes_on_disk)` over `system.parts` where `database = 'system'`, once the system tables have run a full TTL window. Under `cached-object` the parts live in the bucket, and the volume holds their metadata and the cache instead.

## Where the pieces live

- Chart: `helm/charts/clickhouse-cluster` (modes above, with a Keeper ensemble for cluster mode and Keeper embedded in the server for single mode).
- Keeper, macros, cluster and access storage per mode: `scripts/tests/test_clickhouse_replication.py`.
- The storage matrix across both data stores: [storage.md](storage.md).
- SSD tiering over a bulk volume, opt-in per deployment: [clickhouse-tiering.md](clickhouse-tiering.md).
- Storage-model assertions: `scripts/tests/test_storage_model.py`.
- Server log bounds and system log TTLs: `scripts/tests/test_clickhouse_server_log.py`.
- Replica spread selectors: `scripts/tests/test_clickhouse_replica_spread.py`. Service accounts: `scripts/tests/test_pod_identity_accounts.py`.
- Operator install: `argocd/appsets/layer-scale.yaml` (wave 3, scale tier alone, since single mode needs no operator).
- Version pins and the operator decision record: `versions.yaml` (`operators.clickhouse-operator`, `services.clickhouse-version`).
