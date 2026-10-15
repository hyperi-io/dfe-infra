#!/usr/bin/env bats
# smoke-test-data.sh's ClickHouse section against each place the server runs.
# An external server (ClickHouse Cloud) runs no pod in the cluster, so a pod check can
# never pass: it is asked a query from the engine's own container instead, with the
# engine's own host, port, scheme and credential. An in-cluster server is still
# checked for a Running pod. The brokerless slim profile keeps Kafka out of the way.
# kubectl is a stub that answers from files in STUB_DIR:
#   annotation-<key>  the dfe-cluster annotation dfe.hyperi.io/<key>
#   engine-env        the engine container's environment, one KEY=VALUE a line; an
#                     engine exec of `sh -c` runs its script under exactly that
#                     environment, with pod-bin/curl standing in for curl
#   ch-unreachable    pod-bin/curl exits 7, as on a server it cannot reach
#   ch-pods           ns/clickhouse holds one Running ClickHouse pod
# kubectl.log records every call, curl.log every curl the engine container ran,
# curl-config.log the config curl read on stdin, and query.log the query it posted.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    SMOKE="${REPO_ROOT}/bootstrap/smoke-test-data.sh"
    STUB_DIR="${BATS_TEST_TMPDIR}/stub"
    mkdir -p "${STUB_DIR}/bin" "${STUB_DIR}/pod-bin"
    : > "${STUB_DIR}/kubectl.log"
    : > "${STUB_DIR}/curl.log"
    : > "${STUB_DIR}/curl-config.log"
    : > "${STUB_DIR}/query.log"

    cat > "${STUB_DIR}/pod-bin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DIR}/curl.log"
body=-
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
    case "${args[i]}" in
        -K | --config) [[ "${args[i + 1]}" == - ]] && cat >> "${STUB_DIR}/curl-config.log" ;;
        --data-binary) body="${args[i + 1]#@}" ;;
    esac
done
[[ "${body}" == - ]] && body=/dev/stdin
cat "${body}" >> "${STUB_DIR}/query.log"
[[ -f "${STUB_DIR}/ch-unreachable" ]] && exit 7
echo 1
STUB
    chmod +x "${STUB_DIR}/pod-bin/curl"

    cat > "${STUB_DIR}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DIR}/kubectl.log"
args=("$@")
last="${args[${#args[@]} - 1]}"
case "$*" in
    "-n argocd get secret dfe-cluster -o jsonpath="*)
        key="${last##*/}"
        cat "${STUB_DIR}/annotation-${key%\}}" 2>/dev/null
        exit 0 ;;
    *" exec -i deploy/dfe-engine -c dfe-engine -- sh -c "*)
        pod_env=()
        while IFS= read -r line; do
            [[ -n "${line}" ]] && pod_env+=("${line}")
        done < "${STUB_DIR}/engine-env"
        exec env -i PATH="${STUB_DIR}/pod-bin:/usr/bin:/bin" STUB_DIR="${STUB_DIR}" "${pod_env[@]}" sh -c "${last}" ;;
    "-n clickhouse get pods "*)
        [[ -f "${STUB_DIR}/ch-pods" ]] && echo pod/dfe-clickhouse-0
        exit 0 ;;
    *"jsonpath="*) echo 1; exit 0 ;;
    "get ns "*) exit 0 ;;
esac
exit 1
STUB
    chmod +x "${STUB_DIR}/bin/kubectl"
    : > "${STUB_DIR}/engine-env"
    export STUB_DIR
    export PATH="${STUB_DIR}/bin:${PATH}"
    export DFE_PROFILE=slim DFE_NS=dfe-test
    unset DFE_CLICKHOUSE_MODE DFE_ENGINE_DEPLOY
}

# The cluster secret bootstrap.sh writes for ClickHouse Cloud, and the engine env layer2-apps renders for it.
external_clickhouse() {
    printf external > "${STUB_DIR}/annotation-clickhouse_mode"
    printf '%s\n' \
        DFE_CLICKHOUSE_HOST=ch.example.com \
        DFE_CLICKHOUSE_PORT=8443 \
        DFE_CLICKHOUSE_SECURE=true \
        DFE_CLICKHOUSE_VERIFY=true \
        DFE_CLICKHOUSE_USERNAME=default \
        DFE_CLICKHOUSE_PASSWORD=pw-held-by-the-engine > "${STUB_DIR}/engine-env"
}

@test "an external ClickHouse is asked a query from the engine over https, never looked for as pods" {
    external_clickhouse
    run bash "${SMOKE}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"[PASS] external ClickHouse answers a query from deploy/dfe-engine in ns/dfe-test"* ]]
    [[ "${output}" != *"ClickHouse pods running"* ]]
    [[ "${output}" == *"Layer 2 data platform is healthy."* ]]
    [[ "$(cat "${STUB_DIR}/curl.log")" == *"https://ch.example.com:8443/"* ]]
    [ "$(cat "${STUB_DIR}/query.log")" = "SELECT 1" ]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"-n clickhouse get pods"* ]]
}

@test "the engine's credential reaches curl as config on stdin, never a command line or the output" {
    external_clickhouse
    run bash "${SMOKE}"
    [ "${status}" -eq 0 ]
    [[ "$(cat "${STUB_DIR}/curl-config.log")" == *'user = "default:pw-held-by-the-engine"'* ]]
    [[ "$(cat "${STUB_DIR}/curl.log")" != *"pw-held-by-the-engine"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"pw-held-by-the-engine"* ]]
    [[ "${output}" != *"pw-held-by-the-engine"* ]]
}

@test "expected failure: an external ClickHouse the engine cannot reach fails the check" {
    external_clickhouse
    touch "${STUB_DIR}/ch-unreachable"
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"[FAIL] external ClickHouse answers a query from deploy/dfe-engine in ns/dfe-test"* ]]
    [[ "${output}" == *"Layer 2 data platform is NOT healthy."* ]]
}

@test "DFE_CLICKHOUSE_MODE from the deploy's env needs no cluster secret" {
    external_clickhouse
    rm "${STUB_DIR}/annotation-clickhouse_mode"
    export DFE_CLICKHOUSE_MODE=external
    run bash "${SMOKE}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"[PASS] external ClickHouse answers a query from deploy/dfe-engine"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"io/clickhouse_mode"* ]]
}

@test "an in-cluster ClickHouse is still checked for a Running pod, and the engine is not asked" {
    touch "${STUB_DIR}/ch-pods"
    run bash "${SMOKE}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"[PASS] ClickHouse pods running"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"deploy/dfe-engine"* ]]
    [ ! -s "${STUB_DIR}/curl.log" ]
}

@test "expected failure: an in-cluster deployment with no ClickHouse pod fails" {
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"[FAIL] ClickHouse pods running"* ]]
}
