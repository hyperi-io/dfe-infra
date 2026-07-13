<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/deployment/kafka/redpanda-cloud.md
Purpose:   Config guide for running DFE against Redpanda Cloud (Serverless /
           Dedicated / BYOC, SCRAM-SHA-512). States DFE's defaults, what the
           tier lets you set vs manages, and a working OpenTofu / Terraform
           template.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# DFE on Redpanda Cloud

DFE does NOT auto-deploy Redpanda Cloud -- you provision it, then point DFE at it
with `kafka.mode=external`. Redpanda Cloud is the DFE provider `redpanda-cloud`,
auth **SASL_SSL with SCRAM-SHA-512** (the DFE credential contract; Redpanda Cloud
supports both SCRAM-256 and 512, DFE mandates 512). Redpanda is Raft-native (no
ZooKeeper, no KRaft sidecar) and ships a built-in schema registry.

Read the opinionated baseline in [README.md](README.md) first. Redpanda Cloud
sits between MSK Provisioned and Confluent Cloud on how much it exposes: topic
RF and partitions are settable (unlike Confluent/MSK), and the Dedicated / BYOC
tiers accept cluster-level config; Serverless abstracts capacity away.

Note: this is Redpanda's MANAGED cloud, a different thing from the self-hosted
Redpanda that DFE CAN auto-deploy in-cluster (`kafka.provider=redpanda`, BSL
gate). See `docs/REDPANDA-LIMITATIONS.md` for the self-hosted licensing story;
Redpanda Cloud is a paid managed service, so the BSL "streaming service" clause
is Redpanda's concern, not the deployer's.

## Tiers

- **Serverless** -- fastest to stand up, elastic, capacity fully managed. Topic
  RF/partitions are managed; you set per-topic retention/compression/cleanup.
- **Dedicated** -- a managed cluster on Redpanda's cloud account. Topic RF and
  partitions are settable; cluster-level config (`log_segment_size`,
  `log_retention_ms`, default topic properties) is available.
- **BYOC** (Bring Your Own Cloud) -- the same, provisioned into YOUR cloud
  account/VPC. Same config surface as Dedicated.

## What Redpanda Cloud lets you set vs manages

| DFE default | Serverless | Dedicated / BYOC |
|---|---|---|
| Raft metadata (no ZooKeeper) | Managed | Managed |
| `replication.factor=3` | Managed (topic RF fixed by tier) | Settable per topic (`replication_factor=3`) |
| `min.insync.replicas=2` | Per-topic where supported; else Raft quorum | Per-topic where supported; else Raft quorum (majority ack) |
| `unclean.leader.election` off | N/A -- Raft never elects a non-quorum leader | N/A -- Raft |
| `num.partitions=12` | Per-topic (`partition_count`); auto-scales | Per-topic (`partition_count=12`) |
| `compression.type=producer` | Per-topic (`compression.type`); Redpanda defaults to `producer` | Per-topic; default `producer` |
| `log.retention.hours=72` | Per-topic `retention.ms` | Per-topic `retention.ms`, or cluster `log_retention_ms` |
| `log.retention.bytes` (unset) | Per-topic `retention.bytes` | Per-topic `retention.bytes`, or cluster `retention_bytes` |
| `log.segment.bytes=512 MiB` | Managed | Cluster `log_segment_size` (Redpanda property name) |
| `cleanup.policy=delete` | Per-topic (default `delete`) | Per-topic (default `delete`) |
| `message.max.bytes` (default) | Per-topic `max.message.bytes` | Per-topic `max.message.bytes` |

Redpanda's Raft consensus makes `unclean.leader.election` moot (a leader always
holds a committed quorum) and provides the RF3 durability equivalent via majority
acknowledgement when the client sends `acks=all` (scalo default). Set
`min.insync.replicas` per topic if you want a stricter explicit floor.

## Terraform / OpenTofu -- Redpanda Cloud (SCRAM-SHA-512)

The `redpanda` provider authenticates with a Redpanda Cloud service-account
client ID + secret (Organization IAM). Two shapes below: Serverless (fewest
knobs) and Dedicated/BYOC (full topic + cluster config). The topic resource
targets the cluster's `cluster_api_url`, and the SCRAM user is a `redpanda_user`
with `mechanism = "scram-sha-512"`.

```hcl
terraform {
  required_providers {
    redpanda = {
      source  = "redpanda-data/redpanda"
      version = "~> 1.0" # VERIFY latest + PIN exact; see README pinning note.
    }
  }
}

# Auth via env: REDPANDA_CLIENT_ID / REDPANDA_CLIENT_SECRET (service account).
provider "redpanda" {}

resource "redpanda_resource_group" "dfe" {
  name = "dfe"
}

# ---------- Option A: Serverless ----------
resource "redpanda_serverless_cluster" "dfe" {
  name              = "dfe-kafka-serverless"
  resource_group_id = redpanda_resource_group.dfe.id
  serverless_region = var.redpanda_serverless_region # e.g. "pro-us-east-1"
}

# ---------- Option B: Dedicated (managed on Redpanda's account) ----------
# resource "redpanda_network" "dfe" {
#   name              = "dfe"
#   resource_group_id = redpanda_resource_group.dfe.id
#   cloud_provider    = "aws"
#   region            = var.redpanda_region
#   cluster_type      = "dedicated"
#   cidr_block        = "10.20.0.0/20"
# }
# resource "redpanda_cluster" "dfe" {
#   name              = "dfe-kafka"
#   resource_group_id = redpanda_resource_group.dfe.id
#   network_id        = redpanda_network.dfe.id
#   cloud_provider    = "aws"
#   region            = var.redpanda_region
#   cluster_type      = "dedicated"
#   connection_type   = "public"
#   throughput_tier   = "tier-1-aws"
#   zones             = ["use1-az1", "use1-az2", "use1-az4"] # 3 AZs -> RF3
#   allow_deletion    = false
# }

# --- The DFE SCRAM-512 service account (SASL user) ---
resource "redpanda_user" "dfe_app" {
  name            = "dfe-kafka-user"
  password        = var.dfe_kafka_password
  mechanism       = "scram-sha-512" # DFE contract (provider default is scram-sha-256)
  cluster_api_url = redpanda_serverless_cluster.dfe.cluster_api_url
}

# --- A topic carrying the DFE defaults ---
resource "redpanda_topic" "example" {
  name               = "dfe-example"
  partition_count    = 12 # DFE default
  replication_factor = 3  # Dedicated/BYOC honour this; Serverless manages RF
  cluster_api_url    = redpanda_serverless_cluster.dfe.cluster_api_url

  configuration = {
    "compression.type"  = "producer"
    "cleanup.policy"    = "delete"
    "retention.ms"      = "259200000" # 72h
    "max.message.bytes" = "1048576"   # ~1 MiB, aligned with scalo client budgets
  }
}

output "cluster_api_url" {
  value = redpanda_serverless_cluster.dfe.cluster_api_url
}
```

For the Dedicated / BYOC tier, swap the `cluster_api_url` references to
`redpanda_cluster.dfe.cluster_api_url`, and set cluster-level defaults
(`log_segment_size = 536870912`, `log_retention_ms = 259200000`,
`default_topic_partitions = 12`, `default_topic_replications = 3`) via the
cluster config surface -- these are the same Redpanda property names DFE's own
self-hosted Redpanda CR uses (`helm/charts/kafka/templates/redpanda.yaml`).

## Wiring back to DFE

- `kafka.mode=external`, `kafka.external.bootstrap=<the SASL bootstrap host:port>`
  (the seed brokers, not the HTTP `cluster_api_url` -- that URL is the admin/REST
  plane the Terraform provider uses; the client bootstrap is the Kafka SASL
  endpoint from the cluster's connectivity details).
- `KAFKA_PROVIDER=redpanda-cloud` / `DFE_KAFKA_PROVIDER=redpanda-cloud`
  -> scalo derives `SASL_SSL` + `SCRAM-SHA-512` (never hand-set).
- Credentials: the `redpanda_user` name (username) + password go in the
  Vault-backed external secret; `kafka.external.auth.type=scram`.
- Client config: `dfe kafka client-config --provider redpanda-cloud`.

## Verification -- what is and is not confirmed

Confirmed by 2026 research (sources in README): the `redpanda` provider is GA;
`redpanda_serverless_cluster` (name + resource_group_id + serverless_region),
`redpanda_cluster` (Dedicated/BYOC), `redpanda_topic` (partition_count,
replication_factor, `configuration` map with `cleanup.policy` / `retention.ms` /
etc.), and `redpanda_user` with `mechanism = "scram-sha-512"` are all current.
Redpanda cluster property names (`log_segment_size`, `log_retention_ms`,
`default_topic_partitions`, `default_topic_replications`) are confirmed. The DFE
`redpanda-cloud` provider is SASL_SSL SCRAM-SHA-512 (scalo `providers.rs`).

NOT verified (no Redpanda Cloud account here, cannot deploy): the exact provider
version to pin, the precise `serverless_region` / `throughput_tier` / `zones`
strings for your account, whether Serverless honours per-topic
`replication_factor` (it may manage RF and ignore the field), and the exact
Kafka SASL bootstrap host to feed `kafka.external.bootstrap` (read it from the
cluster's connectivity details after apply). Run `tofu validate` and plan against
your account before apply.
