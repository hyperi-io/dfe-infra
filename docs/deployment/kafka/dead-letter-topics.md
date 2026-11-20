# Dead-letter topics

Every DFE app on Kubernetes dead-letters to a Kafka topic dfe-engine creates:

| App | Topic |
|---|---|
| dfe-receiver | `dfe_receiver_dlq` |
| dfe-loader | `dfe_loader_dlq` |
| dfe-archiver | `dfe_archiver_dlq` |
| dfe-fetcher | `dfe_fetcher_dlq` |
| every transform | `dfe_transform_dlq`, unless a transform names a separate topic |

The five names and their retention are declared in dfe-schemas, and dfe-engine creates them at startup on every tier and provider. A dead-letter write happens at failure time, the one moment nothing can be creating topics, so the engine's wave-5 sync is what has them in place before an app can need one.

They keep 7 days, against the in-cluster broker's 24-hour default for data topics. A poisoned record is the one an operator still has to find days later, and once its source offset commits it exists nowhere else.

## How the apps reach them

The charts set `dlq.mode: kafka_only` for the receiver, loader, archiver and fetcher, in `argocd/values/apps/dfe-archiver/values.yaml`, `argocd/values/apps/dfe-fetcher/values.yaml`, and the receiver and loader blocks of `helm/charts/dfe-extras/values.yaml`. Each app gets the same `DLQ_TOPIC` and `DLQ_MODE`, in its env regime. Chart env doesn't point the transforms at `dfe_transform_dlq` in this release.

Kafka is the one backend because the file fallback can't write under the charts' read-only root filesystem, and would drop silently. `cascade` would fall through to it, so it isn't used.

An entry over the producer's `message.max.bytes`, counting its base64 payload a third larger than the record, fits no backend. It is dropped and counted on `messages_dropped_total`, reason `dlq_too_large` on the archiver.

On Docker, dead-letter topics are opt-in per component, except on the single-node full deploy, which has the same topics as Kubernetes.
