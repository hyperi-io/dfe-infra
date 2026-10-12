#!/usr/bin/env bats
# smoke-test-auth.sh's SecurityPolicy and argocd route checks. A SecurityPolicy
# reports Accepted per ancestor under .status.ancestors (gateway-api
# PolicyStatus) and has no .status.conditions, and the argocd route renders only
# where exposure.groups.admin and routes.argocd are both on.
# kubectl is a stub that answers from files in STUB_DIR:
#   managed-routes       "<namespace>/<name>" for each HTTPRoute Argo CD manages
#   applications-unreadable  listing the Applications fails
#   argocd-route         HTTPRoute argocd/argocd exists
#   policies             one SecurityPolicy name a line
#   policy-<name>        the Accepted statuses of that policy's ancestors, which
#                        the stub prints only for a jsonpath reading
#                        .status.ancestors[*].conditions
# kubectl.log records every call. No cluster is involved.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    SMOKE="${REPO_ROOT}/bootstrap/smoke-test-auth.sh"
    STUB_DIR="${BATS_TEST_TMPDIR}/stub"
    mkdir -p "${STUB_DIR}/bin"
    : > "${STUB_DIR}/kubectl.log"
    cat > "${STUB_DIR}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DIR}/kubectl.log"
case "$*" in
    "-n argocd get applications.argoproj.io -o jsonpath="*)
        [[ -f "${STUB_DIR}/applications-unreadable" ]] && exit 1
        cat "${STUB_DIR}/managed-routes" 2>/dev/null
        ;;
    "-n argocd get httproute argocd")
        [[ -f "${STUB_DIR}/argocd-route" ]] || exit 1
        ;;
    "-n envoy-gateway-system get securitypolicy -l "*)
        while read -r name; do
            echo "securitypolicy.gateway.envoyproxy.io/${name}"
        done < <(cat "${STUB_DIR}/policies" 2>/dev/null)
        ;;
    "-n envoy-gateway-system get securitypolicy "*" -o jsonpath="*)
        if [[ "$*" == *".status.ancestors[*].conditions"* ]]; then
            cat "${STUB_DIR}/policy-${5}" 2>/dev/null
        fi
        ;;
esac
exit 0
STUB
    chmod +x "${STUB_DIR}/bin/kubectl"
    export STUB_DIR
    export PATH="${STUB_DIR}/bin:${PATH}"
}

# One SecurityPolicy whose ancestors report ${1}.
policy_reporting() {
    echo dfe-edge-fence > "${STUB_DIR}/policies"
    printf '%s' "${1}" > "${STUB_DIR}/policy-dfe-edge-fence"
}

@test "a SecurityPolicy its gateway accepted passes" {
    policy_reporting "True"
    run bash "${SMOKE}"
    [[ "${output}" == *"[PASS] SecurityPolicy dfe-edge-fence accepted"* ]]
}

@test "every ancestor accepting the policy passes" {
    policy_reporting "True True"
    run bash "${SMOKE}"
    [[ "${output}" == *"[PASS] SecurityPolicy dfe-edge-fence accepted"* ]]
}

@test "expected failure: a SecurityPolicy its gateway refused fails" {
    policy_reporting "False"
    run bash "${SMOKE}"
    [[ "${output}" == *"[FAIL] SecurityPolicy dfe-edge-fence accepted"* ]]
}

@test "expected failure: a SecurityPolicy one of two ancestors refused fails" {
    policy_reporting "True False"
    run bash "${SMOKE}"
    [[ "${output}" == *"[FAIL] SecurityPolicy dfe-edge-fence accepted"* ]]
}

@test "expected failure: a SecurityPolicy no ancestor has reported on yet fails" {
    policy_reporting ""
    run bash "${SMOKE}"
    [[ "${output}" == *"[FAIL] SecurityPolicy dfe-edge-fence accepted"* ]]
}

@test "the argocd route is required where Argo CD manages it" {
    printf '%s\n' dfe-test/dfe-ui argocd/argocd > "${STUB_DIR}/managed-routes"
    touch "${STUB_DIR}/argocd-route"
    run bash "${SMOKE}"
    [[ "${output}" == *"[PASS] argocd HTTPRoute"* ]]
}

@test "expected failure: an argocd route Argo CD manages and the cluster lacks fails" {
    printf '%s\n' dfe-test/dfe-ui argocd/argocd > "${STUB_DIR}/managed-routes"
    run bash "${SMOKE}"
    [[ "${output}" == *"[FAIL] argocd HTTPRoute"* ]]
}

@test "the argocd route is skipped where the gateway renders none, as with the admin group off" {
    printf '%s\n' dfe-test/dfe-ui dfe-test/dfe-engine hyperdx/hyperdx > "${STUB_DIR}/managed-routes"
    run bash "${SMOKE}"
    [[ "${output}" == *"[SKIP] argocd HTTPRoute"* ]]
    [[ "${output}" != *"[FAIL] argocd HTTPRoute"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"get httproute argocd"* ]]
}

@test "a route named argocd in another namespace does not make the argocd route required" {
    printf '%s\n' dfe-test/argocd > "${STUB_DIR}/managed-routes"
    run bash "${SMOKE}"
    [[ "${output}" == *"[SKIP] argocd HTTPRoute"* ]]
}

@test "expected failure: Applications that cannot be listed leave the argocd route required" {
    touch "${STUB_DIR}/applications-unreadable"
    run bash "${SMOKE}"
    [[ "${output}" == *"[FAIL] argocd HTTPRoute"* ]]
}
