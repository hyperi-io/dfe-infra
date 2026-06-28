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
LOCAL_PATH_VERSION=$(python3 "${SCRIPT_DIR}/read_versions.py" --file "${REPO_ROOT}/versions.yaml" bootstrap.local-path-provisioner)
echo "Versions (from versions.yaml): cert-manager=${CERT_MANAGER_VERSION} eso=${EXTERNAL_SECRETS_VERSION} argocd=${ARGOCD_VERSION} local-path=${LOCAL_PATH_VERSION}"

# Dry-run wrapper
run() {
  if [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
    echo "[DRY-RUN] $*"
  else
    "$@"
  fi
}

# Detect-or-install helpers. DFE assumes only a bare cluster and brings what it
# needs -- but a destination may already run a cluster-singleton operator
# (cert-manager, ESO, Argo) whose cluster-scoped CRDs cannot be owned twice. So
# each install is gated: CRD already present -> ADOPT the existing operator (skip
# install, reuse its CRDs); absent -> INSTALL DFE-owned. Logs the decision (never
# a silent skip). DFE_FORCE_INSTALL=true overrides (always install).
# NOTE: the Argo-managed operators in argocd/appsets (external-dns, keda,
# metrics-server, reloader, cnpg) are NOT yet guarded -- per-operator appset
# adopt is a later iteration that needs a rich cluster to validate. On a bare
# cluster they install correctly.
dfe_have_crd() { kubectl get crd "$1" >/dev/null 2>&1; }

# dfe_should_install <name> <crd>  -> rc 0 = INSTALL, rc 1 = ADOPT (skip).
dfe_should_install() {
  local name="$1" crd="$2"
  if [[ "${DFE_FORCE_INSTALL:-false}" == "true" ]]; then
    echo "  [${name}] DFE_FORCE_INSTALL -> INSTALL DFE-owned"
    return 0
  fi
  if dfe_have_crd "${crd}"; then
    echo "  [${name}] detected (CRD ${crd}) -> ADOPT existing, skip install"
    return 1
  fi
  echo "  [${name}] not detected -> INSTALL DFE-owned"
  return 0
}

# Validate required variables
required_vars=(
  DFE_ENV DFE_CLOUD DFE_REGION DFE_DOMAIN DFE_PROFILE
  DFE_REPO_URL DFE_TARGET_REVISION
  DFE_STORAGE_CLASS DFE_NAMESPACE
  DFE_CLICKHOUSE_HOST DFE_OTEL_ENDPOINT
  DFE_VAULT_ADDR DFE_VAULT_ROLE_ID
  DFE_WORKLOAD_IDENTITY_ANNOTATIONS
)
# DFE_KAFKA_BOOTSTRAP is OPTIONAL: the standard profile is gRPC (kafka disabled),
# so it is empty there; only set when kafka.mode != disabled. Defaulted empty so
# the cluster-secret annotation renders blank (kafka-dependent apps are gated off
# in standard anyway).
export DFE_KAFKA_BOOTSTRAP="${DFE_KAFKA_BOOTSTRAP:-}"
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

echo "==> [1b/7] StorageClass (detect-or-install)"
# DFE assumes only a bare cluster. If a default StorageClass exists -> ADOPT it.
# If StorageClasses exist but none is default -> use DFE_STORAGE_CLASS as-is. If
# NONE exist (bare RKE2/EKS) -> INSTALL local-path-provisioner (pinned) and mark
# it default, so the substrate's PVCs (CH/Gitea/CNPG) can bind with no deployer
# input. This is the onboarding-contract storage derive.
if kubectl get storageclass -o jsonpath='{range .items[*]}{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}{"\n"}{end}' 2>/dev/null | grep -q true; then
  echo "  default StorageClass present -> ADOPT"
elif [[ -n "$(kubectl get storageclass -o name 2>/dev/null)" ]]; then
  echo "  StorageClass(es) present, none default -> using DFE_STORAGE_CLASS=${DFE_STORAGE_CLASS}"
else
  echo "  no StorageClass -> INSTALL local-path-provisioner ${LOCAL_PATH_VERSION}"
  run kubectl apply -f "https://raw.githubusercontent.com/rancher/local-path-provisioner/${LOCAL_PATH_VERSION}/deploy/local-path-storage.yaml"
  run kubectl -n local-path-storage rollout status deployment/local-path-provisioner --timeout=120s
  run kubectl patch storageclass local-path -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
fi

echo "==> [2/7] cert-manager (detect-or-install)"
if dfe_should_install cert-manager certificates.cert-manager.io; then
  run helm upgrade --install cert-manager jetstack/cert-manager \
    --namespace cert-manager --create-namespace \
    --version "${CERT_MANAGER_VERSION}" \
    --set installCRDs=true \
    --wait --timeout 5m
fi

echo "==> [3/7] external-secrets (detect-or-install)"
if dfe_should_install external-secrets clustersecretstores.external-secrets.io; then
  run helm upgrade --install external-secrets external-secrets/external-secrets \
    --namespace external-secrets --create-namespace \
    --version "${EXTERNAL_SECRETS_VERSION}" \
    --wait --timeout 5m
fi

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

echo "==> [6/7] ArgoCD with Valkey cache (detect-or-install)"
# If the destination already runs Argo (its Application CRD + a server deploy are
# present) we ADOPT it -- our AppProjects/ApplicationSets below register into the
# existing Argo. Otherwise install DFE-owned Argo. (Full isolation -- a dedicated
# dfe-system Argo scoped to dfe-* namespaces so it never couples to a host Argo --
# is the Phase 0d adopt-path refinement.)
if dfe_should_install argocd applications.argoproj.io; then
  run helm upgrade --install argocd argo/argo-cd \
    --namespace argocd --create-namespace \
    --version "${ARGOCD_VERSION}" \
    --set redis.enabled=false \
    --set "externalRedis.host=${VALKEY_SVC}.argocd.svc.cluster.local" \
    --set "externalRedis.port=6379" \
    --set-string 'configs.params.reposerver\.disable\.git\.modules=true' \
    --wait --timeout 10m
else
  echo "  Using existing ArgoCD; registering DFE AppProjects + ApplicationSets into it."
fi

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
