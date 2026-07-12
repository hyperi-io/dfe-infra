<!--
Project:   DFE (Data Forensics Engine) - product suite
File:      docs/kafka/confluent-cloud.md
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
is the most managed of the three clouds: it exposes very few broker knobs and
expresses the rest per topic.

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
| `message.max.bytes` (default) | PER TOPIC as `max.message.bytes` (default 1 MiB -- keep aligned with the scalo client budgets). |

Net: on Confluent Cloud the DFE opinion reduces to a per-topic config of
`min.insync.replicas=2`, `partitions=12`, `retention.ms=259200000`,
`compression.type=producer`, `cleanup.policy=delete`, `max.message.bytes` at the
default. RF3, KRaft, unclean-election-off, and segment sizing are all handled by
the platform (and already match the DFE intent). Note that some topic configs are
not editable after create -- set them right at creation and use
`lifecycle { prevent_destroy = true }` on production topics.

## Terraform / OpenTofu -- Confluent Cloud (API-key PLAIN)

Standard cluster (multi-AZ, RF3) with a service account, a cluster-scoped API
key, and one topic carrying the DFE defaults. The `confluent` provider needs a
CLOUD API key (org-level, for management); the topic resource needs a KAFKA API
key (cluster-level, for the data plane) -- both are shown.

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

resource "confluent_role_binding" "dfe_app_write" {
  principal   = "User:${confluent_service_account.dfe_app.id}"
  role_name   = "DeveloperWrite"
  crn_pattern = "${confluent_kafka_cluster.dfe.rbac_crn}/kafka=${confluent_kafka_cluster.dfe.id}/topic=dfe-*"
}

resource "confluent_role_binding" "dfe_app_read" {
  principal   = "User:${confluent_service_account.dfe_app.id}"
  role_name   = "DeveloperRead"
  crn_pattern = "${confluent_kafka_cluster.dfe.rbac_crn}/kafka=${confluent_kafka_cluster.dfe.id}/group=dfe-*"
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

# --- A topic carrying the DFE defaults (the settable subset) ---
resource "confluent_kafka_topic" "example" {
  kafka_cluster { id = confluent_kafka_cluster.dfe.id }
  topic_name       = "dfe-example"
  partitions_count = 12 # DFE default (TF default is 6); increase-only
  rest_endpoint    = confluent_kafka_cluster.dfe.rest_endpoint

  config = {
    "min.insync.replicas" = "2"          # allowed range is 1 or 2 -- use 2
    "compression.type"    = "producer"
    "cleanup.policy"      = "delete"
    "retention.ms"        = "259200000"  # 72h
    "max.message.bytes"   = "1048576"    # ~1 MiB, aligned with scalo client budgets
  }

  credentials {
    key    = confluent_api_key.dfe_app_kafka.id
    secret = confluent_api_key.dfe_app_kafka.secret
  }

  lifecycle { prevent_destroy = true }
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
- `KAFKA_PROVIDER=confluent-cloud` / `DFE_KAFKA_PROVIDER=confluent-cloud`
  -> scalo derives `SASL_SSL` + `PLAIN` (never hand-set). The floor check refuses
  PLAIN on any non-TLS transport.
- Credentials: put the Kafka API key (username) + secret (password) in the
  Vault-backed external secret; `kafka.external.auth.type=scram` still works as
  the user/password carrier (the mechanism itself is PLAIN, derived from the
  provider, not from `auth.type`).
- Client config: `dfe kafka client-config --provider confluent-cloud`.

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
