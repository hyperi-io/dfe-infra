# Deployment Contract: Current State

Research summary for the rustlib deployment contract system and hyperi-ci integration.
This document captures the existing implementation as of 2026-04-01 (dfe-loader remediated,
other apps pending).

## What Already Exists

### hyperi-rustlib `deployment` feature (~3,500 lines)

The `deployment` module in rustlib is **production-ready and shipping in dfe-loader**.
It is NOT a proposal — it is working code.

**Module structure:**
- `src/deployment/contract.rs` — Type definitions (DeploymentContract, HealthContract, etc.)
- `src/deployment/generate.rs` — Generates Dockerfile, Helm chart (9 files), Docker Compose
- `src/deployment/validate.rs` — Validates hand-maintained artifacts against contract
- `src/deployment/native_deps.rs` — Maps rustlib feature flags to APT packages
- `src/deployment/keda.rs` — KEDA autoscaling contract
- `src/deployment/error.rs` — Error types

**Feature gate:** `deployment = ["serde_yaml_ng", "serde_json"]`

### DeploymentContract Struct

Single source of truth for how an app is containerised and deployed:

```rust
pub struct DeploymentContract {
    // Identity
    pub app_name: String,              // "dfe-loader"
    pub binary_name: String,           // Defaults to app_name
    pub description: String,           // One-line for Chart.yaml

    // Network
    pub metrics_port: u16,             // 9090
    pub health: HealthContract,        // /healthz, /readyz, /metrics
    pub extra_ports: Vec<PortContract>,

    // Configuration
    pub env_prefix: String,            // "DFE_LOADER" (uses __ nesting)
    pub metric_prefix: String,         // Prometheus namespace
    pub config_mount_path: String,     // "/etc/dfe/loader.yaml"
    pub default_config: Option<Value>, // App defaults for values.yaml
    pub entrypoint_args: Vec<String>,  // CMD args

    // Container
    pub base_image: String,            // "ubuntu:24.04"
    pub image_registry: String,        // "ghcr.io/hyperi-io"
    pub image_profile: ImageProfile,   // Production or Development
    pub native_deps: NativeDepsContract,

    // Kubernetes
    pub keda: Option<KedaContract>,    // None = no autoscaling
    pub depends_on: Vec<String>,       // Service dependencies
    pub secrets: Vec<SecretGroupContract>,
}
```

### What It Generates

From a single `DeploymentContract`, the library generates:

**1. Dockerfile** — production-grade, with:
- Automatic APT repo setup (Confluent for librdkafka, etc.)
- Feature-driven native deps (transport-kafka → librdkafka1 + Confluent repo)
- Non-root appuser (UID 1000)
- HEALTHCHECK from contract health paths
- EXPOSE from metrics_port + extra_ports
- Profile label (`io.hyperi.profile`)
- Production: stripped binary, minimal. Development: adds debug tools.

**2. Helm chart** — full directory:
```
chart/
├── Chart.yaml, values.yaml
├── templates/
│   ├── _helpers.tpl, deployment.yaml, service.yaml
│   ├── serviceaccount.yaml, configmap.yaml, secret.yaml
│   ├── hpa.yaml, keda-scaledobject.yaml, keda-triggerauth.yaml
│   └── NOTES.txt
```

**3. Docker Compose fragment** — for local dev.

**4. deployment-contract.json** — serialised contract for CI consumption.

### Native Dependency Auto-Detection

`NativeDepsContract::for_rustlib_features(&features, base_image)` maps:

| Feature | APT Repos | Packages |
|---------|-----------|----------|
| `transport-kafka` | Confluent | librdkafka1, libssl3, zlib1g |
| `spool` / `tiered-sink` | — | libzstd1 |
| `directory-config-git` | — | libgit2-1.7 |
| Any TLS feature | — | libssl3, zlib1g |

Base image → APT codename mapping (ubuntu:24.04 → noble, bookworm → bookworm, etc.)

### CLI Integration

Every app implementing `DfeApp` gets:

```bash
# Standard rustlib subcommand
dfe-loader generate-artefacts --output-dir docs/

# dfe-loader also has convenience flags
dfe-loader --emit-dockerfile
dfe-loader --emit-helm ./chart/
```

Outputs: `deployment-contract.json` + `metrics-manifest.json`

### Validation

```rust
validate_dockerfile(contract, "Dockerfile") -> Vec<ContractMismatch>
validate_helm_values(contract, "chart/")    -> Vec<ContractMismatch>
```

Checks: port matches, health paths, env prefix, config mount, KEDA thresholds.

### dfe-loader Implementation

dfe-loader has the full contract implemented in `src/config/loader.rs::deployment_contract()`.
The hand-maintained Dockerfile and Helm chart pass validation against the contract.

## What's Missing (Gaps to Address)

### 1. CI Integration (hyperi-ci)

hyperi-ci does NOT call `generate-artefacts` today. It expects Dockerfile and chart/
to already exist in the repo. The pipeline is:

```
Current:  developer writes Dockerfile → CI builds it → pushes image
Missing:  app emits contract → CI generates Dockerfile → CI builds it → pushes image
```

### 2. OCI Labels

The generated Dockerfile only has `io.hyperi.profile`. Missing standard OCI labels:
- `org.opencontainers.image.source` — GitHub repo URL
- `org.opencontainers.image.revision` — git commit SHA
- `org.opencontainers.image.created` — build timestamp
- `org.opencontainers.image.version` — app version
- `org.opencontainers.image.title` — app name
- `org.opencontainers.image.description` — from contract

### 3. Multi-Stage Build (cargo-chef)

The generated Dockerfile is runtime-only (COPY binary). The actual build
(cargo-chef planner → cook → build) is handled outside the generated Dockerfile.
CI needs to combine the build stage with the generated runtime stage.

### 4. Non-Rust Apps

Python apps (dfe-engine), Node apps (dfe-ui), and upstream images (HyperDX, FerretDB)
don't use rustlib. They need:
- A standard Dockerfile template per language (Python: uv + venv, Node: pnpm)
- CI validation of required labels, health checks, non-root user
- No contract generation — CI owns the build definition

### 5. GHCR Package Creation

GitHub Apps can't create the FIRST org package. First push must use GITHUB_TOKEN
in a GitHub Actions workflow. Subsequent pushes can use the app token.

### 6. dfe-infra Helm Charts vs Generated Charts

dfe-infra currently has hand-maintained Helm charts in `helm/charts/dfe-*`.
The rustlib contract can generate equivalent charts. Decision needed:
- Option A: dfe-infra charts are the SSOT, contract validates against them
- Option B: Generated charts replace dfe-infra charts (contract is the SSOT)
- Option C: dfe-infra charts are thin wrappers that consume contract output

## Key Design Points

1. **~20% config, ~80% boilerplate** — apps define contract, library generates everything
2. **Feature-driven** — APT packages inferred from rustlib features, not manual lists
3. **Deterministic** — same contract always produces identical output
4. **Validation not generation** — dfe-loader hand-maintains its Dockerfile but validates it against contract
5. **Opt-in** — `DfeApp::deployment_contract()` returns `Option`, default is `None`
6. **No external commands** — all generation is in-process string building
