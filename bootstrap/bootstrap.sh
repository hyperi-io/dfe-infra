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
#   - terraform >=1.6 or opentofu >=1.6 -- OPTIONAL here (used earlier for the
#     apply; bootstrap.sh only consumes its outputs via env, never invokes it)
#   - envsubst (gettext package)
#
# Required environment variables (export from Terraform outputs):
#   DFE_ENV                  dev | stg | prod | local
#   DFE_CLOUD                aws | gcp | az | local
#   DFE_REGION               e.g. us-east-1, local
#   DFE_DOMAIN               e.g. dfe.example.com
#   DFE_PROFILE              slim | single | scale
#   DFE_REPO_URL             Git repo URL for ArgoCD (the CHART source)
#   DFE_REPO_TOKEN           optional; HTTPS token when the chart repo is private
#   DFE_REPO_USER            optional; username for DFE_REPO_TOKEN (default: git)
#   DFE_REPO_SSH_KEY         optional; path to an SSH key when the chart repo is
#                            private and DFE_REPO_URL is an SSH URL
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
#   DFE_POST=full            Power-on self test run after the deploy converges:
#                              full       readiness gate + CORE e2e (default)
#                              readiness  readiness gate only (fast, read-only)
#                              off        neither -- deploy health NOT verified
#                            A production deploy should run 'full'. 'readiness'
#                            and 'off' suit previews, or a stand-up that runs the
#                            POST separately (bootstrap/run-all-smoke-tests.sh).
#                            Anything not run is reported as NOT verified.
#                            Legacy per-gate vars are still honoured and override
#                            DFE_POST for that gate:
#                            DFE_SKIP_READINESS_GATE / DFE_SKIP_INTEGRATION_TESTS
#   DFE_POST_CLEANUP=false   Delete the CORE-2 sample rows once the ingest path
#                            is proven. Default keep -- the themed rows double as
#                            a live HyperDX JSON test bed, scoped to the run marker.
#   DFE_POST_FIXTURE=<path>  NDJSON sample-event fixture posted by CORE 2
#                            (default bootstrap/fixtures/post-hitchhiker.ndjson).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="${SCRIPT_DIR}/templates"

# Detect Terraform or OpenTofu (informational only). bootstrap.sh itself never
# invokes the IaC tool -- terraform/tofu runs EARLIER (the apply), and its outputs
# reach us as DFE_* env vars via bridge.py. So a missing binary is not fatal here:
# a deployer driving bootstrap from a pre-computed .env need not have it on PATH.
TF="$(command -v tofu || command -v terraform || true)"
if [[ -n "${TF}" ]]; then
  echo "Detected IaC tool: ${TF}"
else
  echo "No terraform/opentofu on PATH -- continuing (bootstrap.sh does not invoke it)."
fi

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

# dfe_should_install <name> <crd> [<ns> <deploy>]  -> rc 0 = INSTALL, rc 1 = ADOPT.
# ADOPT only when BOTH the CRD and a running operator deployment are present. A
# LINGERING CRD with no deployment (e.g. left behind by destroy.sh's resource-policy
# keep) must NOT fool us into skipping the install -- that stranded argocd +
# cert-manager (uninstalled but CRDs kept) on a clean-slate redeploy. So: CRD +
# deployment -> ADOPT; CRD but no deployment -> INSTALL (re-adopts the CRD).
dfe_should_install() {
  local name="$1" crd="$2" ns="${3:-}" deploy="${4:-}"
  if [[ "${DFE_FORCE_INSTALL:-false}" == "true" ]]; then
    echo "  [${name}] DFE_FORCE_INSTALL -> INSTALL DFE-owned"
    return 0
  fi
  if dfe_have_crd "${crd}"; then
    if [[ -n "${ns}" && -n "${deploy}" ]] && ! kubectl -n "${ns}" get deploy "${deploy}" >/dev/null 2>&1; then
      echo "  [${name}] CRD ${crd} present but ${ns}/${deploy} not running (lingering CRD) -> INSTALL"
      return 0
    fi
    echo "  [${name}] detected (CRD ${crd} + running operator) -> ADOPT existing, skip install"
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
# DFE_KAFKA_BOOTSTRAP is OPTIONAL: the slim profile is gRPC (kafka disabled),
# so it is empty there; only set when kafka.mode != disabled. Defaulted empty so
# the cluster-secret annotation renders blank (kafka-dependent apps are gated off
# in slim anyway).
export DFE_KAFKA_BOOTSTRAP="${DFE_KAFKA_BOOTSTRAP:-}"
# external-dns provider name (aws, google, azure, cloudflare, rfc2136, ...);
# "none" deploys no external-dns, because its own default provider is aws and an
# uncredentialled install crash-loops against Route 53 forever (#223).
export DFE_DNS_PROVIDER="${DFE_DNS_PROVIDER:-none}"
# devex/local enforces DFE onto its dedicated workers via a HARD nodeSelector
# (argocd/values/local.yaml). Label the nodes by default there so the selector is
# satisfiable; a shared/customer cluster labels its own nodes at provisioning.
DFE_LABEL_WORKLOAD_NODES="${DFE_LABEL_WORKLOAD_NODES:-$([[ "${DFE_CLOUD:-}" == "local" ]] && echo true || echo false)}"
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

# Deploy repo (dfe-engine writes config, Argo watches). PROVIDER-AGNOSTIC seam:
# external git (GitHub ~85% / GitLab ~10%) is PRIMARY; the in-cluster Forgejo
# fallback (~5%, tyre-kicking/air-gapped) is used ONLY when no external URL is
# given. If DFE_CONFIG_REPO_URL is set -> external mode (deployer also supplies
# creds, see [4d] below); unset -> bundled Forgejo fallback.
export DFE_CONFIG_REPO_REVISION="${DFE_CONFIG_REPO_REVISION:-main}"
# Admin user owns the bundled deploy repo (fallback path only). The engine's
# DFE_GITOPS_REPO_URL is injected from the config_repo_url annotation by the
# layer2-apps appset, so it tracks this URL automatically -- no manual
# owner-matching to keep in sync, and the engine cannot write a different repo
# than Argo reads.
FORGEJO_ADMIN_USER="${DFE_FORGEJO_ADMIN_USER:-dfe-admin}"
if [[ -n "${DFE_CONFIG_REPO_URL:-}" ]]; then
  export DFE_BUNDLED_DEPLOY_REPO="false"
  echo "Deploy repo: EXTERNAL git (${DFE_CONFIG_REPO_URL}) -- no in-cluster server."
else
  export DFE_CONFIG_REPO_URL="http://dfe-forgejo.forgejo.svc.cluster.local:3000/${FORGEJO_ADMIN_USER}/deploy.git"
  export DFE_BUNDLED_DEPLOY_REPO="true"
  echo "Deploy repo: FALLBACK in-cluster Forgejo (no external git supplied)."
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

echo "==> [1c/7] Node labels (dedicated-worker placement)"
# When a HARD nodeSelector is in play (devex/local) the target nodes MUST carry the
# dfe.hyperi.io/workload=dfe label or the data pods sit Pending forever. Label here
# as Layer-0 node-prep. Opt-in via DFE_LABEL_WORKLOAD_NODES (default on for local).
if [[ "${DFE_LABEL_WORKLOAD_NODES}" == "true" ]]; then
  run kubectl label nodes --all dfe.hyperi.io/workload=dfe --overwrite
  echo "  Labelled all nodes dfe.hyperi.io/workload=dfe"
else
  echo "  Node labelling skipped (DFE_LABEL_WORKLOAD_NODES=false) -- soft/no placement"
fi

echo "==> [2/7] cert-manager (detect-or-install)"
if dfe_should_install cert-manager certificates.cert-manager.io cert-manager cert-manager; then
  # enableGatewayAPI: the gateway-shim watches Gateway annotations and issues
  # the dfe-wildcard-tls Secret the https listener references; without it the
  # annotation is inert and the listener never programs.
  run helm upgrade --install cert-manager jetstack/cert-manager \
    --namespace cert-manager --create-namespace \
    --version "${CERT_MANAGER_VERSION}" \
    --set crds.enabled=true \
    --set config.enableGatewayAPI=true \
    --wait --timeout 5m
fi
# Private-CA issuance (the chart's tls.vault issuer mode): seed the AppRole
# SecretID cert-manager authenticates to Vault/OpenBao with. Outside the
# detect-or-install gate so an existing install still gets the secret.
if [[ -n "${DFE_CERTMANAGER_SECRET_ID:-}" ]] && [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  kubectl -n cert-manager create secret generic cert-manager-approle \
    --from-literal=secretId="${DFE_CERTMANAGER_SECRET_ID}" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "  Seeded cert-manager AppRole SecretID (cert-manager-approle)"
fi

echo "==> [3/7] external-secrets (detect-or-install)"
if dfe_should_install external-secrets clustersecretstores.external-secrets.io external-secrets external-secrets; then
  run helm upgrade --install external-secrets external-secrets/external-secrets \
    --namespace external-secrets --create-namespace \
    --version "${EXTERNAL_SECRETS_VERSION}" \
    --wait --timeout 5m
fi

echo "==> [4/7] ESO ClusterSecretStore (+ OpenBao AppRole SecretID & CA)"
if [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
  echo "[DRY-RUN] seed dfe-vault-approle-secret + envsubst store + patch caBundle"
else
  # Seed the AppRole SecretID the store references. Nothing else creates it, so ESO
  # could never authenticate to OpenBao (store stuck InvalidProviderConfig). Vault
  # provider path only; on cloud (AWS SM + IRSA) there is no SecretID.
  if [[ -n "${DFE_VAULT_SECRET_ID:-}" ]]; then
    kubectl create namespace external-secrets --dry-run=client -o yaml | kubectl apply -f -
    kubectl -n external-secrets create secret generic dfe-vault-approle-secret \
      --from-literal=roleSecretID="${DFE_VAULT_SECRET_ID}" \
      --dry-run=client -o yaml | kubectl apply -f -
    echo "  Seeded ESO AppRole SecretID (dfe-vault-approle-secret)"
  else
    echo "  WARNING: DFE_VAULT_SECRET_ID is unset, so dfe-vault-approle-secret was NOT created."
    echo "           ESO cannot authenticate to OpenBao, so every ExternalSecret stays unresolved."
    echo "           Downstream: dfe-local/dfe-kafka-user never appears and the data plane sits in"
    echo "           CreateContainerConfigError. Set DFE_VAULT_SECRET_ID and re-run."
  fi
  envsubst < "${TEMPLATES_DIR}/eso-cluster-secret-store.yaml.tpl" | kubectl apply -f -
  # ESO's vault provider cannot skip TLS verify (the env's VAULT_SKIP_VERIFY is for
  # terraform, which ESO ignores). Fetch OpenBao's issuing CA from its TLS handshake
  # and patch it into the store so it can verify the cert. No-op if none is found.
  if [[ -z "${DFE_VAULT_CA_BUNDLE:-}" ]] && [[ -n "${DFE_VAULT_ADDR:-}" ]]; then
    DFE_VAULT_CA_BUNDLE=$(echo | openssl s_client -connect "${DFE_VAULT_ADDR#*://}" -showcerts 2>/dev/null | python3 -c "import sys,re,base64; c=re.findall(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', sys.stdin.read(), re.S); sys.stdout.write(base64.b64encode(chr(10).join(c[1:]).encode()).decode() if len(c)>1 else '')" || true)
  fi
  if [[ -n "${DFE_VAULT_CA_BUNDLE:-}" ]]; then
    kubectl patch clustersecretstore dfe-secret-store --type merge \
      -p "{\"spec\":{\"provider\":{\"vault\":{\"caBundle\":\"${DFE_VAULT_CA_BUNDLE}\"}}}}"
    echo "  Patched OpenBao CA into the ESO store"
  fi
fi

echo "==> [4b/7] Creating imagePullSecrets"
# An imagePullSecret is namespace-scoped, so it goes in EVERY namespace that can
# pull a private DFE image -- not just the app namespace. The schema Job runs in
# `clickhouse` (where the admin credential lives) and pulls the engine image, so
# without this it only worked when an app deployment had already cached that tag
# on the node.
for ns in argocd "${DFE_NAMESPACE}" strimzi clickhouse otel hyperdx forgejo; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | run kubectl apply -f -
  # JFrog regcred (if registry credentials provided)
  if [[ -n "${DFE_REGISTRY_USER:-}" ]]; then
    TARGET_NAMESPACE="$ns" envsubst < "${TEMPLATES_DIR}/regcred.yaml.tpl" | run kubectl apply -f -
  fi
  # Private registry pull secret (GHCR, ECR, GCR, etc.)
  # Set DFE_PULL_SECRET_SERVER, DFE_PULL_SECRET_USER, DFE_PULL_SECRET_TOKEN in env.
  if [[ -n "${DFE_PULL_SECRET_TOKEN:-}" ]]; then
    kubectl -n "$ns" create secret docker-registry ghcr-pull-secret \
      --docker-server="${DFE_PULL_SECRET_SERVER:-ghcr.io}" \
      --docker-username="${DFE_PULL_SECRET_USER:-token}" \
      --docker-password="${DFE_PULL_SECRET_TOKEN}" \
      --dry-run=client -o yaml | run kubectl apply -f -
  fi
done
if [[ -n "${DFE_PULL_SECRET_TOKEN:-}" ]]; then
  echo "  Pull secret created/updated in every DFE namespace"
else
  echo "  WARNING: DFE_PULL_SECRET_TOKEN is unset, so ghcr-pull-secret was NOT created."
  echo "           Every app image from a private registry fails with ImagePullBackOff and"
  echo "           FailedToRetrieveImagePullSecret. Set DFE_PULL_SECRET_TOKEN and re-run."
fi

# [4c/7] Deploy-repo credentials -- two paths by provider mode.
if [[ "${DFE_BUNDLED_DEPLOY_REPO}" == "true" ]] && [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  # FALLBACK: in-cluster Forgejo. Generate (once) a random admin password, reuse
  # on re-runs. The Forgejo secret lives in the forgejo namespace (for Forgejo
  # itself); the engine's push cred goes into the dfe namespace under the
  # provider-agnostic name dfe-deploy-repo-auth (see [4c] external path -- SAME
  # name so the chart's credentialsSecret does not change between modes).
  echo "==> [4c/7] Ensuring Forgejo admin secret + engine deploy-repo write cred"
  if kubectl -n forgejo get secret dfe-forgejo-admin >/dev/null 2>&1; then
    FORGEJO_ADMIN_PASSWORD=$(kubectl -n forgejo get secret dfe-forgejo-admin -o jsonpath='{.data.password}' | base64 -d)
  else
    FORGEJO_ADMIN_PASSWORD=$(openssl rand -hex 24)
  fi
  kubectl create namespace forgejo --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n forgejo create secret generic dfe-forgejo-admin \
    --from-literal=username="${FORGEJO_ADMIN_USER}" \
    --from-literal=password="${FORGEJO_ADMIN_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl create namespace "${DFE_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n "${DFE_NAMESPACE}" create secret generic dfe-deploy-repo-auth \
    --from-literal=username="${FORGEJO_ADMIN_USER}" \
    --from-literal=password="${FORGEJO_ADMIN_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "  Forgejo admin secret (forgejo ns) + engine write cred dfe-deploy-repo-auth (${DFE_NAMESPACE}) ready"
elif [[ "${DFE_BUNDLED_DEPLOY_REPO}" != "true" ]] && [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  # EXTERNAL git (GitHub/GitLab/self-hosted): register the Argo READ credential so
  # Argo can pull the deploy repo -- EITHER HTTPS+token (DFE_CONFIG_REPO_USER +
  # DFE_CONFIG_REPO_TOKEN) OR an SSH key (DFE_CONFIG_REPO_SSH_KEY = path to a
  # private key). The engine's WRITE cred (dfe-deploy-repo-auth, dfe namespace) is
  # created below from the SAME token input -- same secret name as the bundled
  # path, so the dfe-engine chart is provider-agnostic.
  echo "==> [4c/7] Registering Argo repo cred for external deploy repo"
  if [[ -n "${DFE_CONFIG_REPO_SSH_KEY:-}" ]]; then
    kubectl -n argocd create secret generic repo-deploy \
      --from-literal=type=git \
      --from-literal=url="${DFE_CONFIG_REPO_URL}" \
      --from-file=sshPrivateKey="${DFE_CONFIG_REPO_SSH_KEY}" \
      --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | kubectl apply -f -
    echo "  Argo repo cred (SSH) registered for ${DFE_CONFIG_REPO_URL}"
  elif [[ -n "${DFE_CONFIG_REPO_TOKEN:-}" ]]; then
    kubectl -n argocd create secret generic repo-deploy \
      --from-literal=type=git \
      --from-literal=url="${DFE_CONFIG_REPO_URL}" \
      --from-literal=username="${DFE_CONFIG_REPO_USER:-oauth2}" \
      --from-literal=password="${DFE_CONFIG_REPO_TOKEN}" \
      --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | kubectl apply -f -
    echo "  Argo repo cred (HTTPS+token) registered for ${DFE_CONFIG_REPO_URL}"
  else
    echo "  WARNING: external deploy repo but no DFE_CONFIG_REPO_TOKEN/SSH_KEY -- Argo may not be able to pull it."
  fi
  # Engine WRITE cred, dfe namespace, provider-agnostic name (matches the bundled
  # path). The engine pushes over HTTPS+token (dulwich), so it needs a token even
  # when Argo READS via SSH. SSH-only external -> the engine cannot push; warn.
  if [[ -n "${DFE_CONFIG_REPO_TOKEN:-}" ]]; then
    kubectl create namespace "${DFE_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
    kubectl -n "${DFE_NAMESPACE}" create secret generic dfe-deploy-repo-auth \
      --from-literal=username="${DFE_CONFIG_REPO_USER:-oauth2}" \
      --from-literal=password="${DFE_CONFIG_REPO_TOKEN}" \
      --dry-run=client -o yaml | kubectl apply -f -
    echo "  Engine write cred dfe-deploy-repo-auth ready in ${DFE_NAMESPACE}"
  else
    echo "  WARNING: external deploy repo with no DFE_CONFIG_REPO_TOKEN -- the engine CANNOT push (gitcrud) over SSH; it needs an HTTPS token. Set DFE_CONFIG_REPO_TOKEN for engine writes."
  fi
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
# Argo HARDENING (dfe-infra#4): back off the controller timers so a degraded app
# can never monopolise the control plane (self-heal 5s->30s, reconciliation
# 180s->300s) and bound the repo-server timeout. Mirrors the devex platform guard.
if dfe_should_install argocd applications.argoproj.io argocd argocd-server; then
  run helm upgrade --install argocd argo/argo-cd \
    --namespace argocd --create-namespace \
    --version "${ARGOCD_VERSION}" \
    --set redis.enabled=false \
    --set "externalRedis.host=${VALKEY_SVC}.argocd.svc.cluster.local" \
    --set "externalRedis.port=6379" \
    --set-string 'configs.params.reposerver\.disable\.git\.modules=true' \
    --set-string 'configs.cm.timeout\.reconciliation=300s' \
    --set-string 'configs.params.controller\.self\.heal\.timeout\.seconds=30' \
    --set-string 'configs.params.controller\.repo\.server\.timeout\.seconds=60' \
    --set-string 'configs.params.controller\.diff\.server\.side=true' \
    --wait --timeout 10m
else
  echo "  Using existing ArgoCD; registering DFE AppProjects + ApplicationSets into it."
fi

# Argo CD repo credential for the CHART repo (DFE_REPO_URL), which is where Argo
# READS charts -- distinct from the DEPLOY repo credential in [4c/7], where the
# engine WRITES. Optional, so one bootstrap serves both postures: a public chart
# repo needs no credential, a private one takes a token or an SSH key.
if [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  if [[ -n "${DFE_REPO_SSH_KEY:-}" ]] && [[ -f "${DFE_REPO_SSH_KEY}" ]]; then
    echo "  [chart repo] SSH key supplied -> creating Argo repository credential"
    kubectl -n argocd create secret generic repo-charts \
      --from-literal=type=git \
      --from-literal=url="${DFE_REPO_URL}" \
      --from-file=sshPrivateKey="${DFE_REPO_SSH_KEY}" \
      --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | kubectl apply -f -
  elif [[ -n "${DFE_REPO_TOKEN:-}" ]]; then
    echo "  [chart repo] token supplied -> creating Argo repository credential"
    kubectl -n argocd create secret generic repo-charts \
      --from-literal=type=git \
      --from-literal=url="${DFE_REPO_URL}" \
      --from-literal=username="${DFE_REPO_USER:-git}" \
      --from-literal=password="${DFE_REPO_TOKEN}" \
      --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | kubectl apply -f -
  else
    echo "  [chart repo] no DFE_REPO_TOKEN/DFE_REPO_SSH_KEY -- assuming ${DFE_REPO_URL} is PUBLIC"
  fi
fi

echo "==> [7/7] Applying ArgoCD AppProjects + bootstrap ApplicationSet"
run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/appproject-bootstrap.yaml"
# Envoy Gateway operator: anonymous OCI Helm repo cred, then the operator app.
# The config app below needs the Gateway API CRDs the operator installs, so
# wait for them to establish before applying it (its sync retry budget is
# finite -- exhausting it on missing CRDs wedges the app until a manual
# refresh).
kubectl -n argocd create secret generic repo-envoyproxy-oci \
  --from-literal=type=helm \
  --from-literal=name=envoyproxy \
  --from-literal=url=registry-1.docker.io/envoyproxy \
  --from-literal=enableOCI="true" \
  --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | run kubectl apply -f -
run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/envoy-gateway-app.yaml"
if [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  echo "  waiting for the Gateway API CRDs (installed by the operator app)..."
  for _ in $(seq 1 60); do
    kubectl get crd httproutes.gateway.networking.k8s.io >/dev/null 2>&1 && break
    sleep 5
  done
  kubectl wait --for condition=Established --timeout=120s \
    crd/gatewayclasses.gateway.networking.k8s.io \
    crd/gateways.gateway.networking.k8s.io \
    crd/httproutes.gateway.networking.k8s.io || \
    echo "  WARNING: Gateway API CRDs not established -- envoy-gateway-config will need a retry"
fi
# NOTE: envoy-gateway-config and network-policies are generated by the
# layer2-platform ApplicationSet, which reads targetRevision from the cluster
# secret. They were applied directly here too until the appset covered them; a
# second copy baked the revision in at apply time, so a bootstrap from a feature
# branch stranded them once that branch was deleted.
# NOTE: keda-scalers chart retired -- KEDA is now folded into each app chart
# (dfe-common.scaledobject helper), driven by the per-instance overlay.
# cluster-addons ApplicationSet uses goTemplate — no envsubst needed
run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/argocd-cluster-addons.yaml"

# Prove Argo can actually READ the chart repo before declaring bootstrap done.
# An unreadable repo leaves every Application stuck Unknown, so layer2 never
# generates and the failure surfaces much later as missing ClickHouse and Kafka
# -- symptoms that name everything except the cause. Checked by reachability,
# not by URL scheme, so an SSH URL with a key supplied stays valid.
if [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  echo "==> Verifying ArgoCD can read the chart repo"
  repo_err=""
  for _ in $(seq 1 30); do
    # Force a refresh each pass. A ComparisonError is CACHED on the Application,
    # so an Application that failed before the credential existed keeps reporting
    # the old error and this check would fail a repo it can now read.
    kubectl -n argocd annotate applications --all \
      argocd.argoproj.io/refresh=hard --overwrite >/dev/null 2>&1 || true
    sleep 4
    repo_err=$(kubectl -n argocd get applications -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="ComparisonError")].message}{"\n"}{end}' 2>/dev/null | grep -m1 'failed to list refs' || true)
    [[ -z "${repo_err}" ]] && break
  done
  if [[ -n "${repo_err}" ]]; then
    echo "ERROR: ArgoCD cannot read the chart repo ${DFE_REPO_URL}" >&2
    echo "       ${repo_err}" >&2
    echo "       Set DFE_REPO_TOKEN (HTTPS) or DFE_REPO_SSH_KEY (SSH) if it is private." >&2
    exit 1
  fi
  echo "  [ok] chart repo readable"
fi

# Argo CD repo credential for the bundled in-cluster Forgejo deploy repo, so Argo
# can pull it. Uses the Forgejo admin creds. External git repo creds are handled
# in [4c/7] above; this block is the FALLBACK (bundled) path only.
if [[ "${DFE_BUNDLED_DEPLOY_REPO}" == "true" ]] && [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  FORGEJO_PW=$(kubectl -n forgejo get secret dfe-forgejo-admin -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)
  if [[ -n "${FORGEJO_PW}" ]]; then
    kubectl -n argocd create secret generic repo-deploy \
      --from-literal=type=git \
      --from-literal=url="${DFE_CONFIG_REPO_URL}" \
      --from-literal=username="${FORGEJO_ADMIN_USER}" \
      --from-literal=password="${FORGEJO_PW}" \
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
echo "  ArgoCD now syncs Layer 2 (Build 2);     "
echo "  the POST waits for it (DFE_POST).       "
echo "=========================================="

# POWER-ON SELF TEST (POST) -- the authoritative end-of-deploy verification, and a
# deployment PARAMETER rather than a bypass (DFE_POST; see the header). Two gates:
#   readiness   -- waits for Argo to converge Layer 2, then FAILS the deploy if
#                  anything is not genuinely Ready (crashloops, 0/N, unmet
#                  replicas). "Argo Healthy"/"pod Running" alone are not enough.
#   integration -- readiness proves pods are Ready; THIS proves the two DEFAULT
#                  ingest pipelines are actually STREAMING DATA end to end:
#                  (1) infra self-telemetry OTel -> HyperDX -> ClickHouse,
#                  (2) receiver -> [kafka default_land ->] loader -> dfe.default.
#                  "The service is up so it must be working" is the trap this closes.
# Choosing a lighter POST is legitimate (a preview, or a stand-up that runs the
# POST separately) -- but whatever we do not run we say we did NOT verify, so a
# green deploy never overstates what was actually proven.
DFE_POST="${DFE_POST:-full}"
case "${DFE_POST}" in
  full)      POST_READINESS=true;  POST_INTEGRATION=true  ;;
  readiness) POST_READINESS=true;  POST_INTEGRATION=false ;;
  off)       POST_READINESS=false; POST_INTEGRATION=false ;;
  *)
    echo "ERROR: DFE_POST must be one of: full | readiness | off (got '${DFE_POST}')" >&2
    exit 1
    ;;
esac
# Legacy per-gate vars win where explicitly set, so existing callers keep working.
if [ "${DFE_SKIP_READINESS_GATE:-false}" = "true" ]; then POST_READINESS=false; fi
if [ "${DFE_SKIP_INTEGRATION_TESTS:-false}" = "true" ]; then POST_INTEGRATION=false; fi

echo ""
echo "  POST: DFE_POST=${DFE_POST} (readiness=${POST_READINESS}, integration=${POST_INTEGRATION})"

echo ""
if [ "${POST_READINESS}" = "true" ]; then
  # DFE_NS names the namespace the apps land in; without it the gate cannot tell
  # an empty deploy from a healthy one. DFE_ENV decides whether the deployment is
  # allowed to be running the shipped admin password.
  if ! DFE_NS="${DFE_NAMESPACE}" \
       DFE_ENV="${DFE_ENV}" \
       "${SCRIPT_DIR}/smoke-test-readiness.sh" "${KUBECONFIG:-}"; then
    echo ""
    echo "  DEPLOY NOT HEALTHY -- see the readiness failures above."
    echo "  Fix them and re-run, or set DFE_POST=off to stand up without verifying."
    exit 1
  fi
else
  echo "  POST readiness gate NOT RUN -- deploy health NOT verified."
fi

echo ""
if [ "${POST_INTEGRATION}" = "true" ]; then
  # Hand the suite the namespaces + tier THIS deploy actually used. Without them it
  # falls back to its own defaults, which silently point at namespaces the deploy
  # never created (DFE_NAMESPACE is deployer-chosen), and the checks assert nothing.
  if ! DFE_NS="${DFE_NAMESPACE}" \
       DFE_PROFILE="${DFE_PROFILE:-}" \
       "${SCRIPT_DIR}/smoke-test-integration.sh" "${KUBECONFIG:-}"; then
    echo ""
    echo "  DEPLOY PODS HEALTHY but a CORE PIPELINE is NOT flowing -- see failures above."
    echo "  Fix them and re-run, or set DFE_POST=readiness to stand up without the e2e proof."
    exit 1
  fi
else
  echo "  POST integration gate NOT RUN -- data flow NOT verified."
fi

# Post-deploy ACCESS SUMMARY -- endpoints + how to log in + how to fetch creds.
# Reached once every POST gate that was ENABLED has passed -- so under
# DFE_POST=readiness the pipelines are unproven, and under DFE_POST=off nothing
# was verified at all. Printed here AND written to a file.
echo ""
"${SCRIPT_DIR}/access-summary.sh" "${KUBECONFIG:-}" "${DFE_ACCESS_OUT:-dfe-access.md}" || \
  echo "  (access-summary skipped -- run bootstrap/access-summary.sh manually)"
