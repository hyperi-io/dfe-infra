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
# The thin chart names the container after the chart, whatever the Deployment is called.
HYPERDX_CONTAINER="${DFE_HYPERDX_CONTAINER:-dfe-hyperdx}"
ENGINE_DEPLOY="${DFE_ENGINE_DEPLOY:-dfe-engine}"
HYPERDX_PORT="${DFE_HYPERDX_PORT:-8080}"
ENGINE_PORT="${DFE_ENGINE_PORT:-8000}"
ENGINE_CONTAINER="${DFE_ENGINE_CONTAINER:-dfe-engine}"

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

# engine_ch_url() -- the ClickHouse URL the engine dials, read from its own container.
# shellcheck source-path=SCRIPTDIR source=scripts/engine-clickhouse.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/engine-clickhouse.sh"

# The fork's runtime image ships node and neither curl nor wget, so the probes run
# in the pod's node with values passed as env vars, never spliced into the JS.
# A probe prints nothing on any failure, so an error is never read as a response.
IFS= read -r -d '' JS_PROBE <<'JS' || true
const e = process.env;
const headers = {};
if (e.PROBE_CH_USER) {
  headers["X-ClickHouse-User"] = e.PROBE_CH_USER;
  headers["X-ClickHouse-Key"] = require("fs").readFileSync(0, "utf8");
}
if (e.PROBE_BEARER_STDIN) headers.Authorization = "Bearer " + require("fs").readFileSync(0, "utf8").trim();
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
# line and headers. PROBE_CH_USER=<user> sends that ClickHouse user with the
# password read from stdin, and PROBE_BEARER_STDIN=1 a bearer token read from stdin
# (hdx_probe_bearer), so a token never sits in an argv.
hdx_probe() {
  local method="$1" url="$2"
  shift 2
  kubectl -n "$NS_HYPERDX" exec "deploy/${HYPERDX_DEPLOY}" -c "$HYPERDX_CONTAINER" -- \
    env "PROBE_METHOD=${method}" "PROBE_URL=${url}" "$@" node -e "$JS_PROBE" 2>/dev/null
}

# hdx_env NAME: the value HyperDX runs with, from the container's own environment, so
# it reads the same whether the chart sets it in the Deployment's env or an envFrom
# ConfigMap. Non-zero when the container cannot be asked, which is not the same as unset.
hdx_env() {
  kubectl -n "$NS_HYPERDX" exec "deploy/${HYPERDX_DEPLOY}" -c "$HYPERDX_CONTAINER" -- \
    node -e 'process.stdout.write(process.env[process.argv[1]] || "")' "$1" 2>/dev/null
}

# hdx_probe_bearer URL: GET with the bearer token read from stdin.
hdx_probe_bearer() {
  kubectl -n "$NS_HYPERDX" exec -i "deploy/${HYPERDX_DEPLOY}" -c "$HYPERDX_CONTAINER" -- \
    env "PROBE_METHOD=GET" "PROBE_URL=$1" "PROBE_BEARER_STDIN=1" node -e "$JS_PROBE" 2>/dev/null
}

# engine_signin_token: a JWT from the engine's own sign-in. The local admin first,
# then the break-glass account when the admin is refused (a fresh deploy's admin
# must change its issued password before anything else, and a used one has changed
# it). Each credential is read from the engine pod's environment inside the pod and
# never leaves it. Prints "<account>" then the token on the next line, or nothing.
engine_signin_token() {
  kubectl -n "$NS_APP" exec "deploy/${ENGINE_DEPLOY}" -c "$ENGINE_CONTAINER" -- python3 -c '
import json, os, sys, urllib.request

port = sys.argv[1]
for account, user_env, pass_env, default in (
    ("admin", "DFE_AUTH_LOCAL_ADMIN_USERNAME", "DFE_AUTH_LOCAL_ADMIN_PASSWORD", "admin"),
    ("breakglass", "", "DFE_AUTH_BREAKGLASS_PASSWORD", "breakglass"),
):
    password = os.environ.get(pass_env, "")
    if not password:
        continue
    body = json.dumps({"username": os.environ.get(user_env, default) if user_env else default, "password": password}).encode()
    req = urllib.request.Request(
        "http://localhost:%s/api/v1/auth/login" % port, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            reply = json.load(r)
    except Exception:
        continue
    if reply.get("access_token") and not reply.get("password_change_required"):
        print(account)
        print(reply["access_token"])
        break
' "$ENGINE_PORT" 2>/dev/null
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
if ! AUTH_MODE="$(hdx_env DFE_AUTH_MODE)"; then
  echo "  [FAIL] read HyperDX's runtime env (exec node in deploy/${HYPERDX_DEPLOY} -c ${HYPERDX_CONTAINER})"
  FAIL=$((FAIL+1))
  note "Without it a setting cannot be told from an unset one, so this seam is not judged."
elif [ -z "$AUTH_MODE" ]; then
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

  JWKS_URL="$(hdx_env DFE_ENGINE_JWKS_URL)"
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
  # The token comes from signing in as the engine's local admin through its own
  # API, read from the pod's environment inside the pod, and reaches HyperDX on
  # stdin. HyperDX answering with the signed-in user is the pass.
  if kubectl -n "$NS_APP" get deploy "$ENGINE_DEPLOY" >/dev/null 2>&1; then
    SIGNIN="$(engine_signin_token)"
    TOKEN="$(printf '%s\n' "$SIGNIN" | sed -n 2p)"
    if [ -n "$TOKEN" ]; then
      note "signed in to ${ENGINE_DEPLOY} as $(printf '%s\n' "$SIGNIN" | sed -n 1p)"
      check "HyperDX accepts an engine-issued token (auth seam closed)" \
        "printf '%s' \"\$TOKEN\" | hdx_probe_bearer 'http://127.0.0.1:${HYPERDX_PORT}/api/me' | grep -q '\"email\"'"
      check "HyperDX refuses a token it did not issue and the engine did not sign" \
        "! printf '%s' 'not.a.jwt' | hdx_probe_bearer 'http://127.0.0.1:${HYPERDX_PORT}/api/me' | grep -q '\"email\"'"
    else
      echo "  [FAIL] engine-issued-token round trip -- neither the local admin nor break-glass signed in to ${ENGINE_DEPLOY}"
      FAIL=$((FAIL+1))
      note "The admin may still owe its forced password change, and break-glass may be turned off or"
      note "throttled. Without a token the two halves AGREEING is unproven, and that is the check"
      note "that matters here."
    fi
  else
    skip "engine-issued-token round trip -- deploy/${ENGINE_DEPLOY} not in ns/${NS_APP}"
  fi
fi

# ---------------------------------------------------------------------------
echo ""
echo "=== SEAM 2: data (HyperDX queries the DFE ClickHouse) ==="

# The chart configures the connection as discrete CLICKHOUSE_* vars; the bundled
# DEFAULT_CONNECTIONS blob is the upstream single-container form.
CH_HOST="$(hdx_env CLICKHOUSE_HOST)"
[ -z "$CH_HOST" ] && CH_HOST="$(hdx_env DEFAULT_CONNECTIONS)"
[ -z "$CH_HOST" ] && CH_HOST="$(hdx_env CLICKHOUSE_ENDPOINT)"

check "HyperDX has a ClickHouse connection configured" "[ -n '$CH_HOST' ]"

# Reaching ClickHouse FROM the HyperDX pod is the seam. Doing it from a CH pod (as
# the integration test does) proves ClickHouse works, not that HyperDX can get to it
# -- different NetworkPolicy, different service DNS, different credentials.
# The URL dialled is the one HyperDX's proxy is handed: dfe-engine serves each team its
# connection from its own settings, so a TLS server is reached over https on its TLS
# port. CLICKHOUSE_HOST only keys HyperDX's webhook refusal, which reads its host and
# port and never its scheme, so the chart's http:// there does not say how to dial.
if kubectl -n "$NS_APP" get deploy "$ENGINE_DEPLOY" >/dev/null 2>&1; then
  CH_URL="$(engine_ch_url "$NS_APP")"
else
  # With no engine to ask, the configured URL is dialled as is.
  CH_URL="$(hdx_env CLICKHOUSE_HOST)"
  CH_URL="${CH_URL:-http://dfe-clickhouse.${NS_CH}.svc.cluster.local:${DFE_CH_HTTP_PORT:-8123}}"
fi
CH_URL="${DFE_HYPERDX_CH_URL:-$CH_URL}"
check "HyperDX pod reaches ClickHouse over HTTP (${CH_URL:-deploy/${ENGINE_DEPLOY} named no ClickHouse URL})" \
  "hdx_probe GET '${CH_URL}/ping' | grep -q '^Ok'"

# The query side of the same tables the integration test writes to. Rows may be
# legitimately absent on a just-provisioned cluster, so assert the table RESOLVES;
# freshness is the integration test's job.
# ClickHouse rejects an unauthenticated query, so it reads as the query reader the
# platform team's connection uses, its password on stdin. One helper, one layer of
# quoting: threading the headers through check's eval mangles them.
READER_USER="${DFE_HYPERDX_CH_USER:-dfe_query_reader}"
READER_SECRET="${DFE_HYPERDX_CH_SECRET:-query-reader-clickhouse}"
hdx_ch_query() {
  kubectl -n "$NS_APP" get secret "$READER_SECRET" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null |
    kubectl -n "$NS_HYPERDX" exec -i "deploy/${HYPERDX_DEPLOY}" -c "$HYPERDX_CONTAINER" -- \
      env "PROBE_METHOD=GET" "PROBE_URL=${CH_URL}/?query=$1" "PROBE_CH_USER=${READER_USER}" node -e "$JS_PROBE" 2>/dev/null
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

FRAME_ANCESTORS="$(hdx_env DFE_EMBED_FRAME_ANCESTORS)"
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
