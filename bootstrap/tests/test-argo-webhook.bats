#!/usr/bin/env bats
# smoke-test-argo-webhook.sh: a Forgejo test delivery has to show up in Argo's log.
# kubectl is a stub that answers from files in STUB_DIR:
#   no-forgejo     the Forgejo deployment does not exist
#   repos          the JSON the repo search prints
#   hooks          the JSON the hook listing prints
#   test-code      the HTTP code the test delivery returns (default 204)
#   argo-log       the lines argocd-server's log holds
# No cluster is involved.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    SMOKE="${REPO_ROOT}/bootstrap/smoke-test-argo-webhook.sh"
    STUB_DIR="${BATS_TEST_TMPDIR}/stub"
    mkdir -p "${STUB_DIR}/bin"
    printf '%s' '{"ok":true,"data":[{"id":1,"full_name":"dfe-admin/deploy"}]}' > "${STUB_DIR}/repos"
    printf '%s' '[{"id":7,"type":"gogs","config":{"content_type":"json","url":"http://argocd-server.argocd.svc/api/webhook"},"events":["push"]}]' > "${STUB_DIR}/hooks"
    cat > "${STUB_DIR}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
case "$*" in
    "-n forgejo get deploy "*)
        [[ -f "${STUB_DIR}/no-forgejo" ]] && exit 1
        ;;
    "-n forgejo exec "*)
        case "$*" in
            *"repos/search"*) cat "${STUB_DIR}/repos" ;;
            *"/hooks/7/tests"*) printf '\n%s\n' "$(cat "${STUB_DIR}/test-code" 2>/dev/null || echo 204)"; exit 0 ;;
            *"/hooks"*) cat "${STUB_DIR}/hooks" ;;
        esac
        printf '\n200\n'
        ;;
    "-n argocd logs "*)
        cat "${STUB_DIR}/argo-log" 2>/dev/null
        ;;
esac
exit 0
STUB
    chmod +x "${STUB_DIR}/bin/kubectl"
    printf '#!/bin/sh\nexit 0\n' > "${STUB_DIR}/bin/sleep"
    chmod +x "${STUB_DIR}/bin/sleep"
    export STUB_DIR
    export PATH="${STUB_DIR}/bin:${PATH}"
}

@test "an Argo that logs the push event passes" {
    echo 'time="x" level=info msg="Received push event repo: http://f/dfe-admin/deploy, revision: main, touchedHead: true"' > "${STUB_DIR}/argo-log"
    run bash "${SMOKE}"
    [ "$status" -eq 0 ]
    [[ "${output}" == *"carries the Argo webhook (hook 7)"* ]]
    [[ "${output}" == *"Argo accepted the push event"* ]]
}

@test "an Argo that logs nothing fails, naming the three things that could be wrong" {
    run env DFE_WEBHOOK_WAIT=2 bash "${SMOKE}"
    [ "$status" -ne 0 ]
    [[ "${output}" == *"webhook.gogs.secret is wrong"* ]]
}

@test "a repo with no Argo hook fails" {
    echo '[]' > "${STUB_DIR}/hooks"
    run bash "${SMOKE}"
    [ "$status" -ne 0 ]
    [[ "${output}" == *"no Argo webhook"* ]]
}

@test "a hook on some other URL is not the Argo hook" {
    echo '[{"id":7,"type":"gogs","config":{"url":"http://elsewhere/notify"}}]' > "${STUB_DIR}/hooks"
    run bash "${SMOKE}"
    [ "$status" -ne 0 ]
    [[ "${output}" == *"no Argo webhook"* ]]
}

@test "a test delivery Forgejo refuses fails" {
    echo 500 > "${STUB_DIR}/test-code"
    run bash "${SMOKE}"
    [ "$status" -ne 0 ]
    [[ "${output}" == *"refused the test delivery"* ]]
}

@test "no repository at all fails" {
    echo '{"ok":true,"data":[]}' > "${STUB_DIR}/repos"
    run bash "${SMOKE}"
    [ "$status" -ne 0 ]
    [[ "${output}" == *"lists no repository"* ]]
}

@test "a deploy with no bundled Forgejo skips and says so" {
    : > "${STUB_DIR}/no-forgejo"
    run bash "${SMOKE}"
    [ "$status" -eq 0 ]
    [[ "${output}" == *"SKIP"* ]]
}
