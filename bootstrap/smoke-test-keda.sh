#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() {
    local name="${1}" cmd="${2}"
    if eval "${cmd}" > /dev/null 2>&1; then
        echo "  [PASS] ${name}"; (( PASS++ )) || true
    else
        echo "  [FAIL] ${name}"; (( FAIL++ )) || true
    fi
}
echo "=== DFE KEDA Smoke Test ==="
echo ""
echo "--- KEDA Operator ---"
check "KEDA operator running" "kubectl -n keda get deploy keda-operator -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
check "KEDA metrics server" "kubectl -n keda get deploy keda-operator-metrics-apiserver -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
echo ""
echo "--- ScaledObjects ---"
check "receiver ScaledObject exists" "kubectl get scaledobject dfe-receiver-scaler --all-namespaces -o name | grep -q scaledobject"
check "loader ScaledObject exists" "kubectl get scaledobject dfe-loader-scaler --all-namespaces -o name | grep -q scaledobject"
echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
if (( FAIL > 0 )); then echo "KEDA is NOT healthy."; exit 1
else echo "KEDA is healthy."; fi
