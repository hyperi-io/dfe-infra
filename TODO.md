# TODO - dfe-infra

This is the **single source of truth** for all tasks and progress.

---

## Active Tasks

- [ ] Deployment-architecture live build (devex/Rancher) `[NEXT -- focused live session]`
  - Per canonical doc `dfe-docs/deployment/state-and-repos.md` + boundary memory.
  - Prereqs (code, then live-validate on devex; do NOT render-only -- Phase 1 was
    reverted for that): (a) engine `dfe-api gitops publish --env --channel`
    (per-env/channel authoring to the deploy instance); (b) appset multi-source
    wiring -- pinned base chart (dfe-infra) + per-env overlay `$values` from the
    deploy instance (in-cluster Gitea); (c) `scripts/deploy_matrix.py` harness:
    per cell `publish -> wait-ready -> acceptance -> destroy -> assert-clean`.
  - Goal: **repeat create-test-teardown matrix solid on devex** (CH
    single/cluster/external x kafka disabled/single/cluster/external x
    standard/scale). This GATES the multi-cloud rollout below.
  - Live: validate via merge to a branch devex Argo tracks (watch sync), iterate.

- [ ] IaC test framework (pytest + kubeconform + tftest) `[PENDING]`
  - Current state: Research complete, framework decision made (pytest as single runner)
  - Next: Design test fixtures (helm_template, terraform_plan, kubeconform_validate), write tests
  - Existing 13 BATS tests stay, new tests go in pytest
  - See discussion notes at end of 2026-04-01 session

- [ ] Build and push remaining DFE service container images `[BLOCKED]`
  - Blocked by: image build ownership discussion (centralised vs per-app)
  - Decision: per-app repo builds, specs written for rustlib + hyperi-ci
  - Specs: hyperi-rustlib deployment-contract-ci-bridge, hyperi-ci container-build-pipeline
  - Derek is implementing the specs now

---

## Work Breakdown Structure (WBS)

### OIDC Infrastructure (Complete)

**Goal:** Implement dfe-infra side of OIDC requirements for dfe-engine

1. [x] Multi-provider Envoy Gateway SecurityPolicy (X-Oidc-Subject/Groups headers)
2. [x] dfe-engine OIDC secret mounting (providers[].envMappings from K8s Secrets)
3. [x] CRITICAL independence tests (13 BATS tests — infra works without dfe-engine)
4. [x] tf-oidc-secrets module (OpenBao seed paths)
5. [x] tf-oidc-google module (OAuth2 client + service account)
6. [x] tf-oidc-entra module (Entra app registration + Graph API)
7. [x] tf-oidc-okta module (Okta OIDC app)
8. [x] Network policy IdP egress documentation
9. [x] Smoke test expansion (multi-provider + independence checks)

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
14. [ ] Build remaining 11 DFE service images (blocked by CI spec implementation)
15. [ ] Verify full stack end-to-end (all pods Running, smoke tests pass)

### Deployment Contract + CI Pipeline (Specs Written)

**Goal:** Standardise container image builds across all DFE apps

- [x] Research existing rustlib deployment contract (3,500 lines, production in dfe-loader)
- [x] Write rustlib deployment-contract-ci-bridge spec
- [x] Write hyperi-ci container-build-pipeline spec
- [ ] Derek implementing both specs (in progress, separate repos)

### Multi-cloud rollout (AWS first) -- GATED

**Gate:** Do NOT start until the Rancher/devex **repeat create-test-teardown
matrix** (CH single/cluster/external x kafka modes x profiles) is solid (see
"Deployment-architecture live build" above). AWS first, then GCP, then Azure --
same overlay model, new `cloud-<cloud>.yaml` per cloud. Multi-cloud needs the
EKS/GKE/AKS clusters provisioned (they do not exist yet). Decision 2026-06-26.

### AWS EKS Deployment (Plan 07a) -- gated on devex matrix solid

**Goal:** Validate same IaC deploys to AWS EKS

1. [ ] Create tf-k8s-cluster module for EKS
2. [ ] Create tf-networking module for VPC
3. [ ] Deploy EKS cluster
4. [ ] Run bootstrap.sh against EKS
5. [ ] Verify all apps sync and pods run
6. [ ] **DESTROY SAME DAY** — never leave AWS infra running overnight

### dfe-vpn Migration

- [ ] dfe-openvpn → dfe-vpn rename/rewrite (in progress, separate project)

---

## Completed (This Session — 2026-04-01)

- [x] OIDC infrastructure — all 10 tasks (Envoy multi-provider, dfe-engine mounting, TF modules, tests)
- [x] Deployment contract research + specs for rustlib and hyperi-ci
- [x] Untracked docs/superpowers/ from git (was committed before gitignore rule)

## Completed (Previous Session — 2026-03-31)

- [x] Node scheduling, Valkey replacement, repo migration, ArgoCD goTemplate
- [x] dfe-common.image helper, GHCR setup, product/devex separation
- [x] Data layer (CNPG, FerretDB, ClickHouse, OTel, Strimzi)
- [x] All layer1 deps updated, dfe-engine running

---

## Backlog

### High Priority

- [ ] IaC test framework — pytest + kubeconform + tftest (discussion concluded, ready to implement)
- [ ] Envoy Gateway OCI chart — find ArgoCD-compatible install method (currently bootstrap-only)
- [ ] ClickHouse operator chart repo — find helm repo for clickhouse.com operator (standalone)

### Medium Priority

- [ ] ArgoCD ignoreDifferences for operator-managed resources (reduce OutOfSync noise)
- [ ] ESO ClusterSecretStore — fix vault token/approle for ESO to read from OpenBao
- [ ] Add documentation comments to common.yaml and chart values

### Low Priority

- [ ] Smoke test scripts — validate they work against live cluster
- [ ] CI workflow for DFE service image builds (reusable, per-service)

---

## Blocked

- [ ] 11 DFE service pods (ImagePullBackOff) **Blocked by:** Container images not built/pushed to GHCR
- [ ] Container image builds **Blocked by:** Derek implementing rustlib + hyperi-ci specs

---

## Notes for AI Assistants

This file is the **single source of truth** for tasks and progress.

**Rules:**

- All tasks go here, nowhere else
- Planning mode outputs go here (WBS section)
- Mark tasks `[IN PROGRESS]` when starting
- Mark tasks `[x]` when complete, move to Completed section
- Never add tasks to STATE.md or CLAUDE.md
