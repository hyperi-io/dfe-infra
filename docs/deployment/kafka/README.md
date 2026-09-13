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

# DFE Kafka defaults and per-platform config guides

DFE speaks to Kafka through a generic, open provider abstraction (scalo
`transport/kafka/providers.rs`). Every provider normalises its own auth and
capabilities behind one trait; the DFE credential contract (dfe-engine#98) then
layers an opinion on top: SASL over TLS, SCRAM-SHA-512 on brokers DFE owns,
PLAIN only where the platform forbids SCRAM (Confluent Cloud), IAM only where
the platform mandates it (MSK Serverless).

DFE auto-deploys two of these providers as in-cluster brokers -- Strimzi
(default, Apache-2.0) and Redpanda (opt-in, BSL). For the managed clouds DFE
does NOT auto-deploy, this directory carries one guide each:

- [aws-msk.md](aws-msk.md) -- AWS MSK provisioned (SASL/SCRAM) and MSK Serverless (IAM);
  operating a running MSK deployment (bootstrap Job, autoscaling, telemetry) is
  [aws-msk-operations.md](aws-msk-operations.md).
- [confluent-cloud.md](confluent-cloud.md) -- Confluent Cloud (API-key PLAIN over TLS).
- [redpanda-cloud.md](redpanda-cloud.md) -- Redpanda Cloud Serverless / Dedicated / BYOC (SCRAM).

Where DFE deploys the broker itself, these defaults live in
`helm/charts/kafka/values.yaml` (`kafka.config`) and are applied by the Strimzi
Kafka CR, the non-operator single-broker `server.properties`, and the Redpanda
CR. On a managed cloud you set the SETTABLE subset yourself (Terraform snippets
in each guide); the rest is fixed by the platform and called out per guide.

## The one opinionated baseline

MINIMAL, best-for-high-volume-at-scale, works-everywhere. One value per line,
not a knob per topic. Rationale below each; sources at the end.

| Setting | DFE default | Why |
|---|---|---|
| Metadata plane | KRaft (no ZooKeeper) | ZooKeeper was REMOVED entirely in Apache Kafka 4.0 (2025-03); KRaft is the only mode in the 4.x line. Not a deprecation -- a hard removal. Redpanda is Raft-native. |
| `replication.factor` | 3 (clamped to broker count on single/small) | The durability triad. RF3 tolerates one broker loss with data intact. Clamped via `min(RF, replicas)` so single/small tiers still render. |
| `min.insync.replicas` | 2 | With RF3 + `acks=all`, a write is acknowledged only once 2 replicas hold it -- no data loss when one broker dies. `RF - 1` is the balance point. |
| Producer `acks` | `all` (client) | scalo enables the idempotent producer by default, which forces `acks=all`. Already aligned; no broker-side action. |
| `unclean.leader.election.enable` | `false` | Consistency over availability -- a data-forensics engine must never surface a leader that lost committed writes. Kafka 4.x already defaults this false; pinned so the contract is auditable. |
| `num.partitions` (default) | 12 | Highly divisible (2/3/4/6/12) for balanced leadership as brokers grow, with consumer-group parallelism headroom. Keyed topics cannot be repartitioned later without breaking `hash(key) % partitions`, so a modest over-provision is deliberate. KRaft removed the old 200k/4k partition ceilings. |
| `compression.type` | `producer` | Store exactly the codec the client sent -- the broker never decompress-recompresses on the ingest hot path. scalo's client sizing profile owns the codec (lz4 default; zstd opt-in for storage-bound topics). A fixed broker codec that differs from the producer's forces re-compression on every produce. |
| `log.retention.hours` | 72 | Time-based, comfortably above the max expected consumer downtime. DFE also archives to object storage via dfe-archiver, so time-retention is the durable, portable default. |
| `log.retention.bytes` | unset (-1) | Size-based retention is PVC/tier-specific and can delete un-consumed forensic data early during a traffic spike. Set it per deploy to match `storage.size`; never a universal value. |
| `log.segment.bytes` | 512 MiB | Finer retention granularity than the 1 GiB default (a segment is only deletable once closed) while keeping open-file counts sane. Raise toward 1 GiB on very-high-partition-density brokers. |
| `cleanup.policy` | `delete` (default) | DFE topics are append-only event streams, not compacted key/state topics. Left at the broker default; not set. |
| `message.max.bytes` | ~1 MiB (Kafka default) | Aligned with the scalo client contract: producer `batch.size` 128 KiB < broker `message.max.bytes` 1 MiB <= the SMALLEST client per-partition fetch budget (low-latency `max.partition.fetch.bytes` = 1 MiB). See the alignment note below. |

### message.max.bytes -- the three-way alignment

`message.max.bytes` (broker) / `max.message.bytes` (topic) holds the largest
record batch AFTER compression. It must line up across four layers or you get
producer rejects or consumer stalls:

```
producer max.request.size / batch.size   (scalo throughput: batch 128 KiB)
  <=  broker  message.max.bytes           (~1 MiB default)
  ==  topic   max.message.bytes           (inherits the broker default)
  <=  broker  replica.fetch.max.bytes     (~1 MiB default -- replication)
  <=  consumer max.partition.fetch.bytes  (scalo: low_latency 1 MiB, balanced 5 MiB, throughput 10 MiB)
      consumer fetch.max.bytes            (scalo: 10-100 MiB, must exceed the per-partition value)
```

DFE leaves all of this at the Kafka default because the smallest scalo consumer
profile (low-latency) fetches only 1 MiB per partition, which equals the broker
message ceiling -- so a producer can never write a message a consumer cannot
read. If a deployment genuinely needs larger messages, raise ALL layers together
(broker `message.max.bytes` + topic `max.message.bytes` + broker
`replica.fetch.max.bytes` + client `max.partition.fetch.bytes` and
`fetch.max.bytes`). Prefer reference-based messaging (a pointer to object
storage) for occasional large blobs.

## Settable vs locked, per platform

"Settable" = you can set it to the DFE default. "Locked" = the platform fixes or
hides it (the DFE default is either already met or cannot be applied -- called
out in the guide). "Per-topic" = not a broker default; set it on each topic.

| Setting | Self-hosted (Strimzi / Redpanda, DFE-deployed) | MSK Provisioned | MSK Serverless | Confluent Cloud | Redpanda Cloud |
|---|---|---|---|---|---|
| Auth | SCRAM-SHA-512 | SASL/SCRAM-512 (Secrets Manager) | IAM only (OAUTHBEARER, port 9098) | API-key PLAIN over TLS | SCRAM-SHA-512 |
| `replication.factor` | Settable (3) | Locked -- forced to 3 (topic RF other than 3 is silently made 3) | Locked -- managed (2 hidden replicas, not counted, not settable) | Locked -- fixed 3 across 3 AZs | Settable per-topic (3) on Dedicated/BYOC; managed on Serverless |
| `min.insync.replicas` | Settable (2) | Broker-level settable; topic-level PROTECTED | Locked -- managed | Settable per-topic but ONLY 1 or 2 (use 2) | Per-topic settable; else Raft quorum |
| `unclean.leader.election.enable` | Settable (false) | Broker-level settable; topic-level PROTECTED | Locked -- managed | Locked -- managed (not exposed) | N/A -- Raft never elects a non-quorum leader |
| `num.partitions` default | Settable (12) | Broker default settable (12) | Locked -- no broker config; per-topic only; quota 2400 leader partitions/cluster | Per-topic (Terraform default 6; set 12); increase-only | Per-topic settable; Serverless auto-scales |
| `compression.type` | Settable (`producer`) | Broker settable (`producer`) | Per-topic (`compression.type`) | Per-topic settable | Per-topic (`producer` default) |
| `log.retention.hours/ms` | Settable (72h) | Broker settable (72h) | Per-topic `retention.ms`; 250 GB/partition hard cap | Per-topic `retention.ms` | Per-topic `retention.ms`; cluster `log_retention_ms` on Dedicated |
| `log.retention.bytes` | Settable (unset) | Broker settable | Per-topic `retention.bytes` | Per-topic `retention.bytes` | Per-topic `retention.bytes` |
| `log.segment.bytes` | Settable (512 MiB) | Broker settable | Locked -- managed | Locked -- not exposed | Cluster `log_segment_size` on Dedicated; managed on Serverless |
| `message.max.bytes` | Settable (default) | Broker settable | Per-topic `max.message.bytes` | Per-topic `max.message.bytes` | Per-topic `max.message.bytes` |

The pattern: **the more managed the platform, the fewer broker knobs it exposes,
and the more the DFE default is either already enforced (RF3, KRaft) or must be
expressed per-topic instead of as a broker default.** MSK Provisioned is the
closest to self-hosted (full `server.properties`); MSK Serverless and Confluent
Cloud hide almost all broker config; Redpanda Cloud sits in between (per-topic
control, cluster config on the Dedicated tier only).

## Storage model: `local` or `tiered-object`

`kafka.storageModel` is fixed for the life of the deployment and defaults to
`local`, where every segment stays on the PVC. The name is `<family>-<bulk>`:
data MOVES to the bulk store, and that store is an object store. (Previously
`tiered`.) Full matrix: [../storage.md](../storage.md).

`tiered-object` is KIP-405 tiered storage: closed segments move to object
storage, so the PVC sizes the hot window only. It renders
`spec.kafka.tieredStorage`, the broker-wide
`remote.log.storage.system.enable`, and the per-topic `remote.storage.enable`
on the landing topic (DLQ topics stay local).

Three constraints, all enforced at render:

- **Cluster mode, strimzi provider** -- `tieredStorage` is a Strimzi CR field
  and the single tier runs no operator.
- **Strimzi >= 0.38.0** -- `kafka.operatorVersion` mirrors the `versions.yaml`
  pin (`scripts/check_versions_drift.py` holds the two together). Strimzi drops
  unknown CR fields silently, so an older pin fails the render instead.
- **A RemoteStorageManager plugin** -- Strimzi supports `type: custom` only and
  ships no implementation, so `kafka.tieredObject.className` must name a class
  the broker image carries, on `kafka.tieredObject.classPath`.

Plugin settings go in `kafka.tieredObject.config` verbatim; credentials do not.
The chart wires `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` into the broker
from a Secret ESO materialises from `<project>/<env>/kafka/tiered`, bound by
`kafka.objectStore.*`.

Changing the model on a live cluster strands the segments already written, so
the engine holds `kafka.storageModel`, `kafka.tieredObject.*` and
`kafka.objectStore.*` as protected vars.

## Wiring a managed cluster back to DFE

1. Stand up the cluster with the guide's Terraform snippet (the right auth for
   the platform, plus whatever DFE defaults that platform lets you set).
2. Point DFE at it: `kafka.mode=external`, `kafka.external.bootstrap=<endpoint>`,
   and set the provider identity so scalo derives the correct
   `security_protocol` + `sasl_mechanism` (never hand-set). The chart env var is
   `KAFKA_PROVIDER` (helper `dfe-common.kafkaProviderEnv`); the engine-side name
   is `DFE_KAFKA_PROVIDER`. Values: `msk`, `msk_iam`, `confluent-cloud`,
   `redpanda-cloud`.
3. Get the client config for that provider from the CLI:
   `dfe kafka client-config --provider <msk|msk_iam|confluent-cloud|redpanda-cloud>`.
   It emits the derived transport settings from the canonical provider table
   (scalo `providers.rs`), so the client never hand-picks the mechanism.
4. Supply credentials via the external-secret seam (`kafka.external.auth`): a
   Vault-backed SCRAM/PLAIN secret, or -- for MSK Serverless -- no static secret
   at all (IAM via IRSA / workload identity; the consumer ServiceAccounts carry
   the role).

## Version pinning note

The Terraform snippets in these guides target external providers DFE does NOT
vendor. Versions cited (hashicorp/aws 6.54.0 verified 2026-07-08;
confluentinc/confluent ~> 2.x; redpanda-data/redpanda ~> 1.0; Mongey/kafka
0.13.1) were the current lines at authoring time. Per the DFE dependency rules
(7-day cooldown, prefer the LTS/stable line, then pin), VERIFY the current
version and PIN the exact one in your own root module before applying. These are
templates, not a committed root module -- treat the version constraints as a
floor to be pinned, not a blessed pin.

## Sources (2026 research)

- Apache Kafka 4.0 KRaft / ZooKeeper removal: [Kafka 4.0 announcement](https://kafka.apache.org/blog/2025/03/18/apache-kafka-4.0.0-release-announcement/), [Confluent 4.0 blog](https://www.confluent.io/blog/latest-apache-kafka-release/).
- Durability (RF/ISR/acks/unclean): [Kafka replication design](https://docs.confluent.io/kafka/design/replication.html), [oneuptime replication + ISR](https://oneuptime.com/blog/post/2026-01-21-kafka-replication-isr/view).
- Partition sizing (KRaft era): [Confluent partition determination](https://docs.confluent.io/kafka/operations-tools/partition-determination.html), [Instaclustr KRaft max partitions](https://www.instaclustr.com/blog/apache-kafka-kraft-abandons-the-zookeeper-part-3-maximum-partitions-and-conclusions/).
- Retention + segment sizing: [Strimzi storage internals](https://strimzi.io/blog/2021/12/17/kafka-segment-retention/), [oneuptime retention policies](https://oneuptime.com/blog/post/2026-01-24-kafka-log-retention-policies/view).
- Compression (`producer` / zstd / lz4): [Confluent compression blog](https://www.confluent.io/blog/apache-kafka-message-compression/), [Conduktor compression](https://docs.conduktor.io/learn/advanced/producers/compression).
- message.max.bytes alignment: [Confluent message size limit](https://www.confluent.io/learn/kafka-message-size-limit/), [Kafka consumer configs](https://kafka.apache.org/41/configuration/consumer-configs/).
