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

CHART="helm/edge/gateway"
BASE_VALUES=(-f argocd/values/common.yaml -f argocd/values/local-dfe.yaml)
OIDC=(--set oidc.enabled=true
      --set-json 'oidc.providers=[{"name":"acme","issuerUrl":"https://id.example.com","clientId":"dfe"}]')

PASS=0
FAIL=0

ERR_FILE="$(mktemp)"
trap 'rm -f "${ERR_FILE}"' EXIT

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

# Render with the given extra --set/-f args; exit status is helm's own (0 =
# rendered, non-zero = refused). stderr lands in ERR_FILE, which the two
# assert_render_* helpers below read.
try_render() {
  helm template envoy-gateway-config "${CHART}" --namespace envoy-gateway-system \
    "${BASE_VALUES[@]}" --set appNamespace=dfe-local "$@" >/dev/null 2>"${ERR_FILE}"
}

assert_render_refused() {
  local label="$1" pattern="$2"; shift 2
  if try_render "$@"; then
    echo "  [FAIL] ${label}: expected a refusal, render succeeded"
    FAIL=$((FAIL+1))
  elif grep -q -- "${pattern}" "${ERR_FILE}"; then
    echo "  [PASS] ${label}: refused (${pattern})"
    PASS=$((PASS+1))
  else
    echo "  [FAIL] ${label}: refused, but not with '${pattern}':"
    sed 's/^/    /' "${ERR_FILE}"
    FAIL=$((FAIL+1))
  fi
}

assert_render_succeeds() {
  local label="$1"; shift
  if try_render "$@"; then
    echo "  [PASS] ${label}: rendered"
    PASS=$((PASS+1))
  else
    echo "  [FAIL] ${label}: render failed:"
    sed 's/^/    /' "${ERR_FILE}"
    FAIL=$((FAIL+1))
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
echo "case 8 -- every internal route pins to the https listener; only the redirect route pins http"
FULL="$(helm template envoy-gateway-config "${CHART}" --namespace envoy-gateway-system \
  "${BASE_VALUES[@]}" --set appNamespace=dfe-local 2>/dev/null)"
HTTPS_PINS="$(printf '%s\n' "${FULL}" | grep -c 'sectionName: https$')"
HTTP_PINS="$(printf '%s\n' "${FULL}" | grep -c 'sectionName: http$')"
if [ "${HTTPS_PINS}" -eq 8 ]; then
  echo "  [PASS] sectionname-https: 8 internal routes pinned"
  PASS=$((PASS+1))
else
  echo "  [FAIL] sectionname-https: expected 8, got ${HTTPS_PINS}"
  FAIL=$((FAIL+1))
fi
if [ "${HTTP_PINS}" -eq 1 ]; then
  echo "  [PASS] sectionname-http: exactly one redirect route pinned"
  PASS=$((PASS+1))
else
  echo "  [FAIL] sectionname-http: expected 1, got ${HTTP_PINS}"
  FAIL=$((FAIL+1))
fi

echo ""
echo "case 9 -- internet-facing gateway with the kill switch on refuses with no OIDC and no CIDR fence"
assert_render_refused "internet-facing-guard" "envoyGateway.service.internetFacing" \
  --set envoyGateway.service.internetFacing=true --set exposure.infraUisExternal=true

echo ""
echo "case 10 -- the same combination renders once OIDC is enabled"
assert_render_succeeds "internet-facing-oidc" \
  --set envoyGateway.service.internetFacing=true --set exposure.infraUisExternal=true --set oidc.enabled=true

echo ""
echo "case 11 -- or once an allow-list fences the load balancer instead"
assert_render_succeeds "internet-facing-cidr" \
  --set envoyGateway.service.internetFacing=true --set exposure.infraUisExternal=true \
  --set ui.allowed_cidrs=203.0.113.0/24 --set ui.trusted_proxy_cidrs=203.0.113.0/24

echo ""
echo "case 12 -- internet-facing with the kill switch OFF (the edge-aws.yaml default) renders clean"
assert_render_succeeds "internet-facing-killswitch-off" \
  --set envoyGateway.service.internetFacing=true --set exposure.infraUisExternal=false

echo ""
echo "case 13 -- a ui.public.* value that survives as a string, not a bool, is refused by name"
assert_render_refused "ui-public-type-guard" "ui.public.kafbat" --set-string ui.public.kafbat=false

echo ""
echo "case 14 -- same guard covers ui.rate_limit.enabled and ui.tls.hsts"
assert_render_refused "ui-rate-limit-type-guard" "ui.rate_limit.enabled" --set-string ui.rate_limit.enabled=true
assert_render_refused "ui-tls-hsts-type-guard" "ui.tls.hsts" --set-string ui.tls.hsts=true

echo ""
echo "case 15 -- the AWS cascade's own defaults render clean: OIDC on, no admin UI on the public edge"
# Both files, in the order layer2-edge.yaml layers them: the edge module's tier
# table sits on top of the cloud overlay, and the kill switch is in the former
# while oidc.enabled stays in the latter.
R="$(helm template envoy-gateway-config "${CHART}" --namespace envoy-gateway-system \
  -f argocd/values/common.yaml -f argocd/values/aws.yaml -f argocd/values/edge-aws.yaml \
  --set appNamespace=dfe-local --set domain=dfe.example.com 2>/dev/null \
  | awk '/^kind: /{k=$2} /^  name: /{if (k=="HTTPRoute") print $2}')"
for r in argocd hyperdx forgejo kafbat links; do
  assert_absent "aws-overlay-default" "${r}" "${R}"
done
for r in dfe-ui dfe-engine otel; do
  assert_has "aws-overlay-default" "${r}" "${R}"
done

echo ""
echo "case 16 -- a route with no hostname refuses by name instead of publishing a leading-dot name"
assert_render_refused "missing-hostname" "routes.dfeUi has no hostname" \
  --set-string hostnames.dfe=""
assert_render_refused "missing-public-hostname" "routes.dfeUi has no hostname" \
  --set-string hostnames.dfe="" --set ui.public_domain=example.com --set ui.public.dfe_ui=true "${OIDC[@]}"

echo ""
echo "=== ${PASS} passed, ${FAIL} failed ==="
[ "${FAIL}" -eq 0 ] || exit 1
