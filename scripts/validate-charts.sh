#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         scripts/validate-charts.sh
#  Purpose:      Render every chart with the real overlays and (optionally)
#                validate the result against a live API server, WITHOUT changing
#                anything on the cluster.
#  Language:     Bash
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
# WHY THIS EXISTS
#   `helm lint` passes on a chart that renders a root pod with no probes, and
#   `--dry-run=client` never sees the cluster's CRDs or admission policy. Neither
#   catches "this manifest is not something the API server would accept". A
#   server-side dry-run does: it runs full schema validation, CRD resolution and
#   admission, and creates nothing. That makes it the cheapest real check for the
#   securityContext / probe / capability work -- and it needs no deployment.
#
# NO ENVIRONMENT-SPECIFICS LIVE HERE (README: every deployment choice is a
# parameter). No cluster name, namespace, hostname or credential is hardcoded --
# all of it comes from the environment, so this runs the same against a dev
# cluster, a customer cluster, or nothing at all.
#
# NOTHING SECRET IS READ BY THIS SCRIPT. The only cluster handle is a kubeconfig
# CONTEXT NAME, and the kubeconfig itself lives outside the repo (KUBECONFIG /
# ~/.kube/config), so there is no credential to leak into git. Set the variables
# below in your shell, or in bootstrap/.env (gitignored -- see local.env.example).
#
# Environment:
#   DFE_VALIDATE_VALUES   Space-separated values files layered in order.
#                         Default: argocd/values/common.yaml
#   DFE_VALIDATE_NS       Namespace to render into. Default: dfe
#   DFE_VALIDATE_CHARTS   Space-separated chart names. Default: every chart.
#   DFE_KUBE_CONTEXT      kubectl context for the server-side dry-run. UNSET =
#                         render-only (offline; safe in CI with no cluster).
#   DFE_VALIDATE_OUT      Where rendered manifests are written.
#                         Default: a mktemp dir, removed on exit.
#
# Usage:
#   scripts/validate-charts.sh                       # render-only, all charts
#   DFE_KUBE_CONTEXT=<ctx> scripts/validate-charts.sh   # + server-side dry-run
#   DFE_VALIDATE_VALUES="argocd/values/common.yaml argocd/values/local.yaml" \
#     DFE_VALIDATE_CHARTS="dfe-receiver dfe-loader" scripts/validate-charts.sh
#
# Exit status is non-zero if any chart fails to render or is rejected.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}" || exit 1

VALUES_FILES="${DFE_VALIDATE_VALUES:-argocd/values/common.yaml}"
RENDER_NS="${DFE_VALIDATE_NS:-dfe}"
KUBE_CONTEXT="${DFE_KUBE_CONTEXT:-}"

CLEANUP_OUT=false
if [ -n "${DFE_VALIDATE_OUT:-}" ]; then
  OUT="${DFE_VALIDATE_OUT}"
  mkdir -p "${OUT}"
else
  OUT="$(mktemp -d)"
  CLEANUP_OUT=true
fi
cleanup() { [ "${CLEANUP_OUT}" = "true" ] && rm -rf "${OUT}"; }
trap cleanup EXIT

# Build the -f argument list, failing loudly on a values file that is not there
# rather than silently rendering defaults that no deployment actually uses.
VALUES_ARGS=()
for vf in ${VALUES_FILES}; do
  if [ ! -f "${vf}" ]; then
    echo "ERROR: values file not found: ${vf}" >&2
    exit 1
  fi
  VALUES_ARGS+=(-f "${vf}")
done

if [ -n "${DFE_VALIDATE_CHARTS:-}" ]; then
  CHARTS="${DFE_VALIDATE_CHARTS}"
else
  CHARTS=""
  for d in helm/charts/*/; do
    [ -f "${d}Chart.yaml" ] || continue
    CHARTS="${CHARTS} $(basename "${d}")"
  done
fi

MODE="render-only"
if [ -n "${KUBE_CONTEXT}" ]; then
  if kubectl --context "${KUBE_CONTEXT}" version -o json --request-timeout=15s >/dev/null 2>&1; then
    MODE="render + server-side dry-run (context: ${KUBE_CONTEXT})"
  else
    echo "ERROR: DFE_KUBE_CONTEXT='${KUBE_CONTEXT}' is set but that cluster is not reachable." >&2
    echo "       Unset it to run render-only, or fix the context." >&2
    exit 1
  fi
fi

echo "=== chart validation: ${MODE} ==="
echo "    values:    ${VALUES_FILES}"
echo "    namespace: ${RENDER_NS}"
echo ""

PASS=0; FAIL=0
FAILED=""

for chart in ${CHARTS}; do
  dir="helm/charts/${chart}"
  if [ ! -f "${dir}/Chart.yaml" ]; then
    echo "  [SKIP] ${chart} -- no such chart"
    continue
  fi

  # The vendored library dep must be present or the render fails for a reason
  # that has nothing to do with the chart under test. Only build when it is
  # genuinely missing: `helm dependency build` REPACKS the file:// library into a
  # fresh .tgz whose gzip timestamp differs every time, so running it
  # unconditionally would dirty the working tree on every validation run.
  if [ -f "${dir}/Chart.lock" ] && [ -z "$(find "${dir}/charts" -maxdepth 1 -name '*.tgz' -print -quit 2>/dev/null)" ]; then
    helm dependency build "${dir}" >/dev/null 2>&1 || true
  fi

  if ! helm template "${chart}" "${dir}" --namespace "${RENDER_NS}" \
        "${VALUES_ARGS[@]}" > "${OUT}/${chart}.yaml" 2>"${OUT}/${chart}.err"; then
    echo "  [RENDER-FAIL] ${chart}"
    head -5 "${OUT}/${chart}.err" | sed 's/^/      /'
    FAIL=$((FAIL+1)); FAILED="${FAILED} ${chart}(render)"
    continue
  fi

  # An empty render is not a pass -- it means every template was gated off, so
  # nothing was actually validated and a silent [PASS] would be a lie.
  if [ ! -s "${OUT}/${chart}.yaml" ]; then
    echo "  [EMPTY] ${chart} -- rendered nothing with these values (not validated)"
    continue
  fi

  if [ -z "${KUBE_CONTEXT}" ]; then
    echo "  [PASS] ${chart} (rendered)"
    PASS=$((PASS+1))
    continue
  fi

  if kubectl --context "${KUBE_CONTEXT}" apply --dry-run=server \
       -f "${OUT}/${chart}.yaml" --request-timeout=60s >"${OUT}/${chart}.apply" 2>&1; then
    echo "  [PASS] ${chart} (accepted by the API server)"
    PASS=$((PASS+1))
  else
    echo "  [DRYRUN-FAIL] ${chart}"
    grep -iE "error|invalid|forbidden|denied|not found" "${OUT}/${chart}.apply" \
      | head -5 | sed 's/^/      /'
    FAIL=$((FAIL+1)); FAILED="${FAILED} ${chart}(dry-run)"
  fi
done

echo ""
echo "=== ${PASS} passed, ${FAIL} failed ==="
if [ "${FAIL}" -gt 0 ]; then
  echo "    failed:${FAILED}"
  exit 1
fi
