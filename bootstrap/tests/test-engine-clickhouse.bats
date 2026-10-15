#!/usr/bin/env bats
# bootstrap/scripts/engine-clickhouse.sh, the one way the smoke tests reach ClickHouse
# through dfe-engine's own container. ENGINE_CH_URL_SH resolves the URL as the engine's
# settings do (settings.py reads DFE_CLICKHOUSE_SECURE as true, 1 or yes in any case,
# and defaults.yaml makes an unset one secure), because HyperDX's proxy is handed the
# URL the engine builds from them. The snippet runs under sh, as in the engine image.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    BOOTSTRAP="${REPO_ROOT}/bootstrap"
    HELPER="${BOOTSTRAP}/scripts/engine-clickhouse.sh"
}

# ENGINE_CH_URL_SH's ch_url, run by sh under only the engine environment given as KEY=VALUE arguments.
url_under() {
    local snippet
    snippet="$(bash -c '. "$1"; printf "%s" "${ENGINE_CH_URL_SH}"' _ "${HELPER}")"
    run env -i PATH=/usr/bin:/bin "$@" sh -c "${snippet}"'
printf "%s" "$ch_url"'
}

@test "a secure flag the engine reads as true dials https on the engine's port" {
    for secure in true TRUE True 1 yes YES; do
        url_under DFE_CLICKHOUSE_HOST=ch.example.com DFE_CLICKHOUSE_PORT=8443 DFE_CLICKHOUSE_SECURE="${secure}"
        [ "${status}" -eq 0 ]
        [ "${output}" = "https://ch.example.com:8443" ]
    done
}

@test "a secure flag the engine reads as false dials http" {
    for secure in false FALSE 0 no off; do
        url_under DFE_CLICKHOUSE_HOST=ch.example.com DFE_CLICKHOUSE_PORT=8123 DFE_CLICKHOUSE_SECURE="${secure}"
        [ "${output}" = "http://ch.example.com:8123" ]
    done
}

@test "an unset or empty secure flag is https, the engine's own default" {
    url_under DFE_CLICKHOUSE_HOST=ch.example.com DFE_CLICKHOUSE_PORT=8443
    [ "${output}" = "https://ch.example.com:8443" ]
    url_under DFE_CLICKHOUSE_HOST=ch.example.com DFE_CLICKHOUSE_PORT=8443 DFE_CLICKHOUSE_SECURE=
    [ "${output}" = "https://ch.example.com:8443" ]
}

@test "an unset port is the engine's default HTTP port" {
    url_under DFE_CLICKHOUSE_HOST=ch.example.com DFE_CLICKHOUSE_SECURE=false
    [ "${output}" = "http://ch.example.com:8123" ]
}

@test "an engine with no host names no URL, so a probe cannot dial a guessed one" {
    url_under DFE_CLICKHOUSE_PORT=8443 DFE_CLICKHOUSE_SECURE=true
    [ "${status}" -eq 0 ]
    [ -z "${output}" ]
}

@test "every script that reaches ClickHouse through the engine sources the helper and carries no copy" {
    for script in smoke-test-integration.sh smoke-test-data.sh smoke-test-hyperdx.sh keda-scale-test.sh; do
        run grep -F 'scripts/engine-clickhouse.sh' "${BOOTSTRAP}/${script}"
        [ "${status}" -eq 0 ]
        run grep -E '^ENGINE_CH_(QUERY|URL_SH)=' "${BOOTSTRAP}/${script}"
        [ "${status}" -eq 1 ]
    done
    [ "$(grep -cE '^ENGINE_CH_QUERY=' "${HELPER}")" -eq 1 ]
}
