# Managed backends on the deployment's own cloud

A managed backend is offered only as its instance on the cloud the deployment runs on. An AWS deployment gets MSK, Confluent Cloud on AWS, Redpanda Cloud on AWS and ClickHouse Cloud on AWS. GCP and Azure follow the same rule, and a service the provider does not carry is not offered there: no MSK on GCP or Azure.

The plan enforces it, every DFE client gets egress to the backend on that backend's own ports, and the data behind both is one file: `terraform/modules/managed-backends/offering.yaml`.

## Why

A service on another provider is reached over the internet. Every byte DFE sends it leaves the deployment's cloud as internet data transfer out, billed per GB, and a data pipeline moves its whole volume through its broker and its datastore. On the same provider that traffic stays on the provider's own network.

## What each cloud offers

The token is what the vendor's own API takes for that cloud, and the tiers are the ones a body here builds that the vendor runs on that cloud. Retrieved 2026-10-15 from the sources in `offering.yaml`.

| Backend | AWS | GCP | Azure | Source |
|---|---|---|---|---|
| MSK | `aws` | not offered | not offered | AWS-only service, https://docs.aws.amazon.com/msk/latest/developerguide/port-info.html |
| Confluent Cloud | `AWS`: freight, enterprise, basic | `GCP`: enterprise, basic | `AZURE`: enterprise, basic | https://docs.confluent.io/cloud/current/clusters/regions.html and the provider's `cloud` docs (Freight is AWS-only) |
| Redpanda Cloud | `aws`: serverless | `gcp`: serverless, in beta, one region, no private connectivity | not offered | https://docs.redpanda.com/redpanda-cloud/get-started/cloud-overview/ and https://docs.redpanda.com/redpanda-cloud/reference/tiers/serverless-regions/ |
| ClickHouse Cloud | host under `.aws.clickhouse.cloud` | host under `.gcp.clickhouse.cloud` | host under `.azure.clickhouse.cloud` | https://clickhouse.com/docs/cloud/reference/supported-regions |

Redpanda runs Dedicated and BYOC on Azure, but both are quote-only and the `redpanda-cloud` body refuses them, so Azure has no Redpanda Cloud here. Strimzi and Redpanda in the cluster run on every cloud: no vendor is involved.

Only the AWS halves are built today. The Confluent and Redpanda bodies build their private attachment with the `aws` provider and refuse any other cloud token, so a GCP or Azure root needs that cloud's own half.

## How the plan refuses

`terraform/environments/aws` calls `terraform/modules/managed-backends` with its cloud, `kafka.provider`, `kafka.confluent_tier` and the ClickHouse host, and hands the vendor's token from it to the Confluent and Redpanda bodies. Nothing in the dial sets a backend's cloud. A ClickHouse Cloud host names its cloud as the label before `clickhouse.cloud` (or `clickhouse-byoc.com`), private endpoints included: `<id>.<region>.vpce.aws.clickhouse.cloud`.

A backend the cloud does not offer:

```
kafka.provider is msk, which is not offered on gcp. A managed backend is offered only as its instance on the deployment's own cloud, because one on another cloud is reached over the internet and every byte to it is billed as internet egress. On gcp the managed Kafka offered is confluent-cloud, redpanda-cloud, and a Kafka in the cluster (strimzi, redpanda) runs on any cloud.
```

A tier the vendor does not run there:

```
The confluent-cloud tier freight is not offered on gcp: the vendor does not run it there. On gcp the confluent-cloud tiers offered are enterprise, basic.
```

A ClickHouse Cloud service on another cloud:

```
clickhouse.host is a ClickHouse Cloud service on gcp, and this deployment runs on aws. A managed backend is offered only as its instance on the deployment's own cloud, because one on another cloud is reached over the internet and every byte to it is billed as internet egress. Create the service on aws (a host under .aws.clickhouse.cloud), or set clickhouse.mode to in_cluster.
```

A body handed another cloud's token refuses too: `cloud is GCP, and this body builds Confluent Cloud's AWS instance only`, and the Redpanda body's equivalent. `tofu test` in `terraform/modules/managed-backends` runs an allowed and a refused combination for each cloud.

## Egress ports

| Backend | Port | What dials it | Source |
|---|---|---|---|
| MSK | 9096 | every DFE client, SASL/SCRAM over TLS | https://docs.aws.amazon.com/msk/latest/developerguide/port-info.html |
| MSK | 9098 | the bootstrap Job alone, SASL/IAM | same |
| Confluent Cloud | 9092 | every DFE client, bootstrap and brokers | https://docs.confluent.io/cloud/current/networking/networking-faq.html |
| Redpanda Cloud | 9092 | every DFE client, the seed | https://docs.redpanda.com/cloud-data-platform/networking/serverless/aws/privatelink-api/ |
| Redpanda Cloud | 9093-9095 | every DFE client, the brokers the seed names | same |
| ClickHouse Cloud | 8443, 9440 | every DFE client, HTTPS and the native protocol over TLS | https://clickhouse.com/docs/guides/sre/network-ports |

In the cluster, network-policies opens these from `kafka.provider`, which layer2-platform reads off the cluster secret (`externalEgress.kafka.providers` in the chart's values), and the ClickHouse ports from the cluster secret's `clickhouse_port` and `clickhouse_native_port`. A broker with no provider entry keeps the generic 9092-9096 and 9098 range. `scripts/tests/test_managed_backend_egress.py` renders every app and data chart as Argo does for each backend, takes every workload given the backend's address as a client, and fails on any client in an isolated namespace with a port its policies do not open. It also holds the chart's ports equal to `offering.yaml`.

Outside the cluster, the toolbox instance (`terraform/modules/toolbox/aws`) is the one machine the AWS root builds, and its security group opens every port above for the selected backend (`backend_egress`): to the VPC for MSK, beyond it for the SaaS services.

dfe-infra builds no DFE component outside Kubernetes. One a deployer runs on the same cloud, such as a compose stack on EC2, needs TCP egress on the ports above: to the MSK brokers from inside the VPC, and to the Confluent, Redpanda and ClickHouse endpoints through NAT, or to their private endpoints inside the VPC. The MSK broker group and the private endpoints admit the VPC's CIDR unless `client_cidrs` narrows it.

## The rest of the stack on AWS

The same like-for-like rule decides the rest of an AWS deployment.

| Concern | On AWS | Note |
|---|---|---|
| Kafka (managed) | MSK, or Confluent or Redpanda Cloud on AWS | this page |
| ClickHouse (managed) | ClickHouse Cloud on AWS | this page |
| DNS | Route 53, through external-dns, and a private zone | |
| Load balancers | AWS Load Balancer Controller | |
| Object store | S3, with an S3 gateway endpoint | |
| Block storage | EBS gp3 | |
| Nodes | EC2 through Karpenter | |
| Workload identity | EKS Pod Identity and IAM | |
| Encryption keys | KMS, one key per deployment | Kubernetes Secrets are envelope-encrypted with it |
| Audit and logs | CloudTrail, and CloudWatch where AWS forces it | |
| TLS certificates | cert-manager | deliberate on every cloud, not ACM |
| HyperDX metadata store | FerretDB on PostgreSQL in the cluster | deliberate, not RDS |
| ClickHouse without ClickHouse Cloud | in the cluster | deliberate |
| Images | GHCR | deliberate, not ECR |

## Next steps, not built

Same region. A service in another region of the same provider still costs inter-region data transfer. MSK sits in the VPC, and both SaaS bodies place their cluster in the deployment's own region, so only ClickHouse Cloud can differ: its host carries the region, and `managed-backends` could refuse one that is not `provision.region`. The trade-off is reach. ClickHouse Cloud and Redpanda Serverless run in fewer regions than AWS, so a deployment in a region without one loses that backend rather than reaching across.

Private connectivity. Over NAT, every byte to a SaaS backend is also billed as NAT gateway data processing. Confluent (Private Network Interface on Freight, PrivateLink on Enterprise) and Redpanda (PrivateLink) are built and are the default `kafka.connectivity`, while a cloud cycle runs public because tofu writes their topics and ACLs from outside the VPC. ClickHouse Cloud PrivateLink is not built: it needs an interface endpoint to the service's endpoint service, a private Route 53 zone for `<region>.vpce.aws.clickhouse.cloud`, the endpoint id on the service's allow list, and a security group admitting 443, 8443 and 9440. The vendor offers it on its Scale plan and above. The trade-off is the interface endpoint's own hourly and per-GB charges against the NAT charge they replace, which favours PrivateLink as volume grows.
