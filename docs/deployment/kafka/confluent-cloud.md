<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/deployment/kafka/confluent-cloud.md
Purpose:   Config guide for running DFE against Confluent Cloud (API-key PLAIN
           over TLS). States DFE's defaults, what Confluent Cloud lets you set
           vs manages, and a working OpenTofu / Terraform template.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# DFE on Confluent Cloud

DFE does NOT auto-deploy Confluent Cloud -- you provision it, then point DFE at
it with `kafka.mode=external`. Confluent Cloud is the DFE provider
`confluent-cloud`, and it is the ONE built-in provider that does NOT do SCRAM:
auth is **SASL_SSL with PLAIN** using a Confluent **API key/secret** pair. The
DFE security floor still holds -- PLAIN is only ever sent over TLS, enforced by
scalo `providers::validate` and `KafkaConfig::validate`.

Read the opinionated baseline in [README.md](README.md) first. Confluent Cloud
is the most managed of the three clouds: it exposes very few broker dials and
expresses the rest per topic.

**On the AWS path, tofu builds this for you.** Setting `kafka.provider: confluent-cloud` in the deployment dial makes `terraform/environments/aws` call the maintained, contract-tested `terraform/modules/managed-kafka/confluent-cloud` module instead of hand-rolling the snippet below -- see [aws-configuration.md](../aws-configuration.md#kafka-and-clickhouse) and that module's `CONTRACT.md`. That module defaults to the **Freight** tier (private networking, no public endpoint, autoscales on eCKUs) rather than the `standard` tier this page's own worked example uses. Confluent caps a topic's `max.message.bytes` by cluster type: 8,388,608 on Basic and Standard, 20,971,520 on Enterprise and Dedicated, and 20 MB on Freight. So Freight and Enterprise carry DFE's 16 MiB size chain and Basic and Standard do not. The example below is for a deployment standing Confluent Cloud up by some other means and pointing DFE at it with `kafka.mode=external`.

## What Confluent Cloud lets you set vs manages

Confluent Cloud manages the broker fleet, KRaft metadata plane, segment sizing,
and leader election. You express durability and retention PER TOPIC, within a
narrow allowed range.

| DFE default | On Confluent Cloud |
|---|---|
| KRaft (no ZooKeeper) | Managed -- Confluent runs KRaft; nothing to set. |
| `replication.factor=3` | LOCKED -- fixed at 3 across three availability zones, platform-wide. You cannot set it; it already matches the DFE default. |
| `min.insync.replicas=2` | Settable PER TOPIC, but only 1 or 2. Set 2 (the DFE default). |
| `unclean.leader.election.enable=false` | Managed -- not exposed. Confluent does not do unclean election; the DFE intent already holds. |
| `num.partitions=12` | PER TOPIC (`partitions_count`; the Terraform default is 6 -- set 12). Increase-only; you cannot shrink later. |
| `compression.type=producer` | Settable per topic. |
| `log.retention.hours=72` | PER TOPIC as `retention.ms` (72h = 259200000). |
| `log.retention.bytes` (unset) | PER TOPIC as `retention.bytes` (leave unset for time-only). |
| `log.segment.bytes=512 MiB` | LOCKED -- not exposed (Confluent manages segments). |
| `cleanup.policy=delete` | Settable per topic (default `delete`). |
| `message.max.bytes` (default) | PER TOPIC as `max.message.bytes` (default 2,097,164; at most 8,388,608 on Basic and Standard, 20,971,520 on Enterprise and Dedicated, 20 MB on Freight -- keep aligned with the scalo client budgets). |
| App record limits (16 MiB producers, 8,000,000-byte receiver request body) | Producers follow the size bootstrap writes on the cluster secret from `DFE_KAFKA_MESSAGE_MAX_BYTES`, which the aws root sets: the receiver, loader, fetcher, archiver and transform producers take it as `message.max.bytes`, and dfe-transform-elastic drops a record over 15/16 of it. The receiver refuses a request body over the DFE-wide 8,000,000-byte cap on every tier, which fits Basic's 8,388,608 with room for the message framing. dfe-fetcher and dfe-archiver read it from their first release after v1.4.36 and v1.7.32; an earlier image ignores it and keeps 16 MiB. |

Net: on Confluent Cloud the DFE opinion reduces to a per-topic config of
`min.insync.replicas=2`, `partitions=12`, `retention.ms=259200000`,
`compression.type=producer`, `cleanup.policy=delete`, `max.message.bytes` at the
default. RF3, KRaft, unclean-election-off, and segment sizing are all handled by
the platform (and already match the DFE intent). Note that some topic configs are
not editable after create. dfe-engine creates every topic at startup, so they
are set at creation from its settings (see Wiring back to DFE).

## Terraform / OpenTofu -- Confluent Cloud (API-key PLAIN)

Standard cluster (multi-AZ, RF3) with the DFE service account, its
cluster-scoped API key, and the ACLs a manager account writes for it. The
`confluent` provider needs a CLOUD API key (org-level, for management) and DFE a
KAFKA API key (cluster-level, for the data plane) -- both are shown. There is no
topic resource: dfe-engine creates every topic.

```hcl
terraform {
  required_providers {
    confluent = {
      source  = "confluentinc/confluent"
      version = "~> 2.0" # VERIFY latest + PIN exact (~> 2.73 seen early 2026); see README pinning note.
    }
  }
}

# Org-level (management plane) credentials.
provider "confluent" {
  cloud_api_key    = var.confluent_cloud_api_key
  cloud_api_secret = var.confluent_cloud_api_secret
}

resource "confluent_environment" "dfe" {
  display_name = "dfe"
}

resource "confluent_kafka_cluster" "dfe" {
  display_name = "dfe-kafka"
  availability = "MULTI_ZONE" # RF3 across 3 AZs (platform-fixed)
  cloud        = "AWS"
  region       = var.confluent_region
  standard {}                 # Standard tier: multi-zone, per-topic config

  environment { id = confluent_environment.dfe.id }
}

# --- Service account + cluster-scoped API key for the DFE data plane ---
resource "confluent_service_account" "dfe_app" {
  display_name = "dfe-app"
  description  = "DFE producer/consumer service account"
}

resource "confluent_api_key" "dfe_app_kafka" {
  display_name = "dfe-app-kafka-key"
  owner {
    id          = confluent_service_account.dfe_app.id
    api_version = confluent_service_account.dfe_app.api_version
    kind        = confluent_service_account.dfe_app.kind
  }
  managed_resource {
    id          = confluent_kafka_cluster.dfe.id
    api_version = confluent_kafka_cluster.dfe.api_version
    kind        = confluent_kafka_cluster.dfe.kind
    environment { id = confluent_environment.dfe.id }
  }
}

# --- A manager account writes the ACLs, and DFE never runs as it ---
resource "confluent_service_account" "dfe_manager" {
  display_name = "dfe-manager"
}

resource "confluent_role_binding" "dfe_manager" {
  principal   = "User:${confluent_service_account.dfe_manager.id}"
  role_name   = "CloudClusterAdmin"
  crn_pattern = confluent_kafka_cluster.dfe.rbac_crn
}

resource "confluent_api_key" "dfe_manager_kafka" {
  display_name = "dfe-manager-kafka-key"
  depends_on   = [confluent_role_binding.dfe_manager]
  owner {
    id          = confluent_service_account.dfe_manager.id
    api_version = confluent_service_account.dfe_manager.api_version
    kind        = confluent_service_account.dfe_manager.kind
  }
  managed_resource {
    id          = confluent_kafka_cluster.dfe.id
    api_version = confluent_kafka_cluster.dfe.api_version
    kind        = confluent_kafka_cluster.dfe.kind
    environment { id = confluent_environment.dfe.id }
  }
}

# --- The DFE principal's grants, as managed-kafka/confluent-cloud/acls.tf ---
# Topic * with every call dfe-engine makes, since it creates every topic at
# startup, and consumer groups fenced to the dfe- prefix. No topic resource.
locals {
  dfe_acls = merge(
    { for op in ["READ", "WRITE", "CREATE", "DESCRIBE", "DELETE", "ALTER", "DESCRIBE_CONFIGS", "ALTER_CONFIGS"] :
    "topic-${lower(op)}" => { type = "TOPIC", name = "*", pattern = "LITERAL", op = op } },
    { for op in ["READ", "DESCRIBE"] :
    "group-${lower(op)}" => { type = "GROUP", name = "dfe-", pattern = "PREFIXED", op = op } },
  )
}

resource "confluent_kafka_acl" "dfe_app" {
  for_each = local.dfe_acls
  kafka_cluster { id = confluent_kafka_cluster.dfe.id }
  resource_type = each.value.type
  resource_name = each.value.name
  pattern_type  = each.value.pattern
  principal     = "User:${confluent_service_account.dfe_app.id}"
  host          = "*"
  operation     = each.value.op
  permission    = "ALLOW"
  rest_endpoint = confluent_kafka_cluster.dfe.rest_endpoint
  credentials {
    key    = confluent_api_key.dfe_manager_kafka.id
    secret = confluent_api_key.dfe_manager_kafka.secret
  }
}

output "bootstrap_endpoint" {
  value = confluent_kafka_cluster.dfe.bootstrap_endpoint
}
```

The API key `id` is the SASL username and its `secret` is the SASL password;
that pair is what DFE's `confluent-cloud` provider sends as PLAIN over TLS.

## Wiring back to DFE

- `kafka.mode=external`, `kafka.external.bootstrap=<bootstrap_endpoint>` (strip
  the `SASL_SSL://` scheme if your endpoint output includes it).
- `kafka.external.provider=confluent-cloud` -> the chart derives `SASL_SSL` +
  `PLAIN` and puts the mechanism in the credential Secret beside the API key, so
  that Secret carries the same `username` / `password` / `sasl.mechanism` shape
  a DFE-owned broker's does.
- `kafka.securityProtocol=SASL_SSL`, the one dial every app chart and dfe-engine read their protocol from. PLAIN never crosses a cleartext transport. On the aws root you set none of the mode, the endpoint or this by hand: `DFE_KAFKA_PROVIDER=confluent-cloud` reaches bootstrap, which puts all three on the Argo cluster secret for the layer 2 appsets to hand on.
- Credentials: put the Kafka API key (username) + secret (password) in the
  Vault-backed external secret; `kafka.external.auth.type=scram` still works as
  the user/password carrier (the mechanism itself is PLAIN, derived from the
  provider, not from `auth.type`).
- Client config: `dfe kafka client-config --provider confluent-cloud`.
- Topic size: set the dfe-engine chart's `kafka.messageMaxBytes` to the `max.message.bytes` your topics carry, within your cluster type's ceiling. Unset, the engine creates each source's topics at its own default size, which a cluster capped below it refuses. On the AWS path tofu sets it for you through `DFE_KAFKA_MESSAGE_MAX_BYTES`.
- Topic retention: set the dfe-engine chart's `kafka.topicRetentionMs`, or the topics keep Confluent's default. On the AWS path tofu sets it through `DFE_KAFKA_RETENTION_MS`.
- Replication: set the dfe-engine chart's `kafka.topicReplicationFactor` to 3. Confluent Cloud refuses any other factor and creates no topic on first write, so a source whose topics are refused never takes data. The layer 2 appsets set it for every external broker.

Note: Confluent Cloud auto-provisions a billable Flink compute pool on cluster
create (scalo `ProviderCapabilities.has_billable_side_resources`). If you tear a
cluster down, sweep that pool too or it keeps billing.

## Verification -- what is and is not confirmed

Confirmed by 2026 research (sources in README): RF is fixed at 3 platform-wide;
`min.insync.replicas` is settable per topic but only 1 or 2; `partitions_count`
defaults to 6 and is increase-only; topic configs `cleanup.policy`,
`retention.ms`, `retention.bytes`, `max.message.bytes`, `compression.type`,
`min.insync.replicas` are settable; segment sizing and leader election are not
exposed; the `confluent_kafka_cluster` / `confluent_kafka_topic` /
`confluent_api_key` / `confluent_service_account` resources and the CLOUD-key +
KAFKA-key split are current. The DFE `confluent-cloud` provider is SASL_SSL PLAIN
(scalo `providers.rs`, canonical table).

NOT verified (no Confluent Cloud account here, cannot deploy): the exact current
provider version to pin, the precise `region` string, whether the Standard tier
(vs Enterprise/Dedicated/Freight) meets your throughput, and that every topic
config key above is editable post-create on your tier (some are create-only --
Confluent's own docs warn config edits can fail after creation). Run
`tofu validate` and plan against your org before apply.
