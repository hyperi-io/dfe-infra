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
Until `dfe-ops upgrade` exists the order is applied by hand from that file.

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
