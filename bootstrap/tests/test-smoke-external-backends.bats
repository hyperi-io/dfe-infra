#!/usr/bin/env bats
# smoke-test-integration.sh against an external ClickHouse and a managed broker.
# An external ClickHouse has no pod to exec into, so every query runs from the
# engine's own container with the engine's own host, port, scheme and credential,
# and the server counts as one node. A managed broker has no broker pod, so CORE 3
# runs from a short-lived Kafka client pod in the app namespace that mounts
# dfe-kafka-user and speaks SASL_SSL.
# No ClickHouse password may reach a command line on either side of kubectl: the
# in-cluster one rides stdin into clickhouse-client's environment, the engine's
# rides stdin into curl's config.
# kubectl is a stub that answers from files in STUB_DIR:
#   annotation-<key>     the dfe-cluster annotation dfe.hyperi.io/<key>
#   engine-env           the engine container's environment, one KEY=VALUE a line;
#                        an engine exec of `sh -c` runs its script under exactly
#                        that environment, with pod-bin/curl standing in for curl
#   ch-unreachable       pod-bin/curl exits 7, as on a server it cannot reach
#   ch-secret            the clickhouse-admin-password Secret's password; absent,
#                        the Secret reads empty
#   ch-login             the only user the in-cluster server accepts
#   receiver-down        every POST to the receiver fails, so CORE 2 posts nothing
#   strimzi             ns/strimzi exists with one Running broker pod
#   client-never-ready   the client pod never reports Ready
#   cred/                the mounted dfe-kafka-user Secret's files
#   groups-output        what kafka-consumer-groups.sh prints. Absent, a dfe-loader
#                        row committed at 5 on main_land
#   engine-offsets       what dfe-engine's python3 prints for the OffsetFetch fallback.
#                        Absent, it prints a librdkafka error and exits 1
# An engine exec of `python3 -` runs pod-bin/python3 under engine-env, which logs its
# argv to python.log, the DFE_KAFKA_SASL_PASSWORD it was handed to python-env.log and
# the script it read to python-script.py.
# An exec into dfe-clickhouse-0 runs its command on this host as the pod would,
# with ch-bin/clickhouse-client logging its argv to clickhouse-client.log and its
# CLICKHOUSE_PASSWORD to clickhouse-env.log. pod-bin/curl appends the config it
# reads on stdin to curl-config.log.
# A `sh -s` exec into a Kafka pod runs its script on this host, with the credential
# mount and /opt/kafka/bin pointed at cred/ and kafka-bin/; each fake tool appends
# its --command-config file to props.log. kubectl.log records every call and
# curl.log every curl the engine container ran. No cluster is involved.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    SMOKE="${REPO_ROOT}/bootstrap/smoke-test-integration.sh"
    STUB_DIR="${BATS_TEST_TMPDIR}/stub"
    mkdir -p "${STUB_DIR}/bin" "${STUB_DIR}/pod-bin" "${STUB_DIR}/ch-bin" "${STUB_DIR}/kafka-bin" "${STUB_DIR}/cred"
    : > "${STUB_DIR}/kubectl.log"
    : > "${STUB_DIR}/curl.log"
    : > "${STUB_DIR}/curl-config.log"
    : > "${STUB_DIR}/clickhouse-client.log"
    : > "${STUB_DIR}/clickhouse-env.log"
    : > "${STUB_DIR}/props.log"
    : > "${STUB_DIR}/kafka.log"
    echo 0 > "${STUB_DIR}/posted"
    echo 0 > "${STUB_DIR}/posted_raw"
    printf '%s' fake-ch-admin-pw > "${STUB_DIR}/ch-secret"

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
body=-
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
    case "${args[i]}" in
        -K | --config) [[ "${args[i + 1]}" == - ]] && cat >> "${STUB_DIR}/curl-config.log" ;;
        --data-binary) body="${args[i + 1]#@}" ;;
    esac
done
[[ "${body}" == - ]] && body=/dev/stdin
[[ -f "${STUB_DIR}/ch-unreachable" ]] && exit 7
exec "${STUB_DIR}/bin/ch-answer" < "${body}"
STUB

    cat > "${STUB_DIR}/ch-bin/clickhouse-client" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DIR}/clickhouse-client.log"
printf '%s\n' "${CLICKHOUSE_PASSWORD-(unset)}" >> "${STUB_DIR}/clickhouse-env.log"
user=default
query=
while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) user="$2"; shift ;;
        --query) query="$2"; shift ;;
    esac
    shift
done
if [[ -f "${STUB_DIR}/ch-login" && "${user}" != "$(cat "${STUB_DIR}/ch-login")" ]]; then
    echo "Code: 516. ${user}: Authentication failed" >&2
    exit 4
fi
printf '%s' "${query}" | exec "${STUB_DIR}/bin/ch-answer"
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
        if [[ -f "${STUB_DIR}/groups-output" ]]; then
            cat "${STUB_DIR}/groups-output"
        else
            echo "GROUP TOPIC PARTITION CURRENT-OFFSET LOG-END-OFFSET LAG"
            echo "dfe-loader main_land 0 5 5 0"
        fi ;;
esac
STUB

    cat > "${STUB_DIR}/pod-bin/python3" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DIR}/python.log"
printf '%s\n' "${DFE_KAFKA_SASL_PASSWORD-(unset)}" >> "${STUB_DIR}/python-env.log"
cat > "${STUB_DIR}/python-script.py"
if [[ -f "${STUB_DIR}/engine-offsets" ]]; then
    cat "${STUB_DIR}/engine-offsets"
    exit 0
fi
echo 'Error: KafkaException: KafkaError{code=_TRANSPORT,val=-195,str="FindCoordinator response error: Local: Broker transport failure"}'
exit 1
STUB
    : > "${STUB_DIR}/python.log"
    chmod +x "${STUB_DIR}/bin/ch-answer" "${STUB_DIR}/pod-bin/curl" "${STUB_DIR}/pod-bin/python3" "${STUB_DIR}/ch-bin/clickhouse-client" "${STUB_DIR}/kafka-bin/tool"
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

# The command after `--`, run here as the pod would run it, with only ${1} and the system on PATH.
run_in_pod() {
    local i
    for ((i = 0; i < ${#args[@]}; i++)); do
        [[ "${args[i]}" == -- ]] && break
    done
    exec env -i PATH="$1:/usr/bin:/bin" STUB_DIR="${STUB_DIR}" "${args[@]:i+1}"
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
    *" exec -i deploy/dfe-engine -c dfe-engine -- python3 - "*)
        pod_env=()
        if [[ -f "${STUB_DIR}/engine-env" ]]; then
            while IFS= read -r line; do
                [[ -n "${line}" ]] && pod_env+=("${line}")
            done < "${STUB_DIR}/engine-env"
        fi
        for ((i = 0; i < ${#args[@]}; i++)); do
            [[ "${args[i]}" == -- ]] && break
        done
        exec env -i PATH="${STUB_DIR}/pod-bin:/usr/bin:/bin" STUB_DIR="${STUB_DIR}" "${pod_env[@]}" "${args[@]:i+1}" ;;
    *" exec -i deploy/dfe-receiver -- curl "*)
        body="$(cat)"
        [[ -f "${STUB_DIR}/receiver-down" ]] && exit 1
        echo $(( $(cat "${STUB_DIR}/posted") + 1 )) > "${STUB_DIR}/posted"
        if [[ "${body}" == *'"_raw"'* ]]; then
            echo $(( $(cat "${STUB_DIR}/posted_raw") + 1 )) > "${STUB_DIR}/posted_raw"
        fi
        exit 0 ;;
    "-n clickhouse get pods "*) echo dfe-clickhouse-0; exit 0 ;;
    "-n clickhouse get secret "*)
        [[ -f "${STUB_DIR}/ch-secret" ]] && base64 < "${STUB_DIR}/ch-secret"
        exit 0 ;;
    "-n clickhouse exec "*"dfe-clickhouse-0 -- "*) run_in_pod "${STUB_DIR}/ch-bin" ;;
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
    unset DFE_CLICKHOUSE_MODE DFE_KAFKA_MODE DFE_KAFKA_BOOTSTRAP DFE_KAFKA_CLI_IMAGE DFE_PROFILE DFE_CH_USER DFE_CH_PASSWORD
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
        DFE_CLICKHOUSE_PASSWORD="${3:-pw-held-by-the-engine}" > "${STUB_DIR}/engine-env"
}

# How many lines of log ${1} match the fixed string ${2}.
count_of() {
    grep -cF -- "${2}" "${STUB_DIR}/${1}" || true
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

@test "the engine's own credential reaches curl as config on stdin, never a command line on either side" {
    external_clickhouse
    run bash "${SMOKE}"
    [[ "${output}" == *"Vertical integration verified."* ]]
    [[ "$(cat "${STUB_DIR}/curl-config.log")" == *'user = "default:pw-held-by-the-engine"'* ]]
    [ -s "${STUB_DIR}/curl.log" ]
    [[ "$(cat "${STUB_DIR}/curl.log")" != *"pw-held-by-the-engine"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"pw-held-by-the-engine"* ]]
    [[ "${output}" != *"pw-held-by-the-engine"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"clickhouse-admin-password"* ]]
}

@test "an engine credential carrying quotes and backslashes is escaped for curl's config" {
    external_clickhouse 8443 true 'fake"pw\x'
    run bash "${SMOKE}"
    [[ "${output}" == *"Vertical integration verified."* ]]
    [[ "$(cat "${STUB_DIR}/curl-config.log")" == *'user = "default:fake\"pw\\x"'* ]]
    [[ "$(cat "${STUB_DIR}/curl.log")" != *'fake"pw'* ]]
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
    [[ "$(cat "${STUB_DIR}/clickhouse-client.log")" == *"--query SELECT count() FROM dfe.main WHERE "* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"deploy/dfe-engine -c dfe-engine -- sh -c"* ]]
    [ ! -s "${STUB_DIR}/curl.log" ]
}

@test "an in-cluster ClickHouse password reaches clickhouse-client in its environment, never a command line" {
    run bash "${SMOKE}"
    [[ "${output}" == *"Vertical integration verified."* ]]
    [ -s "${STUB_DIR}/clickhouse-client.log" ]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"fake-ch-admin-pw"* ]]
    [[ "$(cat "${STUB_DIR}/clickhouse-client.log")" != *"fake-ch-admin-pw"* ]]
    [[ "${output}" != *"fake-ch-admin-pw"* ]]
    [ "$(sort -u "${STUB_DIR}/clickhouse-env.log")" = fake-ch-admin-pw ]
}

@test "ClickHouse auth is resolved once per run, not once per query" {
    run bash "${SMOKE}"
    [[ "${output}" == *"Vertical integration verified."* ]]
    [ "$(count_of kubectl.log 'get secret clickhouse-admin-password')" -eq 1 ]
    [ "$(count_of clickhouse-client.log '--query SELECT 1')" -eq 1 ]
    [ "$(count_of clickhouse-client.log '--user default --query SELECT count()')" -gt 3 ]
}

@test "the admin login is found when default is refused, resolved once and off every command line" {
    echo admin > "${STUB_DIR}/ch-login"
    run bash "${SMOKE}"
    [[ "${output}" == *"Vertical integration verified."* ]]
    [ "$(count_of kubectl.log 'get secret clickhouse-admin-password')" -eq 1 ]
    [ "$(count_of clickhouse-client.log '--user default --query SELECT 1')" -eq 1 ]
    [ "$(count_of clickhouse-client.log '--user admin --query SELECT 1')" -eq 1 ]
    # Every call but the refused default probe logs in as admin.
    [ "$(count_of clickhouse-client.log '--user admin --query ')" -eq "$(( $(wc -l < "${STUB_DIR}/clickhouse-client.log") - 1 ))" ]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"fake-ch-admin-pw"* ]]
    [ "$(sort -u "${STUB_DIR}/clickhouse-env.log")" = fake-ch-admin-pw ]
}

@test "DFE_CH_USER and DFE_CH_PASSWORD ride the same stdin path and skip the Secret" {
    export DFE_CH_USER=reader DFE_CH_PASSWORD=fake-explicit-pw
    run bash "${SMOKE}"
    [[ "${output}" == *"Vertical integration verified."* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"get secret clickhouse-admin-password"* ]]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"fake-explicit-pw"* ]]
    [[ "$(cat "${STUB_DIR}/clickhouse-client.log")" != *"fake-explicit-pw"* ]]
    [ "$(sort -u "${STUB_DIR}/clickhouse-env.log")" = fake-explicit-pw ]
    [ "$(count_of clickhouse-client.log '--user reader --query ')" -eq "$(wc -l < "${STUB_DIR}/clickhouse-client.log")" ]
}

@test "with no admin Secret the default login runs with no password set" {
    rm "${STUB_DIR}/ch-secret"
    run bash "${SMOKE}"
    [[ "${output}" == *"Vertical integration verified."* ]]
    [ "$(sort -u "${STUB_DIR}/clickhouse-env.log")" = "(unset)" ]
    [[ "$(cat "${STUB_DIR}/clickhouse-client.log")" != *"--user"* ]]
}

@test "a managed broker's seam is proven from a client pod in the app namespace that mounts dfe-kafka-user" {
    managed_kafka
    run bash "${SMOKE}"
    [[ "${output}" == *"[PASS] topic main_land exists (created)"* ]]
    [[ "${output}" == *"[PASS] topic main_land has messages (receiver PRODUCED)"* ]]
    [[ "${output}" == *"[PASS] consumer group dfe-loader is committed on main_land (loader CONSUMED): offset 5, read by kafka-consumer-groups.sh"* ]]
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

# What kafka-consumer-groups.sh 4.3.1 printed against MSK Express in a cloud cycle, verbatim.
msk_groups_error() {
    printf '\n%s\n' "Error: Executing consumer group command failed due to java.util.concurrent.ExecutionException: org.apache.kafka.common.errors.UnknownServerException: The server experienced an unexpected error when processing the request." > "${STUB_DIR}/groups-output"
}

GROUP_CHECK="consumer group dfe-loader is committed on main_land (loader CONSUMED)"

@test "the group check describes the loader's own group, never every group" {
    managed_kafka
    run bash "${SMOKE}"
    [[ "${output}" == *"[PASS] ${GROUP_CHECK}: offset 5, read by kafka-consumer-groups.sh"* ]]
    [[ "$(cat "${STUB_DIR}/kafka.log")" == *"kafka-consumer-groups.sh --bootstrap-server b-1.kafka.example.com:9096,b-2.kafka.example.com:9096 --command-config "*" --describe --group dfe-loader --offsets"* ]]
    [[ "$(cat "${STUB_DIR}/kafka.log")" != *"--all-groups"* ]]
    [ ! -s "${STUB_DIR}/python.log" ]
}

@test "expected failure: MSK's UnknownServerException FAILs the group check with its first line, when the fallback fails too" {
    managed_kafka
    msk_groups_error
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"[FAIL] ${GROUP_CHECK}"$'\n'"    kafka-consumer-groups.sh: Error: Executing consumer group command failed due to java.util.concurrent.ExecutionException: org.apache.kafka.common.errors.UnknownServerException: The server experienced an unexpected error when processing the request."$'\n'"    dfe-engine OffsetFetch: Error: KafkaException: "* ]]
    [[ "${output}" != *"[PASS] ${GROUP_CHECK}"* ]]
}

@test "a groups read that errors falls back to OffsetFetch from dfe-engine, which passes on a committed offset" {
    managed_kafka
    msk_groups_error
    printf 'committed 3 over 3 partition(s) of main_land\n' > "${STUB_DIR}/engine-offsets"
    printf '%s\n' DFE_KAFKA_SASL_PASSWORD=engine-kafka-pw > "${STUB_DIR}/engine-env"
    run bash "${SMOKE}"
    [[ "${output}" == *"[PASS] ${GROUP_CHECK}: offset 3, read by librdkafka OffsetFetch from dfe-engine"$'\n'"    kafka-consumer-groups.sh failed first: Error: Executing consumer group command failed due to "* ]]
    [[ "${output}" == *"Vertical integration verified."* ]]
    [ "$(cat "${STUB_DIR}/python.log")" = "- dfe-loader main_land" ]
    [[ "$(cat "${STUB_DIR}/python-script.py")" == *"list_consumer_group_offsets"* ]]
    # The engine's own env holds its credential, and no command line on either side carries it.
    [ "$(cat "${STUB_DIR}/python-env.log")" = engine-kafka-pw ]
    [[ "$(cat "${STUB_DIR}/kubectl.log")" != *"engine-kafka-pw"* ]]
    [[ "${output}" != *"engine-kafka-pw"* ]]
}

@test "expected failure: a fallback that finds no committed offset above 0 stays a FAIL" {
    managed_kafka
    msk_groups_error
    printf 'committed 0 over 0 partition(s) of main_land\n' > "${STUB_DIR}/engine-offsets"
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"[FAIL] ${GROUP_CHECK}"* ]]
    [[ "${output}" == *"    dfe-engine OffsetFetch: committed 0 over 0 partition(s) of main_land"* ]]
}

@test "expected failure: another group's commit on main_land does not pass the loader's check" {
    managed_kafka
    printf '%s\n' "GROUP TOPIC PARTITION CURRENT-OFFSET LOG-END-OFFSET LAG" "dfe-archiver main_land 0 5 5 0" > "${STUB_DIR}/groups-output"
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"[FAIL] ${GROUP_CHECK}"$'\n'"    kafka-consumer-groups.sh, no offset above 0 for dfe-loader: GROUP TOPIC PARTITION"* ]]
    # The tool answered, so its answer stands: no fallback is asked.
    [ ! -s "${STUB_DIR}/python.log" ]
}

@test "expected failure: a credential in the line a FAIL prints is masked" {
    managed_kafka
    printf '%s\n' 'Error: bad client config sasl.jaas.config=ScramLoginModule required username="u" password="leak-pw";' > "${STUB_DIR}/groups-output"
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"    kafka-consumer-groups.sh: Error: bad client config sasl.jaas.config="* ]]
    [[ "${output}" == *"***REDACTED***"* ]]
    [[ "${output}" != *"leak-pw"* ]]
}

@test "expected failure: any failed check prints a line of what its command said" {
    external_clickhouse
    touch "${STUB_DIR}/ch-unreachable" "${STUB_DIR}/receiver-down"
    run bash "${SMOKE}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"[FAIL] infra OTel logs landing fresh"*$'\n'"    output: "* ]]
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
