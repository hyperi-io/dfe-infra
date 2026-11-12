# Upgrades

<!-- docs-gate: off process -- the rc tags are published stack tags, and these upgrade steps are keyed to them -->

Every upgrade is a committed diff in the deployment repo, applied in a declared order, with a pre-flight that names what can't be rolled back. Moving off a pre-2.2 private-cloud deployment is a separate job: [migrating-from-2.1.md](migrating-from-2.1.md).

> **Coming from Splunk or Elastic?** DFE records deployment changes in git. `dfe-ops upgrade` checks the proposed stack change and runs its upgrade hooks, and the pre-flight lists irreversible steps before apply.

## Stack upgrade

Between 2.2.x releases you deploy fresh. A 2.2.2 deployment is a fresh install, not an upgraded 2.2.1 one, because no path from one release to the next is verified. `dfe-stack check-upgrade` reads each release's `stack.previous` ([stack versioning](stack-versioning.md#overrides-and-upgrades)) and exits 0 for `SUPPORTED`:

```text
$ dfe-stack check-upgrade --from 2.2.1 --to 2.2.2
2.2.1 -> 2.2.2: NOT A VERIFIED PATH (2.2.1 is not in 2.2.2's declared stack.previous (2.2.2-rc.4))
$ dfe-stack check-upgrade --from 2.2.2-rc.4 --to 2.2.2
2.2.2-rc.4 -> 2.2.2: SUPPORTED (declared stack.previous of 2.2.2: 2.2.2-rc.4)
```

The verified edges run a release into the next release's first candidate, one candidate into the next, and the last candidate into its release. An operator moving between two releases does a fresh deploy.

`dfe-ops upgrade preflight` and `apply` run the same check and refuse an unverified path. `--allow-unverified-path` overrides the refusal, and nothing has tested the move it then runs.

On a verified path, bump `pins.yaml` in the deployment repo. One pin selects the certified stack, and `overrides` move one component with a support-drift notice. `dfe-stack resolve` emits the values and Argo CD syncs. The order is data, in `upgrade-order.yaml` at the root of this repo:

1. Operators first, with their stored-version conversions run on the running cluster BEFORE the operator that drops the old version starts.
2. The data services, Keeper before ClickHouse, then the Kafka brokers under the operator's rolling update, with `metadata.version` finalised after a soak.
3. The apps.

Moving the Argo CD ref by hand isn't a supported upgrade. Every chart and operator Application renders from the cluster secret's `dfe.hyperi.io/target_revision`, and `pins.yaml` reaches only `dfe-stack resolve`, so a pin bump alone leaves a tag-pinned deployment on its old charts. `dfe-ops upgrade apply` rewrites that annotation, with `dfe.hyperi.io/stack_version` beside it. A hand retarget across the Strimzi 0.x to 1.x lift also wedges Kafka, so `preflight` and `apply` refuse while any of the ten Strimzi CRDs stores a pre-v1 version.

## dfe-ops upgrade

`dfe-ops upgrade <verb> --deploy <dfe-deploy checkout> [--to <stack>]` walks `upgrade-order.yaml` stage by stage. Run it only along a path `check-upgrade` reports as `SUPPORTED`. `--to` defaults to the `current` pointer in `versions.yaml`, and FROM is `base.dfe-infra` in `pins.yaml`. `--help` lists five verbs:

- `plan` diffs the pinned stack against the target, runs `dfe-stack compat-check --strict`, and writes the numbered plan to `--out`, by default `.tmp/upgrades/<from>-to-<to>.md` in the dfe-infra checkout. <!-- doc-paths: ignore --> It never writes to the deploy repo and runs no path check. Exit 0 is clean, 1 is blocked, 2 means the deploy repo or stack name don't resolve.
- `preflight` is the gate `apply` won't run without.
- `apply` executes the plan stage by stage.
- `rollback` is the reverse plan.
- `migrate-values` is covered [below](#migrate-values).

`preflight` prints `PASS` or `FAIL` with the evidence for each check:

- the path is verified and the deploy repo is clean
- the cluster answers, every Argo Application is Synced and Healthy, and no KafkaRebalance is running
- ClickHouse carries no merge past `--clickhouse-merge-threshold` (default 300 seconds)
- no `schema_lock` table's quorum names a part every replica has lost
- all ten Strimzi CRDs store `v1` only, checked only when the plan crosses the conversion
- the on-prem node capacity holds the new sizing
- a backup marker exists at `--backup-marker` (default `upgrades/.backup-ok`) when the plan carries a one-way step

`apply` runs preflight, reads the cluster secret and the Kafka CRs, and refuses before anything moves if either is in a state it can't move. For each stage it then:

- bumps `pins.yaml`'s `base.dfe-infra` by a surgical field edit
- commits (`chore(upgrade): <stack> stage <n> -- <keys>`), and pushes only with `--push`
- holds until Argo converges and every Deployment and StatefulSet in the DFE namespace has rolled out, within `--timeout` (default 900 seconds)

The first commit carries the whole pin move, and `target_revision` moves once, at the first stage that moves an Argo-managed component. `bootstrap.sh` installs external-secrets, cert-manager and Argo CD before Argo exists, so their pins move nothing on the cluster. `apply` prints `[DONE]` where the chart version on the Deployment's `helm.sh/chart` label matches the pin, otherwise `[PENDING]` with the helm command to run. It never runs that command, and a run with anything pending ends `NOT complete` with exit 1.

`apply` confirms before each stage unless `--yes`, and `--stop-before <stage-key>` halts before a named stage. `--finalise` asks whether the soak is over and writes `upgrades/<from>-to-<to>.finalised`, the marker `rollback` reads. `--dry-run` touches nothing.

`rollback --to <stack>` refuses by name a step carrying `rollback: none` with no `finalise`, or a `finalise`-bearing step whose marker exists. It also refuses a Kafka rollback while a live `status.kafkaMetadataVersion` is above the target's Kafka line. It needs `--push` to move `target_revision` back.

## migrate-values

`dfe-ops upgrade migrate-values --deploy <dir>` writes the thin-chart keys beside a deploy repo's 2.2.0 value keys and commits nothing. `--check` writes nothing and exits 1 while a key is left to carry or needs a hand edit. It belongs to the 2.2.0 to 2.2.1-rc.1 step, the only listed path that crosses `since: "2.2.1-rc.1"` in `values-migrations.yaml`. A dfe-extras overlay carrying a 2.2.0 key with no new key beside it fails the render and names this command.

## Kafka version moves

When the plan moves `services.kafka-version` on a Strimzi cluster, `apply` holds Kafka back. The first stage that moves an Argo-managed component writes `kafka.metadataVersion` (the live `status.kafkaMetadataVersion`) and `kafka.version` (the FROM version) into the deploy repo's `infra/kafka.yaml`, each marked `# dfe-ops upgrade hold`. Unpinned, Strimzi raises the metadata version the moment the roll finishes, which ends the soak before it starts.

The stage that carries `services.kafka-version` drops the version hold, the brokers roll under the old metadata version, and `apply` waits for every `status.kafkaVersion` to read the new one. Then soak, 24 hours by default ([upgrade-rollback.md](upgrade-rollback.md)). A rollback during the soak takes the brokers back. `dfe-ops upgrade apply --deploy <dir> --from <stack> --finalise --push` asks whether the soak is over, drops the metadata hold and writes the finalise marker. That step is one way.

`apply` reads the brokers first. Brokers on neither the FROM nor the TO version, disagreeing Kafka CRs, or a CR with no metadata version refuse the run with nothing committed.

A version bump can fail with `Apply failed with 1 conflict: conflict with "fabric8-kubernetes-client"`, when Helm's prior apply owns a field the Strimzi operator also writes. Resolve it with `kubectl apply --server-side --force-conflicts --field-manager=helm -n <namespace> -f <rendered manifest>`, but only when the rendered value already matches the live one. Never force through a field the operator computes.

## Re-size and platform upgrade

Re-run the sizing resolver with a new estimate, focus or ratio and diff the result against `sizing/resolved.yaml`. <!-- doc-paths: ignore --> Every changed field is classified:

- LIVE-SAFE: broker count up, ClickHouse replicas up, a PVC growing, an instance type moving under a Karpenter node roll, retention, any dynamically updatable dial.
- DRAIN-FIRST: broker count down. `remove-brokers` moves data off the broker before its pod goes.
- LOCKED: the six fields in `sizing.yaml`'s `locked:` section: partition count, storage model, Kafka provider, combined against separate KRaft controllers, the cloud token and the AZ count. The resolver refuses a move in any of them without `--migrate` and the runbook it names.

On EKS the control plane goes first, then Karpenter drift rolls the nodes. On MSK the Kafka version moves in place and Express broker size moves vertically. Pre-flight checks version skew from `versions.yaml`: librdkafka against the broker, the ClickHouse client against the server, Karpenter against the Kubernetes version.

## dfe-hyperdx document store

From 2.2.1 dfe-hyperdx keeps its state in a DocumentDB cluster that the DocumentDB operator runs on CloudNativePG (`helm/charts/documentdb`), in place of FerretDB. The two keep documents in different PostgreSQL schemas, so nothing moves across in place. A deployment moved from an earlier stack starts on an empty store, without the saved searches, alerts, users and unprovisioned dashboards it held. To keep them, copy them before dfe-hyperdx cuts over, with `mongodump` against the FerretDB endpoint and `mongorestore` into the gateway, using TLS against the store's CA and SCRAM-SHA-256.

Between DocumentDB releases the upgrade is in place and two-phase. The binary moves first, with `documentdb.version` and the extension and gateway images in the chart's values, and reverting them rolls it back. The schema moves only when `spec.schemaVersion` is set, which runs `ALTER EXTENSION documentdb UPDATE` and can't be undone, and the chart sets none. Upstream's runbook: https://github.com/documentdb/documentdb-kubernetes-operator/blob/1.0.0/docs/operator-public-documentation/preview/operations/upgrades.md

The operator mounts the extension as an image volume, which needs the Kubernetes ImageVolume feature. It's on by default from 1.35 and behind a feature gate on 1.33 and 1.34. CloudNativePG 1.29 is end-of-life upstream, and 1.29.3 its last release.

## What cannot be rolled back

- A DocumentDB schema update (`spec.schemaVersion`) is one way.
- Kafka's `metadata.version` bump is one way, so the roll and the finalise are separate steps with a soak between them.
- ClickHouse downgrades only within the same LTS line.
- `apply --finalise` across 2.2.1-rc.1 drops the 2.2.0 value keys, after which no stack before 2.2.1-rc.1 renders the deploy repo until that commit is reverted.
- Kafka data is a buffer. Before upgrading, check that topic retention covers the upgrade window and the time consumers need to catch up.

Pre-flight prints the one-way steps before asking to continue. [upgrade-rollback.md](upgrade-rollback.md) has the full per-stage runbook and the sizing config-vs-data rule for a locked field.
