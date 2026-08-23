#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         smoke-test-data.sh
#  Purpose:      Verify Layer 2 data platform components are PRESENT/Ready.
#                NOTE: these are LIVENESS checks ("pod Running / status Ready") --
#                they do NOT prove the dependency chains work. For real chain/seam
#                verification (ferretdb->PG, hyperdx->ferretdb, receiver->...->
#                ClickHouse) run smoke-test-integration.sh.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
set -uo pipefail

PASS=0
FAIL=0

check() {
    local name="${1}"
    local cmd="${2}"
    if eval "${cmd}" > /dev/null 2>&1; then
        echo "  [PASS] ${name}"
        (( PASS++ )) || true
    else
        echo "  [FAIL] ${name}"
        (( FAIL++ )) || true
    fi
}

skip() { echo "  [SKIP] ${1}"; }

# FerretDB shares the cnpg namespace; there is no ferretdb namespace.
NS_CNPG="${DFE_CNPG_NS:-cnpg}"
NS_KAFKA="${DFE_KAFKA_NS:-strimzi}"
NS_CH="${DFE_CH_NS:-clickhouse}"
NS_OTEL="${DFE_OTEL_NS:-otel}"

# slim and single run the datastores as plain StatefulSets from the charts; only
# scale hands them to their operators, so only scale has CRs and operator labels.
PROFILE="${DFE_PROFILE:-}"

running_pods() {  # ns, label-selector -- at least one Running pod matches
    kubectl -n "${1}" get pods -l "${2}" --field-selector=status.phase=Running \
        -o name 2>/dev/null | grep -qc .
}

echo "=== DFE Layer 2 Data Platform Smoke Test ==="
echo "  profile: ${PROFILE:-unknown}"
echo ""

echo "--- Namespaces ---"
check "cnpg namespace (also hosts FerretDB)" "kubectl get ns ${NS_CNPG}"
check "clickhouse namespace" "kubectl get ns ${NS_CH}"
check "otel namespace" "kubectl get ns ${NS_OTEL}"
if [ "${PROFILE}" = "slim" ]; then
    skip "kafka namespace -- slim is brokerless (receiver feeds loader directly)"
else
    check "kafka namespace" "kubectl get ns ${NS_KAFKA}"
fi

echo ""
echo "--- PostgreSQL (FerretDB's DocumentDB backend) ---"
if kubectl -n "${NS_CNPG}" get cluster.postgresql.cnpg.io dfe-pg >/dev/null 2>&1; then
    check "CNPG cluster healthy" \
      "kubectl -n ${NS_CNPG} get cluster.postgresql.cnpg.io dfe-pg -o jsonpath='{.status.phase}' | grep -q 'healthy state'"
    check "CNPG instances running" "running_pods ${NS_CNPG} cnpg.io/cluster=dfe-pg"
else
    check "documentdb statefulset ready" \
      "kubectl -n ${NS_CNPG} get sts dfe-ferretdb-documentdb -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
fi

echo ""
echo "--- Kafka ---"
if [ "${PROFILE}" = "slim" ]; then
    skip "kafka broker -- slim is brokerless"
elif kubectl -n "${NS_KAFKA}" get kafka dfe-kafka >/dev/null 2>&1; then
    check "Kafka CR ready" \
      "kubectl -n ${NS_KAFKA} get kafka dfe-kafka -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}' | grep -q True"
    check "Kafka brokers running" "running_pods ${NS_KAFKA} strimzi.io/name=dfe-kafka-kafka"
else
    check "Kafka brokers running" "running_pods ${NS_KAFKA} app.kubernetes.io/name=dfe-kafka"
fi

echo ""
echo "--- ClickHouse ---"
check "ClickHouse pods running" "running_pods ${NS_CH} app.kubernetes.io/name=dfe-clickhouse"

echo ""
echo "--- FerretDB ---"
check "FerretDB deployment ready" \
  "kubectl -n ${NS_CNPG} get deploy dfe-ferretdb -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"

echo ""
echo "--- OTel Collector ---"
check "OTel Gateway running" \
  "kubectl -n ${NS_OTEL} get deploy dfe-otel-collector-gateway -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
check "OTel DaemonSet running" \
  "kubectl -n ${NS_OTEL} get daemonset dfe-otel-collector-daemonset -o jsonpath='{.status.numberReady}' | grep -qE '^[1-9]'"

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="

if (( FAIL > 0 )); then
    echo "Layer 2 data platform is NOT healthy."
    exit 1
else
    echo "Layer 2 data platform is healthy."
fi
