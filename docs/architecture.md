# dfe-infra architecture

dfe-infra is how DFE gets onto a bare Kubernetes cluster: the Helm charts, Argo CD ApplicationSets and bootstrap. Every deployment starts from its pinned base. It never writes config. The engine writes overlays and DDL into the deploy repo, and this repo's appsets reconcile them. Never the reverse.

## Where this repo sits in the suite

DFE's Argo CD deploys from two git repos: the deploy repo dfe-engine writes, and dfe-infra's charts.

```mermaid
---
config:
  theme: base
  look: neo
  fontFamily: Inter, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif
  flowchart:
    curve: linear
    nodeSpacing: 40
    rankSpacing: 64
    padding: 18
    wrappingWidth: 280
    htmlLabels: true
  elk:
    preset: modelOrder
  themeVariables:
    fontSize: 15px
    lineColor: "#64748B"
    primaryTextColor: "#0F172A"
    clusterBorder: "#0072B2"
    edgeLabelBackground: "#FFFFFF"
  themeCSS: >-
    p{margin:0}
    .node[data-look] rect,.node[data-look] path,.node[data-look] polygon,.node[data-look] .outer-path{filter:drop-shadow(0 1px 2px rgba(15,23,42,.10)) drop-shadow(0 6px 14px rgba(15,23,42,.10))}
    .node rect{rx:10px;ry:10px}
    .cluster rect{rx:16px;ry:16px}
    .node .label div{min-width:150px}
    .node.decision .label div{min-width:0}
    .cluster[data-look] rect{filter:none}
    .nodeLabel{line-height:1.35}
    .nodeLabel b{font-weight:600;font-size:15px;letter-spacing:-0.01em}
    .nodeLabel span{font-size:12.5px;font-weight:400;color:#334155}
    .cluster-label span.nodeLabel{display:inline-block;background:#FFFFFF;color:#0B2A3F;border:1.5px solid #0072B2;border-radius:999px;padding:2px 12px;font-size:11.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}
    .flowchart-link{stroke-width:1.75px}
    .labelBkg{background:transparent!important}
    span.edgeLabel{display:inline-block;background:#FFFFFF!important;color:#1E293B;border:1px solid #94A3B8;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:500}
    span.edgeLabel p{background:transparent!important}
    .label-icon{width:20px;height:20px;vertical-align:-4px;margin-right:6px}
    .ingest .label-icon{color:#0E8A7D}
    .xform .label-icon{color:#7B4FB0}
    .load .label-icon{color:#0072B2}
    .bus .label-icon{color:#A8517F}
    .store .label-icon{color:#00785A}
    .ui .label-icon{color:#D9480F}
    .ext .label-icon{color:#64748B}
---
flowchart LR
    accTitle: L2 Services -- DFE deployment
    accDescr: dfe-engine writes compiled values and DDL to the deploy repo. DFE's Argo CD reads the overlays from the deploy repo and the charts and ApplicationSets from dfe-infra, and applies them with Helm to the backing services, the dfe apps and the UIs.

    engine("<b>dfe-engine</b>")

    deployrepo("<b>Deploy repo</b>")
    infrarepo("<b>dfe-infra</b>")

    argocd("<b>DFE's Argo CD</b>")

    services("<b>Backing services, apps and UIs</b>")

    engine -->|values, DDL| deployrepo
    deployrepo -->|overlays| argocd
    infrarepo -->|charts, appsets| argocd
    argocd -->|helm| services

    class engine,deployrepo,infrarepo,argocd control
    class services part
  classDef ext fill:#F4F6F8,stroke:#94A3B8,stroke-width:1.5px,stroke-dasharray:5 4,color:#0F172A
  classDef ingest fill:#FFFFFF,stroke:#0E8A7D,stroke-width:3px,color:#0F172A
  classDef xform fill:#FFFFFF,stroke:#7B4FB0,stroke-width:3px,color:#0F172A
  classDef load fill:#FFFFFF,stroke:#0072B2,stroke-width:3px,color:#0F172A
  classDef bus fill:#FFFFFF,stroke:#A8517F,stroke-width:3px,color:#0F172A
  classDef store fill:#FFFFFF,stroke:#00785A,stroke-width:3px,color:#0F172A
  classDef ui fill:#FFFFFF,stroke:#D9480F,stroke-width:3px,color:#0F172A
  classDef decision fill:#FFFFFF,stroke:#9A6700,stroke-width:3px,color:#0F172A
  classDef error fill:#FFFFFF,stroke:#C2410C,stroke-width:3px,color:#0F172A
  classDef control fill:#FFFFFF,stroke:#334155,stroke-width:3px,color:#0F172A
  classDef part fill:#FFFFFF,stroke:#94A3B8,stroke-width:1.5px,color:#0F172A
```

*Figure 1. L2 Services -- DFE deployment.* Up: top of dfe-infra. Down: [L4 Detail -- Deployment layers](deployment/README.md#the-three-layers), [L4 Detail -- The deploy repo](deployment/state-and-repos.md#the-repositories), [L4 Detail -- Edge exposure](edge-auth.md#exposure-topology), [L4 Detail -- Federated login](edge-auth.md#the-identity-plane-engine-authority--external-oidc).

Bootstrap installs DFE's Argo CD (release `argocd` in namespace `argocd`) before any of this runs, or adopts a healthy Argo CD the cluster has.

The suite rule is **dfe-infra deploys, dfe-engine configures.** The engine never installs backing services or authors Argo Applications. This repo never reads or writes engine config. The whole-suite map lives in the dfe-engine repo (`docs/architecture.md` there). This page covers the infra side.

One exception to "this repo owns its charts": dfe-engine is the controlling repository for dfe-hyperdx, the search fork embedded in the DFE console.

This repo owns:

| Area | Where | What it does |
|---|---|---|
| Bootstrap | `bootstrap/` | bare-cluster Layer 0: detect-or-install for StorageClass, cert-manager, ESO, Argo CD |
| ApplicationSets | `argocd/appsets/` | layer1 addons, layer2 data + apps (git-files fan-out over the deploy repo's `values/*-values.yaml`), scale-tier operators |
| Tier values | `argocd/values/` | `profile-{slim,single,scale}.yaml` + `common.yaml` + per-cloud overlays |
| Charts | `helm/charts/` | backing services (clickhouse-cluster, kafka, documentdb, forgejo, kafbat, otel-collector), platform charts and dfe-extras. Each dfe-* service deploys from its published thin chart |
| Chart library | `helm/library/dfe-common` | shared templates for the charts in this repo (image, names, labels, security context, the Kafka provider table, the mesh pool listener, the wait-for-engine init container) |
| Version pins | `versions.yaml` | single source for chart/operator/image versions |
| Cloud base | `terraform/` (OpenTofu) | the per-cloud base: the EKS cluster, managed brokers, secrets and IAM |

## Swappable components (the modularity contract)

Why it works this way is in [design principles](design-principles.md#your-cluster-your-choice-of-parts). This table is where each part gets switched in this repo.

| Component | Switch | Dial | Swap targets |
|---|---|---|---|
| Cluster | vanilla-cluster contract | (bootstrap preflight) | RKE2 (deployed onto, never built here, #1123) and EKS (built by the aws root), tested. AKS and GKE Standard, values overlays alone |
| DNS | external-dns / estate-managed | provider config, or the deployment's DNS | CoreDNS record (on-prem), Route53, ... |
| Private CA / TLS | cert-manager ClusterIssuer | `tls.acme.*` or `tls.vault.*` (exactly one) | ACME/Let's Encrypt, Vault/OpenBao PKI, cloud CA issuer later |
| Secrets manager | ESO ClusterSecretStore | bootstrap store template + env | OpenBao/Vault, AWS SM (+EKS Pod Identity), ... |
| Kafka | chart mode + endpoint | `kafka` chart mode, `kafka.bootstrapServers` | in-k8s single/Strimzi cluster, MSK, Redpanda (licence gate) |
| ClickHouse | chart mode + endpoint | `clickhouse.mode` single/cluster/external, `clickhouse.host` | in-k8s, ClickHouse Cloud SaaS, private cloud |
| Document store | mongo-protocol endpoint | documentdb chart (a DocumentDB CR on the DocumentDB operator's CloudNativePG -- the stack's one postgres), `mongodb.uri` in the hyperdx values | bundled DocumentDB, external mongo-protocol service |
| Edge / LB | Gateway API + EnvoyProxy CR | `envoyGateway.service.*` (type, class, annotations, externalIPs) | MetalLB, cloud NLB, NodePort behind HW LB, externalIPs with no LB controller |
| Deploy repo | provider mode | `DFE_BUNDLED_DEPLOY_REPO` + creds | bundled Forgejo, GitHub/GitLab |

Two files hold the shared facts (the hyperi-ci versions.yaml pattern). `versions.yaml` holds every pin. `argocd/values/common.yaml` holds every shared deploy-config fact, including the canonical `hostnames:` map (service subdomains by role: `dfe`, `hyperdx`, `argocd`, `git`, `kafbat`, `cruiseControl`, `links`, `otel`, `receiver`, `vpn`) that the gateway routes, links page and dfe-ui embed URLs all resolve from. A shared fact defined anywhere else is config sprawl, and we merge it into one of those two.

## Schema and topics: dfe-engine is the controller

Nothing in this repo creates, alters or drops a ClickHouse database, table, view or role, or a Kafka topic. `dfe-schemas` declares every one of them and dfe-engine applies them at startup, from the wheel pinned inside its image, on both tiers and every broker provider. A CI guard (`scripts/tests/test_engine_only_schema_control.py`) fails the build if DDL or a topic-creation step turns up under `helm/`, `argocd/`, `bootstrap/`, `scripts/` or `terraform/`.

> **Coming from Splunk or Elastic?** You don't set up indexes or mappings on these clusters. dfe-engine applies the schema from the versioned `dfe-schemas` definition when it starts, and the CI guard fails the build if this repo tries to. You change a schema by changing its definition, never by editing a table in place. More in [what's different](../docs-site/content-root/coming-from.md).

What that leaves this repo:

- The clusters themselves. ClickHouse and Kafka are deployed, sized, backed up and upgraded here. The schema inside them moved to the engine.
- The managed-broker ACLs. `terraform/modules/managed-kafka/` writes the DFE principal's grants on confluent-cloud and redpanda-cloud, topic Create among them, and creates no topic. dfe-engine creates those topics at startup like every other provider's.
- The MSK first ACL. `msk-bootstrap-job.yaml` writes the SCRAM principal's ACLs over SASL/IAM. On a cluster that has stopped granting by default, nothing can write the FIRST ACL unless it's permitted. The job creates no topic. Its grants match `kafka-user.yaml`'s, grant for grant: Read, Write, Create, Describe, Alter, Delete, DescribeConfigs and AlterConfigs on topic `*`, and Read and Describe on consumer groups prefixed `dfe-`. Create makes the bootstrap topic set, Alter raises a partition count, and Delete lets deleting a source take that source's `_land`/`_load` pair with it. Every cluster operation is withheld.
- The ordering. dfe-engine syncs at Argo wave 5, the otel collector at 6 and every other app at 7. Each app pod runs a `wait-for-engine` init container (`dfe-common.waitForEngine`) that polls the engine Service's `/readyz`. Ready means the last schema pass converged, so an app can't start against an absent table. That failure reads as data loss and is really start ordering. The hunt runner ships in the engine's chart (dfe-extras) and syncs in its wave, so the same init container holds it until its coordination tables exist.

`bootstrap/smoke-test-integration.sh` asserts the engine's `schema` readiness check (CORE 0) before it looks at any pipeline. The per-object record is `GET /api/v1/system/schema` on the engine.

## Dead-letter queues

Every k8s-deployed app dead-letters to a Kafka topic dfe-engine creates: `dfe_receiver_dlq`, `dfe_loader_dlq`, `dfe_archiver_dlq`, `dfe_fetcher_dlq`, and one shared `dfe_transform_dlq` for the transforms (a transform overrides it when it needs a separate topic). Kafka is the DEFAULT DLQ target because the file fallback can't work under the charts' read-only rootfs and silently drops. In dfe-docker, DLQs are opt-IN per component, EXCEPT the single-node full deploy, which has the same DLQ topics as k8s.

The five names and their retention are declared in dfe-schemas and created by dfe-engine at startup, on every tier and every provider. A DLQ write happens AT failure time, the one moment nothing can be creating topics, so the engine's wave-5 sync is what guarantees they're there before an app can poison one. DLQ topics keep 7 days, against the in-cluster broker's 24-hour data-topic default. A poisoned message is exactly the record an operator must still find days later, and once its source offset commits it exists nowhere else. Chart env points the four fleet apps at their topic (the same `DLQ_TOPIC` / `DLQ_MODE` on every app, each in the app's env regime). Chart env doesn't point the transforms at `dfe_transform_dlq` in this release.

The charts set `dlq.mode: kafka_only` for all four (`argocd/values/apps/dfe-archiver/values.yaml`, `argocd/values/apps/dfe-fetcher/values.yaml`, and the receiver and loader blocks in `helm/charts/dfe-extras/values.yaml`), so Kafka is the one DLQ backend. An entry over the producer's `message.max.bytes`, counting its base64 payload a third larger than the record, fits no backend and is dropped and counted (`messages_dropped_total`, reason `dlq_too_large` on the archiver). `cascade` would fall through to the file backend, which the read-only rootfs rules out.

## How engine dials land here

> **Coming from Splunk or Elastic?** The engine writes every config change, from the console or the API, to the deploy repo, and Argo CD reconciles it, so what's running is what's in git. More in [what's different](../docs-site/content-root/coming-from.md#gitops-by-design-from-the-ground-up).

One mechanism does it. layer2-apps generates one Argo Application per `values/<service>-<instance>-values.yaml` in the deploy repo, from four sources:

- the component's thin chart over OCI, pinned by digest (`chart-digests` in `versions.yaml`)
- `helm/charts/dfe-extras`, its DFE objects
- this repo as ref `infra`, for the value files
- the deploy repo as ref `values`, its overlay file layered last

Add a values file and you add the app. Remove it and the app goes. The engine turns dials and this repo defines what dials exist, so every dial must be a chart value.

The overlay entry is `$values/{{ .path.path }}/{{ .path.filename }}` (`argocd/appsets/layer2-apps.yaml`), the directory and name of the file the generator matched, so each Application layers its file and no other.

Each Application is named `<service>-<instance>-<cluster>`, and that name is its Helm release name and the `app.kubernetes.io/instance` label on its objects. Helm rejects a release name past 53 characters, so a longer Application name installs under its first 44 characters, a `-` and the first 8 hex characters of its sha256.

The pages in this area:

| Doc | Covers |
|---|---|
| [deployment/README.md](deployment/README.md) | layers, tiers, values cascade, bootstrap contract |
| [deployment/rke2.md](deployment/rke2.md) | RKE2, the reference distribution, and its provisioning design (not built, #1123) |
| [deployment/on-prem.md](deployment/on-prem.md) | build, deploy, test and tear down on an on-prem cluster (the `local` target) |
| [deployment/kafka/](deployment/kafka/README.md) | managed-Kafka guides + Redpanda licence gate |
| [deployment/clickhouse.md](deployment/clickhouse.md) | CH target matrix + operator history (official operator / ClickHouse Cloud, private-cloud swap, Altinity untested) |
| [deployment/deployment-logs/](deployment/deployment-logs/template.md) | per-deploy log template |
| [testing-cycle.md](testing-cycle.md) | THE validation loop (preflight -> deploy+E2E -> smoke -> destroy), the env-file contract, the vanilla-cluster contract |
| [cloud-cycle.md](cloud-cycle.md), [release-rounds.md](release-rounds.md), [failure-evidence.md](failure-evidence.md) | the cycle on a cloud account, how a release candidate is proven, and what the readiness gate rejects and a failed stage leaves |
| [source-suite.md](source-suite.md) | the post-deploy source tests the cycle's `acceptance --suite source` runs: the two cases, what proves each, the reload-and-restart rule |
| [autoscaling.md](autoscaling.md) | KEDA pod scaling vs the per-target node-autoscaler fork decision |
| [edge-auth.md](edge-auth.md) | web-plane exposure + auth: every UI with a login external by default, route class (product/infra/ingest), the infra kill switch, edge OIDC + group RBAC, per-surface policy |
| [ingest-edge.md](ingest-edge.md) | the dfe-receiver data door: public LoadBalancers vs the Gateway route, source ranges, why an empty allow-list is the whole internet |
| [design-principles.md](design-principles.md) | the rules every DFE app follows: any source on day one, swappable parts, who owns the schema, where parsing happens |
