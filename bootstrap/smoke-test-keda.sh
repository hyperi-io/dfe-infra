#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() {
    local name="${1}" cmd="${2}"
    # pipefail is off for the check itself: a check ending in grep -q closes the
    # pipe on the first match, and the producer's SIGPIPE would fail a passing check.
    if ( set +o pipefail; eval "${cmd}" ) > /dev/null 2>&1; then
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
# kubectl refuses a named resource together with --all-namespaces, so these
# search the cluster-wide listing by name instead.
check "receiver ScaledObject exists" "kubectl get scaledobject -A -o name | grep -q '/dfe-receiver-scaler\$'"
check "loader ScaledObject exists" "kubectl get scaledobject -A -o name | grep -q '/dfe-loader-scaler\$'"
echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
if (( FAIL > 0 )); then echo "KEDA is NOT healthy."; exit 1
else echo "KEDA is healthy."; fi
