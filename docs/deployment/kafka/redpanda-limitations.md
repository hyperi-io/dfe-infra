# Redpanda as a DFE Kafka provider -- limitations and when to use it

DFE deploys two Kafka providers itself, in-cluster: **Strimzi** (default) and
**Redpanda** (opt-in). Three more are managed clouds it does not deploy --
`msk`, `confluent-cloud`, `redpanda-cloud` -- covered in the neighbouring
guides in this directory. This page is Redpanda-the-in-cluster-operator only.
Redpanda is a Kafka-API-compatible broker with a much
smaller footprint (no JVM, no ZooKeeper/KRaft sidecar, built-in schema registry).
It is a strong choice for `single`/small/test and a lean option at scale -- but it
carries constraints that must be a conscious decision. Redpanda is **off unless**
`kafka.redpanda.acceptLicense=true` is set (the chart fails otherwise).

## 1. Licensing (the gating constraint)

Redpanda Community Edition is licensed under the **Business Source License
(BSL)** -- source-available, **not** open source. Sources: Redpanda licensing
docs and the BSL announcement.

- **Core restriction:** you may not offer Redpanda as a **"Streaming or Queuing
  Service"** to third parties (a commercial service where third parties cause
  topic creation). The BSL code converts to Apache-2.0 four years after each
  merge.
- **Internal / self-hosted production use is permitted.**

**What this means for DFE:**
- A **customer self-hosting DFE** with bundled Redpanda -- **fine** (internal use).
- A **HyperI-hosted, multi-tenant DFE SaaS** that bundles Redpanda and lets
  tenants create topics -- **likely breaches the BSL clause.** Use **Strimzi**
  (Apache-2.0) for the hosted offering, or obtain a Redpanda Enterprise licence /
  legal sign-off.
- Enterprise-only features (Tiered Storage, remote read replicas, continuous
  data balancing, Console SSO) require a paid licence (RCL). **DFE uses none of
  these** -- it does its own S3 archival via `dfe-archiver`.

## 2. Kafka-API compatibility caveats (vs DFE's usage)

Redpanda implements the Kafka wire protocol (clients >= 0.11), so DFE's
produce/consume/consumer-group usage works without code changes. Known gaps:

| Caveat | Relevant to DFE? |
|--------|------------------|
| **Kafka Streams unsupported** | No -- DFE uses its own Rust transforms, not Kafka Streams |
| Transactions: server-side KIP-890 absent (4.x clients fall back to old protocol) | Only if DFE relies on exactly-once Kafka transactions; confirm in the loader (DFE is at-least-once) |
| **One SASL/SCRAM mechanism per user** (no simultaneous 256+512) | No -- DFE uses SCRAM-SHA-512 only |
| HTTP Proxy cannot do topic/ACL admin CRUD | No -- DFE creates topics via the admin API, not pandaproxy |
| No live KRaft<->Redpanda metadata migration | No -- greenfield |
| May lag newest Kafka KIPs by months | Low risk for basic produce/consume |

**Upside:** built-in Schema Registry + HTTP proxy per node, no JVM/ZK -- smaller
and simpler than Strimzi for single/small deployments.

## 3. Deployment status in DFE charts

Redpanda is deployed via the **Redpanda operator** (`Redpanda` + `User` CRDs,
`cluster.redpanda.com/v1alpha2`) -- the same operator+thin-CR model DFE uses for
Strimzi. The operator is installed only on an explicit opt-in: the
`dfe-redpanda-operator` ApplicationSet keys on the cluster label
`dfe.hyperi.io/kafka-provider: redpanda` (BSL stays off any cluster that has not
chosen it). Pinned in `versions.yaml` (`redpanda-operator` 26.2.3, broker
`redpanda-version` v26.2.2).

- **Auth: SASL/SCRAM-SHA-512, on from first boot** (the DFE kafka standard), TLS
  off on the internal listener -- matching Strimzi's plain SCRAM listener. The
  operator generates+manages the bootstrap superuser; the `dfe-kafka-user` service
  account is a `User` CR whose operator-generated Secret has the **same shape as
  the Strimzi-minted secret**, so clients authenticate identically across
  providers. Kafka API is on **9093** (TLS-off internal listener).
- **single** (small/test): one-broker `Redpanda` CR. Live-validated end to end
  on the on-prem reference cluster (SCRAM produce->consume via the matrix
  harness).
- **cluster**: multi-broker `Redpanda` CR (`statefulset.replicas`); the operator
  owns Raft/seed bootstrap (no hand-rolled `seed_servers`). Validate at the target
  scale before relying on it.
- **Known operator bug (workaround in place):** the `User` controller's
  `clusterRef` SRV discovery fails when SASL is enabled (redpanda-operator #1130,
  "Operator fails to load credentials from clusterRef when SASL is enabled"). The
  `User` CR therefore uses `spec.cluster.staticConfiguration` (explicit kafka +
  admin endpoints, authenticating as the bootstrap superuser) instead of
  `clusterRef`. Revert to `clusterRef` once the upstream bug is fixed.
- **MSK / external**: not applicable to Redpanda -- use `kafka.mode=external` with
  a Redpanda Cloud endpoint, or MSK with the Strimzi/external path.

## 4. Decision summary

- Default to **Strimzi** (Apache-2.0, CNCF, mature, fully cluster-validated here).
- Choose **Redpanda** for footprint-sensitive **self-hosted** single/small/test,
  with `acceptLicense=true`, having read the BSL constraint above.
- **Do not bundle Redpanda into a HyperI-hosted multi-tenant SaaS** without legal
  sign-off.
