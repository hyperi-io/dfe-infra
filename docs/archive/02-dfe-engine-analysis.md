# DFE Engine -- Comprehensive Analysis for DFE 2.2 Upgrade

**Date:** 2026-03-30
**Repository:** `/projects/dfe-engine/`
**Version:** 1.7.0
**Language:** Python 3.12
**License:** FSL-1.1-ALv2

---

## 1. Architecture Overview

### 1.1 Overall Application Architecture

DFE Engine is a **dual-purpose Python package**: both a pip-installable library containing all DFE business logic, and a standalone **FastAPI API server** (entry point: `dfe-engine`). It sits at the centre of the DFE architecture as the "big dials" management layer -- the component through which users and the UI configure data streams, deploy services, manage schemas, run hunts, and orchestrate Kubernetes deployments via Argo CD.

**Architecture position:**

```
dfe-ui --> dfe-engine API (FastAPI) --> hyperi-pylib + dfe-schemas
                |
   Rust services (receiver, loader, archiver, transforms, fetcher)
                |
         (Kafka) --> (ClickHouse)
```

The engine does NOT run data-plane workloads. It is the **control plane** that generates configuration consumed by Rust services and Argo CD.

**Design patterns:**
- **YAML Single Source of Truth (SSoT):** All configuration is stored as YAML files in a git-backed directory via `DirectoryConfigStore` from hyperi-pylib. No PostgreSQL for config storage.
- **Contract-First API:** `openapi-spec/openapi.json` is committed and serves as the source of truth for the UI.
- **Plugin architecture:** Service types are registered via Python entry points (`dfe_engine.services` group).
- **Registry pattern:** Singleton registries (`SourceRegistry`, `ServiceConfigRegistry`, `DeploymentConfigRegistry`, `FieldMapRegistry`) backed by `DirectoryConfigStore` with in-memory caching and background polling refresh.
- **Pure compilation + imperative operations separation:** `HelmValuesCompiler` is pure (deterministic). Side-effectful operations (DDL execution, Kafka topic creation) are in `ImperativeOperations`.

### 1.2 Module Structure (115 source files, ~22,636 lines)

| Module | Path | Purpose |
|--------|------|---------|
| `api/` | `src/dfe_engine/api/` | FastAPI REST API: 9 routers, 43 endpoints, JWT auth, RBAC, pagination |
| `auth/` | `src/dfe_engine/auth/` | Role-permission RBAC, LocalAuthProvider (bcrypt), Cedar-compatible interface |
| `source/` | `src/dfe_engine/source/` | Source model (Pydantic), TypeRegistry (13 primitives), SourceRegistry (CRUD) |
| `schema/` | `src/dfe_engine/schema/` | v2 YAML-to-DDL pipeline: SchemaLoader, DDLGenerator, SchemaBuilderV2 |
| `services/` | `src/dfe_engine/services/` | Service config registry, plugin system (7 built-in plugins), source routing |
| `deployment/` | `src/dfe_engine/deployment/` | K8s deployment config, DeploymentConfigRegistry, t-shirt sizing, KEDA config |
| `helm/` | `src/dfe_engine/helm/` | HelmValuesCompiler, Argo CD CRD generators, RBAC CSV, environment config |
| `hunts/` | `src/dfe_engine/hunts/` | HuntEngine (background scheduler), RuleCreationService, alerting |
| `query/` | `src/dfe_engine/query/` | ViewExecutor (ClickHouse views), QueryResult (Arrow-native), catalog |
| `fieldmap/` | `src/dfe_engine/fieldmap/` | FieldMap model, resolver, ViewGenerator (Sigma/ECS/CIM mapping) |
| `sigma/` | `src/dfe_engine/sigma/` | Sigma rule conversion to ClickHouse SQL |
| `pipeline/` | `src/dfe_engine/pipeline/` | Vector pipeline generation (Jinja2 templates) |
| `clickhouse/` | `src/dfe_engine/clickhouse/` | ClickHouseManager (clickhouse-connect, HTTP pooling) |
| `ai/` | `src/dfe_engine/ai/` | AIModuleInterface ABC: QueryOptimiser, SchemaOptimiser, LogParser |
| `storage/` | `src/dfe_engine/storage/` | Multi-backend storage: Local, HTTP (Artifactory), S3 |
| `settings.py` | `src/dfe_engine/settings.py` | Pydantic settings cascade: defaults.yaml, config file, env vars |

### 1.3 API Design

The API follows **v1 versioned REST** at `/api/v1/`. OpenAPI spec at `openapi-spec/openapi.json` (4,549 lines, OpenAPI 3.1.0) documents 43 endpoints across 31 paths.

**Routers:**

| Router | Prefix | Purpose |
|--------|--------|---------|
| Auth | `/auth` | JWT auth, local accounts |
| Sources | `/sources` | Data stream source definitions CRUD |
| Services | `/services` | Rust service runtime configs |
| Deployments | `/deployments` | K8s deployment configs |
| Field Maps | `/field-maps` | Sigma/ECS/CIM field mappings |
| Rules | `/rules` | Hunt rule creation + SQL validation |
| Alerts | `/alerts` | Alert destination management |
| System | `/system` | Health, version, settings |
| Transforms | `/transforms` | WASM transform compilation + testing |

**Conventions:**
- **Pagination:** `PaginatedResponse[T]` compatible with TanStack Query `useInfiniteQuery`
- **Error handling:** Unified `ErrorResponse` model
- **Auth:** JWT Bearer tokens via `python-jose`, RBAC via `require_action()` dependency
- **Search/Sort:** In-memory helpers (YAML-backed registries fit in memory)

### 1.4 Integration Points

**dfe-ui:** Committed `openapi-spec/openapi.json` used for TypeScript type generation. Prism mock server via docker-compose.

**hyperi-pylib:** `hyperi-pylib[expression,http]>=2.25.0` -- logger, DirectoryConfigStore, HttpClient, CEL expression evaluator. Logger is mandatory; stdlib `logging` is forbidden.

**dfe-schemas:** Git submodule at `schemas/`. Meta-schema definitions (common headers, hunt result schemas).

### 1.5 "Big Dials" Infrastructure Management

1. **Helm Values Compilation** (`helm/compiler.py`): Merges deployment + service + source routing + environment into complete `values.yaml` per service
2. **Argo CD CRD Generation** (`helm/argo_app.py`, `helm/argo_rbac.py`): Application, AppProject CRDs, RBAC CSV
3. **DDL and Kafka Topic Compilation** (`helm/operations.py`): ClickHouse CREATE TABLE + Kafka topic specs from Sources
4. **T-Shirt Sizing** (`deployment/sizing.py`): Unified resource table (xs-xlarge, 2GiB:1CPU ratio)
5. **Source Routing** (`services/source_routing.py`): Receiver routing config and loader routing config
6. **Environment Configuration** (`helm/environment.py`): `EnvironmentConfig` for Kafka, ClickHouse, OTEL, Argo CD

---

## 2. Technical Implementation

### 2.1 Core Engine Logic

**Schema Management (v2 pipeline):**
1. `SchemaLoader` loads profile headers from versioned YAML
2. Source-specific columns from `meta_schema`, `derived_schema`, `additional_fields`
3. `SchemaLoader.compose()` merges profile + source columns
4. Validation against `TypeRegistry` (13 primitives)
5. `DDLGenerator` produces ClickHouse CREATE TABLE DDL (MergeTree, ReplicatedMergeTree, SharedMergeTree)

**Source model** (`source/models.py`): Top-level data entity with identity, schema config, match rule, transform, fetcher, sigma, and derived properties (topic_land, topic_load, table_name).

### 2.2 Configuration System

**Settings cascade** (`settings.py`):
1. `defaults.yaml` (package resource, lowest priority)
2. User config file (YAML, optional)
3. Environment variables (DFE_ prefix, highest priority)

The `DFESettings` Pydantic model contains 15 nested settings groups.

**Config directory** (`config/` submodule = the deployment's config repo):
```
config/
  environments/    -- EnvironmentConfig YAML
  services/        -- Rust service runtime configs
  deployment/      -- K8s deployment configs
  sources/         -- Source definitions
  field-maps/      -- Sigma/ECS/CIM field mappings
  hunts/           -- Hunt scheduler configs
  hunt-rules/      -- Jinja2 SQL rule templates
  queries/         -- Query definitions
```

### 2.3 Authentication and Authorisation

**Authentication:**
- `LocalAuthProvider`: Three fixed accounts (admin, operator, viewer) with bcrypt passwords
- JWT tokens via `python-jose` (HS256)
- Dev mode: `auth.enabled=False` returns root `AuthContext` for all requests

**Authorisation (RBAC):**
- `authorize(auth, action, resource)` signature (Cedar/OPAL compatible)
- Actions: `config:read/write`, `source:read/write`, `query:execute`, `helm:compile`, `helm:execute_ddl`, `helm:create_topics`
- Argo CD namespace: `argo:<resource>:<action>`
- Built-in roles: admin (wildcard), infra_admin, operator, viewer
- `group_role_mapping` maps OIDC groups to DFE roles

### 2.4 Helm Chart (`chart/`)

- `Chart.yaml`: type=application, apiVersion=v2, name=dfe-engine
- Resources: 250m-2 CPU, 512Mi-2Gi memory
- Service: ClusterIP:8000
- ConfigVolume: PVC for YAML SSoT config files
- Secrets: ClickHouse password, JWT secret, auth account passwords

### 2.5 Database Usage

**ClickHouse** (primary): clickhouse-connect driver, HTTP pooling, used for hunts, queries, DDL, schema management.

**PostgreSQL** (test only): Docker container for integration tests. Not used in production.

**Kafka:** Not consumed at runtime. Engine compiles topic specs; `confluent_kafka` used for topic creation as imperative operation.

### 2.6 Observability (Current State)

- Logging: `hyperi_pylib.logger` (structured)
- OTEL in Helm compiler: Injects `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`
- `OTelEnvironment` model: collector endpoint, protocol, Prometheus port for KEDA
- Hunt resource tracking: rows, bytes, memory, execution time
- **Not yet:** Auto-instrumentation of dfe-engine API with OTel SDK

### 2.7 Dependencies

**Runtime (key packages):**
- hyperi-pylib[expression,http]>=2.25.0
- fastapi>=0.135.1, uvicorn[standard]>=0.41.0
- clickhouse-connect>=0.13.0
- pyarrow>=23.0.1, pandas>=3.0.1
- python-jose[cryptography]>=3.5.0, bcrypt>=5.0.0
- pysigma>=1.1.1
- apscheduler>=3.11.2
- Jinja2>=3.1.6
- boto3>=1.42.60

### 2.8 Test Structure

92 test files across 3 tiers: unit, integration, e2e.
- Coverage: ~75%, threshold 70%, target 80%
- Parallel execution via pytest-xdist
- Docker containers for ClickHouse/PostgreSQL in integration tests

---

## 3. Installation Dependencies

### 3.1 Requirements
- **Python 3.12+**, package manager: **uv**, build system: **hatchling**
- Container: `python:3.12-slim`, multi-arch (amd64, arm64), non-root user
- Registry: Harbor (`harbor.hyperi.io/dfe/dfe-engine`)

### 3.2 External Services
| Service | Required For |
|---------|-------------|
| ClickHouse | Hunt execution, query execution, DDL deployment |
| Kafka | Topic creation, routing config generation |
| Argo CD | Application/AppProject CRD generation, RBAC |
| OTEL Collector | Telemetry endpoint injection |

### 3.3 CI/CD
- `hyperi-ci` reusable workflows
- Build: package (wheel) + app (container + Helm)
- Quality: ruff, semgrep, vulture, ty, bandit
- Semantic release (conventional commits)

---

## 4. DFE 2.2 Upgrade Considerations

### 4.1 OIDC/OAuth2 Auth

**Already in place:**
- `AuthContext` has `groups` field for OIDC groups
- `group_role_mapping` setting maps OIDC groups to DFE roles
- Argo RBAC generation supports OIDC group-to-role mapping
- `docs/oauth2/HYPERDX-MIDDLEWARE.md` documents OIDC middleware design

**Needed:**
1. OIDC token validation middleware (JWKS endpoint discovery, issuer/audience validation)
2. Dual-mode auth (local fallback + OIDC)
3. Move from HS256 to RS256/ES256 for external tokens
4. Envoy Gateway SecurityPolicy for native OIDC at gateway level

**Files to modify:** `api/deps.py`, `settings.py`, `auth/` (add OIDCProvider)

### 4.2 Config-Driven Patterns

**Already in place:** YAML SSoT, Pydantic settings cascade, git-aware writes, schema-less mode for new services.

**Still needed:** Config validation on git push, optimistic concurrency, config repo decision (mono vs separate).

### 4.3 ArgoCD Integration

**Already implemented:** HelmValuesCompiler, Application/AppProject CRD generation, RBAC CSV, external component support.

**For "ArgoCD as mechanism for large dial changes":** Engine writes to config repo -> ArgoCD auto-syncs. Gap: explicit sync trigger endpoint.

### 4.4 OTel/Observability

**Gaps:**
1. dfe-engine API auto-instrumentation with `opentelemetry-instrumentation-fastapi`
2. OTEL auto-configuration based on environment
3. Bridge `hyperi_pylib.logger` to OTEL log exporter
4. OTEL metrics in Rust services (replace Prometheus) -- elevated for 2.2
5. HyperDX (ClickStack) as standard deployment component

### 4.5 PostgreSQL 17

**Impact on dfe-engine is minimal** -- engine uses YAML SSoT, not PostgreSQL. Potential uses: query views catalog, task/job tracking, audit log, session storage.

---

## 5. Integration Points

### 5.1 Service Interactions

| Service | How It Interacts |
|---------|-----------------|
| dfe-receiver | Reads source routing config (generated by engine) |
| dfe-loader | Reads loader config (generated by engine) |
| dfe-archiver | Service + deployment config via registries |
| dfe-fetcher | Plugin in services system, SourceFetcher config |
| dfe-transform-wasm | API proxied through `/transforms/compile` and `/test` |
| dfe-ui | Consumes OpenAPI spec, calls REST endpoints |
| Argo CD | Consumes generated CRDs, values files, RBAC CSV |

### 5.2 Shared Configuration

- **Config repo** (a submodule at `config/`): Shared between engine and Rust services
- **Environment config** (`environments/*.yaml`): Single YAML per deployment environment
- **Secrets strategy:** YAML files never contain secrets inline; K8s Secrets referenced by name

### 5.3 Key Findings for DFE 2.2

**Strengths:** Config-driven architecture is mature, Argo CD integration comprehensive, OTEL foundation exists, auth model Cedar-compatible, plugin architecture enables extensibility.

**Gaps:** OIDC token validation, API auto-instrumentation, PostgreSQL 17 role TBD, Phase 3 API routers pending, test mock remediation needed.

**Risk:** `python-jose` may need replacement with `authlib` for OIDC; `diskcache` CVE mitigated by architecture; coverage floor at 70%.
