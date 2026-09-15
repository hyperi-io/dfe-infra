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

# -F throughout: a needle is a literal name or a literal path, and a path
# carries a dot that would otherwise be a regex wildcard.
assert_has() {
  local label="$1" needle="$2" haystack="$3"
  if printf '%s\n' "${haystack}" | grep -qxF -- "${needle}"; then
    echo "  [PASS] ${label}: ${needle} present"
    PASS=$((PASS+1))
  else
    echo "  [FAIL] ${label}: ${needle} MISSING"
    FAIL=$((FAIL+1))
  fi
}

assert_absent() {
  local label="$1" needle="$2" haystack="$3"
  if printf '%s\n' "${haystack}" | grep -qxF -- "${needle}"; then
    echo "  [FAIL] ${label}: ${needle} STILL PRESENT"
    FAIL=$((FAIL+1))
  else
    echo "  [PASS] ${label}: ${needle} absent"
    PASS=$((PASS+1))
  fi
}

assert_count() {
  local label="$1" want="$2" got="$3"
  if [ "${got}" -eq "${want}" ]; then
    echo "  [PASS] ${label}: ${got}"
    PASS=$((PASS+1))
  else
    echo "  [FAIL] ${label}: expected ${want}, got ${got}"
    FAIL=$((FAIL+1))
  fi
}

assert_same() {
  local label="$1" want="$2" got="$3"
  if [ "${want}" = "${got}" ]; then
    echo "  [PASS] ${label}: unchanged"
    PASS=$((PASS+1))
  else
    echo "  [FAIL] ${label}: differs"
    diff <(printf '%s\n' "${want}") <(printf '%s\n' "${got}") | sed 's/^/    /'
    FAIL=$((FAIL+1))
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
echo "=== the engine API on the product's public hostname ==="

# The cascade a cloud deploy actually runs, with a public zone named. dfe-ui is
# public here, so the engine route is too.
AWS_VALUES=(-f argocd/values/common.yaml -f argocd/values/aws.yaml -f argocd/values/edge-aws.yaml)
AWS_SET=(--set appNamespace=dfe-local --set domain=dfe.example.com --set ui.public_domain=example.com)

aws_render() {
  helm template envoy-gateway-config "${CHART}" --namespace envoy-gateway-system \
    "${AWS_VALUES[@]}" "${AWS_SET[@]}" "$@" 2>/dev/null
}

aws_names() {
  local kind="$1"; shift
  aws_render "$@" | awk -v kind="${kind}" '
      /^kind: /   { k = $2 }
      /^  name: / { if (k == kind) print $2 }'
}

# Every PathPrefix one public HTTPRoute matches. The HSTS filter carries a
# `value:` of its own, so only a value that is a path is read.
route_paths() {
  local route="$1"; shift
  aws_render "$@" | awk -v want="${route}" '
      /^---/         { kind = ""; name = "" }
      /^kind: /      { kind = $2 }
      /^  name: /    { if (name == "") name = $2 }
      $1 == "value:" { if (kind == "HTTPRoute" && name == want && $2 ~ /^\//) print $2 }'
}

# The engine team's answer (hyperi-io/dfe-engine#395), as the chart carries it.
BROWSER_PREFIXES=(
  /api/v1/auth /api/v1/config/client /api/v1/system /api/v1/orgs /api/v1/sources
  /api/v1/services /api/v1/service-surfaces /api/v1/deployments /api/v1/field-maps
  /api/v1/rules /api/v1/alerts /api/v1/transforms /api/v1/schemas /api/v1/hunts
  /api/v1/governance /api/v1/helm /api/v1/apps /api/v1/backing-services
  /api/v1/library /api/v1/gitops /api/v1/lifecycle /api/v1/repository
)
CLI_PREFIXES=(
  /api/v1/queries /api/v1/discovery /api/v1/sigma /api/v1/cel /api/v1/pipeline
  /api/v1/authoring /api/v1/app-contracts /api/v1/tasks /api/v1/synthetic-data
  /api/v1/sample /api/v1/samples /api/v1/hyperdx /openapi.json
)
# Never on the public route, whatever the two opt-ins say.
NEVER_PUBLIC=(/api/e2e /docs /redoc /livez /readyz)

echo ""
echo "case 17 -- the browser's families answer on the product hostname, and only those"
P="$(route_paths dfe-engine-public)"
for p in "${BROWSER_PREFIXES[@]}"; do
  assert_has "engine-browser" "${p}" "${P}"
done
assert_has "engine-browser" "/.well-known" "${P}"
# 22 families plus /.well-known, and nothing that was not asked for.
assert_count "engine-browser-total" 23 "$(printf '%s\n' "${P}" | grep -c .)"
for p in "${CLI_PREFIXES[@]}" "${NEVER_PUBLIC[@]}"; do
  assert_absent "engine-browser" "${p}" "${P}"
done

echo ""
echo "case 18 -- cli_families_public adds the twelve CLI families and the spec they are generated from"
P="$(route_paths dfe-engine-public --set ui.engine_api.cli_families_public=true)"
for p in "${CLI_PREFIXES[@]}"; do
  assert_has "engine-cli" "${p}" "${P}"
done
assert_count "engine-cli-total" 36 "$(printf '%s\n' "${P}" | grep -c .)"
assert_absent "engine-cli" "/api/v1/scim/v2" "${P}"

echo ""
echo "case 19 -- scim_public is its own opt-in and adds that prefix alone"
P="$(route_paths dfe-engine-public --set ui.engine_api.scim_public=true)"
assert_has "engine-scim" "/api/v1/scim/v2" "${P}"
assert_count "engine-scim-total" 24 "$(printf '%s\n' "${P}" | grep -c .)"
assert_absent "engine-scim" "/api/v1/queries" "${P}"

echo ""
echo "case 20 -- the never-public paths stay off the route under every combination"
for flags in "" "--set ui.engine_api.cli_families_public=true" \
             "--set ui.engine_api.scim_public=true" \
             "--set ui.engine_api.cli_families_public=true --set ui.engine_api.scim_public=true"; do
  # shellcheck disable=SC2086 -- the flags are this script's own, deliberately split
  P="$(route_paths dfe-engine-public ${flags})"
  for p in "${NEVER_PUBLIC[@]}"; do
    assert_absent "engine-never${flags:+ (${flags})}" "${p}" "${P}"
  done
done

echo ""
echo "case 21 -- the engine rides dfe-ui's listener, so no listener and no certificate are added"
assert_count "engine-public-listeners" 1 \
  "$(aws_render | grep -c '^    - name: https-public-')"
assert_has "engine-listener" "https-public-dfe-ui" \
  "$(aws_render | awk '/^    - name: https-public-/ { print $3 }')"
assert_absent "engine-listener" "https-public-dfe-engine" \
  "$(aws_render | awk '/^    - name: https-public-/ { print $3 }')"
# Three, the same three as before: the internal root, the internal wildcard and
# one publicly trusted leaf for dfe-ui's hostname.
assert_count "engine-certificates" 3 "$(aws_names Certificate | grep -c .)"
assert_has "engine-certificate" "dfe-public-dfe-ui-tls" "$(aws_names Certificate)"
assert_absent "engine-certificate" "dfe-public-dfe-engine-tls" "$(aws_names Certificate)"
assert_has "engine-route-pin" "https-public-dfe-ui" \
  "$(aws_render --show-only templates/httproute-public.yaml | awk '/sectionName:/ { print $2 }')"

echo ""
echo "case 22 -- the CIDR fence, the rate limit and the edge login all cover the public engine route"
assert_has "engine-ratelimit" "dfe-public-dfe-engine-ratelimit" "$(aws_names BackendTrafficPolicy)"
CIDR=(--set ui.allowed_cidrs=203.0.113.0/24 --set ui.trusted_proxy_cidrs=203.0.113.0/24)
assert_has "engine-cidr" "dfe-public-cidr-dfe-engine" "$(aws_names SecurityPolicy "${CIDR[@]}")"
assert_has "engine-oidc" "dfe-public-oidc-acme-dfe-engine" \
  "$(aws_names SecurityPolicy "${OIDC[@]}")"

echo ""
echo "case 23 -- either switch off the product surface or off the engine, and the public route goes"
assert_absent "engine-with-product-off" "dfe-engine-public" \
  "$(aws_names HTTPRoute --set ui.engine_api.with_product=false)"
assert_has "engine-with-product-off" "dfe-ui-public" \
  "$(aws_names HTTPRoute --set ui.engine_api.with_product=false)"
assert_absent "engine-ui-private" "dfe-engine-public" \
  "$(aws_names HTTPRoute --set ui.public.dfe_ui=false)"
assert_absent "engine-ui-private" "dfe-ui-public" \
  "$(aws_names HTTPRoute --set ui.public.dfe_ui=false)"

echo ""
echo "case 24 -- the internal engine route is the same object it was before the public one existed"
# Byte-for-byte, because the internal route carries /docs, /redoc and
# /openapi.json and a deployment's own tooling reaches them on the private name.
read -r -d '' INTERNAL_ENGINE <<'EOF'
---
# Source: envoy-gateway-config/templates/httproute-dfe-engine.yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: dfe-engine
  namespace: dfe-local
  labels:
    app.kubernetes.io/name: "dfe-gateway"
    app.kubernetes.io/instance: "envoy-gateway-config"
    app.kubernetes.io/part-of: "dfe"
    app.kubernetes.io/managed-by: "helm"
    app.kubernetes.io/version: "1.0.0"
    dfe.hyperi.io/env: "local"
    dfe.hyperi.io/cloud: "local"
  annotations:
    dfe.hyperi.io/publish-dns: "true"
spec:
  parentRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: dfe-gateway
      namespace: envoy-gateway-system
      # See httproute-argocd.yaml: pinned off the plaintext :80 listener.
      sectionName: https
  hostnames:
    - "dfe.dfe.example.com"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /api/v1
        - path:
            type: PathPrefix
            value: /docs
        - path:
            type: PathPrefix
            value: /redoc
        - path:
            type: PathPrefix
            value: /openapi.json
        - path:
            type: PathPrefix
            value: /.well-known
      backendRefs:
        - name: dfe-engine
          port: 8000
          group: ""
          kind: Service
          weight: 1
EOF
assert_same "internal-engine-route" "${INTERNAL_ENGINE}" \
  "$(aws_render --show-only templates/httproute-dfe-engine.yaml \
     --set ui.engine_api.cli_families_public=true --set ui.engine_api.scim_public=true)"

echo ""
echo "=== ${PASS} passed, ${FAIL} failed ==="
[ "${FAIL}" -eq 0 ] || exit 1
