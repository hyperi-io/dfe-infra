#!/usr/bin/env bash
# preview.sh -- an isolated preview environment from BRANCH images.
#
# Builds each service from its own checkout, pushes to the registry's preview
# project (never the release registry, never main), deploys, then tears down.
#
# Usage:
#   preview.sh up   <name> [--hyperdx <branch>] [--engine <branch>] \
#                          [--infra <branch>] [--services hyperdx,engine]
#   preview.sh sync <name> [--services ...]
#   preview.sh down <name>
#   preview.sh status <name>
#
# Every location is an environment variable: REGISTRY_HOST and DEPLOY_REPO_URL
# (both required), PREVIEW_PROJECT, CHART_REGISTRY (the registry the thin charts
# are pulled from, default ghcr.io/hyperi-io), KUBE_CONTEXT (default: the
# kubeconfig's current context), and DFE_HYPERDX_REPO / DFE_ENGINE_REPO, which
# default to a sibling checkout beside this repo.
#
# Example:
#   preview.sh up hdx29 --hyperdx chore/upstream-sync-2.29 --services hyperdx
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECKOUTS="$(dirname "$REPO_ROOT")"

# ---- config -----------------------------------------------------------------
REGISTRY_HOST="${REGISTRY_HOST:-}"                 # the preview registry the branch images push to
PREVIEW_PROJECT="${PREVIEW_PROJECT:-dfe-preview}"  # the project images are pushed to
DEPLOY_REPO_URL="${DEPLOY_REPO_URL:-}"             # the cluster secret's repo_url
CHART_REGISTRY="${CHART_REGISTRY:-ghcr.io/hyperi-io}"  # where the thin charts are published
INFRA_BRANCH_DEFAULT="main"

# service registry: name | source repo | dockerfile (rel to repo) | image name
#   hyperdx image is built from the dfe-hyperdx FORK, engine image from dfe-engine.
#   Each deploys its thin chart, as layer2-apps.yaml does, with the image overridden.
declare -A SVC_REPO=(
  [hyperdx]="${DFE_HYPERDX_REPO:-$CHECKOUTS/dfe-hyperdx}"
  [engine]="${DFE_ENGINE_REPO:-$CHECKOUTS/dfe-engine}"
)
declare -A SVC_DOCKERFILE=(
  [hyperdx]="docker/hyperdx/Dockerfile"
  [engine]="Dockerfile"
)
# Build TARGET (multi-stage). hyperdx: `prod` = the app-only image the chart deploys
# (node alpine, serves UI + in-process api) -- NOT `all-in-one*` (bundles CH+otel).
declare -A SVC_TARGET=(
  [hyperdx]="prod"
  [engine]=""
)
# deploy.service, which names the integration values and the chart-digests entry.
declare -A SVC_SERVICE=(
  [hyperdx]="hyperdx"
  [engine]="dfe-engine"
)
# The thin chart's name, which is the service except where layer2-apps.yaml maps it.
declare -A SVC_CHART=(
  [hyperdx]="dfe-hyperdx"
  [engine]="dfe-engine"
)

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo ">> $*" >&2; }

ns_of() { echo "dfe-preview-$1"; }

# The context to act on: the caller's if set, else whatever kubectl is pointed at.
kube_context() {
  if [[ -n "${KUBE_CONTEXT:-}" ]]; then
    echo "$KUBE_CONTEXT"
    return
  fi
  kubectl config current-context 2>/dev/null \
    || die "no KUBE_CONTEXT set and no current kubeconfig context"
}

# ---- build + push one service image from a branch ---------------------------
build_push() {   # <service> <branch> <preview-name>  -> echoes the pushed tag
  local svc=$1 branch=$2 name=$3
  local repo="${SVC_REPO[$svc]:-}" dockerfile="${SVC_DOCKERFILE[$svc]:-}" target="${SVC_TARGET[$svc]:-}"
  [[ -n "$repo" ]] || die "unknown service '$svc'"
  [[ -d "$repo" ]] || die "no checkout for '$svc' at $repo -- set DFE_$(echo "$svc" | tr '[:lower:]' '[:upper:]')_REPO"
  [[ -n "$REGISTRY_HOST" ]] || die "set REGISTRY_HOST to the registry the preview images push to"
  local sha; sha=$(git -C "$repo" rev-parse --short "$branch") || die "bad branch $branch in $repo"
  local tag="${REGISTRY_HOST}/${PREVIEW_PROJECT}/${svc}:${name}-${sha}"
  log "build $svc  ${repo}@${branch} ($sha)  ->  $tag${target:+  (target: $target)}"
  docker build -t "$tag" ${target:+--target "$target"} -f "${repo}/${dockerfile}" "$repo"
  # Needs a prior `docker login "$REGISTRY_HOST"` as the preview project's robot.
  docker push "$tag"
  echo "$tag"
}

# ---- deploy the preview -----------------------------------------------------
# One Application per service, applied directly: a preview is ephemeral and
# single-service, so it never enters the declarative appset set. Its sources are
# layer2-apps.yaml's -- the thin chart the stack pins, dfe-extras, and this repo's
# integration values -- with the main image pointed at the branch build.
deploy() {   # <preview-name> <infra-branch> <svc=image> ...
  local name=$1 infra=$2; shift 2
  local ns; ns=$(ns_of "$name")
  [[ -n "$DEPLOY_REPO_URL" ]] || die "set DEPLOY_REPO_URL to the cluster secret's repo_url"
  for kv in "$@"; do
    local svc="${kv%%=*}" image="${kv#*=}"
    local service="${SVC_SERVICE[$svc]}" chart="${SVC_CHART[$svc]}"
    local digest; digest=$(python3 "${REPO_ROOT}/bootstrap/read_versions.py" "chart-digests.${service}") \
      || die "versions.yaml pins no chart-digests.${service}"
    [[ "$digest" == sha256:* ]] || die "chart-digests.${service} is '${digest}', not a published chart"
    log "apply Application preview-${name}-${svc}  (${chart}@${digest:0:19}..., values @ ${infra}, ns ${ns})"
    kubectl --context "$(kube_context)" apply -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: preview-${name}-${svc}
  namespace: argocd
spec:
  project: default
  destination:
    server: https://kubernetes.default.svc
    namespace: ${ns}
  sources:
    - repoURL: oci://${CHART_REGISTRY}/charts/${chart}
      path: "."
      targetRevision: "${digest}"
      helm:
        ignoreMissingValueFiles: true
        valueFiles:
          - \$infra/argocd/values/common.yaml
          - \$infra/argocd/values/apps/_common.yaml
          - \$infra/argocd/values/apps/${service}/values.yaml
        parameters:
          - name: global.registry
            value: "${CHART_REGISTRY}"
          - name: image.repository
            value: "${image%:*}"
          - name: image.tag
            value: "${image##*:}"
          - name: image.digest
            value: ""
    - repoURL: ${DEPLOY_REPO_URL}
      targetRevision: ${infra}
      path: helm/charts/dfe-extras
      helm:
        ignoreMissingValueFiles: true
        valueFiles:
          - \$infra/argocd/values/common.yaml
          - \$infra/argocd/values/apps/_common.yaml
          - \$infra/argocd/values/apps/${service}/values.yaml
        parameters:
          - name: chartName
            value: ${chart}
          - name: global.registry
            value: "${CHART_REGISTRY}"
    - repoURL: ${DEPLOY_REPO_URL}
      targetRevision: ${infra}
      ref: infra
  syncPolicy:
    automated: {}
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
EOF
    argocd app sync "preview-${name}-${svc}"
  done
}

# ---- subcommands ------------------------------------------------------------
cmd_up() {
  local name=$1; shift
  local infra="$INFRA_BRANCH_DEFAULT" services="hyperdx"
  declare -A BR=()
  while [[ $# -gt 0 ]]; do case "$1" in
    --hyperdx) BR[hyperdx]="$2"; shift 2;;
    --engine)  BR[engine]="$2"; shift 2;;
    --infra)   infra="$2"; shift 2;;
    --services) services="$2"; shift 2;;
    *) die "unknown flag $1";;
  esac; done
  IFS=',' read -ra SVCS <<< "$services"
  local images=()
  for svc in "${SVCS[@]}"; do
    [[ -n "${SVC_SERVICE[$svc]:-}" ]] || die "unknown service '$svc'"
    local br="${BR[$svc]:-main}"
    local tag; tag=$(build_push "$svc" "$br" "$name")
    images+=("${svc}=${tag}")
  done
  deploy "$name" "$infra" "${images[@]}"
  log "preview '$name' up in namespace $(ns_of "$name"). Port-forward or ingress to view."
}

cmd_sync() {  # rebuild changed images + resync (fast loop)
  local name=$1; shift
  cmd_up "$name" "$@"
}

cmd_down() {
  local name=$1; local ns; ns=$(ns_of "$name")
  log "delete preview '$name' (apps + namespace $ns)"
  # BSD xargs has no -r, so the empty case is guarded here rather than by a flag.
  local apps; apps=$(argocd app list -o name 2>/dev/null | grep "^preview-${name}-" || true)
  if [[ -n "$apps" ]]; then
    echo "$apps" | xargs -n1 argocd app delete --cascade -y || true
  fi
  kubectl --context "$(kube_context)" delete ns "$ns" --ignore-not-found
  # The preview tags stay until the registry project's retention policy drops them.
}

cmd_status() {
  local name=$1; local ns; ns=$(ns_of "$name")
  argocd app list 2>/dev/null | grep "preview-${name}-" || true
  kubectl --context "$(kube_context)" -n "$ns" get pods 2>/dev/null || true
}

main() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    up)     cmd_up "$@";;
    sync)   cmd_sync "$@";;
    down)   cmd_down "$@";;
    status) cmd_status "$@";;
    *) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 1;;
  esac
}
main "$@"
