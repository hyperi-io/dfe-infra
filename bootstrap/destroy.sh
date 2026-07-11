#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         destroy.sh
#  Purpose:      Clean teardown of DFE deployment (reverse of bootstrap.sh)
#  Language:     Bash
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DRY_RUN="${DFE_DRY_RUN:-false}"
run() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "[DRY-RUN] $*"
    else
        "$@"
    fi
}

echo "=== DFE Teardown ==="
echo "This will DELETE all DFE resources from the cluster."
echo ""

if [[ "${DRY_RUN}" != "true" ]] && [[ "${1:-}" != "--force" ]]; then
    read -rp "Are you sure? (type 'yes' to confirm): " confirm
    if [[ "${confirm}" != "yes" ]]; then
        echo "Aborted."
        exit 0
    fi
fi

echo "==> [1/7] Deleting ArgoCD Applications + ApplicationSets"
run kubectl -n argocd delete applicationset --all 2>/dev/null || true
run kubectl -n argocd delete app --all 2>/dev/null || true

echo "==> [2/7] Waiting for ArgoCD to clean up managed resources..."
if [[ "${DRY_RUN}" != "true" ]]; then
    sleep 10
fi

echo "==> [3/7] Deleting DFE data resources (CRDs)"
run kubectl -n strimzi delete kafka --all 2>/dev/null || true
run kubectl -n cnpg delete cluster --all 2>/dev/null || true
run kubectl -n clickhouse delete clickhouseinstallation --all 2>/dev/null || true
if [[ "${DRY_RUN}" != "true" ]]; then
    sleep 5
fi

echo "==> [4/7] Deleting DFE namespaces"
# Data-plane + operator + bundled deploy-repo (Forgejo) namespaces. The operator
# namespaces (clickhouse-operator/redpanda-operator) and kafka exist only in some
# profiles; --ignore-not-found makes listing them harmless when a profile did not
# create them. Missing any here strands the namespace (+ its finalizers) after a
# teardown, which then blocks a clean redeploy.
for ns in strimzi kafka clickhouse clickhouse-operator cnpg cnpg-system ferretdb otel hyperdx keda reloader external-dns redpanda-operator forgejo; do
    run kubectl delete ns "${ns}" --ignore-not-found 2>/dev/null || true
done
# Delete any dfe-* namespaces
if [[ "${DRY_RUN}" != "true" ]]; then
    kubectl get ns -o name 2>/dev/null | grep "namespace/dfe-" | while read -r ns; do
        kubectl delete "${ns}" --ignore-not-found 2>/dev/null || true
    done
else
    echo "[DRY-RUN] kubectl delete ns dfe-*"
fi

echo "==> [5/7] Uninstalling ArgoCD + Valkey"
run helm uninstall argocd -n argocd 2>/dev/null || true
run helm uninstall dfe-valkey -n argocd 2>/dev/null || true

echo "==> [6/7] Uninstalling Layer 1 (ESO, cert-manager)"
run helm uninstall external-secrets -n external-secrets 2>/dev/null || true
run helm uninstall cert-manager -n cert-manager 2>/dev/null || true

echo "==> [7/7] Cleaning up namespaces"
for ns in argocd cert-manager external-secrets envoy-gateway-system; do
    run kubectl delete ns "${ns}" --ignore-not-found 2>/dev/null || true
done

echo ""
echo "=== Teardown complete ==="
echo ""
echo "To also destroy Terraform state:"
echo "  cd terraform/environments/local && terraform destroy"
echo ""
echo "To redeploy:"
echo "  cd terraform/environments/local && terraform apply"
echo "  python3 bootstrap/bridge.py --tf-dir terraform/environments/local"
