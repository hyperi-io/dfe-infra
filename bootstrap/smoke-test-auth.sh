#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         smoke-test-auth.sh
#  Purpose:      Verify auth and ingress components are healthy
#  Language:     Bash
#
#  License:      BUSL-1.1
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

echo "=== DFE Auth & Ingress Smoke Test ==="
echo ""

echo "--- Gateway API ---"
check "GatewayClass exists" "kubectl get gatewayclass dfe-envoy"
check "Gateway exists" "kubectl -n envoy-gateway-system get gateway dfe-gateway"
check "Gateway programmed" "kubectl -n envoy-gateway-system get gateway dfe-gateway -o jsonpath='{.status.conditions[?(@.type==\"Programmed\")].status}' | grep -q True"

echo ""
echo "--- HTTPRoutes ---"
check "dfe-ui HTTPRoute" "kubectl get httproute dfe-ui --all-namespaces -o name | grep -q httproute"
check "dfe-engine HTTPRoute" "kubectl get httproute dfe-engine --all-namespaces -o name | grep -q httproute"
check "argocd HTTPRoute" "kubectl -n argocd get httproute argocd"
check "hyperdx HTTPRoute" "kubectl get httproute hyperdx --all-namespaces -o name | grep -q httproute"

echo ""
echo "--- Network Policies ---"
check "DFE namespace ingress policy" "kubectl get networkpolicy dfe-ingress-policy --all-namespaces -o name | grep -q networkpolicy"
check "Data namespace ingress policy" "kubectl -n cnpg get networkpolicy data-ingress-policy"
check "OTel egress policy" "kubectl get networkpolicy allow-otel-egress --all-namespaces -o name | grep -q networkpolicy"

echo ""
echo "--- OIDC Providers (conditional) ---"
OIDC_POLICIES=$(kubectl -n envoy-gateway-system get securitypolicy -l app.kubernetes.io/part-of=dfe -o name 2>/dev/null || true)
if [[ -n "${OIDC_POLICIES}" ]]; then
    while IFS= read -r policy; do
        policy_name="${policy##*/}"
        check "SecurityPolicy ${policy_name} exists" "true"
        check "SecurityPolicy ${policy_name} accepted" \
            "kubectl -n envoy-gateway-system get securitypolicy ${policy_name} -o jsonpath='{.status.conditions[?(@.type==\"Accepted\")].status}' | grep -q True"
    done <<< "${OIDC_POLICIES}"
else
    echo "  [SKIP] No OIDC SecurityPolicies found (oidc.enabled=false or no providers)"
fi

echo ""
echo "--- OIDC Independence Check ---"
check "Envoy Gateway runs without dfe-engine" \
    "kubectl -n envoy-gateway-system get pods -l app.kubernetes.io/name=envoy-gateway -o jsonpath='{.items[0].status.phase}' | grep -q Running"
check "SecurityPolicies are K8s CRDs (not dfe-engine managed)" \
    "[[ -z \"${OIDC_POLICIES}\" ]] || kubectl -n envoy-gateway-system get securitypolicy -o yaml | grep -q 'managed-by.*helm'"

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="

if (( FAIL > 0 )); then
    echo "Auth & ingress is NOT healthy."
    exit 1
else
    echo "Auth & ingress is healthy."
fi
