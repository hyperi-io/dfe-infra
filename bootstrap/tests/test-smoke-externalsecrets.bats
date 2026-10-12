#!/usr/bin/env bats
# smoke-test.sh's ExternalSecret check. The internal CA restore is CreatedOnce
# and Ready=False on a first bootstrap by design: there is nothing in the store
# to restore yet, and once cert-manager mints the root the restore never writes
# again. Anything else that is not synced still fails the check.
# kubectl is a stub that answers from files in STUB_DIR:
#   externalsecrets        the "<ns>|<name>|<ready>|<refreshPolicy>|<target>" lines
#                          the ExternalSecret listing prints
#   externalsecrets-unreadable  listing the ExternalSecrets fails
#   secrets                "<ns>/<name>" for each Secret that exists
# No cluster is involved.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    SMOKE="${REPO_ROOT}/bootstrap/smoke-test.sh"
    STUB_DIR="${BATS_TEST_TMPDIR}/stub"
    mkdir -p "${STUB_DIR}/bin"
    : > "${STUB_DIR}/secrets"
    cat > "${STUB_DIR}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
case "$*" in
    "get externalsecrets -A -o jsonpath="*)
        [[ -f "${STUB_DIR}/externalsecrets-unreadable" ]] && exit 1
        cat "${STUB_DIR}/externalsecrets" 2>/dev/null
        ;;
    "-n "*" get secret "*)
        grep -qxF "${2}/${5}" "${STUB_DIR}/secrets"
        exit $?
        ;;
esac
exit 0
STUB
    chmod +x "${STUB_DIR}/bin/kubectl"
    export STUB_DIR
    export PATH="${STUB_DIR}/bin:${PATH}"
}

# The deploy's ordinary ExternalSecrets, all synced, plus the restore in state ${1}.
externalsecrets_with_restore() {
    printf '%s\n' \
        'dfe-test|dfe-engine-secrets|True||' \
        'hyperdx|hyperdx-secrets|True||' \
        "cert-manager|dfe-internal-ca-restore|${1}|CreatedOnce|dfe-internal-ca-tls" > "${STUB_DIR}/externalsecrets"
}

@test "a first bootstrap's CreatedOnce restore is not counted once cert-manager has minted the root" {
    externalsecrets_with_restore False
    echo "cert-manager/dfe-internal-ca-tls" > "${STUB_DIR}/secrets"
    run bash "${SMOKE}"
    [[ "${output}" == *"[PASS] every ExternalSecret is synced"* ]]
}

@test "expected failure: the CreatedOnce restore fails the check while its Secret is absent" {
    externalsecrets_with_restore False
    run bash "${SMOKE}"
    [[ "${output}" == *"[FAIL] every ExternalSecret is synced"* ]]
}

@test "expected failure: an ExternalSecret that refreshes fails the check even when its Secret exists" {
    printf '%s\n' 'dfe-test|dfe-engine-secrets|False||dfe-engine-secrets' > "${STUB_DIR}/externalsecrets"
    echo "dfe-test/dfe-engine-secrets" > "${STUB_DIR}/secrets"
    run bash "${SMOKE}"
    [[ "${output}" == *"[FAIL] every ExternalSecret is synced"* ]]
}

@test "expected failure: a Secret of that name in another namespace does not excuse the restore" {
    externalsecrets_with_restore False
    echo "dfe-test/dfe-internal-ca-tls" > "${STUB_DIR}/secrets"
    run bash "${SMOKE}"
    [[ "${output}" == *"[FAIL] every ExternalSecret is synced"* ]]
}

@test "a CreatedOnce ExternalSecret with no target name is judged by the Secret named after it" {
    printf '%s\n' 'dfe-test|seeded-once|False|CreatedOnce|' > "${STUB_DIR}/externalsecrets"
    echo "dfe-test/seeded-once" > "${STUB_DIR}/secrets"
    run bash "${SMOKE}"
    [[ "${output}" == *"[PASS] every ExternalSecret is synced"* ]]
}

@test "expected failure: no ExternalSecret at all fails the check" {
    run bash "${SMOKE}"
    [[ "${output}" == *"[FAIL] every ExternalSecret is synced"* ]]
}

@test "expected failure: an ExternalSecret list the cluster will not answer fails the check" {
    externalsecrets_with_restore True
    touch "${STUB_DIR}/externalsecrets-unreadable"
    run bash "${SMOKE}"
    [[ "${output}" == *"[FAIL] every ExternalSecret is synced"* ]]
}

@test "every ExternalSecret synced passes" {
    externalsecrets_with_restore True
    run bash "${SMOKE}"
    [[ "${output}" == *"[PASS] every ExternalSecret is synced"* ]]
}
