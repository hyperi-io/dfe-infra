# Project Context

**Project:** dfe-infra
**Purpose:** Multi-cloud Terraform + Helm + ArgoCD SSOT for DFE 2.2+ deployments (Rancher local, AWS, GCP, Azure)

> **Note:** The `hyperi-ai/` submodule provides standards and configuration — not
> code to import. Your project never imports or links to it.

---

## DO NOT ADD TO THIS FILE

**The following belong elsewhere:**

| Data | Correct Location |
|------|------------------|
| Version numbers | `VERSION` file, `git describe --tags` |
| Tasks/Progress | `TODO.md` |
| Session history | Git log (`git log --oneline -10`) |
| Changelog | `CHANGELOG.md` (semantic-release) |
| Dates | Git commit timestamps |

**This file is for static project context only.**

---

## Project Overview

### Architecture

Two-layer model: Layer 1 (base infra, cloud-specific) bootstrapped by `bootstrap.sh` (Build 1, static/release-pinned). Layer 2 (DFE platform, identical on any K8s) managed by ArgoCD (Build 2, dynamic/GitOps). Cluster secret annotation bridge carries Terraform outputs into the GitOps layer.

See `docs/superpowers/specs/2026-03-30-dfe-infra-design.md` for the full design spec.

### Key Components

1. **terraform/modules/** - Reusable IaC modules (tf-naming, tf-k8s-cluster, tf-iam, tf-secrets, etc.)
2. **helm/charts/** - One Helm chart per DFE service and data component
3. **argocd/** - ApplicationSets (matrix generator), AppProjects, cloud-specific values
4. **bootstrap/** - Idempotent cluster bootstrap script + envsubst templates

### Tech Stack

- **IaC:** Terraform >=1.6 OR OpenTofu >=1.6 (both supported, common HCL subset)
- **Orchestration:** ArgoCD 2.x with Valkey cache, ApplicationSet matrix generator
- **Ingress/Auth:** Envoy Gateway + OIDC SecurityPolicy (no nginx, no oauth2-proxy)
- **Observability:** OTel → ClickHouse → HyperDX (no Prometheus, Grafana, CloudWatch)
- **Database:** CNPG PostgreSQL 17, ClickHouse (Altinity operator), FerretDB
- **Messaging:** Strimzi Kafka (KRaft, SASL/SCRAM)
- **Secrets:** ESO + OpenBao (local) / cloud SM (AWS/GCP/Azure)
- **Autoscaling:** KEDA from OTel metrics (Kedify OTEL Scaler default)
- **Registry:** JFrog (temporary) → GHCR when OSS cutover

---

## Key Decisions

### Scripting Language Escalation Rule

**Decision:** As soon as bash gets complex or starts processing data using tools like jq, it should be converted to Python 3 + stdlib.
**Rationale:** Bash is fine for simple glue (kubectl, helm, envsubst). But data processing, JSON manipulation, conditional logic trees belong in Python for readability, testability, and error handling.
**How to apply:** bootstrap.sh stays bash (simple command orchestration). Anything parsing JSON, building complex data structures, or doing conditional logic → Python 3.

### No OpenSearch

**Decision:** OpenSearch is removed and deprecated from DFE 2.2. Do not reference or recommend it anywhere.
**Rationale:** Replaced entirely by OTel → ClickHouse → HyperDX stack.

### Terraform/OpenTofu Dual Support

**Decision:** Support both Terraform >=1.6 and OpenTofu >=1.6. Stay on the common HCL subset.
**Rationale:** OSS project — users may prefer either. Zero maintenance cost if we avoid tool-specific features.

### Container Registry Migration Path

**Decision:** JFrog temporarily, GHCR (ghcr.io/hyperi-io) long-term when OSS cutover.
**Rationale:** JFrog is current team standard. GHCR is natural for GitHub-hosted OSS. Migration is a one-line change in `argocd/values/common.yaml` (`global.registry`).

### Two-CI Model

**Decision:** Two separate CI pipelines: (1) self-CI validates dfe-infra code, (2) deployment CI deploys DFE clusters.
**Rationale:** Self-CI runs on every PR (fast, safe). Deployment CI is triggered deliberately (destructive, targets real infrastructure).

### NEVER Use Bitnami Charts

**Decision:** Never use Bitnami Helm charts for anything. Use operators or official charts instead.
**Rationale:** Bitnami charts have non-standard paths, custom entrypoints, image pull issues, and are prohibited by HyperI K8s standards. The Valkey Bitnami chart failure during devex deployment confirmed this.
**How to apply:** PostgreSQL→CNPG, Kafka→Strimzi, ClickHouse→Altinity, Redis/Valkey→Spotahome Operator or existing deployment, ArgoCD→official chart. If no operator exists, deploy via Deployment manifest directly.

### OIDC Group-Mapping Service (Parked)

**Decision:** DFE needs a standalone auth/group-mapping service that bridges OIDC provider group names to DFE RBAC groups. dfe-engine is the consumer and its API is being shaped now — this service will be specced after dfe-engine's auth interface stabilises.
**Targets:** Entra ID, Okta, Google Workspace, AWS IAM Identity Center, Auth0, Ping Identity, OneLogin, JumpCloud, Keycloak (self-hosted).
**How to apply:** Parked until dfe-engine auth work completes. Will be its own project (not in dfe-infra).

### Two Auth Modes

**Decision:** DFE supports two auth modes: (1) Simple — no OIDC, Envoy for routing only, dfe-engine LocalAuthProvider with basic username/password/groups. (2) Normal — full Envoy Gateway OIDC SecurityPolicy + jwt_authn claim forwarding.
**Rationale:** Simple mode enables local dev and air-gapped deployments without configuring an identity provider. Normal mode is for prod/preprod/testing.
**How to apply:** `oidc.enabled: false` (default) = simple mode. `oidc.enabled: true` in ArgoCD values = normal mode.

### No Prometheus — ALL OTel

**Decision:** No Prometheus anywhere in the stack. ALL metrics, logging, and tracing go through OTel. This includes KEDA — KEDA uses OTel-native metrics (via OTel Collector's metrics API or Kedify OTEL Scaler), not Prometheus triggers.
**Rationale:** OTel is the standardised pipeline. Adding Prometheus would be a second metrics stack to maintain.
**How to apply:** Never use `type: prometheus` in KEDA ScaledObjects. Use `type: metrics-api` querying the OTel Collector, or `type: external` with Kedify OTEL Scaler. The OTel Collector does expose a Prometheus-format endpoint (:8889) but this is an OTel component, not a Prometheus server.

### DevEx Infrastructure Access

**Access:** This host (desktop-derek.devex.hyperi.io) has direct kubectl access to the devex RKE2 cluster.
- **Cluster:** 3-node RKE2 at api.k8s.devex.hyperi.io:6443 (k8s-1, k8s-2, k8s-3)
- **OpenBao:** bao.devex.hyperi.io:8200 (VAULT_ADDR set in env)
- **Reference IaC:** /projects/hyperi-infra (FULL CRUD access)
- **DFE service repos:** /projects/dfe-* (all available locally)

**CRITICAL DEPLOYMENT RULES:**
- Do NOT deploy DFE to the existing k8s-1, k8s-2, k8s-3 nodes. These are the infra cluster managed by hyperi-infra.
- Stand up 3 DEDICATED K8s worker nodes for DFE workloads (new VMs via Proxmox/Ansible)
- The DFE deployment gets its own separate cluster or dedicated node pool — NOT shared with existing infra services
- OpenBao and other infra services on the existing nodes must not be disrupted
- When ready to deploy: provision new nodes first, then bootstrap DFE onto them

---

## External Dependencies

- **ArgoCD** - GitOps controller (Layer 2 management)
- **Envoy Gateway** - Ingress + OIDC auth
- **HyperDX** - Observability UI over ClickHouse
- **Strimzi** - Kafka operator (KRaft mode)
- **CNPG** - PostgreSQL 17 operator
- **FerretDB** - MongoDB wire protocol over CNPG PG17 (for HyperDX)
- **KEDA** - Event-driven autoscaling from OTel metrics
- **ESO** - External Secrets Operator (unified secrets from any backend)
- **hyperi-ai** - AI standards submodule (read-only, auto-updates)
- **hyperi-ci** - CI/CD framework with semantic-release enforcement

---

## Resources

**Documentation:**

- [DIRECTORY.md](DIRECTORY.md) - Canonical repo structure
- [docs/superpowers/specs/](docs/superpowers/specs/) - Design specifications
- [docs/superpowers/plans/](docs/superpowers/plans/) - Implementation plans
- [docs/01-07](docs/) - Research corpus (dfe-core, dfe-engine, rustlib, dfe-ui, best practices, hyperi-infra, synthesis)
- [docs/license-review.md](docs/license-review.md) - OSS license audit

**External:**

- [hyperi-io/licensing](https://github.com/hyperi-io/licensing) - OSS license policy
- [Spec Section 9](docs/superpowers/specs/2026-03-30-dfe-infra-design.md) - Naming & Tagging Standard

---

## Notes for AI Assistants

This file is **STATE.md**, symlinked as **CLAUDE.md**. It contains shared
project context visible to the whole team. Do not duplicate its contents
into auto-memory — read this file directly instead.

**DO NOT add:**

- Version numbers (use `git describe --tags`)
- Progress/tasks (use `TODO.md`)
- Dates or session history (use `git log`)
- "Current Session" or "Last Session" sections
- Personal preferences (use auto-memory)

**DO add:**

- Architecture decisions and rationale
- Key component descriptions
- External dependencies
- How things work (not what's happening)

When in doubt, ask: "Will this be true next week?" If no, it doesn't belong here.
