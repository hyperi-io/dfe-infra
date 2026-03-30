# DFE 2.2 Infrastructure — Design Specification

**Date:** 2026-03-30
**Project:** dfe-infra
**Status:** Draft v3
**Research corpus:** `docs/01` through `docs/07`

---

## 1. Problem Statement

DFE 2.1 (dfe-core) is a working Terraform + Helm + ArgoCD deployment but is deeply AWS-specific: EKS, MSK, Cognito, CloudWatch, Prometheus, Grafana, FluentBit, and IRSA are all hardcoded. There is no path to other clouds, no unified observability, and auth is fragmented across Cognito, oauth2-proxy, and Dex.

DFE 2.2 must be a new SSOT deployment repo that:
- Runs identically on Rancher local, AWS, GCP, and Azure
- Replaces AWS-specific services with cloud-agnostic equivalents
- Centralises observability on OTel → ClickHouse → HyperDX
- Unifies auth through Envoy Gateway OIDC
- Scales with KEDA driven by OTel-sourced metrics

---

## 2. Architecture

### 2.1 Two-Layer Model

All deployments follow a strict two-layer separation:

```
┌─────────────────────────────────────────────────────────┐
│  Layer 2: DFE Platform  (identical on any K8s target)   │
│                                                         │
│  Kafka  ClickHouse  PostgreSQL  FerretDB  HyperDX       │
│  OTel Collector  KEDA  DFE services (engine/ui/Rust)    │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Base Infrastructure  (varies by target)       │
│                                                         │
│  K8s cluster  Envoy Gateway  cert-manager               │
│  External Secrets Operator  ArgoCD + Valkey             │
│  Storage class  DNS  Secrets backend                    │
└─────────────────────────────────────────────────────────┘
```

**Layer 1** is either pre-existing (customer's EKS/GKE/AKS cluster) or provisioned by DFE's Terraform modules. For Rancher local, it is bootstrapped via hyperi-infra patterns.

**Layer 2** is always deployed by ArgoCD. ArgoCD itself is part of Layer 1; all other DFE components (operators, data services, DFE apps) are Layer 2.

**Layer 1/Layer 2 boundary for ArgoCD-managed components:** cert-manager and ESO are bootstrapped in Layer 1 (by the bootstrap script via Helm) before ArgoCD is operational. ArgoCD then takes ownership of them via self-managing ApplicationSets at sync wave 2.

### 2.2 Deployment Targets

| Target | K8s | Ingress & LB | Secrets | Storage | Kafka |
|--------|-----|-------------|---------|---------|-------|
| **Rancher local** | RKE2 (hyperi-infra bootstrap) | Envoy Gateway + `externalIPs` | OpenBao + ESO | local-path-provisioner | Strimzi |
| **AWS** | EKS | Envoy Gateway + NLB | AWS SM + ESO | EBS CSI (gp3) | MSK or Strimzi |
| **GCP** | GKE | Envoy Gateway + GCP LB | GCP SM + ESO | PD CSI | Strimzi or Confluent |
| **Azure** | AKS | Envoy Gateway + Azure LB | Azure KV + ESO | Azure Disk CSI | Strimzi or Confluent |

In all cases: DNS is cloud-specific (CoreDNS/Route53/Cloud DNS/Azure DNS), certs are via cert-manager, and OIDC is via Envoy Gateway SecurityPolicy.

### 2.3 GitOps Flow

The bootstrap script is idempotent (all steps use `helm upgrade --install` and `kubectl apply`):

```
Terraform provisions Layer 1
    │
    ▼
Bootstrap script (idempotent):
  1. kubectl apply cluster-secret
       annotations: domain, tenancy, cloud, clickhouse_host,
                    workload_identity_annotations, storage_class, ...
  2. helm upgrade --install cert-manager
  3. helm upgrade --install external-secrets
  4. kubectl apply eso-cluster-secret-store.yaml
  5. helm upgrade --install argocd (with Valkey)
  6. kubectl apply argocd-bootstrap.yaml   (AppProject bootstrap)
  7. kubectl apply cluster-addons.yaml     (ApplicationSet root)
    │
    ▼
ArgoCD takes over (idempotent from this point):
  bootstrap/ → AppProjects (infra, data, dfe-apps)
  addons/argo_apps/ → ApplicationSets (matrix generator)
    │
    ▼
ApplicationSets deploy Layer 2 via Helm (sync waves 2→5)
```

The cluster secret annotation bridge (preserved from dfe-core) carries all Terraform outputs into the GitOps layer. ApplicationSets read `{{ .metadata.annotations.* }}`.

### 2.4 Ingress and Auth

**Envoy Gateway** is the single entry point for all HTTP/HTTPS traffic. It replaces nginx-ingress, ALB Controller, and oauth2-proxy entirely.

```
HTTPS request
    │
    ▼
Envoy Gateway GatewayClass + Gateway
    │
    ├─ SecurityPolicy (OIDC) → authorization code flow → session cookie
    │     provider: { Google | Cognito | Entra | any OIDC }
    │     clientSecret: ESO-synced from secrets backend
    │     scopes: [openid, email, profile, groups]
    │
    ├─ BackendTLSPolicy for upstream TLS where needed
    │
    ├─ HTTPRoute → dfe-ui
    ├─ HTTPRoute → dfe-engine
    ├─ HTTPRoute → argocd
    └─ HTTPRoute → hyperdx
```

**OIDC claim forwarding:** Envoy Gateway's SecurityPolicy performs the OAuth2 authorization code flow and issues a session cookie. An `envoy.filters.http.jwt_authn` filter chained after the OIDC filter extracts claims from the ID token and sets:
- `X-Forwarded-User` (email/sub claim)
- `X-Auth-Groups` (groups claim, comma-separated)

dfe-engine reads these headers via its `get_current_user()` dependency when OIDC is enabled. The `group_role_mapping` setting maps OIDC group names/IDs to DFE roles.

**Auth modes:**
- **External OIDC (recommended for production):** Any OIDC-compliant provider (Entra ID, Google Workspace, Cognito, Keycloak, etc.) via Envoy SecurityPolicy
- **Local auth (default, always available):** `LocalAuthProvider` (bcrypt, 3 accounts: admin/operator/viewer). Used for: small deployments with no IdP, development/test environments, break-glass access when OIDC is unavailable. OIDC is not mandatory — it is recommended for any serious production deployment.

The local auth provider and external OIDC can coexist. `auth.oidc.enabled` is a per-deployment config flag.

### 2.5 Observability

OTel Collector is deployed in two tiers. Data can reach HyperDX/ClickHouse via two valid paths:

```
DFE Rust services ──OTLP gRPC──▶
DFE Python engine ──OTLP gRPC──▶  [Gateway Collector]  ──▶  ClickHouse
K8s applications  ──OTLP gRPC──▶  (Deployment, 1+ pods) ──▶  HyperDX (dashboards, search, alerts)
                                          ▲
[DaemonSet Collector]  ──────────────────┘
(1 pod per node)
Collects: kubelet metrics, container logs, node metrics

── Path A: Direct to HyperDX (OTel Collector → HyperDX OTLP ingest endpoint)
── Path B: Via DFE pipeline (OTel Collector → dfe-receiver → Kafka → dfe-loader → ClickHouse)
```

Both paths are valid and can coexist. Path A is simpler for infrastructure telemetry. Path B routes observability data through the DFE pipeline (useful when telemetry should also be archived or enriched by DFE transforms). The choice is per data type and configurable in the OTel Collector pipeline config.

- DaemonSet collectors forward to the Gateway collector via OTLP
- Gateway collector exports to ClickHouse/HyperDX and exposes a Prometheus scrape endpoint (`:8889`) for KEDA
- Replaces: Prometheus, Grafana, CloudWatch, FluentBit, kube-prometheus-stack
- HyperDX provides Kibana-like search + dashboards; FerretDB (MongoDB wire protocol over CNPG PG17) backs HyperDX metadata
- ClickHouse is the single analytics + observability store

### 2.6 KEDA Autoscaling

KEDA scales DFE services based on OTel-sourced metrics. The default approach is Kedify OTEL Scaler (Option B); Option A is the fallback if Kedify is not production-ready.

| Option | Mechanism | Status |
|--------|-----------|--------|
| **B (default)** | Kedify OTEL Scaler — direct OTLP push from OTel Collector to KEDA | Start here; change if problematic |
| A (fallback) | OTel Collector Gateway exposes Prometheus endpoint `:8889` → KEDA Prometheus trigger | Proven, no extra dependencies |

```
[Option B — default]
OTel Collector Gateway ──OTLP push──▶ Kedify OTEL Scaler ──▶ KEDA ScaledObject
                                                                      │
                                                             DFE service replicas

[Option A — fallback]
OTel Collector Gateway (:8889 Prometheus exporter)
    │
    ▼
KEDA ScaledObject (Prometheus trigger)
    │
    ▼
DFE service replicas
```

Key scaling metrics from `hyperi-rustlib`:
- `dfe_scaling_pressure` (0.0–1.0 composite gauge) — primary KEDA trigger for dfe-receiver, dfe-loader
- `dfe_transport_queue_size` — Kafka consumer lag equivalent
- `dfe_spool_bytes` — back-pressure signal

**Transform scale-to-zero:** `dfe-transform-*` apps are ALWAYS associated with a SINGLE source Kafka topic. KEDA scales them to zero when no new data appears on the topic for a configurable idle period (default: 5 minutes). The KEDA Kafka trigger detects new messages on the topic and starts the pod back up. This is a standard KEDA Kafka consumer lag pattern:

```
Kafka topic (source) ──consumer lag > 0──▶ KEDA ScaledObject ──▶ dfe-transform-* (1+ pods)
                      ──consumer lag = 0 for X──▶ KEDA ──▶ scale to 0 pods (cooldown)
                      ──new message arrives──▶ KEDA ──▶ scale to 1 pod (activation)
```

```yaml
# ScaledObject for dfe-transform-*
minReplicaCount: 0           # scale to zero when idle
maxReplicaCount: 10
cooldownPeriod: 300          # 5 min of zero lag → scale to 0
pollingInterval: 15
triggers:
  - type: kafka
    metadata:
      bootstrapServers: "..."
      consumerGroup: "dfe-transform-{name}"
      topic: "{source_topic}"
      lagThreshold: "10"       # start scaling at 10+ messages lag
      activationLagThreshold: "1"  # activate from zero at 1+ message
```

### 2.6.1 Node Scaling (Capacity Autoscaling)

KEDA handles **pod** scaling. Node scaling (adding/removing K8s nodes when pod demand exceeds cluster capacity) is a separate concern handled per target:

| Target | Node scaling | Mechanism |
|--------|-------------|-----------|
| **Rancher local (devex)** | Fixed (overprovisioned) | 3 nodes sized for peak. No autoscaling. |
| **Rancher local (production)** | CAPI + Proxmox provider | Cluster API `MachineDeployment` with cluster autoscaler. Proxmox CAPI provider (`ionos-cloud/cluster-api-provider-proxmox`) creates VMs via Proxmox API. cloud-init → RKE2 agent join. ~2 min scale-up. |
| **AWS EKS** | Karpenter | Direct EC2 provisioning based on pending pod requirements. Preserved from dfe-core 2.1. |
| **GCP GKE** | GKE node auto-provisioning | Native GKE feature. No additional components. |
| **Azure AKS** | AKS cluster autoscaler | Native AKS feature. No additional components. |

```
KEDA scales pods → pods Pending (no node capacity)
    │
    ├─ On-prem: CAPI Proxmox provider → new VM → RKE2 join → pods scheduled
    ├─ AWS: Karpenter → new EC2 → EKS node join → pods scheduled
    └─ GCP/Azure: native autoscaler → new node → pods scheduled
```

**Phasing:**
- DevEx (now): fixed 3 nodes, overprovisioned (72 cores, 192GB RAM)
- Production on-prem: CAPI + Proxmox provider (new plan, after AWS validation)
- Cloud: Karpenter (AWS) / native (GCP/Azure) in cloud-specific plans

**Terraform module:** `tf-node-autoscaler` — deploys CAPI operator + provider + MachineDeployment CRD for on-prem, Karpenter for AWS. Added to Layer 1 wave 3 alongside other operators.

### 2.7 Data and Config Flow

```
Config storage (see note below)
    │
    ├── dfe-engine reads via DirectoryConfigStore
    │      compiles → Helm values + Argo CD CRDs
    │      writes → config storage (GitOps SSOT)
    │      ArgoCD detects change → syncs → K8s
    │         ▼
    │   Rust services read env vars (DFE_{SERVICE}_{KEY})
    │   + /config/ mount (hot-reload via file polling)
    │
    └── Source data flow:
         dfe-receiver / dfe-fetcher
              ▼ (Kafka: {source}_land, Strimzi SASL/SCRAM, zstd)
         dfe-transform-* (optional, VRL/WAF/Vector/WASM)
              ▼ (Kafka: {source}_load)
         dfe-loader → ClickHouse (async insert, tiered roles)
         dfe-archiver → S3/MinIO (optional)
```

**Config storage backends** (the `/config/` mount for Rust services and DirectoryConfigStore for dfe-engine):
- **Rancher local:** NFS file share (from storage VM) or git-sync sidecar
- **AWS:** EFS (NFS-compatible) or S3 bucket (with s3-fuse/mountpoint CSI)
- **GCP:** Filestore NFS or GCS bucket
- **Azure:** Azure Files (NFS) or Azure Blob

The `directory_config` feature in hyperi-rustlib supports local filesystem, S3, and git backends natively.

### 2.8 Secrets Management

```
OpenBao (local) / Cloud SM (AWS/GCP/Azure)
    │
    └── ESO ClusterSecretStore (vault or cloud provider)
              ▼
         K8s Secrets (synced, auto-rotated)
              │
              ├── Stakater Reloader watches → rolls pods on change
              └── Pods (env vars or volume mounts)
```

**Workload Identity:** DFE services that need cloud API access use cloud-native workload identity:
- AWS: IRSA (`eks.amazonaws.com/role-arn` annotation on ServiceAccount)
- GCP: WIF (`iam.gke.io/gcp-service-account` annotation)
- Azure: Workload Identity (`azure.workload.identity/client-id` annotation)
- Local: OpenBao AppRole credentials via ESO

The `tf-iam` module outputs a `workload_identity_annotations` map per DFE service. Written into the cluster secret; consumed by Helm charts via the ApplicationSet annotation bridge. See Section 9 for the full naming standard.

### 2.9 Network Policy and Multi-Tenancy

Initial deployment model: **single tenant per cluster** (same as dfe-core). Namespace-level multi-tenancy is not in scope for 2.2.

Within the cluster, Kubernetes NetworkPolicies are applied per namespace:
- `dfe-*` namespaces allow ingress from Envoy Gateway and other `dfe-*` namespaces
- Data namespaces (Strimzi, ClickHouse, CNPG) allow ingress only from `dfe-*` namespaces
- All namespaces allow egress to OTel Collector
- `dfe-*` namespaces deny ingress from `default` and unrelated namespaces

**Receiver network:** DFE receivers are designed to listen on the `100.64.0.0/10` address space (IANA CGNAT range). This is a deliberate design choice to dovetail with `dfe-openvpn`: edge stream hubs deployed in live corporate environments connect via OpenVPN, and the `100.64.x.x` range avoids IP collisions with both the corporate LAN and the VPN tunnel range in any standard deployment.

### 2.10 Backup and Disaster Recovery

| Component | Backup Mechanism | Target | RPO |
|-----------|-----------------|--------|-----|
| CNPG PostgreSQL 17 | CNPG `ScheduledBackup` CRD (Barman, WAL streaming) | S3/MinIO (tf-storage) | < 1h |
| ClickHouse | `BACKUP TO S3` via operator CRD | S3/MinIO (tf-storage) | < 24h |
| Kafka | 3-replica + 72h retention | In-cluster | 72h window |
| ArgoCD | Git repo is authoritative; ArgoCD state recoverable from git | Git remote | Near-zero |
| Config repo | Git remote (GitHub/GitLab) | Git remote | Near-zero |

RTO targets: CNPG PITR restore < 30m; ClickHouse restore from S3 < 2h; full cluster rebuild from git < 30m.

---

## 3. Components

### 3.1 Terraform Modules

| Module | Purpose | Cloud variants |
|--------|---------|---------------|
| `tf-k8s-cluster` | K8s cluster provisioning | RKE2 (Proxmox), EKS, GKE, AKS |
| `tf-networking` | VPC/VNet + subnets + NAT | AWS VPC, GCP VPC, Azure VNet, (none for Rancher) |
| `tf-secrets` | Secrets backend setup | OpenBao, AWS SM, GCP SM, Azure KV |
| `tf-dns` | DNS zone + records | Route53, Cloud DNS, Azure DNS, CoreDNS |
| `tf-certs` | TLS wildcard cert | ACME (Let's Encrypt) via DNS-01 |
| `tf-storage` | Cloud storage buckets (archiving + backups + config) | S3, GCS, Azure Blob, MinIO |
| `tf-clickhouse` | ClickHouse service | ClickHouse Cloud or self-hosted operator |
| `tf-iam` | Workload identity per DFE service | IRSA (AWS), WIF (GCP), Workload Identity (Azure), OpenBao AppRole (local) |

Each module exposes standardised outputs consumed by the cluster secret annotation bridge. The `tf-iam` module outputs a `workload_identity_annotations` map used by all DFE Helm charts.

### 3.2 ArgoCD ApplicationSets (sync waves)

cert-manager and ESO are bootstrapped before ArgoCD, then adopted at wave 2 for ongoing management.

| Wave | Components |
|------|-----------|
| 2 | cert-manager (adopted), ESO (adopted), Envoy Gateway, external-dns |
| 3 | KEDA, metrics-server, VPA, Reloader, CNPG operator, Strimzi operator, ClickHouse operator |
| 4 | CNPG PostgreSQL cluster, ClickHouse cluster, Strimzi Kafka cluster, FerretDB, OTel Collector |
| 5 | HyperDX, ArgoCD (self-managing), dfe-engine, dfe-ui, dfe-receiver, dfe-loader, dfe-archiver, dfe-fetcher, dfe-transform-* |

### 3.3 DFE Applications

All DFE application Helm charts follow `common.yaml` + cloud-specific overrides. Helm values are compiled by dfe-engine's `HelmValuesCompiler`.

| Chart | Language | Key values |
|-------|----------|-----------|
| dfe-engine | Python 3.12 | ClickHouse creds, JWT secret, config volume, workload identity annotation |
| dfe-ui | TypeScript/Next.js | API URL, HyperDX URL, NextAuth secret |
| dfe-receiver | Rust | Kafka brokers, OTel endpoint, config mount, listen on `100.64.x.x`, workload identity |
| dfe-loader | Rust | Kafka brokers, ClickHouse host, OTel endpoint, workload identity |
| dfe-archiver | Rust | Kafka brokers, S3/blob endpoint, OTel endpoint, workload identity |
| dfe-fetcher | Rust | API credentials (ESO-synced), Kafka brokers, OTel endpoint |
| dfe-transform-* | Rust | Kafka brokers (source + dest topics), OTel endpoint |

### 3.4 Tenancy Sizing

Three profiles (dev / small / large) controlling:
- Kafka brokers and partition count
- ClickHouse replicas and memory limits
- K8s node count and instance type/size
- KEDA min/max replica bounds
- ClickHouse backup schedule frequency
- ClickHouse Cloud idle suspend (ClickHouse Cloud targets only; not applicable to self-hosted operator)

---

## 4. Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Ingress + Auth | Envoy Gateway with SecurityPolicy | Replaces nginx + oauth2-proxy. Gateway API standard. OIDC native. Proven in hyperi-infra. |
| OIDC requirement | Recommended, not mandatory | Mandatory for serious production. Optional for dev/test/small deployments. Local auth always available. |
| OIDC claim forwarding | jwt_authn filter chained after OIDC SecurityPolicy | Envoy manages login flow; JWT filter extracts claims for backend header forwarding |
| Autoscaling | KEDA from OTel-sourced metrics (Kedify default) | Start with Kedify OTEL Scaler. Fall back to OTel Collector Prometheus endpoint if Kedify not ready. |
| OTel destination | Dual path: direct to HyperDX OR via dfe-receiver | Both valid. Direct is simpler for infra telemetry. Via dfe-receiver for DFE pipeline integration. |
| Local K8s | Rancher (RKE2 via hyperi-infra) | On-prem only. Cloud targets use native K8s (EKS/GKE/AKS). |
| Observability | OTel → ClickHouse → HyperDX | Replaces entire Prometheus/Grafana/CloudWatch stack. |
| Cache/state | Valkey (not Redis) | ArgoCD cache is ephemeral. Trivial swap. |
| Database | CNPG PostgreSQL 17 | Standardised for FerretDB, HyperDX metadata, and services. |
| MongoDB compat | FerretDB over CNPG PG17 | HyperDX metadata. No MongoDB binary required. |
| Secrets | OpenBao (local) + cloud SM (cloud) | Unified via ESO with pluggable provider. |
| Config storage | Cloud-native storage per target (NFS/S3/GCS/Azure Files) | Not ConfigMaps — config YAML lives in persistent cloud storage or git-sync. |
| Workload identity | Cloud-native per target (IRSA/WIF/Azure WI/OpenBao) | No long-lived credentials. tf-iam outputs annotations consumed by Helm. |
| Receiver network | `100.64.0.0/10` (CGNAT range) | Avoids IP collisions with corporate LANs when edge stream hubs connect via dfe-openvpn. |
| GitOps pattern | ArgoCD ApplicationSet matrix generator | Preserved from dfe-core. Cloud-agnostic bridge via cluster secret annotations. |
| Config management | YAML SSoT via DirectoryConfigStore | dfe-engine pattern. No DB required for config. |
| Schema DDL | dfe-engine writes to config repo; ArgoCD Job applies DDL | GitOps SSOT preserved. |
| Multi-tenancy | Single tenant per cluster | Aligned with dfe-core model. NetworkPolicies enforce namespace isolation. |
| Backup | CNPG ScheduledBackup + ClickHouse S3 backup | Automated, S3-compatible, RPO < 1h (PG), < 24h (CH). |
| Repo co-iteration | dfe-infra and dfe-engine developed and iterated together | Changes to dfe-engine's Helm values, API, or config schema are reflected immediately in dfe-infra. |

---

## 5. Data Flow Detail

### 5.1 Inbound Data (receiver / fetcher)

```
External data source
    │ (HTTP/gRPC/SFTP/API — received on 100.64.x.x range)
    ▼
dfe-receiver (or dfe-fetcher)
    │ match rule → topic routing (DfeSource: {source}_land / {source}_load)
    │ optional: dfe-transform-* (VRL/WAF/Vector/WASM)
    ▼
Kafka: {source}_land (Strimzi, KRaft, SASL/SCRAM, zstd, 72h retention)
    ▼
dfe-loader → ClickHouse (async insert, tiered roles, dfe_loader_role)
             + dfe-archiver → S3/MinIO (optional, per-source config)
```

### 5.2 Query / Hunt

```
dfe-engine (hunt scheduler: APScheduler cron / query executor: ViewExecutor)
    │ parameterised ClickHouse SQL views
    ▼
ClickHouse (dfe_hunts_tier_* role, max_execution_time: 600s/120s/15s by tier)
    │
    ├─ alerts → Apprise (Slack/email/webhook/PagerDuty)
    └─ results → dfe-ui (via REST API /api/v1/query)
```

### 5.3 Schema Management (GitOps-native)

```
dfe-ui / API caller
    ▼
dfe-engine API (POST /sources → SchemaBuilderV2)
    │ YAML SSoT source definition → ClickHouse DDL
    │ Writes to config storage (DirectoryConfigStore git commit)
    ▼
ArgoCD detects git change → syncs K8s Job manifest
    ▼
K8s Job (ImperativeOperations container)
    │ Executes: CREATE TABLE IF NOT EXISTS ...
    └─ ClickHouse
```

---

## 6. Auth Detail

### 6.1 Single Source of Truth Auth

```
External OIDC provider (Google/Entra/Cognito/Keycloak/any)
    │
    ▼
Envoy Gateway SecurityPolicy
    │ OAuth2 authorization code flow → session cookie
    ▼
Envoy jwt_authn filter (chained)
    │ Validates ID token, extracts claims
    │ Sets: X-Forwarded-User (email/sub), X-Auth-Groups (groups, comma-separated)
    ▼
dfe-engine API (get_current_user dependency)
    │ Reads X-Forwarded-User, X-Auth-Groups
    │ group_role_mapping → AuthContext.roles
    │ authorize(auth, action, resource) → Cedar-compatible RBAC
    ▼
RBAC enforcement per endpoint
```

Local fallback (`LocalAuthProvider`, bcrypt, admin/operator/viewer) available always — activated when OIDC is unavailable or not configured.

### 6.2 ArgoCD RBAC

dfe-engine generates `argocd-rbac-cm` policy CSV (via `generate_rbac_csv()`). ArgoCD's own Dex is disabled; Envoy Gateway handles OIDC for ArgoCD too.

---

## 7. Open Questions

1. **KEDA Kedify production readiness:** Evaluate Kedify OTEL Scaler maturity before committing. Fallback is OTel Collector Prometheus endpoint → KEDA Prometheus trigger (Option A).
2. **Config repo topology:** Mono-repo (dfe-infra contains infra TF + app config YAML) vs separate `dfe-config` repo per customer deployment. Separate repo aligns better with dfe-engine's git-aware `DirectoryConfigStore`.
3. **ClickHouse deployment model per cloud:** Default self-hosted (operator). ClickHouse Cloud as swap-in for AWS/GCP/Azure.
4. **VPN for edge/stream hub deployments:** `dfe-openvpn` (purpose-built) vs WireGuard-based (Netmaker/Tailscale). Default: dfe-openvpn.
5. **Terraform state backend:** Cloud-native per target (S3/GCS/Azure Blob with locking) vs centralised OpenTofu Cloud.
6. **FerretDB workload validation:** Must validate HyperDX aggregation pipelines work with FerretDB before committing. Fallback: native MongoDB.

---

## 8. Success Criteria

**Deployment:**
- Single `terraform apply` + idempotent bootstrap script deploys a fully operational DFE 2.2 cluster on Rancher local
- Same Layer 2 ApplicationSet definitions deploy successfully on at least one cloud target (AWS EKS) with no changes to charts or GitOps structure
- Full cluster rebuild from git (ArgoCD re-sync) completes in under 30 minutes

**Observability:**
- OTel telemetry from all DFE services flows to ClickHouse and is queryable in HyperDX within 60 seconds of deployment
- KEDA scales dfe-receiver horizontally in response to `dfe_scaling_pressure` changes (validated with synthetic load)

**Auth:**
- OIDC single login works across dfe-ui, dfe-engine, ArgoCD, and HyperDX
- Local auth fallback is functional without OIDC connectivity

**Cloud agnosticism:**
- No cloud-specific resources in Layer 2 charts; all cloud-specifics are in Terraform modules and cluster secret annotations only

**Sizing:**
- Tenancy sizing profiles (dev/small/large) produce measurably different resource allocations for Kafka, ClickHouse, and K8s nodes

**Migration:**
- Documented migration path from dfe-core 2.1 (ClickHouse RBAC, secret migration, Helm value equivalence)

**Operations:**
- Secret rotation via ESO triggers pod restart (via Reloader) within 60 seconds
- CNPG backup completes successfully to S3/MinIO on schedule

---

## 9. Naming and Tagging Standard

### 9.1 Canonical Dimensions

Every DFE resource is identified by four dimensions:

| Dimension | Variable | Constraint | Examples |
|-----------|----------|-----------|---------|
| Project | `project` | 2-10 chars, `[a-z][a-z0-9]*` | `dfe` |
| Component | `component` | 2-15 chars, `[a-z][a-z0-9-]*[a-z0-9]` | `loader`, `receiver`, `keda-scaler` |
| Environment | `env` | enum: `dev`, `stg`, `prod`, `local` | `prod` |
| Cloud | `cloud` | enum: `aws`, `gcp`, `az`, `local` | `aws` |

**Canonical name:** `{project}-{component}-{env}` — max 30 chars (GCP Service Account ID is the binding constraint across all platforms).

### 9.2 Platform-Specific Names (all derived from canonical)

| Platform | Pattern | Example |
|----------|---------|---------|
| K8s ServiceAccount | `{project}-{component}` | `dfe-loader` |
| K8s Namespace | `{project}-{env}` | `dfe-prod` |
| Rancher Cluster | `{project}-{cloud}-{env}` | `dfe-local-prod` |
| Rancher Project | `{project}-{env}` | `dfe-prod` (maps to K8s namespace group) |
| AWS IAM Role (IRSA) | `{project}-{component}-{env}-irsa` | `dfe-loader-prod-irsa` |
| GCP Service Account | `{project}-{component}-{env}` | `dfe-loader-prod` |
| Azure Managed Identity | `id-{project}-{component}-{env}` | `id-dfe-loader-prod` |
| Azure Federated Credential | `fc-{project}-{component}-{env}` | `fc-dfe-loader-prod` |
| OpenBao AppRole | `{project}-{component}-{env}` | `dfe-loader-prod` |
| OpenBao Policy | `{project}-{component}-{env}-policy` | `dfe-loader-prod-policy` |
| OpenBao Secret Path | `secret/data/{project}/{env}/{component}/*` | `secret/data/dfe/prod/loader/*` |

### 9.3 Cloud Tags / Labels

Applied to every cloud resource managed by Terraform. Keys are **lowercase underscore**:

| Key | Value | Mandatory? |
|-----|-------|-----------|
| `project` | `dfe` | Yes |
| `component` | `loader` | Yes |
| `env` | `prod` | Yes |
| `cloud` | `aws` | Yes |
| `managed_by` | `terraform` | Yes |
| `repo` | `github.com/hyperi-io/dfe-infra` | Yes |
| `version` | `2.2.0` | Recommended |
| `region` | `us-east-1` | Recommended |
| `owner` | `platform-team` | Recommended |
| `cost_center` | `eng-platform` | Recommended (billing) |

### 9.4 Kubernetes Labels

Applied to every K8s object via Helm chart `commonLabels`:

```yaml
app.kubernetes.io/name:        "{project}-{component}"    # e.g. dfe-loader
app.kubernetes.io/part-of:     "{project}"                # e.g. dfe
app.kubernetes.io/managed-by:  "helm"
app.kubernetes.io/version:     "{app_version}"            # semver
dfe.hyperi.io/env:             "{env}"                    # dev/stg/prod/local
dfe.hyperi.io/cloud:           "{cloud}"                  # aws/gcp/az/local
```

### 9.5 OTel Resource Attributes

Derived mechanically from the same four dimensions:

| OTel Attribute | Value | Example |
|---------------|-------|---------|
| `service.name` | `{project}-{component}` | `dfe-loader` |
| `service.namespace` | `{project}` | `dfe` |
| `service.version` | Helm `appVersion` | `2.2.0` |
| `service.instance.id` | K8s pod UID (auto via OTel K8s detector) | — |
| `deployment.environment.name` | `{env}` | `prod` |
| `k8s.namespace.name` | `{project}-{env}` | `dfe-prod` |
| `k8s.deployment.name` | `{project}-{component}` | `dfe-loader` |
| `cloud.provider` | `{cloud}` | `aws` |
| `cloud.region` | `{region}` | `us-east-1` |

This means a single Terraform `locals` block drives every cloud resource name, K8s label, and OTel resource attribute — one source of truth across all layers.

### 9.6 Terraform Implementation

```hcl
variable "project"   { default = "dfe" }
variable "component" { }  # required, e.g. "loader"
variable "env"       { }  # required, enum: dev/stg/prod/local
variable "cloud"     { }  # required, enum: aws/gcp/az/local
variable "region"    { default = "local" }

locals {
  canonical_name = "${var.project}-${var.component}-${var.env}"  # ≤30 chars

  k8s_namespace       = "${var.project}-${var.env}"
  k8s_service_account = "${var.project}-${var.component}"

  aws_irsa_role_name    = "${local.canonical_name}-irsa"
  gcp_sa_account_id     = local.canonical_name
  azure_identity_name   = "id-${local.canonical_name}"
  vault_approle_name    = local.canonical_name
  vault_policy_name     = "${local.canonical_name}-policy"

  common_tags = {
    project    = var.project
    component  = var.component
    env        = var.env
    cloud      = var.cloud
    region     = var.region
    managed_by = "terraform"
    repo       = "github.com/hyperi-io/dfe-infra"
  }
}

# Validation: canonical name must fit GCP SA 30-char limit
resource "null_resource" "validate_canonical_name" {
  lifecycle {
    precondition {
      condition     = length(local.canonical_name) <= 30
      error_message = "Canonical name '${local.canonical_name}' exceeds 30 chars (GCP SA limit)."
    }
  }
}
```
