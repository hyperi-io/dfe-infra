#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         smoke-test-argo-webhook.sh
#  Purpose:      Prove a push to the bundled Forgejo deploy repo reaches Argo CD
#                before the repo poll, by firing Forgejo's own test delivery and
#                reading Argo's log for the push event it accepted.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
# Argo logs "Received push event" only after the delivery's HMAC checks out against
# argocd-secret's webhook.gogs.secret, so the line proves the hook exists, reaches
# argocd-server and shares its secret with Forgejo. The admin credential is read
# inside the Forgejo pod and never leaves it. No repository is written.
#
#   bash bootstrap/smoke-test-argo-webhook.sh [kubeconfig]
#
# A deploy with no bundled Forgejo (the deploy repo is hosted elsewhere) has no
# hook of ours to prove and exits 0 saying so.
set -uo pipefail

[ -n "${1:-}" ] && export KUBECONFIG="$1"

NS_FORGEJO="${DFE_FORGEJO_NS:-forgejo}"
NS_ARGOCD="${DFE_ARGOCD_NS:-argocd}"
FORGEJO_DEPLOY="${DFE_FORGEJO_DEPLOY:-dfe-forgejo}"
# Seconds Argo gets to log the delivery. The poll it replaces is 300.
WAIT="${DFE_WEBHOOK_WAIT:-30}"

PASS=0; FAIL=0
pass() { echo "  [PASS] $1"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

echo "=== DFE Forgejo -> Argo push webhook ==="

if ! kubectl -n "${NS_FORGEJO}" get deploy "${FORGEJO_DEPLOY}" >/dev/null 2>&1; then
    echo "  [SKIP] no bundled Forgejo (${NS_FORGEJO}/${FORGEJO_DEPLOY}): this deploy's repo host registers its own hook"
    exit 0
fi

# Runs in the Forgejo pod, which has GITEA_ADMIN_USER and GITEA_ADMIN_PASSWORD in its
# env. curl reads the credential from its config on stdin, so it never sits in an
# argv a process listing shows.
forgejo_api() {
    local method="$1" path="$2"
    # shellcheck disable=SC2016 # the pod's shell expands these, not ours
    kubectl -n "${NS_FORGEJO}" exec "deploy/${FORGEJO_DEPLOY}" -- sh -c \
        'printf "user = \"%s:%s\"\n" "$GITEA_ADMIN_USER" "$GITEA_ADMIN_PASSWORD" | curl -s -K - -o - -w "\n%{http_code}" -X "$0" "http://localhost:${GITEA__server__HTTP_PORT:-3000}/api/v1$1"' \
        "${method}" "${path}" 2>/dev/null
}

repos="$(forgejo_api GET '/repos/search?limit=50' | sed '$d' | tr ',{}' '\n\n\n' | sed -n 's/^"full_name":"\(.*\)"$/\1/p')"
if [[ -z "${repos}" ]]; then
    fail "Forgejo lists no repository, so there is no deploy repo to push to"
fi

hook_repo=""; hook_id=""
for repo in ${repos}; do
    # One hook per line: each starts {"id":N and its config nests the url.
    id="$(forgejo_api GET "/repos/${repo}/hooks" | sed '$d' | sed 's/{"id":/\n{"id":/g' \
        | grep -F '/api/webhook' | sed -n 's/^{"id":\([0-9]*\),.*/\1/p' | head -1)"
    if [[ -n "${id}" ]]; then hook_repo="${repo}"; hook_id="${id}"; break; fi
done

if [[ -z "${hook_id}" ]]; then
    fail "no Argo webhook (a hook on /api/webhook) is registered on any Forgejo repo"
else
    pass "Forgejo repo ${hook_repo} carries the Argo webhook (hook ${hook_id})"
    before="$(date +%s)"
    code="$(forgejo_api POST "/repos/${hook_repo}/hooks/${hook_id}/tests" | tail -1)"
    if [[ "${code}" != "204" ]]; then
        fail "Forgejo refused the test delivery (HTTP ${code:-none})"
    else
        seen=false
        for ((waited = 0; waited < WAIT; waited += 2)); do
            if kubectl -n "${NS_ARGOCD}" logs deploy/argocd-server --since-time="$(date -u -d "@${before}" +%Y-%m-%dT%H:%M:%SZ)" 2>/dev/null \
                | grep -q 'Received push event'; then
                seen=true; break
            fi
            sleep 2
        done
        if [[ "${seen}" == "true" ]]; then
            pass "Argo accepted the push event within ${WAIT}s (the repo poll is 300s)"
        else
            fail "Argo logged no push event ${WAIT}s after the test delivery: the hook, the route to argocd-server or webhook.gogs.secret is wrong"
        fi
    fi
fi

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
(( FAIL == 0 ))
