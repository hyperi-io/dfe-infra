#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         scripts/test-route-exposure.sh
#  Purpose:      Assert the envoy-gateway-config route exposure cascade -- class
#                default, per-route opt-out, and the infra kill switch that beats
#                both -- plus which routes take the edge RBAC policy.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
# WHY THIS EXISTS
#   validate-charts.sh proves a chart RENDERS and that the API server accepts it.
#   It cannot prove that turning a switch off removed the right objects, and the
#   kill switch is exactly the kind of rule that silently stops working when a
#   route is added without a class. These are pure render assertions, so they
#   need no cluster.
#
# Usage:
#   scripts/test-route-exposure.sh
#
# Exit status is non-zero if any assertion fails.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}" || exit 1

CHART="helm/charts/envoy-gateway-config"
BASE_VALUES=(-f argocd/values/common.yaml -f argocd/values/local-dfe.yaml)
OIDC=(--set oidc.enabled=true
      --set-json 'oidc.providers=[{"name":"acme","issuerUrl":"https://id.example.com","clientId":"dfe"}]')

PASS=0
FAIL=0

# Render once per case and report the names of one kind, newline separated.
render_names() {
  local kind="$1"; shift
  helm template envoy-gateway-config "${CHART}" --namespace envoy-gateway-system \
    "${BASE_VALUES[@]}" --set appNamespace=dfe-local "$@" 2>/dev/null \
    | awk -v kind="${kind}" '
        /^kind: /   { k = $2 }
        /^  name: / { if (k == kind) print $2 }'
}

assert_has() {
  local label="$1" needle="$2" haystack="$3"
  if printf '%s\n' "${haystack}" | grep -qx -- "${needle}"; then
    echo "  [PASS] ${label}: ${needle} present"
    PASS=$((PASS+1))
  else
    echo "  [FAIL] ${label}: ${needle} MISSING"
    FAIL=$((FAIL+1))
  fi
}

assert_absent() {
  local label="$1" needle="$2" haystack="$3"
  if printf '%s\n' "${haystack}" | grep -qx -- "${needle}"; then
    echo "  [FAIL] ${label}: ${needle} STILL PRESENT"
    FAIL=$((FAIL+1))
  else
    echo "  [PASS] ${label}: ${needle} absent"
    PASS=$((PASS+1))
  fi
}

echo "=== route exposure cascade ==="

echo ""
echo "case 1 -- all defaults: every UI route renders, ingest stays off"
R="$(render_names HTTPRoute)"
for r in dfe-ui dfe-engine argocd hyperdx forgejo kafbat links otel; do
  assert_has "default" "${r}" "${R}"
done
assert_absent "default" "receiver" "${R}"

echo ""
echo "case 2 -- kill switch off: infra class gone, product and ingest stay"
R="$(render_names HTTPRoute --set exposure.infraUisExternal=false)"
for r in argocd hyperdx forgejo kafbat links; do
  assert_absent "killswitch" "${r}" "${R}"
done
for r in dfe-ui dfe-engine otel; do
  assert_has "killswitch" "${r}" "${R}"
done

echo ""
echo "case 3 -- kill switch beats a per-route enabled:true"
R="$(render_names HTTPRoute --set exposure.infraUisExternal=false --set routes.kafbat.enabled=true)"
assert_absent "killswitch-absolute" "kafbat" "${R}"

echo ""
echo "case 4 -- per-route opt-out removes only that route"
R="$(render_names HTTPRoute --set routes.kafbat.enabled=false)"
assert_absent "per-route" "kafbat" "${R}"
for r in argocd links dfe-ui; do
  assert_has "per-route" "${r}" "${R}"
done

echo ""
echo "case 5 -- product class answers only to its own flag"
R="$(render_names HTTPRoute --set routes.dfeUi.enabled=false)"
assert_absent "product" "dfe-ui" "${R}"
assert_has "product" "dfe-engine" "${R}"
assert_has "product" "argocd" "${R}"

echo ""
echo "case 6 -- edge RBAC covers the infra routes that take a policy"
R="$(render_names SecurityPolicy "${OIDC[@]}")"
for p in dfe-oidc-acme-argocd-admin dfe-oidc-acme-forgejo-admin dfe-oidc-acme-links-admin; do
  assert_has "edge-rbac" "${p}" "${R}"
done
# Exempt by design: kafbat runs its own OIDC, hyperdx is framed inside dfe-ui.
for p in dfe-oidc-acme-kafbat-admin dfe-oidc-acme-hyperdx-admin; do
  assert_absent "edge-rbac" "${p}" "${R}"
done

echo ""
echo "case 7 -- kill switch also withdraws the infra edge policies"
R="$(render_names SecurityPolicy "${OIDC[@]}" --set exposure.infraUisExternal=false)"
for p in dfe-oidc-acme-argocd-admin dfe-oidc-acme-forgejo-admin dfe-oidc-acme-links-admin; do
  assert_absent "killswitch-policy" "${p}" "${R}"
done

echo ""
echo "=== ${PASS} passed, ${FAIL} failed ==="
[ "${FAIL}" -eq 0 ] || exit 1
