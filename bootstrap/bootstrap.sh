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
#   DFE_TENANCY              dev | small | large
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
  DFE_ENV DFE_CLOUD DFE_REGION DFE_DOMAIN DFE_TENANCY
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
for ns in argocd "${DFE_NAMESPACE}" strimzi clickhouse otel hyperdx; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | run kubectl apply -f -
  # JFrog regcred (if registry credentials provided)
  if [[ -n "${DFE_REGISTRY_USER:-}" ]]; then
    TARGET_NAMESPACE="$ns" envsubst < "${TEMPLATES_DIR}/regcred.yaml.tpl" | run kubectl apply -f -
  fi
done
# GHCR pull secret — uses GHCR_TOKEN from env or OpenBao (secret/dfe/ghcr-pat)
GHCR_PAT="${GHCR_TOKEN:-}"
if [[ -z "${GHCR_PAT}" ]]; then
  GHCR_PAT=$(/projects/hyperi-infra/scripts/bao-admin kv get -field=token secret/dfe/ghcr-pat 2>/dev/null || true)
fi
if [[ -n "${GHCR_PAT}" ]]; then
  kubectl -n "${DFE_NAMESPACE}" create secret docker-registry ghcr-pull-secret \
    --docker-server=ghcr.io \
    --docker-username=catinspace-au \
    --docker-password="${GHCR_PAT}" \
    --dry-run=client -o yaml | run kubectl apply -f -
  echo "  GHCR pull secret created/updated in ${DFE_NAMESPACE}"
else
  echo "  WARNING: No GHCR_TOKEN in env and OpenBao unreachable — pull secret not created"
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
envsubst < "${SCRIPT_DIR}/../argocd/bootstrap/keda-scalers-app.yaml" | run kubectl apply -f -
# cluster-addons ApplicationSet uses goTemplate — no envsubst needed
run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/argocd-cluster-addons.yaml"

echo ""
echo "=========================================="
echo "  Bootstrap complete (Build 1: static)    "
echo "=========================================="
echo "  ArgoCD will now sync Layer 2 (Build 2)  "
echo "  ArgoCD UI: https://argocd.${DFE_DOMAIN} "
echo "  Watch sync: kubectl -n argocd get app -w "
echo "=========================================="
