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

**Pointing Argo CD at a newer stack is not a supported upgrade.** Moving a running deployment's dfe-infra ref, the cluster secret's `dfe.hyperi.io/target_revision`, onto another stack syncs every Application to the new ref at once. None of the `before:` steps in `upgrade-order.yaml` runs, and nothing refuses the move today.

Across the Strimzi 0.x to 1.x lift that wedges Kafka. The 1.x CRDs fail Argo CD's server-side diff against CRDs still storing `v1beta2`, so the operator stays on 0.x while its Application reports Synced over a ComparisonError. The kafka chart applies cleanly and asks for a Kafka version the running operator rejects, and the Kafka CR goes NotReady and stops being reconciled. `dfe-ops upgrade preflight` and `apply` both refuse while a Strimzi CRD stores a pre-v1 version, and name the conversion tool to run first.

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
  rewrite), re-runs the resolver with `--migrate` when `--dial` is given (with
  `--fixtures` or `--live`) and copies its `sizing/` output, never its
  OpenTofu inputs, over `<deploy>/sizing/`,
  commits the stage (`chore(upgrade): <stack> stage <n> -- <keys>`), pushes
  only with `--push`, and waits for Argo to report every Application Synced
  and Healthy, bounded by `--timeout`. Confirms before each stage unless
  `--yes`; `--stop-before <stage-key>` halts before a named
  `upgrade-order.yaml` stage, touching nothing in it or after. A `before`
  note this repo already has a program for (today, only the Strimzi
  conversion) runs automatically; any other needs a confirmed "I ran this by
  hand". That check also names the conversion tarball to fetch at the version
  the cluster is RUNNING (`strimzi-v1-api-conversion-<from>.tar.gz`), not the
  target's, because the tool rewrites the CRs the running operator wrote. After
  a stage that bumps the Strimzi operator, apply waits a second time on every
  Kafka CR's `status.operatorLastSuccessfulVersion` reaching the new operator
  version under the same `--timeout`, because the CR's own `Ready` condition
  stays True and stale across the lift and a wait on it returns at once and
  proves nothing. A reached `finalise` note prints and stays pending unless
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

### What a stage moves

One pin, `base.dfe-infra`, selects the whole certified stack, and `apply` sets it to the target at every stage. So the first stage moves every component: its commit, named for that stage's keys alone, carries the whole pin move, and with `--push` Argo CD converges on the full target during that stage's wait. Later stages find the pin already moved and skip their commit unless they add something, such as a finalise marker under `upgrades/`.

The work around the pin still runs in stage order: confirms, `before` checks, `finalise` notes and waits. Preflight still enforces the Strimzi conversion before anything moves. `--stop-before` skips a stage's hooks and waits, not its component versions. Staging the pin itself is open in https://github.com/hyperi-io/dfe-infra/issues/508.

## Re-size

Re-run the sizing resolver with a new estimate, focus or ratio and diff the
result against `sizing/resolved.yaml`. Every changed field is classified:

- LIVE-SAFE: broker count up (the rebalancer spreads data onto the new
  brokers), ClickHouse replicas up, a PVC growing, an instance type moving
  under a Karpenter node roll, retention, any dynamically updatable knob.
- DRAIN-FIRST: broker count down. `remove-brokers` moves data off the broker
  before its pod goes.
- LOCKED: the six fields in `sizing.yaml`'s `locked:` section -- partition
  count, storage model, Kafka provider, combined against separate KRaft
  controllers, the cloud token, the AZ count. The resolver refuses a move in
  any of them without `--migrate` and the runbook it names. In the deployment
  repo, `governance/policies/sizing-locks.yaml` holds the chart keys behind
  three of them as protected vars (`kafka.sizing.*`,
  `kafka.controllerPool.enabled`, `cloud`), and `storage-layout.yaml` holds
  the storage model's (`clickhouse.storageModel`, `kafka.storageModel`). The
  Kafka provider and the AZ count are OpenTofu
  inputs with no chart key, so the resolver is their only lock.

## Platform upgrade

EKS: the control plane first, then Karpenter drift rolls the nodes. MSK: the
Kafka version in place, Express broker size vertically. GKE and AKS follow the
same shape when their roots exist. Pre-flight checks version skew from
`versions.yaml`: librdkafka against the broker, the ClickHouse client against
the server, Karpenter against the Kubernetes version.

## Migrating from DFE 2.x before 2.2

A private-cloud DFE deployment run before 2.2 used an internal ClickHouse
fork with its own SSD cache in front of bulk storage, under an internal
operator (described, not named, in [clickhouse.md](clickhouse.md)).
DFE 2.2 retired that pairing: a small performance loss in a couple of
specific use cases buys much less code to maintain, lower operational risk,
and a storage model held in common across on-prem and cloud deployments. The
replacement is the official ClickHouse operator with the `cached-object`
storage model -- an object-store disk with a local cache disk in front of it,
under the `s3_cached` policy. `cached-object` is now the default wherever an
object-store endpoint exists (`sizing.storage_model: auto`); `local` is the
explicit opt-out.

Migrating off the retired pairing means a second deployment beside the
first. `clickhouse.storageModel` is a protected var for the life of a
deployment (see [clickhouse.md](clickhouse.md)), so the storage model cannot
flip under the running one. Stand up a DFE 2.2 deployment with an
object-store endpoint: a cloud bucket via the cluster module on AWS, or
on-prem an S3-compatible endpoint such as MinIO (the supported flavours are
in [storage.md](storage.md)'s object-store dimension). Run the new deployment
alongside the old one until data has moved and the cutover is proven.

Move the data by ClickHouse `BACKUP`/`RESTORE` to the object store where the
source server supports it. This repo carries no documented replay path from
dfe-archiver or any other component back into ClickHouse, so where
`BACKUP`/`RESTORE` is not available the re-ingest comes from the deployer's
own retained raw stream, and is a customer-specific plan. Once the new
deployment holds the required retention window, cut the senders over to its
receiver address, then decommission the old deployment.

### Payload format

JSON is the only payload format. MessagePack, supported in DFE/XDR 2.0 and 2.1, is deprecated in DFE 2.2 and no longer accepted: the JSON path (SIMD parsing with sonic-rs, zstd on the wire) is fast enough that MessagePack gave no CPU saving. A producer outside DFE that writes MessagePack to a DFE topic has to switch to JSON, because a MessagePack record is dead-lettered as not JSON. The loader's `payload.format` and dfe-transform-vrl's `source.format` settings are gone. Fluentd input over the Fluent Forward protocol is unaffected: the receiver still accepts it and turns it into JSON.

Three things need a customer-specific plan on top of the steps above: schema
differences between the fork's engine and upstream MergeTree, any table or
setting the fork added that upstream does not have, and a retention window
longer than the cutover overlap can cover. Expect a small performance loss
against the fork in a couple of specific use cases, and measure it during the
overlap against the deployment's own heaviest queries.

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
