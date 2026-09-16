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

Module and inputs (provisioning, Terraform, wiring): [aws-msk.md](aws-msk.md).

# DFE on AWS MSK -- Operations

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

**One principal, one ACL set, for the whole fleet.** This is deliberate
on-prem parity, not a regression -- and a roadmap item toward per-app
principals (one SCRAM user per DFE service, scoped to the topics it
touches), so a compromised app credential reaches only its own topics
rather than every topic on the cluster, ahead of the product going public.

**The IAM identity exists for this bootstrap path alone.** Every DFE app still
authenticates with SASL/SCRAM-512 on 9096; the cluster enables both mechanisms so
the Job can write the grants the apps rely on. Nothing is mounted into the Job --
EKS Pod Identity supplies the credential.

`apache/kafka` ships `kafka-acls.sh` and `kafka-topics.sh` but not the MSK IAM
login module, so an init container fetches `aws-msk-iam-auth-<version>-all.jar`
from its GitHub release and refuses it unless the sha256 matches the pinned one.
An air-gapped deploy sets `url` to an internal mirror; the checksum still has to
match.

**On the standard AWS path, `kafka.mode` and `bootstrapIam` are wired, not
set.** `bootstrap.sh` derives `DFE_KAFKA_MODE=external` from
`DFE_KAFKA_PROVIDER=msk` and carries the aws root's `DFE_KAFKA_BOOTSTRAP_IAM`
output onto the cluster secret; the `layer2-data` ApplicationSet turns both
annotations into the kafka chart's values, so the Job and its ServiceAccount
render with no manual step once the dial names `kafka.provider: msk`.

## What you set

The two rows below are what the wiring above sets automatically for a
`kafka.provider: msk` deploy on this path -- name them by hand only when
driving the chart directly (a bespoke overlay, or outside `bootstrap.sh`).

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

The alarm's metric query reads the `Average` statistic, not `Sum`.
`BytesInPerSec` is already a per-second rate, published once a minute, so a
300s evaluation period holds five of those samples -- summing them inflates
the reading roughly fivefold against `autoscaling_threshold_bytes_per_sec`,
a genuine bytes-per-second figure, and fires the alarm at about a fifth of
the traffic it was sized for. The outer `SUM()` in the metric-math
expression is a different aggregation and is correct as-is: it is the
spatial sum across brokers that `SEARCH` discovers, not a sum over time.

## Telemetry

DFE's monitoring goes to its own OTel feed and HyperDX, never CloudWatch --
the deployment dial's `telemetry.aws.sink` decides which of MSK's forced
CloudWatch touchpoints get a workaround and which stay on AWS's own path.
`otel` is the default; `cloudwatch` is the opt-in AWS-native path for a
compliance need CloudWatch itself satisfies.

| Touchpoint | Under `sink: otel` | Under `sink: cloudwatch` | Cost |
|---|---|---|---|
| Broker logs | S3 bucket this module creates, lifecycle-expired at `retention_days` (default 2), SSE-KMS on the deployment key. The fetcher's `object_store` source reads it. | `aws_cloudwatch_log_group`, retention `retention_days` (default 7 under this sink). | S3: XS at broker-log volumes, storage only past the lifecycle window. CloudWatch: ingestion plus storage per GB, the dearer path at any real broker throughput. |
| EKS control-plane audit log | CloudWatch, pinned to a 1-day floor -- EKS has no other export path, so this is an unavoidable exception rather than a chosen destination (`kubernetes-cluster/CONTRACT.md`). | CloudWatch, `retention_days`. | CloudWatch ingestion plus storage either way; otel only bounds it to 1 day instead of a longer dial value. |
| CloudTrail | S3 under both sinks -- a trail always delivers to S3, and `sink` gates only the CloudWatch Logs attachment. `LookupEvents` API access via the fetcher's `aws` source needs no bucket name at all. | Adds a CloudWatch Logs attachment at `retention_days`, on top of the S3 delivery every trail already has. | S3-only under otel: XS at management-event volumes. cloudwatch doubles the storage (the same events land in two destinations) plus CloudWatch ingestion. |
| MSK open_monitoring (JMX + node exporter) | Scraped in-cluster by the otel-collector gateway's Prometheus receiver, never delivered through either sink. | Same -- `open_monitoring` has no CloudWatch delivery option; it is always scrape-only. | No AWS-side cost; ordinary in-cluster Prometheus scrape traffic. |
| Broker-count autoscaler Lambda log group | 1 day's retention under both sinks (see "Broker-count autoscaling" above) -- diagnostic-only, and predates the telemetry dial. | Same. | XS; the Lambda fires once per scale-out event. |

`terraform/modules/managed-kafka/CONTRACT.md` is the source of truth for the
`telemetry` input's shape; this table is the cost summary a deployer reads
before picking a sink, in the buckets
[aws.md](../aws.md#how-costs-are-described) defines.

## Ports the broker security group admits

9096 (SASL/SCRAM, every DFE app)
and 9098 (SASL/IAM, the bootstrap Job above) are open from `client_cidrs`
(the whole VPC when that list is empty). 11001 and 11002 (the JMX and node
exporter the `open_monitoring` scrape reads) are open separately, always from
the whole VPC (`var.network.cidr`) regardless of how `client_cidrs` is set --
a caller narrowing `client_cidrs` below the VPC for the Kafka ports does not
also cut off the in-cluster otel-collector's scrape. All four ports are
`terraform/modules/managed-kafka/msk/main.tf` (`aws_vpc_security_group_ingress_rule.brokers`
and `.open_monitoring`).
