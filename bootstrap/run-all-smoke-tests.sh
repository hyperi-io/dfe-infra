#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         run-all-smoke-tests.sh
#  Purpose:      Run all DFE smoke tests in sequence
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
TOTAL_PASS=0
TOTAL_FAIL=0
TOTAL_UNPROVEN=0
# A suite that ran and could not decide exits with this and gets its own word in
# the log, and it still fails the run: a scale nobody proved is not a working autoscaler.
readonly EXIT_UNPROVEN=3

run_test() {
    local name="${1}"
    local script="${2}"
    local rc=0
    echo ""
    echo "========================================"
    echo "  ${name}"
    echo "========================================"
    bash "${SCRIPT_DIR}/${script}" || rc=$?
    case "${rc}" in
        0) echo "  >>> ${name}: PASSED" ;;
        "${EXIT_UNPROVEN}")
            echo "  >>> ${name}: UNPROVEN -- it ran and could not decide, which fails the run; see the verdict above"
            (( TOTAL_UNPROVEN++ )) || true
            ;;
        *)
            echo "  >>> ${name}: FAILED"
            (( TOTAL_FAIL++ )) || true
            ;;
    esac
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
# A source write reaches Argo on the push, not on the 300s repo poll.
run_test "Forgejo -> Argo push webhook" "smoke-test-argo-webhook.sh"
run_test "Transform file sets (engine files = pod mount, byte for byte)" "smoke-test-transforms.sh"
run_test "KEDA Autoscaling" "smoke-test-keda.sh"
# The deep scale PROOF (artificial +1 pod per pressure-driven app via the fail-safe shim)
# mutates the cluster and takes about 12 min, so it is gated. Defaults ON for full
# validation. DFE_KEDA_SCALE_TEST=0 skips it for a fast smoke run.
if [ "${DFE_KEDA_SCALE_TEST:-1}" = "1" ]; then
    run_test "KEDA scale proof (artificial +1 pod)" "keda-scale-test.sh"
fi

echo ""
echo "========================================"
echo "  FINAL: ${TOTAL_PASS} suites run, ${TOTAL_FAIL} failed, ${TOTAL_UNPROVEN} unproven"
echo "========================================"

if (( TOTAL_FAIL > 0 || TOTAL_UNPROVEN > 0 )); then
    exit 1
fi
