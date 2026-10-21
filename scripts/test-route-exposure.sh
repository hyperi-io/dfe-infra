#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         scripts/test-route-exposure.sh
#  Purpose:      Assert the envoy-gateway-config route exposure cascade -- class
#                default, per-route opt-out, and the exposure group switch that
#                beats both -- plus which routes take the edge RBAC policy, the
#                engine's browser/CLI split and the retired-key refusals.
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
# The collector's bearer-token receiver on, which is what the otel route fronts.
OTEL_ON=(--set otel.ingress.enabled=true --set otel.ingress.auth.remoteKey=dfe/local/otel/ingress)

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
echo "case 1 -- all defaults: every UI with a login renders and so does the on-prem receiver door; links, forgejo and otel stay off"
R="$(render_names HTTPRoute)"
# local-dfe.yaml sets exposure.groups.ingest.door: gateway, the on-prem door.
for r in dfe-ui dfe-engine argocd hyperdx kafbat receiver; do
  assert_has "default" "${r}" "${R}"
done
# links has no login of its own and edge OIDC is off; Forgejo is not deployed;
# otel waits for otel.ingress.enabled.
for r in links forgejo otel; do
  assert_absent "default" "${r}" "${R}"
done

echo ""
echo "case 1b -- the receiver route follows the ingest door, and the ingest group off takes both ingest routes"
for door in loadbalancer tunnel; do
  assert_absent "ingest-door-${door}" "receiver" \
    "$(render_names HTTPRoute --set "exposure.groups.ingest.door=${door}" --set routes.receiver.enabled=true)"
done
R="$(render_names HTTPRoute --set exposure.groups.ingest.enabled=false "${OTEL_ON[@]}")"
for r in receiver otel; do
  assert_absent "ingest-off" "${r}" "${R}"
done
assert_has "ingest-off" "dfe-ui" "${R}"
R="$(render_names HTTPRoute "${OIDC[@]}" --set deployRepo.bundled=true)"
for r in links forgejo; do
  assert_has "default-with-login-and-forgejo" "${r}" "${R}"
done

echo ""
echo "case 2 -- admin group off: infra class gone, product (HyperDX included), api and ingest stay"
R="$(render_names HTTPRoute --set exposure.groups.admin.enabled=false "${OTEL_ON[@]}")"
for r in argocd forgejo kafbat links; do
  assert_absent "admin-off" "${r}" "${R}"
done
for r in dfe-ui dfe-engine dfe-engine-cli hyperdx otel; do
  assert_has "admin-off" "${r}" "${R}"
done

echo ""
echo "case 3 -- a group switched off beats a per-route enabled:true"
R="$(render_names HTTPRoute --set exposure.groups.admin.enabled=false --set routes.kafbat.enabled=true)"
assert_absent "admin-off-absolute" "kafbat" "${R}"
R="$(render_names HTTPRoute --set exposure.groups.console.enabled=false --set routes.dfeUi.enabled=true)"
assert_absent "console-off-absolute" "dfe-ui" "${R}"

echo ""
echo "case 3b -- the console and api groups each take their own routes and nothing else"
R="$(render_names HTTPRoute --set exposure.groups.console.enabled=false "${OTEL_ON[@]}")"
for r in dfe-ui dfe-engine hyperdx; do
  assert_absent "console-off" "${r}" "${R}"
done
for r in dfe-engine-cli argocd otel; do
  assert_has "console-off" "${r}" "${R}"
done
R="$(render_names HTTPRoute --set exposure.groups.api.enabled=false)"
assert_absent "api-off" "dfe-engine-cli" "${R}"
for r in dfe-ui dfe-engine hyperdx argocd; do
  assert_has "api-off" "${r}" "${R}"
done

echo ""
echo "case 4 -- per-route opt-out removes only that route"
R="$(render_names HTTPRoute --set routes.kafbat.enabled=false)"
assert_absent "per-route" "kafbat" "${R}"
for r in argocd hyperdx dfe-ui; do
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
R="$(render_names SecurityPolicy "${OIDC[@]}" --set deployRepo.bundled=true)"
# The name carries no provider: there is ONE policy per route, because two
# SecurityPolicies naming the same HTTPRoute silently leave only the oldest live.
for p in dfe-oidc-argocd-admin dfe-oidc-forgejo-admin dfe-oidc-links-admin; do
  assert_has "edge-rbac" "${p}" "${R}"
done
# Exempt by design: kafbat runs its own OIDC, hyperdx is framed inside dfe-ui.
for p in dfe-oidc-kafbat-admin dfe-oidc-hyperdx-admin; do
  assert_absent "edge-rbac" "${p}" "${R}"
done

echo ""
echo "case 7 -- the admin group off also withdraws the infra edge policies"
R="$(render_names SecurityPolicy "${OIDC[@]}" --set deployRepo.bundled=true --set exposure.groups.admin.enabled=false)"
for p in dfe-oidc-argocd-admin dfe-oidc-forgejo-admin dfe-oidc-links-admin; do
  assert_absent "admin-off-policy" "${p}" "${R}"
done

echo ""
echo "case 8 -- every internal route pins to the https listener; only the redirect route pins http"
# The edge login, the bundled deploy repo and OTLP ingress, so every internal route renders.
FULL="$(helm template envoy-gateway-config "${CHART}" --namespace envoy-gateway-system \
  "${BASE_VALUES[@]}" --set appNamespace=dfe-local "${OIDC[@]}" --set deployRepo.bundled=true \
  "${OTEL_ON[@]}" 2>/dev/null)"
HTTPS_PINS="$(printf '%s\n' "${FULL}" | grep -c 'sectionName: https$')"
HTTP_PINS="$(printf '%s\n' "${FULL}" | grep -c 'sectionName: http$')"
# Ten: dfe-ui, both engine routes, argocd, hyperdx, kafbat, forgejo, links, otel, receiver.
if [ "${HTTPS_PINS}" -eq 10 ]; then
  echo "  [PASS] sectionname-https: 10 internal routes pinned"
  PASS=$((PASS+1))
else
  echo "  [FAIL] sectionname-https: expected 10, got ${HTTPS_PINS}"
  FAIL=$((FAIL+1))
fi
if [ "${HTTP_PINS}" -eq 1 ]; then
  echo "  [PASS] sectionname-http: exactly one redirect route pinned"
  PASS=$((PASS+1))
else
  echo "  [FAIL] sectionname-http: expected 1, got ${HTTP_PINS}"
  FAIL=$((FAIL+1))
fi

# Internet-facing on the cloud ingest door, as common.yaml sets it; the gateway
# door there needs the receiver's caller auth (case 12b).
FACING=(--set envoyGateway.service.internetFacing=true --set exposure.groups.ingest.door=loadbalancer)
ADMIN_LIST=(--set-json 'exposure.groups.admin.allow=["203.0.113.0/24"]')

echo ""
echo "case 9 -- internet-facing with the admin group on and an empty admin list refuses, OIDC or not"
assert_render_refused "admin-empty-list" "empty exposure.groups.admin.allow" "${FACING[@]}"
assert_render_refused "admin-empty-list-oidc" "empty exposure.groups.admin.allow" "${FACING[@]}" "${OIDC[@]}"

echo ""
echo "case 10 -- allowPublic opens the empty list only behind an OIDC provider, not on the switch alone"
assert_render_refused "admin-public-no-oidc" "no edge OIDC provider" \
  "${FACING[@]}" --set exposure.groups.admin.allowPublic=true
assert_render_refused "admin-public-oidc-no-provider" "no edge OIDC provider" \
  "${FACING[@]}" --set exposure.groups.admin.allowPublic=true --set oidc.enabled=true
assert_render_succeeds "admin-public-oidc" \
  "${FACING[@]}" --set exposure.groups.admin.allowPublic=true "${OIDC[@]}"

echo ""
echo "case 11 -- or once the admin group names who may reach it"
assert_render_succeeds "admin-list" "${FACING[@]}" "${ADMIN_LIST[@]}" --set ui.trusted_proxy_cidrs=203.0.113.0/24

echo ""
echo "case 12 -- internet-facing with the admin group OFF (the edge-aws.yaml default) renders clean"
assert_render_succeeds "internet-facing-admin-off" "${FACING[@]}" --set exposure.groups.admin.enabled=false

echo ""
echo "case 12b -- the gateway ingest door on an internet-facing Service needs the receiver's caller auth"
assert_render_refused "ingest-gateway-door-no-auth" "exposure.groups.ingest.door is gateway on an internet-facing Service" \
  --set envoyGateway.service.internetFacing=true --set exposure.groups.admin.enabled=false
assert_render_succeeds "ingest-gateway-door-bearer" \
  --set envoyGateway.service.internetFacing=true --set exposure.groups.admin.enabled=false \
  --set routes.receiver.authMode=bearer

echo ""
echo "case 13 -- a ui.public.* or admin tool value that survives as a string, not a bool, is refused by name"
assert_render_refused "ui-public-type-guard" "ui.public.hyperdx" --set-string ui.public.hyperdx=false
assert_render_refused "admin-tool-type-guard" "exposure.groups.admin.tools.kafbat" \
  --set-string exposure.groups.admin.tools.kafbat=false
assert_render_refused "group-switch-type-guard" "exposure.groups.console.enabled" \
  --set-string exposure.groups.console.enabled=true
assert_render_refused "group-list-type-guard" "never a comma-separated string" \
  --set-string exposure.groups.console.allow=203.0.113.0/24
assert_render_refused "unknown-admin-tool" "exposure.groups.admin.tools.hyperdx names no admin route" \
  --set exposure.groups.admin.tools.hyperdx=true
assert_render_refused "unknown-group" "exposure.groups.public is not an exposure group" \
  --set exposure.groups.public.enabled=true

echo ""
echo "case 13b -- every retired key refuses to render and names its replacement"
assert_render_refused "retired-allowed-cidrs" "ui.allowed_cidrs is retired" --set ui.allowed_cidrs=203.0.113.0/24
assert_render_refused "retired-allowed-cidrs-empty" "exposure.groups.console.allow" --set-string ui.allowed_cidrs=""
assert_render_refused "retired-kill-switch" "exposure.infraUisExternal is retired" \
  --set exposure.infraUisExternal=false
for name in kafbat argocd links forgejo; do
  assert_render_refused "retired-ui-public-${name}" "exposure.groups.admin.tools.${name}" \
    --set "ui.public.${name}=false"
done
assert_render_refused "retired-ui-public-cruise-control" "exposure.groups.admin.tools.cruiseControl" \
  --set ui.public.cruise_control=true

echo ""
echo "case 14 -- same guard covers ui.rate_limit.enabled and ui.tls.hsts"
assert_render_refused "ui-rate-limit-type-guard" "ui.rate_limit.enabled" --set-string ui.rate_limit.enabled=true
assert_render_refused "ui-tls-hsts-type-guard" "ui.tls.hsts" --set-string ui.tls.hsts=true

echo ""
echo "case 15 -- the AWS cascade's own defaults render clean: OIDC on, no admin UI on the public edge, HyperDX beside the console"
# Both files, in the order layer2-edge.yaml layers them: the edge module's tier
# table sits on top of the cloud overlay, and the admin group switch is in the
# former while oidc.enabled stays in the latter.
R="$(helm template envoy-gateway-config "${CHART}" --namespace envoy-gateway-system \
  -f argocd/values/common.yaml -f argocd/values/aws.yaml -f argocd/values/edge-aws.yaml \
  --set appNamespace=dfe-local --set domain=dfe.example.com 2>/dev/null \
  | awk '/^kind: /{k=$2} /^  name: /{if (k=="HTTPRoute") print $2}')"
for r in argocd forgejo kafbat links otel; do
  assert_absent "aws-overlay-default" "${r}" "${R}"
done
# HyperDX is class product, so the console's Observe frame has a route to load;
# the api group is on and closed until it names a list.
for r in dfe-ui dfe-engine dfe-engine-cli hyperdx; do
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

# The engine team's answer (hyperi-io/dfe-engine#395), as the chart carries it,
# plus the two families neither list named: /api/v1/deployment, which dfe-ui's
# Admin Tools page reads, and /api/v1/kafka/topics, a CLI family.
BROWSER_PREFIXES=(
  /api/v1/auth /api/v1/config/client /api/v1/deployment /api/v1/system /api/v1/orgs
  /api/v1/sources /api/v1/services /api/v1/field-maps
  /api/v1/rules /api/v1/alerts /api/v1/transforms /api/v1/schemas /api/v1/hunts
  /api/v1/governance /api/v1/helm /api/v1/apps /api/v1/backing-services
  /api/v1/library /api/v1/gitops /api/v1/lifecycle /api/v1/repository
)
CLI_PREFIXES=(
  /api/v1/queries /api/v1/sigma /api/v1/cel /api/v1/pipeline
  /api/v1/authoring /api/v1/app-contracts /api/v1/tasks /api/v1/synthetic-data
  /api/v1/sample /api/v1/samples /api/v1/hyperdx /api/v1/kafka/topics /openapi.json
)
# Never on a public route, whatever the two opt-ins say.
NEVER_PUBLIC=(/api/e2e /docs /redoc /livez /readyz)
SCIM_ON=(--set exposure.groups.api.scim=true)

echo ""
echo "case 17 -- the browser's families answer on the product hostname, and only those"
P="$(route_paths dfe-engine-public)"
for p in "${BROWSER_PREFIXES[@]}"; do
  assert_has "engine-browser" "${p}" "${P}"
done
assert_has "engine-browser" "/.well-known" "${P}"
# 21 families plus /.well-known, and nothing that was not asked for.
assert_count "engine-browser-total" 22 "$(printf '%s\n' "${P}" | grep -c .)"
for p in "${CLI_PREFIXES[@]}" "${NEVER_PUBLIC[@]}"; do
  assert_absent "engine-browser" "${p}" "${P}"
done
assert_absent "engine-cli-public-default" "dfe-engine-cli-public" "$(aws_names HTTPRoute)"

echo ""
echo "case 18 -- cli_families_public puts the api group's route, and only that, on the product hostname"
P="$(route_paths dfe-engine-cli-public --set ui.engine_api.cli_families_public=true)"
for p in "${CLI_PREFIXES[@]}"; do
  assert_has "engine-cli" "${p}" "${P}"
done
# 12 families plus the spec: 35 across the two routes, the 22 browser ones unchanged.
assert_count "engine-cli-total" 13 "$(printf '%s\n' "${P}" | grep -c .)"
for p in "${BROWSER_PREFIXES[@]}" /.well-known /api/v1/scim/v2; do
  assert_absent "engine-cli" "${p}" "${P}"
done
P="$(route_paths dfe-engine-public --set ui.engine_api.cli_families_public=true)"
assert_count "engine-browser-unmoved" 22 "$(printf '%s\n' "${P}" | grep -c .)"
for p in "${CLI_PREFIXES[@]}"; do
  assert_absent "engine-browser-unmoved" "${p}" "${P}"
done

echo ""
echo "case 19 -- scim_public needs the api group's scim, and then adds that prefix alone"
P="$(route_paths dfe-engine-cli-public --set ui.engine_api.scim_public=true "${SCIM_ON[@]}")"
assert_has "engine-scim" "/api/v1/scim/v2" "${P}"
assert_count "engine-scim-total" 1 "$(printf '%s\n' "${P}" | grep -c .)"
assert_absent "engine-scim" "/api/v1/queries" "${P}"
assert_render_refused "engine-scim-without-api-scim" "exposure.groups.api.scim is false" \
  --set ui.engine_api.scim_public=true
assert_render_refused "engine-public-cli-with-api-off" "exposure.groups.api.enabled is false" \
  --set ui.engine_api.cli_families_public=true --set exposure.groups.api.enabled=false

echo ""
echo "case 20 -- the never-public paths stay off both public routes under every combination"
for flags in "" "--set ui.engine_api.cli_families_public=true" \
             "--set ui.engine_api.scim_public=true --set exposure.groups.api.scim=true" \
             "--set ui.engine_api.cli_families_public=true --set ui.engine_api.scim_public=true --set exposure.groups.api.scim=true"; do
  for route in dfe-engine-public dfe-engine-cli-public; do
    # The flags are this script's own and are deliberately word-split.
    # shellcheck disable=SC2086
    P="$(route_paths "${route}" ${flags})"
    for p in "${NEVER_PUBLIC[@]}"; do
      assert_absent "engine-never ${route}${flags:+ (${flags})}" "${p}" "${P}"
    done
  done
done

echo ""
echo "case 20b -- the internal names split the same way: no CLI path on the browser route, no browser path on the CLI route"
P="$(route_paths dfe-engine)"
assert_count "engine-internal-browser-total" 22 "$(printf '%s\n' "${P}" | grep -c .)"
for p in "${CLI_PREFIXES[@]}" /api/v1 /api/v1/scim/v2 /docs /redoc; do
  assert_absent "engine-internal-browser" "${p}" "${P}"
done
P="$(route_paths dfe-engine-cli)"
for p in "${CLI_PREFIXES[@]}" /docs /redoc; do
  assert_has "engine-internal-cli" "${p}" "${P}"
done
for p in "${BROWSER_PREFIXES[@]}" /.well-known /api/v1 /api/v1/scim/v2; do
  assert_absent "engine-internal-cli" "${p}" "${P}"
done
assert_has "engine-internal-cli-scim" "/api/v1/scim/v2" "$(route_paths dfe-engine-cli "${SCIM_ON[@]}")"

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
echo "case 22 -- the CIDR fence, the rate limit and the edge login all cover the public engine routes"
assert_has "engine-ratelimit" "dfe-public-dfe-engine-ratelimit" "$(aws_names BackendTrafficPolicy)"
CIDR=(--set-json 'exposure.groups.console.allow=["203.0.113.0/24"]' --set ui.trusted_proxy_cidrs=203.0.113.0/24)
assert_has "engine-cidr" "dfe-public-cidr-dfe-engine" "$(aws_names SecurityPolicy "${CIDR[@]}")"
assert_has "engine-oidc" "dfe-public-oidc-acme-dfe-engine" \
  "$(aws_names SecurityPolicy "${OIDC[@]}")"
CLI_PUBLIC=(--set ui.engine_api.cli_families_public=true)
assert_has "engine-cli-cidr" "dfe-public-cidr-dfe-engine-cli" "$(aws_names SecurityPolicy "${CLI_PUBLIC[@]}")"
assert_has "engine-cli-ratelimit" "dfe-public-dfe-engine-cli-ratelimit" \
  "$(aws_names BackendTrafficPolicy "${CLI_PUBLIC[@]}")"
# The CLI cannot follow an interactive redirect, so the engine's edge login
# fronts the browser route alone.
assert_absent "engine-cli-no-oidc" "dfe-public-oidc-acme-dfe-engine-cli" \
  "$(aws_names SecurityPolicy "${OIDC[@]}" "${CLI_PUBLIC[@]}")"

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
echo "case 24 -- the public flags leave both internal engine routes exactly as they were"
# Byte-for-byte, because the internal CLI route carries /openapi.json, and on
# this render's dev posture /docs and /redoc, and a deployment's own tooling
# reaches them on the private name.
INTERNAL_ENGINE="$(aws_render --show-only templates/httproute-dfe-engine.yaml "${SCIM_ON[@]}")"
assert_count "internal-engine-routes" 2 "$(printf '%s\n' "${INTERNAL_ENGINE}" | grep -c '^kind: HTTPRoute')"
assert_same "internal-engine-route" "${INTERNAL_ENGINE}" \
  "$(aws_render --show-only templates/httproute-dfe-engine.yaml "${SCIM_ON[@]}" \
     --set ui.engine_api.cli_families_public=true --set ui.engine_api.scim_public=true)"

echo ""
echo "=== OTLP ingest from outside the cluster ==="

echo ""
echo "case 25 -- the otel route renders only with otel.ingress.enabled, and never without a token"
assert_has "otel-ingress-on" "otel" "$(render_names HTTPRoute "${OTEL_ON[@]}")"
assert_has "otel-ingress-on-aws" "otel" "$(aws_names HTTPRoute "${OTEL_ON[@]}")"
assert_absent "otel-ingress-route-opt-out" "otel" \
  "$(render_names HTTPRoute "${OTEL_ON[@]}" --set routes.otel.enabled=false)"
assert_render_refused "otel-ingress-no-token" "otel.ingress.auth.remoteKey is empty" \
  --set otel.ingress.enabled=true
assert_render_refused "otel-ingress-quoted-switch" "otel.ingress.enabled is false" \
  --set-string otel.ingress.enabled=false

echo ""
echo "=== ${PASS} passed, ${FAIL} failed ==="
[ "${FAIL}" -eq 0 ] || exit 1
