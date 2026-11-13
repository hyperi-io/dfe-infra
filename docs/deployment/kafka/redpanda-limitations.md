# Redpanda in your cluster

Redpanda is a Kafka-API-compatible broker that DFE can run in your cluster in place of Strimzi. It has no JVM and no separate controller nodes, which suits single-node installs, small deployments and test rigs. Strimzi stays the default. Redpanda Cloud, the managed service, has its own guide: [redpanda-cloud.md](redpanda-cloud.md).

Redpanda Community Edition is source-available under the Business Source License 1.1, not an open-source licence. Read [the licence](https://github.com/redpanda-data/redpanda/blob/dev/licenses/bsl.md) and [Redpanda's licensing page](https://docs.redpanda.com/current/get-started/licensing/) and decide whether your use fits them.

The kafka chart refuses to render Redpanda without `kafka.redpanda.acceptLicense: true`. Redpanda's Enterprise features, such as Tiered Storage, need Redpanda's own licence. DFE uses none of them, because `dfe-archiver` does its own archival to object storage.

## Turning it on

Pick the provider at the first deploy. Switching a live deployment to the other broker leaves every message already written on the old one, and DFE has no way to move them. [storage.md](../storage.md) covers what else is fixed at the first deploy.

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
| `scale` profile | Strimzi operator, installed with the profile | Redpanda operator, installed only when the cluster secret carries the label |
| JVM | Yes | No |
| Broker autoscaling with KEDA | On `scale`, once the chart has a trigger: a landing source, or the Prometheus trigger. Check with `kubectl -n strimzi get scaledobject` ([autoscaling.md](autoscaling.md)) | No, set `kafka.replicas` |
| Kafka metadata version holds during upgrades | Yes | No, Redpanda has none |

Redpanda's own gaps against Apache Kafka are in [its Kafka compatibility notes](https://docs.redpanda.com/current/develop/kafka-clients/). None of them touches what DFE does: it produces, consumes and runs consumer groups, and creates its topics through the admin API.

The Redpanda operator has one known bug that the chart works around. The `User` resource finds the cluster by name, and that lookup fails when SASL is on ([redpanda-operator #1130](https://github.com/redpanda-data/redpanda-operator/issues/1130)). So the chart gives it the broker and admin addresses directly.
