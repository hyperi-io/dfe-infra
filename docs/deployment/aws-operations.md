<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/deployment/aws-operations.md
Purpose:   Operator guide for running a DFE cluster on AWS once it exists --
           node pools and images, edge exposure, Kafka telemetry and
           autoscaling, teardown, and upgrades.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# DFE on AWS -- Operations

Provisioning (the dial, sizing, Kafka and ClickHouse choices):
[aws.md](aws.md).

## Kafka telemetry and autoscaling

- MSK's own broker-count scaler is a CloudWatch alarm on `BytesInPerSec`
  driving a Lambda, wired from `kafka.msk.autoscaling`. The alarm reads the
  metric with the `Average` statistic, not `Sum` -- `BytesInPerSec` is
  already a per-second rate, so summing the five one-minute samples a 300s
  period holds would inflate the reading roughly fivefold against the
  bytes-per-second threshold it is compared to. The telemetry dial
  (`telemetry.aws.sink`, default `otel`) governs the rest: broker logs land
  in an S3 bucket the fetcher reads, and only the opt-in `cloudwatch` sink
  keeps them in CloudWatch instead -- see the Telemetry section of
  `docs/deployment/kafka/aws-msk.md` for the cost table. `open_monitoring`
  is always scrape-only, read by the OTel collector gateway. Intelligent
  rebalancing is turned on, so a broker added later gets existing
  partitions moved onto it.

## Nodes and images

- The small always-on managed group (`node_pools.system` in the dial) carries
  only Karpenter itself and the AWS Load Balancer Controller, both installed
  at the same early sync wave -- neither can allocate anything until it runs.
  Every OTHER workload the resolver sizes (kafka-broker, clickhouse, keeper,
  and kraft-controller only where `kafka.controller_pool` is `separate`) gets
  BOTH a fixed EKS managed node group, sized to the resolved floor, AND a
  Karpenter pool over the same instance families for elastic capacity above
  it -- the two are not an either/or split; the fixed group is what a stateful
  workload keeps even if Karpenter cannot place a replacement. Every Karpenter
  pool is arm64, and its generation floor sits one generation below the oldest
  generation in its resolved instance family list, so a new Graviton
  generation arrives as drift, with no plan change.
- Karpenter's node image is `amiAlias: al2023@v20260827`
  (`helm/charts/karpenter-pools/values.yaml`, `operators.karpenter-al2023-ami`
  in `versions.yaml`, drift-checked) -- a dated AL2023 release, not `latest`,
  so a new image arrives as a `versions.yaml` bump through the usual 7-day
  cooldown rather than as unwatched drift on every reconcile. A deployment
  under a different image policy overrides `karpenter.nodeClass.amiAlias` in
  a values overlay rather than editing this chart's default.
- Every pinned image is checked for both `linux/amd64` and `linux/arm64`
  manifests (`scripts/check_image_arch.py`) -- an image missing either pulls
  fine and only fails at container start. The one exception the stack works
  around rather than ships: the Cruise Control UI's only credible
  third-party image is amd64-only, so DFE fetches its static release
  tarball at pod start instead of running that image at all.

## Admin UI exposure

[edge.md](edge.md) owns which surface is exposed on which flavour, at which
tier, by which dial key. What is AWS's own:

- `argocd/values/aws.yaml` sets `oidc.enabled: true`, and it stays with the
  cloud overlay rather than the edge one because kafbat and dfe-engine read the
  same switch.
- The class-wide kill switch (`edge.admin_uis.external`) is off here because
  this cloud's Envoy Gateway Service is internet-facing. The chart's render
  guard refuses to bring it back on for an internet-facing Service unless edge
  OIDC is on or an allow-list is set, so re-exposing an admin UI is a deliberate
  overlay change rather than a one-line flip that ships with no edge auth.
- A public hostname needs `dns.public_zone` and gets a Let's Encrypt certificate
  by DNS-01, through cert-manager's own Pod Identity role.
- `render_dial.py` validates every boolean and enum in the dial's `edge:` block
  and reports which surfaces it marks public, but the values that reach the
  chart are whatever a deploy-repo overlay pastes in from it.

## Teardown and lifecycle tags

- A full cloud cycle is batched, never run per finding
  ([TESTING-CYCLE.md](../TESTING-CYCLE.md)). Two halves bear on teardown: the
  managed cluster goes down first, being the long pole both ways, and teardown
  starts when the last proof lands, not at the end of a session.
- `tags.lifecycle: ephemeral` (tyre-kick, provision-test-destroy) is the one
  governance tag that changes what gets built, not only what gets labelled.
  It sets the CloudTrail bucket's `force_destroy`, the secret recovery
  windows on both the deployment's own secrets and the Kafka SCRAM
  credential (0 days instead of 30), and redpanda-cloud's `allow_deletion`
  (true instead of the vendor's own default refusal) -- so a tyre-kick
  deployment tears down cleanly and anything else keeps the vendor's normal
  deletion protection, without a per-resource flag to remember. Every `dfe-ops`
  command against an ephemeral dial also opens with one stderr line: the
  resolved `compute_bucket` from `sizing/resolved.yaml` -- a T-shirt bucket,
  never a rate -- plus how long the cluster has been up, from the root's
  `cluster_created_at` output. Nothing in the tree
  dates a deployment -- a local state file's mtime dates the last apply, and
  the S3 backend leaves none here -- so the control plane's own stamp is the
  reading, and a dial with no cluster behind it reports the age unavailable.
  Neither reading may fail the command.
- Tear down with `tofu destroy` in `terraform/environments/aws`, deleting
  the Kubernetes workloads first -- anything that made a load balancer, a
  volume or a DNS record did so through a controller, and `tofu destroy`
  does not know about it. Then run `scripts/cloud_sweep.py --region
  <region>` read-only to see what is left, add `--exclude-bucket <state
  bucket>` so the state store is never a candidate, and only add `--delete`
  once you have read the listing. `--delete` requires `--account <12-digit>`,
  checked against `aws sts get-caller-identity` before anything is touched --
  refused on a mismatch or a missing `--account`, the same guard both tofu
  roots already carry against running against the wrong account. `--delete`
  also prints the full eligible list and asks for a typed `yes` before
  deleting anything; `--yes` skips that prompt for a CI run. `--include-untagged`
  widens the sweep to every resource the listers found, tagged or not, turning
  `--delete` into a region-wide wipe of the account (`--exclude-bucket` is the
  one exemption); without it, `--delete` only ever removes resources carrying
  the tag filter. The AWS
  account defaults -- the default VPC and everything that comes with it,
  AWS-managed KMS aliases -- are never listed or touched.
- Stack, re-size and platform upgrades follow
  [upgrades.md](upgrades.md); the operator order across charts is data,
  not judgement -- `upgrade-order.yaml` at the repo root.
