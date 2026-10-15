#!/usr/bin/env bats
# bootstrap/scripts/cluster-facts.sh, the one cluster_fact() the smoke tests and the
# access summary share. It reads an annotation off the dfe-cluster Secret's metadata
# and never its data, and it must not stop a caller running under set -e.
# kubectl is a stub: with STUB_DIR/value present it prints that file, otherwise it
# fails as it does on a Secret that is not there. kubectl.log records every call.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    BOOTSTRAP="${REPO_ROOT}/bootstrap"
    HELPER="${BOOTSTRAP}/scripts/cluster-facts.sh"
    STUB_DIR="${BATS_TEST_TMPDIR}/stub"
    mkdir -p "${STUB_DIR}/bin"
    : > "${STUB_DIR}/kubectl.log"

    cat > "${STUB_DIR}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DIR}/kubectl.log"
[[ -f "${STUB_DIR}/value" ]] || exit 1
cat "${STUB_DIR}/value"
STUB
    chmod +x "${STUB_DIR}/bin/kubectl"
    export STUB_DIR
    export PATH="${STUB_DIR}/bin:${PATH}"
}

@test "a fact is read from the annotation on the dfe-cluster Secret in argocd" {
    printf external > "${STUB_DIR}/value"
    run bash -c '. "$1"; cluster_fact kafka_mode' _ "${HELPER}"
    [ "${status}" -eq 0 ]
    [ "${output}" = external ]
    [ "$(cat "${STUB_DIR}/kubectl.log")" = '-n argocd get secret dfe-cluster -o jsonpath={.metadata.annotations.dfe\.hyperi\.io/kafka_mode}' ]
}

@test "the Secret's data is never read" {
    printf external > "${STUB_DIR}/value"
    run bash -c '. "$1"; cluster_fact kafka_mode' _ "${HELPER}"
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *".data"* ]]
}

@test "an unreadable Secret gives an empty fact and status 0, so set -e survives" {
    run bash -e -c '. "$1"; fact="$(cluster_fact kafka_mode)"; echo "after:${fact}:"' _ "${HELPER}"
    [ "${status}" -eq 0 ]
    [ "${output}" = "after::" ]
}

@test "the smoke tests and the access summary source the shared helper and carry no copy" {
    for script in smoke-test-data.sh smoke-test-integration.sh access-summary.sh; do
        run grep -F 'scripts/cluster-facts.sh' "${BOOTSTRAP}/${script}"
        [ "${status}" -eq 0 ]
        run grep -E '^(cluster_fact|ann)\(\)' "${BOOTSTRAP}/${script}"
        [ "${status}" -eq 1 ]
    done
    [ "$(grep -cE '^cluster_fact\(\)' "${HELPER}")" -eq 1 ]
}
