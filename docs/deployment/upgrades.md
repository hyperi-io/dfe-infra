# Upgrades

Every upgrade is a committed diff in the deployment repo, applied in a
declared order, with a pre-flight that names what cannot be rolled back.
Four kinds of upgrade share that rule.

## Stack upgrade

Bump `pins.yaml` in the deployment repo. One pin selects the certified stack,
and `overrides` move one component with a support-drift notice. `channel`
records the maturity the deployment tracks, and nothing reads it.
`dfe-stack resolve` emits the values and Argo CD syncs.

The order is data, `upgrade-order.yaml` at the root of this repo: operators
first, with their stored-version conversions run on the running cluster
BEFORE the operator that drops the old version starts; then the data
services, Keeper before ClickHouse, the Kafka brokers under the operator's
rolling update with `metadata.version` finalised after a soak; then the apps.
`dfe-ops upgrade` (below) walks that file so the order is never applied by
hand; see it for a deployment-repo driven upgrade.

**`dfe-ops upgrade apply` moves the Argo CD ref; moving it by hand is not a supported upgrade.** Every chart and operator Application renders from the cluster secret's `dfe.hyperi.io/target_revision`, and `pins.yaml` reaches only `dfe-stack resolve`, so bumping the pin alone leaves a tag-pinned deployment on its old charts. `apply` rewrites that annotation, with `dfe.hyperi.io/stack_version` beside it, at the first stage that moves an Argo-managed component: after that stage's `before:` checks pass and its commit is pushed. A hand edit of the annotation runs none of that.

Across the Strimzi 0.x to 1.x lift a hand retarget wedges Kafka. The 1.x CRDs fail Argo CD's server-side diff against CRDs still storing `v1beta2`, so the operator stays on 0.x while its Application reports Synced over a ComparisonError. The kafka chart applies cleanly and asks for a Kafka version the running operator rejects, and the Kafka CR goes NotReady and stops being reconciled. `dfe-ops upgrade preflight` and `apply` both refuse while any of the ten Strimzi CRDs stores a pre-v1 version, and print the conversion commands to run first.

## In place from Strimzi 0.51

2.2.0-rc.13 runs Strimzi 0.51.0 with Kafka 4.2.0, and 2.2.0-rc.14 runs Strimzi 1.2.0 with Kafka 4.3.1. 0.51 cannot run 4.3.1, and 1.x reads only resources stored as `v1`. `dfe-ops upgrade apply --push` runs steps 2 to 4 and checks step 1.

1. **Convert**, on the running cluster, with the tool from the RUNNING release (`strimzi-v1-api-conversion-0.51.0.tar.gz`, never the target's): `bin/v1-api-conversion.sh convert-resource --all-namespaces`, then `bin/v1-api-conversion.sh crd-upgrade`. All ten Strimzi CRDs then store `v1` only, including `kafkarebalances.kafka.strimzi.io` and `strimzipodsets.core.strimzi.io`, which a 0.51 cluster with rebalancing on (the default) stores as `v1beta2`. `crd-upgrade` is one way and needs a JVM and CRD patch rights, so `apply` checks the result and never runs the tool.
2. **Hold the metadata version.** `apply` writes `kafka.metadataVersion: "4.2-IV1"` (the live `status.kafkaMetadataVersion`) and `kafka.version: "4.2.0"` into the deploy repo's `infra/kafka.yaml`, each marked `# dfe-ops upgrade hold`. Unpinned, Strimzi raises the metadata version the moment the version roll finishes, which ends the soak before it starts.
3. **Operator 1.2.0, Kafka still on 4.2.0.** With the holds pushed, `apply` moves `target_revision` to `2.2.0-rc.14`, waits until no Application renders from `2.2.0-rc.13`, then waits for every Kafka CR's `status.operatorLastSuccessfulVersion` to read `1.2.0`.
4. **Kafka 4.3.1.** The `30-services` stage drops the version hold, the brokers roll under metadata 4.2-IV1, and `apply` waits for every `status.kafkaVersion` to read `4.3.1`.
5. **Soak**, 24 hours by default ([upgrade-rollback.md](upgrade-rollback.md)). A rollback here takes the brokers back to 4.2.0, because the metadata version has not moved.
6. **Finalise to 4.3-IV0.** `dfe-ops upgrade apply --deploy <dir> --from 2.2.0-rc.13 --finalise --push` asks whether the soak is over, drops the metadata hold, writes the finalise marker and waits for the 4.3 line. One way: Kafka 4.2.0 cannot run metadata 4.3-IV0.

`apply` reads the brokers before it moves anything. Brokers on neither 4.2.0 nor 4.3.1, Kafka CRs that disagree, or a CR with no metadata version refuse the run with nothing committed.

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

The bundled in-cluster deploy repo is seeded without a `pins.yaml`. Against a deploy repo with none, `plan`, `preflight` and `apply` take FROM from the cluster secret's `dfe.hyperi.io/stack_version`, which bootstrap writes and every retarget moves, and say so. `plan` takes `--kubeconfig` for that read. `apply` writes a `pins.yaml` in the dfe-deploy template's shape at its first stage, and from that commit on `pins.yaml` is what FROM is read from. A deploy repo with neither refuses, naming both.

`preflight` and `apply` find ClickHouse by `--clickhouse-selector`, which defaults to `app.kubernetes.io/name in (dfe-clickhouse,clickhouse-server)`: the single-mode StatefulSet's pods and the ClickHouse operator's server pods, never its Keeper pods. The merge check asks every pod it matches, because `system.merges` is per server, and refuses when any one carries a long merge or does not answer. It logs in with the `password` key of the `--clickhouse-credentials` Secret (default `clickhouse-admin-password`) as `default`, then `admin`, then as `default` with no password, and never prints the password.

- `plan` diffs FROM -> TO, grouped by stage, each step carrying its
  `before`/`finalise`/`pair`/`rollback` note. A step moves when its pin moves, or the image or thin-chart digest its `image:`/`chart:` names does; `dfe-hyperdx` is the step whose pin is `content.dfe-hyperdx`. Runs `dfe-stack compat-check
  --strict` for TO and writes the numbered plan to `--out`, by default
  `.tmp/upgrades/<from>-to-<to>.md` in the dfe-infra checkout. Never the deploy
  repo: `preflight` refuses a deploy tree with an untracked file in it. Pass `--dial <deployment.yaml>` (with
  `--fixtures` or `--live`) to also run `resolve_sizing.py`'s locked-change
  classifier against the deploy's committed `sizing/resolved.yaml` -- a
  LOCKED field moving without `--migrate` blocks the plan. Exit 0 clean, 1
  blocked (compat-check failed, or an unmigrated locked change), 2 the
  deploy repo or stack name do not resolve.
- `preflight` is the gate `apply` will not run without: the deploy repo is clean, the cluster answers, every Argo Application is Synced and Healthy, no KafkaRebalance is running, ClickHouse carries no merge past `--clickhouse-merge-threshold`, all ten Strimzi CRDs store `v1` only (checked only when the plan crosses the conversion; a CRD kubectl cannot read fails), the on-prem node capacity holds the new sizing (`check_node_capacity.py`), and a backup marker exists at `--backup-marker` when the plan carries a one-way step. Each check prints `PASS`/`FAIL` with the evidence line that decided it.
- `apply` runs preflight, then reads the cluster secret and the Kafka CRs and refuses before anything moves if either is in a state it cannot move. It then walks the plan stage by stage: bumps `pins.yaml`'s `base.dfe-infra` via a surgical field edit, re-runs the resolver with `--migrate` when `--dial` is given and copies its `sizing/` output over `<deploy>/sizing/`, commits the stage (`chore(upgrade): <stack> stage <n> -- <keys>`), pushes only with `--push`, and waits for Argo and then for every Deployment and StatefulSet in the DFE namespace to finish rolling out, bounded by `--timeout`. A rollout is finished on the conditions `kubectl rollout status` waits on, because an Application can read Healthy while its Deployment still rolls; a timeout names each rollout still running. The namespace is `--namespace`, else the cluster secret's `dfe.hyperi.io/dfe_namespace`, and a secret naming none refuses the run before anything moves. The first stage moving an Argo-managed component writes the Kafka holds and moves `target_revision`, as [In place from Strimzi 0.51](#in-place-from-strimzi-051) describes; a secret tracking a branch is left alone, one pinned to a commit needs `--target-revision <ref>`, and a retarget refuses without `--push`. After a Strimzi operator bump apply waits on every Kafka CR's `status.operatorLastSuccessfulVersion`, because the CR's own `Ready` condition stays True and stale across the lift. A `before` note with no program needs a confirmed "I ran this by hand". Confirms before each stage unless `--yes`; `--stop-before <stage-key>` halts before a named stage. A reached `finalise` note stays pending unless `--finalise` is given, which asks whether the soak is over, runs the step's finalise program and writes `upgrades/<from>-to-<to>.finalised`, the marker `rollback` reads. `--from <stack>` names the FROM stack once `pins.yaml` already names the target, for a finalise after the soak or a resumed run. `--dry-run` prints every command and touches nothing.
- `rollback --to <stack>` is the reverse plan. It refuses by name a step carrying `rollback: none` with no `finalise`, or a `finalise`-bearing step whose marker exists; with no marker yet the step reverses like any other. A rollback that moves `services.kafka-version` also reads every live Kafka CR and refuses when `status.kafkaMetadataVersion` is above the target's Kafka line, marker or not, which is what an unpinned metadata version leaves behind. It moves `target_revision` back the same way `apply` moves it forward, and needs `--push` to. `--skip-cluster-check` moves the pin alone, reading nothing.

### What a stage moves

One pin, `base.dfe-infra`, selects the whole certified stack, and `apply` sets it to the target at every stage, so the first stage's commit carries the whole pin move. One ref, `target_revision`, selects every chart, and `apply` moves it once, at the first stage that moves an Argo-managed component; every chart and operator converges on the target during that stage's wait. Later stages skip their commit unless they add something, such as dropping a Kafka hold or writing a finalise marker.

The work around the pin still runs in stage order: confirms, `before` checks, `finalise` notes and waits. The Kafka version hold is the one place a component waits for its own stage. `--stop-before` skips a stage's hooks and waits, not its component versions. Staging the pin itself is open in https://github.com/hyperi-io/dfe-infra/issues/508.

The `10-bootstrap` stage is the exception: `bootstrap.sh` installs external-secrets, cert-manager and Argo CD before Argo exists, and Argo never renders them, so their pins move nothing on the cluster. After that stage `apply` reads the chart version each release runs off its Deployment's `helm.sh/chart` label. It prints `[DONE]` where that matches the pin, and otherwise `[PENDING]` with the command that moves the release, for example:

```
helm -n cert-manager upgrade cert-manager cert-manager --repo https://charts.jetstack.io --version v1.21.2 --reset-values --set crds.enabled=true --set config.enableGatewayAPI=true --wait --timeout 5m
```

`--reset-values` starts from the new chart's defaults, and every value after it is one `bootstrap.sh`'s install sets: both read them from `bootstrap/helm_releases.py`, so the upgraded release carries what a fresh bootstrap installs. Argo CD's command also pipes in the login values `bootstrap/argocd_login.py` computes, as `bootstrap.sh` does, and fills in the domain from the cluster secret's `dfe.hyperi.io/domain`. `apply` never runs it. The walk carries on, and a run with anything still pending ends `NOT complete` with exit 1. Run the printed commands, then re-run `apply` with `--from <stack>` to confirm.

After the walk `apply` reads every other bootstrap pin of the target stack the same way, marked `(unchanged by this upgrade)`. A release an earlier upgrade left on its old chart therefore keeps a later upgrade from reporting OK, even when that later plan moves no bootstrap pin.

An Argo CD that `bootstrap/argocd_release.py` does not recognise as `bootstrap.sh`'s own reads `[ADOPTED]` and is left to its owner, the same call `bootstrap.sh` makes before it upgrades Argo. Nothing tells a cert-manager or external-secrets `bootstrap.sh` installed from one it adopted, so their command says to run it only where this deploy installed them. A `bootstrap.sh` re-run skips a running install of either unless `DFE_FORCE_INSTALL=true`.

### Onto the thin charts

A move to a stack carrying `chart-digests:`, from one that does not, puts the apps on thin charts, and `apply` adds a stage either side of the first one that moves an Argo-managed component. `overlay-vocabulary`, before it, copies each set value in `values/*-values.yaml` to the key `scripts/weave/value-map.yaml` names and keeps the 2.2.0 key, so the 2.2.0 charts render as before. `podSecurityContext.seccompProfileType` alone moves out, because the thin chart would copy it into the pod spec. The stage never overwrites a key already set, prints what it cannot carry for a hand edit, as `plan` does, and commits nothing on a second run. `--stop-before` that first stage leaves the rewrite committed and nothing else moved. `enrichment-tables`, after it, names each table file in its app's config under the mount `apps.yaml` declares. A transform that reads a table fails to compile on the thin chart until that entry exists, so the wait after the retarget asks only that every Application has synced the new ref, and the `enrichment-tables` wait asks for health.

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

- Kafka's `metadata.version` bump is one way, so the roll and the finalise are separate steps with a soak between them, and `apply` holds the metadata version so Strimzi cannot take that step on its own.
- ClickHouse downgrades only within the same LTS line.
- Kafka data is a buffer: an upgrade window shorter than the topic retention
  loses nothing. ClickHouse on `cached-object` keeps its parts in the object
  store.

Pre-flight snapshots the Argo application versions, `sizing/resolved.yaml`
and the tofu state, and prints the one-way steps before asking to continue.

`dfe-ops upgrade rollback --to <stack>` (above) refuses by name a step whose finalise has already run, read from the `upgrades/<from>-to-<to>.finalised` marker `apply --finalise` writes, and refuses a Kafka rollback whenever the live metadata version is above the target's line. Between the Kafka broker roll and its `metadata.version` finalise, soak the cluster under normal ingest for 24 hours by default, watching consumer lag, under-replicated partitions, ClickHouse's merge backlog and insert errors, and confirming KEDA and Cruise Control both stay quiet. A problem during the soak ends it early: `dfe-ops upgrade rollback --to <stack> --push` reverses the pin and `target_revision` like any other step, because the metadata version is still held. The full per-stage runbook, the sizing config-vs-data rule for a locked field, and what `apply --finalise`/`--stop-before` do at finalise time are in [upgrade-rollback.md](upgrade-rollback.md).
