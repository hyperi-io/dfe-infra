# Upgrades

Every upgrade is a committed diff in the deployment repo, applied in a
declared order, with a pre-flight that names what cannot be rolled back.
Four kinds of upgrade share that rule.

## Stack upgrade

Bump `pins.yaml` in the deployment repo. One pin selects the certified stack,
`channel` picks the maturity, and `overrides` move one component with a
support-drift notice. `dfe-stack resolve` emits the values and Argo CD syncs.

The order is data, `upgrade-order.yaml` at the root of this repo: operators
first, with their stored-version conversions run on the running cluster
BEFORE the operator that drops the old version starts; then the data
services, Keeper before ClickHouse, the Kafka brokers under the operator's
rolling update with `metadata.version` finalised after a soak; then the apps.
`dfe-ops upgrade` (below) walks that file so the order is never applied by
hand; see it for a deployment-repo driven upgrade.

A Kafka version bump can hit a server-side-apply field-ownership conflict:
the Strimzi operator's own client also writes fields like `KafkaNodePool`
`.spec.storage.volumes` or `KafkaUser` `.spec.authorization.acls`, and if
Helm's prior apply already owns them, the upgrade fails with `Apply failed
with 1 conflict: conflict with "fabric8-kubernetes-client"`. Resolve it with
`kubectl apply --server-side --force-conflicts --field-manager=helm -n
<namespace> -f <rendered manifest>` -- `helm` because that is Helm's own
default field manager (the base name it runs under), so reusing it hands
ownership back cleanly and the next plain `helm upgrade` no longer conflicts.
This is safe only when the rendered value already matches the live one (the
operator is re-asserting a default, not diverging from the chart); diff the
object first, and never force through a field the operator computes on its
own, such as a broker-assigned identifier.

## dfe-ops upgrade

`dfe-ops upgrade <verb> --deploy <dfe-deploy checkout> [--to <stack>]` drives a
stack upgrade from a deployment repo's own `pins.yaml`, walking
`upgrade-order.yaml` stage by stage. `--to` defaults to versions.yaml's
`current` pointer; the FROM stack is read from the deploy's `pins.yaml`
(`base.dfe-infra`).

- `plan` diffs FROM -> TO, grouped by stage, each step carrying its
  `before`/`finalise`/`pair`/`rollback` note. Runs `dfe-stack compat-check
  --strict` for TO and writes the numbered plan to
  `<deploy>/upgrades/<from>-to-<to>.md`. Pass `--dial <deployment.yaml>` (with
  `--fixtures` or `--live`) to also run `resolve_sizing.py`'s locked-change
  classifier against the deploy's committed `sizing/resolved.yaml` -- a
  LOCKED field moving without `--migrate` blocks the plan. Exit 0 clean, 1
  blocked (compat-check failed, or an unmigrated locked change), 2 the
  deploy repo or stack name do not resolve.
- `preflight` is the gate `apply` will not run without: the deploy repo is
  clean, the cluster answers, every Argo Application is Synced and Healthy,
  no KafkaRebalance is running, ClickHouse carries no merge past
  `--clickhouse-merge-threshold`, the Strimzi stored-version conversion
  already ran (checked only when the plan crosses the 0.x -> 1.x boundary,
  read from the CRD's `status.storedVersions`), the on-prem node capacity
  holds the new sizing (`check_node_capacity.py`), and a backup marker exists
  at `--backup-marker` when the plan carries a one-way step. Each check
  prints `PASS`/`FAIL` with the evidence line that decided it.
- `apply` runs preflight, then walks the plan stage by stage: bumps
  `pins.yaml`'s `base.dfe-infra` via a surgical field edit (never a hand
  rewrite), re-runs the resolver with `--migrate` when `--dial` is given,
  commits the stage (`chore(upgrade): <stack> stage <n> -- <keys>`), pushes
  only with `--push`, and waits for Argo to report every Application Synced
  and Healthy, bounded by `--timeout`. Confirms before each stage unless
  `--yes`; `--stop-before <stage-key>` halts before a named
  `upgrade-order.yaml` stage, touching nothing in it or after. A `before`
  note this repo already has a program for (today, only the Strimzi
  conversion) runs automatically; any other needs a confirmed "I ran this by
  hand". A reached `finalise` note prints and stays pending unless
  `--finalise` is given, which asks whether the soak is over and, on yes,
  writes `upgrades/<from>-to-<to>.finalised` -- the marker `rollback` reads.
  `--dry-run` prints every command, finalise and stop-before included, and
  touches nothing. Apply stops at the first failure and prints that step's
  rollback note.
- `rollback --to <stack>` is the reverse plan. It refuses by name a step
  carrying `rollback: none` with no `finalise` (unconditionally one-way), or
  a `finalise`-bearing step whose finalise has ALREADY run -- read from the
  marker above, not the pin diff alone. A `finalise`-bearing step with no
  marker yet reverses like any other step, noting the soak can be abandoned
  safely. `--check-cluster` also reads the live Kafka CR's
  `status.kafkaMetadataVersion` and refuses when it already shows the
  bumped value even with no marker -- a finalise run by hand, outside this
  tool.

## Re-size

Re-run the sizing resolver with a new estimate, focus or ratio and diff the
result against `sizing/resolved.yaml`. Every changed field is classified:

- LIVE-SAFE: broker count up (the rebalancer spreads data onto the new
  brokers), ClickHouse replicas up, a PVC growing, an instance type moving
  under a Karpenter node roll, retention, any dynamically updatable knob.
- DRAIN-FIRST: broker count down. `remove-brokers` moves data off the broker
  before its pod goes.
- LOCKED: partition count, `storageModel`, MSK Standard against Express,
  combined against separate KRaft controllers, the cloud token, the AZ
  count. Refused without `--migrate` and the runbook it names. The lock list
  is `governance/policies/sizing-locks.yaml` in the deployment repo, beside
  the storage-layout lock the engine already enforces.

## Platform upgrade

EKS: the control plane first, then Karpenter drift rolls the nodes. MSK: the
Kafka version in place, Express broker size vertically. GKE and AKS follow the
same shape when their roots exist. Pre-flight checks version skew from
`versions.yaml`: librdkafka against the broker, the ClickHouse client against
the server, Karpenter against the Kubernetes version.

## What cannot be rolled back

- Kafka's `metadata.version` bump is one way, so the roll and the finalise are
  separate steps with a soak between them.
- ClickHouse downgrades only within the same LTS line.
- Kafka data is a buffer: an upgrade window shorter than the topic retention
  loses nothing. ClickHouse on `cached-object` keeps its parts in the object
  store.

Pre-flight snapshots the Argo application versions, `sizing/resolved.yaml`
and the tofu state, and prints the one-way steps before asking to continue.

`dfe-ops upgrade rollback --to <stack>` (above) refuses by name a step whose
finalise has already run -- read from the `upgrades/<from>-to-<to>.finalised`
marker `apply --finalise` writes, never from the pin diff alone. Between the
Kafka broker roll and its `metadata.version` finalise, soak the cluster
under normal ingest for 24 hours by default, watching consumer lag,
under-replicated partitions, ClickHouse's merge backlog and insert errors,
and confirming KEDA and Cruise Control both stay quiet. A problem during the
soak ends it early: `dfe-ops upgrade rollback --to <stack>` reverses the pin
like any other step, because no marker exists yet -- no manual git revert
needed. `--check-cluster` also refuses when the live Kafka CR already shows
the bumped `status.kafkaMetadataVersion`, catching a finalise run by hand
outside this tool. The full per-stage runbook, the sizing config-vs-data
rule for a locked field, and what `apply --finalise`/`--stop-before` do at
finalise time are in [upgrade-rollback.md](upgrade-rollback.md).
