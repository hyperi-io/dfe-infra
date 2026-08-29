# dfe-infra architecture

dfe-infra is the DEPLOYMENT VEHICLE of the DFE product suite: the Helm
charts, Argo CD ApplicationSets, and bootstrap that put DFE onto a bare
Kubernetes cluster. It is the pinned base every deployment starts from. It
never writes config - the engine writes overlays and DDL into the deploy
repo, and this repo's appsets reconcile them; never the reverse.

## Where this repo sits in the suite

```mermaid
flowchart LR
    ENG[dfe-engine<br/>config control plane] -->|compiled values + DDL| DREPO[(deploy repo)]
    INF[dfe-infra<br/>this repo] -->|bootstrap + charts + appsets| ARGO[DFE's Argo CD<br/>dfe-system]
    DREPO --> ARGO
    ARGO -->|helm, base + overlay| APPS[backing services +<br/>dfe-* Rust apps + UIs]
```

The hard boundary (suite invariant): **dfe-infra deploys, dfe-engine
configures.** The engine never installs backing services or authors Argo
Applications; this repo never reads or writes engine config. The
whole-suite map lives in the dfe-engine repo (`docs/architecture.md`
there) - this page covers only the infra side.

One named exception to "this repo owns its charts": **HyperDX is
controlled by the dfe-engine repo** (SSoT - it integrates most closely).
Anything HyperDX needs doing - the fork, the image, this repo's hyperdx
chart - goes through a GitHub issue on dfe-engine first. If it is
time-critical, security-urgent, or a small change that clearly cannot
impact dfe-engine, make it here directly and raise a dfe-engine issue
noting it was done, for politeness.

## What this repo owns

| Area | Where | What it does |
|---|---|---|
| Bootstrap | `bootstrap/` | bare-cluster Layer 0: detect-or-install for StorageClass, cert-manager, ESO, DFE's own Argo CD |
| ApplicationSets | `argocd/appsets/` | layer1 addons, layer2 data + apps (git-files fan-out over the deploy repo's `values/*-values.yaml`), scale-tier operators |
| Tier values | `argocd/values/` | `profile-{slim,single,scale}.yaml` + `common.yaml` + per-cloud overlays |
| App charts | `helm/charts/` | one base chart per dfe-* service + backing services (clickhouse-cluster, kafka, cnpg-cluster, ferretdb, hyperdx, forgejo, kafbat, otel-collector) |
| Chart library | `helm/library/dfe-common` | shared templates (image, KEDA scaledobject, names) |
| Version pins | `versions.yaml` | single source for chart/operator/image versions |
| Cloud prep | `terraform/` (OpenTofu-first, terraform-compatible) | secrets/IAM prep per cloud target |

## Swappable components (the modularity contract)

The initial build is native-k8s-only, but every major component sits behind
a config-driven seam so a deployment swaps it without redesign. RKE2 is the
standard cluster for all deployments (on-prem and cloud) unless a deployment
has a strong reason otherwise. The seam is always an existing k8s
abstraction or a chart mode -- never a fork of the charts.

| Component | Seam | Knob | Swap targets |
|---|---|---|---|
| Cluster | vanilla-cluster contract | (bootstrap preflight) | RKE2 standard; EKS-class only with cause |
| DNS | external-dns / estate-managed | provider config; or the deployment's own DNS | CoreDNS record (on-prem), Route53, ... |
| Private CA / TLS | cert-manager ClusterIssuer | `tls.acme.*` or `tls.vault.*` (exactly one) | ACME/Let's Encrypt, Vault/OpenBao PKI; cloud CA issuer later |
| Secrets manager | ESO ClusterSecretStore | bootstrap store template + env | OpenBao/Vault, AWS SM (+IRSA), ... |
| Kafka | chart mode + endpoint | `kafka` chart mode, `kafka.bootstrapServers` | in-k8s single/Strimzi cluster, MSK, Redpanda (licence gate) |
| ClickHouse | chart mode + endpoint | `clickhouse.mode` single/cluster/external, `clickhouse.host` | in-k8s, ClickHouse Cloud SaaS, private cloud |
| Document store | mongo-protocol endpoint | ferretdb chart (own embedded documentdb PG -- the stack's only postgres) | bundled FerretDB, external mongo-protocol service |
| Edge / LB | Gateway API + EnvoyProxy CR | `gateway.service.*` (type, class, annotations) | MetalLB, cloud NLB, NodePort behind HW LB |
| Deploy repo | provider mode | `DFE_BUNDLED_DEPLOY_REPO` + creds | bundled Forgejo, GitHub/GitLab |

A new architectural dependency must name its seam in this table before it
lands; a component reachable only through one provider is a bug.

Two files anchor the seams (the hyperi-ci versions.yaml pattern):
`versions.yaml` holds every pin, and `argocd/values/common.yaml` holds every
shared deploy-config fact -- including the canonical `hostnames:` map
(role-named service subdomains: `dfe`, `hyperdx`, `auth` for the issuer,
`argocd`, `git`, `kafbat`, `links`, `otel`) that the gateway routes, links
page and dfe-ui embed URLs all resolve from. A shared fact defined outside
these two files is config sprawl and gets consolidated on sight.

## Dead-letter queues

Every k8s-deployed app dead-letters to a Kafka topic dfe-infra creates:
`dfe_receiver_dlq`, `dfe_loader_dlq`, `dfe_archiver_dlq`,
`dfe_fetcher_dlq`, and one shared `dfe_transform_dlq` for the transforms
(a transform overrides only when it genuinely needs its own). Kafka is
the DEFAULT DLQ target -- the file fallback cannot work under the charts'
read-only rootfs and silently drops. dfe-docker: DLQs are opt-IN per
component, EXCEPT the single-node full deploy, which carries the same
DLQ topics as k8s.

The kafka chart's bootstrap-topics seam (`kafka.dlqTopics`) pre-creates
the five topics on both tiers (cluster tier: Strimzi provider only -- the
redpanda provider creates no topics on either path) -- a DLQ write
happens AT failure time, the one moment nothing can be creating topics.
DLQ topics carry 7-day retention against the 72h data-topic default: a
poisoned message is exactly the record an operator must still find days
later, and once its source offset commits it exists nowhere else. The
four fleet apps are pointed at their topic by chart env (the
fleet-uniform `DLQ_TOPIC` / `DLQ_MODE` contract, each in the app's own
env regime); the transforms cannot consume `dfe_transform_dlq` yet
(dfe-transform-vrl#30, dfe-transform-vector#46 own that wiring).

## The overlay seam (how engine dials land here)

One mechanism: layer2-apps generates one Argo Application per
`values/<service>-<instance>-values.yaml` in the deploy repo, multi-source -
source 1 the pinned base chart here, source 2 the overlay file layered last
via `$values`. Adding/removing a values file adds/removes the app; the
engine turns dials, this repo defines what dials exist (every dial must be
a chart value).

> **Note:** the 2nd-pass review found the `$values` reference currently
> resolves to the values directory rather than the matched file - see the
> review report for the one-line fix before relying on overlay dials.

## Documents in this area

| Doc | Covers |
|---|---|
| [deployment/index.md](deployment/index.md) | layers, tiers, values cascade, bootstrap contract |
| [deployment/rke2.md](deployment/rke2.md) | RKE2, the hardened default distribution |
| [deployment/kafka/](deployment/kafka/README.md) | managed-Kafka guides + Redpanda licence gate |
| [deployment/clickhouse.md](deployment/clickhouse.md) | CH target matrix + operator history (official operator / ClickHouse Cloud; private-cloud swap; Altinity untested) |
| [deployment/deployment-logs/](deployment/deployment-logs/TEMPLATE.md) | per-deploy log template |
| [TESTING-CYCLE.md](TESTING-CYCLE.md) | THE validation loop (preflight -> deploy+E2E -> smoke -> destroy), the env-file contract, the vanilla-cluster contract |
| [AUTOSCALING.md](AUTOSCALING.md) | KEDA pod scaling vs the per-target node-autoscaler fork decision |
| [EDGE-AUTH.md](EDGE-AUTH.md) | web-plane exposure + auth: every UI external by default, route class (product/infra/ingest), the infra kill switch, edge OIDC + group RBAC, per-surface policy |
| [INGEST-EDGE.md](INGEST-EDGE.md) | the dfe-receiver data door: public LoadBalancers vs the Gateway route, source ranges, why an empty allow-list is the whole internet |
| [archive/](archive/) | the 2026-03 research corpus + superseded material (decision trail) |

DEVEX-LIFECYCLE.md and DEVEX-OPERATIONS.md remain at docs/ root pending
relocation to the private ops repo (they describe one vendor deployment,
not the product).
