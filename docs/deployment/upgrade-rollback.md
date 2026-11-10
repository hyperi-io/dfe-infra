# Rollback runbook

Most of an upgrade rolls back by moving the pin, and a few steps are one way ([upgrades.md](upgrades.md), "What cannot be rolled back"). Check the stage notes below before moving a pin backward.

## Per-stage rollback

`10-bootstrap` (external-secrets, cert-manager, argocd) and `40-apps` (dfe-engine, dfe-receiver, dfe-transform-vrl, dfe-transform-vector, dfe-loader, dfe-archiver, dfe-fetcher, dfe-ui) carry no `before`, `finalise` or `rollback` note. Rolling one back is the same move as rolling it forward. `dfe-ops upgrade rollback --to <stack>` bumps `pins.yaml` to the earlier stack and Argo CD reconciles the chart down. No data changes hands.

`20-operators`: envoy-gateway, external-dns and keda roll back the same way. strimzi-kafka-operator carries a `before` note. That's the CRD stored-version conversion (`bin/v1-api-conversion.sh convert-resource --all-namespaces`, then `bin/v1-api-conversion.sh crd-upgrade`), and it has to run on the live cluster before the 1.x operator starts. The tool comes from the tarball the RUNNING operator's release publishes (`strimzi-v1-api-conversion-<from>.tar.gz`), never the target's, because it rewrites the CRs the running operator wrote.

Once the operator itself has moved, the Kafka CR's `Ready` condition stays True and stale. A `kubectl wait --for=condition=Ready` returns at once and proves nothing. The signal that the new operator has reconciled the cluster is `status.operatorLastSuccessfulVersion` reaching the new version, and that's what apply waits on.

`upgrade-order.yaml` doesn't mark the operator step `rollback: none`, so `dfe-ops upgrade rollback` won't refuse a plan that moves this pin backward. The conversion is one way once it has run all the same. All ten Strimzi CRDs (`kafkas`, `kafkanodepools`, `kafkatopics`, `kafkausers`, `kafkaconnects`, `kafkaconnectors`, `kafkabridges`, `kafkamirrormaker2s` and `kafkarebalances` in `kafka.strimzi.io`, and `strimzipodsets.core.strimzi.io`) then store only `v1`, and there's no conversion back.

So treat it as one way by policy even though the tool doesn't stop you. Check `kubectl get crd <name> -o json` for `.status.storedVersions`, the field `check_strimzi_conversion` in `scripts/dfe_ops_upgrade.py` reads. Never roll the operator pin back once it lists only `v1`. redpanda-operator carries `pair: services.redpanda-version`: roll both back together, since the operator and the broker pin move as one change.

`30-services`: clickhouse-keeper (scope `keeper`) and otel-collector carry no notes and roll back like any other pin.

clickhouse-server (scope `server`) carries `rollback: "within the same LTS line only"`. That's advisory text the tool doesn't check, so `dfe-ops upgrade rollback` will move the pin across an LTS boundary without complaint. ClickHouse ties its on-disk MergeTree part format and defaults to its version and `compatibility` setting. A newer server can write parts in a format an older LTS line can't open, so a server rolled back across LTS lines can find data it can't read. Stay within the same LTS line, and don't roll the server pin back without the restore point below.

kafka-brokers carries a `finalise` note (the metadata version bump) and `rollback: "none"`. It's the one step `dfe-ops upgrade rollback` refuses, on a marker and on the live cluster. Both refusals are below. redpanda-brokers pairs with `operators.redpanda-operator`, so roll both together, same as above.

## The rollback command

    dfe-ops upgrade rollback --deploy <dfe-deploy checkout> --to <stack> \
        [--kubeconfig <path>] [--argocd-namespace <ns>] [--push] [--dry-run] \
        [--skip-cluster-check] [--target-revision <ref>]

`--to` is required. `plan`, `preflight` and `apply` default to `versions.yaml`'s `current` pointer, but a rollback always names its target.

The command diffs the deploy's currently pinned stack against `--to` using the same `upgrade-order.yaml` steps as a forward plan, then checks every step the diff touches. Nothing is edited before that check runs.

- A step carrying `rollback: none` with no `finalise` note is refused unconditionally. It has no soak to wait out.
- A step carrying a `finalise` note is refused only when `upgrades/<from>-to-<to>.finalised` already records that key. That's the marker `dfe-ops upgrade apply --finalise` writes once the operator confirms the soak is over.
- With no marker, the step reverses like any other. The pin moves back, and rollback prints a note that the soak can be abandoned safely.

A refusal stops the whole command, not just the offending step. It names every blocked step and returns exit code 1.

With today's `upgrade-order.yaml`, the only step this can ever block on is kafka-brokers (`services.kafka-version`). A rollback that moves it also reads every live Kafka CR, in every namespace, and refuses when `status.kafkaMetadataVersion` is above the rollback target's Kafka line. `4.3-IV0` refuses a rollback to Kafka 4.2.0, and `4.2-IV1` doesn't. That refusal needs no marker, because the metadata version moves without one whenever it isn't held: Strimzi raises an unset `metadataVersion` the moment a version roll finishes. A cluster kubectl can't read, or a Kafka CR with no metadata version, also refuses. No Strimzi broker (single mode, Redpanda, a managed Kafka) passes.

The rollback then reads the cluster secret's `dfe.hyperi.io/target_revision`. When it names the deploy's current stack, the rollback moves it to the rollback target's tag after the rollback commit is pushed, so the charts follow the pin. That needs `--push`. `--skip-cluster-check` reads neither the Kafka CRs nor the secret and moves the pin alone. It doesn't make a downgrade below the live metadata version possible.

`--dry-run` prints the plan and the commands it would run, the retarget included, without writing or committing anything. Without it, a clean rollback bumps `pins.yaml`, commits `chore(upgrade): rollback to <stack> -- <keys>` in the deploy repo, and pushes only with `--push`. Any `# dfe-ops upgrade hold` lines in `infra/kafka.yaml` stay. A metadata hold at the old line is what makes the rollback safe, and the next `apply` reuses it.

That commit moves the one `base.dfe-infra` pin, so every stage reverses at once. The per-stage notes above say what each stage's components need around it.

## Backup marker and restore points

`dfe-ops upgrade preflight` (which `apply` runs first) checks a backup marker file at `--backup-marker` (default `upgrades/.backup-ok` in the deploy repo) whenever the plan carries a one-way step (`check_backup_marker` in `scripts/dfe_ops_upgrade.py`). The marker only proves a backup was taken. Nothing in this repo takes it. Before creating it:

- Keeper and ClickHouse share the `services.clickhouse-version` pin, so take a Keeper snapshot and a ClickHouse backup before either scope moves. Neither is scripted here.
- The deploy repo commit the first stage makes (`chore(upgrade): <stack> stage <n> -- <keys>`) is the config-side restore point. It carries the whole pin move (see [upgrades.md](upgrades.md), "What a stage moves"), so `git revert` of it, or a hand edit of `pins.yaml`, returns every component to the stack pinned before the upgrade.
- The committed `sizing/resolved.yaml`, as it stood before a `--dial` <!-- doc-paths: ignore -->
  re-resolve, is the sizing-side restore point `--previous` diffs the next resolve against, whichever direction a field moves.

## Config vs data -- rolling back a sizing move

A sizing move that touched a LOCKED field needed `--migrate` to go forward. sizing.yaml's `locked:` section names six fields, each with a reason: `partition_count`, `storage_model`, `kafka_provider`, `controller_mode`, `cloud_token`, `az_count`.

In the deployment repo, `governance/policies/sizing-locks.yaml` protects the chart keys behind three of them (`kafka.sizing.*`, `kafka.controllerPool.enabled`, `cloud`). `storage-layout.yaml` protects `storage_model`'s (`clickhouse.storageModel`, `kafka.storageModel`). `kafka_provider` and `az_count` are OpenTofu inputs with no chart key.

Reversing one is the same operation run backward. Re-resolve with the previous dial against the committed `sizing/resolved.yaml` as `--previous`, and `resolve_sizing.py` <!-- doc-paths: ignore -->
reports the field moving the other way as a new locked change, refused with exit 3 unless `--migrate` is passed again.

That command only rewrites config: `sizing/resolved.yaml`, the chart <!-- doc-paths: ignore -->
values overlay, the tofu tfvars. It writes no infrastructure and moves no data. Whether the underlying resource can follow the config back down depends on the field:

- `partition_count` can't go back down at all. sizing.yaml's own reason is that a keyed topic hashes on partition count, so the count "only ever grows."
- `storage_model`, `kafka_provider`, `controller_mode` and `cloud_token` each need the migration sizing.yaml's reason names: a data migration, a cluster replacement, a quorum re-formation, a new deployment. You carry it out by hand before the config reversal means anything.
- `az_count` needs every replica and volume re-homed.

Re-resolving is the first step of a locked-field rollback, never the whole of it.

## Soak before finalise

Between the Kafka broker roll (the `services.kafka-version` pin move) and its `finalise` (the `metadata.version` bump), run the cluster at the new version under normal ingest for 24 hours by default. Watch:

- consumer lag against `kafka.autoscaling.lagThreshold` (default 10) on every `<source>_land` topic
- under-replicated partitions
- ClickHouse's merge backlog, using the same `system.merges` query `dfe-ops upgrade preflight`'s ClickHouse check already runs (`elapsed > 300s` by default)
- insert errors: `FailedInsertQuery` against `InsertedRows` in the otel-collector's ClickHouse metrics
- KEDA quiet (no unexpected scale events) and Cruise Control quiet (no `KafkaRebalance` in progress, the same check `dfe-ops upgrade preflight` already runs)

A problem during the soak ends it early. Roll back. The metadata version is still held at the old line in `infra/kafka.yaml`, and the finalise marker is only written once `apply --finalise` runs. So `dfe-ops upgrade rollback --deploy <deploy> --to <stack before the roll> --push` reverses the `services.kafka-version` pin and `target_revision` like any other step, and Argo CD reconciles the brokers down. Its live metadata check confirms nothing moved the metadata version by another path.

## Finalising after the soak

By the time the soak is over `pins.yaml` already names the target, so the finalise run names where the upgrade started:

    dfe-ops upgrade apply --deploy <deploy> --from <stack before the upgrade> --finalise --push

The walk re-runs every stage's checks, finds the pin, the retarget and the broker roll already done, and reaches the finalise-bearing move. There apply asks `soak complete -- run finalise now: <note>` (`[y/N]`, or auto-yes under `--yes`).

On yes it runs the step's finalise program. For `services.kafka-version`, that drops the `kafka.metadataVersion` hold from `infra/kafka.yaml`, so Strimzi raises the metadata version to the new Kafka version's default once Argo syncs. It then records the key and an ISO-8601 timestamp in `upgrades/<from>-to-<to>.finalised`, commits and pushes both, and waits for every Kafka CR's `status.kafkaMetadataVersion` to reach the new line.

On no, or when `infra/kafka.yaml` carries a `kafka.metadataVersion` the deployer set by hand, it prints `[PENDING]`, writes no marker, and the run isn't aborted. A finalise note with no program falls back to the operator's confirmation alone. Without `--finalise`, apply prints `finalise pending (manual, after a soak): <key>: <note>` and moves on.

`--stop-before <stage-key>` stops the walk before a named `upgrade-order.yaml` stage (e.g. `30-services`). It skips that stage's hooks, finalise notes and waits and everything after. It doesn't hold back that stage's component versions, which the first stage already moved with the whole pin.

The per-stage confirm still applies to the stages that do run, unless `--yes`. `--dry-run` prints every command a stage would run, including a reached finalise or a `--stop-before` halt, without touching anything.
