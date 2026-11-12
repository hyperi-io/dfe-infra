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

DFE doesn't deploy Redpanda Cloud. You provision it, then point DFE at it with `kafka.mode=external`. Redpanda Cloud is the DFE provider `redpanda-cloud`, with SASL_SSL and SCRAM-SHA-512 auth. Redpanda Cloud supports both SCRAM-256 and 512, and DFE mandates 512. Redpanda is Raft-native (no ZooKeeper, no KRaft sidecar) and ships a built-in schema registry.

Read the baseline in [README.md](README.md) first. Dedicated and BYOC let you set topic RF and partitions and accept cluster-level config (see Tiers). Serverless manages capacity.

This is Redpanda's MANAGED cloud, which isn't the self-hosted Redpanda DFE can deploy in-cluster (`kafka.provider=redpanda`, BSL gate). `docs/deployment/kafka/redpanda-limitations.md` has the self-hosted licensing story. Redpanda Cloud is a paid managed service, so the BSL "streaming service" clause is Redpanda's concern and not the deployer's.

## Tiers

- Serverless is elastic and its capacity is fully managed. Topic RF and partitions are managed, and you set per-topic retention, compression and cleanup.
- Dedicated is a managed cluster on Redpanda's cloud account. Topic RF and partitions are settable, and cluster-level config (`log_segment_size`, `log_retention_ms`, default topic properties) is available.
- BYOC (Bring Your Own Cloud) is the same, provisioned into YOUR cloud account and VPC. It has the same config surface as Dedicated.

## What Redpanda Cloud lets you set vs manages

| DFE default | Serverless | Dedicated / BYOC |
|---|---|---|
| Raft metadata (no ZooKeeper) | Managed | Managed |
| `replication.factor=3` | Managed (topic RF fixed by tier) | Settable per topic (`replication_factor=3`) |
| `min.insync.replicas=2` | Per-topic where supported, else Raft quorum | Per-topic where supported, else Raft quorum (majority ack) |
| `unclean.leader.election` off | N/A. Raft never elects a non-quorum leader | N/A (Raft) |
| `num.partitions=12` | Per-topic (`partition_count`), auto-scales | Per-topic (`partition_count=12`) |
| `compression.type=producer` | Per-topic (`compression.type`), and Redpanda defaults to `producer` | Per-topic, default `producer` |
| `log.retention.hours=72` | Per-topic `retention.ms` | Per-topic `retention.ms`, or cluster `log_retention_ms` |
| `log.retention.bytes` (unset) | Per-topic `retention.bytes` | Per-topic `retention.bytes`, or cluster `retention_bytes` |
| `log.segment.bytes=512 MiB` | Managed | Cluster `log_segment_size` (Redpanda property name) |
| `cleanup.policy=delete` | Per-topic (default `delete`) | Per-topic (default `delete`) |
| `message.max.bytes` (default) | Per-topic `max.message.bytes` | Per-topic `max.message.bytes` |

Redpanda's Raft consensus makes `unclean.leader.election` moot, because a leader always holds a committed quorum. It gives you the RF3 durability equivalent through majority acknowledgement when the client sends `acks=all` (the scalo default). Set `min.insync.replicas` per topic if you want a stricter explicit floor.

## OpenTofu: Redpanda Cloud (SCRAM-SHA-512)

The `redpanda` provider authenticates with a Redpanda Cloud service-account client ID and secret (Organization IAM). There are two shapes below, Serverless (fewest dials) and Dedicated/BYOC (full topic and cluster config). The SCRAM user is a `redpanda_user` with `mechanism = "scram-sha-512"` and its ACLs, against the cluster's Data Plane API URL (`dataplane_api.url`). There's no topic resource, because dfe-engine creates every topic.

```hcl
terraform {
  required_providers {
    redpanda = {
      source  = "redpanda-data/redpanda"
      version = "~> 2.3" # the line managed-kafka/redpanda-cloud pins
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

# The Data Plane API URL the user and ACL resources talk to.
locals {
  cluster_api_url = redpanda_serverless_cluster.dfe.dataplane_api.url
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
  cluster_api_url = local.cluster_api_url
}

# --- The user's grants, as managed-kafka/redpanda-cloud/acls.tf ---
# Topic * with every call dfe-engine makes, since it creates every topic at
# startup, and consumer groups fenced to the dfe- prefix. No topic resource.
resource "redpanda_acl" "dfe_topics" {
  for_each              = toset(["READ", "WRITE", "CREATE", "DESCRIBE", "DELETE", "ALTER", "DESCRIBE_CONFIGS", "ALTER_CONFIGS"])
  resource_type         = "TOPIC"
  resource_name         = "*"
  resource_pattern_type = "LITERAL"
  principal             = "User:${redpanda_user.dfe_app.name}"
  host                  = "*"
  operation             = each.value
  permission_type       = "ALLOW"
  cluster_api_url       = local.cluster_api_url
}

resource "redpanda_acl" "dfe_groups" {
  for_each              = toset(["READ", "DESCRIBE"])
  resource_type         = "GROUP"
  resource_name         = "dfe-"
  resource_pattern_type = "PREFIXED"
  principal             = "User:${redpanda_user.dfe_app.name}"
  host                  = "*"
  operation             = each.value
  permission_type       = "ALLOW"
  cluster_api_url       = local.cluster_api_url
}

output "cluster_api_url" {
  value = local.cluster_api_url
}
```

For the Dedicated / BYOC tier, point the local at `redpanda_cluster.dfe.cluster_api_url`. Then set cluster-level defaults (`log_segment_size = 536870912`, `log_retention_ms = 259200000`, `default_topic_partitions = 12`, `default_topic_replications = 3`) through the cluster config surface. They're the same Redpanda property names DFE's own self-hosted Redpanda CR uses (`helm/charts/kafka/templates/redpanda.yaml`).

## Wiring back to DFE

- `kafka.mode=external`, `kafka.external.bootstrap=<the SASL bootstrap host:port>`. That's the seed brokers. It isn't the HTTP `cluster_api_url`, which is the admin/REST plane the Terraform provider uses. The client bootstrap is the Kafka SASL endpoint from the cluster's connectivity details.
- `kafka.securityProtocol=SASL_SSL`, the one dial every app chart and dfe-engine read their protocol from. Redpanda Cloud serves SCRAM-SHA-512 over TLS only. On the aws root you set none of the mode, the endpoint or this by hand. `DFE_KAFKA_PROVIDER=redpanda-cloud` reaches bootstrap, which puts all three on the Argo cluster secret for the layer 2 appsets to hand on.
- Credentials: the `redpanda_user` name (username) and password go in the Vault-backed external secret, with `kafka.external.auth.type=scram`.
- Client config: `dfe kafka client-config --provider redpanda-cloud`.

## Verification: what is and isn't confirmed

Confirmed by 2026 research (sources in the README):

- The `redpanda` provider is GA.
- `redpanda_serverless_cluster` (name, resource_group_id, serverless_region), `redpanda_cluster` (Dedicated/BYOC), `redpanda_topic` (partition_count, replication_factor, a `configuration` map with `cleanup.policy`, `retention.ms` and so on) and `redpanda_user` with `mechanism = "scram-sha-512"` are all current.
- The Redpanda cluster property names `log_segment_size`, `log_retention_ms`, `default_topic_partitions` and `default_topic_replications` are confirmed.
- The DFE `redpanda-cloud` provider is SASL_SSL SCRAM-SHA-512 (scalo `providers.rs`).

NOT verified, because we have no Redpanda Cloud account here and can't deploy:

- The exact provider version to pin.
- The precise `serverless_region`, `throughput_tier` and `zones` strings for your account.
- Whether Serverless honours per-topic `replication_factor`. It may manage RF and ignore the field.
- The exact Kafka SASL bootstrap host to feed `kafka.external.bootstrap`. Read it from the cluster's connectivity details after apply.

Run `tofu validate` and plan against your account before you apply.
