#!/usr/bin/env bats
# smoke-test-integration.sh against an external ClickHouse and a managed broker.
# An external ClickHouse has no pod to exec into, so every query runs from the
# engine's own container with the engine's own host, port, scheme and credential,
# and the server counts as one node. A managed broker has no broker pod, so CORE 3
# runs from a short-lived Kafka client pod in the app namespace that mounts
# dfe-kafka-user and speaks SASL_SSL.
# kubectl is a stub that answers from files in STUB_DIR:
#   annotation-<key>     the dfe-cluster annotation dfe.hyperi.io/<key>
#   engine-env           the engine container's environment, one KEY=VALUE a line;
#                        an engine exec of `sh -c` runs its script under exactly
#                        that environment, with pod-bin/curl standing in for curl
#   ch-unreachable       pod-bin/curl exits 7, as on a server it cannot reach
#   receiver-down        every POST to the receiver fails, so CORE 2 posts nothing
#   strimzi             ns/strimzi exists with one Running broker pod
#   client-never-ready   the client pod never reports Ready
#   cred/                the mounted dfe-kafka-user Secret's files
# A `sh -s` exec into a Kafka pod runs its script on this host, with the credential
# mount and /opt/kafka/bin pointed at cred/ and kafka-bin/; each fake tool appends
# its --command-config file to props.log. kubectl.log records every call and
# curl.log every curl the engine container ran. No cluster is involved.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    SMOKE="${REPO_ROOT}/bootstrap/smoke-test-integration.sh"
    STUB_DIR="${BATS_TEST_TMPDIR}/stub"
    mkdir -p "${STUB_DIR}/bin" "${STUB_DIR}/pod-bin" "${STUB_DIR}/kafka-bin" "${STUB_DIR}/cred"
    : > "${STUB_DIR}/kubectl.log"
    : > "${STUB_DIR}/curl.log"
    : > "${STUB_DIR}/props.log"
    : > "${STUB_DIR}/kafka.log"
    echo 0 > "${STUB_DIR}/posted"
    echo 0 > "${STUB_DIR}/posted_raw"

    # One answer per query shape, shared by clickhouse-client and the engine's curl.
    cat > "${STUB_DIR}/bin/ch-answer" <<'STUB'
#!/usr/bin/env bash
q="$(cat)"
posted="$(cat "${STUB_DIR}/posted")"
posted_raw="$(cat "${STUB_DIR}/posted_raw")"
case "${q}" in
    *otel_logs*) echo 1 ;;
    "SHOW DATABASES") printf 'default\ndfe\nsystem\n' ;;
    *"ALTER TABLE"*) ;;
    *"_raw IS NULL"*) echo $((posted - posted_raw)) ;;
    *"length(_raw)"*) echo "${posted_raw}" ;;
    *LIKE*) echo "${posted}" ;;
    *) echo 1 ;;
esac
STUB

    cat > "${STUB_DIR}/pod-bin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DIR}/curl.log"
[[ -f "${STUB_DIR}/ch-unreachable" ]] && exit 7
exec "${STUB_DIR}/bin/ch-answer"
STUB

    # Every Kafka CLI the smoke runs, by the name it was called under.
    cat > "${STUB_DIR}/kafka-bin/tool" <<'STUB'
#!/usr/bin/env bash
tool="$(basename "$0")"
printf '%s %s\n' "${tool}" "$*" >> "${STUB_DIR}/kafka.log"
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
    [[ "${args[i]}" == --command-config ]] && cat "${args[i + 1]}" >> "${STUB_DIR}/props.log"
done
case "${tool} $*" in
    "kafka-topics.sh "*--list*)
        printf '%s\n' main_land dfe_receiver_dlq dfe_loader_dlq dfe_archiver_dlq dfe_fetcher_dlq dfe_transform_dlq ;;
    "kafka-topics.sh "*--describe*)
        printf 'Topic: dfe_loader_dlq\tPartitionCount: 3\tConfigs: retention.ms=604800000\n' ;;
    "kafka-get-offsets.sh "*) echo "main_land:0:5" ;;
    "kafka-consumer-groups.sh "*)
        echo "GROUP TOPIC PARTITION CURRENT-OFFSET LOG-END-OFFSET LAG"
        echo "dfe-loader main_land 0 5 5 0" ;;
esac
STUB
    chmod +x "${STUB_DIR}/bin/ch-answer" "${STUB_DIR}/pod-bin/curl" "${STUB_DIR}/kafka-bin/tool"
    for tool in kafka-topics.sh kafka-get-offsets.sh kafka-consumer-groups.sh; do
        ln -s tool "${STUB_DIR}/kafka-bin/${tool}"
    done

    cat > "${STUB_DIR}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DIR}/kubectl.log"
args=("$@")
last="${args[${#args[@]} - 1]}"

# A Kafka pod's script, run here against the stub's credential and tools.
run_kafka_script() {
    local script
    script="$(cat)"
    script="${script//\/etc\/dfe-kafka/${STUB_DIR}/cred}"
    script="${script//\/opt\/kafka\/bin/${STUB_DIR}/kafka-bin}"
    printf '%s\n' "${script}" | env TMPDIR="${STUB_DIR}" sh -s
}

case "$*" in
    "-n argocd get secret dfe-cluster -o jsonpath="*)
        key="${last##*/}"
        cat "${STUB_DIR}/annotation-${key%\}}" 2>/dev/null
        exit 0 ;;
    *" get deploy dfe-engine") exit 0 ;;
    *" exec deploy/dfe-engine -c dfe-engine -- curl "*)
        echo '{"status": "ready", "checks": {"clickhouse": true, "schema": true}}'
        exit 0 ;;
    *" exec -i deploy/dfe-engine -c dfe-engine -- sh -c "*)
        pod_env=()
        while IFS= read -r line; do
            [[ -n "${line}" ]] && pod_env+=("${line}")
        done < "${STUB_DIR}/engine-env"
        exec env -i PATH="${STUB_DIR}/pod-bin:/usr/bin:/bin" STUB_DIR="${STUB_DIR}" "${pod_env[@]}" sh -c "${last}" ;;
    *" exec -i deploy/dfe-receiver -- curl "*)
        body="$(cat)"
        [[ -f "${STUB_DIR}/receiver-down" ]] && exit 1
        echo $(( $(cat "${STUB_DIR}/posted") + 1 )) > "${STUB_DIR}/posted"
        if [[ "${body}" == *'"_raw"'* ]]; then
            echo $(( $(cat "${STUB_DIR}/posted_raw") + 1 )) > "${STUB_DIR}/posted_raw"
        fi
        exit 0 ;;
    "-n clickhouse get pods "*) echo dfe-clickhouse-0; exit 0 ;;
    "-n clickhouse get secret "*) printf 'Y2gtcHc='; exit 0 ;;
    "-n clickhouse exec dfe-clickhouse-0 -- clickhouse-client "*)
        printf '%s' "${last}" | "${STUB_DIR}/bin/ch-answer"
        exit 0 ;;
    "get ns strimzi") [[ -f "${STUB_DIR}/strimzi" ]]; exit $? ;;
    "-n strimzi get pods -l strimzi.io/broker-role=true "*)
        echo "dfe-kafka-broker-0 Running"; exit 0 ;;
    "-n strimzi get pods "*) exit 0 ;;
    "-n strimzi get secret dfe-kafka-user "*) printf 'YnJva2VyLXB3'; exit 0 ;;
    "-n strimzi exec dfe-kafka-broker-0 -- sh -c command -v rpk") exit 1 ;;
    "-n strimzi exec -i dfe-kafka-broker-0 -- sh -s") run_kafka_script; exit $? ;;
    "apply -f -") cat > "${STUB_DIR}/applied.yaml"; exit 0 ;;
    "-n dfe-test wait --for=condition=Ready pod/"*)
        [[ -f "${STUB_DIR}/client-never-ready" ]] && exit 1
        exit 0 ;;
    "-n dfe-test exec -i dfe-smoke-"*"-kafka -- sh -s") run_kafka_script; exit $? ;;
    "-n dfe-test delete pod,networkpolicy -l dfe.hyperi.io/smoke-run="*) exit 0 ;;
esac
exit 1
STUB
    chmod +x "${STUB_DIR}/bin/kubectl"
    export STUB_DIR
    export PATH="${STUB_DIR}/bin:${PATH}"
    export DFE_NS=dfe-test
    export DFE_ENGINE_WAIT=0
    export DFE_OTEL_WAIT=0
    unset DFE_CLICKHOUSE_MODE DFE_KAFKA_MODE DFE_KAFKA_BOOTSTRAP DFE_KAFKA_CLI_IMAGE DFE_PROFILE
}

# The cluster secret bootstrap.sh writes for ClickHouse Cloud, and the engine env layer2-apps renders for it.
external_clickhouse() {
    echo external > "${STUB_DIR}/annotation-clickhouse_mode"
    printf '%s\n' \
        DFE_CLICKHOUSE_HOST=ch.example.com \
        DFE_CLICKHOUSE_PORT="${1:-8443}" \
        DFE_CLICKHOUSE_SECURE="${2:-true}" \
        DFE_CLICKHOUSE_VERIFY=true \
        DFE_CLICKHOUSE_USERNAME=default \
        DFE_CLICKHOUSE_PASSWORD=pw-held-by-the-engine > "${STUB_DIR}/engine-env"
}

# A managed broker with ${1} as dfe-kafka-user's mechanism.
managed_kafka() {
    echo external > "${STUB_DIR}/annotation-kafka_mode"
    echo "b-1.kafka.example.com:9096,b-2.kafka.example.com:9096" > "${STUB_DIR}/annotation-kafka_bootstrap"
    printf '%s' dfe-kafka-user > "${STUB_DIR}/cred/username"
    printf '%s' 'p"w\d' > "${STUB_DIR}/cred/password"
    printf '%s' "${1:-SCRAM-SHA-512}" > "${STUB_DIR}/cred/sasl.mechanism"
    export DFE_PROFILE=scale
}

@test "an external ClickHouse is queried from the engine container over https, as one node" {
    external_clickhouse
    run bash "${SMOKE}"
    [[ "${output}" == *"[PASS] fixture events posted to receiver land in dfe.main on EVERY ClickHouse node"* ]]
    [[ "${output}" =~ "    external: "[1-9] ]]
    [[ "${output}" == *"[PASS] infra OTel logs landing fresh"* ]]
    [[ "${output}" == *"[PASS] ClickHouse answers a query (dfe DB present)"* ]]
    [[ "${output}" == *"Vertical integration verified."* ]]
    [[ "$(cat "${STUB_DIR}/curl.log")" == *"https://ch.example.com:8443/"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"-n clickhouse"* ]]
}

@test "the engine's own credential carries the query and never reaches a host-side command line" {
    external_clickhouse
    run bash "${SMOKE}"
    [[ "$(cat "${STUB_DIR}/curl.log")" == *"--user default:pw-held-by-the-engine"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"pw-held-by-the-engine"* ]]
    [[ "${output}" != *"pw-held-by-the-engine"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"clickhouse-admin-password"* ]]
}

@test "a plaintext external server is dialled over http on its own port" {
    external_clickhouse 8123 false
    run bash "${SMOKE}"
    [[ "$(cat "${STUB_DIR}/curl.log")" == *"http://ch.example.com:8123/"* ]]
    [[ "$(cat "${STUB_DIR}/curl.log")" != *"https://"* ]]
    [[ "${output}" == *"Vertical integration verified."* ]]
}

@test "DFE_CLICKHOUSE_MODE from the deploy's env needs no cluster secret" {
    external_clickhouse
    rm "${STUB_DIR}/annotation-clickhouse_mode"
    export DFE_CLICKHOUSE_MODE=external
    run bash "${SMOKE}"
    [[ "$(cat "${STUB_DIR}/curl.log")" == *"https://ch.example.com:8443/"* ]]
    [[ "${output}" == *"Vertical integration verified."* ]]
}

@test "expected failure: an external server the engine cannot reach fails every ClickHouse check" {
    external_clickhouse
    touch "${STUB_DIR}/ch-unreachable"
    # Nothing posted skips CORE 2's 90s landing poll; its typed read still asks the server.
    touch "${STUB_DIR}/receiver-down"
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"[FAIL] infra OTel logs landing fresh"* ]]
    [[ "${output}" == *"[FAIL] native JSON typed sub-column reads back"* ]]
    [[ "${output}" == *"[FAIL] ClickHouse answers a query (dfe DB present)"* ]]
    [[ "$(cat "${STUB_DIR}/curl.log")" == *"https://ch.example.com:8443/"* ]]
}

@test "an in-cluster ClickHouse is still queried pod by pod with clickhouse-client" {
    run bash "${SMOKE}"
    [[ "${output}" =~ "    dfe-clickhouse-0: "[1-9] ]]
    [[ "${output}" == *"Vertical integration verified."* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" == *"-n clickhouse exec dfe-clickhouse-0 -- clickhouse-client"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"-- sh -c"*"DFE_CLICKHOUSE"* ]]
    [ ! -s "${STUB_DIR}/curl.log" ]
}

@test "a managed broker's seam is proven from a client pod in the app namespace that mounts dfe-kafka-user" {
    managed_kafka
    run bash "${SMOKE}"
    [[ "${output}" == *"[PASS] topic main_land exists (created)"* ]]
    [[ "${output}" == *"[PASS] topic main_land has messages (receiver PRODUCED)"* ]]
    [[ "${output}" == *"[PASS] a consumer group is committed on main_land (loader CONSUMED)"* ]]
    [[ "${output}" == *"[PASS] DLQ topic dfe_transform_dlq pre-created"* ]]
    [[ "${output}" == *"[PASS] DLQ retention is longer than the data-topic default"* ]]
    [[ "${output}" == *"Vertical integration verified."* ]]
    manifest="$(cat "${STUB_DIR}/applied.yaml")"
    [[ "${manifest}" == *"kind: Pod"* ]]
    [[ "${manifest}" == *"namespace: dfe-test"* ]]
    [[ "${manifest}" == *"secretName: dfe-kafka-user"* ]]
    [[ "${manifest}" == *"kind: NetworkPolicy"* ]]
    [ "$(grep -c -- '- port: 9096' "${STUB_DIR}/applied.yaml")" -eq 1 ]
    [[ "$(cat "${STUB_DIR}/kafka.log")" == *"--bootstrap-server b-1.kafka.example.com:9096,b-2.kafka.example.com:9096"* ]]
    [[ "$(cat "${STUB_DIR}/kafka.log")" != *"localhost:9092"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" == *"-n dfe-test delete pod,networkpolicy -l dfe.hyperi.io/smoke-run=smoke-"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"-n strimzi"* ]]
}

@test "the client authenticates over SASL_SSL with the mounted credential, escaped, never on a host command line" {
    managed_kafka
    run bash "${SMOKE}"
    props="$(cat "${STUB_DIR}/props.log")"
    [[ "${props}" == *"security.protocol=SASL_SSL"* ]]
    [[ "${props}" == *"sasl.mechanism=SCRAM-SHA-512"* ]]
    [[ "${props}" == *'ScramLoginModule required username="dfe-kafka-user" password="p\"w\\d";'* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *'p"w'* ]]
    [[ "$(cat "${STUB_DIR}/applied.yaml")" != *'p"w'* ]]
    [[ "${output}" != *'p"w'* ]]
}

@test "a PLAIN credential, as Confluent Cloud issues, takes the plain login module" {
    managed_kafka PLAIN
    run bash "${SMOKE}"
    [[ "$(cat "${STUB_DIR}/props.log")" == *"sasl.mechanism=PLAIN"* ]]
    [[ "$(cat "${STUB_DIR}/props.log")" == *"PlainLoginModule required"* ]]
    [[ "${output}" == *"[PASS] topic main_land exists (created)"* ]]
}

@test "the client runs the image DFE_KAFKA_CLI_IMAGE names" {
    managed_kafka
    export DFE_KAFKA_CLI_IMAGE=registry.example.com/kafka:9.9.9@sha256:0000
    run bash "${SMOKE}"
    [[ "$(cat "${STUB_DIR}/applied.yaml")" == *"image: registry.example.com/kafka:9.9.9@sha256:0000"* ]]
}

@test "expected failure: a client pod that never becomes Ready fails CORE 3 and is still removed" {
    managed_kafka
    touch "${STUB_DIR}/client-never-ready"
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"[FAIL] managed-broker client pod Ready in ns/dfe-test"* ]]
    [[ "${output}" != *"[SKIP] kafka seam"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" == *"-n dfe-test delete pod,networkpolicy -l dfe.hyperi.io/smoke-run=smoke-"* ]]
}

@test "expected failure: a managed broker with no bootstrap address fails CORE 3 rather than skipping" {
    managed_kafka
    rm "${STUB_DIR}/annotation-kafka_bootstrap"
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"[FAIL] managed-broker client pod Ready in ns/dfe-test"* ]]
    [ ! -f "${STUB_DIR}/applied.yaml" ]
}

@test "an in-cluster broker is still reached in ns/strimzi over SASL_PLAINTEXT" {
    touch "${STUB_DIR}/strimzi"
    export DFE_PROFILE=scale
    run bash "${SMOKE}"
    [[ "${output}" == *"[PASS] topic main_land exists (created)"* ]]
    [[ "${output}" == *"Vertical integration verified."* ]]
    [[ "$(cat "${STUB_DIR}/props.log")" == *"security.protocol=SASL_PLAINTEXT"* ]]
    [[ "$(cat "${STUB_DIR}/kafka.log")" == *"--bootstrap-server localhost:9092"* ]]
    [ ! -f "${STUB_DIR}/applied.yaml" ]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"delete pod,networkpolicy"* ]]
}
