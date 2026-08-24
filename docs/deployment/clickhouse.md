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
| **HyperI internal operator + CH fork** (private cloud) | swaps in for the official operator + this chart | HyperI private cloud only, if required |
| Altinity operator | -- | **NOT tested.** Nothing in this stack has run under it |

## History and rationale

Until recently there was no official ClickHouse operator -- for years the
Kubernetes options were the Altinity operator or building your own. HyperI
built its own: an internal operator evolved over time alongside a custom
ClickHouse fork whose engine adds SSD caching in front of spinning disk
(n-tier storage). That pairing remains the ClickHouse layer of the HyperI
private cloud, and a private-cloud deployment can swap it in for the official
operator and this chart if required. This is a public repo, so the internal
operator, fork, and chart are deliberately not referenced by name or detail
beyond that.

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

## Where the pieces live

- Chart: `helm/charts/clickhouse-cluster` (modes above; keeper bundled for
  cluster mode).
- SSD tiering and caching over the official operator, opt-in per deployment:
  [clickhouse-tiering.md](clickhouse-tiering.md).
- Operator install: `argocd/appsets/layer-scale.yaml` (wave 3, scale tier
  only -- single mode needs no operator).
- Version pins + the operator decision record: `versions.yaml`
  (`operators.clickhouse-operator`, `services.clickhouse-version`).
