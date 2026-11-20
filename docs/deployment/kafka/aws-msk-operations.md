<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/deployment/kafka/aws-msk-operations.md
Purpose:   Operator guide for a running DFE-on-MSK deployment -- the ACL and
           topic bootstrap flow, broker-count autoscaling, and telemetry
           routing.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# Running DFE on AWS MSK

Module and inputs (provisioning, OpenTofu, wiring): [aws-msk.md](aws-msk.md).

## The bootstrap Job -- ACLs

Kafka ACLs live in the **Kafka data plane**, not the AWS control plane, so no
`tofu apply` can write one: the AWS provider has no such call, and the brokers sit
in private subnets your deploy runner cannot reach. Once the cluster stops
granting by default, nothing can write the first ACL unless it is already
permitted. Topics have the same problem from the other end --
`auto.create.topics.enable=false`, and dfe-loader treats "no matching topics" as
fatal.

So the kafka chart runs an in-cluster Job, rendered only at
`kafka.mode=external` with `kafka.external.provider=msk`
(`helm/charts/kafka/templates/msk-bootstrap-job.yaml`). It authenticates with
**SASL/IAM on port 9098**, where the IAM policy is the authorisation and no Kafka
ACL has to exist yet, then:

1. grants the SCRAM principal exactly what an on-prem deployment grants -- topic
   literal `*` with Read, Write, Create, Describe, Alter, Delete, DescribeConfigs
   and AlterConfigs (every topic call dfe-engine makes), consumer groups by the
   `dfe-` prefix with Read and Describe
2. prints the resulting ACL list, and exits non-zero on any failure.

It creates no topic: dfe-engine creates the bootstrap topic set at its own
startup, which is why the grant includes Create.

**One principal, one ACL set, for the whole fleet.** One SCRAM principal
holds the ACLs for every DFE app, as on-prem. A compromised app credential
therefore reaches every topic.

**The IAM identity exists for this bootstrap path alone.** Every DFE app still
authenticates with SASL/SCRAM-512 on 9096, and the cluster enables both mechanisms so
the Job can write the grants the apps rely on. Nothing is mounted into the Job --
EKS Pod Identity supplies the credential.

The Job runs `kafka.cliImage`, Strimzi's Kafka image from quay.io at the brokers' Kafka version. It ships `kafka-acls.sh` and `kafka-topics.sh` but not the MSK IAM login module, so an init container fetches `aws-msk-iam-auth-<version>-all.jar` from its GitHub release and refuses it unless the sha256 matches the pinned one. An air-gapped deploy sets `url` to an internal mirror, and the checksum still has to match.

**On the standard AWS path, `kafka.mode` and `bootstrapIam` are wired, not
set.** `bootstrap.sh` derives `DFE_KAFKA_MODE=external` from
`DFE_KAFKA_PROVIDER=msk` and carries the aws root's `DFE_KAFKA_BOOTSTRAP_IAM`
output onto the cluster secret, and the `layer2-data` ApplicationSet turns both
annotations into the kafka chart's values, so the Job and its ServiceAccount
render with no manual step once the dial names `kafka.provider: msk`.

## What you set

The two rows below are what the wiring above sets automatically for a
`kafka.provider: msk` deploy on this path -- name them by hand only when
driving the chart directly (a bespoke overlay, or outside `bootstrap.sh`).

| Value | What it is |
|---|---|
| `kafka.mode` / `kafka.external.provider` | `external` and `msk` -- the Job renders for nothing else. |
| `kafka.external.msk.bootstrapIam` | The 9098 bootstrap string, `bootstrap_brokers_sasl_iam`. Empty renders no Job. |
| `kafka.external.msk.scramUsername` | The principal the ACLs are written for. It must equal the username in the `AmazonMSK_` secret. |
| `kafka.external.msk.credentialRef` | The `AmazonMSK_` secret's ARN, the cluster secret's `dfe.hyperi.io/kafka_credential_ref`. The one copy of the credential: the apps' `dfe-kafka-user` reads its password from it through the `dfe-secret-store-unprefixed` store, and no `kafka/msk` seed exists. |
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

MSK Express has no scaling policy (`UpdateBrokerCount` is a manual API), so `managed-kafka/msk` runs a Lambda on an EventBridge schedule, every `autoscaling.period_seconds`, behind `var.autoscaling` (`enabled`, `max_brokers`, `step`, `per_broker_capacity_mb_s`, `headroom`, `period_seconds`, `evaluation_periods`, wired from the dial's `kafka.msk.autoscaling`). Each run reads the cluster's current broker count and the `BytesInPerSec` of exactly those brokers over the last `evaluation_periods`. It adds `step` brokers when every period's per-broker mean was over `per_broker_capacity_mb_s` x `headroom`, and never scales in or passes `max_brokers`.

A run acts only on an ACTIVE cluster a full window after its last operation ended, so an overlapping or repeated run never adds a step on load read before the last one landed. A schedule beats a CloudWatch alarm here, because an alarm names a fixed broker set and notifies once per transition, so it never sees an added broker and never fires a second step while load stays high.

Express storage is MSK-managed, so there is no disk metric to watch. The Lambda's log group is CloudWatch under either sink, kept for `telemetry.aws.forced_retention_days`, and DFE's own monitoring stays on its OTel feed.

## Telemetry

DFE's monitoring goes to its OTel feed and dfe-hyperdx, never CloudWatch --
the deployment dial's `telemetry.aws.sink` decides which of MSK's forced
CloudWatch touchpoints get a workaround and which stay on AWS's path.
`otel` is the default, and `cloudwatch` is the opt-in AWS-native path for a
compliance need CloudWatch satisfies.

| Touchpoint | Under `sink: otel` | Under `sink: cloudwatch` | Cost |
|---|---|---|---|
| Broker logs | S3 bucket this module creates, lifecycle-expired at `retention_days` (default 2), SSE-KMS on the deployment key. The fetcher's `object_store` source reads it. | `aws_cloudwatch_log_group`, retention `retention_days` (default 7 under this sink). | S3: XS at broker-log volumes, storage only past the lifecycle window. CloudWatch: ingestion plus storage per GB, the dearer path at any production broker throughput. |
| EKS control-plane audit log | CloudWatch, at `telemetry.aws.forced_retention_days` (1 day unless the dial sets another). EKS has no other export path, so this is an unavoidable exception rather than a chosen destination (`kubernetes-cluster/CONTRACT.md`). The otel collector reads it into the otel database ([provider logs](../../observability/provider-logs.md)). | CloudWatch, `retention_days`. Nothing here reads it. | CloudWatch ingestion plus storage either way. `telemetry.aws.eks_log_types` picks the streams, `audit` by default. Otel bounds the storage to the forced retention. |
| CloudTrail | S3 under both sinks, and a CloudWatch log group under both sinks as well. Under otel the group keeps `telemetry.aws.forced_retention_days` and the otel collector reads it into the otel database ([provider logs](../../observability/provider-logs.md)). `LookupEvents` API access via the fetcher's `aws` source needs no bucket name. | The same group at `retention_days`, kept for the AWS-native path. | S3: XS at management-event volumes. CloudWatch adds ingestion plus storage under both sinks, the short retention under otel being what bounds it. |
| MSK open_monitoring (JMX + node exporter) | Scraped in-cluster by the otel-collector gateway's Prometheus receiver, never delivered through either sink. | Same -- `open_monitoring` has no CloudWatch delivery option, so it is always scrape-only. | No AWS-side cost, just ordinary in-cluster Prometheus scrape traffic. |
| Broker-count autoscaler Lambda log group | 1 day's retention under both sinks (see "Broker-count autoscaling" above) -- diagnostic-only, and predates the telemetry dial. | Same. | XS, because the Lambda fires once per scale-out event. |

`terraform/modules/managed-kafka/CONTRACT.md` is the source of truth for the
`telemetry` input's fields. This table is the cost summary a deployer reads
before picking a sink, in the buckets
[aws.md](../aws.md#how-costs-are-described) defines.

## Module design notes

The module builds Express brokers because MSK cannot convert a Standard cluster to Express, so starting on Standard to move later is the rebuild the choice avoids. Express manages its own storage, so nothing sizes a volume. It holds the thread pools, replication factor, segment size and unclean leader election read-only, so only `num.partitions`, `log.retention.ms`, `message.max.bytes` and `replica.fetch.max.bytes` reach the configuration. `auto.create.topics.enable` stays false because dfe-engine creates the landing topics and dfe-loader treats a missing one as fatal.

The cluster enables both SASL mechanisms. SCRAM serves the apps, and IAM serves the bootstrap Job alone, because the IAM policy is its authorisation and no Kafka ACL has to exist yet.

The broker security group admits 9096 (SASL/SCRAM, every DFE app) and 9098 (SASL/IAM, the bootstrap Job above) from `client_cidrs`, the whole VPC when that list is empty. It admits 11001 and 11002 (the JMX and node exporter the `open_monitoring` scrape reads) separately, always from the whole VPC (`var.network.cidr`), so a caller narrowing `client_cidrs` for the Kafka ports does not cut off the in-cluster otel-collector's scrape. The rules are `aws_vpc_security_group_ingress_rule.brokers` and `.open_monitoring` in `terraform/modules/managed-kafka/msk/main.tf`.

The broker security group has one egress rule, 443/tcp to `0.0.0.0/0`. Secrets Manager, KMS and CloudWatch Logs have no VPC endpoint here, and AWS publishes a managed prefix list for only Secrets Manager, so scoping that one leg would leave the other two open anyway.

On the `otel` sink the log delivery service needs a grant in the deployment KMS key's own policy. The cluster module is that key's one policy owner and passes the statement in through `key_policy_grants`, because `aws_kms_key_policy` replaces the whole policy and a second one here would strip it.

MSK rewrites `configuration_info` and `client_authentication` out of band, so the cluster ignores changes to both. Remove the ignore for the one apply that lands a deliberate change.

The `AmazonMSK_` secret container exists before the cluster and its version after, which keeps the SCRAM association off the cluster's creation path. MSK rejects any other secret name, and the default `aws/secretsmanager` key, as an association failure rather than a create failure.
