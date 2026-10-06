#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         destroy.sh
#  Purpose:      Clean teardown of DFE deployment (reverse of bootstrap.sh)
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
set -euo pipefail

DRY_RUN="${DFE_DRY_RUN:-false}"
run() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "[DRY-RUN] $*"
    else
        "$@"
    fi
}

# Success only when kubectl answered and no KEDA operator pod is running, so an
# unreadable answer never reads as "gone".
keda_operator_absent() {
    local pods
    pods="$(kubectl -n keda get pods -l app.kubernetes.io/name=keda-operator \
        --field-selector=status.phase=Running -o name 2>/dev/null)" || return 1
    [[ -z "${pods}" ]]
}

# Only the KEDA operator clears finalizer.keda.sh, so a KEDA resource still
# terminating once the operator is gone would hold its Argo Application forever.
clear_stranded_keda_finalizers() {
    local resource kind ns name finalizers
    keda_operator_absent || return 0
    for resource in scaledobjects scaledjobs triggerauthentications; do
        while read -r kind ns name finalizers; do
            [[ "${finalizers}" == *'"finalizer.keda.sh"'* ]] || continue
            if kubectl patch "${kind}.keda.sh" "${name}" -n "${ns}" --type merge \
                -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1; then
                echo "  no KEDA operator is running: cleared finalizer.keda.sh from ${kind} ${ns}/${name}"
            else
                echo "  WARN: could not clear finalizer.keda.sh from ${kind} ${ns}/${name}"
            fi
        done < <(kubectl get "${resource}.keda.sh" -A -o jsonpath='{range .items[?(@.metadata.deletionTimestamp)]}{.kind}{" "}{.metadata.namespace}{" "}{.metadata.name}{" "}{.metadata.finalizers}{"\n"}{end}' 2>/dev/null || true)
    done
}

# Bounded, so an Application held up by anything else cannot stall the teardown.
await_applications_gone() {
    local waited=0
    while (( waited < 300 )); do
        clear_stranded_keda_finalizers
        if [[ -z "$(kubectl -n argocd get applications.argoproj.io -o name 2>/dev/null)" ]]; then
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    echo "  WARN: ArgoCD Applications still present after ${waited}s, continuing"
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

echo "==> [1/8] Deleting ArgoCD Applications + ApplicationSets"
# ApplicationSets first, so Argo stops recreating what is deleted below.
run kubectl -n argocd delete applicationset --all 2>/dev/null || true
# KEDA clears its own finalizer.keda.sh, so its resources go while it still runs.
run kubectl delete scaledobject --all -A --timeout=30s 2>/dev/null || true
run kubectl delete scaledjob --all -A --timeout=30s 2>/dev/null || true
run kubectl delete triggerauthentication --all -A --timeout=30s 2>/dev/null || true
# Fully qualified: on a Rancher-managed cluster the bare `app` resolves to
# app.catalog.cattle.io and every Argo Application survives the teardown.
run kubectl -n argocd delete applications.argoproj.io --all --wait=false 2>/dev/null || true
if [[ "${DRY_RUN}" != "true" ]]; then
    await_applications_gone
else
    echo "[DRY-RUN] wait for the Applications to go, clearing finalizer.keda.sh from any KEDA resource still terminating once no KEDA operator pod is running"
fi

echo "==> [2/8] Waiting for ArgoCD to clean up managed resources..."
if [[ "${DRY_RUN}" != "true" ]]; then
    sleep 10
fi

echo "==> [3/8] Deleting DFE data resources (CRDs)"
run kubectl -n strimzi delete kafka --all 2>/dev/null || true
# A CloudNativePG Cluster exists only on an install that predates its removal.
run kubectl -n cnpg delete clusters.postgresql.cnpg.io --all 2>/dev/null || true
# ClickHouseCluster/KeeperCluster are the clickhouse.com operator's kinds and
# clickhouseinstallation is Altinity's; a CR left behind keeps its finalizer and
# wedges the namespace delete below.
run kubectl -n clickhouse delete clickhousecluster --all 2>/dev/null || true
run kubectl -n clickhouse delete keepercluster --all 2>/dev/null || true
run kubectl -n clickhouse delete clickhouseinstallation --all 2>/dev/null || true
if [[ "${DRY_RUN}" != "true" ]]; then
    sleep 5
fi

echo "==> [4/8] Deleting DFE namespaces"
# Data-plane + operator + bundled deploy-repo (Forgejo) namespaces. The operator
# namespaces (clickhouse-operator-system/redpanda-operator) and kafka exist only in
# some profiles; --ignore-not-found makes listing them harmless when a profile did
# not create them. Missing any here strands the namespace (+ its finalizers) after a
# teardown, which then blocks a clean redeploy.
# clickhouse-operator is the pre-rc.7 namespace, kept so a teardown of an older
# deploy cannot leave a second operator reconciling the same CRs.
# Deleting the external-dns namespace here does not itself clean up the DNS
# records it published on AWS: sync policy only removes a record once it has
# reconciled the Service/Ingress deletion from step 1, and this script gives
# it no guaranteed time to do that before its pod is gone. That is what
# terraform/modules/kubernetes-cluster/aws/dns.tf's destroy-time cleanup is
# for -- it empties the private zone itself, independent of whether
# external-dns ever got the chance.
for ns in strimzi kafka clickhouse clickhouse-operator-system clickhouse-operator cnpg ferretdb otel hyperdx reloader external-dns redpanda-operator forgejo gitea links; do
    run kubectl delete ns "${ns}" --ignore-not-found 2>/dev/null || true
done
# KEDA registers the external-metrics APIService cluster-wide; deleting its
# namespace strands the registration, which wedges the metrics API and every
# later readiness/teardown pass that touches it.
run kubectl delete apiservice v1beta1.external.metrics.k8s.io --ignore-not-found 2>/dev/null || true
# Delete any dfe-* namespaces. grep exits 1 on no match, which pipefail
# would turn into an abort on an already-clean cluster.
if [[ "${DRY_RUN}" != "true" ]]; then
    { kubectl get ns -o name 2>/dev/null | grep "namespace/dfe-" || true; } | while read -r ns; do
        kubectl delete "${ns}" --ignore-not-found 2>/dev/null || true
    done
else
    echo "[DRY-RUN] kubectl delete ns dfe-*"
fi
# Last, once no ScaledObject can still need its operator.
run kubectl delete ns keda --ignore-not-found 2>/dev/null || true

echo "==> [5/8] Deleting the Strimzi CRDs"
# A leftover Strimzi CRD keeps every version it stored, and the next operator
# release's chart refuses one it no longer serves.
# Deleting a CRD deletes its resources cluster-wide, so all stay while any still
# holds one or cannot be read.
if [[ "${DRY_RUN}" != "true" ]]; then
    strimzi_crds=""
    strimzi_held=""
    while read -r crd; do
        crd="${crd#*/}"
        strimzi_crds="${strimzi_crds} ${crd}"
        if ! held="$(kubectl get "${crd}" -A -o name 2>/dev/null </dev/null)" || [[ -n "${held}" ]]; then
            strimzi_held="${strimzi_held} ${crd}"
        fi
    done < <(kubectl get crd -o name 2>/dev/null | grep '\.strimzi\.io$' || true)
    if [[ -z "${strimzi_crds}" ]]; then
        echo "  no Strimzi CRD on this cluster"
    elif [[ -n "${strimzi_held}" ]]; then
        echo "  Strimzi CRDs left in place -- still holding a resource, or unreadable:${strimzi_held}"
    else
        # shellcheck disable=SC2086 # one word per CRD name
        kubectl delete crd ${strimzi_crds} --ignore-not-found --timeout=120s 2>/dev/null \
            || echo "  WARN: could not delete the Strimzi CRDs:${strimzi_crds}"
    fi
else
    echo "[DRY-RUN] kubectl delete crd <every *.strimzi.io CRD, once none holds a resource>"
fi

echo "==> [6/8] Uninstalling ArgoCD + Valkey"
run helm uninstall argocd -n argocd 2>/dev/null || true
run helm uninstall dfe-valkey -n argocd 2>/dev/null || true

echo "==> [7/8] Uninstalling Layer 1 (ESO, cert-manager)"
run helm uninstall external-secrets -n external-secrets 2>/dev/null || true
run helm uninstall cert-manager -n cert-manager 2>/dev/null || true

echo "==> [8/8] Cleaning up namespaces"
for ns in argocd cert-manager external-secrets envoy-gateway-system; do
    run kubectl delete ns "${ns}" --ignore-not-found 2>/dev/null || true
done
# destroy.sh cannot tell a MetalLB bootstrap installed from one the cluster
# already carried, and removing an adopted LoadBalancer provider would strand
# every other tenant's Service on the cluster.
echo "  metallb-system and its address pool left in place -- an adopted LoadBalancer provider is never removed"

echo ""
echo "=== Teardown complete ==="
echo ""
echo "To also destroy OpenTofu state:"
echo "  cd terraform/environments/local && tofu destroy"
echo "  (AWS: cd terraform/environments/aws && tofu destroy -- the private DNS"
echo "  zone empties itself of what external-dns published as its own last"
echo "  step, so it needs no manual record cleanup first; see docs/deployment/aws.md)"
echo ""
echo "To redeploy:"
echo "  cd terraform/environments/local && tofu apply"
echo "  python3 bootstrap/bridge.py --tf-dir terraform/environments/local"
