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
  Every OTHER workload the resolver sizes (kafka-broker, kraft-controller,
  clickhouse, keeper) gets BOTH a fixed EKS managed node group, sized to the
  resolved floor, AND a Karpenter pool over the same instance families for
  elastic capacity above it -- the two are not an either/or split; the fixed
  group is what a stateful workload keeps even if Karpenter cannot place a
  replacement. Every Karpenter pool is arm64, and its generation floor sits
  one generation below the oldest generation in its resolved instance family
  list, so a new Graviton generation arrives as drift, with no plan change.
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

- dfe-ui is public by default on a cloud deploy; every admin UI (Argo CD,
  Kafbat, HyperDX, Forgejo, the links page, Cruise Control) is opt-in, one at
  a time (`ui.public.*`). A public UI with no authentication fails the
  render -- dfe-ui's own login counts, as does edge OIDC or an admin app's
  own scheme (Kafbat's OIDC, HyperDX's session cookie); nothing else does.
  The dial's own `ui:` block (`deployment.yaml`) carries these same names
  verbatim (deliberately, unquoted booleans included) so it is a COPY target,
  not something the renderer reads: `render_dial.py` validates every `ui.*`
  boolean by name and reports which UIs the dial marks public, but the
  values that actually reach the chart are whatever a deploy-repo overlay
  (or `argocd/values/<cloud>.yaml` directly, as below) pastes in from it.
- Separately from that per-UI flag, `argocd/values/aws.yaml` sets the
  envoy-gateway-config chart's `exposure.infraUisExternal: false` and
  `oidc.enabled: true` by default, because this cloud's Envoy Gateway Service
  is `internet-facing` (an NLB with a public address). `infraUisExternal` is
  the class-wide kill switch: false takes every infra-class route (Argo CD,
  Kafbat, HyperDX, Forgejo, the links page, Cruise Control) off the edge
  outright, and beats a route's own `enabled: true`. The chart's own render
  guard refuses to bring that switch back on for an internet-facing Service
  unless `oidc.enabled` is true or `ui.allowed_cidrs` is set -- so re-exposing
  an admin UI on this cloud is a deliberate overlay change, not a one-line
  flip that quietly ships with no edge auth.
- `ui.allowed_cidrs` is opt-in and enforced twice, at Envoy and at the load
  balancer's `loadBalancerSourceRanges`. Set it alongside
  `ui.trusted_proxy_cidrs`, or the client address comes from a header the
  caller writes. **It fences the WHOLE front door, not just the admin UIs.**
  One Envoy Gateway Service carries every listener -- product, admin and
  ingest alike -- so an allow-list narrow enough to keep an admin UI locked
  down also blocks agent ingest (`otel`, `receiver`) from anything outside
  it. There is no separate Service for ingest today; widening the allow-list
  until ingest works widens the UI filter with it. The default rate limit is
  300 requests a minute, counted locally per proxy replica. `ui.waf.mode`
  only ever renders `none` -- anything else would move the public
  certificate to the cloud's own store. A public hostname needs
  `dns.public_zone` and gets a Let's Encrypt certificate by DNS-01, through
  cert-manager's own Pod Identity role.

## Teardown and lifecycle tags

- `tags.lifecycle: ephemeral` (tyre-kick, provision-test-destroy) is the one
  governance tag that changes what gets built, not only what gets labelled.
  It sets the CloudTrail bucket's `force_destroy`, the secret recovery
  windows on both the deployment's own secrets and the Kafka SCRAM
  credential (0 days instead of 30), and redpanda-cloud's `allow_deletion`
  (true instead of the vendor's own default refusal) -- so a tyre-kick
  deployment tears down cleanly and anything else keeps the vendor's normal
  deletion protection, without a per-resource flag to remember.
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
