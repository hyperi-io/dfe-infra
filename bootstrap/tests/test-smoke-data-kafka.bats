#!/usr/bin/env bats
# smoke-test-data.sh's Kafka section against each way a deployment runs its broker.
# A managed broker (MSK, Confluent Cloud, Redpanda Cloud) has no pod in the cluster,
# so the in-cluster broker checks can never pass and must SKIP, naming the provider.
# An in-cluster broker (Strimzi, Redpanda) is still checked, and a brokerless profile
# still skips on the profile alone.
# kubectl is a stub that answers from files in STUB_DIR:
#   annotation-<key>          the dfe-cluster annotation dfe.hyperi.io/<key>
#   cluster-secret-unreadable the dfe-cluster read fails, as on a missing Secret
#   kafka-cr                  the Strimzi Kafka CR dfe-kafka exists in ns/strimzi
#   running-pods              the label selectors that match a Running pod in ns/strimzi,
#                             one a line
# kubectl.log records every call, so a test can see which ones were never made.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    SMOKE="${REPO_ROOT}/bootstrap/smoke-test-data.sh"
    STUB_DIR="${BATS_TEST_TMPDIR}/stub"
    mkdir -p "${STUB_DIR}/bin"
    : > "${STUB_DIR}/kubectl.log"
    : > "${STUB_DIR}/running-pods"

    cat > "${STUB_DIR}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DIR}/kubectl.log"
args=("$@")
last="${args[${#args[@]} - 1]}"
case "$*" in
    "-n argocd get secret dfe-cluster -o jsonpath="*)
        [[ -f "${STUB_DIR}/cluster-secret-unreadable" ]] && exit 1
        key="${last##*/}"
        cat "${STUB_DIR}/annotation-${key%\}}" 2>/dev/null
        exit 0 ;;
    "-n strimzi get kafka dfe-kafka -o jsonpath="*) echo True; exit 0 ;;
    "-n strimzi get kafka dfe-kafka") [[ -f "${STUB_DIR}/kafka-cr" ]]; exit $? ;;
    "-n strimzi get pods -l "*)
        if grep -qxF -- "${args[5]}" "${STUB_DIR}/running-pods"; then echo pod/dfe-kafka-0; fi
        exit 0 ;;
    "-n clickhouse get pods "*) echo pod/dfe-clickhouse-0; exit 0 ;;
    *"jsonpath="*) echo 1; exit 0 ;;
    "get ns "*) exit 0 ;;
esac
exit 1
STUB
    chmod +x "${STUB_DIR}/bin/kubectl"
    export STUB_DIR
    export PATH="${STUB_DIR}/bin:${PATH}"
    unset DFE_KAFKA_MODE DFE_KAFKA_PROVIDER
    export DFE_PROFILE=scale
}

# The cluster secret bootstrap.sh writes for a deployment with ${1} as its broker provider.
cluster_secret() {
    printf '%s' "${1}" > "${STUB_DIR}/annotation-kafka_provider"
    case "${1}" in
        msk | confluent-cloud | redpanda-cloud) printf external > "${STUB_DIR}/annotation-kafka_mode" ;;
    esac
}

@test "a managed broker skips the in-cluster broker checks, naming its provider" {
    for provider in msk confluent-cloud redpanda-cloud; do
        cluster_secret "${provider}"
        run bash "${SMOKE}"
        [ "${status}" -eq 0 ]
        [[ "${output}" == *"[SKIP] kafka brokers -- managed broker (${provider}), nothing runs in the cluster"* ]]
        [[ "${output}" != *"[FAIL]"* ]]
        [[ "${output}" != *"Kafka CR ready"* ]]
        [[ "${output}" != *"Kafka brokers running"* ]]
        [[ "${output}" == *"Layer 2 data platform is healthy."* ]]
    done
}

@test "a managed broker is never probed for pods or a Kafka CR" {
    cluster_secret redpanda-cloud
    run bash "${SMOKE}"
    [ "${status}" -eq 0 ]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"-n strimzi"* ]]
}

@test "the deploy's own env says managed, so the cluster secret is never read" {
    export DFE_KAFKA_MODE=external DFE_KAFKA_PROVIDER=msk
    run bash "${SMOKE}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"[SKIP] kafka brokers -- managed broker (msk), nothing runs in the cluster"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"get secret dfe-cluster"* ]]
}

@test "a managed broker whose provider is not recorded still skips" {
    printf external > "${STUB_DIR}/annotation-kafka_mode"
    run bash "${SMOKE}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"[SKIP] kafka brokers -- managed broker (unknown), nothing runs in the cluster"* ]]
}

@test "a Strimzi broker is still checked: CR ready and brokers running" {
    cluster_secret strimzi
    touch "${STUB_DIR}/kafka-cr"
    echo strimzi.io/name=dfe-kafka-kafka > "${STUB_DIR}/running-pods"
    run bash "${SMOKE}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"[PASS] Kafka CR ready"* ]]
    [[ "${output}" == *"[PASS] Kafka brokers running"* ]]
    [[ "${output}" != *"[SKIP] kafka brokers"* ]]
}

@test "expected failure: a Strimzi deployment with no broker pod fails" {
    cluster_secret strimzi
    touch "${STUB_DIR}/kafka-cr"
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"[FAIL] Kafka brokers running"* ]]
    [[ "${output}" != *"[SKIP] kafka brokers"* ]]
}

@test "an in-cluster Redpanda broker is still checked" {
    cluster_secret redpanda
    echo app.kubernetes.io/name=dfe-kafka > "${STUB_DIR}/running-pods"
    run bash "${SMOKE}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"[PASS] Kafka brokers running"* ]]
    [[ "${output}" != *"[SKIP] kafka brokers"* ]]
}

@test "expected failure: an in-cluster Redpanda deployment with no broker pod fails" {
    cluster_secret redpanda
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"[FAIL] Kafka brokers running"* ]]
    [[ "${output}" != *"[SKIP] kafka brokers"* ]]
}

@test "expected failure: an unreadable cluster secret leaves the in-cluster check in force" {
    touch "${STUB_DIR}/cluster-secret-unreadable"
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"[FAIL] Kafka brokers running"* ]]
    [[ "${output}" != *"[SKIP] kafka brokers"* ]]
}

@test "a brokerless profile skips on the profile alone" {
    export DFE_PROFILE=slim
    run bash "${SMOKE}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"[SKIP] kafka broker -- not deployed on profile slim"* ]]
    [[ "${output}" != *"managed broker"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"get secret dfe-cluster"* ]]
}
