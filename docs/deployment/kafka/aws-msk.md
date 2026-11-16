<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/deployment/kafka/aws-msk.md
Purpose:   Config guide for running DFE against AWS MSK Provisioned
           (SASL/SCRAM). States DFE's defaults, which the platform lets you
           set vs locks down, and a working OpenTofu template.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# DFE on AWS MSK

Operations (bootstrap Job, autoscaling, telemetry) are in [aws-msk-operations.md](aws-msk-operations.md).

The aws root builds MSK Provisioned itself under `kafka.provider: msk` (`terraform/modules/managed-kafka/msk`). This guide is for an MSK you stand up yourself and then point DFE at with `kafka.mode=external`. DFE supports MSK Provisioned: fixed broker-hour capacity, KRaft and full `server.properties` control. It's DFE provider `msk`, with SASL/SCRAM-SHA-512 auth (Secrets Manager), and it's the closest managed cluster to DFE's self-hosted brokers.

MSK Serverless isn't supported. It takes IAM auth alone, and DFE's Kafka clients can't produce the AWS IAM token, so scalo rejects provider `msk_iam` at startup ([#292](https://github.com/hyperi-io/dfe-infra/issues/292)).

Read the baseline in [README.md](README.md) first. This guide covers what MSK lets you apply and how.

MSK Provisioned is the slowest backend to create and delete, and you can't pause a cluster, so it bills for every hour it exists. Plan any test environment around that. DFE was tested on MSK once, with 2.2.1, and is re-tested on explicit approval alone, for a change that needs a live MSK cluster ([cloud-cycle.md](../../cloud-cycle.md#msk-runs-last-and-rarely)).

## What MSK lets you set vs locks down

MSK Provisioned gives you a full `server.properties` through an `aws_msk_configuration` attached to the cluster, so the broker-level DFE defaults apply directly.

| DFE default | On MSK Provisioned |
|---|---|
| KRaft (no ZooKeeper) | Yes. MSK manages KRaft and there's nothing to set. |
| `default.replication.factor=3` | Settable, and MSK FORCES topic RF to 3 anyway (a create with RF != 3 is coerced to 3). That matches DFE. |
| `min.insync.replicas=2` | Settable at the broker level (`server_properties`). The topic-level override is PROTECTED, so you can't weaken it per topic. |
| `unclean.leader.election.enable=false` (Kafka 4.x default) | Settable at the broker level. Topic-level override PROTECTED. |
| `num.partitions=12` | Settable at the broker level (applies to auto-created topics). |
| `compression.type=producer` | dfe-engine sets it on each topic it creates, so there's nothing to set on the broker. |
| `log.retention.hours=72` | Settable. 72 hours is what `dfe-ops init` writes to the dial's `kafka.msk.log_retention_ms` for a managed broker. |
| `log.retention.bytes` (unset) | Settable (leave off / -1). |
| `log.segment.bytes` (Kafka default, 1 GiB) | Settable. DFE leaves it at the default. |
| `message.max.bytes=16777216` | Settable. Keep it in step with `replica.fetch.max.bytes`. |

Locked: MSK protects the topic-level overrides of `min.insync.replicas`, `unclean.leader.election.enable` and the replication factor. The DFE defaults for those three (2, false and 3) are the values you set at broker level, so nothing needs a topic-level override.


## OpenTofu: MSK Provisioned (SASL/SCRAM-512)

This is the DFE `msk` provider path. It's a provisioned cluster with a custom configuration carrying the DFE broker defaults, SASL/SCRAM auth backed by Secrets Manager, and the SCRAM secret associated to the cluster. Your tfvars set the Kafka version, broker count, broker type and volume size. They're never literals in the `.tf`.

```hcl
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "<exact pin>" # versions.yaml providers.hashicorp-aws is what DFE's roots run. See README pinning note.
    }
  }
}

# --- Custom broker configuration: the DFE opinionated defaults ---
resource "aws_msk_configuration" "dfe" {
  kafka_versions = [var.kafka_version] # aws kafka list-kafka-versions prints the ACTIVE ones
  name           = "dfe-kafka"

  server_properties = <<-PROPERTIES
    auto.create.topics.enable=false
    default.replication.factor=3
    min.insync.replicas=2
    num.partitions=12
    log.retention.ms=259200000
    message.max.bytes=16777216
    replica.fetch.max.bytes=16777216
    replica.lag.time.max.ms=30000
  PROPERTIES
}

# --- KMS key for the SCRAM secret (Secrets Manager requires a CMK, not the AWS-managed key) ---
resource "aws_kms_key" "dfe_kafka" {
  description = "DFE MSK SCRAM credential encryption"
}

# --- SCRAM credential in Secrets Manager (name MUST start with "AmazonMSK_") ---
resource "aws_secretsmanager_secret" "dfe_kafka_user" {
  name       = "AmazonMSK_dfe-kafka-user"
  kms_key_id = aws_kms_key.dfe_kafka.key_id
}

resource "aws_secretsmanager_secret_version" "dfe_kafka_user" {
  secret_id     = aws_secretsmanager_secret.dfe_kafka_user.id
  secret_string = jsonencode({ username = "dfe-kafka-user", password = var.dfe_kafka_password })
}

# --- The provisioned cluster ---
resource "aws_msk_cluster" "dfe" {
  cluster_name           = "dfe-kafka"
  kafka_version          = var.kafka_version
  number_of_broker_nodes = var.broker_count # a multiple of the subnet count, RF3 across AZs

  broker_node_group_info {
    instance_type   = var.broker_instance_type
    client_subnets  = var.private_subnet_ids # 3 subnets in 3 AZs
    security_groups = [aws_security_group.msk.id]
    storage_info {
      ebs_storage_info { volume_size = var.broker_volume_gib }
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.dfe.arn
    revision = aws_msk_configuration.dfe.latest_revision
  }

  client_authentication {
    sasl { scram = true }
  }

  encryption_info {
    encryption_in_transit {
      client_broker = "TLS"        # SASL_SSL: SCRAM over TLS -- the DFE floor
      in_cluster    = true
    }
  }
}

# --- Bind the SCRAM secret to the cluster ---
resource "aws_msk_scram_secret_association" "dfe" {
  cluster_arn     = aws_msk_cluster.dfe.arn
  secret_arn_list = [aws_secretsmanager_secret.dfe_kafka_user.arn]
  depends_on      = [aws_secretsmanager_secret_version.dfe_kafka_user]
}

output "bootstrap_brokers_sasl_scram" {
  value = aws_msk_cluster.dfe.bootstrap_brokers_sasl_scram
}
```

Point DFE at the `bootstrap_brokers_sasl_scram` endpoint with provider `msk` (see "Wiring back to DFE" below).

### Topics (the AWS provider does not manage topics)

`aws_msk_cluster` creates no topics, and on a cluster carrying the DFE configuration nothing else will either. `auto.create.topics.enable` is false, so the partition count per source is an explicit number and not a broker default. dfe-engine creates the topics at startup from the topic set dfe-schemas declares, under the ACLs the bootstrap Job below writes. Leave them to it. A topic created any other way keeps its partitions and configs, and the engine doesn't reshape them.

[aws-msk-operations.md](aws-msk-operations.md) covers the bootstrap Job that writes the ACLs (and what you set to drive it by hand), broker-count autoscaling, telemetry routing and the broker security group's ports.

## Wiring back to DFE

- `kafka.mode=external`, `kafka.external.bootstrap=<bootstrap endpoint>`.
- `kafka.securityProtocol=SASL_SSL`. The SCRAM listener on 9096 is TLS alone, and every app chart and dfe-engine read their protocol from this one dial. On the aws root you set none of this by hand. `DFE_KAFKA_PROVIDER=msk` reaches bootstrap, which puts the mode, the endpoint and `SASL_SSL` on the Argo cluster secret for the layer 2 appsets to hand on.
- `kafka.external.provider` is the identity the chart derives the SASL mechanism from. `msk` gives `SASL_SSL` and `SCRAM-SHA-512`, and credentials come from the external secret in the deployment's secrets store, Secrets Manager on AWS (`kafka.external.auth.type=scram`).
- Client config: `dfe kafka client-config --provider msk` prints the derived settings from the canonical provider table.

## Verification: what is and isn't confirmed

Confirmed against the platform's documentation (sources in the README):

- MSK Provisioned exposes `server_properties`, including all the DFE broker defaults.
- Topic-level RF, `min.insync.replicas` and `unclean.leader.election.enable` are protected, and RF is forced to 3.
- The `aws_msk_configuration` and `aws_msk_scram_secret_association` resources are current in the provider.

The bootstrap Job's pinned jar is verified. `versions.yaml` pins `services.aws-msk-iam-auth`, and the Job checks the downloaded jar against the sha256 in `values.yaml`.

NOT verified:

- The bootstrap Job on any release after 2.2.1, the one DFE ran on MSK. It has the same jar pin as that run.
- The broker type (`broker_shape_ref`) and count the resolver picks, against your throughput.
- That every `server.properties` key above is permitted on the Kafka version you pick. The allowed set varies by version, so check the AWS "Custom MSK configurations" page.

Treat the snippets as templates. Run `tofu validate` and plan against your account before you apply.
