#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         smoke-test.sh
#  Purpose:      Verify Layer 1 components are healthy after bootstrap
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
set -euo pipefail

readonly SCRIPT_NAME="$(basename "${0}")"
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

echo "=== DFE Layer 1 Smoke Test ==="
echo ""

echo "--- Namespaces ---"
check "argocd namespace exists" "kubectl get ns argocd"
check "cert-manager namespace" "kubectl get ns cert-manager"
check "external-secrets namespace" "kubectl get ns external-secrets"
check "envoy-gateway-system namespace" "kubectl get ns envoy-gateway-system"

echo ""
echo "--- Core Pods ---"
check "ArgoCD server running" "kubectl -n argocd get deploy argocd-server -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
check "ArgoCD repo-server running" "kubectl -n argocd get deploy argocd-repo-server -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
check "Valkey running" "kubectl -n argocd get deploy valkey -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
check "cert-manager running" "kubectl -n cert-manager get deploy cert-manager -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
check "ESO running" "kubectl -n external-secrets get deploy external-secrets -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"
check "Envoy Gateway running" "kubectl -n envoy-gateway-system get deploy envoy-gateway -o jsonpath='{.status.readyReplicas}' | grep -qE '^[1-9]'"

echo ""
echo "--- ArgoCD State ---"
check "Cluster secret exists" "kubectl -n argocd get secret dfe-cluster"
check "AppProjects exist" "kubectl -n argocd get appproject infra data dfe-apps"
check "Root ApplicationSet exists" "kubectl -n argocd get applicationset dfe-cluster-addons"

echo ""
echo "--- ESO ---"
# Assert on the secrets the deploy consumes, not on one named store: an unused
# optional store is not a broken deployment.
check "every ExternalSecret is synced" \
  "test -n \"\$(kubectl get externalsecrets -A -o name 2>/dev/null)\" && ! kubectl get externalsecrets -A -o jsonpath='{range .items[*]}{.status.conditions[?(@.type==\"Ready\")].status}{\"\n\"}{end}' | grep -q False"

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="

if (( FAIL > 0 )); then
    echo "Layer 1 bootstrap is NOT healthy."
    exit 1
else
    echo "Layer 1 bootstrap is healthy."
fi
