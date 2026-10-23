<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/deployment/aws-operations.md
Purpose:   Operator guide for running a DFE cluster on AWS once it exists --
           node pools and images, edge exposure, Kafka telemetry and
           autoscaling, and upgrades.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# DFE on AWS -- Operations

Provisioning (the dial, sizing, Kafka and ClickHouse choices): [aws.md](aws.md). Lifecycle tags and teardown: [aws-teardown.md](aws-teardown.md). Stack, re-size and platform upgrades follow [upgrades.md](upgrades.md), and the operator order across charts is data, not judgement -- `upgrade-order.yaml` at the repo root.

## Kafka telemetry and autoscaling

- MSK's own broker-count scaler is a CloudWatch alarm on `BytesInPerSec`
  driving a Lambda, wired from `kafka.msk.autoscaling`. The alarm reads the
  metric with the `Average` statistic, not `Sum` -- `BytesInPerSec` is
  already a per-second rate, so summing the five one-minute samples the
  default 300s period (`kafka.msk.autoscaling.period_seconds`) holds would inflate the reading roughly fivefold against the
  bytes-per-second threshold it is compared to. The telemetry dial
  (`telemetry.aws.sink`, default `otel`) governs the rest: broker logs land
  in an S3 bucket the fetcher reads, and only the opt-in `cloudwatch` sink
  keeps them in CloudWatch instead -- see the Telemetry section of
  `docs/deployment/kafka/aws-msk.md` for the cost table. `open_monitoring`
  is always scrape-only, read by the OTel collector gateway. Intelligent
  rebalancing is turned on, so a broker added later gets existing
  partitions moved onto it.
- The alarm names one CloudWatch metric per broker, and a metric math
  expression takes at most ten, so `autoscaling.enabled` is refused above
  `broker_count: 10`. Refused at plan, because at apply the cluster, the
  Lambda, the SNS topic and the IAM all exist before `PutMetricAlarm` rejects
  it. A larger cluster scales by hand.
- **Building the broker on its own.** `module.kafka` carries no module-wide
  `depends_on`, so `tofu apply -target='module.kafka[0].aws_msk_cluster.this'`
  plans the broker plus the VPC, its subnets and the KMS key and key policy --
  and nothing else. Targeting the whole `module.kafka` also pulls the EKS
  control plane in, because the bootstrap Job's Pod Identity association names
  the cluster and cannot exist without it; the node groups, the addons and
  Karpenter stay out of both. **`-target` and `-exclude` cannot be combined**
  ("The target and exclude planning options are mutually-exclusive"), so an
  MSK-only apply targets the module and excludes nothing.

## Nodes and images

- The small always-on managed group (`node_pools.system` in the dial) carries only Karpenter and the AWS Load Balancer Controller, both at the same early sync wave. Each stateful workload the resolver sizes (kafka-broker, clickhouse, keeper, and kraft-controller where `kafka.controller_pool` is `separate`) gets a fixed EKS managed node group at the resolved floor AND a node pool over the same instance family above it. The fixed group is what it keeps when Karpenter cannot place a replacement. Every pool is arm64 and admits only the family the resolver picked, or the generations a cheapest-of policy compared, so a new Graviton generation arrives with the next resolve.
- Karpenter's node image is a dated `amiAlias: al2023@v<YYYYMMDD>`. `scripts/resolve_sizing.py images` writes the newest release that clears `versions.yaml`'s `node-images` gates to `sizing/node-pools.values.yaml`: commit it, with `sizing/node-images.json`, to the deploy repo's `sizing/`. Without it the chart default `operators.karpenter-al2023-ami` applies. Your own `pools.aws.nodeClass.amiAlias` in `infra/node-pools.yaml` beats both.
- Images are checked for both `linux/amd64` and `linux/arm64` manifests, because an image missing either pulls fine and only fails at container start. `scripts/check_image_arch.py --third-party-only --resolve-charts` does it with no registry credential, on every pin change (`helm-lint.yml`) and in the stack release gate (`release.yml`). `dfe-stack release-gate` reads the DFE images by digest before a release-maturity stack ships. A digest-pinned verdict is cached (`--verdict-cache`), so only unproven references are read.
- Every third-party image a pin tags deploys as tag@sha256, and the check reads it by that digest. Forgejo's curl helper comes from the chart values that pin it, and Dex, MetalLB's FRR pair and Karpenter's digest-pinned controller from their chart's own values at the pinned version. `services-digests.karpenter` and `services.aws-load-balancer-controller-image` move Karpenter's chart and the load balancer controller to digests too, once a stack pins them.
- Two pinned images still deploy by tag: the cluster-mode Redpanda broker, whose operator chart accepts only a bare semver tag, and Strimzi's broker, which its operator resolves.
- Named in a chart render but never pulled, so not checked: Envoy Gateway's rate-limit image (the gateway uses only `rateLimit.local`), cert-manager's ACME HTTP-01 solver (every issuer here solves DNS-01), and Strimzi's Kafka Bridge, Kafka Connect build and other-version Kafka images (no chart renders a KafkaBridge or KafkaConnect, and the Kafka resource names only `services.kafka-version`).
- The one exception the stack works around rather than ships: the Cruise Control UI's only credible third-party image is amd64-only, so DFE fetches its static release tarball at pod start instead of running that image at all.

## Disruption budgets

Each Karpenter pool gets one budget per reason from its class: how many nodes may be terminating at once.

| Pool | Underutilized | Empty | Drifted |
|---|---|---|---|
| kafka-broker, kraft-controller, clickhouse, keeper | 0 | 1 | 1 |
| eks-system, general | 25% | 25% | 10% |
| ci-burst | 100% | 100% | 100% |

- `k8s.disruption.maintenance_window` (a UTC cron `schedule`, daily or one weekday, and a `duration`) holds drift to the window on every pool but ci-burst. Karpenter has no "only inside" budget, so the resolver writes a zero Drifted budget from the window's close to its next open.
- `k8s.disruption.posture: conservative` drops elastic Underutilized to 12% and replaces a stateful node for drift only inside the window. With no window, drift never replaces one.
- Expiry is not budgeted. The elastic pools' 720h `expireAfter` replaces a node whether the window is open or not.

## Registry cache

An ECR pull-through cache in the deployment's own account, which every node pulls its quay.io, registry.k8s.io and public.ecr.aws images through, so those registries' rate limits cannot fail a deploy. None of the three needs a credential. Earlier DFE 2.1 customer deployments ran the same pattern. Design: [registry-cache/CONTRACT.md](../../terraform/modules/registry-cache/CONTRACT.md).

- On by default. `registry_cache.enabled: "false"` in the dial turns it off, and `tofu output registry_cache` lists what is cached.
- docker.io and ghcr.io images pull straight from upstream, anonymously, since no image the stack pulls needs a credential to read. A Docker Official Image comes from its `public.ecr.aws/docker/library/` mirror instead of docker.io, so the cache covers it.
- A publisher's copy on one of the three cached registries wins over docker.io, so the default cache covers it. The Kafka CLI (`kafka.cliImage`) is Strimzi's Kafka image from quay.io. The single-mode broker still runs `apache/kafka`.
- Image references never change: each node writes a containerd mirror per cached host with the upstream as fallback, so a cache that cannot answer costs a retry, never a failed pull. The mirror logs in with the node's own IAM role.
- Turning it on gives each managed node group its first launch template, which replaces the group.
- The deploying identity needs ECR's pull-through cache rule and repository creation template actions, `iam:PassRole` to `ecr.amazonaws.com`, and `iam:CreateServiceLinkedRole` the first time for both `pullthroughcache.ecr.amazonaws.com` and `ecr.amazonaws.com` (the `AWSServiceRoleForECRTemplate` role a repository creation template needs). Where the deploying identity may not create service-linked roles, create both once per account beforehand. A permissions boundary on the roles it creates must allow `ecr:BatchImportUpstreamImage`, `ecr:CreateRepository`, `ecr:TagResource` and `kms:RetireGrant`.
- `code.forgejo.org` and `docker.redpanda.com` have no ECR rule type. Helm charts are fetched by Argo CD and bootstrap, not nodes, and stay on their own registries.
- Destroy deletes every repository ECR created under the deployment's rules.

### Optional: cache docker.io and ghcr.io too

Not part of the default deploy. ECR caches docker.io and ghcr.io only through a Secrets Manager credential, an AWS rule that holds whether the image is public or not. Where their rate limits bite, name one secret per upstream:

```yaml
registry_cache:
  enabled: "true"
  credentials:
    docker-hub: "<secret ARN>"   # a Docker Hub username and personal access token
    ghcr: "<secret ARN>"         # a GitHub username and a classic token with read:packages
```

- Each secret is named under `ecr-pullthroughcache/`, holds `{"username", "accessToken"}`, and is encrypted with the default `aws/secretsmanager` key. ECR reads no other prefix and supports no customer managed key here.
- Either key may be left out. An upstream with no secret gets no rule, and its images pull directly.
- ECR alone reads the secret, when it fetches from the upstream. No node or pod is handed it.

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
