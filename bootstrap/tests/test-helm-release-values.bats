#!/usr/bin/env bats
# bootstrap.sh's helm installs take their values from bootstrap/helm_releases.py,
# the table `dfe-ops upgrade apply` prints its bootstrap upgrades from, so the
# install and the upgrade an operator is told to run cannot set different values.
# scripts/tests/test_dfe_ops_upgrade.py compares the two argument for argument.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    SCRIPT="${REPO_ROOT}/bootstrap/bootstrap.sh"
    export SCRIPT_DIR="${REPO_ROOT}/bootstrap"
    # bootstrap.sh's own helper, cut out of the script as it ships, run under its strict mode.
    DRIVER="${BATS_TEST_TMPDIR}/values.sh"
    {
        echo 'set -euo pipefail'
        sed -n '/^dfe_release_values() {$/,/^}$/p' "${SCRIPT}"
        echo 'dfe_release_values "$@"'
        echo 'printf "[%s]\n" ${DFE_RELEASE_VALUES[@]+"${DFE_RELEASE_VALUES[@]}"}'
    } > "${DRIVER}"
}

@test "each of the three installs splices the helper's values" {
    run grep -c '${DFE_RELEASE_VALUES\[@\]+"${DFE_RELEASE_VALUES\[@\]}"}' "${SCRIPT}"
    [ "$status" -eq 0 ]
    [ "$output" -eq 3 ]
}

@test "the helper is called for each release it installs" {
    run grep -E '^\s+dfe_release_values (cert-manager|external-secrets)$' "${SCRIPT}"
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 2 ]
    run grep -F 'dfe_release_values argocd --domain "${DFE_DOMAIN}" --cache-service "${VALKEY_SVC}"' "${SCRIPT}"
    [ "$status" -eq 0 ]
}

@test "no install writes a value of its own" {
    run grep -E -- '--set (crds|config|redis|externalRedis)\.|--set-string .?(global|configs)\.' "${SCRIPT}"
    [ "$status" -ne 0 ]
}

@test "cert-manager installs with its CRDs and the Gateway API shim" {
    run bash "${DRIVER}" cert-manager
    [ "$status" -eq 0 ]
    [ "$output" = "$(printf '%s\n' '[--set]' '[crds.enabled=true]' '[--set]' '[config.enableGatewayAPI=true]')" ]
}

@test "external-secrets installs with no values, and the empty array survives set -u" {
    run bash "${DRIVER}" external-secrets
    [ "$status" -eq 0 ]
    [ "$output" = "[]" ]
}

@test "Argo installs wired to the cache Service and the domain it is given" {
    run bash "${DRIVER}" argocd --domain slim.dfe.example.com --cache-service cache
    [ "$status" -eq 0 ]
    [[ "$output" == *"[externalRedis.host=cache.argocd.svc.cluster.local]"* ]]
    [[ "$output" == *"[global.domain=argocd.slim.dfe.example.com]"* ]]
    [[ "$output" == *'[configs.params.server\.insecure=true]'* ]]
    [ "${lines[${#lines[@]}-2]}" = "[--values]" ]
    [ "${lines[${#lines[@]}-1]}" = "[-]" ]
}

@test "Argo without a domain stops the script rather than installing a blank one" {
    run bash "${DRIVER}" argocd
    [ "$status" -ne 0 ]
    [[ "$output" == *"no --domain was given"* ]]
}
