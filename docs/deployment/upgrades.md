# Upgrades

<!-- docs-gate: off process -- the rc tags are published stack tags, and these upgrade steps are keyed to them -->

Every upgrade is a committed diff in the deployment repo, applied in a declared order, with a pre-flight that names what can't be rolled back. Four kinds of upgrade share that rule.

> **Coming from Splunk or Elastic?** DFE records deployment changes in git. `dfe-ops upgrade` checks the proposed stack change and runs its upgrade hooks, and the pre-flight lists irreversible steps before apply.

2.2.x is fresh deploys only. There's no verified upgrade path between 2.2.x releases yet, so a 2.2.2 deployment is a fresh install, not an upgraded 2.2.1 one. `dfe-stack check-upgrade --from 2.2.1 --to 2.2.2` prints `NOT A VERIFIED PATH`, because 2.2.2's `stack.previous` names only `2.2.2-rc.4` ([stack versioning](stack-versioning.md#overrides-and-upgrades)). Within a release, `dfe-ops upgrade` moves a deployment from one release candidate to the next, and from the last candidate to the release.

## Stack upgrade

Bump `pins.yaml` in the deployment repo. One pin selects the certified stack, and `overrides` move one component with a support-drift notice. `channel` records the maturity the deployment tracks, and nothing reads it. `dfe-stack resolve` emits the values and Argo CD syncs.

The order is data, in `upgrade-order.yaml` at the root of this repo:

1. Operators first, with their stored-version conversions run on the running cluster BEFORE the operator that drops the old version starts.
2. The data services, Keeper before ClickHouse, then the Kafka brokers under the operator's rolling update, with `metadata.version` finalised after a soak.
3. The apps.

`dfe-ops upgrade` (below) walks that file.

`dfe-ops upgrade apply` moves the Argo CD ref, and moving it by hand isn't a supported upgrade. Every chart and operator Application renders from the cluster secret's `dfe.hyperi.io/target_revision`, and `pins.yaml` reaches only `dfe-stack resolve`. So bumping the pin alone leaves a tag-pinned deployment on its old charts. `apply` rewrites that annotation, with `dfe.hyperi.io/stack_version` beside it, at the first stage that moves an Argo-managed component: after that stage's `before:` checks pass and its commit is pushed. A hand edit of the annotation runs none of that.

Across the Strimzi 0.x to 1.x lift a hand retarget wedges Kafka. The 1.x CRDs fail Argo CD's server-side diff against CRDs still storing `v1beta2`, so the operator stays on 0.x while its Application reports Synced over a ComparisonError. The kafka chart applies cleanly and asks for a Kafka version the running operator rejects, and the Kafka CR goes NotReady and stops being reconciled. `dfe-ops upgrade preflight` and `apply` both refuse while any of the ten Strimzi CRDs stores a pre-v1 version, and print the conversion commands to run first.

## Kafka version moves

When the plan moves `services.kafka-version` on a Strimzi cluster, `apply` holds Kafka back. The first stage that moves an Argo-managed component writes `kafka.metadataVersion` (the live `status.kafkaMetadataVersion`) and `kafka.version` (the FROM version) into the deploy repo's `infra/kafka.yaml`, each marked `# dfe-ops upgrade hold`, so the operator moves before the brokers do. Unpinned, Strimzi raises the metadata version the moment the version roll finishes, which ends the soak before it starts.

The stage that carries `services.kafka-version` drops the version hold, the brokers roll under the old metadata version, and `apply` waits for every `status.kafkaVersion` to read the new one. Then soak, 24 hours by default ([upgrade-rollback.md](upgrade-rollback.md)). A rollback during the soak takes the brokers back, because the metadata version hasn't moved. `dfe-ops upgrade apply --deploy <dir> --from <stack> --finalise --push` asks whether the soak is over, drops the metadata hold and writes the finalise marker. That step is one way.

`apply` reads the brokers before it moves anything. Brokers on neither the FROM nor the TO version, Kafka CRs that disagree, or a CR with no metadata version refuse the run with nothing committed.

A Kafka version bump can hit a server-side-apply field-ownership conflict. The Strimzi operator's own client also writes fields like `KafkaNodePool` `.spec.storage.volumes` or `KafkaUser` `.spec.authorization.acls`. If Helm's prior apply already owns them, the upgrade fails with `Apply failed with 1 conflict: conflict with "fabric8-kubernetes-client"`. Resolve it with `kubectl apply --server-side --force-conflicts --field-manager=helm -n <namespace> -f <rendered manifest>`. Use `helm` because that's Helm's own default field manager (the base name it runs under), so reusing it hands ownership back cleanly and the next plain `helm upgrade` no longer conflicts.

That's safe only when the rendered value already matches the live one, so the operator is re-asserting a default. Diff the object first, and never force through a field the operator computes, such as a broker-assigned identifier.

## From 2.2.0

### Ingest door

Below 2.2.1-rc.7 the receiver's door is `exposure.mode`. From 2.2.1-rc.7 it is the ingest exposure group, `exposure.groups.ingest` ([ingest-edge.md](../ingest-edge.md)). A deploy repo that sets `exposure.mode` or `exposure.public.loadBalancerSourceRanges` fails the render, which names the replacement. One that sets neither renders cleanly and takes the new cloud default, which closes or moves the door:

| Cloud | Door below 2.2.1-rc.7 | Default door from 2.2.1-rc.7 |
|---|---|---|
| `local-dfe`, or any whose values set no `exposure.mode` | `public`: a LoadBalancer open to every address on the receiver's public ports | `gateway`: HTTP `/ingest` on `receiver.{domain}`, nothing else |
| `aws`, `gcp`, `azure` | `vpn`: ClusterIP, reached through the tunnel | `loadbalancer` with an empty `allow`: no load balancer, and no NetworkPolicy admitting the tunnel |
| `local` | `internal`: the gateway route | `gateway`: the same route |

`dfe-ops upgrade plan` reports an upgrade from below 2.2.1-rc.7 to 2.2.1-rc.7 or later as BLOCKED. `preflight`, and so `apply`, refuses it until the deploy repo's `infra/common.yaml` sets `exposure.groups.ingest`. Any explicit block passes, `allow: []` included, so a closed door is a decision rather than a default.

To keep an L4 door, set `door: loadbalancer` and name the senders:

```yaml
exposure:
  groups:
    ingest:
      door: loadbalancer
      allow:
        - 203.0.113.0/24
```

`allow` is each load balancer's `loadBalancerSourceRanges`. A `/0` admits every address of its family, as the 2.2.0 default did, and renders with the Service marked `dfe.hyperi.io/ingest-fence: allow-all`. The allow list isn't authentication, so set the receiver's `server.auth.mode` as well. To keep the address senders already post to, pin it as `exposure.public.loadBalancerIP`, or as the cluster secret's `dfe.hyperi.io/receiver_address`, before the upgrade.

To keep the tunnel, set `door: tunnel`.

On-prem, `door: gateway` carries HTTP `/ingest` on `receiver.{domain}` and nothing else. A syslog, lumberjack, OTLP gRPC or other non-HTTP listener marked `exposed` fails the render on this door. Senders on those protocols need `door: loadbalancer`, or a dedicated Gateway listener with its own TCPRoute or UDPRoute.

On `local-dfe` and `rancher`, 2.2.0 served two doors at once: the LoadBalancer, and HTTP `/ingest` on the gateway at `receiver.{domain}`. From 2.2.1-rc.7 there's one. `door: loadbalancer` drops the `receiver.{domain}` route, so a sender posting there repoints to the load balancer's address. `door: gateway` drops the load balancer, so its senders repoint to `receiver.{domain}`. The 2.2.0 LoadBalancer also listed port 8443, where the receiver never listened, so nothing goes with it.

### Web plane

Below 2.2.1-rc.6 the gateway fenced no web route by address. From 2.2.1-rc.6 an internet-facing gateway answers 403 on every web route whose allow list is empty. That's `ui.allowed_cidrs` at 2.2.1-rc.6, and each group's `exposure.groups.<group>.allow` from 2.2.1-rc.7 ([edge-auth-cloud.md](../edge-auth-cloud.md#internet-facing-defaults)). `argocd/values/edge-<cloud>.yaml` makes the gateway internet-facing on `aws`, `gcp` and `azure`. So a deploy repo there that set nothing loses dfe-ui, dfe-hyperdx and the engine API (the `console` and `api` groups) from the first sync. On-prem gateways aren't internet-facing, and an empty list fences nothing.

`plan` reports BLOCKED, and `preflight` and `apply` refuse, until `infra/common.yaml` or `infra/envoy-gateway-config.yaml` sets `enabled`, `allow` or `allowPublic` on both groups, `allow: []` included. For a move to 2.2.1-rc.6 itself, set `ui.allowed_cidrs` instead. The cloud is `--cloud`, else the cluster secret's `dfe.hyperi.io/cloud`.

```yaml
exposure:
  groups:
    console:
      allow:
        - 203.0.113.0/24
    api:
      allow:
        - 203.0.113.0/24
```

A `/0` admits every address, as 2.2.0 did, and the chart's NOTES warn.

On `gcp` and `azure`, 2.2.0 also published the admin UIs with a login of their own: Argo CD, Kafbat, dfe-hyperdx, and Forgejo where bundled. From 2.2.1-rc.6 the admin group is off there, as it already was on `aws`, and nothing refuses. dfe-hyperdx moves to `console`. The rest stay off until `exposure.groups.admin.enabled: true` with an `allow` list, or `allowPublic: true` behind an edge OIDC provider.

### Values

The 2.2.0 charts named each object `{project}-{component}` and read their own value keys. From 2.2.1-rc.1 the thin charts name it `fullnameOverride`, else the chart's own name, and read other keys. Rendered as 2.2.0 left it, a deploy repo renames every per-config workload and prunes the fetcher's cursor claim and its volume. It also moves the transforms to new Kafka consumer groups that replay from the oldest record, and empties their transform and table files.

`helm/charts/dfe-extras/files/values-migrations.yaml` maps each 2.2.0 key to the key the thin charts read. `dfe-ops upgrade apply` writes each new key beside its old one in `values/*-values.yaml` and `infra/common.yaml`, removing nothing, in the commit of the first stage that moves an Argo-managed component. The 2.2.0 charts read none of the new keys, so they render the same objects. The one exception is dfe-transform-vrl. Its table mount moves to `/etc/dfe-transform-vrl-enrichment`, the directory the thin chart mounts, with an `enrichment_tables` entry per table, so each vrl pod rolls once before the retarget.

Until the retarget the 2.2.0 engine writes only the old keys. So just before it, `apply` pulls the deploy repo, writes the new keys again from the old ones, and pushes. Don't edit sources or transforms while `dfe-ops upgrade apply` runs. An edit landing between that second write and the retarget reaches the old keys only.

`apply --finalise` drops the old keys once the soak is over, and only once `target_revision` names the new stack. Until then a rollback is a retarget, provided nothing was edited while `apply` ran. After it, `dfe-ops upgrade rollback` to a stack before 2.2.1-rc.1 refuses until the deploy repo's `drop the 2.2.0 value keys` commit is reverted.

`plan` lists every key it finds in a values migration section:

- `MIGRATE`, a key `apply` carries.
- `REFUSED`, which blocks `plan`, `preflight` and `apply` until a person edits it. That's an `image.repository`, `image.tag` or `image.digest` in an overlay, which would keep the 2.2.0 image under the new chart (an `image.tag` and `image.digest` equal to the service's `overrides.apps` entry in `pins.yaml` are that override, and pass), or an `enrichment_tables` path under another instance's table mount.
- `DROPPED`, a key the thin charts read nothing in place of. dfe-hyperdx's `defaultConnections` is config loss. The thin chart no longer hands dfe-hyperdx `DEFAULT_CONNECTIONS` from `defaultConnections.secretName`, so recreate those connections in dfe-hyperdx.

`dfe-ops upgrade migrate-values --deploy <dir>` writes the same keys on its own and commits nothing. `--check` writes nothing and exits 1 while a key is left to carry or needs an edit.

dfe-extras fails the render of an overlay that carries a 2.2.0 key with no new key beside it, and names `migrate-values`. Argo CD 3.5.x leaves an Application's live objects as they are while a source fails to render.

Each workload's main container is now named for its chart, such as `dfe-engine` (was `engine`) and `dfe-transform-vrl` (was `transform`). culvert's stays `culvert`. A `kubectl logs -c` or a log query naming the 2.2.0 container needs the new name.

## dfe-ops upgrade

`dfe-ops upgrade <verb> --deploy <dfe-deploy checkout> [--to <stack>]` drives a stack upgrade from a deployment repo's own `pins.yaml`, walking `upgrade-order.yaml` stage by stage. Run it along a path `dfe-stack check-upgrade` reports as `SUPPORTED`, which in 2.2.x is a release candidate to the next one or to the release. `--to` defaults to versions.yaml's `current` pointer. The FROM stack is read from the deploy's `pins.yaml` (`base.dfe-infra`).

The bundled in-cluster deploy repo is seeded without a `pins.yaml`. Against a deploy repo with none, `plan`, `preflight` and `apply` take FROM from the cluster secret's `dfe.hyperi.io/stack_version`, which bootstrap writes and every retarget moves, and say so. `plan` takes `--kubeconfig` for that read. `apply` writes a `pins.yaml` in the dfe-deploy template's shape at its first stage, and from that commit on `pins.yaml` is what FROM is read from. A deploy repo with neither refuses, naming both.

The bundled repo is private and its only credential is a cluster Secret, so `apply` and `rollback` take `--bundled-forgejo`. `--deploy` is then a checkout that is cloned on first use, the Secret is read in the process, Forgejo is port-forwarded for the run, and the push needs no clone or credential of yours.

`preflight` and `apply` find ClickHouse by `--clickhouse-selector`, which defaults to `app.kubernetes.io/name in (dfe-clickhouse,clickhouse-server)`. That's the single-mode StatefulSet's pods and the ClickHouse operator's server pods, never its Keeper pods. The merge check asks every pod it matches, because `system.merges` is per server, and refuses when any one carries a long merge or doesn't answer. It logs in with the `password` key of the `--clickhouse-credentials` Secret (default `clickhouse-admin-password`) as `default`, then `admin`, then as `default` with no password, and never prints the password.

- `plan` diffs FROM -> TO, grouped by stage, each step carrying its `before`/`finalise`/`pair`/`rollback` note. A step moves when its pin moves, or the image or thin-chart digest its `image:`/`chart:` names does. `dfe-hyperdx` is the step whose pin is `content.dfe-hyperdx`. It runs `dfe-stack compat-check --strict` for TO and preflight's two exposure checks, the ingest door and the web plane ([From 2.2.0](#from-220)). It lists the 2.2.0 value keys an upgrade across 2.2.1-rc.1 carries ([Values](#values)), and writes the numbered plan to `--out`, by default `.tmp/upgrades/<from>-to-<to>.md` in the dfe-infra checkout. It never writes to the deploy repo, because `preflight` refuses a deploy tree with an untracked file in it. The web check reads the cluster secret's `dfe.hyperi.io/cloud` through `--kubeconfig` only when `--cloud` is not given and the deploy repo leaves a web group unstated. Pass `--dial <deployment.yaml>` (with `--fixtures` or `--live`) to also run `resolve_sizing.py`'s locked-change classifier against the deploy's committed `sizing/resolved.yaml`. A LOCKED field moving without `--migrate` blocks the plan. Exit 0 is clean, 1 is blocked (compat-check failed, an exposure check failed, a 2.2.0 value key needs an edit, or an unmigrated locked change), and 2 means the deploy repo or stack name don't resolve. <!-- doc-paths: ignore -->
- `preflight` is the gate `apply` won't run without. It checks that:
  - the deploy repo is clean
  - its `infra/common.yaml` sets `exposure.groups.ingest` when the upgrade crosses 2.2.1-rc.7
  - it states the `console` and `api` allow lists when the upgrade crosses 2.2.1-rc.6 on an internet-facing gateway (both in [From 2.2.0](#from-220))
  - no 2.2.0 value key needs an edit when the upgrade crosses 2.2.1-rc.1 ([Values](#values))
  - the cluster answers
  - every Argo Application is Synced and Healthy
  - no KafkaRebalance is running
  - ClickHouse carries no merge past `--clickhouse-merge-threshold`
  - no `schema_lock` table's quorum names a part every replica has lost. A wedged lock no engine can read, and the check prints the `DROP TABLE <db>.schema_lock SYNC` to run while no engine holds the lease. An engine at v1.22.85 or later recreates the table at boot, and an older one cannot
  - all ten Strimzi CRDs store `v1` only (checked only when the plan crosses the conversion, and a CRD kubectl can't read fails)
  - the on-prem node capacity holds the new sizing (`check_node_capacity.py`)
  - a backup marker exists at `--backup-marker` when the plan carries a one-way step

  Each check prints `PASS`/`FAIL` with the evidence line that decided it.
- `apply` runs preflight, then reads the cluster secret and the Kafka CRs and refuses before anything moves if either is in a state it can't move. It then walks the plan stage by stage:
  - It bumps `pins.yaml`'s `base.dfe-infra` via a surgical field edit.
  - It re-runs the resolver with `--migrate` when `--dial` is given and copies its `sizing/` output over `<deploy>/sizing/`.
  - It commits the stage (`chore(upgrade): <stack> stage <n> -- <keys>`) and pushes only with `--push`.
  - It waits for Argo, then for every Deployment and StatefulSet in the DFE namespace to finish rolling out, bounded by `--timeout`.

  A rollout is finished on the conditions `kubectl rollout status` waits on, because an Application can read Healthy while its Deployment still rolls. A timeout names each rollout still running.

  After a push that carried a commit, and after a retarget, every Application must also have a `status.reconciledAt` later than the moment just before that change. Until Argo compares an Application against it again, the Synced and Healthy it reports are from before. Argo does that on its own `timeout.reconciliation` cycle (bootstrap sets 300s) unless a change to the Application's spec triggers it sooner, so a pushed stage can wait that long. A timeout names each Application not yet reconciled. An Application made only of pinned Helm chart sources, which Argo last compared against its current spec (`status.sync.comparedTo`), is exempt, because neither change can move it without changing that spec. Auto-sync skips a revision it already synced, so an auto-synced Application whose spec changed beside a pinned chart stays OutOfSync after Argo compares it, and `apply` starts one sync on it with its own sync options.

  The namespace is `--namespace`, else the cluster secret's `dfe.hyperi.io/dfe_namespace`, and a secret naming none refuses the run before anything moves. The first stage moving an Argo-managed component writes the Kafka holds and moves `target_revision`, as [Kafka version moves](#kafka-version-moves) describes. Across 2.2.1-rc.1 it carries the 2.2.0 value keys in its commit and again just before the retarget ([Values](#values)). A secret tracking a branch is left alone, one pinned to a commit needs `--target-revision <ref>`, and a retarget refuses without `--push`. After a Strimzi operator bump apply waits on every Kafka CR's `status.operatorLastSuccessfulVersion`, because the CR's own `Ready` condition stays True and stale across the lift.

  A `before` note with no program needs a confirmed "I ran this by hand". Apply confirms before each stage unless `--yes`, and `--stop-before <stage-key>` halts before a named stage. A reached `finalise` note stays pending unless `--finalise` is given. That asks whether the soak is over, runs the step's finalise program and writes `upgrades/<from>-to-<to>.finalised`, the marker `rollback` reads. `--from <stack>` names the FROM stack once `pins.yaml` already names the target, for a finalise after the soak or a resumed run. Without it, a deploy repo holding commits its remote lacks takes FROM from the remote's `pins.yaml`, because that's what Argo reads, so a rerun after a failed push replays the stage and pushes it. `--dry-run` prints every command and touches nothing.
- `rollback --to <stack>` is the reverse plan. It refuses by name a step carrying `rollback: none` with no `finalise`, or a `finalise`-bearing step whose marker exists. With no marker yet the step reverses like any other. A rollback that moves `services.kafka-version` also reads every live Kafka CR and refuses when `status.kafkaMetadataVersion` is above the target's Kafka line, marker or not. That's what an unpinned metadata version leaves behind. It moves `target_revision` back the same way `apply` moves it forward, and needs `--push` to. `--skip-cluster-check` moves the pin alone, reading nothing. Once `apply --finalise` has dropped the 2.2.0 value keys, it refuses a rollback below 2.2.1-rc.1.
- `migrate-values --deploy <dir> [--check]` writes the thin-chart keys beside the 2.2.0 value keys and commits nothing. `--check` writes nothing and exits 1 while a key is left ([Values](#values)).

### What a stage moves

One pin, `base.dfe-infra`, selects the whole certified stack, and `apply` sets it to the target at every stage, so the first stage's commit carries the whole pin move. One ref, `target_revision`, selects every chart, and `apply` moves it once, at the first stage that moves an Argo-managed component. Every chart and operator converges on the target during that stage's wait. Later stages skip their commit unless they add something, such as dropping a Kafka hold or writing a finalise marker.

The work around the pin still runs in stage order: confirms, `before` checks, `finalise` notes and waits. The Kafka version hold is the one place a component waits for its own stage. `--stop-before` skips a stage's hooks and waits, not its component versions.

The `10-bootstrap` stage is the exception. `bootstrap.sh` installs external-secrets, cert-manager and Argo CD before Argo exists, and Argo never renders them, so their pins move nothing on the cluster. After that stage `apply` reads the chart version each release runs off its Deployment's `helm.sh/chart` label. It prints `[DONE]` where that matches the pin, and otherwise `[PENDING]` with the command that moves the release, for example:

```bash
helm -n cert-manager upgrade cert-manager cert-manager --repo https://charts.jetstack.io --version v1.21.2 --reset-values --set crds.enabled=true --set config.enableGatewayAPI=true --wait --timeout 5m
```

`--reset-values` starts from the new chart's defaults, and every value after it is one `bootstrap.sh`'s install sets. Both read them from `bootstrap/helm_releases.py`, so the upgraded release carries what a fresh bootstrap installs. Argo CD's command also pipes in the login values `bootstrap/argocd_login.py` computes, as `bootstrap.sh` does, and reads the domain off the cluster secret's `dfe.hyperi.io/domain` as it runs, stopping before helm when the secret names none. `apply` never runs it. The walk carries on, and a run with anything still pending ends `NOT complete` with exit 1. Run the printed commands, then re-run `apply` with `--from <stack>` to confirm.

After the walk `apply` reads every other bootstrap pin of the target stack the same way, marked `(unchanged by this upgrade)`. So a release an earlier upgrade left on its old chart keeps a later upgrade from reporting OK, even when that later plan moves no bootstrap pin.

An Argo CD that `bootstrap/argocd_release.py` doesn't recognise as `bootstrap.sh`'s own reads `[ADOPTED]` and is left to its owner, the same call `bootstrap.sh` makes before it upgrades Argo. Nothing tells a cert-manager or external-secrets `bootstrap.sh` installed from one it adopted, so their command says to run it only where this deploy installed them. A `bootstrap.sh` re-run skips a running install of either unless `DFE_FORCE_INSTALL=true`.

## Re-size

Re-run the sizing resolver with a new estimate, focus or ratio and diff the result against `sizing/resolved.yaml`. Every changed field is classified: <!-- doc-paths: ignore -->

- LIVE-SAFE: broker count up (the rebalancer spreads data onto the new brokers), ClickHouse replicas up, a PVC growing, an instance type moving under a Karpenter node roll, retention, any dynamically updatable dial.
- DRAIN-FIRST: broker count down. `remove-brokers` moves data off the broker before its pod goes.
- LOCKED: the six fields in `sizing.yaml`'s `locked:` section. They are partition count, storage model, Kafka provider, combined against separate KRaft controllers, the cloud token and the AZ count. The resolver refuses a move in any of them without `--migrate` and the runbook it names. In the deployment repo, `governance/policies/sizing-locks.yaml` holds the chart keys behind three of them as protected vars (`kafka.sizing.*`, `kafka.controllerPool.enabled`, `cloud`), and `storage-layout.yaml` holds the storage model's (`clickhouse.storageModel`, `kafka.storageModel`). The Kafka provider and the AZ count are OpenTofu inputs with no chart key, so the resolver is their only lock.

## Platform upgrade

On EKS the control plane goes first, then Karpenter drift rolls the nodes. On MSK the Kafka version moves in place and Express broker size moves vertically. GKE and AKS follow the same shape when their roots exist. Pre-flight checks version skew from `versions.yaml`: librdkafka against the broker, the ClickHouse client against the server, Karpenter against the Kubernetes version.

## Migrating from DFE 2.x before 2.2

A private-cloud DFE deployment run before 2.2 used an internal ClickHouse fork with its own SSD cache in front of bulk storage, under an internal operator (described, not named, in [clickhouse.md](clickhouse.md)). DFE 2.2 retired that pairing for less code to maintain, lower operational risk, and a storage model held in common across on-prem and cloud deployments.

The replacement is the official ClickHouse operator with the `cached-object` storage model: an object-store disk with a local cache disk in front of it, under the `s3_cached` policy. `cached-object` is now the default wherever an object-store endpoint exists (`sizing.storage_model: auto`), and `local` is the explicit opt-out.

Migrating off the retired pairing means a second deployment beside the first. `clickhouse.storageModel` is a protected var for the life of a deployment (see [clickhouse.md](clickhouse.md)), so the storage model can't flip under the running one. Stand up a DFE 2.2 deployment with an object-store endpoint. That's a cloud bucket via the cluster module on AWS, or on-prem an S3-compatible endpoint such as MinIO (the supported flavours are in [storage.md](storage.md)'s object-store dimension). Run the new deployment alongside the old one until data has moved and the cutover is proven.

Move the data by ClickHouse `BACKUP`/`RESTORE` to the object store where the source server supports it. This repo carries no documented replay path from dfe-archiver or any other component back into ClickHouse. So where `BACKUP`/`RESTORE` isn't available the re-ingest comes from the deployer's own retained raw stream, and is a customer-specific plan. Once the new deployment holds the required retention window, cut the senders over to its receiver address, then decommission the old deployment.

### Payload format

JSON is the only payload format. MessagePack, supported in DFE/XDR 2.0 and 2.1, is deprecated in DFE 2.2 and no longer accepted. The JSON path (SIMD parsing with sonic-rs, zstd on the wire) is fast enough that MessagePack gave no CPU saving. A producer outside DFE that writes MessagePack to a DFE topic has to switch to JSON, because a MessagePack record is dead-lettered as not JSON. The loader's `payload.format` and dfe-transform-vrl's `source.format` settings are gone. Fluentd input over the Fluent Forward protocol is unaffected: the receiver still accepts it and turns it into JSON.

Three things need a customer-specific plan on top of the steps above:

- schema differences between the fork's engine and upstream MergeTree
- any table or setting the fork added that upstream doesn't have
- a retention window longer than the cutover overlap can cover

During the overlap, compare the old and new deployments using your heaviest queries.

## dfe-hyperdx document store

From 2.2.1 dfe-hyperdx keeps its state in a DocumentDB cluster that the DocumentDB operator runs on CloudNativePG (`helm/charts/documentdb`), in place of FerretDB. The two keep documents in different PostgreSQL schemas, so nothing moves across in place. 2.2.1 is a fresh install. A deployment upgraded from an earlier stack starts dfe-hyperdx on an empty store, without the saved searches, alerts, users and unprovisioned dashboards it held.

One that has to keep them copies them before dfe-hyperdx cuts over, with `mongodump` against the FerretDB endpoint and `mongorestore` into the gateway, the step upstream documents for moving data between DocumentDB clusters. The restore side needs TLS against the store's CA and SCRAM-SHA-256, the options dfe-hyperdx's own `mongodb.uri` carries.

Between DocumentDB releases the upgrade is in place and two-phase. The binary moves first, with `documentdb.version` and the extension and gateway images in the chart's values, and reverting them rolls it back. The schema moves only when `spec.schemaVersion` is set, which runs `ALTER EXTENSION documentdb UPDATE` and can't be undone. The chart sets no `schemaVersion`, so an image bump never migrates the schema by itself. Upstream's runbook: https://github.com/documentdb/documentdb-kubernetes-operator/blob/1.0.0/docs/operator-public-documentation/preview/operations/upgrades.md

The operator mounts the extension as an image volume, which needs the Kubernetes ImageVolume feature. It's on by default from 1.35 and behind a feature gate on 1.33 and 1.34. Without it the operator's admission webhook refuses the DocumentDB CR. The operator chart's CRDs reach the cluster through Argo CD, server-side, so an operator upgrade carries its CRD changes with it.

The operator chart 1.0.0 bundles the cloudnative-pg chart 0.28.3, whose CRDs and RBAC are CloudNativePG 1.29.1's, while the operator runs 1.29.3. `helm/charts/documentdb` grants the EndpointSlice read 1.29.3 needs. The 1.29.1 CRDs drop three status fields 1.29.3 writes, one of them the target image of an in-progress PostgreSQL major upgrade, so a major upgrade waits for an operator chart that bundles matching CRDs.

CloudNativePG 1.29 is end-of-life upstream, and 1.29.3 its last release. DFE moves to a supported line with the DocumentDB operator chart's next CloudNativePG bump.

## What cannot be rolled back

- A DocumentDB schema update (`spec.schemaVersion`) is one way.
- Kafka's `metadata.version` bump is one way, so the roll and the finalise are separate steps with a soak between them, and `apply` holds the metadata version so Strimzi can't take that step on its own.
- ClickHouse downgrades only within the same LTS line.
- `apply --finalise` across 2.2.1-rc.1 drops the 2.2.0 value keys, after which no stack before 2.2.1-rc.1 renders the deploy repo until that commit is reverted ([Values](#values)).
- Kafka data is a buffer. Before upgrading, check that topic retention covers the upgrade window and the time consumers need to catch up. ClickHouse on `cached-object` keeps its parts in the object store.

Pre-flight snapshots the Argo application versions, `sizing/resolved.yaml` <!-- doc-paths: ignore -->
and the tofu state, and prints the one-way steps before asking to continue.

`dfe-ops upgrade rollback --to <stack>` (above) refuses by name a step whose finalise has already run, read from the `upgrades/<from>-to-<to>.finalised` marker `apply --finalise` writes. It also refuses a Kafka rollback whenever the live metadata version is above the target's line.

Between the Kafka broker roll and its `metadata.version` finalise, soak the cluster under normal ingest for 24 hours by default. Watch consumer lag, under-replicated partitions, ClickHouse's merge backlog and insert errors, and confirm KEDA and Cruise Control stay quiet. A problem ends the soak early. `dfe-ops upgrade rollback --to <stack> --push` reverses the pin and `target_revision` like any other step, because the metadata version is still held.

[upgrade-rollback.md](upgrade-rollback.md) has the full per-stage runbook, the sizing config-vs-data rule for a locked field, and what `apply --finalise`/`--stop-before` do at finalise time.
