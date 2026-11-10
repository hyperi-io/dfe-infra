# Redpanda as a DFE Kafka provider -- limitations and when to use it

DFE deploys two Kafka providers itself, in-cluster: Strimzi (the default) and Redpanda (opt-in). Three more are managed clouds it doesn't deploy, `msk`, `confluent-cloud` and `redpanda-cloud`, and the neighbouring guides in this directory cover them. This page is only about Redpanda as the in-cluster operator.

Redpanda is a Kafka-API-compatible broker with no JVM, no ZooKeeper/KRaft sidecar and a built-in schema registry. Its licence (BSL) limits how you can host it, and a few Kafka features differ (sections 1 and 2). Redpanda is off unless `kafka.redpanda.acceptLicense=true` is set, and the chart fails otherwise.

In short:

- Default to Strimzi (Apache-2.0, CNCF).
- Choose Redpanda for self-hosted single, small and test deployments where you want no JVM, with `acceptLicense=true`, once you've read the BSL constraint below.
- Don't bundle Redpanda into a HyperI-hosted multi-tenant SaaS without legal sign-off.

## 1. Licensing

Redpanda Community Edition is licensed under the Business Source License (BSL). That's source-available, not open source. Sources: Redpanda licensing docs and the BSL announcement.

- The core restriction is that you may not offer Redpanda as a "Streaming or Queuing Service" to third parties, meaning a commercial service where third parties cause topic creation. The BSL code converts to Apache-2.0 four years after each merge.
- Internal, self-hosted production use is permitted.

What that means for DFE:

- A customer self-hosting DFE with bundled Redpanda is fine, because that's internal use.
- A HyperI-hosted, multi-tenant DFE SaaS that bundles Redpanda and lets tenants create topics likely breaches the BSL clause. Use Strimzi (Apache-2.0) for the hosted offering, or get a Redpanda Enterprise licence or legal sign-off.
- Enterprise-only features (Tiered Storage, remote read replicas, continuous data balancing, Console SSO) need a paid licence (RCL). DFE uses none of them. It does its own S3 archival through `dfe-archiver`.

## 2. Kafka-API compatibility caveats

Redpanda implements the Kafka wire protocol (clients 0.11 and later), so DFE's produce, consume and consumer-group usage works without code changes. These are the known gaps:

| Caveat | Relevant to DFE? |
|--------|------------------|
| Kafka Streams unsupported | No. DFE uses its own Rust transforms, not Kafka Streams |
| Transactions: server-side KIP-890 absent (4.x clients fall back to the old protocol) | Only if DFE relies on exactly-once Kafka transactions. Confirm in the loader (DFE is at-least-once) |
| One SASL/SCRAM mechanism per user (no simultaneous 256 and 512) | No. DFE uses SCRAM-SHA-512 only |
| HTTP Proxy can't do topic/ACL admin CRUD | No. DFE creates topics through the admin API, not pandaproxy |
| No live KRaft to Redpanda metadata migration | No. It's greenfield |
| May lag newest Kafka KIPs by months | Low risk for basic produce/consume |

Each node has a built-in Schema Registry and HTTP proxy, with no JVM or ZooKeeper.

## 3. Deployment status in DFE charts

The Redpanda operator (`Redpanda` and `User` CRDs, `cluster.redpanda.com/v1alpha2`) deploys Redpanda. It's the same operator-plus-thin-CR model DFE uses for Strimzi. The operator installs only on an explicit opt-in. The `dfe-redpanda-operator` ApplicationSet keys on the cluster label `dfe.hyperi.io/kafka-provider: redpanda`, so the BSL stays off any cluster that hasn't chosen it. The pins are in `versions.yaml`: `redpanda-operator` 26.2.3 and broker `redpanda-version` v26.2.2.

- Auth is SASL/SCRAM-SHA-512, on from first boot (the DFE Kafka standard). TLS is off on the internal listener, matching Strimzi's plain SCRAM listener, and the Kafka API is on 9093. The operator generates and manages the bootstrap superuser. The `dfe-kafka-user` `User` CR reads its password from the store-backed `dfe-kafka-user-store` Secret that the Strimzi `KafkaUser` also reads. That Secret carries `password` alone, so the chart writes `username`, `password` and `sasl.mechanism` into `dfe-kafka-user` in the app namespaces, as it does for Strimzi. With `user.password.fromSecretsStore=false` the operator generates the password into a broker-namespace Secret named after the user, and the chart merges the other two keys into it.
- `single` (small/test) is a one-broker `Redpanda` CR. It's live-validated end to end on the on-prem reference cluster (SCRAM produce to consume through the matrix harness).
- `cluster` is a multi-broker `Redpanda` CR (`statefulset.replicas`). The operator owns Raft and seed bootstrap, so there's no hand-rolled `seed_servers`. Validate at the target scale before you rely on it. The secrets config must set `kafka_provider=redpanda`, or the store path `kafka/redpanda` is never seeded and `dfe-kafka-user-store` never syncs. `bootstrapServers` in `profile-scale.yaml` names Strimzi's Service, so override it with the Redpanda cluster's in your own values.
- Known operator bug, with a workaround in place: the `User` controller's `clusterRef` SRV discovery fails when SASL is enabled (redpanda-operator #1130, "Operator fails to load credentials from clusterRef when SASL is enabled"). So the `User` CR uses `spec.cluster.staticConfiguration` instead of `clusterRef`. That gives explicit kafka and admin endpoints and authenticates as the bootstrap superuser. Revert to `clusterRef` once the upstream bug is fixed.
- MSK and external don't apply to Redpanda. Use `kafka.mode=external` with a Redpanda Cloud endpoint, or MSK with the Strimzi/external path.
