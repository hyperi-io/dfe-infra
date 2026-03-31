# TODO - dfe-infra

This is the **single source of truth** for all tasks and progress.

---

## Active Tasks

- [ ] Build and push remaining DFE service container images `[PENDING]`
  - Only dfe-engine:2.2.0 exists in GHCR
  - 11 services need building: dfe-receiver, dfe-loader, dfe-archiver, dfe-fetcher, dfe-ui, dfe-transform-{wasm,vrl,vector,elastic,splack}, hyperdx
  - Rust services take 30+ min each; Python services (dfe-engine pattern) are fast
  - Use hypersec-ci-bot app for cross-repo checkout + GITHUB_TOKEN for GHCR push

---

## Work Breakdown Structure (WBS)

### DevEx Deployment Validation (Plan 07b)

**Goal:** Prove the full DFE stack runs end-to-end on dedicated devex nodes

1. [x] Provision 3 DFE worker nodes (dfe-k8s-1/2/3)
2. [x] Wire node scheduling into all Helm charts (nodeSelector + tolerations)
3. [x] Fix ArgoCD ApplicationSets (goTemplate, valueFiles paths, appProject rename)
4. [x] Fix repo access (deploy key, hyperi-ai public, submodule auth)
5. [x] Separate product defaults from devex config (common.yaml generic, local.yaml specific)
6. [x] Build + push dfe-engine:2.2.0 to GHCR (proof of life)
7. [x] Fix GHCR pull credentials (classic PAT in OpenBao, K8s pull secret)
8. [x] Fix VM balloon (2GB→8GB minimum, 32GB max)
9. [x] Fix network policies (K8s API egress, inter-namespace)
10. [x] Fix data layer (CNPG clean init, FerretDB→cnpg namespace, ClickHouse CRD migration)
11. [x] Fix OTel DaemonSet (missing ServiceAccount)
12. [x] Update all layer1 addons to latest versions
13. [x] Fix Strimzi Kafka version (3.9.0→4.1.1 for Strimzi 0.51)
14. [ ] Build remaining 11 DFE service images
15. [ ] Verify full stack end-to-end (all pods Running, smoke tests pass)

### AWS EKS Deployment (Plan 07a)

**Goal:** Validate same IaC deploys to AWS EKS

1. [ ] Create tf-k8s-cluster module for EKS
2. [ ] Create tf-networking module for VPC
3. [ ] Deploy EKS cluster
4. [ ] Run bootstrap.sh against EKS
5. [ ] Verify all apps sync and pods run
6. [ ] **DESTROY SAME DAY** — never leave AWS infra running overnight

### dfe-vpn Migration

**Goal:** Upgrade dfe-openvpn to dfe-vpn (aligning with new standards)

- [ ] dfe-openvpn → dfe-vpn rename/rewrite (in progress, separate project)

---

## Completed (This Session)

- [x] Node scheduling (dfe-common.scheduling helper, all 20 charts)
- [x] Bitnami Valkey replaced with plain Deployment manifest
- [x] Repo migrated to hyperi-io/dfe-infra with deploy key
- [x] ArgoCD goTemplate conversion (all 4 ApplicationSets)
- [x] dfe-common.image helper (registry-aware image refs)
- [x] hyperi-container-mgt GitHub App created (PEM in OpenBao)
- [x] GHCR PAT stored in OpenBao + GH org secret (GHCR_PAT)
- [x] Docker credential helper switched to secretservice (gnome-keyring)
- [x] Product/devex separation (deploy.sh sources .env, bootstrap.sh generic)
- [x] Network policies fixed (K8s API + inter-namespace egress)
- [x] CNPG PostgreSQL 3-node HA cluster running
- [x] FerretDB moved to cnpg namespace (PG secret sharing)
- [x] ClickHouse CRD migrated (altinity.com → clickhouse.com)
- [x] All layer1 deps updated to latest (cert-manager v1.20.1, ESO 2.2.0, KEDA 2.19.0, etc.)
- [x] OTel DaemonSet + Gateway running on all DFE nodes
- [x] dfe-engine pod running and healthy (first DFE service alive)

---

## Backlog

### High Priority

- [ ] Envoy Gateway OCI chart — find ArgoCD-compatible install method (currently bootstrap-only)
- [ ] ClickHouse operator chart repo — find helm repo for clickhouse.com operator (standalone deployments)
- [ ] Full /deps audit — verify all chart versions are truly latest and from well-maintained sources

### Medium Priority

- [ ] Add documentation comments to common.yaml and chart values (audit finding P2)
- [ ] ArgoCD ignoreDifferences for operator-managed resources (reduce OutOfSync noise)
- [ ] ESO ClusterSecretStore — fix vault token/approle for ESO to read from OpenBao

### Low Priority

- [ ] Smoke test scripts — validate they work against live cluster
- [ ] CI workflow for DFE service image builds (reusable, per-service)

---

## Blocked

- [ ] 11 DFE service pods (ImagePullBackOff) **Blocked by:** Container images not built/pushed to GHCR

---

## Notes for AI Assistants

This file is the **single source of truth** for tasks and progress.

**Rules:**

- All tasks go here, nowhere else
- Planning mode outputs go here (WBS section)
- Mark tasks `[IN PROGRESS]` when starting
- Mark tasks `[x]` when complete, move to Completed section
- Never add tasks to STATE.md or CLAUDE.md

**Status tags:**

- `[PENDING]` - Not started
- `[IN PROGRESS]` - Currently working on
- `[BLOCKED]` - Waiting on something
- `[x]` - Completed (checkbox checked)

**WBS Format:**

When breaking down complex work, use numbered steps under a feature heading.
Each step should be independently completable and testable.
