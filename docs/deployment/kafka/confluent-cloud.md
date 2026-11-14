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

DFE doesn't deploy Confluent Cloud. You provision it, then point DFE at it with `kafka.mode=external`. Confluent Cloud is the DFE provider `confluent-cloud`, and it's the ONE built-in provider that doesn't do SCRAM. Auth is SASL_SSL with PLAIN, using a Confluent API key and secret pair. The security floor still holds, because PLAIN only ever goes over TLS. scalo `providers::validate` and `KafkaConfig::validate` enforce that.

Read the baseline in [README.md](README.md) first. Confluent Cloud exposes very few broker dials. You set the rest per topic.

On the AWS path, tofu builds this for you. Set `kafka.provider: confluent-cloud` in the deployment dial and `terraform/environments/aws` calls the maintained, tested `terraform/modules/managed-kafka/confluent-cloud` module, so you skip the hand-rolled snippet below. See [aws-configuration.md](../aws-configuration.md#kafka-and-clickhouse) and that module's `CONTRACT.md`. The module defaults to the Freight tier (private networking, no public endpoint, autoscales on eCKUs). The worked example on this page uses `standard`. Confluent caps a topic's `max.message.bytes` by cluster type: 8,388,608 on Basic and Standard, 20,971,520 on Enterprise and Dedicated, and 20 MB on Freight. So Freight and Enterprise carry DFE's 16 MiB size chain and Basic and Standard don't. The example below is for a deployment that stands Confluent Cloud up some other way and points DFE at it with `kafka.mode=external`.

## What Confluent Cloud lets you set vs manages

Confluent Cloud manages the broker fleet, the KRaft metadata plane, segment sizing and leader election. You set durability and retention PER TOPIC, within a narrow range.

| DFE default | On Confluent Cloud |
|---|---|
| KRaft (no ZooKeeper) | Managed. Confluent runs KRaft and there's nothing to set. |
| `replication.factor=3` | LOCKED. Fixed at 3 across three availability zones, platform-wide. You can't set it, and it already matches the DFE default. |
| `min.insync.replicas=2` | Settable PER TOPIC, but only 1 or 2. Set 2 (the DFE default). |
| `unclean.leader.election.enable=false` | Managed and not exposed. Confluent doesn't do unclean election, so the DFE intent already holds. |
| `num.partitions=12` | PER TOPIC (`partitions_count`, and the provider's default is 6, so set 12). Increase-only, so you can't shrink later. |
| `compression.type=producer` | Per topic, and dfe-engine sets it on each topic it creates. |
| `log.retention.hours=72` | PER TOPIC as `retention.ms` (72h = 259200000, what `dfe-ops init` writes to the dial for a managed broker). |
| `log.retention.bytes` (unset) | PER TOPIC as `retention.bytes` (leave unset for time-only). |
| `log.segment.bytes` (Kafka default) | LOCKED and not exposed. Confluent manages segments. |
| `cleanup.policy=delete` | Settable per topic (default `delete`). |
| `message.max.bytes` (default) | PER TOPIC as `max.message.bytes` (default 2,097,164, at most 8,388,608 on Basic and Standard, 20,971,520 on Enterprise and Dedicated, 20 MB on Freight. Keep it in step with the scalo client budgets). |
| App record limits (16 MiB producers, 8,000,000-byte receiver request body) | Producers follow the size bootstrap writes on the cluster secret from `DFE_KAFKA_MESSAGE_MAX_BYTES`, which the aws root sets. The receiver, loader, fetcher, archiver and transform producers take it as `message.max.bytes`, and dfe-transform-elastic drops a record over 15/16 of it. The receiver refuses a request body over the DFE-wide 8,000,000-byte cap on every tier, which fits Basic's 8,388,608 with room for the message framing. dfe-fetcher and dfe-archiver read it from their first release after v1.4.36 and v1.7.32. An earlier image ignores it and keeps 16 MiB. |

Some topic configs can't be edited after create. dfe-engine creates every topic at startup, so it sets them at creation from its settings (see Wiring back to DFE).

## OpenTofu: Confluent Cloud (API-key PLAIN)

A Standard cluster (multi-AZ, RF3) with the DFE service account, its cluster-scoped API key, and the ACLs a manager account writes for it. The `confluent` provider needs a CLOUD API key (org-level, for management) and DFE needs a KAFKA API key (cluster-level, for the data plane). Both are shown. There's no topic resource, because dfe-engine creates every topic.

```hcl
terraform {
  required_providers {
    confluent = {
      source  = "confluentinc/confluent"
      version = "<exact pin>" # versions.yaml providers.confluentinc-confluent is what DFE's roots run. See README pinning note.
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

The API key `id` is the SASL username and its `secret` is the SASL password. DFE's `confluent-cloud` provider sends that pair as PLAIN over TLS.

## Wiring back to DFE

- `kafka.mode=external`, `kafka.external.bootstrap=<bootstrap_endpoint>`. Strip the `SASL_SSL://` scheme if your endpoint output includes it.
- `kafka.external.provider=confluent-cloud`. The chart derives `SASL_SSL` and `PLAIN` from it and puts the mechanism in the credential Secret beside the API key. So that Secret has the same `username`, `password` and `sasl.mechanism` keys a DFE-owned broker's does.
- `kafka.securityProtocol=SASL_SSL`, the one dial every app chart and dfe-engine read their protocol from. PLAIN never crosses a cleartext transport. On the aws root you set none of the mode, the endpoint or this by hand. `DFE_KAFKA_PROVIDER=confluent-cloud` reaches bootstrap, which puts all three on the Argo cluster secret for the layer 2 appsets to hand on.
- Credentials: put the Kafka API key (username) and secret (password) in the external secret in the deployment's secrets store (Secrets Manager on AWS, OpenBao elsewhere). `kafka.external.auth.type=scram` still works as the user/password carrier. The mechanism itself is PLAIN, derived from the provider and not from `auth.type`.
- Client config: `dfe kafka client-config --provider confluent-cloud`.
- Topic size: set the dfe-engine chart's `kafka.messageMaxBytes` to the `max.message.bytes` your topics carry, within your cluster type's ceiling. Unset, the engine creates each source's topics at its own default size, which a cluster capped below it refuses. On the AWS path tofu sets it for you through `DFE_KAFKA_MESSAGE_MAX_BYTES`.
- Topic retention: set the dfe-engine chart's `kafka.topicRetentionMs`, or the topics keep Confluent's default. On the AWS path tofu sets it through `DFE_KAFKA_RETENTION_MS`.
- Replication: set the dfe-engine chart's `kafka.topicReplicationFactor` to 3. Confluent Cloud refuses any other factor and creates no topic on first write, so a source whose topics are refused never takes data. The layer 2 appsets set it for every external broker.

Confluent Cloud auto-provisions a billable Flink compute pool on cluster create (scalo `ProviderCapabilities.has_billable_side_resources`). Sweep that pool when you tear a cluster down, or it keeps billing.

## Verification: what is and isn't confirmed

Confirmed by 2026 research (sources in the README):

- RF is fixed at 3 platform-wide.
- `min.insync.replicas` is settable per topic, but only 1 or 2.
- `partitions_count` defaults to 6 and is increase-only.
- Topic configs `cleanup.policy`, `retention.ms`, `retention.bytes`, `max.message.bytes`, `compression.type` and `min.insync.replicas` are settable.
- Segment sizing and leader election aren't exposed.
- The `confluent_kafka_cluster`, `confluent_kafka_topic`, `confluent_api_key` and `confluent_service_account` resources, and the CLOUD-key and KAFKA-key split, are current.
- The DFE `confluent-cloud` provider is SASL_SSL PLAIN (scalo `providers.rs`, canonical table).

NOT verified for the hand-written snippet above, which the `managed-kafka/confluent-cloud` module path does not exercise:

- The precise `region` string.
- Whether the Standard tier (against Enterprise, Dedicated or Freight) meets your throughput.
- That every topic config key above is editable after create on your tier. Some are create-only, and Confluent's own docs warn config edits can fail after creation.

Run `tofu validate` and plan against your org before you apply.
