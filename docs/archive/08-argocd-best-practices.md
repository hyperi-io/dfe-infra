# ArgoCD Best Practices Analysis for DFE Infrastructure

**Date:** 2026-03-30
**Project:** dfe-infra (DFE 2.2)
**Scope:** Industry best practices, DFE 2.1 assessment, DFE 2.2 alignment, and recommendations

---

## 1. Current Industry Best Practices

### 1.1 Deployment Patterns

#### App-of-Apps vs ApplicationSets

The ArgoCD ecosystem has matured significantly since 2024. The community consensus is:

- **ApplicationSets are the recommended default for dynamic deployments.** They replace most uses of the app-of-apps pattern, providing declarative generators (Git directory, cluster, matrix, merge, pull request) that create Applications dynamically without Helm templating Application CRDs.
- **App-of-Apps (Level 3) remains valid for cluster bootstrapping** -- a single root Application that points to a directory of ApplicationSets or other Applications. This is the "empty cluster to fully operational" entrypoint.
- **Helm-templated Application CRDs are an anti-pattern in 2026.** Two layers of Helm templating (one to render the Application CRD, another to render the underlying chart) produces templates that are nearly impossible to debug. ApplicationSets eliminate this.

The recommended **3-level model**:

```
Level 1: Kubernetes manifests (Helm charts / Kustomize overlays)
         Self-contained -- deployable with plain helm install or kustomize build
Level 2: ApplicationSets wrapping Level 1 into ArgoCD Applications
Level 3: (Optional) A bootstrap Application pointing to the Level 2 directory
```

The critical discipline here is that Level 1 manifests must always be testable without ArgoCD. If you cannot `helm template` or `kustomize build` a chart and apply it manually, the chart is too coupled to ArgoCD.

#### Self-Managing ArgoCD

ArgoCD managing its own configuration (the "self-managing" pattern) is standard practice:

- ArgoCD is installed initially by an external process (CI pipeline, bootstrap script, Ansible playbook).
- An ApplicationSet or Application then points back at the ArgoCD Helm chart, making ArgoCD manage its own upgrades and configuration changes.
- The sync wave for the self-managing ArgoCD Application should be the highest wave (or at least after all infrastructure dependencies), since a failed ArgoCD upgrade could break the entire GitOps loop.
- **Circuit breaker:** Always maintain an out-of-band recovery path (the original bootstrap script or Helm command). If the self-managing sync corrupts ArgoCD, you need to be able to reinstall it manually.

#### Multi-Source Applications

Multi-source Applications (GA since ArgoCD 2.6) are the best practice for deploying third-party Helm charts with custom values:

```yaml
sources:
  - repoURL: https://charts.example.com
    chart: some-chart
    targetRevision: 1.2.3
    helm:
      valueFiles:
        - $values/addons/helm/some-chart/common.yaml
        - $values/addons/helm/some-chart/dev.yaml
  - repoURL: https://github.com/org/infra.git
    targetRevision: HEAD
    ref: values
```

This separates chart source (upstream registry) from value customisation (your git repo), avoiding the need to fork or vendor third-party charts.

### 1.2 ApplicationSet Generators

#### Matrix Generator

The matrix generator is the most powerful pattern for parameterising deployments. It combines two or more generators, producing the Cartesian product of their outputs. The standard use case:

- **Generator A:** Cluster generator (reads cluster secrets, including annotations)
- **Generator B:** List or Git generator (defines which charts to deploy)

The matrix generator's key value is **bridging infrastructure outputs to GitOps**. By reading cluster secret annotations populated by Terraform, ApplicationSets can reference dynamic values (domain names, cloud account IDs, storage class names, workload identity annotations) without hardcoding them in Helm values.

Best practices for matrix generators:

- **Enable Go templates** with `goTemplateOptions: ["missingkey=error"]`. This catches typos in annotation references at sync time rather than producing silent empty values.
- **Use multiple ApplicationSets by type**, not a single monolithic ApplicationSet. Group by lifecycle: infrastructure operators (cert-manager, ESO, KEDA) in one, data services (Kafka, ClickHouse, PostgreSQL) in another, application workloads in a third. This limits the blast radius of ApplicationSet changes.
- **Escape double curly braces** when deploying ApplicationSets via Helm: `{{ printf "{{cluster}}" }}` or use `{{` "{{" `}}`. This is required because both Helm and ArgoCD use the same `{{ }}` delimiters.

#### Merge Generator

The merge generator (stable since ArgoCD 2.9) is useful when you need defaults with per-cluster overrides:

```yaml
generators:
  - merge:
      mergeKeys: [server]
      generators:
        - clusters:
            selector: { matchLabels: { argocd.argoproj.io/secret-type: cluster } }
        - list:
            elements:
              - server: https://kubernetes.default.svc
                replicaCount: "5"
```

This is cleaner than encoding all overrides in cluster secret annotations. The merge generator provides a declarative "defaults + overrides" pattern within the ApplicationSet itself.

#### Git Directory Generator

For monorepos where each subdirectory represents a deployable application:

```yaml
generators:
  - git:
      repoURL: https://github.com/org/apps.git
      directories:
        - path: apps/*
```

This auto-discovers new applications when directories are added -- no ApplicationSet changes required. Best for application teams that own many microservices.

### 1.3 Sync Waves and Ordering

Sync waves control deployment order within ArgoCD. The annotation `argocd.argoproj.io/sync-wave` accepts an integer (negative values go first, default is 0).

#### Best Practices for Wave Design

1. **CRD-first rule:** Operators and CRDs must deploy before resources that depend on them. If CNPG operator is in wave 3, CNPG Cluster resources must be in wave 4 or later.
2. **Keep the wave range narrow.** 5-7 waves is typical. More waves increase sync time and make debugging harder.
3. **Infrastructure before data, data before applications.** A proven ordering:
   - Wave 0-1: Namespaces, RBAC, CRDs
   - Wave 2: Infrastructure operators (cert-manager, ESO, ingress/gateway)
   - Wave 3: Platform operators (KEDA, CNPG, Strimzi, ClickHouse operator, VPA, Reloader)
   - Wave 4: Stateful data services (Kafka cluster, PostgreSQL cluster, ClickHouse cluster)
   - Wave 5: Applications (ArgoCD self-managing, DFE services, observability UIs)
4. **Do not use sync waves as a health-check dependency mechanism.** Sync waves control creation order, not readiness gates. A wave 3 resource will be created after wave 2 resources are created, but ArgoCD does not wait for wave 2 resources to be healthy before proceeding (unless `argocd.argoproj.io/sync-wave` is combined with health checks and `syncOptions: [ApplyOutOfSyncOnly=true]`).
5. **Use `PruneLast=true` for infrastructure resources** to prevent accidental deletion during sync.

#### Pre-Sync and Post-Sync Hooks

ArgoCD supports `PreSync`, `Sync`, `PostSync`, and `SyncFail` hooks. These are useful for:

- **PreSync:** Database migrations, schema validation, backup creation
- **PostSync:** Smoke tests, notification triggers, cache warming
- **SyncFail:** Alerting, rollback triggers

Hooks run as Kubernetes Jobs. Use `argocd.argoproj.io/hook-delete-policy: HookSucceeded` to clean up completed Jobs automatically.

### 1.4 Secrets and External Secrets Operator (ESO)

#### The GitOps Secrets Problem

Secrets cannot be stored in git. The two dominant approaches in production ArgoCD deployments:

1. **External Secrets Operator (ESO)** -- The recommended approach. ESO syncs secrets from external stores (Vault/OpenBao, AWS SM, GCP SM, Azure KV, 1Password, etc.) into Kubernetes Secrets. ESO is a CNCF project, supports 20+ providers, and integrates cleanly with GitOps because the `ExternalSecret` CRD (which is safe to commit) is the declarative specification, and the actual secret data lives outside git.

2. **SOPS + age/KMS** -- Encrypts secrets in-place in git. Decrypted at deploy time. Works but has operational complexity around key management and rotation.

#### ESO Best Practices

- **Use `ClusterSecretStore` for shared secrets backends.** One ClusterSecretStore per secrets provider, referenced by ExternalSecrets across namespaces. Avoids duplicating provider configuration.
- **Do not hardcode region or endpoint** in ClusterSecretStore YAML. Parameterise via Helm values fed from the cluster secret annotation bridge.
- **Set refresh intervals appropriately.** Default 1h is fine for most secrets. For secrets that rotate frequently (database passwords, API tokens), use 5-15 minutes. For static secrets (TLS certs managed by cert-manager), longer intervals reduce API calls.
- **Use Stakater Reloader** alongside ESO. ESO updates the Kubernetes Secret, but pods do not automatically restart. Reloader watches for Secret/ConfigMap changes and triggers rolling restarts.
- **Separate ESO installation from ESO configuration.** Install the ESO operator via Helm at an early sync wave. Deploy ClusterSecretStore and ExternalSecret resources at a later wave. The operator must be healthy before the CRDs are applied.

#### Secret Rotation Pattern

```
Secrets backend (OpenBao / Cloud SM) — secret rotated
    |
    v (refresh interval)
ESO syncs new value to K8s Secret
    |
    v (watch trigger)
Stakater Reloader detects Secret change
    |
    v
Rolling restart of affected Deployments/StatefulSets
```

This pattern provides automated secret rotation with zero manual intervention. The end-to-end latency is bounded by the ESO refresh interval plus the Reloader detection interval (typically under 2 minutes combined).

### 1.5 Scaling and High Availability

#### ArgoCD Controller Scaling

ArgoCD's application controller is the bottleneck in large deployments. Best practices:

- **Shard by cluster** for multi-cluster deployments. ArgoCD 2.8+ supports dynamic controller sharding where the number of controller replicas is independent of the number of clusters.
- **Tune reconciliation frequency.** The default `--app-resync` is 180s (3 minutes). For environments where drift detection speed matters, reduce to 60s. For large deployments (500+ Applications), increase to 300s to reduce API server load.
- **Enable server-side diff** (`--server-side-diff`). This offloads diff computation to the Kubernetes API server, reducing ArgoCD controller memory usage by 40-60% in large deployments.
- **Set resource exclusions** for high-churn resources (Events, EndpointSlices) that ArgoCD does not need to track.

#### ArgoCD Redis/Valkey HA

ArgoCD uses Redis (or Valkey) as an ephemeral cache for repository state, RBAC cache, and UI state. In HA mode:

- Deploy 3 Redis/Valkey replicas with HAProxy or Sentinel for automatic failover.
- Since the cache is ephemeral, total loss of the cache layer causes temporary slowness (ArgoCD rebuilds from git), not data loss.
- **Valkey is the recommended replacement for Redis.** The swap is a container image change (`redis:7-alpine` to `valkey/valkey:8-alpine`) with no data migration required. ArgoCD rebuilds its cache on restart.

#### Repo Server Scaling

For deployments with many Helm charts or large git repos:

- Increase repo server replicas (2-3 for production).
- Enable `--parallelism-limit` to control concurrent manifest generation.
- Use shallow clones (`--revision-cache-expiration` and `--repo-server-timeout-seconds`) to reduce git operations.
- Mount the repo server cache on a fast volume (not emptyDir on slow cloud disks).

### 1.6 GitOps Maintenance

#### Drift Detection and Self-Healing

- **Enable automated sync with self-heal:** `automated: { selfHeal: true, prune: true }`. Self-heal reverts manual changes to the cluster (kubectl edits that deviate from git). Prune removes resources that no longer exist in git.
- **Configure `ignoreDifferences` for expected drift.** Certain fields are legitimately modified by controllers:
  - Deployment `/spec/replicas` (managed by HPA/KEDA)
  - Service `/spec/clusterIP` (assigned by K8s)
  - MutatingWebhookConfiguration `/webhooks/*/clientConfig/caBundle` (injected by cert-manager)
  - CRD status fields
- **Add custom health checks** for CRDs that ArgoCD does not understand natively. KEDA ScaledObjects, Strimzi KafkaTopics, and CNPG Clusters all benefit from custom health checks in `argocd-cm` so that ArgoCD reports accurate sync status.

#### Repository Hygiene

- **Pin chart versions explicitly** in ApplicationSets (`targetRevision: "1.2.3"`, not `*` or `HEAD`). Unpinned versions cause unpredictable upgrades.
- **Use `ignoreMissingValueFiles: true`** for default/override value file patterns (`common.yaml` + optional `dev.yaml`). This allows clean environment-specific overrides without requiring every file to exist.
- **Separate infrastructure and application repos.** Infrastructure changes (Terraform, ArgoCD config) have different review and blast radius than application changes (dfe-engine version bumps). Cross-repo ArgoCD support (multi-source, multiple ApplicationSets) makes this clean.

#### Notifications and Observability

- **ArgoCD Notifications** (built-in since 2.4) should be configured for sync failures, health degradation, and out-of-sync detection. Common targets: Slack, PagerDuty, webhook.
- **Monitor ArgoCD itself** using the `/metrics` endpoint. Key metrics:
  - `argocd_app_sync_total` -- sync success/failure rate
  - `argocd_app_health_status` -- application health distribution
  - `argocd_app_reconcile_duration` -- controller performance
  - `argocd_repo_server_request_duration` -- repo server latency

### 1.7 Multi-Cloud GitOps

#### Cluster Secret as the Abstraction Layer

The **cluster secret annotation bridge** is the industry-standard pattern for multi-cloud ArgoCD deployments. Terraform (or any IaC tool) writes cloud-specific outputs as annotations on the ArgoCD cluster secret (`argocd.argoproj.io/secret-type: cluster`). ApplicationSets read these annotations and inject them into Helm values. The GitOps layer (ApplicationSets, Helm charts) never references a specific cloud.

This pattern's strength is that it creates a clean interface contract:

```
Terraform (cloud-specific) ---> Cluster secret annotations (interface) ---> ArgoCD (cloud-agnostic)
```

Adding a new cloud target means writing a new Terraform module that populates the same annotation keys. The ArgoCD layer does not change.

#### Multi-Cluster Management

For organizations with multiple clusters (dev/staging/prod, or multi-region):

- **Register external clusters** in ArgoCD via `argocd cluster add`. Each cluster gets its own cluster secret with annotations.
- **Use the cluster generator** in ApplicationSets to deploy the same stack across all registered clusters, with per-cluster overrides from annotations.
- **Centralize ArgoCD in a management cluster.** Do not run ArgoCD in every target cluster. One ArgoCD instance manages all clusters, reducing operational overhead and providing a single pane of glass.
- **Consider ArgoCD ApplicationSet progressive rollout** (rolling sync) for deploying changes across clusters incrementally. The `rollingSync` strategy (available since ApplicationSet controller 0.5) supports `maxUpdate` to limit concurrent syncs.

#### Cluster Secret Annotations as Interface Contract

The annotation key set should be treated as a versioned API:

| Category | Example Keys | Purpose |
|----------|-------------|---------|
| Identity | `tenancy_name`, `cloud`, `env`, `region` | Deployment identity |
| Networking | `public_domain`, `cluster_endpoint` | DNS and ingress |
| Storage | `storage_class`, `backup_bucket` | Persistent storage |
| Secrets | `secrets_provider`, `secrets_region` | ESO configuration |
| Auth | `oidc_issuer`, `oidc_client_id` | OIDC configuration |
| Workload Identity | `wi_annotation_key`, `wi_<service>_annotation_value` | Cloud IAM |
| Data Services | `clickhouse_host`, `kafka_brokers` | Connection strings |

### 1.8 ArgoCD and Helm Integration

#### Values Precedence

ArgoCD applies Helm values in this order (last wins):

```
chart values.yaml < valueFiles < values (inline YAML) < valuesObject < parameters
```

Best practice: use `valueFiles` for structured configuration (common.yaml, environment overrides) and reserve `parameters` only for single-value overrides in ApplicationSets (e.g., image tags from CI).

#### Common Helm Anti-Patterns with ArgoCD

1. **Mixing `values`, `valuesObject`, `valueFiles`, and `parameters`** in the same source. Pick one primary approach and stick to it. `valueFiles` (multi-source from git) is the cleanest for infrastructure.

2. **Helm hooks conflicting with ArgoCD sync.** Helm hooks (`helm.sh/hook`) and ArgoCD hooks (`argocd.argoproj.io/hook`) are different systems. When deploying Helm charts via ArgoCD, disable Helm hooks and use ArgoCD hooks instead, or set `--skip-crds` if Helm install hooks conflict with ArgoCD CRD management.

3. **Not testing Helm charts independently.** Every chart must be deployable with `helm install` outside ArgoCD. If a chart only works when deployed through ArgoCD (because it depends on ArgoCD-specific template variables), the chart is broken.

4. **Large inline `values` blocks in ApplicationSets.** These become unreadable at scale. Move values to git-hosted files and reference via multi-source.

#### Server-Side Apply (SSA)

SSA (`syncOptions: [ServerSideApply=true]`) is recommended for:

- CRDs with large schemas (>256KB annotation limit for client-side apply)
- Resources managed by multiple controllers (ArgoCD + cert-manager, ArgoCD + KEDA)
- Any resource where client-side apply produces `metadata.managedFields` conflicts

SSA is required for CRDs like Strimzi Kafka, CNPG Cluster, and ClickHouse Installation that exceed the annotation size limit.

---

## 2. DFE 2.1 Implementation Assessment

### 2.1 What Worked Well

**1. Cluster secret annotation bridge (strong pattern)**

The `argocd_init.yaml.tpl` template injects Terraform outputs as annotations on the ArgoCD in-cluster secret. Every ApplicationSet reads `{{ .metadata.annotations.tenancy_name }}`, `{{ .metadata.annotations.public_domain }}`, etc. This is the correct pattern and remains the industry best practice for bridging IaC outputs to GitOps.

The DFE 2.1 annotation set covers: `tenancy_name`, `account_id`, `aws_pca_arn`, `karpenter_sqs_arn`, `prometheus_endpoint`, `ebs_kms_id`, `addons_repo`, `workloads_repo`, `region`, `vpc_id`, `public_domain`, `clickhouse_host`, `oidc_url`, `tenancy_size`. This is a reasonable set for AWS-only deployments.

**2. ApplicationSet matrix generator (correct architecture)**

All 24 ApplicationSets in `gitOps/addons/argo_apps/` use the matrix generator combining cluster annotations with list elements. This is the right choice -- it avoids app-of-apps Helm templating and provides clean parameterisation.

**3. Sync wave ordering (well-designed)**

The 4-wave structure (2: ingress, 3: core infra, 4: monitoring, 5: apps) respects dependency order. ingress-nginx at wave 2 ensures the gateway is available before cert-manager and external-dns at wave 3. Monitoring at wave 4 means Prometheus can scrape services deployed at wave 3. Applications at wave 5 can rely on all infrastructure.

**4. Self-managing ArgoCD (correct)**

ArgoCD at sync wave 5 manages its own configuration. The initial install is via Helm in the CI pipeline, then the ApplicationSet takes over. This follows best practice.

**5. Sync policies (well-configured)**

Automated sync with `prune: true` and `selfHeal: true` on all ApplicationSets. `CreateNamespace=true`, `Validate=true`, `PruneLast=true`. Some use `ServerSideApply=true`. Retry with exponential backoff. This is production-grade configuration.

**6. Multi-source Applications**

The `$values` ref pattern for Helm values from git is used throughout, separating chart sources from value customisation. This follows best practice.

**7. AppProject RBAC separation**

Three projects (`infra`, `vector`, `dfe-apps`) with appropriate source/destination restrictions. This provides least-privilege at the ArgoCD level.

**8. Tenancy sizing profiles**

The `.auto.tfvars` approach (dev/small/large) produces different resource allocations per component. This is a clean pattern.

### 2.2 What Did Not Work Well

**1. Hardcoded cloud-specific values in GitOps YAML**

The `ClusterSecretStore` has `region: ap-southeast-2` hardcoded. Other YAML files hardcode a literal `cluster.name`. These should be parameterised via the annotation bridge or Helm values. Hardcoded values break the multi-cloud abstraction that the annotation bridge is designed to provide.

**2. Single monolithic `stage_1/` Terraform root**

All modules are invoked from a single root module. This creates a long `terraform apply` (all-or-nothing), makes partial re-applies risky, and produces a single large state file. Best practice is separate state per concern (networking, K8s cluster, data services).

**3. CI pipeline as the only bootstrap path**

The CI pipeline (`core.yml`) is the only way to deploy. There is no idempotent standalone bootstrap script. This means local development, disaster recovery, and manual intervention all depend on GitHub Actions being available.

**4. Missing `ignoreDifferences` for KEDA**

KEDA is deployed (v2.16.1) but ApplicationSets do not configure `ignoreDifferences` for Deployment replica counts. When KEDA scales a Deployment, ArgoCD sees the replica count as drift and tries to revert it. This causes a fight between KEDA and ArgoCD.

**5. OAuth2-proxy + Redis for session management**

OAuth2-proxy with Redis is an extra layer of complexity that Envoy Gateway's native OIDC SecurityPolicy eliminates. In DFE 2.1, every OIDC-protected service requires oauth2-proxy configuration, Redis availability, and Cognito client setup. This is three moving parts where one (Envoy Gateway) suffices.

**6. AWS Cognito as the IdP**

Cognito is deeply embedded: 4 app clients, Dex OIDC connector, oauth2-proxy integration. This is not portable. Any non-AWS deployment requires replacing the entire auth flow.

**7. Prometheus + Grafana + CloudWatch + FluentBit fragmentation**

Four separate systems for observability: Prometheus (in-cluster metrics), AWS Managed Prometheus (remote write), Grafana (dashboards), CloudWatch (logs via FluentBit). This creates operational complexity and vendor lock-in. Consolidated OTel pipelines are the industry direction.

**8. No custom health checks for CRDs**

CNPG Clusters, KEDA ScaledObjects, and MSK-related resources do not have custom ArgoCD health checks. ArgoCD cannot accurately report the health of these resources, leading to misleading sync status.

### 2.3 What is Outdated

**1. ingress-nginx (retiring March 2026)**

Ingress-NGINX Controller is being retired with no further releases, bug fixes, or security updates. DFE 2.1's reliance on it (`ingress-nginx` v4.12.1) is a dead end. Gateway API via Envoy Gateway, Traefik, or NGINX Gateway Fabric is the replacement path.

**2. Redis (license change)**

Redis moved to BUSL, violating CNCF policy. Valkey (BSD-3, Linux Foundation) is the drop-in replacement. ArgoCD's own community has an active proposal to switch. The DFE 2.1 Redis usage (ArgoCD cache + oauth2-proxy sessions) should migrate to Valkey.

**3. AWS ALB Controller alongside ingress-nginx**

Running both `aws-load-balancer-controller` and `ingress-nginx` is redundant complexity. The ALB Controller provisions AWS NLBs/ALBs for Service resources annotated with AWS-specific annotations. Envoy Gateway subsumes this for HTTP/HTTPS traffic.

**4. Dex for OIDC brokering**

While Dex is not deprecated, using Dex to broker Cognito OIDC to ArgoCD is unnecessary when Envoy Gateway handles OIDC at the gateway level. ArgoCD behind Envoy Gateway receives pre-authenticated requests with identity headers, eliminating the need for ArgoCD's own OIDC flow.

**5. FluentBit for log collection**

FluentBit to CloudWatch is AWS-specific. OTel Collector (DaemonSet + Gateway pattern) replaces FluentBit for log collection, adds metrics and traces, and routes to any backend.

**6. Karpenter as the sole autoscaler**

Karpenter is AWS-only. For multi-cloud, cloud-native autoscalers (GKE Autopilot, AKS Node Autoprovisioning) or KEDA with node-level triggers are needed. The DFE 2.1 approach of 3 Karpenter NodePools is not portable.

---

## 3. DFE 2.2 Alignment with Best Practices

### 3.1 Strongly Aligned

**ApplicationSet matrix generator with cluster secret annotation bridge**

DFE 2.2 preserves the core pattern from DFE 2.1 and extends the annotation set to include cloud-agnostic keys: `cloud`, `storage_class`, `secrets_provider`, `workload_identity_annotations`, and OIDC configuration. This is exactly the right approach. The annotation set functions as a versioned interface contract between Terraform modules and the GitOps layer.

**Two-layer architecture (Layer 1 bootstrap, Layer 2 ArgoCD-managed)**

The clean separation between Layer 1 (cluster, gateway, cert-manager, ESO, ArgoCD) and Layer 2 (everything else) follows the best practice of bootstrapping minimum viable infrastructure before handing off to GitOps. Layer 1 varies by cloud target; Layer 2 is identical everywhere. This is the correct architecture for multi-cloud.

**cert-manager and ESO bootstrapped before ArgoCD, then adopted**

This solves the chicken-and-egg problem elegantly. cert-manager and ESO are installed by the bootstrap script (Layer 1) because ArgoCD needs TLS certificates and secrets to start. ArgoCD then adopts them at sync wave 2, taking over lifecycle management. This is a well-known pattern (sometimes called "adopt-and-manage") that avoids the circular dependency of ArgoCD trying to deploy its own prerequisites.

The key detail: the bootstrap Helm releases and the ArgoCD ApplicationSets must use the same Helm release name and namespace. ArgoCD's `--force` or `replace` sync option may be needed for the initial adoption sync if there are annotation/label differences between the bootstrap install and the ArgoCD-managed state.

**Envoy Gateway replacing nginx-ingress**

Gateway API is the Kubernetes networking standard going forward. Envoy Gateway provides native OIDC SecurityPolicy, TLS termination, rate limiting, and header manipulation without auxiliary components (oauth2-proxy, Dex). DFE 2.2's choice to consolidate ingress, auth, and traffic policy into Envoy Gateway is forward-looking and reduces component count.

**ESO + OpenBao/cloud SM for secrets**

ESO with pluggable providers (OpenBao for local, cloud SM for cloud targets) is the best practice. The `ClusterSecretStore` CRD parameterised via cluster secret annotations (provider type, region, endpoint) eliminates the DFE 2.1 hardcoding problem.

**Valkey replacing Redis for ArgoCD cache**

Correct and low-risk. ArgoCD's cache is ephemeral. The swap is a container image change with zero data migration. DFE 2.2 also eliminates the oauth2-proxy Redis dependency entirely by moving OIDC to Envoy Gateway.

**OTel + ClickHouse + HyperDX replacing fragmented observability**

Consolidating from four systems (Prometheus, Grafana, CloudWatch, FluentBit) to a unified pipeline (OTel Collector -> ClickHouse -> HyperDX) reduces operational complexity and eliminates AWS-specific dependencies. The two-tier OTel Collector deployment (DaemonSet per node + Gateway Deployment) is the standard production pattern.

**Sync waves 2-5**

The DFE 2.2 wave structure is well-designed:

| Wave | Components | Rationale |
|------|-----------|-----------|
| 2 | cert-manager (adopted), ESO (adopted), Envoy Gateway, external-dns | Infrastructure prerequisites -- TLS, secrets, ingress, DNS |
| 3 | KEDA, metrics-server, VPA, Reloader, CNPG operator, Strimzi operator, ClickHouse operator | Platform operators -- must be running before their CRs |
| 4 | CNPG PostgreSQL, ClickHouse cluster, Strimzi Kafka, FerretDB, OTel Collector | Stateful data services -- operators from wave 3 must be ready |
| 5 | HyperDX, ArgoCD (self-managing), DFE services | Applications -- all infrastructure available |

This respects the CRD-first rule (operators at wave 3, their custom resources at wave 4) and keeps the wave range narrow (4 waves).

### 3.2 Areas of Divergence (Worth Evaluating)

**1. Envoy Gateway handling ArgoCD OIDC instead of ArgoCD-native OIDC**

DFE 2.2 disables ArgoCD's built-in Dex and routes ArgoCD through Envoy Gateway for OIDC. This is architecturally clean (single auth layer) but has a trade-off: ArgoCD's CLI (`argocd login`) expects ArgoCD's own OIDC flow. Users authenticating via the CLI will need to use `--sso` with a browser-based flow that redirects through Envoy Gateway, or ArgoCD must be configured with `--insecure` behind the gateway (since TLS terminates at Envoy). This is workable but requires explicit documentation for CLI users.

**2. Kedify OTEL Scaler as default (novel integration)**

The Kedify OTEL Scaler (direct OTLP push from OTel Collector to KEDA) is a newer approach that has not been widely battle-tested in production. The fallback (OTel Collector Prometheus endpoint -> KEDA Prometheus trigger) is proven and well-documented. DFE 2.2's approach of defaulting to Kedify with a documented fallback is pragmatic, but the fallback should be implemented first and the Kedify path treated as an upgrade.

**3. No dedicated ArgoCD notifications configuration**

The DFE 2.2 design spec does not mention ArgoCD Notifications. Since HyperDX replaces Grafana for dashboards, there is no Grafana alerting. Sync failure notifications should be configured via ArgoCD's built-in notification controller, targeting a webhook, Slack, or the OTel pipeline itself.

**4. Single-cluster ArgoCD (no management cluster)**

DFE 2.2 deploys ArgoCD within each target cluster (not a central management cluster). This is appropriate for the current scope (single tenant per cluster) but differs from the multi-cluster best practice of centralising ArgoCD. If DFE scales to managing multiple clusters per customer, a management cluster pattern should be considered.

---

## 4. Recommendations

### 4.1 Critical (Pre-Implementation)

**R1. Define the cluster secret annotation contract as a versioned schema**

The annotation keys bridging Terraform to ArgoCD are the most important interface in the system. Document the full set of required and optional annotation keys, their types, and which Terraform module populates each one. Treat additions as non-breaking (new optional keys) and removals as breaking (requires ApplicationSet updates). Consider a validation Job or PreSync hook that checks all required annotations are present before syncing.

**R2. Implement `ignoreDifferences` for KEDA-managed Deployments**

Every ApplicationSet deploying a KEDA-scaled workload must include:

```yaml
ignoreDifferences:
  - group: apps
    kind: Deployment
    jsonPointers:
      - /spec/replicas
```

Without this, ArgoCD and KEDA will fight over replica counts, causing perpetual out-of-sync status and unnecessary pod restarts.

**R3. Implement the OTel-to-KEDA fallback path first**

Deploy the OTel Collector Prometheus exporter (`:8889`) and KEDA Prometheus trigger as the initial scaling mechanism. This is proven and well-documented. Once operational, evaluate the Kedify OTEL Scaler as an upgrade. Do not block the initial deployment on Kedify maturity.

**R4. Add custom ArgoCD health checks for CRDs**

Configure `resource.customizations.health` in `argocd-cm` for:

- `kafka.strimzi.io/Kafka` -- check `.status.conditions` for `Ready`
- `postgresql.cnpg.io/Cluster` -- check `.status.phase` for `Cluster in healthy state`
- `clickhouse.altinity.com/ClickHouseInstallation` -- check `.status.status` for `Completed`
- `keda.sh/ScaledObject` -- check `.status.conditions` for `Ready`
- `gateway.envoyproxy.io/Gateway` -- check `.status.conditions` for `Programmed`

Without these, ArgoCD will report CRDs as "Healthy" or "Progressing" based on generic rules, masking actual failures.

### 4.2 High Priority (First Deployment)

**R5. Configure ArgoCD Notifications for sync failures**

Deploy the ArgoCD Notifications controller with at minimum:

- Webhook trigger on `on-sync-failed` -> OTel Collector (for HyperDX visibility)
- Optional Slack/email trigger for operations team
- Trigger on `on-health-degraded` for application health monitoring

This is a small configuration addition with high operational value.

**R6. Parameterise the ClusterSecretStore via Helm values**

The DFE 2.1 mistake of hardcoding `region: ap-southeast-2` must not be repeated. The ClusterSecretStore template should read all configuration from the cluster secret annotations:

```yaml
provider:
  {{ if eq .Values.secretsProvider "vault" }}
  vault:
    server: {{ .Values.vaultEndpoint }}
    path: {{ .Values.vaultPath }}
    auth:
      kubernetes:
        mountPath: kubernetes
        role: {{ .Values.vaultRole }}
  {{ else if eq .Values.secretsProvider "aws" }}
  aws:
    service: SecretsManager
    region: {{ .Values.awsRegion }}
  {{ end }}
```

These values flow from cluster secret annotations through the ApplicationSet matrix generator.

**R7. Test Helm charts independently of ArgoCD**

Establish a CI step that runs `helm template` on every chart with representative values. This validates that Level 1 manifests are self-contained and do not depend on ArgoCD-specific variables. This catches template errors before they reach ArgoCD.

**R8. Document the bootstrap adoption mechanism**

The cert-manager and ESO adoption (bootstrap installs, ArgoCD adopts) needs explicit documentation:

- The bootstrap script's Helm release names must match the ArgoCD ApplicationSet names.
- The first ArgoCD sync of adopted resources may show diff due to label/annotation differences. Document whether to use `Replace=true` or manual annotation alignment.
- Include a verification step: after adoption, confirm ArgoCD shows the adopted resources as "Synced" and "Healthy".

### 4.3 Medium Priority (Production Hardening)

**R9. Split ApplicationSets by lifecycle domain**

Group ApplicationSets into separate files by lifecycle:

- `infra-operators.yaml` -- cert-manager, ESO, Envoy Gateway, external-dns, Reloader, metrics-server, VPA
- `data-operators.yaml` -- CNPG operator, Strimzi operator, ClickHouse operator, KEDA
- `data-services.yaml` -- CNPG Cluster, Strimzi Kafka, ClickHouse cluster, FerretDB
- `observability.yaml` -- OTel Collector, HyperDX
- `dfe-apps.yaml` -- dfe-engine, dfe-ui, dfe-receiver, dfe-loader, dfe-archiver, dfe-fetcher, dfe-transform-*
- `argocd-self.yaml` -- ArgoCD self-managing

This limits the blast radius of changes (modifying a DFE app ApplicationSet does not risk re-syncing infrastructure operators) and makes code review easier.

**R10. Enable Go template strict mode on all ApplicationSets**

Add `goTemplateOptions: ["missingkey=error"]` to every ApplicationSet. This causes sync failures (rather than silent empty values) when an annotation key is referenced but missing from the cluster secret. This catches misconfiguration immediately rather than deploying with empty values that cause runtime failures.

**R11. Configure resource exclusions**

Add to `argocd-cm`:

```yaml
resource.exclusions: |
  - apiGroups: [""]
    kinds: ["Event"]
  - apiGroups: ["discovery.k8s.io"]
    kinds: ["EndpointSlice"]
  - apiGroups: ["metrics.k8s.io"]
    kinds: ["*"]
```

These high-churn resources waste controller cycles and provide no GitOps value.

**R12. Implement progressive rollout for multi-cluster**

When DFE 2.2 manages multiple clusters (dev -> staging -> prod), use the ApplicationSet `rollingSync` strategy:

```yaml
strategy:
  type: RollingSync
  rollingSync:
    steps:
      - matchExpressions:
          - key: env
            operator: In
            values: [dev]
      - matchExpressions:
          - key: env
            operator: In
            values: [staging]
        maxUpdate: 1
      - matchExpressions:
          - key: env
            operator: In
            values: [prod]
        maxUpdate: 1
```

This ensures changes roll through environments in order, with manual or automated gates between stages.

### 4.4 Long-Term (Multi-Cloud Maturity)

**R13. Consider a management cluster pattern**

As DFE scales beyond single-cluster deployments, evaluate centralising ArgoCD in a lightweight management cluster that manages all target clusters. This provides:

- Single pane of glass for all deployments
- ArgoCD availability independent of target cluster health
- Centralised RBAC and audit logging
- Reduced resource overhead on target clusters

**R14. Evaluate ArgoCD Image Updater**

For DFE application images (dfe-engine, dfe-ui, Rust services), ArgoCD Image Updater can automatically detect new container images in the registry and update Helm values, committing the change back to git. This removes the need for CI pipelines to update image tags in git after builds.

**R15. Plan for ApplicationSet controller scaling**

The ApplicationSet controller runs as a single replica by default. For deployments with 50+ Applications across multiple clusters, the controller becomes a bottleneck. Monitor `argocd_appset_reconcile_duration` and plan to scale the controller or shard by namespace if latency increases.

---

## Appendix: DFE 2.1 to 2.2 ArgoCD Migration Checklist

| DFE 2.1 Component | DFE 2.2 Equivalent | Migration Action |
|-------------------|--------------------|-----------------|
| `argocd_init.yaml.tpl` (cluster secret) | Bootstrap script `kubectl apply cluster-secret` | Extend annotations with cloud-agnostic keys |
| CI `helm install argocd` | Bootstrap script `helm upgrade --install argocd` (with Valkey) | Replace Redis image with Valkey; add Valkey Helm values |
| `argocd_bootstrap.yaml` | `argocd-bootstrap.yaml` | Port AppProject definitions; add `data` project |
| `cluster_bootstrap.yaml` | `cluster-addons.yaml` | Port root ApplicationSet reference |
| 24 ApplicationSets in `gitOps/addons/argo_apps/` | Reorganised ApplicationSets in `addons/argo_apps/` | Remove AWS-specific charts (ALB, Karpenter, FluentBit); add Envoy Gateway, OTel, HyperDX, Strimzi, ClickHouse operator |
| `gitOps/addons/helm/` value directories | `addons/helm/` value directories | Restructure with `common.yaml` + cloud-specific overrides |
| Sync waves 2-5 | Sync waves 2-5 (same range, refined contents) | Move cert-manager and ESO to wave 2 (adopted); add operators to wave 3; add data services to wave 4 |
| Dex OIDC connector | Disabled (Envoy Gateway handles OIDC) | Remove Dex config from ArgoCD Helm values |
| Redis (ArgoCD cache) | Valkey | Image swap in Helm values |
| Redis (oauth2-proxy sessions) | Eliminated | oauth2-proxy removed entirely |
| Prometheus + Grafana | OTel Collector + HyperDX | New ApplicationSets; migrate alerting rules |
| ingress-nginx | Envoy Gateway | New ApplicationSet; migrate Ingress to HTTPRoute |
| `ServerSideApply=true` (selective) | `ServerSideApply=true` (broader) | Enable for all CRDs exceeding annotation size limit |
| No `ignoreDifferences` for KEDA | `ignoreDifferences` for KEDA-managed Deployments | Add to all DFE service ApplicationSets |
| Hardcoded `region: ap-southeast-2` | Parameterised via annotation bridge | All cloud-specific values flow from annotations |
