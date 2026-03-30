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
  DFE_REGISTRY_HOST DFE_REGISTRY_USER DFE_REGISTRY_TOKEN
)
missing=()
for var in "${required_vars[@]}"; do
  [[ -z "${!var:-}" ]] && missing+=("$var")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "ERROR: Missing required environment variables: ${missing[*]}" >&2
  exit 1
fi

# Compute base64 auth for registry — envsubst cannot run subshells
export DFE_REGISTRY_AUTH
DFE_REGISTRY_AUTH=$(printf '%s:%s' "${DFE_REGISTRY_USER}" "${DFE_REGISTRY_TOKEN}" | base64)

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
  --version v1.14.0 \
  --set installCRDs=true \
  --wait --timeout 5m

echo "==> [3/7] Installing external-secrets (idempotent)"
run helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace external-secrets --create-namespace \
  --version 0.9.13 \
  --wait --timeout 5m

echo "==> [4/7] Applying ESO ClusterSecretStore"
if [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
  echo "[DRY-RUN] envsubst < ${TEMPLATES_DIR}/eso-cluster-secret-store.yaml.tpl | kubectl apply -f -"
else
  envsubst < "${TEMPLATES_DIR}/eso-cluster-secret-store.yaml.tpl" | kubectl apply -f -
fi

echo "==> [4b/7] Creating imagePullSecret for JFrog registry"
for ns in argocd "${DFE_NAMESPACE}" strimzi clickhouse otel hyperdx; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | run kubectl apply -f -
  TARGET_NAMESPACE="$ns" envsubst < "${TEMPLATES_DIR}/regcred.yaml.tpl" | run kubectl apply -f -
done

# Valkey MUST be installed before ArgoCD — ArgoCD --wait will timeout
# if the externalRedis host is unreachable on first boot.
echo "==> [5/7] Installing Valkey (ArgoCD cache, replaces Redis)"
run helm upgrade --install dfe-valkey oci://registry-1.docker.io/bitnamicharts/valkey \
  --namespace argocd --create-namespace \
  --version 1.0.0 \
  --set auth.enabled=false \
  --wait --timeout 5m

echo "==> [6/7] Installing ArgoCD with Valkey cache (idempotent)"
run helm upgrade --install argocd argo/argo-cd \
  --namespace argocd --create-namespace \
  --version 7.3.0 \
  --set redis.enabled=false \
  --set "externalRedis.host=dfe-valkey-master.argocd.svc.cluster.local" \
  --set "externalRedis.port=6379" \
  --wait --timeout 10m

echo "==> [7/7] Applying ArgoCD AppProjects + bootstrap ApplicationSet"
run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/appproject-bootstrap.yaml"
run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/argocd-cluster-addons.yaml"

echo ""
echo "=========================================="
echo "  Bootstrap complete (Build 1: static)    "
echo "=========================================="
echo "  ArgoCD will now sync Layer 2 (Build 2)  "
echo "  ArgoCD UI: https://argocd.${DFE_DOMAIN} "
echo "  Watch sync: kubectl -n argocd get app -w "
echo "=========================================="
