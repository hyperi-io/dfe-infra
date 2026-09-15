<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/deployment/aws.md
Purpose:   Operator guide for deploying DFE on AWS -- EKS, Karpenter, MSK/
           Confluent/Redpanda Kafka, sizing and teardown.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# DFE on AWS

Operations (teardown, node pools, edge exposure, Kafka telemetry, upgrades):
[aws-operations.md](aws-operations.md).

DFE's AWS path provisions an EKS cluster with OpenTofu, then hands it to the
same bootstrap and Argo CD layers every other cluster uses. Nothing about
Layer 1 or Layer 2 changes for AWS -- the difference is in what
`terraform/environments/aws` builds and what the deployment dial resolves.

A few things hold for every deployment:

- ARM only. Every node pool, and the MSK broker shape when one is built,
  resolve to Graviton -- the cluster module refuses a `shape_ref` whose
  resolved architecture is not arm64.
- Nodes and the data plane sit in private subnets, behind one NAT gateway by
  default (`network.nat: single`) or one per availability zone (`per-az`)
  when a zone failure or the cross-zone charge outweighs the extra gateway.
- `network.az_count` (default 3, 2-6) sizes the VPC and is one of the four
  fields the resolver derives and locks; a Strimzi/MSK broker count steps up
  to at least the AZ count, so a deployment spanning more zones than the
  tyre-kick floor's three brokers still spreads one broker per zone.
- The Kubernetes API's private endpoint is always on. `endpoint.public: true`
  adds the public one, and only alongside `endpoint.allowed_cidrs` -- the plan
  refuses a public endpoint with no allow-list.
- A gateway VPC endpoint keeps S3 traffic off the NAT path at no extra
  charge.
- The account's state bucket is a one-off: run
  `terraform/environments/aws-state` once, before any deployment applies.
- Every controller that needs an AWS credential -- the EBS CSI driver,
  Karpenter, the AWS Load Balancer Controller, external-dns, cert-manager --
  gets it through EKS Pod Identity, associated to its own service account by
  the cluster module. Nothing here carries a static key.
- external-dns publishes only what the gateway chart marks. Its sources include
  Gateway API routes, and the Gateway admits routes from every namespace, so
  without a filter any namespace-scoped actor could attach an HTTPRoute to the
  wildcard listener and have a name published under your domain with a public
  certificate behind it. The addon carries
  `annotationFilter: dfe.hyperi.io/publish-dns=true`, and `helm/edge/gateway`
  writes that annotation on every route it renders and on the managed proxy's
  own Service. A route from a deploy-repo overlay is published only if it
  carries the same annotation.
- external-dns runs `policy: sync` with a per-deployment `txtOwnerId`
  (`argocd/appsets/layer1-addons.yaml`), so a deleted Service or Ingress has
  its record actively removed -- the chart's own default, `upsert-only`,
  never deletes anything it published. That covers every teardown except
  `tofu destroy` itself: the EKS cluster and every node go down in the same
  run, so whatever external-dns last published is still in the private zone
  with no controller left to react. `terraform/modules/kubernetes-cluster/
  aws/dns.tf` carries a destroy-time cleanup for exactly that case -- it
  empties the zone of everything but its own apex NS/SOA pair, using the
  `aws` CLI directly, before the zone itself is destroyed. It runs under
  whatever AWS identity `tofu destroy` is already authenticated as, so the
  one added prerequisite is the `aws` CLI on the machine running the destroy.
- One file drives all of it: the deployment dial, `deployment.yaml`, copied
  from `deployment.example.yaml`.

## How costs are described

Costs here are T-shirt buckets, never figures. A bucket is relative to the
deployment's own compute, never to a currency: XS is negligible beside the
cluster's compute, S a small fraction of it, M of the same order as one
dedicated node, L of the same order as the whole cluster's compute, and XL
larger than the cluster's compute. A pricing model is named wherever it decides
whether an option is safe -- hourly, per GB processed, per request, per
LCU-hour, per broker-hour -- but never a rate. The scale is deliberate: rates
move by region, by account and by discount, so a deployer prices their own
account rather than trusting a number written down here. Every edge door is
priced on this scale in [edge.md](edge.md).

## From the dial to a running cluster

### Sizing the deployment

- Copy `deployment.example.yaml` to `deployment.yaml`. Every AWS-relevant
  field is documented inline, including the worked `cloud_aws:` example block
  near the bottom.
- Estimate `sizing.ingest_gb_per_day` in GB/day, or leave it blank for the
  tyre-kick floor -- the smallest shape that runs the profile without an
  out-of-memory kill, with whatever throughput it happens to carry reported
  rather than targeted. On AWS economy focus with `kafka.provider: msk` the
  resolver's own live run resolves that floor at the smallest shapes the
  resolver accepts, M, with the managed brokers the largest single line:
  `m9g.large` for eks-system, `m9g.xlarge` for general, `r9gd.large` for
  clickhouse, `m9g.large` for keeper, three `express.m7g.large` for
  msk-broker, and `c8gd.4xlarge` for ci-burst. With Kafka running in-cluster
  (`kafka.provider: strimzi`) the same floor loses that line and stays M:
  kafka-broker and eks-system both sit on `m9g.large` -- clickhouse, keeper,
  general and ci-burst are unchanged. That is
  the default combined quorum, which sizes no controller node at all;
  `kafka.controller_pool: separate` adds three `m9g.medium` controllers on
  top of it. Both buckets come from on-demand Linux pricing in the AWS Pricing
  API, read 2026-09-13 -- rates drift, and the resolver's own
  `sizing/<profile>.report.md` (written under `--out`) is the current answer,
  not this paragraph. At the floor the resolver's A4 check WARNS rather than
  fails: `r9gd.large` and `m9g.large` sustain only 3,600 IOPS / 95 MiB/s on
  their EBS baseline and can burst above it for 30 minutes a day, while
  ClickHouse and Keeper write continuously. The floor is the smallest shape
  that runs, not a throughput target -- set `sizing.ingest_gb_per_day` once
  you know it, so the resolver sizes for the workload instead of the minimum.

### Resolving sizing artefacts

- Run `python3 scripts/resolve_sizing.py --dial deployment.yaml --live
  --out terraform/environments/aws` (or `--fixtures
  scripts/tests/fixtures/sizing --cloud aws` against a captured catalogue, no
  AWS call -- capture one first for a region other than us-west-2 with
  `resolve_sizing.py capture --region <region> --fixtures <dir>`). Every
  artefact lands under `--out` (default the repo root when omitted):
  `<out>/shapes/resolved/aws-<region>.json` (the instance-type answer, keyed
  by region because instance-generation availability is not one worldwide),
  `<out>/sizing.auto.tfvars.json` at the ROOT of `--out` (`node_pools` and
  `resolved_shapes`, nothing the root does not declare -- named so, never
  `<tier>.auto.tfvars.json`, so it sorts after `render_dial.py --tofu`'s own
  `dial.auto.tfvars.json` whatever tier was resolved). The resolver is the
  ONE writer of both variables: it starts from the dial's own
  `node_pools.system` block (the always-on group a deployer sizes by hand)
  and merges the pools it derives on top, and `render_dial.py --tofu` emits
  neither key at all -- so the two producers never collide on the same
  tfvars variable. Under
  `<out>/sizing/`: `<tier>.values.yaml` (only the keys the ClickHouse and
  Kafka charts read), `<tier>.report.md` (what was sized, from which ratio,
  in which cost bucket) and `resolved.yaml` (the baseline the next resolve diffs
  against) -- plus `<tier>.nodes.json` on an on-prem resolve, the demand
  `bootstrap.sh`'s preflight checks the cluster's real nodes against.
- Pointing `--out` at `terraform/environments/aws` is what lands
  `sizing.auto.tfvars.json` beside `render_dial.py --tofu`'s own
  `dial.auto.tfvars.json`, so `tofu init` picks up both -- every
  `*.auto.tfvars.json` in that directory loads, in name order. Commit
  `sizing/resolved.yaml` so a re-size has something to check itself against.
  The values fragment slots into a deploy repo's own overlay for the
  data-layer charts.
- `bootstrap.sh` reads `<out>/sizing/` from the same place, so resolving with
  the `--out` above needs nothing else said. A resolve run anywhere else has to
  set `DFE_SIZING_DIR` to that directory. On a cluster running Karpenter,
  bootstrap REFUSES when `<tier>.karpenter.json` is not there: the chart's own
  empty default renders no NodePool, the Application still reports Synced and
  Healthy, and every workload the fixed managed node groups cannot fit stays
  Pending with nothing reporting why.

### Sizing knobs and locks

- `sizing.focus` buys headroom over the sized peak: `economy` (40%),
  `balanced` (60%) or `performance` (100%). None of the three trade away RF3
  or `min.insync.replicas=2`, and none of them decides where the KRaft
  metadata quorum runs -- that is `kafka.controller_pool` below.
- `sizing.overrides` is a deployer's per-workload override block, applied
  last, over whatever the ratios derived -- and still checked against every
  storage-cap assertion, so an override is a different answer, not an
  exemption.
- `sizing.yaml` names six fields locked once resolved -- partition count, the
  storage model, MSK Standard against Express, combined against separate
  KRaft controllers (`kafka.controller_pool`), the cloud itself and the
  availability-zone count. Every one of them is written into
  `sizing/resolved.yaml`, so a re-resolve that moves any of them refuses and
  exits 3 unless you also pass
  `--previous sizing/resolved.yaml --migrate`.
- Above 100,000 GB/day the resolver refuses outright and points at a
  professional-services engagement rather than a generated profile.

### Applying the plan

- Render the tofu inputs with `python3 scripts/render_dial.py --tofu`, which
  writes `terraform/environments/aws/dial.auto.tfvars.json` from the dial's
  `target.provision` block. Then, from that directory:
  `tofu init`, `tofu plan -out=deployment.tfplan`,
  `tofu apply deployment.tfplan`. `init` reads the state bucket, key and
  region straight out of the tfvars file, so there is no separate
  `-backend-config` to keep in step.
- The root creates the VPC and its subnets, the EKS cluster and its node
  groups, the MSK cluster when the dial asks for one, the deployment's KMS
  key, the Route 53 zones and the Secrets Manager store -- and nothing that
  runs inside Kubernetes. The deploying principal's EKS access entry uses
  `data.aws_iam_session_context.issuer_arn`, so an SSO permission set, an
  assumed role, an instance profile or a Lambda role all work; the entry
  names the IAM role -- with its `aws-reserved/sso.amazonaws.com/<region>/`
  path for SSO -- not the STS session ARN.
- A managed broker builds BESIDE the cluster, not after it. The Kafka module
  takes the network, the cluster name, the KMS key and the Pod Identity trust
  policy as explicit inputs, and the key's ARN carries the ordering against the
  key policy that a broker's first log delivery needs, so nothing holds the
  broker behind the control plane, the node groups or the addons. The broker is
  still the longest resource in the deployment, so it sets when the apply ends.
- `tofu` writes NO outputs to the state until the whole apply finishes, so
  `--from-terraform` cannot read them while the broker is still creating and
  the deploy below waits for the apply to end even though Layer 0 and Layer 1
  touch no broker at all. Two applies are the way round it -- one
  `-target=module.cluster` to land the cluster outputs, then the full apply --
  but the broker's own annotations are only written by a bootstrap run that
  happens after it exists, so that route costs a second bootstrap pass. Take
  the single apply unless the second pass is worth it to you.
- `eval "$(tofu output -raw kubeconfig_command)"` gets you `kubectl`. The
  driver is `python3 scripts/dfe-ops stack-deploy --stack <version> --mode
  scale --from-terraform terraform/environments/aws --kubeconfig <path>` --
  the same one the k8s team deploys with. It assembles the bootstrap env
  from the tofu outputs, sets the stack version, runs the offline
  pin/drift/render preflight, then `bootstrap.sh`, the readiness gate and
  the two default end-to-end tests; `--check-only` runs the preflight
  alone, with no cluster contact. `bootstrap/bridge.py` is the reader it
  imports, not a command an operator runs.
- The AWS path is an rc.14 deployment: the pins it needs -- karpenter, the
  load balancer controller, `aws-msk-iam-auth`, the toolbox images, the AWS
  tofu providers -- live in the `2.2.0-rc.14` stack block, which sits on
  the rc.14 cut branch until that cut merges to `main` and moves `current`.
  So deploy with `--stack 2.2.0-rc.14` from a checkout carrying that block;
  on a tree whose `current` is still rc.13 the drift check prints NOTE
  lines for those keys instead of failing, and `dfe-ops bastion up` refuses
  because the toolbox tool versions are empty.
- Set `DFE_VAULT_ADDR` / `DFE_VAULT_ROLE_ID` only when `secrets.backend` is
  `openbao`. `k8s.repo_url` is this repo -- dfe-infra, which stays private
  until GA -- so `DFE_REPO_TOKEN` (an HTTPS token with read access;
  `bootstrap.sh` turns it into the Argo repository credential) is required
  today: without it every Layer 2 Application stays `Unknown` with
  "authentication required". `DFE_PULL_SECRET_TOKEN` (a GHCR pull token) is
  required the same way, for the dfe-* app images -- export both, or carry
  them in an `--env-file`.
- When `k8s.storage_class` names no class the cluster already has,
  `bootstrap.sh` creates it -- provisioner `ebs.csi.aws.com`, gp3 baseline
  3,000 IOPS / 125 MiB/s, encrypted with the account's default EBS key,
  `WaitForFirstConsumer` -- because EKS 1.30+ ships only `gp2` with no default
  class and the CSI add-on creates none itself. It is marked the cluster
  default only when the cluster has none, so an existing default is left in
  place. Per-use-case classes are a follow-on. The same run's internal-CA persist
  step renders the gateway chart with the identical value files Argo layers
  afterwards: `argocd/values/common.yaml`, `argocd/values/<cloud>.yaml`,
  `argocd/values/profile-<profile>.yaml`.
- The AWS Load Balancer Controller is given `vpcId` (the `DFE_VPC_ID`
  output, carried on the cluster secret as `dfe.hyperi.io/vpc_id`) and
  `region` explicitly rather than discovering them itself; node IMDS hop
  limit stays at 1.

## Kafka and ClickHouse

### Kafka

- `kafka.provider: msk` is the only managed Kafka the AWS root builds today.
  It runs MSK Express brokers -- Express only, because MSK cannot convert a
  Standard cluster to Express later -- authenticated both ways: SASL/SCRAM
  for every DFE app, and SASL/IAM for the in-cluster bootstrap Job alone,
  since Kafka ACLs live in the data plane and tofu cannot write the first
  one. That Job creates the landing and DLQ topics and the SCRAM principal's
  ACLs, and exits non-zero on any failure.
- MSK's broker-count scaler, its telemetry sink and its rebalancing
  behaviour are operational detail, not part of the provider choice -- see
  [Kafka telemetry and autoscaling](aws-operations.md#kafka-telemetry-and-autoscaling)
  in aws-operations.md.

### Managed and SaaS Kafka bodies

- `confluent-cloud` and `redpanda-cloud` module bodies exist under
  `terraform/modules/managed-kafka/`, contract-tested like `msk/`, and the
  AWS root now calls whichever one `kafka.provider` names -- all five
  tokens validate on both the resolver and the tofu root (`strimzi`,
  `redpanda`, `msk`, `confluent-cloud`, `redpanda-cloud`), each body behind
  its own `count`. Neither SaaS body sizes, versions or scales itself from a
  shape the resolver picks, but both take the same tuning `msk` does
  (`num_partitions`, `log_retention_ms`,
  `message_max_bytes`, `landing_topics`, since neither runs a bootstrap Job of
  its own to pre-create topics); `redpanda-cloud` additionally takes the
  shared SCRAM password and the ephemeral-lifecycle `allow_deletion` flag.
  Only `name`, `env` and `network` are true of every body regardless of
  provider. The vendor API credential is a provider environment variable,
  never a tfvar: `CONFLUENT_CLOUD_API_KEY` / `CONFLUENT_CLOUD_API_SECRET`
  for Confluent, `REDPANDA_CLIENT_ID` / `REDPANDA_CLIENT_SECRET` for
  Redpanda Cloud -- export both before `tofu plan`, since the `redpanda`
  provider checks for its pair at configure time regardless of which
  `kafka.provider` is selected. Confluent's default tier (Freight) is a
  Private Network Interface, not PrivateLink, and caps `max.message.bytes`
  at 8 MiB, under the chain's 16 MiB. Redpanda Cloud's only creatable
  tier is Serverless, over a PrivateLink-style endpoint, and its SCRAM
  password reuses MSK's own seed. Hand-written Terraform for both still lives
  in `docs/deployment/kafka/confluent-cloud.md` and `redpanda-cloud.md`.
- `strimzi` runs in the cluster exactly as it does anywhere else. Its broker
  autoscaling is a KEDA `ScaledObject` on the `KafkaNodePool` `/scale`
  subresource, triggered on consumer lag by default, with scale-in off until
  proven safe.

### ClickHouse

- ClickHouse's storage model dial (`sizing.storage_model`) defaults to `auto`:
  object store by default wherever an endpoint exists, `local` is the explicit
  opt-out. On AWS that means `cached-object`: parts sit in the S3 bucket
  `terraform/modules/kubernetes-cluster/aws/object-store.tf`
  provisions, authenticated through its own Pod Identity role rather than a
  static key, behind a local read-through cache. The AWS ClickHouse shape pins a
  local-NVMe family (`r*d`, generation floor 8), and the resolver sizes a
  dedicated instance-store cache disk for it and points
  `clickhouse.objectStore.cache.volume` at `instance-store`, so the cache runs
  on that NVMe rather than sharing the data PVC -- see
  [storage.md](storage.md) for the mechanism and its default (`pvc`, bounded
  at 60% of the data PVC's space) for any shape with no such NVMe.
- Retention starts from a 24-hour assumed consumer downtime on the sized
  (`scale`) tier, plus any `archiver_lag_hours`; `single` and `slim` are not
  sized and keep the chart's fixed 72-hour profile. DLQ topics hold 7
  days (168 hours) regardless of tier.

## Receiver ingress

DFE pushes terabytes a day through the receiver, and every AWS front door
bills by volume: a Network Load Balancer charges per LCU-hour (1 GB
processed), a Classic ELB per GB, CloudFront and Global Accelerator per GB,
WAF per request. A volume-priced front door on that kind of traffic is not
an option, so the receiver ships with none.

The default is `edge.ingest.receiver.mode: vpn`: the receiver stays on a
ClusterIP with no public address, and there is no external path until the
deployer brings one. [edge.md](edge.md) carries the AWS tier table -- every door,
its group, its tier, the dial key that turns it and its cost bucket -- and the
three ways to reach the receiver.

An NLB bills an hourly charge plus a per-LCU-hour charge, one LCU-hour being
1 GB processed for TCP/UDP (verified 2026-09-15 against AWS's Elastic Load
Balancing pricing page). The hourly half is XS. The processing half is why there
is no load balancer by default. AWS data transfer out still applies to the
replies on every path, culvert included.

See [Nodes, the edge and lifecycle](aws-operations.md) in aws-operations.md
for node pools and images, admin UI exposure, teardown, lifecycle tags and
upgrades.
