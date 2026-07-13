# DFE 2.2 Research Synthesis

**Date:** 2026-03-30
**Purpose:** Cross-cutting findings, architectural decisions, gaps, and recommendations synthesised from all analysis documents.

---

## 1. Architecture Decision Record

### 1.1 Confirmed Decisions (from scope + user clarifications)

| Decision | Rationale |
|----------|-----------|
| **Envoy Gateway** replaces nginx-ingress + ALB Controller + oauth2-proxy | Gateway API standard, native OIDC SecurityPolicy, already proven in hyperi-infra |
| **Envoy Gateway handles auth** (OIDC) | Single SecurityPolicy at gateway level, eliminates per-app oauth2-proxy |
| **KEDA scales from OTel metrics** | Not Prometheus, not CloudWatch. Direct OTel -> KEDA pipeline |
| **Rancher is local-only** | On-prem/edge deployments use Rancher-managed RKE2. Cloud uses native K8s (EKS/GKE/AKS) |
| **Valkey replaces Redis** | For ArgoCD cache (trivial swap -- cache is ephemeral) and any other Redis usage |
| **PostgreSQL 17** via CNPG | Standardised database for FerretDB, HyperDX metadata, and services |
| **OTel -> ClickHouse + HyperDX** | Replaces entire Prometheus/Grafana/CloudWatch/FluentBit stack |
| **OpenBao for secrets** | Replaces AWS Secrets Manager. ESO with vault provider syncs to K8s |
| **TF + Helm + ArgoCD** approach preserved | Proven in dfe-core, enhanced for multi-cloud |

### 1.2 Deployment Targets (in order)

| Target | K8s | Ingress | Auth | Secrets | Storage | Kafka |
|--------|-----|---------|------|---------|---------|-------|
| **Rancher local** | RKE2 (hyperi-infra) | Envoy Gateway + externalIPs | OpenBao + Envoy OIDC | OpenBao | local-path | Strimzi |
| **AWS** | EKS | Envoy Gateway + NLB | Envoy OIDC + Cognito/Entra | AWS SM or OpenBao | EBS CSI (gp3) | MSK or Strimzi |
| **GCP** | GKE | Envoy Gateway + GCP LB | Envoy OIDC + Google IdP | GCP SM or OpenBao | PD CSI | Confluent or Strimzi |
| **Azure** | AKS | Envoy Gateway + Azure LB | Envoy OIDC + Entra ID | Azure KV or OpenBao | Azure Disk CSI | Confluent or Strimzi |

### 1.3 Two-Layer Architecture (from hyperi-infra)

This is the key architectural pattern for DFE 2.2:

**Layer 1 (Base Infrastructure)** -- varies by target, pre-exists before DFE deploys:
- K8s cluster (RKE2/EKS/GKE/AKS)
- Envoy Gateway
- cert-manager
- External Secrets Operator
- ArgoCD + Valkey
- Storage classes
- (Local only: Rancher, OpenBao, DNS, NFS)

**Layer 2 (DFE Platform)** -- deploys identically on ANY K8s:
- Strimzi Kafka (or cloud Kafka swap-in)
- ClickHouse (operator or ClickHouse Cloud)
- CNPG PostgreSQL 17
- FerretDB
- HyperDX + OTel Collector
- KEDA
- DFE services (engine, UI, receivers, loaders, archivers, transforms, fetchers)

---

## 2. Cross-Cutting Findings

### 2.1 What's Already Working Well Across Repos

| Pattern | Where Proven | Carry Forward? |
|---------|-------------|---------------|
| ArgoCD ApplicationSet matrix generator + cluster annotations | dfe-core | Yes -- cloud-agnostic GitOps bridge |
| Sync wave ordering (2: ingress, 3: infra, 4: monitoring, 5: apps) | dfe-core | Yes |
| Tenancy sizing profiles (.auto.tfvars) | dfe-core | Yes -- extend for multi-cloud |
| YAML SSoT config with DirectoryConfigStore | dfe-engine, rustlib | Yes -- proven config pattern |
| Plugin architecture for service types | dfe-engine | Yes |
| Cedar-compatible RBAC with group_role_mapping | dfe-engine | Yes -- ready for OIDC |
| Transport abstraction (7 backends, enum dispatch) | rustlib | Yes -- cloud-agnostic |
| Flat env var config override (DFE_{SERVICE}_{KEY}) | rustlib | Yes -- Helm -> env -> service |
| Config hot-reload (file polling) | rustlib | Yes -- ArgoCD ConfigMap updates |
| Envoy Gateway + OIDC SecurityPolicy | hyperi-infra | Yes -- already replaces nginx |
| OpenBao + ESO ClusterSecretStore | hyperi-infra | Yes -- secrets pattern |
| cert-manager + OpenBao PKI | hyperi-infra | Yes |
| Strimzi Kafka (KRaft, SASL/SCRAM) | hyperi-infra | Yes -- replaces MSK |
| CNPG PostgreSQL 17 | hyperi-infra, dfe-core | Yes |
| HyperDX + FerretDB + OTel Collector | hyperi-infra | Yes |
| HyperDX postMessage bridge for rule creation | dfe-ui | Yes |

### 2.2 Components Being Removed

| Component | Replaced By | Risk |
|-----------|------------|------|
| nginx-ingress | Envoy Gateway | Low (already proven in hyperi-infra) |
| ALB Controller | Envoy Gateway + cloud LB | Low |
| oauth2-proxy + Redis | Envoy Gateway OIDC + Valkey (for ArgoCD only) | Low |
| AWS Cognito | External OIDC provider via Envoy SecurityPolicy | Medium (all auth flows change) |
| Prometheus + kube-prometheus-stack | OTel Collector + ClickHouse | Medium (alerting rules need migration) |
| Grafana + Grafana Operator | HyperDX | Medium (dashboards need recreation) |
| CloudWatch + FluentBit | OTel Collector | Low |
| AWS Managed Prometheus | OTel -> ClickHouse | Low |
| AWS Secrets Manager | OpenBao (local) or cloud SM | Medium (secret migration) |
| MSK | Strimzi Kafka (local) or cloud Kafka | High (operational complexity) |
| Lambda (MSK ACL bootstrap) | K8s Job | Low |
| Karpenter (AWS-only) | Cloud-native autoscaler or KEDA node triggers | Medium |

### 2.3 Novel/Unproven Integrations

| Integration | Status | Recommendation |
|-------------|--------|---------------|
| **KEDA scaling from OTel metrics** | Not proven anywhere yet | Top priority R&D. Options: (1) Kedify OTEL Scaler (direct push), (2) OTel Collector -> Prometheus remote-write -> KEDA, (3) Custom ClickHouse external scaler. Recommend Kedify if available, otherwise option 2. |
| **FerretDB for HyperDX** | Deployed in hyperi-infra (v2-beta) | Needs workload testing for aggregation pipeline compatibility |
| **dfe-engine OIDC token validation** | Not implemented | Needs JWKS discovery, dual-mode auth (local fallback + OIDC) |
| **dfe-engine OTel auto-instrumentation** | Not implemented | Needs opentelemetry-instrumentation-fastapi |
| **rustlib unified OTel init** | Gaps identified | Needs single otel::init() for metrics + traces + logs |
| **Multi-cloud Terraform abstraction** | Not implemented | New module structure needed |

---

## 3. Component Inventory for DFE 2.2

### 3.1 Infrastructure Components (Layer 1 prerequisites)

| Component | Local (Rancher) | AWS | GCP | Azure |
|-----------|----------------|-----|-----|-------|
| K8s | RKE2 via hyperi-infra | EKS (TF module) | GKE (TF module) | AKS (TF module) |
| Ingress | Envoy Gateway + externalIPs | Envoy Gateway + NLB | Envoy Gateway + GCP LB | Envoy Gateway + Azure LB |
| Certs | cert-manager + OpenBao PKI | cert-manager + Let's Encrypt | cert-manager + Let's Encrypt | cert-manager + Let's Encrypt |
| Secrets | OpenBao + ESO | AWS SM + ESO or OpenBao | GCP SM + ESO or OpenBao | Azure KV + ESO or OpenBao |
| DNS | CoreDNS + wildcard | Route53 + external-dns | Cloud DNS + external-dns | Azure DNS + external-dns |
| Storage | local-path + NFS | EBS CSI (gp3) | PD CSI | Azure Disk CSI |
| Auth | Envoy OIDC + any IdP | Envoy OIDC + Cognito/Entra | Envoy OIDC + Google/Entra | Envoy OIDC + Entra |

### 3.2 DFE Platform Components (Layer 2)

| Component | Chart/Operator | Purpose |
|-----------|---------------|---------|
| ArgoCD | argo-cd (Helm) + Valkey | GitOps engine |
| Strimzi Kafka | strimzi-kafka-operator | Message bus (or cloud Kafka swap-in) |
| ClickHouse | clickhouse-operator or ClickHouse Cloud | Analytics storage |
| CNPG PostgreSQL 17 | cloudnative-pg operator | Standardised DB |
| FerretDB | ferretdb (Helm) -> CNPG | MongoDB API for HyperDX |
| HyperDX | hyperdx (Helm) | Observability UI |
| OTel Collector | opentelemetry-collector (Helm) | Telemetry pipeline |
| KEDA | keda (Helm) | Autoscaling (OTel metrics) |
| Reloader | stakater-reloader (Helm) | Secret/ConfigMap hot-reload |
| cert-manager | cert-manager (Helm) | TLS automation |
| ESO | external-secrets (Helm) | Secret sync |
| external-dns | external-dns (Helm) | DNS automation (cloud only) |
| VPA | vertical-pod-autoscaler (Helm) | Right-sizing |
| metrics-server | metrics-server (Helm) | K8s resource metrics |

### 3.3 DFE Application Components

| Service | Language | Depends On |
|---------|----------|-----------|
| dfe-engine | Python 3.12 | ClickHouse, Kafka (topic creation), config git |
| dfe-ui | TypeScript/Next.js 16 | dfe-engine API, HyperDX (optional) |
| dfe-receiver | Rust (hyperi-rustlib) | Kafka, config |
| dfe-loader | Rust (hyperi-rustlib) | Kafka, ClickHouse, config |
| dfe-archiver | Rust (hyperi-rustlib) | Kafka, S3/MinIO, config |
| dfe-fetcher | Rust (hyperi-rustlib) | Kafka, external APIs, config |
| dfe-transform-* | Rust (hyperi-rustlib) | Kafka, config |

---

## 4. Key Gaps to Address

### 4.1 Critical Path (must solve before implementation)

1. **KEDA + OTel metrics integration** -- This is novel. Need to prototype and prove the scaling pipeline works. Recommend: deploy KEDA, test with Kedify OTEL Scaler or OTel Collector Prometheus exporter -> KEDA Prometheus trigger.

2. **Multi-cloud Terraform module structure** -- Need an abstraction layer that swaps cloud-specific modules while keeping the ArgoCD GitOps layer identical. Design the module interface before implementing any cloud target.

3. **Envoy Gateway OIDC parameterisation** -- The SecurityPolicy needs to accept different OIDC providers per deployment. Design a config-driven approach (env config YAML -> Helm values -> SecurityPolicy).

### 4.2 High Priority (needed for first deployment)

4. **dfe-engine OIDC** -- Add OIDCProvider alongside LocalAuthProvider. JWKS discovery, token validation, group-to-role mapping already designed.

5. **ArgoCD ApplicationSet definitions** -- Port the dfe-core ApplicationSet matrix generator pattern to DFE 2.2 with cloud-agnostic values.

6. **OTel auto-instrumentation** -- dfe-engine needs `opentelemetry-instrumentation-fastapi`. Rustlib needs unified `otel::init()` for metrics + traces + logs.

7. **Alerting migration** -- Translate dfe-core PrometheusRules into HyperDX/ClickHouse equivalent alerting.

### 4.3 Medium Priority (needed for production readiness)

8. **FerretDB workload testing** -- Validate HyperDX aggregation pipelines work with FerretDB.

9. **Secrets migration tooling** -- For existing dfe-core customers moving from AWS SM to OpenBao.

10. **Config repo decision** -- Mono-repo vs separate repo for DFE infra config (dfe-engine TODO lists this as high priority).

11. **Reloader deployment** -- Stakater Reloader for secret/ConfigMap change propagation.

12. **Node autoscaling per cloud** -- Karpenter (AWS), GKE Autopilot (GCP), AKS Node Autoprovisioning (Azure), static sizing (local).

---

## 5. Recommended Implementation Order

### Phase 1: Foundation
1. Repository structure (Terraform modules, GitOps layout, Helm values structure)
2. Local Rancher target (leveraging hyperi-infra Layer 1)
3. ArgoCD bootstrap (port app-of-apps from dfe-core)
4. Layer 2 operator deployment (Strimzi, ClickHouse, CNPG, KEDA)

### Phase 2: DFE Platform
5. OTel Collector + HyperDX + FerretDB deployment
6. KEDA + OTel metrics integration (prototype and prove)
7. Envoy Gateway OIDC configuration
8. DFE application deployment (engine, UI, Rust services)

### Phase 3: Multi-Cloud
9. AWS EKS target (Terraform module, cloud-specific values)
10. Secret management abstraction (OpenBao vs AWS SM vs GCP SM vs Azure KV)
11. Storage class abstraction
12. GCP GKE target
13. Azure AKS target

### Phase 4: Production Readiness
14. Tenancy sizing profiles (dev/small/large per cloud)
15. Config wizard / AWS Marketplace form
16. Alerting migration from PrometheusRules
17. Documentation and runbooks

---

## 6. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| KEDA + OTel metrics doesn't work cleanly | Medium | High | Prototype early. Fallback: OTel -> Prometheus bridge -> KEDA |
| FerretDB aggregation gaps break HyperDX | Medium | Medium | Workload test early. Fallback: native MongoDB |
| Envoy Gateway maturity on cloud targets | Low | Medium | Gateway API is standardised; implementation may vary. Test on each cloud. |
| IRSA -> Workload Identity migration complexity | High | High | Abstract workload identity early. Cloud-specific modules with common interface. |
| Strimzi operational overhead vs MSK | Medium | Medium | Document operational runbooks. Consider cloud Kafka as swap-in option. |
| Multi-cloud Terraform complexity explosion | Medium | High | Strict module interfaces. Cloud-specific modules must implement same outputs. |

---

## Appendix: Document Index

| Doc | Path | Covers |
|-----|------|--------|
| DFE Core Analysis | `docs/01-dfe-core-analysis.md` | TF + Helm + ArgoCD infra for DFE 2.1 |
| DFE Engine Analysis | `docs/02-dfe-engine-analysis.md` | Python control plane, API, config, auth |
| Rustlib Analysis | `docs/03-hyperi-rustlib-analysis.md` | Shared Rust lib, config, metrics, transport |
| DFE UI Analysis | `docs/04-dfe-ui-analysis.md` | Next.js UI, HyperDX integration |
| Best Practices Research | `docs/05-best-practices-research.md` | Web research on tech stack best practices |
| Hyperi-Infra Analysis | `docs/06-hyperi-infra-analysis.md` | Local Rancher K8s deployment |
| Synthesis (this doc) | `docs/07-synthesis.md` | Cross-cutting findings and recommendations |
