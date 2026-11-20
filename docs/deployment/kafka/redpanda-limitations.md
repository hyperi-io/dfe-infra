# Redpanda in your cluster

Redpanda is a Kafka-API-compatible broker that DFE can run in your cluster in place of Strimzi. It has no JVM and no separate controller nodes, which suits single-node installs, small deployments and test rigs. Strimzi stays the default. Redpanda Cloud, the managed service, has a separate guide: [redpanda-cloud.md](redpanda-cloud.md).

Redpanda Community Edition is source-available under the Business Source License 1.1, not an open-source licence. Read [the licence](https://github.com/redpanda-data/redpanda/blob/dev/licenses/bsl.md) and [Redpanda's licensing page](https://docs.redpanda.com/current/get-started/licensing/) and decide whether your use fits them.

The kafka chart fails the render on Redpanda without `kafka.redpanda.acceptLicense: true`. Redpanda's Enterprise features, such as Tiered Storage, need a Redpanda licence. DFE uses none of them, because `dfe-archiver` archives to object storage.

A new Redpanda cluster starts on a 30-day trial with two Enterprise features on: continuous partition balancing and continuous core balancing. If either is on when the trial ends, the broker refuses upgrades to new feature releases. The chart sets `partition_autobalancing_mode` to `node_add` (or `off`) and `core_balancing_continuous` to `false`. It fails the render on `kafka.rebalancing.redpanda.partitionAutobalancingMode: continuous`.

- `scale`: the operator applies both properties to a running cluster at the next sync.
- `single`: the broker sets both at every start, then exits if `rpk cluster license info` still lists an Enterprise feature. A running broker picks them up at its next pod restart.
- `kafka.mode: external`: the chart deploys no broker, so it sets neither. Whoever runs that Redpanda turns both off there.

With a Redpanda Enterprise licence (beta, and untested against a paid licence), put it in a Secret under the key `license` and name the Secret in `kafka.redpanda.enterprise.licenseSecretRef.name`. The chart then hands it to the broker and stops forcing either feature off, so `partitionAutobalancingMode: continuous` renders. In `single` mode the broker applies it at every start and exits if `rpk cluster license info` reports a violation.

To fix a running broker now, without a restart:

```bash
kubectl -n strimzi exec <broker pod> -- rpk cluster config set partition_autobalancing_mode node_add
kubectl -n strimzi exec <broker pod> -- rpk cluster config set core_balancing_continuous false
kubectl -n strimzi exec <broker pod> -- rpk cluster license info
```

## Turning it on

Pick the provider at the first deploy. Switching a live deployment to the other broker leaves every message written on the old one, and DFE has no way to move them. [storage.md](../storage.md) covers what else is fixed at the first deploy.

Set the provider in the env file you deploy with:

```bash
DFE_KAFKA_PROVIDER=redpanda
```

Then accept the licence in the deploy repo's `infra/kafka.yaml`:

```yaml
kafka:
  redpanda:
    acceptLicense: true
```

The `single` profile then runs one Redpanda broker as a plain StatefulSet, with no operator. The `scale` profile runs a multi-broker cluster through the Redpanda operator and broker versions `versions.yaml` pins (`operators.redpanda-operator`, `services.redpanda-version`). That cluster setup hasn't been proven on a live cluster yet, so test it at your target size before you rely on it. It needs two more steps:

1. Label the cluster secret, which is what installs the operator. Bootstrap doesn't set this label.

   ```bash
   kubectl -n argocd label secret dfe-cluster dfe.hyperi.io/kafka-provider=redpanda
   ```

2. Point the apps at the Redpanda cluster in the deploy repo's `infra/common.yaml`, because the `scale` profile names Strimzi's Service:

   ```yaml
   kafka:
     bootstrapServers: "dfe-kafka.strimzi.svc.cluster.local:9093"
   ```

## How it differs from Strimzi

Both run SASL/SCRAM-SHA-512 from first boot, with TLS off on the in-cluster listener. Both give the apps the same `dfe-kafka-user` Secret, so the apps don't know which broker they're talking to.

| | Strimzi | Redpanda |
|---|---|---|
| Licence | Apache-2.0 | Business Source License 1.1 |
| `single` profile | One Apache Kafka broker, no operator | One Redpanda broker, no operator |
| `scale` profile | Strimzi operator, installed with the profile | Redpanda operator, installed when the cluster secret has the label |
| JVM | Yes | No |
| Broker autoscaling with KEDA | On `scale`, once the chart has a trigger: a landing source, or the Prometheus trigger. Check with `kubectl -n strimzi get scaledobject` ([autoscaling.md](autoscaling.md)) | No, set `kafka.replicas` |
| Kafka metadata version holds during upgrades | Yes | No, Redpanda has none |

Redpanda's gaps against Apache Kafka are in [its Kafka compatibility notes](https://docs.redpanda.com/current/develop/kafka-clients/). None of them touches what DFE does: it produces, consumes and runs consumer groups, and creates its topics through the admin API.

The Redpanda operator has one known bug that the chart works around. The `User` resource finds the cluster by name, and that lookup fails when SASL is on ([redpanda-operator #1130](https://github.com/redpanda-data/redpanda-operator/issues/1130)). So the chart gives it the broker and admin addresses directly.
