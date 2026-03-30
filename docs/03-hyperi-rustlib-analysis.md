# hyperi-rustlib Comprehensive Analysis

**Date:** 2026-03-30
**Repository:** `/projects/hyperi-rustlib/`
**Version:** 1.20.2
**Language:** Rust 2024 (stable 1.94+)
**License:** FSL-1.1-ALv2

---

## 1. Architecture Overview

### 1.1 Crate Structure and Module Organisation

hyperi-rustlib is a single crate with **feature-gated modules**. Each module can be enabled/disabled independently. Ships as `hyperi-rustlib` on a private JFrog Artifactory Cargo registry.

**Default features:** `config`, `logger`, `metrics`, `runtime`, `shutdown`, `health`

**Core (always compiled):**

| Module | Purpose |
|--------|---------|
| `env` | Runtime environment detection (K8s, Docker, Container, BareMetal) |
| `kafka_config` | Shared librdkafka profiles, DFE topic naming, consumer group conventions |
| `sensitive` | `SensitiveString` type that redacts on serialise/display/debug |

**Feature-gated modules (24+ modules):**

| Module | Feature | Purpose |
|--------|---------|---------|
| `config` | `config` | 8-layer cascade, flat env vars, registry, sensitive redaction |
| `config::reloader` | `config-reload` | Hot-reload (SIGHUP, periodic, file polling) |
| `config::postgres` | `config-postgres` | PostgreSQL as config source |
| `logger` | `logger` | Format auto-detection, masking, throttling, security events |
| `metrics` | `metrics` | Prometheus scrape + process/container metrics |
| `metrics` (OTel) | `otel-metrics` | OTLP push via gRPC/HTTP, fanout to both backends |
| `metrics::dfe_groups` | `metrics-dfe` | Composable metric structs (app, buffer, consumer, sink, etc.) |
| `health` | `health` | Global health registry, `/readyz` aggregation |
| `shutdown` | `shutdown` | Global CancellationToken, SIGTERM/SIGINT handling |
| `transport` | `transport` | Traits (Sender/Receiver), factory, format detection, propagation |
| `transport::kafka` | `transport-kafka` | Kafka producer/consumer via librdkafka |
| `transport::grpc` | `transport-grpc` | DFE native gRPC protocol |
| `transport::vector_compat` | `transport-grpc-vector-compat` | Vector wire-protocol compatibility |
| `secrets` | `secrets` | Multi-provider secrets with caching |
| `secrets::vault` | `secrets-vault` | OpenBao/Vault integration |
| `secrets::aws` | `secrets-aws` | AWS Secrets Manager |
| `directory_config` | `directory-config` | File-based config store with refresh |
| `scaling` | `scaling` | KEDA scaling pressure calculation |
| `memory` | `memory` | Cgroup-aware OOM prevention |
| `deployment` | `deployment` | Helm/Docker contract validation and generation |
| `spool` | `spool` | Disk-backed message queue (yaque) |
| `tiered_sink` | `tiered-sink` | Circuit-breaker-protected tiered output |
| `expression` | `expression` | CEL expression evaluation |
| `cli` | `cli` | DfeApp trait, standard CLI framework |

### 1.2 Core Abstractions and Traits

**Six Core Pillars** (global singletons, auto-wiring):

| Pillar | Singleton | Pattern |
|--------|-----------|---------|
| Config | `OnceLock<Config>` | `config::get().unmarshal_key("section")` |
| Logging | Global `tracing` subscriber | `tracing::info!()` macros |
| Metrics | Global `metrics` recorder | `metrics::counter!()` (no-op if no recorder) |
| OTel Tracing | OTel subscriber + W3C traceparent | Auto-propagated in gRPC/Kafka/HTTP |
| Health | Global `HealthRegistry` | Modules auto-register; `/readyz` aggregates |
| Shutdown | `OnceLock<CancellationToken>` | SIGTERM/SIGINT -> all modules drain |

**Transport traits** (split design):
```rust
pub trait TransportSender: TransportBase {
    fn send(&self, key: &str, payload: &[u8]) -> impl Future<Output = SendResult> + Send;
}

pub trait TransportReceiver: TransportBase {
    type Token: CommitToken;
    fn recv(&self, max: usize) -> impl Future<...> + Send;
    fn commit(&self, tokens: &[Self::Token]) -> impl Future<...> + Send;
}
```

**Key:** `AnySender` (enum dispatch, not trait objects) enables runtime transport selection. Supports 7 backends: Kafka, gRPC, Memory, File, Pipe, HTTP, Redis.

### 1.3 How Services Consume the Library

| Service | Features Used |
|---------|------------|
| dfe-loader | transport-kafka |
| dfe-archiver | config, logger, metrics, transport-kafka, spool, tiered-sink |
| dfe-receiver | config, logger, metrics, http-server, transport-kafka, spool, tiered-sink, runtime, secrets |

Standard bootstrap (~15 lines):
```rust
config::setup(ConfigOptions { env_prefix: "DFE_LOADER".into(), .. })?;
logger::setup_default()?;
let mut metrics = MetricsManager::new("dfe_loader");
metrics.start_server("0.0.0.0:9090").await?;
let token = shutdown::install_signal_handler();
```

### 1.4 Protobuf Definitions

**DFE native transport** (`proto/dfe/transport/v1/dfe_transport.proto`):
- `PushRequest`: payload bytes + format hint (JSON/MsgPack/Arrow IPC) + metadata map
- `PushResponse`: accepted count
- `HealthCheck`: service health

**Vector wire-protocol compatibility** (`proto/vector/`): Vendored from Vector project. Enables rustlib to receive/send data from/to Vector agents via native gRPC.

---

## 2. Technical Implementation

### 2.1 Configuration System

**8-layer cascade** (highest to lowest priority):
1. CLI arguments
2. Environment variables (`{PREFIX}_{KEY}`)
3. `.env` file
4. PostgreSQL (optional)
5. `settings.{env}.yaml`
6. `settings.yaml`
7. `defaults.yaml`
8. Hard-coded defaults

**File discovery** searches: `./`, `./config/`, `/config/`, `~/.config/{app_name}/`, and extra paths.

**How dfe-engine controls config:**
1. **Helm values -> env vars**: `DFE_{SERVICE}_{KEY}` flat env vars, read via `ApplyFlatEnv`
2. **Config git directory**: YAML files at `/config/` mounted as ConfigMaps
3. **librdkafka settings**: Loaded from git-managed config files (only sanctioned exception)
4. **PostgreSQL config source**: Optional layer 4 for dynamic config

**Config hot-reload:**
- `SharedConfig<T>`: `Arc<RwLock<T>>` with version counter and watch channel
- `ConfigReloader<T>`: SIGHUP, periodic timer, or file mtime polling
- Validates before applying, integrates with registry updates

### 2.2 Metrics and Observability

**Dual backend:** Prometheus scrape (`/metrics` on :9090) + OTLP push (gRPC 4317 / HTTP 4318). `FanoutBuilder` sends to both when both features enabled.

**Standard DFE metrics:**

| Category | Key Metrics |
|----------|------------|
| Transport | `dfe_transport_{sent,send_errors,backpressured,refused}_total`, `dfe_transport_{healthy,queue_size,inflight}`, `dfe_transport_send_duration_seconds` |
| Pipeline | `dfe_pipeline_ready`, `dfe_pipeline_stall_seconds_total` |
| Records | `dfe_records_{received,delivered,filtered,dlq}_total` |
| Scaling | `dfe_scaling_{pressure,circuit_open,memory_pressure}` |
| Spool | `dfe_spool_{bytes,messages,disk_available}` |
| Security | `dfe_auth_failures_total`, `dfe_validation_failures_total` |

**Composable DFE metric groups** (`metrics-dfe` feature): AppMetrics, BufferMetrics, ConsumerMetrics, SinkMetrics, CircuitBreakerMetrics, BackpressureMetrics, EnrichmentMetrics, SchemaCacheMetrics.

**OTel metrics:** Respects `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `OTEL_METRIC_EXPORT_INTERVAL`. Bridges `metrics` crate facade to OTel SDK via `metrics-exporter-opentelemetry`.

### 2.3 Logging

- Format auto-detection: JSON in containers/CI, coloured text on TTY
- RFC 3339 timestamps with UTC
- Sensitive field masking via `MaskingWriter`
- Log throttling via `tracing-throttle`
- Security event logging: `SecurityEvent` struct for audit

### 2.4 Data Serialisation and Transport

- **Formats:** JSON (serde_json), MessagePack (rmp-serde), Arrow IPC (format hint)
- **Auto-detection:** `FormatDetector` from byte preamble
- **Trace propagation:** W3C `traceparent` in gRPC, Kafka, HTTP
- **Routed transport:** `RoutedSender` dispatches by key (receiver/fetcher use)

### 2.5 Performance

- Dynamic linking for C deps (rdkafka, libgit2, zstd, openssl, zlib) -- saves ~30min build
- Kafka: cooperative-sticky assignment, batch fetches (1MiB min)
- Tiered sink: LZ4 default compression, circuit breaker for dead sinks
- `parking_lot::RwLock` for read-heavy config access
- Enum dispatch (`AnySender`) avoids dynamic dispatch overhead

---

## 3. Installation Dependencies

### 3.1 Rust Toolchain
- **Edition:** 2024, **Min version:** 1.94 stable
- **Build jobs:** `CARGO_BUILD_JOBS=2` (OOM prevention)

### 3.2 System Libraries (Dynamic Linking)

| Feature | Library | Ubuntu Package |
|---------|---------|---------------|
| `transport-kafka` | librdkafka (>= 2.12.1) | `librdkafka-dev` (Confluent APT) |
| `directory-config-git` | libgit2 | `libgit2-dev` |
| `spool`/`tiered-sink` | zstd | `libzstd-dev` |
| (transitive) | OpenSSL | `libssl-dev` |

### 3.3 Key Dependencies
- **Transport:** rdkafka, tonic, prost (gRPC), redis, rmp-serde
- **HTTP:** axum (server), reqwest (client)
- **Secrets:** vaultrs (OpenBao/Vault), aws-sdk-secretsmanager
- **Resilience:** yaque (disk queue), tower-resilience (circuit breaker), moka (cache)
- **OTel:** opentelemetry + opentelemetry_sdk + opentelemetry-otlp, tracing-opentelemetry

---

## 4. DFE-Engine Specific Items

### 4.1 Config: How dfe-engine Pushes/Pulls Configuration

1. **Helm values -> flat env vars** (primary): `DFE_{SERVICE}_{KEY}` set by HelmValuesCompiler. Read via `flat_env_string()`, `flat_env_list()`, `flat_env_bool()`, `flat_env_parsed::<T>()`.
2. **Config git directory** (secondary): `/config/` mount with `DirectoryConfigStore`, change notifications, optional git commit-on-write.
3. **librdkafka profiles** (exception): Direct file load for Kafka broker configs.
4. **PostgreSQL** (optional): Layer 4 in cascade for dynamic config.

### 4.2 Metrics Exposed for dfe-engine/KEDA

| Category | Metrics |
|----------|---------|
| Transport | sent/errors/backpressured/refused totals, healthy/queue_size/inflight gauges, send duration histogram |
| Pipeline | ready gauge, stall duration |
| Records | received/delivered/filtered/dlq totals |
| Scaling | **`dfe_scaling_pressure`** (KEDA primary), circuit_open, memory_pressure |
| Spool | bytes, messages, disk_available |

**`dfe_scaling_pressure` is the key KEDA metric** -- combines consumer lag, circuit breaker state, and memory pressure into a 0.0-1.0 gauge for autoscaling decisions.

### 4.3 Control Interfaces

| Interface | Purpose |
|-----------|---------|
| Flat env vars | dfe-engine sets `DFE_{SERVICE}_{KEY}` in Helm |
| Config cascade | YAML from ConfigMaps at `/config/` |
| `DfeSource` | Topic naming: `{source}_land` -> `{source}_load` |
| Consumer groups | `dfe-{service}` (universal) or `dfe-{service}-{source}` (transform) |
| Health endpoints | `/readyz`, `/healthz` for K8s probes |
| Scaling pressure | `dfe_scaling_pressure` gauge for KEDA |
| Memory guard | `/memory/pressure`, OOM prevention |
| Config reload | SIGHUP, periodic, or file-change triggered |
| Secrets | OpenBao/Vault + AWS SM, for SASL credentials |
| CEL expressions | DFE expression profile for filtering/routing |
| Deployment contracts | Validates Helm charts + Dockerfiles against app contract |

### 4.4 Shared Type Definitions

- **DFE Transport Proto:** PushRequest/PushResponse, health check
- **DfeSource:** Topic naming convention, consumer group conventions
- **TransportConfig/TransportType:** Backend selection from config
- **KafkaConfig:** Brokers, topics, consumer group, SASL, TLS
- **KedaConfig/KedaContract:** KEDA autoscaling (min/max replicas, cooldown, triggers)
- **DeploymentContract:** Full deployment manifest (app, binary, ports, probes, env prefix)

---

## 5. DFE 2.2 Upgrade Considerations

### 5.1 OTel Readiness

**Exists:**
- OTel metrics export (OTLP gRPC/HTTP) with fanout
- W3C Trace Context propagation
- `tracing-opentelemetry` layer for traces
- Standard env var support

**Gaps for "auto OTel":**
1. No unified `otel::init()` for metrics + traces + logs together
2. OTel logging export not wired (tracing events don't push to OTLP logs endpoint)
3. Transport send/recv don't create OTel spans automatically
4. No OTel resource detection from K8s downward API
5. `otel-tracing` feature is minimal

### 5.2 Cloud-Agnostic Assessment

**Good:** Transport abstraction (7 backends), Kafka via librdkafka (any broker), pluggable secrets (File, Vault, AWS), config cascade works with any file mount, no hardcoded cloud endpoints.

**To address:** No GCP or Azure secrets providers yet (only AWS and Vault). AWS provider is feature-gated and opt-in.

### 5.3 ArgoCD Config Pattern Support

**Supported:** File-based config cascade from ConfigMap mounts, hot-reload via file polling (5s default), SIGHUP reload, config registry with change notification.

**Gaps:** No webhook/push model for config reload, no config version tracking (git SHA), no diff/audit trail on reload.

### 5.4 Hardcoded Assumptions

1. `hs-app` default app name (should be `dfe-app`)
2. `/app` container base path
3. Confluent APT repo for librdkafka builds
4. `{source}_land`/`{source}_load` topic naming (configurable but defaults encode DFE topology)
5. `dfe-{service}` consumer group naming
6. reqwest pinned to 0.12 (blocking dependency updates)
