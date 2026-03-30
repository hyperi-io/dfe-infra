# DFE Infrastructure Best Practices Research

> **Date:** 2026-03-30
> **Scope:** Migration from AWS-specific Terraform+Helm+ArgoCD to multi-cloud (Rancher local -> AWS -> GCP -> Azure)
> **Status:** Research complete -- ready for architecture review

---

## Table of Contents

1. [Multi-Cloud Terraform Patterns](#1-multi-cloud-terraform-patterns)
2. [ArgoCD + Valkey (Redis Replacement)](#2-argocd--valkey-redis-replacement)
3. [KEDA Autoscaling](#3-keda-autoscaling)
4. [OpenTelemetry -> ClickHouse Pipeline](#4-opentelemetry--clickhouse-pipeline)
5. [FerretDB over PostgreSQL](#5-ferretdb-over-postgresql)
6. [OIDC/OAuth2 for Platform Auth](#6-oidcoauth2-for-platform-auth)
7. [Multi-Cloud Kubernetes Deployment](#7-multi-cloud-kubernetes-deployment)
8. [DFE/ETL Pipeline Best Practices](#8-dfeetl-pipeline-best-practices)
9. [OpenVPN for Edge/Hybrid Connectivity](#9-openvpn-for-edgehybrid-connectivity)

---

## 1. Multi-Cloud Terraform Patterns

### 1.1 Abstraction Layer Architecture

The consensus pattern for multi-cloud Terraform is a **thin abstraction layer** that defines a common interface with cloud-specific implementations underneath. The recommended directory structure:

```
modules/
  compute/
    interface/     # Shared variable and output definitions
    aws/           # AWS-specific implementation
    azure/         # Azure-specific implementation
    gcp/           # GCP-specific implementation
  networking/
    interface/
    aws/
    azure/
    gcp/
```

The goal is **not** to abstract away every cloud difference but to provide a consistent interface where it makes sense. Focus portability on infrastructure primitives that genuinely benefit from it (compute, networking, storage) and leave cloud-specific features in cloud-specific modules.

### 1.2 Provider Abstraction with Normalized Sizes

A widely recommended pattern uses **normalized sizes** that map to provider-specific instance types:

```hcl
variable "cloud_provider" {
  type = string
  validation {
    condition     = contains(["aws", "azure", "gcp"], var.cloud_provider)
    error_message = "Must be aws, azure, or gcp."
  }
}

locals {
  instance_types = {
    aws   = { small = "t3.small",  medium = "t3.medium",  large = "t3.large"  }
    azure = { small = "Standard_B1s", medium = "Standard_B2s", large = "Standard_B4ms" }
    gcp   = { small = "e2-small",  medium = "e2-medium",  large = "e2-standard-4" }
  }
}
```

Use **wrapper modules** with `coalesce` and `try` to produce unified outputs regardless of which cloud is active.

### 1.3 State Management Across Clouds

**Separate state per cloud is mandatory.** Mixing AWS and Azure resources in the same state file creates unnecessary coupling.

| Backend | Locking | Encryption | Notes |
|---------|---------|------------|-------|
| **AWS S3 + DynamoDB** | DynamoDB table | KMS | Most common; audit via CloudTrail |
| **GCS** | Built-in | CMEK | No separate lock resource needed |
| **Azure Blob Storage** | Blob leases | Key Vault | Native locking, no extra table |
| **HCP Terraform** | Built-in | Managed | Free tier ending March 31, 2026 |

**Best practices:**
- One state file per environment per cloud (e.g., `aws/prod`, `gcp/staging`)
- Enable versioning on all state buckets for rollback capability
- Encrypt at rest with cloud-native KMS; restrict access via IAM
- Use partial backend configuration (`-backend-config`) to keep secrets out of VCS
- Avoid tight cross-state coupling via `terraform_remote_state`; prefer explicit interfaces
- Use Terragrunt for dynamic backend selection across providers

### 1.4 Secret Injection Patterns Per Cloud

**Recommended approach:** Use the External Secrets Operator (ESO) for Kubernetes workloads and cloud-native data sources for Terraform.

| Cloud | Secrets Service | Terraform Data Source | ESO Provider |
|-------|----------------|----------------------|-------------|
| **AWS** | Secrets Manager | `aws_secretsmanager_secret_version` | `aws` |
| **GCP** | Secret Manager | `google_secret_manager_secret_version` | `gcpsm` |
| **Azure** | Key Vault | `azurerm_key_vault_secret` | `azurekv` |

**ESO over CSI Driver** -- ESO is more flexible and cloud-agnostic. It runs on any Kubernetes cluster and supports many secret backends. ESO allows defining secrets, access policies, and sync behavior as Kubernetes CRDs, enabling clean GitOps separation of concerns. The CSI driver couples secrets into pod specs.

**Terraform-specific best practices:**
- Never hardcode secrets in `.tf` files or commit them to VCS
- Fetch secrets from external stores at runtime via `data` sources
- State files contain secrets in plain text -- always use encrypted remote backends
- Consider HashiCorp Vault for dynamic secrets (credentials generated on demand with automatic expiry)
- Use SOPS (`terraform-provider-sops`) for encrypted secret files in Git

### 1.5 Common Pitfalls

- **Over-abstraction:** Do not try to abstract everything. Some cloud-specific features are worth using directly.
- **Monolithic state:** Splitting state by environment and component minimizes blast radius and improves `terraform init` performance.
- **Missing locking:** Two concurrent `terraform apply` operations can corrupt state.
- **Ignoring state encryption:** State files contain resource IDs, IP addresses, and sometimes passwords.

### 1.6 References

- [OneUptime: Terraform Multi-Cloud (Jan 2026)](https://oneuptime.com/blog/post/2026-01-27-terraform-multi-cloud/view)
- [OneUptime: Creating Terraform Modules for Multi-Cloud (Feb 2026)](https://oneuptime.com/blog/post/2026-02-23-how-to-create-terraform-modules-for-multi-cloud/view)
- [Spacelift: Terraform Multi-Cloud](https://spacelift.io/blog/terraform-multi-cloud)
- [DEV Community: Best Practices for Building Multi-Cloud Modules](https://dev.to/jei/best-practices-for-building-multi-cloud-modules-in-terraform-1l56)
- [Spacelift: Managing Terraform State](https://spacelift.io/blog/terraform-state)
- [Scalr: Terraform Backend Configuration Guide 2025](https://scalr.com/learning-center/terraform-backend-configuration-guide-choosing-the-right-state-management-solution/)
- [Gruntwork: Comprehensive Guide to Managing Secrets in Terraform](https://www.gruntwork.io/blog/a-comprehensive-guide-to-managing-secrets-in-your-terraform-code)
- [External Secrets Operator (GitHub)](https://github.com/external-secrets/external-secrets)

---

## 2. ArgoCD + Valkey (Redis Replacement)

### 2.1 Why Valkey?

Redis changed its license to the Source-Available BUSL license, which violates CNCF policy. **Valkey** is a BSD-3-Clause licensed fork hosted by the Linux Foundation, designed as a drop-in replacement maintaining Redis API compatibility.

As of 2026:
- Valkey is the default on AWS ElastiCache, Google Memorystore, and Akamai
- Valkey 8.1 is the current stable release; Valkey 9.0 introduces hash field expiration, atomic slot migration, and multiple databases in cluster mode
- Valkey is compatible with Redis OSS 7.2 and all earlier open-source versions
- ~90% command-level compatibility with Redis CE 7.4+, though data files from Redis CE 7.4+ are **not** compatible

### 2.2 ArgoCD's Redis/Valkey Status

ArgoCD uses Redis as an ephemeral cache/state backend. The ArgoCD project has an open proposal to replace Redis with Valkey in its manifests (`docs/proposals/valkey.md`). The HA configuration includes six extra pods: three Redis and three HAProxy.

**Key insight:** ArgoCD's cache is ephemeral. Migration requires no data migration -- ArgoCD rebuilds its cache automatically after restart. The swap is as simple as changing the container image from `redis:7-alpine` to `valkey/valkey:8-alpine`.

### 2.3 Migration Approaches

| Method | Downtime | Complexity | Best For |
|--------|----------|-----------|----------|
| **Image swap + restart** | Seconds | Minimal | ArgoCD (ephemeral cache) |
| **Physical (RDB copy)** | Minutes | Low | Stateful workloads |
| **Replication-based** | Near-zero | Medium | Production with data |
| **Managed service** | Zero | Low | Cloud deployments |

**For ArgoCD specifically:**
1. Replace the Redis image with Valkey in Helm values or manifests
2. If using managed services (ElastiCache, Memorystore), provision a Valkey instance
3. Update ArgoCD components with `--redis-use-tls` if the managed service enforces TLS
4. Disable built-in Redis/HAProxy pods
5. Restart ArgoCD -- cache rebuilds automatically

**Real-world example (Kaltura):** Migrated to AWS ElastiCache Serverless for Valkey, eliminated Redis ops, cut Redis costs by >95%, and simplified GitOps at scale. Used logical databases to support multiple ArgoCD environments from a single backend.

### 2.4 ArgoCD Best Practices (2025-2026)

#### Repository Structure: The 3-Level Model

```
Level 1: Kubernetes manifests (Helm/Kustomize) -- self-contained, deployable without ArgoCD
Level 2: ApplicationSets wrapping manifests into ArgoCD Applications
Level 3: (Optional) App-of-Apps for bootstrapping empty clusters
```

#### ApplicationSet vs App-of-Apps

| Pattern | When to Use |
|---------|-------------|
| **ApplicationSets** | Dynamic sources (Git dirs, cluster lists, PRs); recommended default in 2025-2026 |
| **Helm App-of-Apps** | Complex conditional logic, nested loops, helper functions |
| **Multi-source Apps** | Third-party Helm charts with Git-managed values |

**Anti-pattern:** Using Helm to template Application CRDs that point to other Helm charts. This creates two layers of Helm templating and becomes impossible to reason about at scale. Use ApplicationSets instead.

#### ApplicationSet Best Practices

- Use multiple ApplicationSets by type (not one monolithic set)
- Enable Go template support with `goTemplateOptions: ["missingkey=error"]`
- Use Git generators for monorepos where each directory is a deployable app
- Escape double curly braces when deploying ApplicationSets via Helm: `{{ printf "{{cluster}}" }}`

#### Helm Integration

- Use multi-source Applications (values from Git, chart from Helm repo) for third-party charts
- Handle missing value files with `ignoreMissingValueFiles: true` for default/override patterns
- **Do not** mix `parameters`, `values`, `valuesObject`, and `valuesFiles` -- pick one approach
- Values precedence: `parameters > valuesObject > values > valueFiles > chart values.yaml`

### 2.5 Common Pitfalls

- Deeply nested Apps of Apps cause confusion and debugging difficulty
- Mixing Helm templating at two levels (Application CRDs and underlying charts)
- Not testing manifests independently of ArgoCD (apps should be installable with plain `helm` or `kustomize`)
- Client library version negotiation differences between Valkey and Redis CE 7.4+

### 2.6 References

- [ArgoCD Issue #17892: Replace Redis with Valkey](https://github.com/argoproj/argo-cd/issues/17892)
- [ArgoCD Issue #17970: Investigate OSS Alternatives to Redis](https://github.com/argoproj/argo-cd/issues/17970)
- [Kaltura: Ditch Redis in Kubernetes for Leaner GitOps](https://medium.com/kaltura-tech/still-managing-redis-in-kubernetes-for-argo-cd-we-stopped-96b9b825d987)
- [Valkey: Migration from Redis](https://valkey.io/topics/migration/)
- [Percona: Redis to Valkey Migration Guide](https://docs.percona.com/valkey/migration.html)
- [DEV Community: Redis vs Valkey in 2026](https://dev.to/synsun/redis-vs-valkey-in-2026-what-the-license-fork-actually-changed-1kni)
- [Codefresh: Structuring Argo CD Repositories](https://codefresh.io/blog/how-to-structure-your-argo-cd-repositories-using-application-sets/)
- [CNCF: App of Apps Pattern in ArgoCD (Oct 2025)](https://www.cncf.io/blog/2025/10/07/managing-kubernetes-workloads-using-the-app-of-apps-pattern-in-argocd-2/)
- [Codefresh: Helm Values in ArgoCD Multi-Source](https://codefresh.io/blog/helm-values-argocd/)

---

## 3. KEDA Autoscaling

### 3.1 Overview

KEDA (Kubernetes Event-Driven Autoscaling) is a CNCF project that extends the Kubernetes HPA to scale based on external event sources (Kafka, RabbitMQ, AWS SQS, Prometheus, HTTP, and 60+ others). It is lightweight, introduces minimal overhead, and supports scale-to-zero.

**Why KEDA for DFE pipelines:** Traditional CPU/memory-based HPA does not correlate well with Kafka consumer pressure. Consumer group lag (unprocessed message count) is a far better proxy for scaling decisions. KEDA with Kafka triggers has demonstrated a 62.15% reduction in consumer lag compared to default HPA.

### 3.2 Production Best Practices

#### Tuning Parameters

| Parameter | Guidance |
|-----------|----------|
| `pollingInterval` | 15-30s for most workloads; shorter = faster response but more API calls |
| `cooldownPeriod` | 300s+ to prevent thrashing on bursty workloads |
| `minReplicaCount` | 1 for critical services (avoids cold-start); 0 for batch/dev |
| `maxReplicaCount` | Set based on partition count and downstream capacity |
| `lagThreshold` | Per-topic: 200 for low-volume, 5000 for high-volume topics |

#### Example: Kafka ScaledObject

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: dfe-consumer
  namespace: prod
spec:
  scaleTargetRef:
    name: dfe-consumer-deployment
  pollingInterval: 30
  cooldownPeriod: 300
  minReplicaCount: 1
  maxReplicaCount: 20
  triggers:
    - type: kafka
      metadata:
        bootstrapServers: kafka-broker:9092
        consumerGroup: dfe-consumer-group
        topic: dfe-ingest
        lagThreshold: "1000"
      authenticationRef:
        name: kafka-trigger-auth
```

#### Hybrid Scaling: Lag-Based + Scheduled

Event-driven scaling alone is insufficient for production workloads with predictable patterns. Combine lag-based triggers for spikes with KEDA's cron trigger for predictable batch loads (e.g., daily bulk ingestion at 9 AM).

#### Securing Event Source Credentials

Use `TriggerAuthentication` resources instead of hardcoding secrets in ScaledObjects:

```yaml
apiVersion: keda.sh/v1alpha1
kind: TriggerAuthentication
metadata:
  name: kafka-trigger-auth
spec:
  secretTargetRef:
    - parameter: sasl
      name: kafka-secrets
      key: sasl-password
```

### 3.3 KEDA + ArgoCD Integration

When deploying KEDA ScaledObjects with ArgoCD, several considerations apply:

1. **Install KEDA as a separate ArgoCD Application** to handle CRD dependencies
2. **Configure `ignoreDifferences`** to prevent replica count conflicts:
   ```yaml
   ignoreDifferences:
     - group: apps
       kind: Deployment
       jsonPointers:
         - /spec/replicas
   ```
3. **Add custom health checks** for ScaledObjects so ArgoCD reports accurate status
4. **For scale-to-zero:** Ensure Deployment health checks account for KEDA-managed zero-replica states
5. **Use Kustomize overlays** to tune scaling parameters per environment

### 3.4 KEDA + OpenTelemetry Custom Metrics

Three approaches for using OTel metrics with KEDA:

| Approach | Pros | Cons |
|----------|------|------|
| **OTel -> Prometheus -> KEDA Prometheus Scaler** | Mature, well-documented | Requires Prometheus; scrape-interval latency |
| **Kedify OTEL Scaler** | Push-based, no Prometheus needed, near-instant | Add-on, newer |
| **Sawmills KEDA Scaler Exporter** | OTel Collector becomes KEDA metrics provider | Newer, less battle-tested |

KEDA 2.12+ can also emit its own internal metrics via OpenTelemetry (experimental), providing visibility into scaler activity, errors, and control loop timing.

### 3.5 Common Pitfalls

- **Conflicting ScaledObjects:** KEDA admission webhooks prevent multiple ScaledObjects targeting the same Deployment, but misconfigurations can slip through
- **Overreacting to noise:** Bursty events without proper cooldown cause rapid scale-up/down thrashing
- **Ignoring downstream capacity:** Scaling consumers up without considering database/API rate limits
- **Not monitoring KEDA itself:** Use KEDA's Prometheus/OTel metrics to monitor scaler health

### 3.6 References

- [KEDA Official Site](https://keda.sh/)
- [Kedify: KEDA + Kafka Performance (62% Improvement)](https://kedify.io/resources/blog/keda-kafka-improve-performance-by-62-15-at-peak-loads/)
- [Medium: Auto Scaling Kafka Consumers with KEDA](https://medium.com/google-cloud/auto-scaling-kafka-consumers-with-kubernetes-and-keda-eb6b6ddb4f34)
- [Medium: Hybrid Approach to Scaling Kafka Consumers with KEDA](https://medium.com/@rohit.shinkar/scaling-kafka-consumers-on-kubernetes-with-keda-a-hybrid-approach-b74e830993e1)
- [OneUptime: Deploy KEDA ScaledObjects with ArgoCD (Feb 2026)](https://oneuptime.com/blog/post/2026-02-26-deploy-keda-scaledobjects-argocd/view)
- [KEDA Docs: OpenTelemetry Integration (Experimental)](https://keda.sh/docs/2.18/integrations/opentelemetry/)
- [Kedify: Using OTel Collector with KEDA](https://kedify.io/resources/blog/using-otel-collector-with-keda/)
- [Dash0: Observable Event-Driven Autoscaling with KEDA and OTel](https://www.dash0.com/blog/observable-event-driven-autoscaling-with-keda-opentelemetry-and-dash0)

---

## 4. OpenTelemetry -> ClickHouse Pipeline

### 4.1 Architecture Overview

The production OTel-to-ClickHouse pipeline consists of three layers:

```
Applications (SDK/Auto-instrumentation)
    |
    v
OpenTelemetry Collector (Contrib Distro)
  - Receivers: OTLP gRPC (4317), OTLP HTTP (4318)
  - Processors: Batch, Memory Limiter, Resource Attributes
  - Exporters: ClickHouse Exporter
    |
    v
ClickHouse (Columnar Storage)
  - Logs, Metrics, Traces tables
  - Materialized Views for aggregation
  - TTL-based data lifecycle
    |
    v
HyperDX / Grafana (Visualization)
```

### 4.2 Why ClickHouse for Observability?

- **Columnar storage:** 10-100x compression vs row-based databases
- **Vectorized query execution:** Sub-second queries on billions of rows
- **Native time-series support:** Specialized functions for temporal analysis
- **Unified backend:** Logs, metrics, and traces in one database (no Prometheus + Loki + Tempo)
- **Cost:** Significantly lower storage costs than commercial observability platforms

### 4.3 Collector Configuration for Production

**Critical:** Use the OpenTelemetry Collector Contrib Distro (not the core distro) for the ClickHouse exporter and filelog receiver.

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    send_batch_size: 10000
    send_batch_max_size: 50000
    timeout: 5s
  memory_limiter:
    check_interval: 1s
    limit_mib: 4096
    spike_limit_mib: 800
  resource:
    attributes:
      - key: deployment.environment
        value: production
        action: upsert

exporters:
  clickhouse:
    endpoint: tcp://clickhouse:9000
    database: otel
    create_schema: false  # Manage schemas manually in production
    logs_table_name: otel_logs
    traces_table_name: otel_traces
    metrics_table_name: otel_metrics
    ttl: 720h  # 30 days
    compress: lz4
    timeout: 10s
    sending_queue:
      queue_size: 5000
      num_consumers: 10
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 300s

service:
  pipelines:
    logs:
      receivers: [otlp]
      processors: [memory_limiter, batch, resource]
      exporters: [clickhouse]
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch, resource]
      exporters: [clickhouse]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch, resource]
      exporters: [clickhouse]
```

**Tuning guidelines:**
- Always use batch processing (ClickHouse recommends inserts of >=1000 rows, no more than 1 insert/second)
- A gateway instance with 3 cores and 12GB RAM handles ~60k events/second
- For high-volume: batch sizes up to 500,000, send_batch_max_size of 1,000,000
- Use LZ4 compression on the ClickHouse connection
- Deploy multiple collectors behind a load balancer for high-volume; use the trace ID/service name load-balancing exporter

### 4.4 Schema Management

**In production, manage your own schema** by setting `create_schema: false`. This prevents exporter processes from racing to create tables and makes upgrades cleaner.

Key schema considerations:
- Use MergeTree engine with time-based partitioning
- Order by service name and timestamp for fast lookups
- ClickHouse v25+ recommended for reliable JSON column support (replaces Map columns)
- Customize indexes, TTL, and partitioning for your deployment
- Use materialized views for metric aggregation and downsampling

### 4.5 HyperDX / ClickStack

**ClickHouse acquired HyperDX in March 2025.** The combined stack is called **ClickStack**:

- **Ingestion:** OpenTelemetry Collector (vendor-neutral)
- **Storage:** ClickHouse (unified columnar database for all signal types)
- **Visualization:** HyperDX (purpose-built UI with Lucene-style queries, session replay)

**Deployment options:**
- Docker: Single command for local/dev (requires 4GB RAM, 2 cores minimum)
- ClickHouse Cloud: Managed ClickStack (private preview as of early 2026)
- Self-hosted: Full control with your own ClickHouse cluster

HyperDX provides unified search across logs, traces, metrics, errors, and session replays. It uses Lucene-style query syntax and supports natural language search, abstracting SQL complexity.

### 4.6 Auto-Instrumentation

#### Python (Mature, Zero-Code)

```bash
pip install opentelemetry-distro opentelemetry-exporter-otlp
opentelemetry-bootstrap -a install  # Auto-detects installed libraries
opentelemetry-instrument \
  --traces_exporter otlp \
  --metrics_exporter otlp \
  --logs_exporter otlp \
  --service_name dfe-processor \
  python app.py
```

Works with Flask, Django, FastAPI, requests, SQLAlchemy, and many more. Handles async correctly (FastAPI, asyncio).

#### Rust (Manual Instrumentation Required)

Rust does not have auto-instrumentation. Use the official OTel SDK:

- `opentelemetry` -- API crate (Context, Baggage, Propagators, Logging Bridge, Metrics, Tracing)
- `opentelemetry-sdk` -- SDK implementation
- `opentelemetry-otlp` -- OTLP exporter

Advantages: zero-cost abstractions (disabled spans compile to no-ops), async-native (tokio/async-std), unified SDK for all three signals, vendor-neutral export.

For the Logs Bridge API, OTel Rust integrates with existing `log` and `tracing` crates rather than introducing a new logging API.

**Experimental:** eBPF-based auto-instrumentation for Rust (uprobes, no code changes, works with Rust 1.70+ binaries including stripped binaries).

#### Kubernetes Operator (Recommended)

Use the OpenTelemetry Operator for auto-injection in Kubernetes. It provides lifecycle management, auto-updates, and collector configuration, and can inject OTel libraries into Go, Java, Node.js, Python, .NET, and Apache HTTP Server applications without manual code changes.

### 4.7 Production Deployment Patterns

- **Dual-write for migration:** Send telemetry to both legacy backend and ClickHouse during transition
- **Gateway pattern:** Multiple collectors behind a load balancer with trace ID-based routing
- **Two-tier storage:** Operational (hot, local SSD) + archival (cold, S3) with TTL-based rollover
- **Introduce cold tier** once data volumes exceed ~100 GB/day
- **Use the OpenTelemetry Operator** for Kubernetes lifecycle management

### 4.8 Common Pitfalls

- Using the core Collector distro instead of Contrib (missing ClickHouse exporter)
- Not using batch processing (kills ClickHouse insert performance)
- Letting the exporter auto-create schemas in multi-replica deployments (race conditions)
- High-cardinality metric labels (user IDs, request IDs) causing storage explosion
- Not flushing telemetry on process exit (losing last events before crash/restart)

### 4.9 References

- [ClickHouse Docs: Integrating OpenTelemetry](https://clickhouse.com/docs/observability/integrating-opentelemetry)
- [ClickHouse Exporter (GitHub)](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/clickhouseexporter/README.md)
- [ClickHouse Blog: Storing Traces and Spans](https://clickhouse.com/blog/storing-traces-and-spans-open-telemetry-in-clickhouse)
- [ClickHouse: Bindplane + ClickStack Migration](https://clickhouse.com/blog/bindplane-faster-otel-migrations-to-clickstack)
- [ClickHouse acquires HyperDX (March 2025)](https://clickhouse.com/blog/clickhouse-acquires-hyperdx-the-future-of-open-source-observability)
- [HyperDX (GitHub)](https://github.com/hyperdxio/hyperdx)
- [SigNoz: OpenTelemetry in Rust](https://signoz.io/blog/opentelemetry-rust/)
- [Red Hat: Auto-Instrumentation with OpenTelemetry (Feb 2026)](https://developers.redhat.com/articles/2026/02/25/how-use-auto-instrumentation-opentelemetry)
- [OpenTelemetry Rust SDK (GitHub)](https://github.com/open-telemetry/opentelemetry-rust)

---

## 5. FerretDB over PostgreSQL

### 5.1 What Is FerretDB?

FerretDB is an open-source (Apache 2.0) alternative to MongoDB. It is a proxy that converts MongoDB 5.0+ wire protocol queries to SQL, using PostgreSQL with the **DocumentDB extension** as its database engine. Created after MongoDB moved to the SSPL license.

### 5.2 Architecture

```
Application (MongoDB driver/client)
    |  MongoDB Wire Protocol (5.0+)
    v
FerretDB Proxy
    |  SQL (via DocumentDB extension)
    v
PostgreSQL 17 + DocumentDB Extension
    |  Standard PostgreSQL storage
    v
Disk / Cloud Storage
```

FerretDB 2.x uses Microsoft's open-source DocumentDB PostgreSQL extension, which introduces the BSON data type and operations natively to PostgreSQL. Azure Cosmos DB for MongoDB (vCore) uses the same extension, enabling seamless workload portability between FerretDB and Cosmos DB.

### 5.3 Production Readiness Assessment

**FerretDB 2.0 GA was released in March 2025** -- declared production-ready by the project.

| Aspect | Status |
|--------|--------|
| **License** | Apache 2.0 (fully open source) |
| **Backend** | PostgreSQL 17 + DocumentDB 0.107.0 confirmed |
| **MongoDB wire protocol** | 5.0+ compatible |
| **Management tools** | MongoDB Compass, Studio 3T, MingoUI work transparently |
| **Cloud offering** | FerretDB Cloud launched August 2025 (AWS only initially) |
| **Enterprise support** | Subscriptions available (dedicated support, performance tuning, migration assistance) |
| **Vector search** | Available in both self-hosted and cloud (vs. MongoDB Atlas-only) |

### 5.4 MongoDB Compatibility & Limitations

**What works:**
- Core CRUD operations
- Aggregation pipeline (commonly used stages)
- Indexing
- MongoDB wire protocol clients and drivers
- Management tools (Compass, Studio 3T, etc.)
- Vector search

**What to be aware of:**
- Not every MongoDB feature is implemented; the focus is on the core feature set
- Performance characteristics differ from native MongoDB due to the relational backend
- No published MongoDB compatibility test results (unlike AWS DocumentDB and Azure Cosmos DB)
- Some advanced MongoDB features may not be available
- **Must test your specific workload** before deciding to migrate

### 5.5 Comparison with Native MongoDB

| Factor | FerretDB + PostgreSQL | Native MongoDB |
|--------|----------------------|----------------|
| **License** | Apache 2.0 | SSPL |
| **Storage engine** | PostgreSQL (relational) | WiredTiger (document-native) |
| **Deployment** | Anywhere (on-prem, any cloud) | Atlas (managed) or self-hosted |
| **Performance** | Workload-dependent; test required | Optimized for document operations |
| **Ecosystem** | PostgreSQL tooling + MongoDB clients | MongoDB-native tooling |
| **Vendor lock-in** | None | Atlas lock-in risk |
| **Feature completeness** | Core feature set | Full feature set |
| **Operational overhead** | Standard PostgreSQL ops | MongoDB-specific ops |

### 5.6 Recommended Approach for DFE

FerretDB makes sense when:
- You want MongoDB API compatibility without SSPL licensing concerns
- You already operate PostgreSQL and want to consolidate
- You need deployment flexibility (any cloud, on-prem, hybrid)
- Your MongoDB usage is limited to core CRUD + basic aggregation

FerretDB is risky when:
- You rely on advanced MongoDB features (change streams, sharding, etc.)
- You need maximum document-store performance at high scale
- You have complex aggregation pipelines using MongoDB-specific stages

### 5.7 References

- [FerretDB 2.0 GA Announcement](https://blog.ferretdb.io/ferretdb-v2-ga-open-source-mongodb-alternative-ready-for-production/)
- [The New Stack: FerretDB 2.0](https://thenewstack.io/ferretdb-2-0-open-source-mongodb-alternative-with-postgresql-power/)
- [FerretDB Documentation](https://docs.ferretdb.io/)
- [FerretDB (GitHub)](https://github.com/FerretDB/FerretDB)
- [InfoQ: FerretDB Cloud (September 2025)](https://www.infoq.com/news/2025/09/ferretdb-cloud-mongodb/)
- [Instaclustr: FerretDB with PostgreSQL](https://www.instaclustr.com/blog/ferretdb-with-postgresql/)

---

## 6. OIDC/OAuth2 for Platform Auth

### 6.1 Architectural Options

| Approach | Description | Best For |
|----------|-------------|----------|
| **Keycloak** | Full-featured IdP with OIDC, SAML, LDAP, realm federation | Enterprise SSO, complex requirements |
| **Dex** | Lightweight OIDC provider, Kubernetes-native | ArgoCD integration, simple setups |
| **Authentik** | Self-hosted IdP with OIDC/OAuth2/SAML/LDAP | Local-first with cloud federation |
| **OpenUnison** | Multi-cluster SSO with control plane model | Multi-cluster Kubernetes |
| **Rancher Prime Unified SSO** | Centralized SSO/RBAC across all managed clusters | Rancher-managed environments |

### 6.2 Single Source of Truth Pattern

The recommended architecture uses a **centralized Identity Provider** (Keycloak, Authentik, or cloud IdP) with federation to downstream services:

```
Central IdP (Keycloak/Authentik)
    |
    +-- ArgoCD (direct OIDC or via Dex)
    +-- Kubernetes API (OIDC token auth)
    +-- Grafana/HyperDX (OIDC)
    +-- Harbor (OIDC)
    +-- Custom services (OAuth2 Proxy)
```

OIDC solves traditional auth problems: SSO (log in once, access all services), centralized identity management (one source of truth), standardized authentication (all apps use the same protocol), and group-based RBAC.

### 6.3 Local Fallback + External OIDC

For edge/hybrid deployments where external IdPs may be unreachable:

1. **Primary:** External OIDC provider (cloud IdP, corporate AD)
2. **Fallback:** Self-hosted Keycloak/Authentik/Dex running within the cluster
3. **Federation:** Local IdP federates to central IdP when connectivity is available

**Rancher Prime pattern:** Rancher serves as the single source of truth, logging every user action and permission change across every managed cluster. Security policies are applied uniformly across hybrid and multi-cloud environments, automatically eliminating configuration drift. Users authenticate through existing systems (AD, LDAP, SAML, OAuth) without exposing the identity provider.

### 6.4 ArgoCD OIDC Integration

ArgoCD supports two SSO methods:

#### Direct OIDC (Keycloak Example)

```yaml
# argocd-cm ConfigMap
data:
  oidc.config: |
    name: Keycloak
    issuer: https://keycloak.example.com/realms/platform
    clientID: argocd
    clientSecret: $oidc.keycloak.clientSecret
    requestedScopes: ["openid", "profile", "email", "groups"]
```

#### Via Bundled Dex

```yaml
# argocd-cm ConfigMap
data:
  dex.config: |
    connectors:
      - type: oidc
        id: keycloak
        name: Keycloak
        config:
          issuer: https://keycloak.example.com/realms/platform
          clientID: argocd
          clientSecret: $dex.keycloak.clientSecret
          insecureEnableGroups: true
```

Use Dex when your IdP doesn't support OIDC natively (SAML, LDAP) or when you need Dex-specific features like UserInfo endpoint fetching and federated tokens.

#### Group-Based RBAC

```yaml
# argocd-rbac-cm ConfigMap
data:
  policy.csv: |
    p, role:admin, applications, *, */*, allow
    p, role:readonly, applications, get, */*, allow
    g, platform-admins, role:admin
    g, developers, role:readonly
```

### 6.5 Multi-Service SSO in Kubernetes

For services that don't natively support OIDC, use **OAuth2 Proxy** as a sidecar or reverse proxy:

```
User -> OAuth2 Proxy -> Application
          |
          v
        IdP (OIDC)
```

This pattern works for any web application. OAuth2 Proxy handles authentication and passes identity information (email, groups) as HTTP headers to the upstream application.

### 6.6 Common Pitfalls

- **Self-signed certificates:** ArgoCD doesn't natively trust non-standard CAs for OIDC; configure the CA in `argocd-cm` (supported since v2.5)
- **Group claim configuration:** Ensure the IdP includes group claims in the OIDC token; configure `requestedScopes` to include `groups`
- **Token expiry:** Configure reasonable token lifetimes; too short causes frequent re-authentication
- **CLI authentication:** If using `argocd` CLI with Keycloak, you must use the PKCE flow (not client authentication flow)

### 6.7 References

- [ArgoCD Docs: User Management](https://argo-cd.readthedocs.io/en/stable/operator-manual/user-management/)
- [ArgoCD Docs: Keycloak Integration](https://argo-cd.readthedocs.io/en/stable/operator-manual/user-management/keycloak/)
- [OneUptime: SSO with OIDC in ArgoCD (Jan 2026)](https://oneuptime.com/blog/post/2026-01-25-sso-oidc-argocd/view)
- [Pi Cluster: SSO with Keycloak and OAuth2-Proxy](https://picluster.ricsanfre.com/docs/sso/)
- [Codefresh: Argo CD with SSO Practical Guide](https://codefresh.io/learn/argo-cd/argo-cd-with-sso-practical-guide-enabling-signup-with-github/)
- [SUSE: Unified SSO/RBAC for Rancher Prime (KubeCon NA 2025)](https://www.suse.com/c/kubecon-na-2025-sso-rbac/)
- [OpenUnison: Multi-Cluster SSO](https://openunison.github.io/multi_cluster_sso/)
- [Keycloak OIDC Production Guide (Medium)](https://medium.com/@asheshthapa/implementing-oidc-authentication-with-keycloak-a-production-guide-f7c16c375b15)

---

## 7. Multi-Cloud Kubernetes Deployment

### 7.1 Rancher for Local/Edge Deployments

**SUSE Rancher Prime** is an open-source Kubernetes management platform that provides:

- **Unified cluster provisioning** across EKS, AKS, GKE, K3s, and RKE2 from a single UI/API
- **Centralized RBAC** using existing LDAP/SAML -- no need for per-cloud IAM configuration
- **Import existing clusters** (EKS, AKS, GKE) into Rancher for unified management
- **Full flexibility** for CNI, CSI, OS, and networking choices (unlike managed services)
- **Cost:** Open source and free; only pay for infrastructure. Enterprise support via SUSE Rancher Prime.

**Rancher for DFE:** Create on-prem RKE2/K3s clusters and cloud-managed EKS/GKE clusters using the same workflow. Developers don't need to learn three different cloud CLIs.

### 7.2 EKS vs GKE vs AKS Comparison (2025-2026)

| Feature | EKS | AKS | GKE |
|---------|-----|-----|-----|
| **Control plane cost** | $72/month | Free (basic) / $72 with SLA | Free (1 zonal) / $72 regional |
| **Operational complexity** | Highest | Moderate | Lowest (Autopilot) |
| **K8s version adoption** | 4-8 weeks | 3-6 weeks | 0-2 weeks |
| **Autoscaling** | Karpenter, Fargate | Scale-to-zero nodes | Autopilot (pay-per-pod) |
| **Security** | IAM + IRSA + GuardDuty | Entra ID + Defender + Policy | Workload Identity + Binary Auth + gVisor |
| **AI/ML** | Inferentia/Trainium | Confidential containers | TPU v5 (18-22% cheaper training) |
| **Hybrid** | EKS Anywhere | AKS Arc | Anthos |
| **Best for** | Deep AWS integration | Microsoft enterprises | K8s purity, AI, minimal ops |

### 7.3 Portable Ingress: The NGINX Retirement

**Critical change:** Ingress-NGINX Controller is being retired in March 2026. No further releases, bug fixes, or security updates.

**Traefik has emerged as the leading replacement** -- adopted by IBM, SUSE, Nutanix, OVHcloud as their default. Key advantages:
- Drop-in NGINX Ingress Provider reads existing annotations (>90% coverage)
- Security by design: Go (no memory safety vulnerabilities), structured parsing (no template injection)
- Dynamic service discovery (no static config reloads)
- Gateway API native support

**Two migration paths:**
1. **Drop-in replacement:** Run Traefik with NGINX Ingress Provider -- existing annotations work without rewriting
2. **Modernize with Gateway API:** Define GatewayClass, Gateway, and HTTPRoute resources for a future-proof architecture

**Alternative controllers:** NGINX Gateway Fabric (NGF), Kong Kubernetes Gateway, Contour (Envoy-based).

SUSE confirmed Traefik will become the default in RKE2 starting with v1.36.

### 7.4 Cloud-Agnostic Storage

| Solution | Protocols | Scale | Complexity | Best For |
|----------|-----------|-------|-----------|----------|
| **Rook-Ceph** | Block, File, Object | Large/enterprise | High | Bare metal, multi-protocol |
| **Longhorn** | Block only | Small-to-mid | Low | Edge, K3s, simple ops |
| **OpenEBS** | Block | Small-to-mid | Low-Medium | Cloud-native, flexible engines |

**Longhorn for DFE edge deployments:** Easy UI, built-in S3 backups, snapshots, lightweight. Best for K3s/RKE2 edge clusters.

**Rook-Ceph for DFE cloud deployments:** Enterprise-grade with Block (RBD for databases), CephFS (shared workloads), and S3-compatible object (RGW).

### 7.5 Cloud-Agnostic Backup with Velero

Velero provides cloud-agnostic Kubernetes backup and disaster recovery:
- Backs up all K8s resources and volume data
- Scheduled automated backups
- CSI plugin for volume snapshots (works with both Longhorn and Rook-Ceph)
- S3-compatible storage backend for portability
- Cross-cluster migration and StorageClass conversion

**Gotcha with Rook-Ceph:** Velero + Ceph RBD CSI may silently skip PVCs without proper `VolumeSnapshotClass` configuration with Velero labels.

### 7.6 Cloud-Native Service Alternatives

| Concern | Cloud-Specific | Portable Alternative |
|---------|---------------|---------------------|
| **Secrets** | AWS Secrets Manager, GCP Secret Manager, Azure Key Vault | External Secrets Operator (ESO) |
| **Load Balancer** | AWS ALB, GCP GCLB, Azure AG | Traefik / MetalLB (bare metal) |
| **Storage** | EBS, Persistent Disk, Azure Disk | Longhorn / Rook-Ceph |
| **DNS** | Route53, Cloud DNS, Azure DNS | ExternalDNS (cloud-agnostic) |
| **Certificates** | ACM, GCP Managed SSL, Azure Managed | cert-manager |

### 7.7 References

- [Darumatic: Managing AKS, EKS, GKE with Rancher](https://darumatic.com/blog/rancher-vs-cloud-providers)
- [Northflank: Choosing the Right Enterprise Kubernetes Platform (2026)](https://northflank.com/blog/choosing-the-right-enterprise-kubernetes-platform)
- [Sedai: Kubernetes Pricing 2026 -- EKS vs AKS vs GKE](https://sedai.io/blog/kubernetes-cost-eks-vs-aks-vs-gke)
- [Atmosly: EKS vs GKE vs AKS (2026)](https://atmosly.com/blog/eks-vs-gke-vs-aks-which-managed-kubernetes-is-best-2025)
- [Traefik Labs: Ingress NGINX Replacement](https://traefik.io/blog/migrate-from-ingress-nginx-to-traefik-now)
- [Cloud Native Now: Traefik Emerges as Kubernetes Networking Standard (KubeCon EU 2026)](https://cloudnativenow.com/kubecon-cloudnativecon-europe-2026/traefik-proxy-emerges-as-kubernetes-networking-standard-as-ibm-nutanix-suse-and-ovhcloud-migrate-from-ingress-nginx/)
- [Simplyblock: Top Kubernetes Storage Solutions in 2026](https://simplyblock.io/blog/5-storage-solutions-for-kubernetes-in-2025/)
- [Kubedo: Kubernetes Storage Comparison](https://kubedo.com/kubernetes-storage-comparison/)
- [Platform Cloudogu: Cluster Backups with Velero and Longhorn](https://platform.cloudogu.com/en/blog/velero-longhorn-backup-restore/)

---

## 8. DFE/ETL Pipeline Best Practices

### 8.1 Pipeline Architecture at Scale

The modern DFE/ETL pipeline architecture follows a **streaming-first** pattern with optional batch fallback:

```
Data Sources (Sensors, APIs, Logs, etc.)
    |
    v
Ingestion Layer (Kafka / managed streaming)
    |
    +-- Stream Processing (Flink / Kafka Streams / ClickHouse MVs)
    |       |
    |       v
    |   Serving Layer (ClickHouse for analytics, PostgreSQL for operational)
    |
    +-- Batch Processing (for backfills, reprocessing)
            |
            v
        Cold Storage (S3 / GCS with Parquet / Iceberg)
```

**2026 trend -- Shift Left Architecture:** The streaming layer is becoming the first place where data is enriched, transformed, and analyzed, rather than waiting for batch ETL. Enrichment happens upstream before insertion into ClickHouse, keeping queries fast and tables simpler.

### 8.2 Kafka in Multi-Cloud Deployments

#### Key Developments (2025-2026)

- **Apache Kafka 4.0 (March 2025):** ZooKeeper support completely removed. All 4.x versions use **KRaft** (Kafka Raft) exclusively -- reduces operational complexity, eliminates ZooKeeper dependency, improves startup time by up to 10x.
- **Schema Registry:** Used by 80%+ of organizations running Kafka at scale (Confluent 2025 survey). Prevents the most common production incident: schema changes in producers breaking downstream consumers.
- **Partitioning:** Each partition handles ~10 MB/s writes on modern hardware. Replication factor of 3 ensures data survives loss of 2 brokers.

#### Multi-Cloud Kafka Options

| Option | Managed By | Multi-Cloud | Notes |
|--------|-----------|-------------|-------|
| **Confluent Cloud** | Confluent | AWS, GCP, Azure | Full platform with Schema Registry, Connect |
| **Amazon MSK** | AWS | AWS only | Deeply integrated with AWS services |
| **Aiven for Kafka** | Aiven | AWS, GCP, Azure | Multi-cloud managed, unified billing |
| **Self-hosted (Strimzi)** | You | Any K8s | Maximum control, operational overhead |

### 8.3 Kafka-to-ClickHouse Integration

| Method | Managed | Semantics | Best For |
|--------|---------|-----------|----------|
| **ClickPipes** | Fully managed | Exactly-once | ClickHouse Cloud users |
| **Kafka Connect Sink** | Self-managed | Exactly-once | Existing Kafka Connect infrastructure |
| **Kafka Table Engine** | Built-in | At-least-once | Simple setups, lowest infra cost |
| **Vector** | Self-managed | At-least-once | Vendor-agnostic pipelines |

**Best practice:** Perform enrichment upstream (in Kafka Streams, Flink, or the Collector) before inserting into ClickHouse. ClickHouse JOINs consume substantial memory and materialized views don't auto-update when joined tables change.

### 8.4 ClickHouse Operational Best Practices

#### Schema Design

- **Plan schemas carefully upfront** -- partitioning and sorting decisions become part of on-disk storage format
- **Use additive/incremental evolution** -- add columns, don't rewrite tables
- **Partition-based backfills** for schema migrations (drop/re-ingest one month at a time)
- **Ingestion-projection model** -- ingestion tables accept new fields without modification; projections handle transformation
- **Treat schema as code** with automated drift detection in CI/CD

#### Data Lifecycle

- **Tiered storage:** Hot data on local SSD, cold data on S3 (queryable, not just archive)
- **TTL-based rollover:** Automatic data lifecycle management
- **Materialized views** for downsampling high-cardinality metrics
- **Two-tier model** (operational + archival) for teams starting out; add cold tier at >100 GB/day

#### Performance

- Always insert in batches (>=1000 rows, <=1 insert/second)
- Use LZ4 compression
- Order tables by frequently filtered columns (service_name, timestamp)
- Avoid high-cardinality columns in sort keys
- Use `FINAL` queries sparingly -- they force merge-on-read

### 8.5 Data Archival Patterns

```
Hot Tier (Local SSD / RAM Cache)
  - Recent data, sub-millisecond access
  - TTL: 7-30 days
    |
    v
Warm Tier (S3 / GCS with local cache)
  - Historical data, queryable at S3 prices
  - TTL: 30-365 days
    |
    v
Cold Tier (S3 Deep Archive / Parquet + Iceberg)
  - Long-term retention, compliance
  - Minimal query access
  - TTL: 1-7 years
```

**ClickHouse natively supports tiered storage** -- treats S3 as queryable disk, not just backup. Local cache on compute nodes masks S3 latency.

For partition-based archival: detach partitions, export to Parquet on S3, maintain metadata for queryability.

### 8.6 Schema Management and Evolution

**The key challenge:** "What looks like a simple column addition in a row-oriented database can trigger a full table rewrite in ClickHouse."

**Recommended tools and patterns:**
- Version-controlled SQL migrations (treat schema as code)
- Automated drift detection in CI pipelines
- Additive changes only (add columns, don't modify existing ones)
- Partition-based backfills for breaking changes
- Short-lived ingest tables with materialized view transformations

### 8.7 References

- [ClickHouse Docs: Integrating Kafka](https://clickhouse.com/docs/integrations/kafka)
- [ClickHouse Blog: 2025 Roundup](https://clickhouse.com/blog/clickhouse-2025-roundup)
- [Kai Waehner: Data Streaming Trends 2026](https://www.kai-waehner.de/blog/2025/12/10/top-trends-for-data-streaming-with-apache-kafka-and-flink-in-2026/)
- [ClickHouse Blog: How Braze Rebuilt Real-Time Analytics](https://clickhouse.com/blog/how-braze-rebuilt-real-time-analytics-pipeline-with-clickHouse-cloud)
- [Tinybird: ClickHouse Schema Migrations 2026](https://www.tinybird.co/blog/clickhouse-schema-migrations)
- [Axis Engineering: Schema Changes in ClickHouse](https://engineeringat.axis.com/schema-changes-clickhouse/)
- [DEV Community: Type-Safe Schema Management in ClickHouse](https://dev.to/lureilly1/type-safe-schema-management-evolution-in-clickhouse-keeping-analytics-in-sync-cmd)
- [Instaclustr: ClickHouse Best Practices Part 2](https://www.instaclustr.com/blog/clickhouse-best-practices-part-2-scaling-data-management-and-optimization/)

---

## 9. OpenVPN for Edge/Hybrid Connectivity

### 9.1 OpenVPN vs WireGuard Comparison

| Factor | OpenVPN | WireGuard |
|--------|---------|-----------|
| **Codebase** | ~70,000 lines (C) | ~4,000 lines (C, in-kernel) |
| **Speed** | Moderate (292 Mbps typical) | Fast (812 Mbps typical, 2-4x faster) |
| **Latency** | 113ms (TCP) | 40ms (UDP) |
| **Overhead** | 20-40% (TCP) | 4-6% (UDP) |
| **Protocol** | TCP or UDP | UDP only |
| **Firewall traversal** | Excellent (TCP 443 = indistinguishable from HTTPS) | Limited (UDP blocked by some firewalls) |
| **Cryptography** | AES-256, configurable | ChaCha20, Curve25519, Poly1305 (fixed) |
| **Configuration** | Complex, highly configurable | Simple, 10-15 min setup |
| **PKI** | Full certificate infrastructure | Simple key pairs |
| **Audit maturity** | 20+ years of audits | Newer, but lean codebase is easily audited |
| **Post-quantum** | Not by default | Not by default (can add ML-KEM) |

### 9.2 Recommendation for Edge Stream Hubs

**Use WireGuard for the data plane** (high-throughput sensor data, telemetry streams) and **keep OpenVPN as a fallback** for restrictive networks that block UDP.

WireGuard's kernel-level performance makes it ideal for edge-to-cloud data pipelines. The 2-4x throughput advantage and near-zero jitter are significant for streaming workloads. However, OpenVPN over TCP 443 is essential for environments with restrictive firewalls.

### 9.3 VPN Mesh Patterns for Hybrid Cloud

#### Hub-and-Spoke (Traditional)

```
Edge Site A --\
Edge Site B ----> Central VPN Gateway --> Cloud VPC
Edge Site C --/
```

Simple to manage but creates a bottleneck at the hub. All traffic routes through the central gateway.

#### Full Mesh (WireGuard-based)

```
Edge Site A <---> Edge Site B
     ^                ^
     |                |
     v                v
Cloud VPC  <---> Edge Site C
```

Direct peer-to-peer communication between all sites. WireGuard makes this feasible due to low resource usage per tunnel. **Scales poorly** with manual configuration beyond dozens of nodes -- requires orchestration tooling.

#### Mesh Orchestration Platforms

| Tool | Protocol | Key Features | Best For |
|------|----------|-------------|----------|
| **Tailscale** | WireGuard | Zero-config, SSO/MFA, NAT traversal | Teams, SaaS-managed |
| **Netmaker** | WireGuard | Centralized dashboard, edge device management | Self-hosted enterprise |
| **NetBird** | WireGuard | SSO/MFA, device posture checks, peer-to-peer | DevOps teams |
| **Pritunl** | OpenVPN + WireGuard | Hub-and-spoke + mesh, web admin UI | Hybrid protocol needs |
| **Headscale** | WireGuard (Tailscale-compatible) | Self-hosted Tailscale control plane | Privacy-conscious orgs |

### 9.4 AWS Client VPN vs Self-Hosted

| Factor | AWS Client VPN | Self-Hosted WireGuard | Self-Hosted OpenVPN |
|--------|---------------|----------------------|---------------------|
| **Setup** | Managed, easy | 10-15 min | 30-60 min |
| **Bandwidth** | 50 Mbps/user baseline | Line speed (kernel-level) | Moderate (userspace) |
| **Cost** | Per-connection + hourly | EC2 instance (~$3-5/mo) | EC2 instance (~$3-5/mo) |
| **IPv6** | No | Yes | Yes |
| **Customization** | Limited | Moderate | High |

**AWS Client VPN limitations:** Maximum 50 Mbps baseline per user, no IPv6, complex authorization rules, TLS-over-OpenVPN bottleneck. For most organizations, self-hosted WireGuard or a mesh platform provides better performance at lower cost.

### 9.5 Network Security for Data Pipelines

**Zero Trust principles (2025-2026 trend):**
- Authenticate every connection, not just at the network perimeter
- Encrypt all data in transit (WireGuard provides this by default)
- Use identity-based access controls (Tailscale, NetBird integrate with SSO/OIDC)
- Implement device posture checks before granting access
- Segment networks by workload sensitivity

**For DFE edge deployments:**
- WireGuard mesh between edge stream hubs and cloud clusters
- mTLS for service-to-service communication within clusters
- Kafka TLS + SASL for cross-site streaming
- Network policies in Kubernetes for pod-level segmentation

### 9.6 Recommended Architecture for DFE

```
Edge Stream Hub (Rancher/K3s)
  - WireGuard tunnel to cloud VPC
  - Local Kafka for buffering
  - OTel Collector for telemetry
  - OpenVPN fallback for restricted networks
      |
      | WireGuard (primary) / OpenVPN TCP 443 (fallback)
      |
      v
Cloud VPC (EKS/GKE/AKS)
  - Kafka cluster (central)
  - ClickHouse (analytics)
  - ArgoCD (GitOps)
  - Keycloak (auth)
```

Use a mesh orchestrator (Netmaker or Headscale) for multi-site coordination with automated key exchange and NAT traversal.

### 9.7 Common Pitfalls

- **Relying solely on WireGuard:** UDP-only means it fails in restrictive corporate networks -- always have a TCP fallback
- **Manual mesh configuration:** Does not scale beyond dozens of nodes; use an orchestration platform
- **Ignoring MTU:** WireGuard and OpenVPN both reduce effective MTU; misconfigured MTU causes fragmentation and throughput degradation
- **Not monitoring tunnel health:** VPN tunnels fail silently; implement health checks and alerting
- **Overloading a single gateway:** Hub-and-spoke creates a bottleneck for high-throughput data pipelines

### 9.8 References

- [ExpressVPN: WireGuard vs OpenVPN (2026)](https://www.expressvpn.com/blog/wireguard-vs-openvpn/)
- [CyberInsider: WireGuard vs OpenVPN -- 7 Key Differences (2026)](https://cyberinsider.com/vpn/wireguard/wireguard-vs-openvpn/)
- [Wiley: Full-Mesh VPN Performance for Edge-Cloud Continuum](https://onlinelibrary.wiley.com/doi/full/10.1002/spe.3329)
- [Medium: Rethinking Private Connectivity in Data Integration](https://rudhra13.medium.com/rethinking-private-connectivity-in-data-integration-from-ssh-tunnels-to-overlay-mesh-networks-89a020ae5918)
- [Tailscale: Secure Connectivity](https://tailscale.com/)
- [Netmaker: Zero Trust Platform](https://www.netmaker.io/)
- [ElasticScale: AWS Client VPN Alternatives](https://elasticscale.com/blog/aws-client-vpn-alternatives-why-you-should-look-elsewhere/)
- [Pinggy: Top 5 Self-Hosted VPNs in 2026](https://pinggy.io/blog/top_5_best_self_hosted_vpns/)
- [Palo Alto Networks: WireGuard vs OpenVPN](https://www.paloaltonetworks.com/cyberpedia/wireguard-vs-openvpn)

---

## Summary: Key Decisions Matrix

| Decision | Recommended Choice | Rationale |
|----------|-------------------|-----------|
| **IaC Tool** | Terraform with thin abstraction layer | Multi-cloud support, separate state per cloud |
| **GitOps** | ArgoCD with ApplicationSets | Mature, CNCF, dynamic source support |
| **Cache/State** | Valkey (drop-in Redis replacement) | BSD-3 license, CNCF-aligned, managed options on all clouds |
| **Autoscaling** | KEDA with Kafka lag triggers | Event-driven, scale-to-zero, OTel integration |
| **Observability** | OTel Collector -> ClickHouse + HyperDX | Unified backend, 10-100x compression, open source |
| **Document Store** | FerretDB over PostgreSQL 17 | Apache 2.0, consolidates on PostgreSQL, test workload first |
| **Auth** | Keycloak (central IdP) + Dex (ArgoCD) | Full OIDC/SAML/LDAP, local fallback, group RBAC |
| **K8s Management** | Rancher (local/edge) + managed K8s (cloud) | Unified provisioning, import existing clusters |
| **Ingress** | Traefik (replacing NGINX) | NGINX retiring March 2026, drop-in migration, Gateway API |
| **Storage** | Longhorn (edge) + Rook-Ceph (cloud) | Match complexity to environment |
| **Backup** | Velero + CSI snapshots | Cloud-agnostic, works with both storage solutions |
| **Streaming** | Kafka (KRaft mode, no ZooKeeper) | Industry standard, Schema Registry, multi-cloud options |
| **Analytics DB** | ClickHouse (tiered storage) | Columnar, high compression, OTel-native |
| **VPN** | WireGuard (primary) + OpenVPN (fallback) | Performance + firewall traversal |
| **Mesh VPN** | Netmaker or Headscale | Self-hosted WireGuard orchestration |
| **Secrets** | External Secrets Operator + cloud-native stores | Cloud-agnostic, GitOps-compatible |
