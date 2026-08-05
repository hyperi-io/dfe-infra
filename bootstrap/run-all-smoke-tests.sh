#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         run-all-smoke-tests.sh
#  Purpose:      Run all DFE smoke tests in sequence
#  Language:     Bash
#
#  License:      BUSL-1.1
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

run_test "Readiness gate (pods Ready)" "smoke-test-readiness.sh"
run_test "CORE e2e (pipelines streaming)" "smoke-test-integration.sh"
run_test "Layer 1 (Bootstrap)" "smoke-test.sh"
run_test "Layer 2 (Data Platform)" "smoke-test-data.sh"
run_test "Auth & Ingress" "smoke-test-auth.sh"
# The fork's three DFE-specific seams (engine JWT / ClickHouse reads / embed
# headers). Nothing upstream covers them, so an upstream sync can merge clean and
# still break them -- this is where that shows up.
run_test "HyperDX seams (auth/data/embed)" "smoke-test-hyperdx.sh"
run_test "KEDA Autoscaling" "smoke-test-keda.sh"
# The deep scale PROOF (artificial +1 pod via the fail-safe shim) mutates the cluster +
# takes ~2-3 min, so it is gated. Defaults ON for full validation; set
# DFE_KEDA_SCALE_TEST=0 to skip it for a fast smoke run.
if [ "${DFE_KEDA_SCALE_TEST:-1}" = "1" ]; then
    run_test "KEDA scale proof (artificial +1 pod)" "keda-scale-test.sh"
fi

echo ""
echo "========================================"
echo "  FINAL: ${TOTAL_PASS} suites run, ${TOTAL_FAIL} failed"
echo "========================================"

if (( TOTAL_FAIL > 0 )); then
    exit 1
fi
