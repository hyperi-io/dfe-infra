<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/deployment/kafka/aws-msk.md
Purpose:   Config guide for running DFE against AWS MSK -- provisioned
           (SASL/SCRAM) and serverless (IAM). States DFE's defaults, which
           the platform lets you set vs locks down, and a working OpenTofu /
           Terraform template for each.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# DFE on AWS MSK

DFE does NOT auto-deploy MSK -- you stand it up, then point DFE at it with
`kafka.mode=external`. MSK comes in two shapes DFE supports as distinct
providers:

- **MSK Provisioned** -- fixed broker-hour capacity, KRaft, full
  `server.properties` control. DFE provider `msk`, auth **SASL/SCRAM-SHA-512**
  (Secrets Manager). This is the closest managed cluster to DFE's own
  self-hosted brokers.
- **MSK Serverless** -- elastic, IAM-only. DFE provider `msk_iam`, auth
  **IAM / SASL_SSL OAUTHBEARER** (port 9098). No static secret; almost no broker
  config.

Read the opinionated baseline in [README.md](README.md) first -- this guide only
covers what MSK lets you apply and how.

## What MSK lets you set vs locks down

### MSK Provisioned

MSK Provisioned gives you a real `server.properties` via an
`aws_msk_configuration` attached to the cluster, so the broker-level DFE defaults
apply directly.

| DFE default | On MSK Provisioned |
|---|---|
| KRaft (no ZooKeeper) | Yes -- MSK is KRaft-managed; nothing to set. |
| `default.replication.factor=3` | Settable, and MSK FORCES topic RF to 3 anyway (a create with RF != 3 is coerced to 3). Aligned with DFE. |
| `min.insync.replicas=2` | Settable at the broker level (`server_properties`). NOTE: the topic-level override is PROTECTED -- you cannot weaken it per topic. |
| `unclean.leader.election.enable=false` | Settable at the broker level. Topic-level override PROTECTED. |
| `num.partitions=12` | Settable at the broker level (applies to auto-created topics). |
| `compression.type=producer` | Settable. |
| `log.retention.hours=72` | Settable. |
| `log.retention.bytes` (unset) | Settable (leave off / -1). |
| `log.segment.bytes=512 MiB` | Settable. |
| `message.max.bytes` (default) | Settable; keep aligned with `replica.fetch.max.bytes`. |

Locked: the topic-level overrides of `min.insync.replicas`,
`unclean.leader.election.enable`, and the replication factor are protected by
MSK. This is FINE for DFE -- the DFE defaults are exactly what MSK enforces, so
the protection works in your favour.

### MSK Serverless

MSK Serverless hides the broker entirely. There is **no `aws_msk_configuration`**
-- you cannot set `num.partitions`, `default.replication.factor`,
`unclean.leader.election.enable`, `log.segment.bytes`, or any other broker
default. AWS manages durability (each partition gets replicas AWS does not
count against your quota).

| DFE default | On MSK Serverless |
|---|---|
| KRaft / replication / ISR / unclean election | Locked -- AWS-managed, not exposed. Durability is handled by AWS; you cannot set RF/ISR. |
| `num.partitions=12` | Not a broker default -- set partition count PER TOPIC at create time. Cluster quota: 2400 leader partitions (raise via AWS Support). |
| `compression.type`, `retention.ms`, `retention.bytes`, `max.message.bytes`, `cleanup.policy` | Settable PER TOPIC only (these five are the honoured topic configs). |
| `log.segment.bytes` | Locked -- managed. |
| Per-partition storage | Hard cap 250 GB/partition -- add partitions for more storage, not longer single-partition retention. |

Because Serverless exposes so little, the DFE opinion collapses to: RF/ISR/unclean
are AWS-managed (already durable), and you express retention + compression +
partitions per topic.

## Terraform / OpenTofu -- MSK Provisioned (SASL/SCRAM-512)

This is the DFE `msk` provider path: a provisioned cluster with a custom
configuration carrying the DFE broker defaults, SASL/SCRAM auth backed by Secrets
Manager, and the SCRAM secret associated to the cluster.

```hcl
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0" # VERIFY latest + PIN exact (6.54.0 seen 2026-07-08); see README pinning note.
    }
  }
}

# --- Custom broker configuration: the DFE opinionated defaults ---
resource "aws_msk_configuration" "dfe" {
  kafka_versions = ["3.9.x"] # MSK's latest KRaft line; verify the exact version MSK offers.
  name           = "dfe-kafka"

  server_properties = <<-PROPERTIES
    auto.create.topics.enable=true
    default.replication.factor=3
    min.insync.replicas=2
    unclean.leader.election.enable=false
    num.partitions=12
    compression.type=producer
    log.retention.hours=72
    log.segment.bytes=536870912
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
  kafka_version          = "3.9.x" # match aws_msk_configuration.kafka_versions
  number_of_broker_nodes = 3       # one per AZ -> RF3 across AZs

  broker_node_group_info {
    instance_type   = "kafka.m5.large"
    client_subnets  = var.private_subnet_ids # 3 subnets in 3 AZs
    security_groups = [aws_security_group.msk.id]
    storage_info {
      ebs_storage_info { volume_size = 100 }
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

Point DFE at the `bootstrap_brokers_sasl_scram` endpoint with provider `msk`
(see "Wiring back to DFE" below).

## Terraform / OpenTofu -- MSK Serverless (IAM)

This is the DFE `msk_iam` provider path. Minimal cluster (AWS manages capacity);
auth is IAM only, so there is NO Secrets Manager secret and NO
`aws_msk_configuration`.

```hcl
resource "aws_msk_serverless_cluster" "dfe" {
  cluster_name = "dfe-kafka-serverless"

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.msk.id]
  }

  client_authentication {
    sasl { iam { enabled = true } } # IAM only; port 9098
  }
}

# IAM policy the DFE consumer/producer role needs (attach to the IRSA role that
# the DFE ServiceAccounts assume -- there is no static credential).
data "aws_iam_policy_document" "dfe_kafka" {
  statement {
    actions   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"]
    resources = [aws_msk_serverless_cluster.dfe.arn]
  }
  statement {
    actions = [
      "kafka-cluster:CreateTopic", "kafka-cluster:DescribeTopic",
      "kafka-cluster:WriteData", "kafka-cluster:ReadData",
    ]
    resources = ["${replace(aws_msk_serverless_cluster.dfe.arn, ":cluster/", ":topic/")}/*"]
  }
  statement {
    actions   = ["kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"]
    resources = ["${replace(aws_msk_serverless_cluster.dfe.arn, ":cluster/", ":group/")}/dfe-*"]
  }
}

output "bootstrap_brokers_sasl_iam" {
  value = aws_msk_serverless_cluster.dfe.bootstrap_brokers_sasl_iam
}
```

### Topics on either MSK (the AWS provider does not manage topics)

Neither `aws_msk_cluster` nor `aws_msk_serverless_cluster` creates topics, and on
a Provisioned cluster carrying the DFE configuration nothing else will either:
`auto.create.topics.enable` is false, so that the partition count per source is an
explicit number rather than a broker default. The kafka chart pre-creates them
from the bootstrap Job below. If you would rather manage topics as IaC, the
community `Mongey/kafka` provider speaks both SCRAM and `aws-iam`:

```hcl
terraform {
  required_providers {
    kafka = { source = "Mongey/kafka", version = "0.13.1" } # verify + pin
  }
}

# IAM (Serverless) example:
provider "kafka" {
  bootstrap_servers = split(",", aws_msk_serverless_cluster.dfe.bootstrap_brokers_sasl_iam)
  tls_enabled       = true
  sasl_mechanism    = "aws-iam"
  sasl_aws_region   = var.aws_region
}

resource "kafka_topic" "example" {
  name               = "dfe-example"
  partitions         = 12   # DFE default -- set per topic on Serverless
  replication_factor = 3    # Provisioned forces 3; Serverless manages replicas
  config = {
    "compression.type"  = "producer"
    "retention.ms"      = "259200000" # 72h
    "cleanup.policy"    = "delete"
    "max.message.bytes" = "1048576"
  }
}
```

For MSK Provisioned with SCRAM, set `sasl_mechanism = "scram-sha512"`,
`sasl_username`, and `sasl_password` on the provider instead.

## The bootstrap Job -- ACLs and landing topics

Kafka ACLs live in the **Kafka data plane**, not the AWS control plane, so no
`tofu apply` can write one: the AWS provider has no such call, and the brokers sit
in private subnets your deploy runner cannot reach. Once the cluster stops
granting by default, nothing can write the first ACL unless it is already
permitted. Topics have the same problem from the other end --
`auto.create.topics.enable=false`, and dfe-loader treats "no matching topics" as
fatal.

So the kafka chart runs an in-cluster Job, rendered only at
`kafka.mode=external` with `kafka.provider=msk`
(`helm/charts/kafka/templates/msk-bootstrap-job.yaml`). It authenticates with
**SASL/IAM on port 9098**, where the IAM policy is the authorisation and no Kafka
ACL has to exist yet, then:

1. creates every landing and DLQ topic the chart derives, `--if-not-exists`, with
   the derived partition count, `max.message.bytes` and `retention.ms`;
2. grants the SCRAM principal exactly what an on-prem deployment grants -- topic
   literal `*` with Read, Write, Create and Describe, consumer groups by the
   `dfe-` prefix with Read and Describe;
3. prints the resulting ACL and topic lists, and exits non-zero on any failure.

**The IAM identity exists for this bootstrap path alone.** Every DFE app still
authenticates with SASL/SCRAM-512 on 9096; the cluster enables both mechanisms so
the Job can write the grants the apps rely on. Nothing is mounted into the Job --
EKS Pod Identity supplies the credential.

`apache/kafka` ships `kafka-acls.sh` and `kafka-topics.sh` but not the MSK IAM
login module, so an init container fetches `aws-msk-iam-auth-<version>-all.jar`
from its GitHub release and refuses it unless the sha256 matches the pinned one.
An air-gapped deploy sets `url` to an internal mirror; the checksum still has to
match.

### What you set

| Value | What it is |
|---|---|
| `kafka.mode` / `kafka.provider` | `external` and `msk` -- the Job renders for nothing else. |
| `kafka.external.msk.bootstrapIam` | The 9098 bootstrap string, `bootstrap_brokers_sasl_iam`. Empty renders no Job. |
| `kafka.external.msk.scramUsername` | The principal the ACLs are written for; must equal the username in the `AmazonMSK_` secret. |
| `kafka.external.msk.region` | Optional. Leave empty and the Pod Identity agent's own `AWS_REGION` is used. |
| `kafka.external.msk.bootstrap.serviceAccount` | The ServiceAccount the Job runs as. |
| `kafka.external.msk.bootstrap.iamAuth.version` / `.sha256` / `.url` | The jar pin, its published checksum, and the mirror override. |

**The service account name and the release namespace must match the Pod Identity
association**, which `managed-kafka/msk` takes as
`pod_identity = { namespace, service_account }` with no defaults. Rename either
side and the agent hands the pod no credential, so the Job fails at the SASL
handshake rather than at admission.

The Job is a tracked Argo resource, not a sync hook, so it stays in the
Application's resource tree and holds the app layer back until it completes.

## Broker-count autoscaling

MSK Express has no native broker-count autoscaling (`UpdateBrokerCount` is a
manual API), so `managed-kafka/msk` adds a CloudWatch alarm on cluster-wide
`BytesInPerSec` that triggers a Lambda calling it, increase-only, behind
`var.autoscaling` (`enabled`, `max_brokers`, `step`, `per_broker_capacity_mb_s`,
`headroom` -- wired from the dial's `kafka.msk.autoscaling`, defaulted when the
dial names none). It is the only CloudWatch surface this module adds -- DFE's
own AWS monitoring stays on its OTel feed -- and the Lambda's log group is
capped at one day's retention rather than the Lambda default of never expiring.

## Telemetry

DFE's monitoring goes to its own OTel feed and HyperDX, never CloudWatch --
the deployment dial's `telemetry.aws.sink` decides which of MSK's forced
CloudWatch touchpoints get a workaround and which stay on AWS's own path.
`otel` is the default; `cloudwatch` is the opt-in AWS-native path for a
compliance need CloudWatch itself satisfies.

| Touchpoint | Under `sink: otel` | Under `sink: cloudwatch` | Cost |
|---|---|---|---|
| Broker logs | S3 bucket this module creates, lifecycle-expired at `retention_days` (default 2), SSE-KMS on the deployment key. The fetcher's `object_store` source reads it. | `aws_cloudwatch_log_group`, retention `retention_days` (default 7 under this sink). | S3: near-zero at broker-log volumes, storage only past the lifecycle window. CloudWatch: ingestion plus storage per GB, the more expensive path at any real broker throughput. |
| EKS control-plane audit log | CloudWatch, pinned to a 1-day floor -- EKS has no other export path, so this is an unavoidable exception rather than a chosen destination (`kubernetes-cluster/CONTRACT.md`). | CloudWatch, `retention_days`. | CloudWatch ingestion plus storage either way; otel only bounds it to 1 day instead of a longer dial value. |
| CloudTrail | S3 under both sinks -- a trail always delivers to S3, and `sink` gates only the CloudWatch Logs attachment. `LookupEvents` API access via the fetcher's `aws` source needs no bucket name at all. | Adds a CloudWatch Logs attachment at `retention_days`, on top of the S3 delivery every trail already has. | S3-only under otel: negligible at management-event volumes. cloudwatch doubles the storage (the same events land in two destinations) plus CloudWatch ingestion. |
| MSK open_monitoring (JMX + node exporter) | Scraped in-cluster by the otel-collector gateway's Prometheus receiver, never delivered through either sink. | Same -- `open_monitoring` has no CloudWatch delivery option; it is always scrape-only. | No AWS-side cost; ordinary in-cluster Prometheus scrape traffic. |
| Broker-count autoscaler Lambda log group | 1 day's retention under both sinks (see "Broker-count autoscaling" above) -- diagnostic-only, and predates the telemetry dial. | Same. | Negligible; the Lambda fires once per scale-out event. |

`terraform/modules/managed-kafka/CONTRACT.md` is the source of truth for the
`telemetry` input's shape; this table is the cost summary a deployer reads
before picking a sink.

## Wiring back to DFE

- `kafka.mode=external`, `kafka.external.bootstrap=<bootstrap endpoint>`.
- Provider identity so scalo derives the transport (never hand-set):
  - Provisioned: `KAFKA_PROVIDER=msk` / `DFE_KAFKA_PROVIDER=msk`
    -> `SASL_SSL` + `SCRAM-SHA-512`. Credentials via the Vault-backed external
    secret (`kafka.external.auth.type=scram`).
  - Serverless: `KAFKA_PROVIDER=msk_iam` / `DFE_KAFKA_PROVIDER=msk_iam`
    -> `SASL_SSL` + `OAUTHBEARER`. Set `kafka.external.auth.type=msk_iam` -- NO
    static secret is rendered; the consumer ServiceAccounts carry the IAM role
    via IRSA / workload identity.
- Client config: `dfe kafka client-config --provider msk` (or `msk_iam`) emits
  the derived settings from the canonical provider table.

## Verification -- what is and is not confirmed

Confirmed by 2026 research (sources in README): MSK Provisioned exposes
`server_properties` including all the DFE broker defaults; topic-level RF /
`min.insync.replicas` / `unclean.leader.election.enable` are protected and RF is
forced to 3. MSK Serverless is IAM-only on 9098, has no `aws_msk_configuration`,
caps at 2400 leader partitions and 250 GB/partition, and honours only
`cleanup.policy`, `compression.type`, `max.message.bytes`, `retention.bytes`,
`retention.ms` per topic. The `aws_msk_configuration`,
`aws_msk_scram_secret_association`, `aws_msk_serverless_cluster`, and IAM SASL
resources are all current in the provider.

The bootstrap Job's pinned jar is verified: `aws-msk-iam-auth` v2.3.8 was
published 2026-09-05 and the sha256 in `values.yaml` matches both the release's
own `.sha256` asset and a hand-computed sum of the downloaded jar.

NOT verified (no live AWS account in this workstream, cannot deploy): that the
bootstrap Job authenticates end to end -- the Pod Identity credential reaching the
IAM login module, and `kafka-acls.sh` writing an ACL over SASL/IAM, have not been
run against a real cluster. Also the exact
`kafka_version` string MSK offers today (shown as `3.9.x` -- confirm against
`aws kafka list-kafka-versions`), the precise `instance_type` / storage sizing
for your throughput, and that every `server.properties` key above is permitted
on the specific Kafka version you pick (the allowed set varies by version --
check the AWS "Custom MSK configurations" page). Treat the snippets as templates
to `tofu validate` and plan against your account before apply.
