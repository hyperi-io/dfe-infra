#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         smoke-test-hyperdx.sh
#  Purpose:      Verify the THREE DFE-specific HyperDX seams. dfe-hyperdx is a fork
#                and these seams exist nowhere upstream, so nothing upstream tests
#                them and a clean-merging upstream sync can break any of them:
#
#                  AUTH  -- HyperDX verifies the ENGINE's ES384 JWT itself (it is a
#                           Policy Enforcement Point, not a header-truster). Envoy
#                           already checked the token at the edge; if this second
#                           check regresses, the failure is silent and the blast
#                           radius is every tenant.
#                  DATA  -- HyperDX reads the DFE ClickHouse and returns rows.
#                           smoke-test-integration.sh proves rows LAND; this proves
#                           the query side of the same tables is reachable.
#                  EMBED -- HyperDX is iframed by dfe-ui, which needs a CSP
#                           frame-ancestors allowlist and NO X-Frame-Options.
#
#                Every check that cannot run SKIPS loudly with the reason. A skip is
#                not a pass: an untested seam must never read as a green one.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage: ./smoke-test-hyperdx.sh [kubeconfig]
#  Everything env-overridable; defaults follow the standard namespace layout.
set -uo pipefail

[ -n "${1:-}" ] && export KUBECONFIG="$1"

NS_HYPERDX="${DFE_HYPERDX_NS:-hyperdx}"
NS_APP="${DFE_NS:-${DFE_NAMESPACE:-dfe}}"
NS_CH="${DFE_CH_NS:-clickhouse}"

HYPERDX_DEPLOY="${DFE_HYPERDX_DEPLOY:-dfe-hyperdx}"
ENGINE_DEPLOY="${DFE_ENGINE_DEPLOY:-dfe-engine}"
HYPERDX_PORT="${DFE_HYPERDX_PORT:-8080}"
ENGINE_PORT="${DFE_ENGINE_PORT:-8000}"

# The otel db/table HyperDX reads back. Same SSoT defaults as the integration test.
OTEL_DB="${DFE_OTEL_DB:-dfe}"
OTEL_LOGS_TABLE="${DFE_OTEL_LOGS_TABLE:-otel_logs}"

PASS=0; FAIL=0; SKIP=0
check() {
  local name="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then echo "  [PASS] $name"; PASS=$((PASS+1)); else echo "  [FAIL] $name"; FAIL=$((FAIL+1)); fi
}
skip() { echo "  [SKIP] $1"; SKIP=$((SKIP+1)); }
note() { echo "         $1"; }

# curl OR wget: the fork's runtime image is slim and which one is present has
# changed between base-image bumps. Trying both stops a green seam reading as red
# for a purely cosmetic reason.
hdx_exec() {
  kubectl -n "$NS_HYPERDX" exec "deploy/${HYPERDX_DEPLOY}" -- sh -c "$1" 2>/dev/null
}

hdx_head() {
  local path="$1"
  hdx_exec "curl -fsSI http://localhost:${HYPERDX_PORT}${path} 2>/dev/null || wget -qS --spider http://localhost:${HYPERDX_PORT}${path} 2>&1"
}

echo "=== DFE HyperDX seam smoke test (auth / data / embed) ==="

if ! kubectl -n "$NS_HYPERDX" get deploy "$HYPERDX_DEPLOY" >/dev/null 2>&1; then
  echo ""
  skip "ALL HyperDX seams -- deploy/${HYPERDX_DEPLOY} not found in ns/${NS_HYPERDX}."
  note "HyperDX is the DEFAULT OTLP destination (telemetry.mode=hyperdx), so this is"
  note "a real gap in the deploy, not an optional extra. Nothing below was tested."
  echo ""
  echo "=== Results: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped ==="
  exit 1
fi

# ---------------------------------------------------------------------------
echo ""
echo "=== SEAM 1: auth (engine ES384 JWT verified BY HyperDX) ==="

# Which identity mode is the fork actually running? oidc-proxy = verify the engine
# JWT; header-dev = trust identity headers WITHOUT verification, which is a dev-only
# fallback and must never be what a deployed cluster is doing.
AUTH_MODE="$(kubectl -n "$NS_HYPERDX" get deploy "$HYPERDX_DEPLOY" \
  -o jsonpath='{.spec.template.spec.containers[*].env[?(@.name=="DFE_AUTH_MODE")].value}' 2>/dev/null)"

if [ -z "$AUTH_MODE" ]; then
  skip "DFE_AUTH_MODE unset -- the fork's DFE middleware is DISABLED and HyperDX is"
  note "running stock upstream auth. Nothing DFE-specific to verify."
elif [ "$AUTH_MODE" = "header-dev" ]; then
  echo "  [FAIL] DFE_AUTH_MODE=header-dev on a deployed cluster"
  FAIL=$((FAIL+1))
  note "header-dev trusts x-forwarded-email with NO signature check. It exists so"
  note "local dev works without a running engine. On a cluster it is an auth bypass."
else
  check "DFE_AUTH_MODE=oidc-proxy (engine JWT is verified, not trusted)" \
    "[ '$AUTH_MODE' = 'oidc-proxy' ]"

  JWKS_URL="$(kubectl -n "$NS_HYPERDX" get deploy "$HYPERDX_DEPLOY" \
    -o jsonpath='{.spec.template.spec.containers[*].env[?(@.name=="DFE_ENGINE_JWKS_URL")].value}' 2>/dev/null)"
  check "DFE_ENGINE_JWKS_URL configured" "[ -n '$JWKS_URL' ]"

  # The JWKS must be REACHABLE FROM the HyperDX pod, not merely configured. A URL
  # that resolves on the operator's laptop and not in-cluster is the classic way
  # this seam dies quietly: every token then fails verification and the middleware
  # falls through to the route guard, so users see 401s with no auth error logged.
  if [ -n "$JWKS_URL" ]; then
    check "HyperDX can fetch the engine JWKS (${JWKS_URL})" \
      "hdx_exec \"curl -fsS '${JWKS_URL}' || wget -qO- '${JWKS_URL}'\" | grep -q '\"keys\"'"
    check "engine JWKS advertises an ES384 key" \
      "hdx_exec \"curl -fsS '${JWKS_URL}' || wget -qO- '${JWKS_URL}'\" | grep -q 'ES384'"
  else
    skip "JWKS reachability -- no DFE_ENGINE_JWKS_URL to fetch"
  fi

  # End to end: mint a token AT the engine and present it TO HyperDX. This is the
  # only check that proves the two halves agree; the ones above prove config only.
  if kubectl -n "$NS_APP" get deploy "$ENGINE_DEPLOY" >/dev/null 2>&1; then
    TOKEN="$(kubectl -n "$NS_APP" exec "deploy/${ENGINE_DEPLOY}" -- sh -c \
      "curl -fsS -X POST http://localhost:${ENGINE_PORT}/api/v1/auth/token/smoke 2>/dev/null" 2>/dev/null \
      | sed -n 's/.*"\(access_token\|token\)"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\2/p')"
    if [ -n "$TOKEN" ]; then
      check "HyperDX accepts an engine-issued token (auth seam closed)" \
        "hdx_exec \"curl -fsS -H 'Authorization: Bearer ${TOKEN}' http://localhost:${HYPERDX_PORT}/api/v1/me\" | grep -q '\"'"
    else
      skip "engine-issued-token round trip -- the engine did not mint a smoke token."
      note "Config above is verified; the two halves AGREEING is not. Wire a service"
      note "token for the smoke user to close this."
    fi
  else
    skip "engine-issued-token round trip -- deploy/${ENGINE_DEPLOY} not in ns/${NS_APP}"
  fi
fi

# ---------------------------------------------------------------------------
echo ""
echo "=== SEAM 2: data (HyperDX queries the DFE ClickHouse) ==="

CH_HOST="$(kubectl -n "$NS_HYPERDX" get deploy "$HYPERDX_DEPLOY" \
  -o jsonpath='{.spec.template.spec.containers[*].env[?(@.name=="DEFAULT_CONNECTIONS")].value}' 2>/dev/null)"
[ -z "$CH_HOST" ] && CH_HOST="$(kubectl -n "$NS_HYPERDX" get deploy "$HYPERDX_DEPLOY" \
  -o jsonpath='{.spec.template.spec.containers[*].env[?(@.name=="CLICKHOUSE_ENDPOINT")].value}' 2>/dev/null)"

check "HyperDX has a ClickHouse connection configured" "[ -n '$CH_HOST' ]"

# Reaching ClickHouse FROM the HyperDX pod is the seam. Doing it from a CH pod (as
# the integration test does) proves ClickHouse works, not that HyperDX can get to it
# -- different NetworkPolicy, different service DNS, different credentials.
CH_URL="${DFE_HYPERDX_CH_URL:-http://dfe-clickhouse.${NS_CH}.svc.cluster.local:8123}"
check "HyperDX pod reaches ClickHouse over HTTP (${CH_URL})" \
  "hdx_exec \"curl -fsS '${CH_URL}/ping' || wget -qO- '${CH_URL}/ping'\" | grep -qi ok"

# The query side of the same tables the integration test writes to. Rows may be
# legitimately absent on a just-provisioned cluster, so assert the table RESOLVES;
# freshness is the integration test's job.
check "otel table ${OTEL_DB}.${OTEL_LOGS_TABLE} is queryable from HyperDX" \
  "hdx_exec \"curl -fsS '${CH_URL}/?query=SELECT+count()+FROM+${OTEL_DB}.${OTEL_LOGS_TABLE}' || wget -qO- '${CH_URL}/?query=SELECT+count()+FROM+${OTEL_DB}.${OTEL_LOGS_TABLE}'\" | grep -qE '^[0-9]+'"

# ---------------------------------------------------------------------------
echo ""
echo "=== SEAM 3: embed (dfe-ui iframes HyperDX) ==="

# next.config.mjs sets `Content-Security-Policy: frame-ancestors 'self' <extra>` on
# every route and deliberately does NOT set X-Frame-Options -- the latter is
# all-or-nothing and browsers that honour it block the embed whatever the CSP says.
HEADERS="$(hdx_head '/')"
if [ -z "$HEADERS" ]; then
  skip "embed headers -- could not read response headers from the HyperDX pod"
  note "(no curl and no wget in the image). The embed is UNVERIFIED, not proven."
else
  check "CSP frame-ancestors present (dfe-ui may frame HyperDX)" \
    "printf '%s' \"\$HEADERS\" | grep -qi 'content-security-policy.*frame-ancestors'"
  check "X-Frame-Options NOT set (it would defeat the allowlist)" \
    "! printf '%s' \"\$HEADERS\" | grep -qi 'x-frame-options'"
fi

FRAME_ANCESTORS="$(kubectl -n "$NS_HYPERDX" get deploy "$HYPERDX_DEPLOY" \
  -o jsonpath='{.spec.template.spec.containers[*].env[?(@.name=="DFE_EMBED_FRAME_ANCESTORS")].value}' 2>/dev/null)"
if [ -n "$FRAME_ANCESTORS" ]; then
  echo "  [PASS] DFE_EMBED_FRAME_ANCESTORS set (${FRAME_ANCESTORS})"
  PASS=$((PASS+1))
else
  skip "DFE_EMBED_FRAME_ANCESTORS unset -- only same-origin framing is allowed."
  note "Correct when dfe-ui and HyperDX share an origin behind the gateway; a"
  note "cross-origin dfe-ui WILL be refused until this names its origin."
fi

# The full browser-level proof (chromeless iframe, dfe-ui owns the nav, live theme
# sync) needs Playwright and a reachable UI, so it is opt-in rather than assumed.
if [ "${DFE_EMBED_BROWSER_TEST:-0}" = "1" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  echo ""
  echo "--- browser embed proof (DFE_EMBED_BROWSER_TEST=1) ---"
  if python3 "${SCRIPT_DIR}/../scripts/verify_embed.py" \
       --dfeui "${DFE_UI_URL:-http://localhost:3000}" \
       --hyperdx "${DFE_HYPERDX_URL:-http://localhost:8080}"; then
    echo "  [PASS] browser embed proof"; PASS=$((PASS+1))
  else
    echo "  [FAIL] browser embed proof"; FAIL=$((FAIL+1))
  fi
else
  skip "browser embed proof -- set DFE_EMBED_BROWSER_TEST=1 (needs Playwright + a"
  note "reachable dfe-ui). Header checks above do NOT prove the iframe renders."
fi

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped ==="

if (( FAIL > 0 )); then
  echo "HyperDX seams are NOT healthy."
  exit 1
fi
echo "HyperDX seams are healthy (${SKIP} untested -- see SKIP lines above)."
