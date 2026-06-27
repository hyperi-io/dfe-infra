#!/usr/bin/env bash
# bootstrap.sh — Idempotent DFE cluster bootstrap (Build 1: static/release-pinned).
#
# This script installs Layer 1 infrastructure once per cluster. It only changes
# on DFE version bumps or customer environment customization. After it completes,
# ArgoCD takes over for dynamic Layer 2 management (Build 2).
#
# All steps use 'helm upgrade --install' or 'kubectl apply' — safe to re-run.
#
# Requirements:
#   - kubectl configured and pointing at the target cluster
#   - helm 3.x installed
#   - terraform >=1.6 or opentofu >=1.6 (detected automatically)
#   - envsubst (gettext package)
#
# Required environment variables (export from Terraform outputs):
#   DFE_ENV                  dev | stg | prod | local
#   DFE_CLOUD                aws | gcp | az | local
#   DFE_REGION               e.g. us-east-1, local
#   DFE_DOMAIN               e.g. devex.hyperi.io
#   DFE_PROFILE              standard | scale
#   DFE_REPO_URL             Git repo URL for ArgoCD
#   DFE_TARGET_REVISION      Git branch/tag (e.g. main)
#   DFE_STORAGE_CLASS        e.g. local-path, gp3, standard
#   DFE_NAMESPACE            K8s namespace for DFE apps (e.g. dfe-prod)
#   DFE_CLICKHOUSE_HOST      ClickHouse service hostname
#   DFE_KAFKA_BOOTSTRAP      Kafka bootstrap servers
#   DFE_OTEL_ENDPOINT        OTel Collector gRPC endpoint
#   DFE_VAULT_ADDR           OpenBao/Vault address
#   DFE_VAULT_ROLE_ID        ESO AppRole role_id
#   DFE_WORKLOAD_IDENTITY_ANNOTATIONS  JSON map of service → cloud identity annotations
#   DFE_REGISTRY_HOST        JFrog registry hostname
#   DFE_REGISTRY_USER        JFrog service account username
#   DFE_REGISTRY_TOKEN       JFrog API token
#
# Optional:
#   DFE_DRY_RUN=true         Print commands without executing (for CI validation)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="${SCRIPT_DIR}/templates"

# Detect Terraform or OpenTofu (support both)
TF=$(command -v tofu || command -v terraform || { echo "ERROR: install terraform (>=1.6) or opentofu (>=1.6)" >&2; exit 1; })
echo "Detected IaC tool: ${TF}"

# Read versions from SSOT (versions.yaml)
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CERT_MANAGER_VERSION=$(python3 "${SCRIPT_DIR}/read_versions.py" --file "${REPO_ROOT}/versions.yaml" bootstrap.cert-manager)
EXTERNAL_SECRETS_VERSION=$(python3 "${SCRIPT_DIR}/read_versions.py" --file "${REPO_ROOT}/versions.yaml" bootstrap.external-secrets)
ARGOCD_VERSION=$(python3 "${SCRIPT_DIR}/read_versions.py" --file "${REPO_ROOT}/versions.yaml" bootstrap.argocd)
echo "Versions (from versions.yaml): cert-manager=${CERT_MANAGER_VERSION} eso=${EXTERNAL_SECRETS_VERSION} argocd=${ARGOCD_VERSION}"

# Dry-run wrapper
run() {
  if [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
    echo "[DRY-RUN] $*"
  else
    "$@"
  fi
}

# Validate required variables
required_vars=(
  DFE_ENV DFE_CLOUD DFE_REGION DFE_DOMAIN DFE_PROFILE
  DFE_REPO_URL DFE_TARGET_REVISION
  DFE_STORAGE_CLASS DFE_NAMESPACE
  DFE_CLICKHOUSE_HOST DFE_KAFKA_BOOTSTRAP DFE_OTEL_ENDPOINT
  DFE_VAULT_ADDR DFE_VAULT_ROLE_ID
  DFE_WORKLOAD_IDENTITY_ANNOTATIONS
)
# Registry vars are optional — skip regcred if not set
# DFE_REGISTRY_HOST DFE_REGISTRY_USER DFE_REGISTRY_TOKEN
missing=()
for var in "${required_vars[@]}"; do
  [[ -z "${!var:-}" ]] && missing+=("$var")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "ERROR: Missing required environment variables: ${missing[*]}" >&2
  exit 1
fi

# Compute base64 auth for registry (only if registry vars are set)
if [[ -n "${DFE_REGISTRY_HOST:-}" ]] && [[ -n "${DFE_REGISTRY_USER:-}" ]]; then
  export DFE_REGISTRY_AUTH
  DFE_REGISTRY_AUTH=$(printf '%s:%s' "${DFE_REGISTRY_USER}" "${DFE_REGISTRY_TOKEN}" | base64)
fi

# Deploy-specific gitops repo (dfe-engine writes, Argo watches). Defaults to the
# in-cluster Gitea service; override DFE_CONFIG_REPO_URL to use external GitHub.
export DFE_CONFIG_REPO_URL="${DFE_CONFIG_REPO_URL:-http://dfe-gitea.gitea.svc.cluster.local:3000/dfe/deploy.git}"
export DFE_CONFIG_REPO_REVISION="${DFE_CONFIG_REPO_REVISION:-main}"
# Gitea admin user that dfe-engine pushes as.
GITEA_ADMIN_USER="${DFE_GITEA_ADMIN_USER:-dfe}"

# Add Helm repos (idempotent)
echo "==> [0/7] Adding Helm repositories"
run helm repo add jetstack https://charts.jetstack.io 2>/dev/null || true
run helm repo add external-secrets https://charts.external-secrets.io 2>/dev/null || true
run helm repo add argo https://argoproj.github.io/argo-helm 2>/dev/null || true
run helm repo update

echo "==> [1/7] Applying ArgoCD namespace + cluster secret"
if [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
  echo "[DRY-RUN] kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -"
  echo "[DRY-RUN] envsubst < ${TEMPLATES_DIR}/cluster-secret.yaml.tpl | kubectl apply -f -"
else
  kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
  envsubst < "${TEMPLATES_DIR}/cluster-secret.yaml.tpl" | kubectl apply -f -
fi

echo "==> [2/7] Installing cert-manager (idempotent)"
run helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --version "${CERT_MANAGER_VERSION}" \
  --set installCRDs=true \
  --wait --timeout 5m

echo "==> [3/7] Installing external-secrets (idempotent)"
run helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace external-secrets --create-namespace \
  --version "${EXTERNAL_SECRETS_VERSION}" \
  --wait --timeout 5m

echo "==> [4/7] Applying ESO ClusterSecretStore"
if [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
  echo "[DRY-RUN] envsubst < ${TEMPLATES_DIR}/eso-cluster-secret-store.yaml.tpl | kubectl apply -f -"
else
  envsubst < "${TEMPLATES_DIR}/eso-cluster-secret-store.yaml.tpl" | kubectl apply -f -
fi

echo "==> [4b/7] Creating imagePullSecrets"
for ns in argocd "${DFE_NAMESPACE}" strimzi clickhouse otel hyperdx gitea; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | run kubectl apply -f -
  # JFrog regcred (if registry credentials provided)
  if [[ -n "${DFE_REGISTRY_USER:-}" ]]; then
    TARGET_NAMESPACE="$ns" envsubst < "${TEMPLATES_DIR}/regcred.yaml.tpl" | run kubectl apply -f -
  fi
done
# Private registry pull secret (GHCR, ECR, GCR, etc.)
# Set DFE_PULL_SECRET_SERVER, DFE_PULL_SECRET_USER, DFE_PULL_SECRET_TOKEN in env.
if [[ -n "${DFE_PULL_SECRET_TOKEN:-}" ]]; then
  kubectl -n "${DFE_NAMESPACE}" create secret docker-registry ghcr-pull-secret \
    --docker-server="${DFE_PULL_SECRET_SERVER:-ghcr.io}" \
    --docker-username="${DFE_PULL_SECRET_USER:-token}" \
    --docker-password="${DFE_PULL_SECRET_TOKEN}" \
    --dry-run=client -o yaml | run kubectl apply -f -
  echo "  Pull secret created/updated in ${DFE_NAMESPACE}"
fi

# Gitea admin secret for the in-cluster deploy repo. Generated once (random),
# reused on re-runs, mirrored to the dfe namespace as dfe-engine's push creds.
# Skipped when DFE_CONFIG_REPO_URL points at an external repo (not in-cluster Gitea).
if [[ "${DFE_CONFIG_REPO_URL}" == *"dfe-gitea"* ]] && [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  echo "==> [4c/7] Ensuring Gitea admin secret (dfe-gitea-admin)"
  if kubectl -n gitea get secret dfe-gitea-admin >/dev/null 2>&1; then
    GITEA_ADMIN_PASSWORD=$(kubectl -n gitea get secret dfe-gitea-admin -o jsonpath='{.data.password}' | base64 -d)
  else
    GITEA_ADMIN_PASSWORD=$(openssl rand -hex 24)
  fi
  for ns in gitea "${DFE_NAMESPACE}"; do
    kubectl -n "$ns" create secret generic dfe-gitea-admin \
      --from-literal=username="${GITEA_ADMIN_USER}" \
      --from-literal=password="${GITEA_ADMIN_PASSWORD}" \
      --dry-run=client -o yaml | kubectl apply -f -
  done
  echo "  Gitea admin secret ready in gitea + ${DFE_NAMESPACE}"
elif [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
  echo "[DRY-RUN] ensure Gitea admin secret dfe-gitea-admin in gitea + ${DFE_NAMESPACE}"
fi

# Valkey for ArgoCD cache — check if already running, skip install if so.
# On fresh clusters: deploy plain Valkey manifest. On existing clusters: use existing.
VALKEY_SVC="${DFE_VALKEY_SERVICE:-valkey}"  # default: 'valkey' (hyperi-infra pattern)
echo "==> [5/7] Checking Valkey"
if kubectl -n argocd get svc "${VALKEY_SVC}" > /dev/null 2>&1; then
  echo "  Valkey service '${VALKEY_SVC}' already exists — skipping install"
else
  echo "  Deploying Valkey (ArgoCD cache) — plain manifest, no Bitnami"
  run kubectl apply -f "${TEMPLATES_DIR}/valkey.yaml"
  run kubectl -n argocd rollout status deployment/valkey --timeout=120s
fi

echo "==> [6/7] Installing ArgoCD with Valkey cache (idempotent)"
run helm upgrade --install argocd argo/argo-cd \
  --namespace argocd --create-namespace \
  --version "${ARGOCD_VERSION}" \
  --set redis.enabled=false \
  --set "externalRedis.host=${VALKEY_SVC}.argocd.svc.cluster.local" \
  --set "externalRedis.port=6379" \
  --wait --timeout 10m

echo "==> [7/7] Applying ArgoCD AppProjects + bootstrap ApplicationSet"
run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/appproject-bootstrap.yaml"
# Standalone in-repo chart apps are envsubst-templated (repoURL, cloud overlay)
envsubst < "${SCRIPT_DIR}/../argocd/bootstrap/envoy-gateway-config-app.yaml" | run kubectl apply -f -
envsubst < "${SCRIPT_DIR}/../argocd/bootstrap/network-policies-app.yaml" | run kubectl apply -f -
# NOTE: keda-scalers chart retired -- KEDA is now folded into each app chart
# (dfe-common.scaledobject helper), driven by the per-instance overlay.
# cluster-addons ApplicationSet uses goTemplate — no envsubst needed
run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/argocd-cluster-addons.yaml"

# Argo CD repo credentials for the (private) in-cluster deploy repo, so the
# deploy-repo app-of-apps can read it. Uses the Gitea admin creds.
if [[ "${DFE_CONFIG_REPO_URL}" == *"dfe-gitea"* ]] && [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  GITEA_PW=$(kubectl -n gitea get secret dfe-gitea-admin -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)
  if [[ -n "${GITEA_PW}" ]]; then
    kubectl -n argocd create secret generic deploy-repo-creds \
      --from-literal=type=git \
      --from-literal=url="${DFE_CONFIG_REPO_URL}" \
      --from-literal=username="${GITEA_ADMIN_USER}" \
      --from-literal=password="${GITEA_PW}" \
      --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | kubectl apply -f -
  fi
fi
# NOTE: the deploy-repo app-of-apps is retired. The engine no longer authors Argo
# Application/AppProject manifests -- the dfe-layer2-apps ApplicationSet fans out
# one Application per deploy-repo values file (git-files generator). The deploy
# repo's config_repo_url/revision (on the cluster secret) is consumed there.

echo ""
echo "=========================================="
echo "  Bootstrap complete (Build 1: static)    "
echo "=========================================="
echo "  ArgoCD will now sync Layer 2 (Build 2)  "
echo "  ArgoCD UI: https://argocd.${DFE_DOMAIN} "
echo "  Watch sync: kubectl -n argocd get app -w "
echo "=========================================="
