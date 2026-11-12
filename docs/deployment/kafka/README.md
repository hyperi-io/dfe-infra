<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/deployment/kafka/README.md
Purpose:   Index for the DFE Kafka platform guides. States DFE's opinionated
           broker/cluster/topic defaults ONCE, and the settable-vs-locked
           matrix across the managed clouds DFE does NOT auto-deploy.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# Kafka providers

DFE talks to Kafka through one open provider abstraction (scalo `transport/kafka/providers.rs`). Each provider handles its own auth and capabilities behind a single trait. The brokers DFE deploys in the cluster authenticate with SCRAM-SHA-512 over a plaintext listener (`SASL_PLAINTEXT`). Every managed broker is SASL over TLS: SCRAM-SHA-512 on MSK Provisioned and Redpanda Cloud, PLAIN on Confluent Cloud, which offers no SCRAM, and IAM on MSK Serverless, which mandates it. A deployment on a TLS listener sets `kafka.securityProtocol: SASL_SSL` with its `kafka.bootstrapServers` in its deploy repo's `infra/common.yaml`.

The charts take `security.protocol` and `sasl.mechanism` from one table, `helm/library/dfe-common/templates/_kafka.tpl`. Nobody sets a mechanism by hand. The table has two extra keys, `strimzi-no-tls` and `redpanda-no-tls`, for a DFE-owned broker on the TLS-off listener the charts stand up in-cluster. Every other key matches scalo's canonical table.

DFE deploys two providers as in-cluster brokers, Strimzi (the default, Apache-2.0) and Redpanda (opt-in, BSL). For the managed clouds it doesn't deploy, there's one guide each:

- [aws-msk.md](aws-msk.md) covers AWS MSK provisioned (SASL/SCRAM) and MSK Serverless (IAM). Running an MSK deployment (bootstrap Job, autoscaling, telemetry) is in [aws-msk-operations.md](aws-msk-operations.md).
- [confluent-cloud.md](confluent-cloud.md) covers Confluent Cloud (API-key PLAIN over TLS).
- [redpanda-cloud.md](redpanda-cloud.md) covers Redpanda Cloud Serverless, Dedicated and BYOC (SCRAM).

Where DFE deploys the broker, these defaults live in `helm/charts/kafka/values.yaml` (`kafka.config`). The Strimzi Kafka CR, the single-broker `server.properties` (no operator) and the Redpanda CR all apply them. On a managed cloud you set the settable subset yourself, with the Terraform snippets in each guide. The platform fixes the rest, and each guide says which.

## The baseline

> **Coming from Splunk or Elastic?** Kafka here is a buffer between stages. Its retention default is consumer downtime plus archiver lag (see `log.retention.ms` below), and dfe-archiver copies to object storage.

Each row gives the default, one value per line, and the reason for it. Sources are at the end.

| Setting | DFE default | Why |
|---|---|---|
| Metadata plane | KRaft (no ZooKeeper) | Apache Kafka 4.0 (2025-03) removed ZooKeeper outright, so KRaft is the only mode in the 4.x line. Redpanda is Raft-native. |
| `replication.factor` | 3 (clamped to broker count on single/small) | RF3 survives one broker loss with the data intact. `min(RF, replicas)` clamps it so single and small tiers still render. |
| `min.insync.replicas` | 2 | With RF3 and `acks=all`, a write is acknowledged only once 2 replicas hold it, so one broker dying loses nothing. `RF - 1` is the balance point. |
| Producer `acks` | `all` (client) | scalo turns on the idempotent producer by default, which forces `acks=all`. Nothing to do on the broker. |
| `unclean.leader.election.enable` | `false` (Kafka 4.x default, not set) | Consistency over availability. A data-forensics engine must never surface a leader that lost committed writes. Kafka 4.x already defaults this to false, so DFE leaves it alone. |
| `num.partitions` (default) | Derived: 12 on scale, 3 on single | One count from the broker count and the KEDA consumer ceiling (`kafka.sizing.*`), so leadership stays balanced and no consumer idles. You can't repartition a keyed topic later without breaking `hash(key) % partitions`, so the resolver only ever raises it. KRaft removed the old 200k/4k partition ceilings. |
| `compression.type` | `producer`, on each topic | dfe-engine sets it on every topic it creates (`kafka.topic_compression_type`), so a broker whose own default differs never recompresses. Store whatever codec the client sent, so the broker never decompresses and recompresses on the ingest hot path. scalo's client sizing profile picks the codec (lz4 by default, zstd opt-in for storage-bound topics). |
| `log.retention.ms` | 24 hours | `(kafka.retention.assumedConsumerDowntimeH + archiverLagH)` hours, which is how long a consumer can be down before an event is lost. dfe-archiver also copies to object storage, so time retention is the durable, portable default. |
| `log.retention.bytes` | PVC x 0.85 / partition replicas a broker holds for ONE topic | Each topic gets the full per-partition allowance, so any one topic can use the whole volume and the size cap never deletes unread data ahead of the time window. The broker disk is sized for the busy topics. The resolver counts every event on `_land`, and the dial's `sizing.transformed_share` of them again on `_load`. The `kafka.diskAlert` PrometheusRule warns at 70% and goes critical at 85%, before a disk fills. A full disk stops the broker, and the receiver spools through that. A deleted record is silent loss. |
| `log.segment.bytes` | 1 GiB (Kafka default, not set) | 512 MiB doubles the mmap count against `vm.max_map_count` at DFE's partition counts, so the 1 GiB default stays. |
| `cleanup.policy` | `delete` (default) | Our topics are append-only event streams, not compacted key/state topics. We leave the broker default alone. |
| `message.max.bytes` | 16 MiB (`kafka.messageMaxBytes`) | One figure for the whole chain, so a produce is never rejected and a follower never stalls. See below. |

### message.max.bytes: one figure for the chain

DFE sets broker `message.max.bytes`, broker `replica.fetch.max.bytes` and every topic's `max.message.bytes` to one figure, 16 MiB by default (`kafka.messageMaxBytes`, overridden by the resolved sizing's `event.message_chain_bytes`). Raise one alone and a produce is rejected or a follower stalls. The receiver's request-body cap is 8,000,000 bytes (`maxBodySize`), below that figure.

The figure is the largest record batch AFTER compression. filebeat truncates at its own 10 MiB, and enrichment re-enters Kafka at about 1.48 times the source event, hence 16 MiB. For the odd large blob, send a pointer to object storage instead.

## Settable vs locked, per platform

Settable means you can set it to the DFE default. Locked means the platform fixes or hides it, so the default is either already met or can't be applied (each guide says which). Per-topic means there's no broker default, so you set it on each topic.

| Setting | Self-hosted (Strimzi / Redpanda, DFE-deployed) | MSK Provisioned | MSK Serverless | Confluent Cloud | Redpanda Cloud |
|---|---|---|---|---|---|
| Auth | SCRAM-SHA-512 | SASL/SCRAM-512 (Secrets Manager) | IAM only (OAUTHBEARER, port 9098) | API-key PLAIN over TLS | SCRAM-SHA-512 |
| `replication.factor` | Settable (3) | Locked -- forced to 3 (topic RF other than 3 is silently made 3) | Locked -- managed (2 hidden replicas, not counted, not settable) | Locked -- fixed 3 across 3 AZs | Settable per-topic (3) on Dedicated/BYOC, managed on Serverless |
| `min.insync.replicas` | Settable (2) | Broker-level settable, topic-level PROTECTED | Locked -- managed | Settable per-topic but ONLY 1 or 2 (use 2) | Per-topic settable, else Raft quorum |
| `unclean.leader.election.enable` | Kafka default (false) | Broker-level settable, topic-level PROTECTED | Locked -- managed | Locked -- managed (not exposed) | N/A -- Raft never elects a non-quorum leader |
| `num.partitions` default | Derived (12 on scale, 3 on single) | Broker default settable (12) | Locked, no broker config. Per-topic only, quota 2400 leader partitions/cluster | Per-topic (Terraform default 6, set 12), increase-only | Per-topic settable. Serverless auto-scales |
| `compression.type` | Per-topic, set by dfe-engine (`producer`) | Per-topic, set by dfe-engine (`producer`) | Per-topic (`compression.type`) | Per-topic settable | Per-topic (`producer` default) |
| `log.retention.hours/ms` | Settable (24h) | Broker settable (72h from the dial's `kafka.msk.log_retention_ms`) | Per-topic `retention.ms`, 250 GB/partition hard cap | Per-topic `retention.ms` | Per-topic `retention.ms`, cluster `log_retention_ms` on Dedicated |
| `log.retention.bytes` | Settable (unset) | Broker settable | Per-topic `retention.bytes` | Per-topic `retention.bytes` | Per-topic `retention.bytes` |
| `log.segment.bytes` | Kafka default (1 GiB) | Broker settable | Locked -- managed | Locked -- not exposed | Cluster `log_segment_size` on Dedicated, managed on Serverless |
| `message.max.bytes` | Settable (16 MiB) | Broker settable | Per-topic `max.message.bytes` | Per-topic `max.message.bytes` | Per-topic `max.message.bytes` |

The more managed the platform, the fewer broker dials it exposes. The DFE default is then either already enforced (RF3, KRaft) or has to be set per topic. MSK Provisioned is closest to self-hosted, with the full `server.properties`. MSK Serverless and Confluent Cloud hide almost all broker config. Redpanda Cloud sits between them, with per-topic control and cluster config on the Dedicated tier only.

## Storage model: `local` or `tiered-object`

`kafka.storageModel` is fixed for the life of the deployment and defaults to `local`, where every segment stays on the PVC. The full matrix is in [../storage.md](../storage.md).

`tiered-object` is KIP-405 tiered storage. Closed segments move to object storage, so the PVC only has to hold the hot window. It renders `spec.kafka.tieredStorage`, the broker-wide `remote.log.storage.system.enable`, and the per-topic `remote.storage.enable` on the landing topic. DLQ topics stay local.

Three constraints, all checked at render:

- Cluster mode with the strimzi provider. `tieredStorage` is a Strimzi CR field and the single tier runs no operator.
- Strimzi 0.38.0 or later. `kafka.operatorVersion` mirrors the `versions.yaml` pin, and `scripts/check_versions_drift.py` keeps the two together. Strimzi silently drops CR fields it doesn't know, so an older pin fails the render instead.
- A RemoteStorageManager plugin. Strimzi supports `type: custom` only and ships no implementation, so `kafka.tieredObject.className` must name a class the broker image carries, on `kafka.tieredObject.classPath`.

Plugin settings go in `kafka.tieredObject.config` verbatim, but credentials don't. The chart wires `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` into the broker from a Secret that ESO materialises from `<project>/<env>/kafka/tiered`, bound by `kafka.objectStore.*`.

Changing the model on a live cluster strands the segments already written. So the engine holds `kafka.storageModel`, `kafka.tieredObject.*` and `kafka.objectStore.*` as protected vars.

## Wiring a managed cluster back to DFE

1. Stand up the cluster with the guide's OpenTofu snippet. It has the right auth for the platform and whatever DFE defaults the platform lets you set.
2. Point DFE at it: `kafka.mode=external`, `kafka.external.bootstrap=<endpoint>` and `kafka.securityProtocol=SASL_SSL`, because every managed cloud here serves TLS only. Every app chart and dfe-engine read the wire protocol from that one dial, never from a provider key. On the bootstrap path you set none of the three by hand: `DFE_KAFKA_PROVIDER` of `msk`, `confluent-cloud` or `redpanda-cloud` puts them on the Argo cluster secret, and the layer 2 appsets hand them to the charts.
3. Get the client config for that provider from the CLI: `dfe kafka client-config --provider <msk|msk_iam|confluent-cloud|redpanda-cloud>`. It prints the transport settings from the canonical provider table (scalo `providers.rs`), so nobody hand-picks the mechanism.
4. Supply credentials through the external-secret hook (`kafka.external.auth`). That's a SCRAM or PLAIN secret in the deployment's secrets store (Secrets Manager on AWS, OpenBao elsewhere). MSK Serverless needs no static secret at all, because IAM comes through EKS Pod Identity, bound to the consumer ServiceAccounts.

## Version pinning note

The OpenTofu snippets in these guides use external providers DFE doesn't vendor. The constraints DFE's own roots run are in `versions.yaml` under `providers` (`hashicorp-aws`, `confluentinc-confluent`, `redpanda-data-redpanda`). Pin exact provider versions in your root module before applying these templates.

## Sources (2026 research)

- Apache Kafka 4.0 KRaft / ZooKeeper removal: [Kafka 4.0 announcement](https://kafka.apache.org/blog/2025/03/18/apache-kafka-4.0.0-release-announcement/), [Confluent 4.0 blog](https://www.confluent.io/blog/latest-apache-kafka-release/).
- Durability (RF/ISR/acks/unclean): [Kafka replication design](https://docs.confluent.io/kafka/design/replication.html), [oneuptime replication + ISR](https://oneuptime.com/blog/post/2026-01-21-kafka-replication-isr/view).
- Partition sizing (KRaft era): [Confluent partition determination](https://docs.confluent.io/kafka/operations-tools/partition-determination.html), [Instaclustr KRaft max partitions](https://www.instaclustr.com/blog/apache-kafka-kraft-abandons-the-zookeeper-part-3-maximum-partitions-and-conclusions/).
- Retention + segment sizing: [Strimzi storage internals](https://strimzi.io/blog/2021/12/17/kafka-segment-retention/), [oneuptime retention policies](https://oneuptime.com/blog/post/2026-01-24-kafka-log-retention-policies/view).
- Compression (`producer` / zstd / lz4): [Confluent compression blog](https://www.confluent.io/blog/apache-kafka-message-compression/), [Conduktor compression](https://docs.conduktor.io/learn/advanced/producers/compression).
- message.max.bytes alignment: [Confluent message size limit](https://www.confluent.io/learn/kafka-message-size-limit/), [Kafka consumer configs](https://kafka.apache.org/41/configuration/consumer-configs/).
