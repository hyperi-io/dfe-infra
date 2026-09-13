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

DFE's AWS path provisions an EKS cluster with OpenTofu, then hands it to the
same bootstrap and Argo CD layers every other cluster uses. Nothing about
Layer 1 or Layer 2 changes for AWS -- the difference is entirely in what
`terraform/environments/aws` builds and what the deployment dial resolves
before it runs.

A few things hold for every deployment on this path:

- ARM only. Every node pool, and the MSK broker shape when one is built,
  resolve to Graviton -- the cluster module refuses a `shape_ref` whose
  resolved architecture is not arm64.
- Nodes and the data plane sit in private subnets. NAT is one gateway for the
  whole network by default (`network.nat: single`), or one per availability
  zone (`per-az`) when a zone failure or the cross-zone data charge matters
  more than the extra NAT gateway.
- The Kubernetes API's private endpoint is always on. `endpoint.public: true`
  adds the public one, and only alongside `endpoint.allowed_cidrs` -- the plan
  refuses a public endpoint with no allow-list.
- A gateway VPC endpoint keeps S3 traffic off the NAT path, at no extra
  charge.
- The account's state bucket is a one-off: run
  `terraform/environments/aws-state` once, before any deployment's own apply.
- Every controller that needs an AWS credential -- the EBS CSI driver,
  Karpenter, the AWS Load Balancer Controller, external-dns, cert-manager --
  gets it through EKS Pod Identity, associated to its own service account by
  the cluster module. Nothing here carries a static key.
- One file drives all of it: the deployment dial, `deployment.yaml`, copied
  from `deployment.example.yaml`.

## From the dial to a running cluster

- Copy `deployment.example.yaml` to `deployment.yaml`. Every AWS-relevant
  field is documented inline, including the worked `cloud_aws:` example block
  near the bottom.
- Estimate `sizing.ingest_gb_per_day` in GB/day, or leave it blank for the
  tyre-kick floor -- the smallest shape that runs the profile without an
  out-of-memory kill, with whatever throughput it happens to carry reported
  rather than targeted.
- Run `python3 scripts/resolve_sizing.py --dial deployment.yaml --live` (or
  `--fixtures scripts/tests/fixtures/sizing --cloud aws` against a captured
  catalogue, no AWS call made -- capture one first for a region other than
  us-west-2 with `resolve_sizing.py capture --region <region> --fixtures
  <dir>`). It writes `shapes/resolved/aws-<region>.json` (the committed
  instance-type answer, keyed by region because instance-generation
  availability is not one worldwide), `sizing/scale.auto.tfvars.json` (node
  pools and resolved shapes, nothing the root does not declare), `sizing/scale.
  values.yaml` (only the keys the ClickHouse and Kafka charts read),
  `sizing/scale.report.md` (what was sized, from which ratio, at what price),
  and `sizing/resolved.yaml` (the baseline the next resolve diffs against).
- Commit `resolved.yaml` so a re-size has something to check itself against.
  The values fragment slots into a deploy repo's own values overlay for the
  data-layer charts; point `resolve_sizing.py`'s `--out` at
  `terraform/environments/aws` and its tfvars fragment lands next to
  `render_dial.py --tofu`'s own `dial.auto.tfvars.json` there, so `tofu init`
  picks up both -- every `*.auto.tfvars.json` in that directory loads, in name
  order.
- `sizing.focus` buys headroom over the sized peak: `economy` (40%),
  `balanced` (60%) or `performance` (100%). None of the three trade away RF3,
  `min.insync.replicas=2` or the separate KRaft controllers -- those hold at
  every focus level.
- `sizing.overrides` is a deployer's per-workload override block, applied
  last, over whatever the ratios derived -- and still checked against every
  storage-cap assertion, so an override is a different answer, not an
  exemption.
- Partition count, the storage model, MSK Standard against Express,
  combined against separate KRaft controllers, the cloud itself and the
  availability-zone count are locked once resolved. A re-resolve that moves
  one refuses and exits 3 unless you also pass
  `--previous sizing/resolved.yaml --migrate`.
- Above 100,000 GB/day the resolver refuses outright rather than
  extrapolating, and points at a professional-services engagement instead of
  a generated profile.
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
  runs inside Kubernetes. `eval "$(tofu output -raw kubeconfig_command)"`
  gets you `kubectl`; `python3 bootstrap/bridge.py --tf-dir
  terraform/environments/aws` reads the rest of the `DFE_*` outputs into the
  bootstrap run.

## Kafka and ClickHouse

- `kafka.provider: msk` is the only managed Kafka the AWS root builds today.
  It runs MSK Express brokers -- Express only, because MSK cannot convert a
  Standard cluster to Express later -- authenticated both ways: SASL/SCRAM
  for every DFE app, and SASL/IAM for the in-cluster bootstrap Job alone,
  since Kafka ACLs live in the data plane and tofu cannot write the first
  one. That Job creates the landing and DLQ topics and the SCRAM principal's
  ACLs, and exits non-zero on any failure.
- MSK's own broker-count scaler is a CloudWatch alarm on `BytesInPerSec`
  driving a Lambda, wired from `kafka.msk.autoscaling`. The telemetry dial
  (`telemetry.aws.sink`, default `otel`) governs the rest: broker logs land
  in an S3 bucket the fetcher reads, and only the opt-in `cloudwatch` sink
  keeps them in CloudWatch instead -- see the Telemetry section of
  `docs/deployment/kafka/aws-msk.md` for the cost table. `open_monitoring`
  is always scrape-only, read by the OTel collector gateway. Intelligent
  rebalancing is turned on, so a broker added later gets existing
  partitions moved onto it.
- `confluent-cloud` and `redpanda-cloud` module bodies exist under
  `terraform/modules/managed-kafka/`, contract-tested like `msk/`, and the
  AWS root now calls whichever one `kafka.provider` names -- all five
  tokens validate (`strimzi`, `redpanda`, `msk`, `confluent-cloud`,
  `redpanda-cloud`), each body behind its own `count`. Neither SaaS body
  takes a tuning input; the root passes only `name`, `env` and `network`,
  and the vendor API credential is a provider environment variable, never a
  tfvar. Confluent's default tier (Freight) is a Private Network Interface
  rather than PrivateLink and caps `max.message.bytes` at 8 MiB, under the
  16 MiB the size chain carries. Redpanda Cloud's only creatable tier is
  Serverless, over a PrivateLink-style endpoint, and its SCRAM password
  reuses MSK's own seed. Hand-written Terraform for both still lives in
  `docs/deployment/kafka/confluent-cloud.md` and `redpanda-cloud.md`.
- `strimzi` runs in the cluster exactly as it does anywhere else. Its broker
  autoscaling is a KEDA `ScaledObject` on the `KafkaNodePool` `/scale`
  subresource, triggered on consumer lag by default, with scale-in off until
  proven safe.
- ClickHouse's storage model on a cloud deploy is `cached-object`: parts sit
  in S3, behind a local read-through cache. The AWS ClickHouse shape pins a
  local-NVMe family (`r*d`, generation floor 8), and the resolver already
  sizes a dedicated instance-store cache disk for it -- but the chart has not
  yet wired a separate cache volume, so today the cache shares the data PVC,
  bounded at 60% of its space by default.
- Retention starts from a 24-hour assumed consumer downtime on the sized
  (`scale`) tier, plus any `archiver_lag_hours`; `single` and `slim` are not
  sized at all and keep the chart's fixed 72-hour profile. DLQ topics hold 7
  days (168 hours) regardless of tier.

## Nodes, the edge and lifecycle

- Karpenter provisions every node pool outside the small always-on managed
  group (`node_pools.system` in the dial) that carries Karpenter itself and
  the AWS Load Balancer Controller before Karpenter can allocate anything --
  both install at the same early sync wave. Every Karpenter pool is arm64,
  and its generation floor sits one generation below the oldest generation in
  its resolved instance family list, so a new Graviton generation arrives as
  drift, with no plan change.
- Every pinned image is checked for both `linux/amd64` and `linux/arm64`
  manifests (`scripts/check_image_arch.py`) -- an image missing either pulls
  fine and only fails at container start. The one exception the stack works
  around rather than ships: the Cruise Control UI's only credible
  third-party image is amd64-only, so DFE fetches its static release
  tarball at pod start instead of running that image at all.
- dfe-ui is public by default on a cloud deploy; every admin UI (Argo CD,
  Kafbat, HyperDX, the links page) is opt-in, one at a time. A public UI
  with no authentication fails the render -- dfe-ui's own login counts, as
  does edge OIDC or an admin app's own scheme (Kafbat's OIDC, HyperDX's
  session cookie); nothing else does.
- `ui.allowed_cidrs` is opt-in and enforced twice, at Envoy and at the load
  balancer's `loadBalancerSourceRanges`. Set it alongside
  `ui.trusted_proxy_cidrs`, or the client address comes from a header the
  caller writes. The default rate limit is 300 requests a minute, counted
  locally per proxy replica. `ui.waf.mode` only ever renders `none` --
  anything else would move the public certificate to the cloud's own store.
  A public hostname needs `dns.public_zone` and gets a Let's Encrypt
  certificate by DNS-01, through cert-manager's own Pod Identity role.
- Tear down with `tofu destroy` in `terraform/environments/aws`, deleting
  the Kubernetes workloads first -- anything that made a load balancer, a
  volume or a DNS record did so through a controller, and `tofu destroy`
  does not know about it. Then run `scripts/cloud_sweep.py --region
  <region>` read-only to see what is left, add `--exclude-bucket <state
  bucket>` so the state store is never a candidate, and only add `--delete`
  once you have read the listing. The AWS account defaults -- the default
  VPC and everything that comes with it, AWS-managed KMS aliases -- are
  never listed or touched.
- Stack, re-size and platform upgrades follow
  [upgrades.md](upgrades.md); the operator order across charts is data,
  not judgement -- `upgrade-order.yaml` at the repo root.
