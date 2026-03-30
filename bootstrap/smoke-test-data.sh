#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         smoke-test-data.sh
#  Purpose:      Verify Layer 2 data platform components are healthy
#  Language:     Bash
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
set -euo pipefail

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

echo "=== DFE Layer 2 Data Platform Smoke Test ==="
echo ""

echo "--- Namespaces ---"
check "cnpg namespace" "kubectl get ns cnpg"
check "strimzi namespace" "kubectl get ns strimzi"
check "clickhouse namespace" "kubectl get ns clickhouse"
check "ferretdb namespace" "kubectl get ns ferretdb"
check "otel namespace" "kubectl get ns otel"
check "hyperdx namespace" "kubectl get ns hyperdx"

echo ""
echo "--- CNPG PostgreSQL ---"
check "CNPG cluster ready" "kubectl -n cnpg get cluster dfe-pg -o jsonpath='{.status.phase}' | grep -q 'Cluster in healthy state'"
check "CNPG instances running" "kubectl -n cnpg get pods -l cnpg.io/cluster=dfe-pg --field-selector=status.phase=Running -o name | grep -c . | grep -qE '^[1-9]'"

echo ""
echo "--- Strimzi Kafka ---"
check "Kafka cluster ready" "kubectl -n strimzi get kafka dfe-kafka -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}' | grep -q True"
check "Kafka brokers running" "kubectl -n strimzi get pods -l strimzi.io/name=dfe-kafka-kafka --field-selector=status.phase=Running -o name | grep -c . | grep -qE '^[1-9]'"

echo ""
echo "--- ClickHouse ---"
check "ClickHouse pods running" "kubectl -n clickhouse get pods -l clickhouse.altinity.com/chi=dfe-clickhouse --field-selector=status.phase=Running -o name | grep -c . | grep -qE '^[1-9]'"

echo ""
echo "--- FerretDB ---"
check "FerretDB deployment ready" "kubectl -n ferretdb get deploy dfe-ferretdb -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"

echo ""
echo "--- OTel Collector ---"
check "OTel Gateway running" "kubectl -n otel get deploy dfe-otel-collector-gateway -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
check "OTel DaemonSet running" "kubectl -n otel get daemonset dfe-otel-collector-daemonset -o jsonpath='{.status.numberReady}' | grep -qE '^[1-9]'"

echo ""
echo "--- HyperDX ---"
check "HyperDX deployment ready" "kubectl -n hyperdx get deploy dfe-hyperdx -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="

if (( FAIL > 0 )); then
    echo "Layer 2 data platform is NOT healthy."
    exit 1
else
    echo "Layer 2 data platform is healthy."
fi
