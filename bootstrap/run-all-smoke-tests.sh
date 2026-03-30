#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         run-all-smoke-tests.sh
#  Purpose:      Run all DFE smoke tests in sequence
#  Language:     Bash
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOTAL_PASS=0
TOTAL_FAIL=0

run_test() {
    local name="${1}"
    local script="${2}"
    echo ""
    echo "========================================"
    echo "  ${name}"
    echo "========================================"
    if bash "${SCRIPT_DIR}/${script}"; then
        echo "  >>> ${name}: PASSED"
    else
        echo "  >>> ${name}: FAILED"
        (( TOTAL_FAIL++ )) || true
    fi
    (( TOTAL_PASS++ )) || true
}

echo "=== DFE Full Deployment Validation ==="
echo "Running all smoke tests..."

run_test "Layer 1 (Bootstrap)" "smoke-test.sh"
run_test "Layer 2 (Data Platform)" "smoke-test-data.sh"
run_test "Auth & Ingress" "smoke-test-auth.sh"
run_test "KEDA Autoscaling" "smoke-test-keda.sh"

echo ""
echo "========================================"
echo "  FINAL: ${TOTAL_PASS} suites run, ${TOTAL_FAIL} failed"
echo "========================================"

if (( TOTAL_FAIL > 0 )); then
    exit 1
fi
