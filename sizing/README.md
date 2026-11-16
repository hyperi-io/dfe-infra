<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      sizing/README.md
Purpose:   What sizing.yaml is, the rules it is written under, and how to add a
           provider or a ratio to it.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# sizing.yaml -- the ratios a deployment is sized from

`sizing.yaml` holds every number the resolver turns a throughput estimate into a
cluster with: MB/s per vCPU, the RAM and disk formulas, compression, event size,
the `focus` headroom multipliers, the tyre-kick floors, the per-provider
ceilings, retention defaults, the partition rules and the locked fields.

The operator turns two dials -- `sizing.ingest_gb_per_day` (or nothing, and the
floor applies) and `focus` -- and everything else is derived. Tofu and the charts
receive numbers; they never compute them. So changing how a deployment is sized
is an edit here, not a code change.

## Every ratio carries its provenance

A ratio is any map with a `value:`, and it must also carry `unit`, `applies_to`,
`source`, `read`, `confidence` and `spot_test`. `source` is an absolute URL or a
repo path (`helm/charts/kafka/templates/kafka.yaml:43`), `read` is the ISO date
that source was read, and `confidence` is one of `vendor-documented`,
`benchmark-named-hardware`, `rule-of-thumb`, `dfe-core-evidence`, `measured` or
`measure-only`.

`scripts/validate_sizing.py` refuses the file otherwise. A researched estimate is
welcome, labelled as one; a number whose origin nobody can reconstruct in six
months is not. A `source` of `sizing/sizing.yaml` means a product decision taken
here with no external source to cite.

`spot_test` names the one ten-minute test that would promote the ratio to
`measured`. Nothing here is sized by a scale test.

## Reading the ratios

`confidence` levels: `vendor-documented` (the vendor publishes the number), `benchmark-named-hardware` (a published benchmark on stated hardware), `rule-of-thumb` (judgement, not measured), `dfe-core-evidence` (carried over from the 2.1 estate), `measured` (our own spot test, recorded under `sizing/measurements/`) and `measure-only` (no constant exists, the value is `unmeasured` and the resolver reads it off the running system). `unit` is `formula` or `policy` where the value is an expression or a named policy.

- Brokers size for the peak, which is the daily average times `event.peak_factor`. Broker storage sizes from the average, because a retention window of a day or more spans the whole diurnal curve and peak times retention would count the evening twice.
- Broker disk holds every event raw on `<source>_land`, plus the `sizing.transformed_share` of them enriched on `<source>_load`, which is why the formula multiplies by `1 + transformed_share x enrichment_growth_factor`.
- ClickHouse RAM sizes from the frequent-access band (`ram_to_compressed_frequent_access_*`) over `hunt_window_days` of retained data, because hunts read recent data. The long-retention band over the whole retention is printed beside it in the report.
- Keeper IOPS are parts created a second (average ingest over `loader_flush_bytes`) times `raft_appends_per_part`, floored at `keeper_idle_iops` so a deployment with no estimate still gets a real volume. The spot test measured 14,782 Keeper packets received per GB at 1 MiB flushes against 1,061 at 16 MiB (13.9x fewer), and about 14.8 a part at either size. It is in `measurements/2026-09-13-clickhouse-spot-test.md`.
- The root volume holds the OS, images and logs. `root_volume_demand_*` is what a quiet root disk asks of the instance. gp3's free minimum (3,000 IOPS / 125 MiB/s) is what it is provisioned at, never the demand.
- `daemonset_*`, `node_memory_overhead_fraction` and `node_max_pods` set how much node memory is left for pods. The resolver reads no ENI limits, so it uses nodeadm's 110-pod fallback for the kubelet reserve.
- `wire_to_disk_*` is uncompressed bytes in over compressed bytes on disk, which is not the parts-to-compressed ratio ClickHouse reports. `wire_to_disk_synthetic_floor` comes from a high-entropy synthetic corpus, a lower bound that does not replace `wire_to_disk_default` or the vendor band.
- `merge_cpu_fraction` is the elapsed-time share of merges in `system.part_log`, which has no per-event CPU column. `jsoneachrow_parse_factor` and the ZSTD figures are synthetic runs, recorded in the same file.
- The Kafka partition floor is `ceil(peak_mb_s / per_partition_mb_s_cap)` where a provider caps per-partition throughput, so it rises with `peak_factor`.
- `mb_s_per_vcpu_kind_check` and `partition_scale_footprint_delta_kind_check` come from one kind run (`measurements/2026-09-13-kind-scale-proof.md`). They support the floors, they are not ceilings.

## No lists

The ops scripts read YAML with `scripts/yaml_subset.py` -- nested maps and
scalar values, no third-party parser, because dfe-infra ships no third-party
Python. A list is therefore a map keyed by name, or a comma-separated scalar
(`applies_to`). Duplicate keys inside one map are an error rather than a silent
overwrite.

## The resolver

`scripts/resolve_sizing.py` turns these ratios into a deployment: a dial in, the
target-agnostic core derived here, then concrete shapes from
`shapes/compute-shapes.yaml` and the cloud's live API.

    python3 scripts/resolve_sizing.py --dial deployment.yaml --live

| Artefact | What it is |
|---|---|
| `shapes/resolved/<cloud>-<region>.json` | the committed shape answer, merged over what is there, so API drift is a reviewed diff. Keyed by region: instance-generation availability is not one worldwide |
| `sizing.auto.tfvars.json` | `node_pools` and `resolved_shapes`, and nothing the root does not declare. Written at the ROOT of `--out`, beside `render_dial.py --tofu`'s own `dial.auto.tfvars.json` -- OpenTofu auto-loads `*.auto.tfvars.json` only from the root module directory, never a subdirectory. This script is the single writer of `node_pools`: it merges the dial's own `node_pools.system` (a group the deployer sizes by hand) with the pools it derives, so the two producers never collide on the same variable. `resolved_shapes` also carries the `toolbox` shape, which the toolbox instance and the tunnel forwarder launch on and which never becomes a pool, and every shape's `root_volume` type and size |
| `sizing/<tier>.values.yaml` | only keys the two charts already read |
| `sizing/<tier>.data-charts.values.yaml` | the Kafka and ClickHouse keys of that overlay a later edit may move. layer2-data reads the deploy repo's copy under its `infra/` overlays, and bootstrap seeds it into a bundled deploy repo |
| `sizing/<tier>.report.md` | what was sized, from which ratio, at which confidence, in which cost bucket, and where the ceiling is. Prices choose the shapes and are never printed -- see `docs/deployment/aws.md#how-costs-are-described` |

`--fixtures scripts/tests/fixtures/sizing` resolves against captured answers
instead of calling AWS; `capture` refreshes them and refuses to write an account
id, an ARN or a private address.

Scale tier only. No estimate means the tyre-kick floor, with the throughput it
carries reported rather than targeted; above the generator cap it refuses with
the professional-services message and exit status 2.
`scripts/tests/test_resolve_sizing.py` snapshots every deployment in the matrix,
so a ratio edit here shows every deployment it moves.

## Adding to it

- A ratio needs the map with all seven fields, then
  `python3 scripts/validate_sizing.py`.
- A Kafka provider is a key under `kafka:` carrying at least `ram_formula` and
  `disk_formula` -- memory is per provider because Redpanda bypasses the page
  cache that JVM brokers live on -- plus its ceiling under `ceilings:` and its
  name in the validator's `APPLIES_TO`.
- A cloud needs nothing here. Clouds live in `shapes/compute-shapes.yaml`.
