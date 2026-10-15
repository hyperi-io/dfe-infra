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
#   pods                      the Running pods in ns/strimzi, one a line: <name> <k=v,k=v labels>.
#                             A -l selector of k=v and !k terms is matched against them for real.
# kubectl.log records every call, so a test can see which ones were never made.
# The pod labels below are what helm template renders: the kafka chart's single-tier
# StatefulSet, and the redpanda chart 26.2.4 the Redpanda operator 26.2.4 reconciles
# from the Redpanda CR (release dfe-kafka). Cruise Control's UI shares the single
# tier's name label, which is why a bare name match is not a broker.
STRIMZI_BROKER='strimzi.io/cluster=dfe-kafka,strimzi.io/name=dfe-kafka-kafka,strimzi.io/broker-role=true'
REDPANDA_BROKER='app.kubernetes.io/name=redpanda,app.kubernetes.io/instance=dfe-kafka,app.kubernetes.io/component=redpanda-statefulset,cluster.redpanda.com/broker=true'
SINGLE_BROKER='app.kubernetes.io/name=dfe-kafka,app.kubernetes.io/instance=kafka'
CRUISE_CONTROL_UI='app.kubernetes.io/name=dfe-kafka,app.kubernetes.io/component=cruise-control-ui'

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    SMOKE="${REPO_ROOT}/bootstrap/smoke-test-data.sh"
    STUB_DIR="${BATS_TEST_TMPDIR}/stub"
    mkdir -p "${STUB_DIR}/bin"
    : > "${STUB_DIR}/kubectl.log"
    : > "${STUB_DIR}/pods"

    cat > "${STUB_DIR}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DIR}/kubectl.log"
args=("$@")
last="${args[${#args[@]} - 1]}"

# Whether a pod's labels (k=v,k=v) satisfy a selector of k=v and !k terms.
pod_matches() {
    local labels=",${1}," term
    local IFS=,
    for term in ${2}; do
        case "${term}" in
            !*) [[ "${labels}" != *",${term#!}="* ]] || return 1 ;;
            *=*) [[ "${labels}" == *",${term},"* ]] || return 1 ;;
            *) return 1 ;;
        esac
    done
}

case "$*" in
    "-n argocd get secret dfe-cluster -o jsonpath="*)
        [[ -f "${STUB_DIR}/cluster-secret-unreadable" ]] && exit 1
        key="${last##*/}"
        cat "${STUB_DIR}/annotation-${key%\}}" 2>/dev/null
        exit 0 ;;
    "-n strimzi get kafka dfe-kafka -o jsonpath="*) echo True; exit 0 ;;
    "-n strimzi get kafka dfe-kafka") [[ -f "${STUB_DIR}/kafka-cr" ]]; exit $? ;;
    "-n strimzi get pods -l "*)
        while read -r name labels; do
            if [[ -n "${name}" ]] && pod_matches "${labels}" "${args[5]}"; then echo "pod/${name}"; fi
        done < "${STUB_DIR}/pods"
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

# A Running pod ${1} in ns/strimzi carrying the labels ${2}.
add_pod() {
    printf '%s %s\n' "${1}" "${2}" >> "${STUB_DIR}/pods"
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
    add_pod dfe-kafka-dfe-kafka-pool-0 "${STRIMZI_BROKER}"
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

@test "an in-cluster Redpanda broker is found by the operator's broker label" {
    cluster_secret redpanda
    add_pod dfe-kafka-0 "${REDPANDA_BROKER}"
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

@test "a single-tier broker is found by its name label" {
    export DFE_PROFILE=single
    add_pod dfe-kafka-0 "${SINGLE_BROKER}"
    run bash "${SMOKE}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"[PASS] Kafka brokers running"* ]]
}

@test "expected failure: a pod that only shares the single tier's name label is not a broker" {
    add_pod dfe-kafka-cruise-control-ui-c564c4844-kbg6h "${CRUISE_CONTROL_UI}"
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"[FAIL] Kafka brokers running"* ]]
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
