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

NS_APP="${DFE_NS:-${DFE_NAMESPACE:-dfe}}"
# HyperDX ships as an app, so it lands in the app namespace; the bare `hyperdx`
# namespace bootstrap creates is empty legacy debris.
NS_HYPERDX="${DFE_HYPERDX_NS:-$NS_APP}"
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
  # pipefail is off for the check itself: a check ending in grep -q closes the
  # pipe on the first match, and the producer's SIGPIPE would fail a passing check.
  if ( set +o pipefail; eval "$cmd" ) >/dev/null 2>&1; then echo "  [PASS] $name"; PASS=$((PASS+1)); else echo "  [FAIL] $name"; FAIL=$((FAIL+1)); fi
}
skip() { echo "  [SKIP] $1"; SKIP=$((SKIP+1)); }
note() { echo "         $1"; }

# The fork's runtime image ships node and neither curl nor wget, so the probes run
# in the pod's node with values passed as env vars, never spliced into the JS.
# A probe prints nothing on any failure, so an error is never read as a response.
IFS= read -r -d '' JS_PROBE <<'JS' || true
const e = process.env;
const headers = {};
if (e.PROBE_CH_AUTH) {
  headers["X-ClickHouse-User"] = e.CLICKHOUSE_USER;
  headers["X-ClickHouse-Key"] = e.CLICKHOUSE_PASSWORD;
}
if (e.PROBE_BEARER) headers.Authorization = "Bearer " + e.PROBE_BEARER;
fetch(e.PROBE_URL, {
  method: e.PROBE_METHOD,
  headers,
  redirect: "manual",
  signal: AbortSignal.timeout(10000),
})
  .then(async (r) => {
    if (r.status >= 400) {
      process.exitCode = 1;
      return;
    }
    if (e.PROBE_METHOD === "HEAD") {
      let out = "HTTP " + r.status + "\n";
      for (const [k, v] of r.headers) out += k + ": " + v + "\n";
      process.stdout.write(out);
    } else {
      process.stdout.write(await r.text());
    }
  })
  .catch(() => {
    process.exitCode = 1;
  });
JS

# hdx_probe METHOD URL [VAR=value ...]: a GET prints the body and a HEAD the status
# line and headers; PROBE_CH_AUTH=1 sends the pod's own ClickHouse credentials and
# PROBE_BEARER=<jwt> a bearer token.
hdx_probe() {
  local method="$1" url="$2"
  shift 2
  kubectl -n "$NS_HYPERDX" exec "deploy/${HYPERDX_DEPLOY}" -c hyperdx -- \
    env "PROBE_METHOD=${method}" "PROBE_URL=${url}" "$@" node -e "$JS_PROBE" 2>/dev/null
}

# 127.0.0.1, never localhost: the frontend binds IPv4 only while the API binds
# IPv6 too, so localhost resolves to ::1 and the frontend probe is refused.
hdx_head() {
  hdx_probe HEAD "http://127.0.0.1:${HYPERDX_PORT}$1"
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
    # shellcheck disable=SC2034  # read inside the checks' eval.
    JWKS_BODY="$(hdx_probe GET "$JWKS_URL")"
    check "HyperDX can fetch the engine JWKS (${JWKS_URL})" \
      "printf '%s' \"\$JWKS_BODY\" | grep -q '\"keys\"'"
    check "engine JWKS advertises an ES384 key" \
      "printf '%s' \"\$JWKS_BODY\" | grep -q 'ES384'"
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
        "hdx_probe GET 'http://127.0.0.1:${HYPERDX_PORT}/api/v1/me' 'PROBE_BEARER=${TOKEN}' | grep -q '\"'"
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

hdx_env() {
  kubectl -n "$NS_HYPERDX" get deploy "$HYPERDX_DEPLOY" \
    -o jsonpath="{.spec.template.spec.containers[*].env[?(@.name==\"$1\")].value}" 2>/dev/null
}

# The chart configures the connection as discrete CLICKHOUSE_* vars; the bundled
# DEFAULT_CONNECTIONS blob is the upstream single-container form.
CH_HOST="$(hdx_env CLICKHOUSE_HOST)"
[ -z "$CH_HOST" ] && CH_HOST="$(hdx_env DEFAULT_CONNECTIONS)"
[ -z "$CH_HOST" ] && CH_HOST="$(hdx_env CLICKHOUSE_ENDPOINT)"

check "HyperDX has a ClickHouse connection configured" "[ -n '$CH_HOST' ]"

# Reaching ClickHouse FROM the HyperDX pod is the seam. Doing it from a CH pod (as
# the integration test does) proves ClickHouse works, not that HyperDX can get to it
# -- different NetworkPolicy, different service DNS, different credentials.
# Built from the host HyperDX is configured with: the Service name differs per
# mode, so a literal resolves to no such host on the mode it was not written for.
CH_HOST_ONLY="$(hdx_env CLICKHOUSE_HOST)"
CH_URL="${DFE_HYPERDX_CH_URL:-http://${CH_HOST_ONLY:-dfe-clickhouse.${NS_CH}.svc.cluster.local}:${DFE_CH_HTTP_PORT:-8123}}"
check "HyperDX pod reaches ClickHouse over HTTP (${CH_URL})" \
  "hdx_probe GET '${CH_URL}/ping' | grep -q '^Ok'"

# The query side of the same tables the integration test writes to. Rows may be
# legitimately absent on a just-provisioned cluster, so assert the table RESOLVES;
# freshness is the integration test's job.
# ClickHouse serves /ping unauthenticated but rejects an unauthenticated query, so
# the credentials come from HyperDX's own environment inside the pod. One helper,
# one layer of quoting: threading the headers through check's eval mangles them.
hdx_ch_query() {
  hdx_probe GET "${CH_URL}/?query=$1" PROBE_CH_AUTH=1
}
check "otel table ${OTEL_DB}.${OTEL_LOGS_TABLE} is queryable from HyperDX" \
  "printf '%s' \"\$(hdx_ch_query 'SELECT+count()+FROM+${OTEL_DB}.${OTEL_LOGS_TABLE}')\" | grep -qE '^[0-9]+'"

# ---------------------------------------------------------------------------
echo ""
echo "=== SEAM 3: embed (dfe-ui iframes HyperDX) ==="

# next.config.mjs sets `Content-Security-Policy: frame-ancestors 'self' <extra>` on
# every route and deliberately does NOT set X-Frame-Options -- the latter is
# all-or-nothing and browsers that honour it block the embed whatever the CSP says.
HEADERS="$(hdx_head '/')"
if [[ "$HEADERS" != HTTP* ]]; then
  skip "embed headers -- could not read response headers from the HyperDX pod"
  note "(the in-pod node probe returned no status line). The embed is UNVERIFIED, not proven."
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
