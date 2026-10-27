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

`terraform/environments/aws` builds an EKS cluster and hands it to the same bootstrap and Argo CD layers every other cluster uses. Nothing about Layer 1 or Layer 2 changes for AWS. The difference is in what the root builds and what the deployment dial resolves.

- ARM only. Every node pool, and the MSK broker shape when one is built, resolve to Graviton. The cluster module refuses a `shape_ref` whose resolved architecture is not arm64.
- Nodes and the data plane sit in private subnets, behind one NAT gateway by default (`network.nat: single`) or one per availability zone (`per-az`) when a zone failure or the cross-zone charge outweighs the extra gateway.
- `network.az_count` (default 3, 2-6) sizes the VPC and is one of the six fields `sizing.yaml` locks once resolved ([Sizing dials and locks](#sizing-dials-and-locks)). A Strimzi or MSK broker count steps up to at least the AZ count, so a deployment spanning more zones than the tyre-kick floor's three brokers still spreads one broker per zone.
- The Kubernetes API's private endpoint is always on. `endpoint.public: true` adds the public one, and only alongside `endpoint.allowed_cidrs`. The plan refuses a public endpoint with no allow-list.
- A gateway VPC endpoint keeps S3 traffic off the NAT path at no extra charge.
- The account's state bucket is a one-off: run `terraform/environments/aws-state` once, before any deployment applies.
- Every controller that needs an AWS credential -- the EBS CSI driver, Karpenter, the AWS Load Balancer Controller, external-dns, cert-manager -- gets it through EKS Pod Identity, associated to its own service account by the cluster module. Nothing here carries a static key.
- One file drives all of it: the deployment dial, `deployment.yaml`, copied from `deployment.example.yaml` or written by `dfe-ops init` ([wizard.md](wizard.md)).

### external-dns

external-dns publishes only what the gateway chart marks. Its sources include Gateway API routes, and the Gateway admits routes from every namespace. Without a filter any namespace-scoped actor could attach an HTTPRoute to the wildcard listener and have a name published under your domain with a public certificate behind it. The addon carries `annotationFilter: dfe.hyperi.io/publish-dns=true`, and `helm/edge/gateway` writes that annotation on every route it renders and on the managed proxy's own Service. A route from a deploy-repo overlay is published only if it carries the same annotation.

external-dns runs `policy: sync` with a per-deployment `txtOwnerId` (`argocd/appsets/layer1-addons.yaml`), so a deleted Service or Ingress has its record actively removed. The chart's own default, `upsert-only`, never deletes anything it published. That covers every teardown except `tofu destroy` itself: the EKS cluster and every node go down in the same run, so whatever external-dns last published is still in the private zone with no controller left to react.

`terraform/modules/kubernetes-cluster/aws/dns.tf` carries a destroy-time cleanup for that case. It empties the zone of everything but its own apex NS/SOA pair, using the `aws` CLI directly, before the zone itself is destroyed. It runs under whatever AWS identity `tofu destroy` is already authenticated as, so the one added prerequisite is the `aws` CLI on the machine running the destroy. The cluster module's destroy-time sweep of leaked CNI network interfaces (`eks.tf`) needs it too, and a destroy without the CLI fails.

## Sizing the deployment

- Copy `deployment.example.yaml` to `deployment.yaml`, or run `dfe-ops init`. Every AWS-relevant field is documented inline, including the worked `cloud_aws:` example block near the bottom.
- Estimate `sizing.ingest_gb_per_day` in GB/day, or leave it blank for the tyre-kick floor: the smallest shape that runs the profile without an out-of-memory kill, with whatever throughput it happens to carry reported rather than targeted.
- On AWS economy focus with `kafka.provider: msk` the resolver's own live run resolves that floor at the smallest shapes the resolver accepts, S, with the managed brokers the largest single line: `m8g.large` for eks-system, `m8g.xlarge` for general, `r8gd.large` for clickhouse, `m8g.large` for keeper, three `express.m7g.large` for msk-broker, and `c8gd.4xlarge` for ci-burst.
- With Kafka running in the cluster (`kafka.provider: strimzi`) the same floor loses that line and stays S, with kafka-broker on `m8g.xlarge`: its 8 GiB is more than an `m8g.large` leaves a pod. That is the default combined quorum. `kafka.controller_pool: separate` adds three `m8g.large` controllers.
- Both buckets come from on-demand Linux pricing in the AWS Pricing API, read 2026-09-13. Rates drift, and the resolver's own `sizing/<profile>.report.md` (written under `--out`) is the current answer.
- At the floor the resolver's A4 check WARNS rather than fails: `r8gd.large` and `m8g.large` sustain only 3,600 IOPS / 79 MiB/s on their EBS baseline and can burst above it for 30 minutes a day, while ClickHouse and Keeper write continuously. The floor is the smallest shape that runs, not a throughput target. Set `sizing.ingest_gb_per_day` once you know it, so the resolver sizes for the workload instead of the minimum.

## Resolving sizing artefacts

- Run `python3 scripts/resolve_sizing.py --dial deployment.yaml --live --out terraform/environments/aws`. Or resolve with `--fixtures scripts/tests/fixtures/sizing --cloud aws` against a captured catalogue, with no AWS call. Capture one first for a region other than us-west-2 with `resolve_sizing.py capture --region <region> --fixtures <dir>`.
- Every artefact lands under `--out` (default the repo root when omitted). `<out>/shapes/resolved/aws-<region>.json` is the instance-type answer, keyed by region because instance-generation availability is not one worldwide.
- `<out>/sizing.auto.tfvars.json` sits at the ROOT of `--out` and holds `node_pools` and `resolved_shapes`, nothing the root does not declare. It is named so, never `<tier>.auto.tfvars.json`, so it sorts after `render_dial.py --tofu`'s own `dial.auto.tfvars.json` whatever tier was resolved.
- The resolver is the ONE writer of both variables. It starts from the dial's own `node_pools.system` block (the always-on group a deployer sizes by hand) and merges the pools it derives on top. `render_dial.py --tofu` emits neither key at all, so the two producers never collide on the same tfvars variable.
- Under `<out>/sizing/`: `<tier>.values.yaml`, its `<tier>.data-charts.values.yaml` subset, `<tier>.report.md` (what was sized, from which ratio, in which cost bucket) and `resolved.yaml` (the baseline the next resolve diffs against). An on-prem resolve adds `<tier>.nodes.json`, the demand `bootstrap.sh`'s preflight checks the cluster's real nodes against.
- Pointing `--out` at `terraform/environments/aws` is what lands `sizing.auto.tfvars.json` beside `render_dial.py --tofu`'s own `dial.auto.tfvars.json`, so `tofu init` picks up both. Every `*.auto.tfvars.json` in that directory loads, in name order.
- Copy `<out>/sizing/` into the deployment repo's own `sizing/` and commit it there, so a re-size has something to check itself against. `dfe-ops upgrade --dial` diffs a re-resolve against `<deploy>/sizing/resolved.yaml`, never against `--out`. layer2-data reads its `<tier>.data-charts.values.yaml` under `infra/`.
- `bootstrap.sh` reads `<out>/sizing/` from the same place, and seeds the data-charts file into a bundled repo. A resolve run anywhere else has to set `DFE_SIZING_DIR` to that directory. On a cluster running Karpenter, bootstrap REFUSES when `<tier>.pools.json` is not there, and the node-pools chart refuses to render with no pool classes rather than prune the cluster's NodePools.

## Sizing dials and locks

- `sizing.focus` buys headroom over the sized peak: `economy` (40%), `balanced` (60%) or `performance` (100%). None of the three trade away RF3 or `min.insync.replicas=2`, and none of them decides where the KRaft metadata quorum runs. That is `kafka.controller_pool` below.
- `sizing.overrides` is a deployer's per-workload override block, applied last, over whatever the ratios derived. It is still checked against every storage-cap assertion, so an override is a different answer, not an exemption.
- `sizing.yaml`'s `locked:` section names six fields locked once resolved: `partition_count`, `storage_model`, `kafka_provider` (recorded from the dial's `kafka.provider`), `controller_mode` (combined against separate KRaft controllers, `kafka.controller_pool`), `cloud_token` and `az_count`. The resolver records each in `sizing/resolved.yaml` (`az_count` only when it resolved against a cloud catalogue). A re-resolve given `--previous <deploy>/sizing/resolved.yaml` that moves any of them refuses and exits 3 unless you also pass `--migrate`. <!-- doc-paths: ignore -->
- Four of the six also have a chart key, and the deployment repo holds those keys as protected vars, so the engine's API refuses an edit to them unless the caller holds `helmvars:override`. `governance/policies/sizing-locks.yaml` covers three: `kafka.sizing.*` (the partition count derives from it), `kafka.controllerPool.enabled` and `cloud`. `storage-layout.yaml` covers the storage model (`clickhouse.storageModel`, `kafka.storageModel`). Neither policy governs a direct git commit.
- The Kafka provider and the AZ count are OpenTofu inputs with no chart key, so the resolver's lock is the only one they get. The MSK module refuses any broker shape that is not MSK Express (`terraform/modules/managed-kafka/msk/variables.tf`), and `network.az_count` feeds the VPC's zone list in `terraform/modules/kubernetes-cluster/aws/vpc.tf`.
- Above 100,000 GB/day the resolver refuses outright and points at a professional-services engagement rather than a generated profile.

## What the apply builds

- `python3 scripts/render_dial.py --tofu` writes `terraform/environments/aws/dial.auto.tfvars.json` from the dial's `target.provision` block. `tofu init` reads the state bucket, key and region straight out of that file, so there is no separate `-backend-config` to keep in step. <!-- doc-paths: ignore -->
- The root creates the VPC and its subnets, the EKS cluster and its node groups, the managed Kafka when the dial asks for one, the deployment's KMS key, the Route 53 zones and the Secrets Manager store. Nothing that runs inside Kubernetes.
- The deploying principal's EKS access entry uses `data.aws_iam_session_context.issuer_arn`, so an SSO permission set, an assumed role, an instance profile or a Lambda role all work. The entry names the IAM role, with its `aws-reserved/sso.amazonaws.com/<region>/` path for SSO, not the STS session ARN.
- A managed broker builds BESIDE the cluster, not after it. The Kafka module takes the network, the cluster name, the KMS key and the Pod Identity trust policy as explicit inputs, and the key's ARN carries the ordering against the key policy that a broker's first log delivery needs. So nothing holds the broker behind the control plane, the node groups or the addons. The broker is still the longest resource in the deployment, so it sets when the apply ends.
- `tofu` writes NO outputs to the state until the whole apply finishes. `--from-terraform` cannot read them while the broker is still creating, and the deploy waits for the apply to end even though bootstrap touches no broker at all. Two applies are the way round it: one `-target=module.cluster` to land the cluster outputs, then the full apply. The broker's own annotations are only written by a bootstrap run that happens after it exists, so that route costs a second bootstrap pass. Take the single apply unless the second pass is worth it to you.
- `dfe-ops stack-deploy --from-terraform terraform/environments/aws` assembles the bootstrap env from the outputs, sets the stack version, runs the offline pin, drift and render preflight, then `bootstrap.sh`, the readiness gate and the two default data checks. `--check-only` runs the preflight alone, with no cluster contact. `bootstrap/bridge.py` is the reader it imports, not a command you run.
- The secrets store is Secrets Manager (`aws-sm`). The root composes no other, and `stack-deploy` refuses `DFE_SECRETS_BACKEND=openbao` on AWS.
- `k8s.repo_url` is the dfe-infra chart repo Argo reads. A private copy needs `DFE_REPO_TOKEN`, an HTTPS token with read access that `bootstrap.sh` turns into the Argo repository credential. Without it every Layer 2 Application stays `Unknown` with "authentication required". Export it, or carry it in an `--env-file`. The dfe-* images and thin charts pull anonymously.
- When `k8s.storage_class` names no class the cluster already has, `bootstrap.sh` creates it: provisioner `ebs.csi.aws.com`, gp3 baseline 3,000 IOPS / 125 MiB/s, encrypted with the account's default EBS key, `WaitForFirstConsumer`. EKS 1.30+ ships only `gp2` with no default class, and the CSI add-on creates none itself. It is marked the cluster default only when the cluster has none, so an existing default is left in place.
- The same run's internal-CA persist step renders the gateway chart with the identical value files Argo layers afterwards: `argocd/values/common.yaml`, `argocd/values/<cloud>.yaml`, `argocd/values/profile-<profile>.yaml`.
- The AWS Load Balancer Controller is given `vpcId` (the `DFE_VPC_ID` output, carried on the cluster secret as `dfe.hyperi.io/vpc_id`) and `region` explicitly rather than discovering them itself. Node IMDS hop limit stays at 1.

## Kafka and ClickHouse

Every managed backend on AWS is the vendor's AWS instance: the plan refuses a backend AWS does not offer and a ClickHouse Cloud service on another cloud. [managed-backends.md](managed-backends.md) has the rule, the per-cloud table and the egress ports.

### Kafka

All five `kafka.provider` tokens validate on both the resolver and the OpenTofu root: `strimzi`, `redpanda`, `msk`, `confluent-cloud` and `redpanda-cloud`. The root calls the module body the dial names, each behind its own `count`. Only `name`, `env` and `network` are true of every body regardless of provider. dfe-engine creates the topics on every provider. [kafka/README.md](kafka/README.md) has the defaults DFE runs Kafka with.

- `msk` runs MSK Express brokers -- Express only, because MSK cannot convert a Standard cluster to Express later. They authenticate both ways: SASL/SCRAM for every DFE app, and SASL/IAM for the in-cluster bootstrap Job alone, since Kafka ACLs live in the data plane and tofu cannot write the first one. That Job writes only the SCRAM principal's ACLs and exits non-zero on any failure.
- MSK's broker-count scaler, its telemetry sink and its rebalancing behaviour are operational detail, not part of the provider choice. See [Kafka telemetry and autoscaling](aws-operations.md#kafka-telemetry-and-autoscaling) in aws-operations.md.
- `confluent-cloud` and `redpanda-cloud` bodies live under `terraform/modules/managed-kafka/`, contract-tested like `msk/`. Neither sizes, versions or scales itself from a shape the resolver picks.
- The vendor API credential is a provider environment variable, never a tfvar: `CONFLUENT_CLOUD_API_KEY` / `CONFLUENT_CLOUD_API_SECRET` for Confluent, `REDPANDA_CLIENT_ID` / `REDPANDA_CLIENT_SECRET` for Redpanda Cloud. Export both before `tofu plan`, since the `redpanda` provider checks for its pair at configure time regardless of which `kafka.provider` is selected.
- `confluent-cloud` checks `message_max_bytes` against its tier's ceiling. Its default tier (Freight) is a Private Network Interface, not PrivateLink, and caps a topic's `max.message.bytes` at 20 MB, so it carries the chain's 16 MiB. Basic and Standard cap it at 8,388,608. The root's `DFE_KAFKA_MESSAGE_MAX_BYTES` output hands the dial's size to dfe-engine, which creates every topic at it.
- `redpanda-cloud` takes the shared SCRAM password and the ephemeral-lifecycle `allow_deletion` flag. Its only creatable tier is Serverless, over a PrivateLink-style endpoint, and its SCRAM password reuses MSK's own seed.
- Hand-written OpenTofu for both lives in [kafka/confluent-cloud.md](kafka/confluent-cloud.md) and [kafka/redpanda-cloud.md](kafka/redpanda-cloud.md), for a deployment that stands the service up by other means.
- `strimzi` runs in the cluster exactly as it does anywhere else. Its broker autoscaling is a KEDA `ScaledObject` on the `KafkaNodePool` `/scale` subresource, triggered on consumer lag by default, with scale-in off.

### ClickHouse

- ClickHouse's storage model dial (`sizing.storage_model`) defaults to `auto`: object store wherever an endpoint exists, with `local` the explicit opt-out. On AWS that means `cached-object`: parts sit in the S3 bucket `terraform/modules/kubernetes-cluster/aws/object-store.tf` provisions, authenticated through its own Pod Identity role rather than a static key, behind a local read-through cache.
- The AWS ClickHouse shape pins a local-NVMe family (`r*d`, generation floor 8). The resolver sizes a dedicated instance-store cache disk for it and points `clickhouse.objectStore.cache.volume` at `instance-store`, so the cache runs on that NVMe rather than sharing the data PVC. [storage.md](storage.md) has the mechanism and its default (`pvc`, bounded at 60% of the data PVC's space) for any shape with no such NVMe.
- Both node paths assemble that NVMe as one RAID0 array under the kubelet: the Karpenter pool through `instanceStorePolicy`, the managed node group through a nodeadm NodeConfig its launch template carries. The cache takes 0.8 of it (`clickhouse.instance_store_cache_share`). The bucket, the cache placement and the storage model all reach the chart on the cluster secret, so a fresh deploy repo needs no overlay for any of them.
- `clickhouse.mode: external` connects to a server you brought, ClickHouse Cloud first, and deploys none. The dial's `clickhouse.host` travels as `endpoints.clickhouse_host`. The root hands it, the ports, TLS and the user to the cluster secret as `dfe.hyperi.io/clickhouse_*`, which layer2-data, layer2-platform and layer2-apps turn into clickhouse-cluster's, network-policies', the otel collector's and every app's `clickhouse` values.
- The external ClickHouse password is `TF_VAR_clickhouse_external_password`, never a dial field. The secrets module seeds it at `<project>/<env>/clickhouse/external`, and clickhouse-cluster copies it into the engine's namespace as `clickhouse-admin-password`.
- A ClickHouse Cloud service's IP access list has to admit the cluster's NAT egress, the root's `DFE_EGRESS_CIDRS` output.
- Retention starts from a 24-hour assumed consumer downtime on the sized (`scale`) tier, plus any `archiver_lag_hours`. `single` and `slim` are not sized and keep the chart's fixed 72-hour profile. DLQ topics hold 7 days (168 hours) regardless of tier.
