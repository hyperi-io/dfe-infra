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
| `sizing/<tier>.auto.tfvars.json` | `node_pools` and `resolved_shapes`, and nothing the root does not declare |
| `sizing/<tier>.values.yaml` | only keys the two charts already read |
| `sizing/<tier>.report.md` | what was sized, from which ratio, at which confidence, at what price, and where the ceiling is |

`--fixtures scripts/tests/fixtures/sizing` resolves against captured answers
instead of calling AWS; `capture` refreshes them and refuses to write an account
id, an ARN or a private address.

Scale tier only. No estimate means the tyre-kick floor, with the throughput it
carries reported rather than targeted; above the generator cap it refuses with
the professional-services message and exit status 2.
`scripts/tests/test_resolve_sizing.py` snapshots every deployment in the matrix,
so a ratio edit here shows every deployment it moves.

## Adding to it

- **A ratio**: add the map with all seven fields, then run
  `python3 scripts/validate_sizing.py`.
- **A Kafka provider**: add a key under `kafka:` carrying at least
  `ram_formula` and `disk_formula` -- memory is per provider because Redpanda
  bypasses the page cache that JVM brokers live on -- and add its ceiling under
  `ceilings:`. Add the name to `APPLIES_TO` in the validator.
- **A cloud**: nothing here. Clouds live in `shapes/compute-shapes.yaml`.
