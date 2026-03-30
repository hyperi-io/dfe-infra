# DFE Infra 07 — DevEx Deployment (Live Validation)

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the full DFE 2.2 stack on the devex RKE2 cluster — run Terraform, bootstrap, validate ArgoCD syncs all waves, and confirm end-to-end functionality. This is the first real deployment using dfe-infra.

**Architecture:** `terraform apply` provisions OpenBao secrets + IAM → `bridge.py` reads outputs → `bootstrap.sh` installs Layer 1 → ArgoCD syncs waves 2-5 → all data platform + DFE services come up. Smoke tests validate each layer.

**Tech Stack:** Terraform, OpenBao (bao.devex.hyperi.io), RKE2 (k8s-{1,2,3}.devex.hyperi.io), kubectl, Helm 3, Python 3

**Target:** devex.hyperi.io RKE2 cluster (3 nodes, VIP 10.66.0.200)

---

## Prerequisites

- SSH access to devex infrastructure (this host: desktop-derek.devex.hyperi.io has direct access)
- `kubectl` configured for the RKE2 cluster (`/etc/rancher/rke2/rke2.yaml` or kubeconfig)
- OpenBao token with admin privileges (`VAULT_TOKEN` env var)
- Helm 3 + Terraform installed
- This repo checked out at `/projects/dfe-engine-infra`

---

## Chunk 1: Terraform Apply + Bootstrap

### Task 1: Configure kubectl for devex RKE2

- [ ] **Step 1: Verify cluster access**

  ```bash
  # Copy kubeconfig from k8s-1 if not already configured
  ssh ubuntu@k8s-1.devex.hyperi.io "sudo cat /etc/rancher/rke2/rke2.yaml" | \
    sed "s/127.0.0.1/10.66.0.200/" > ~/.kube/dfe-devex.yaml
  export KUBECONFIG=~/.kube/dfe-devex.yaml
  kubectl cluster-info
  kubectl get nodes
  ```

  Expected: 3 nodes Ready (k8s-1, k8s-2, k8s-3).

- [ ] **Step 2: Verify OpenBao access**

  ```bash
  export VAULT_ADDR=https://bao.devex.hyperi.io:8200
  export VAULT_SKIP_VERIFY=true  # self-signed cert
  vault status
  ```

  Expected: Vault is initialized and unsealed.

- [ ] **Step 3: Commit kubeconfig setup notes (not the kubeconfig itself)**

  No commit needed — this is environment setup.

---

### Task 2: Terraform Apply (Local Environment)

- [ ] **Step 1: Initialize Terraform**

  ```bash
  cd terraform/environments/local
  export TF_VAR_vault_token="${VAULT_TOKEN}"
  terraform init
  ```

- [ ] **Step 2: Plan and review**

  ```bash
  terraform plan -out=tfplan
  ```

  Review the plan output. Expected resources:
  - `vault_mount.dfe_kv` — KV v2 secrets engine
  - `vault_auth_backend.approle` — AppRole auth
  - `vault_policy.eso` — ESO policy
  - `vault_approle_auth_backend_role.eso` — ESO AppRole
  - `vault_approle_auth_backend_role_secret_id.eso` — ESO secret ID
  - `vault_kv_secret_v2.seed_argocd` — seed ArgoCD secret
  - Per-service AppRoles (engine, ui, receiver, loader, archiver, fetcher) via tf-iam
  - `null_resource.validate_canonical_name_length` — naming validation

  If any resources already exist (from hyperi-infra), Terraform will either adopt or error. Handle by importing existing resources:
  ```bash
  # Example: if KV mount already exists
  terraform import 'module.secrets.vault_mount.dfe_kv' secret
  # Example: if approle backend already exists
  terraform import 'module.secrets.vault_auth_backend.approle' approle
  ```

- [ ] **Step 3: Apply**

  ```bash
  terraform apply tfplan
  ```

  Expected: All resources created/updated. No errors.

- [ ] **Step 4: Verify outputs**

  ```bash
  terraform output -json | python3 -c "
  import json, sys
  outputs = json.load(sys.stdin)
  for k in sorted(outputs):
      v = outputs[k]
      val = '***' if v.get('sensitive') else str(v['value'])
      print(f'  {k} = {val}')
  "
  ```

  Expected: All DFE_* outputs present with correct values.

---

### Task 3: Run Bootstrap

- [ ] **Step 1: Dry-run first**

  ```bash
  cd /projects/dfe-engine-infra
  python3 bootstrap/bridge.py --tf-dir terraform/environments/local --dry-run
  ```

  Expected: All env vars displayed, `[DRY-RUN]` for each helm/kubectl command.

- [ ] **Step 2: Real bootstrap**

  ```bash
  python3 bootstrap/bridge.py --tf-dir terraform/environments/local
  ```

  Expected: Each step completes. ArgoCD namespace created, cert-manager installed, ESO installed, Valkey installed, ArgoCD installed, AppProjects applied, ApplicationSets applied.

  If any step fails: read the error, fix, re-run (bootstrap.sh is idempotent).

- [ ] **Step 3: Verify ArgoCD is running**

  ```bash
  kubectl -n argocd get pods
  kubectl -n argocd get app
  ```

  Expected: argocd-server, argocd-repo-server, argocd-application-controller all Running. Applications starting to sync.

---

## Chunk 2: Validate ArgoCD Sync + Wave Progression

### Task 4: Monitor Wave 2-3 Sync (Layer 1 Operators)

- [ ] **Step 1: Watch ArgoCD sync**

  ```bash
  kubectl -n argocd get app -w
  ```

  Wait for wave 2 components to sync:
  - cert-manager (adopted)
  - external-secrets (adopted)
  - envoy-gateway
  - external-dns
  - envoy-gateway-config (standalone Application)
  - network-policies (standalone Application)

  Then wave 3:
  - keda
  - metrics-server
  - stakater-reloader
  - cnpg (operator)
  - strimzi-kafka-operator
  - clickhouse-operator

- [ ] **Step 2: Run Layer 1 smoke test**

  ```bash
  bash bootstrap/smoke-test.sh
  ```

  Expected: All checks pass. If failures: check ArgoCD app status, pod logs.

- [ ] **Step 3: Fix any issues**

  Common issues:
  - **CRD not ready**: Wave 3 operators may take time to register CRDs. Wait and re-sync.
  - **Image pull errors**: If JFrog registry is configured, ensure regcred secret is in the right namespace.
  - **ESO SecretStore unhealthy**: Check OpenBao connectivity, AppRole credentials.
  - **Envoy Gateway not programmed**: Check GatewayClass and Gateway status.

  For each issue: fix the root cause (values, secrets, connectivity), then let ArgoCD re-sync.

---

### Task 5: Monitor Wave 4 Sync (Data Platform)

- [ ] **Step 1: Wait for wave 4 applications**

  ```bash
  kubectl -n argocd get app | grep -E "cnpg-cluster|clickhouse|strimzi-kafka|ferretdb|otel-collector"
  ```

  Each should show Synced/Healthy.

- [ ] **Step 2: Run data platform smoke test**

  ```bash
  bash bootstrap/smoke-test-data.sh
  ```

  Expected: CNPG cluster healthy (3 instances), Kafka brokers running, ClickHouse pods running, FerretDB ready, OTel Gateway + DaemonSet ready.

- [ ] **Step 3: Verify data connectivity**

  ```bash
  # PostgreSQL (via CNPG)
  kubectl -n cnpg exec -it dfe-pg-1 -- psql -U postgres -c "SELECT version();"

  # Kafka topics
  kubectl -n strimzi exec -it dfe-kafka-kafka-0 -- bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 --list

  # ClickHouse
  kubectl -n clickhouse exec -it chi-dfe-clickhouse-dfe-0-0-0 -- \
    clickhouse-client --query "SELECT version()"

  # OTel Collector Gateway (should be receiving metrics)
  kubectl -n otel logs -l app.kubernetes.io/name=dfe-otel-collector-gateway --tail=20
  ```

---

### Task 6: Monitor Wave 5 Sync (DFE Apps)

- [ ] **Step 1: Watch wave 5 applications**

  ```bash
  kubectl -n argocd get app | grep -E "dfe-engine|dfe-ui|dfe-receiver|dfe-loader|hyperdx"
  ```

  Note: DFE apps may fail initially if their container images aren't built yet. This is expected for a fresh deployment — the images need to be built and pushed to the registry first.

- [ ] **Step 2: Check which apps are image-ready vs pending**

  For each app that's in `ImagePullBackOff`:
  - This is expected if the service image hasn't been built yet
  - The chart structure is correct; just waiting for images
  - Document which services need images built

- [ ] **Step 3: Run auth smoke test (for components that are running)**

  ```bash
  bash bootstrap/smoke-test-auth.sh
  ```

  Expected: GatewayClass, Gateway, HTTPRoutes, and NetworkPolicies should all be healthy regardless of app image status.

---

## Chunk 3: End-to-End Validation + Documentation

### Task 7: Validate End-to-End

- [ ] **Step 1: Access ArgoCD UI**

  ```bash
  # Get ArgoCD admin password
  kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
  echo ""

  # Port-forward (or access via Envoy Gateway if HTTPRoute is working)
  kubectl -n argocd port-forward svc/argocd-server 8443:443 &
  echo "ArgoCD UI: https://localhost:8443"
  ```

  Login with user `admin` + password from above. Verify all Applications are visible.

- [ ] **Step 2: KEDA smoke test**

  ```bash
  bash bootstrap/smoke-test-keda.sh
  ```

- [ ] **Step 3: Run ALL smoke tests in sequence**

  ```bash
  echo "=== Full DFE Deployment Validation ===" && \
  bash bootstrap/smoke-test.sh && \
  bash bootstrap/smoke-test-data.sh && \
  bash bootstrap/smoke-test-auth.sh && \
  bash bootstrap/smoke-test-keda.sh && \
  echo "=== ALL SMOKE TESTS PASSED ==="
  ```

---

### Task 8: Document Findings + Fix Forward

- [ ] **Step 1: Create deployment log**

  Create `docs/deployment-logs/devex-initial.md` documenting:
  - Date and versions deployed
  - Any Terraform import steps needed (pre-existing resources)
  - Any chart value overrides needed beyond defaults
  - Any issues encountered and fixes applied
  - Which DFE service images are available vs pending
  - Smoke test results

- [ ] **Step 2: Fix any chart/config issues discovered**

  For each issue found during deployment:
  - Fix in the chart/config/bootstrap script
  - Commit with descriptive message
  - Re-sync via ArgoCD (or re-run bootstrap if Layer 1)

- [ ] **Step 3: Commit deployment log + fixes**

  ```bash
  git add docs/deployment-logs/ && git commit -m "docs: add devex initial deployment log"
  # Push fixes
  git push
  ```

---

## Completion Criteria

- [ ] `terraform apply` succeeds in `environments/local/`
- [ ] `bootstrap/bridge.py` runs bootstrap.sh successfully against devex RKE2
- [ ] ArgoCD UI accessible and showing all Applications
- [ ] Layer 1 smoke test: all pass
- [ ] Data platform smoke test: all pass (CNPG, Kafka, ClickHouse, FerretDB, OTel)
- [ ] Auth smoke test: GatewayClass programmed, HTTPRoutes exist, NetworkPolicies applied
- [ ] KEDA smoke test: operator running, ScaledObjects exist
- [ ] Deployment log committed documenting the full process

---

### Task 9: Create Teardown Script (destroy.sh)

Deploy/destroy/deploy cycle will happen repeatedly. Need a clean, reliable teardown.

- [ ] **Step 1: Create `bootstrap/destroy.sh`**

  ```bash
  #!/usr/bin/env bash
  #  Project:      dfe-infra
  #  File:         destroy.sh
  #  Purpose:      Clean teardown of DFE deployment (reverse of bootstrap.sh)
  #  Language:     Bash
  #
  #  License:      FSL-1.1-ALv2
  #  Copyright:    (c) 2026 HYPERI PTY LIMITED
  set -euo pipefail

  readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  DRY_RUN="${DFE_DRY_RUN:-false}"
  run() {
      if [[ "${DRY_RUN}" == "true" ]]; then
          echo "[DRY-RUN] $*"
      else
          "$@"
      fi
  }

  echo "=== DFE Teardown ==="
  echo "This will DELETE all DFE resources from the cluster."
  echo ""

  if [[ "${DRY_RUN}" != "true" ]] && [[ "${1:-}" != "--force" ]]; then
      read -rp "Are you sure? (type 'yes' to confirm): " confirm
      if [[ "${confirm}" != "yes" ]]; then
          echo "Aborted."
          exit 0
      fi
  fi

  # Reverse order of bootstrap — apps first, infrastructure last

  echo "==> [1/7] Deleting ArgoCD Applications (wave 5 → wave 2)"
  run kubectl -n argocd delete applicationset --all 2>/dev/null || true
  run kubectl -n argocd delete app --all 2>/dev/null || true

  echo "==> [2/7] Waiting for ArgoCD to clean up managed resources..."
  sleep 10

  echo "==> [3/7] Deleting DFE data resources (CRDs)"
  run kubectl -n strimzi delete kafka --all 2>/dev/null || true
  run kubectl -n cnpg delete cluster --all 2>/dev/null || true
  run kubectl -n clickhouse delete clickhouseinstallation --all 2>/dev/null || true
  sleep 5

  echo "==> [4/7] Deleting DFE namespaces"
  for ns in strimzi clickhouse cnpg ferretdb otel hyperdx keda; do
      run kubectl delete ns "${ns}" --ignore-not-found 2>/dev/null || true
  done
  # DFE app namespace
  run kubectl delete ns "$(kubectl get ns -o name | grep dfe- | head -1 | sed 's|namespace/||')" --ignore-not-found 2>/dev/null || true

  echo "==> [5/7] Uninstalling ArgoCD + Valkey"
  run helm uninstall argocd -n argocd 2>/dev/null || true
  run helm uninstall dfe-valkey -n argocd 2>/dev/null || true

  echo "==> [6/7] Uninstalling Layer 1 (ESO, cert-manager)"
  run helm uninstall external-secrets -n external-secrets 2>/dev/null || true
  run helm uninstall cert-manager -n cert-manager 2>/dev/null || true

  echo "==> [7/7] Cleaning up namespaces"
  for ns in argocd cert-manager external-secrets envoy-gateway-system reloader; do
      run kubectl delete ns "${ns}" --ignore-not-found 2>/dev/null || true
  done

  echo ""
  echo "=== Teardown complete ==="
  echo "To also destroy Terraform state: cd terraform/environments/local && terraform destroy"
  echo "To redeploy: python3 bootstrap/bridge.py --tf-dir terraform/environments/local"
  ```

- [ ] **Step 2: Make executable, validate**

  ```bash
  chmod +x bootstrap/destroy.sh
  bash -n bootstrap/destroy.sh && echo "OK"
  ```

- [ ] **Step 3: Test dry-run**

  ```bash
  DFE_DRY_RUN=true bash bootstrap/destroy.sh
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add bootstrap/destroy.sh
  git commit -m "feat: add destroy.sh for clean teardown (deploy/destroy/deploy cycle)"
  ```

---

## Completion Criteria (updated)

- [ ] `terraform apply` succeeds in `environments/local/`
- [ ] `bootstrap/bridge.py` runs bootstrap.sh successfully against devex RKE2
- [ ] ArgoCD UI accessible and showing all Applications
- [ ] All 4 smoke tests pass (layer1, data, auth, keda)
- [ ] `destroy.sh` cleanly tears down the deployment
- [ ] Successful deploy → destroy → deploy cycle (idempotency proven)
- [ ] Deployment log committed

**This validates the deployment UX:** point at a cluster → supply domain → deploy → it works. Destroy → redeploy → still works.

**Next:** Plan 08 (AWS EKS) — same deploy/destroy/deploy cycle against a blank AWS account.
