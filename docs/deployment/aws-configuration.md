<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/deployment/aws-configuration.md
Purpose:   Reference for the AWS deployment: what the OpenTofu root builds,
           sizing and its locks, Kafka and ClickHouse choices.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# DFE on AWS -- configuration

What the AWS root builds, how the dial sizes it, and the Kafka and ClickHouse choices behind it. The deploy steps are in [aws.md](aws.md).

- [What every deployment builds](#what-every-deployment-builds)
- [Sizing the deployment](#sizing-the-deployment)
- [Resolving sizing artefacts](#resolving-sizing-artefacts)
- [Sizing dials and locks](#sizing-dials-and-locks)
- [What the apply builds](#what-the-apply-builds)
- [Kafka and ClickHouse](#kafka-and-clickhouse)

## What every deployment builds

`terraform/environments/aws` builds an EKS cluster and hands it to the same bootstrap and Argo CD layers every other cluster uses. Layer 1 and Layer 2 are the same on AWS.

- ARM only. Every node pool, and the MSK brokers when a cluster is built, resolve to Graviton. The cluster module refuses a `shape_ref` whose resolved architecture isn't arm64.
- Nodes and the data plane sit in private subnets. By default they share one NAT gateway (`network.nat: single`). Use one per availability zone (`per-az`) when a zone failure or the cross-zone charge costs more than the extra gateway.
- `network.az_count` (default 3, range 2-6) sizes the VPC and is one of the six fields `sizing.yaml` locks once resolved ([Sizing dials and locks](#sizing-dials-and-locks)). A Strimzi or MSK broker count steps up to at least the AZ count. So a deployment spanning more zones than the tyre-kick floor's three brokers still gets one broker per zone.
- The Kubernetes API's private endpoint is always on. `endpoint.public: true` adds the public one, and only alongside `endpoint.allowed_cidrs`. The plan refuses a public endpoint with no allow-list.
- A gateway VPC endpoint keeps S3 traffic off the NAT path at no extra charge.
- The account's state bucket is a one-off. Run `terraform/environments/aws-state` once, before any deployment applies.
- Every controller that needs an AWS credential gets it through EKS Pod Identity, tied to its own service account by the cluster module. That's the EBS CSI driver, Karpenter, the AWS Load Balancer Controller, external-dns and cert-manager. Nothing here carries a static key.
- One file drives all of it, the deployment dial `deployment.yaml`. Copy it from `deployment.example.yaml` or let `dfe-ops init` write it ([wizard.md](wizard.md)).

### external-dns

external-dns publishes only what the gateway chart marks. Its sources include Gateway API routes, and the Gateway admits routes from every namespace. Without a filter, anyone with namespace-level access could attach an HTTPRoute to the wildcard listener and get a name published under your domain with a public certificate behind it. So the addon carries `annotationFilter: dfe.hyperi.io/publish-dns=true`. `helm/edge/gateway` writes that annotation on every route it renders and on the managed proxy's own Service. A route from a deploy-repo overlay is published only if it carries the same annotation.

external-dns runs `policy: sync` with a per-deployment `txtOwnerId` (`argocd/appsets/layer1-addons.yaml`), so a deleted Service or Ingress has its record removed. The chart's own default, `upsert-only`, never deletes anything it published. Sync covers every teardown except `tofu destroy` itself. There the EKS cluster and every node go down in the same run, so whatever external-dns last published stays in the private zone with no controller left to clean it up.

`terraform/modules/kubernetes-cluster/aws/dns.tf` has a destroy-time cleanup for that case. It empties the zone of everything but its own apex NS/SOA pair, using the `aws` CLI directly, before the zone itself is destroyed. It runs under whatever AWS identity `tofu destroy` is already using. The one extra prerequisite is the `aws` CLI on the machine running the destroy. The cluster module's destroy-time sweep of leaked CNI network interfaces (`eks.tf`) needs it too, and a destroy without the CLI fails.

## Sizing the deployment

- Copy `deployment.example.yaml` to `deployment.yaml`, or run `dfe-ops init`. Every AWS-relevant field is documented inline, including the worked `cloud_aws:` example block near the bottom.
- Estimate `sizing.ingest_gb_per_day` in GB/day, or leave it blank for the tyre-kick floor. That's the smallest size that runs the profile without an out-of-memory kill. It reports whatever throughput it happens to carry and doesn't target one.
- On AWS economy focus with `kafka.provider: msk`, the resolver's own live run puts that floor at the smallest sizes it accepts, S, with the managed brokers the largest single line. That's `m8g.large` for eks-system, `m8g.xlarge` for general, `r8gd.large` for clickhouse, `m8g.large` for keeper, three `express.m7g.large` for msk-broker, and `c8gd.4xlarge` for ci-burst.
- With Kafka in the cluster (`kafka.provider: strimzi`) the floor loses that line and stays S, with kafka-broker on `m8g.xlarge`. Its 8 GiB is more than an `m8g.large` leaves a pod. That's the default combined quorum. `kafka.controller_pool: separate` adds three `m8g.large` controllers.
- Both buckets come from on-demand Linux pricing in the AWS Pricing API, read 2026-09-13. Rates drift, so the resolver's own `sizing/<profile>.report.md` (written under `--out`) is the current answer.
- At the floor, the resolver's A4 check WARNS and doesn't fail. `r8gd.large` and `m8g.large` sustain only 3,600 IOPS / 79 MiB/s on their EBS baseline and can burst above it for 30 minutes a day, while ClickHouse and Keeper write continuously. The floor is the smallest size that runs, not a throughput target. Set `sizing.ingest_gb_per_day` once you know it, and the resolver sizes for the workload instead of the minimum.

## Resolving sizing artefacts

- Run `python3 scripts/resolve_sizing.py --dial deployment.yaml --live --out terraform/environments/aws`. Or resolve with `--fixtures scripts/tests/fixtures/sizing --cloud aws` against a captured catalogue, with no AWS call. For a region other than us-west-2, capture one first with `resolve_sizing.py capture --region <region> --fixtures <dir>`.
- Every artefact lands under `--out` (the repo root if you leave it out). `<out>/shapes/resolved/aws-<region>.json` is the instance-type answer. It's keyed by region because instance generations aren't available everywhere.
- `<out>/sizing.auto.tfvars.json` sits at the ROOT of `--out` and holds `node_pools` and `resolved_shapes`, nothing the root doesn't declare. It's never named `<tier>.auto.tfvars.json`, so it sorts after `render_dial.py --tofu`'s own `dial.auto.tfvars.json` whatever tier was resolved. `tofu init` loads every `*.auto.tfvars.json` in that directory, in name order.
- The resolver is the ONE writer of both variables. It starts from the dial's own `node_pools.system` block (the always-on group you size by hand) and merges the pools it derives on top. `render_dial.py --tofu` emits neither key, so the two producers never collide on a tfvars variable.
- Under `<out>/sizing/` you get `<tier>.values.yaml`, its `<tier>.data-charts.values.yaml` subset, `<tier>.report.md` (what was sized, from which ratio, in which cost bucket) and `resolved.yaml` (the baseline the next resolve diffs against). An on-prem resolve adds `<tier>.nodes.json`, the demand `bootstrap.sh`'s preflight checks the cluster's real nodes against.
- Copy `<out>/sizing/` into the deployment repo's own `sizing/` and commit it, so a re-size has something to check itself against. `dfe-ops upgrade --dial` diffs a re-resolve against `<deploy>/sizing/resolved.yaml`, never against `--out`. layer2-data reads its `<tier>.data-charts.values.yaml` under `infra/`.
- `bootstrap.sh` reads `<out>/sizing/` from the same place and seeds the data-charts file into a bundled repo. A resolve run anywhere else needs `DFE_SIZING_DIR` set to that directory. On a cluster running Karpenter, bootstrap REFUSES when `<tier>.pools.json` isn't there. The node-pools chart also refuses to render with no pool classes, rather than prune the cluster's NodePools.

## Sizing dials and locks

- `sizing.focus` buys headroom over the sized peak: `economy` (40%), `balanced` (60%) or `performance` (100%). None of the three trades away RF3 or `min.insync.replicas=2`, and none decides where the KRaft metadata quorum runs. That's `kafka.controller_pool` below.
- `sizing.overrides` is your per-workload override block, applied last, over whatever the ratios derived. Storage-cap assertions also check overridden values.
- The `locked:` section of `sizing.yaml` names six fields that lock once resolved. They are `partition_count`, `storage_model`, `kafka_provider` (recorded from the dial's `kafka.provider`), `controller_mode` (combined against separate KRaft controllers, `kafka.controller_pool`), `cloud_token` and `az_count`. The resolver records each in `sizing/resolved.yaml` (`az_count` only when it resolved against a cloud catalogue). Give a re-resolve `--previous <deploy>/sizing/resolved.yaml` and it refuses, exiting 3, if any of them moved. Pass `--migrate` as well if you mean it. <!-- doc-paths: ignore -->
- Four of the six also have a chart key. The deployment repo holds those keys as protected vars, so the engine's API refuses an edit unless the caller holds `helmvars:override`. `governance/policies/sizing-locks.yaml` covers three: `kafka.sizing.*` (the partition count derives from it), `kafka.controllerPool.enabled` and `cloud`. `storage-layout.yaml` covers the storage model (`clickhouse.storageModel`, `kafka.storageModel`). Neither policy governs a direct git commit.
- The Kafka provider and the AZ count are OpenTofu inputs with no chart key, so the resolver's lock is the only one they get. The MSK module refuses any broker type that isn't MSK Express (`terraform/modules/managed-kafka/msk/variables.tf`). `network.az_count` feeds the VPC's zone list in `terraform/modules/kubernetes-cluster/aws/vpc.tf`.
- Above 100,000 GB/day the resolver refuses outright and points you at a professional-services engagement, not a generated profile.

## What the apply builds

- `python3 scripts/render_dial.py --tofu` writes `terraform/environments/aws/dial.auto.tfvars.json` from the dial's `target.provision` block. `tofu init` reads the state bucket, key and region straight from that file, so there's no separate `-backend-config` to keep in step. <!-- doc-paths: ignore -->
- The root creates the VPC and its subnets, the EKS cluster and its node groups, the managed Kafka when the dial asks for one, the deployment's KMS key, the Route 53 zones and the Secrets Manager store. Nothing that runs inside Kubernetes.
- The deploying principal's EKS access entry uses `data.aws_iam_session_context.issuer_arn`, so an SSO permission set, an assumed role, an instance profile or a Lambda role all work. The entry names the IAM role, with its `aws-reserved/sso.amazonaws.com/<region>/` path for SSO, not the STS session ARN.
- A managed broker builds BESIDE the cluster, not after it. The Kafka module takes the network, the cluster name, the KMS key and the Pod Identity trust policy as explicit inputs, and the key's ARN carries the ordering against the key policy that a broker's first log delivery needs. So nothing holds the broker behind the control plane, the node groups or the addons. It's still the longest resource in the deployment, so it decides when the apply ends.
- `tofu` writes NO outputs to the state until the whole apply finishes. `--from-terraform` can't read them while the broker is still creating, so the deploy waits for the apply to end even though bootstrap touches no broker at all. Two applies get round it: one `-target=module.cluster` to land the cluster outputs, then the full apply. The broker's own annotations are only written by a bootstrap run after it exists, so that route costs a second bootstrap pass. Take the single apply unless the second pass is worth it to you.
- `dfe-ops stack-deploy --from-terraform terraform/environments/aws` assembles the bootstrap env from the outputs, sets the stack version, runs the offline pin, drift and render preflight, then `bootstrap.sh`, the readiness gate and the two default data checks. `--check-only` runs the preflight alone and never touches the cluster. `bootstrap/bridge.py` is the reader it imports, not a command you run.
- The secrets store is Secrets Manager (`aws-sm`). The root composes no other, and `stack-deploy` refuses `DFE_SECRETS_BACKEND=openbao` on AWS.
- `k8s.repo_url` is the dfe-infra chart repo Argo reads. A private copy needs `DFE_REPO_TOKEN`, an HTTPS token with read access that `bootstrap.sh` turns into the Argo repository credential. Without it every Layer 2 Application stays `Unknown` with "authentication required". Export it, or carry it in an `--env-file`. The dfe-* images and thin charts pull anonymously.
- When `k8s.storage_class` names no class the cluster already has, `bootstrap.sh` creates it. It uses provisioner `ebs.csi.aws.com`, gp3 baseline 3,000 IOPS / 125 MiB/s, encryption with the account's default EBS key, and `WaitForFirstConsumer`. EKS 1.30+ ships only `gp2` with no default class, and the CSI add-on creates none itself. The new class becomes the cluster default only when the cluster has none, so an existing default stays.
- The same run's internal-CA persist step renders the gateway chart with the same value files Argo layers afterwards: `argocd/values/common.yaml`, `argocd/values/<cloud>.yaml`, `argocd/values/profile-<profile>.yaml`.
- The AWS Load Balancer Controller gets `vpcId` (the `DFE_VPC_ID` output, carried on the cluster secret as `dfe.hyperi.io/vpc_id`) and `region` explicitly. It doesn't discover them. Node IMDS hop limit stays at 1.

## Kafka and ClickHouse

Every managed backend on AWS is the vendor's AWS instance. The plan refuses a backend AWS doesn't offer, and a ClickHouse Cloud service on another cloud. [managed-backends.md](managed-backends.md) has the rule, the per-cloud table and the egress ports.

### Kafka

All five `kafka.provider` tokens validate on both the resolver and the OpenTofu root: `strimzi`, `redpanda`, `msk`, `confluent-cloud` and `redpanda-cloud`. The root calls the module body the dial names, each behind its own `count`. Only `name`, `env` and `network` hold for every body whatever the provider. dfe-engine creates the topics on every provider. [kafka/README.md](kafka/README.md) has the defaults DFE runs Kafka with.

- `msk` runs MSK Express brokers, and only Express, because MSK can't convert a Standard cluster to Express later. They authenticate both ways. Every DFE app uses SASL/SCRAM. The in-cluster bootstrap Job alone uses SASL/IAM, because Kafka ACLs live in the data plane and tofu can't write the first one. That Job writes only the SCRAM principal's ACLs and exits non-zero on any failure.
- MSK's broker-count scaler, its telemetry sink and its rebalancing behaviour are operational detail, not part of choosing a provider. See [Kafka telemetry and autoscaling](aws-operations.md#kafka-telemetry-and-autoscaling) in aws-operations.md.
- The `confluent-cloud` and `redpanda-cloud` bodies live under `terraform/modules/managed-kafka/`, tested against the same module rules as `msk/`. Neither sizes, versions or scales itself from an instance type the resolver picks.
- The vendor API credential is a provider environment variable, never a tfvar. That's `CONFLUENT_CLOUD_API_KEY` and `CONFLUENT_CLOUD_API_SECRET` for Confluent, `REDPANDA_CLIENT_ID` and `REDPANDA_CLIENT_SECRET` for Redpanda Cloud. Export both pairs before `tofu plan`, because the `redpanda` provider checks for its pair at configure time whichever `kafka.provider` you chose.
- `confluent-cloud` checks `message_max_bytes` against its tier's ceiling. Its default tier (Freight) is a Private Network Interface, not PrivateLink, and caps a topic's `max.message.bytes` at 20 MB, so it carries the chain's 16 MiB. Basic and Standard cap it at 8,388,608. The root's `DFE_KAFKA_MESSAGE_MAX_BYTES` output hands the dial's size to dfe-engine, which creates every topic at it.
- `redpanda-cloud` takes the shared SCRAM password and the ephemeral-lifecycle `allow_deletion` flag. Its only creatable tier is Serverless, over a PrivateLink-style endpoint, and its SCRAM password reuses MSK's own seed.
- Hand-written OpenTofu for both is in [kafka/confluent-cloud.md](kafka/confluent-cloud.md) and [kafka/redpanda-cloud.md](kafka/redpanda-cloud.md), for a deployment that stands the service up some other way.
- `strimzi` runs in the cluster exactly as it does anywhere else. Its broker autoscaling is a KEDA `ScaledObject` on the `KafkaNodePool` `/scale` subresource. It triggers on consumer lag by default, with scale-in off.

### ClickHouse

> **Coming from Splunk or Elastic?** With the default `cached-object` storage model there are no hot, warm and cold tiers to move data between. ClickHouse keeps its parts in S3 and serves repeated reads from a local NVMe cache ([storage.md](storage.md)).

- ClickHouse's storage model dial (`sizing.storage_model`) defaults to `auto`. That means object store wherever an endpoint exists, with `local` the explicit opt-out. On AWS it resolves to `cached-object`. Parts sit in the S3 bucket `terraform/modules/kubernetes-cluster/aws/object-store.tf` provisions, authenticated through its own Pod Identity role and not a static key, behind a local read-through cache.
- The AWS ClickHouse node pool pins a local-NVMe family (`r*d`, generation floor 8). The resolver sizes a dedicated instance-store cache disk for it and points `clickhouse.objectStore.cache.volume` at `instance-store`, so the cache runs on that NVMe and doesn't share the data PVC. [storage.md](storage.md) has the mechanism and its default for any node type with no such NVMe (`pvc`, bounded at 60% of the data PVC's space).
- Both node paths assemble that NVMe as one RAID0 array under the kubelet. The Karpenter pool does it through `instanceStorePolicy`, the managed node group through a nodeadm NodeConfig its launch template carries. The cache takes 0.8 of it (`clickhouse.instance_store_cache_share`). The bucket, the cache placement and the storage model all reach the chart on the cluster secret, so a fresh deploy repo needs no overlay for any of them.
- `clickhouse.mode: external` connects to a server you brought, ClickHouse Cloud first, and deploys none. The dial's `clickhouse.host` travels as `endpoints.clickhouse_host`. The root hands it, the ports, TLS and the user to the cluster secret as `dfe.hyperi.io/clickhouse_*`. layer2-data, layer2-platform and layer2-apps turn those into the `clickhouse` values for clickhouse-cluster, network-policies, the otel collector and every app.
- The external ClickHouse password is `TF_VAR_clickhouse_external_password`, never a dial field. The secrets module seeds it at `<project>/<env>/clickhouse/external`, and clickhouse-cluster copies it into the engine's namespace as `clickhouse-admin-password`.
- A ClickHouse Cloud service's IP access list has to admit the cluster's NAT egress, the root's `DFE_EGRESS_CIDRS` output.
- Retention starts from a 24-hour assumed consumer downtime on the sized (`scale`) tier, plus any `archiver_lag_hours`. `single` isn't sized and keeps the chart's 24-hour default (`kafka.retention.assumedConsumerDowntimeH`), and `slim` runs no broker. DLQ topics hold 7 days (168 hours) whatever the tier.
