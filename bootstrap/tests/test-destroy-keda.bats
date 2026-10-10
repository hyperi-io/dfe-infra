#!/usr/bin/env bats
# A KEDA resource that is terminating once no KEDA operator is left to clear
# finalizer.keda.sh holds its Argo Application, and so destroy.sh, forever.
# The same goes for an Application once Argo itself is gone, and for any
# namespace a finalizer holds, so every one of those waits is bounded.
# kubectl is a stub that logs every call and answers from files in STUB_DIR:
#   operator-running   a KEDA operator pod is Running
#   pods-unreadable    listing the operator pod fails
#   stuck              the line a terminating ScaledObject prints; the Argo
#                      Application stays until the patch lands
#   argocd-held        the argocd namespace outlives its delete timeout, and
#                      this file is the condition message it carries
# No cluster is involved.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    DESTROY="${REPO_ROOT}/bootstrap/destroy.sh"
    STUB_DIR="${BATS_TEST_TMPDIR}/stub"
    mkdir -p "${STUB_DIR}/bin"
    : > "${STUB_DIR}/kubectl.log"
    cat > "${STUB_DIR}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DIR}/kubectl.log"
case "$*" in
    "-n keda get pods "*)
        if [[ -f "${STUB_DIR}/pods-unreadable" ]]; then
            echo "error: pods unreadable" >&2
            exit 1
        fi
        if [[ -f "${STUB_DIR}/operator-running" ]]; then
            echo "pod/keda-operator-7d9f6b5c4-x2x8q"
        fi
        ;;
    "get scaledobjects.keda.sh "*)
        if [[ -f "${STUB_DIR}/stuck" && ! -f "${STUB_DIR}/patched" ]]; then
            cat "${STUB_DIR}/stuck"
        fi
        ;;
    "patch "*)
        : > "${STUB_DIR}/patched"
        ;;
    "-n argocd get applications.argoproj.io "*)
        if [[ -f "${STUB_DIR}/stuck" && ! -f "${STUB_DIR}/patched" ]]; then
            echo "application.argoproj.io/dfe-receiver"
        fi
        ;;
    "delete ns argocd "*)
        [[ -f "${STUB_DIR}/argocd-held" ]] && exit 1
        ;;
    "get ns argocd "*)
        [[ -f "${STUB_DIR}/argocd-held" ]] && cat "${STUB_DIR}/argocd-held"
        ;;
esac
exit 0
STUB
    # The settle pauses and the Helm uninstalls are not under test.
    for name in sleep helm; do
        printf '#!/bin/sh\nexit 0\n' > "${STUB_DIR}/bin/${name}"
    done
    chmod +x "${STUB_DIR}/bin/"*
    export STUB_DIR
    export PATH="${STUB_DIR}/bin:${PATH}"
    unset DFE_DRY_RUN
}

stick() {
    printf '%s\n' "ScaledObject dfe-a dfe-receiver-scaler ${1}" > "${STUB_DIR}/stuck"
}

# Line number of the first line of ${2} containing the fixed string ${1}.
line_of() {
    grep -nF -- "${1}" <<<"${2}" | head -n 1 | cut -d: -f1
}

log() {
    cat "${STUB_DIR}/kubectl.log"
}

@test "a ScaledObject stuck on finalizer.keda.sh with no KEDA operator running is patched" {
    stick '["finalizer.keda.sh"]'
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    [[ "$(log)" == *'patch ScaledObject.keda.sh dfe-receiver-scaler -n dfe-a --type merge -p {"metadata":{"finalizers":null}}'* ]]
    [[ "${output}" == *"cleared finalizer.keda.sh from ScaledObject dfe-a/dfe-receiver-scaler"* ]]
    [[ "${output}" != *"WARN"* ]]
}

@test "nothing is patched while a KEDA operator pod is running" {
    touch "${STUB_DIR}/operator-running"
    stick '["finalizer.keda.sh"]'
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    [[ "$(log)" != *"patch ScaledObject.keda.sh"* ]]
    [[ "${output}" == *"WARN: ArgoCD Applications still present"* ]]
}

@test "nothing is patched when the operator pod cannot be listed" {
    touch "${STUB_DIR}/pods-unreadable"
    stick '["finalizer.keda.sh"]'
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    [[ "$(log)" != *"patch ScaledObject.keda.sh"* ]]
    [[ "${output}" == *"WARN: ArgoCD Applications still present"* ]]
}

@test "a terminating resource without finalizer.keda.sh is left alone" {
    stick '["example.com/other"]'
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    [[ "$(log)" != *"patch ScaledObject.keda.sh"* ]]
    [[ "${output}" == *"WARN: ArgoCD Applications still present"* ]]
}

@test "an Application left when Argo is uninstalled has its finalizers cleared first" {
    touch "${STUB_DIR}/operator-running"
    stick '["finalizer.keda.sh"]'
    printf '#!/usr/bin/env bash\nprintf "helm %%s\\n" "$*" >> "${STUB_DIR}/kubectl.log"\n' > "${STUB_DIR}/bin/helm"
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    local log_text patched uninstalled
    log_text="$(log)"
    patched="$(line_of '-n argocd patch application.argoproj.io/dfe-receiver --type merge -p {"metadata":{"finalizers":null}}' "${log_text}")"
    uninstalled="$(line_of 'helm uninstall argocd -n argocd' "${log_text}")"
    [ -n "${patched}" ]
    [ -n "${uninstalled}" ]
    [ "${patched}" -lt "${uninstalled}" ]
    [[ "${output}" == *"cleared the finalizers on application.argoproj.io/dfe-receiver"* ]]
}

@test "no namespace delete can wait forever" {
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    local deletes
    deletes="$(grep -E '^delete ns ' "${STUB_DIR}/kubectl.log")"
    [ -n "${deletes}" ]
    [[ "$(grep -cv -- '--timeout=120s' <<<"${deletes}")" -eq 0 ]]
}

@test "a namespace still terminating at its timeout is named and the teardown finishes" {
    echo "Some content in the namespace has finalizers remaining: resources-finalizer.argocd.argoproj.io in 1 resource instances" \
        > "${STUB_DIR}/argocd-held"
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    [[ "${output}" == *"WARN: namespace argocd still terminating after 120s, continuing: Some content in the namespace has finalizers remaining: resources-finalizer.argocd.argoproj.io"* ]]
    [[ "$(log)" == *"delete ns cert-manager --ignore-not-found --timeout=120s"* ]]
    [[ "${output}" == *"=== Teardown complete ==="* ]]
}

@test "a healthy teardown patches nothing and reports no warning" {
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    [[ "$(log)" != *"patch "* ]]
    [[ "${output}" != *"WARN"* ]]
}

@test "the ApplicationSets go first, then KEDA's resources, then the Applications" {
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    local log_text appset scaledobject scaledjob triggerauth applications
    log_text="$(log)"
    appset="$(line_of '-n argocd delete applicationset --all' "${log_text}")"
    scaledobject="$(line_of 'delete scaledobject --all -A' "${log_text}")"
    scaledjob="$(line_of 'delete scaledjob --all -A' "${log_text}")"
    triggerauth="$(line_of 'delete triggerauthentication --all -A' "${log_text}")"
    applications="$(line_of '-n argocd delete applications.argoproj.io --all' "${log_text}")"
    [ -n "${appset}" ]
    [ "${appset}" -lt "${scaledobject}" ]
    [ "${scaledobject}" -lt "${scaledjob}" ]
    [ "${scaledjob}" -lt "${triggerauth}" ]
    [ "${triggerauth}" -lt "${applications}" ]
}

@test "no delete in step 1 can wait forever" {
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    [[ "$(log)" == *"delete scaledobject --all -A --timeout=30s"* ]]
    [[ "$(log)" == *"delete scaledjob --all -A --timeout=30s"* ]]
    [[ "$(log)" == *"delete triggerauthentication --all -A --timeout=30s"* ]]
    [[ "$(log)" == *"-n argocd delete applications.argoproj.io --all --wait=false"* ]]
}

@test "the dry run prints the same order and touches no cluster" {
    run env DFE_DRY_RUN=true bash "${DESTROY}"
    [ "$status" -eq 0 ]
    local appset scaledobject applications backstop
    appset="$(line_of 'kubectl -n argocd delete applicationset --all' "${output}")"
    scaledobject="$(line_of 'kubectl delete scaledobject --all -A' "${output}")"
    applications="$(line_of 'kubectl -n argocd delete applications.argoproj.io --all' "${output}")"
    backstop="$(line_of 'clearing finalizer.keda.sh' "${output}")"
    [ -n "${appset}" ]
    [ "${appset}" -lt "${scaledobject}" ]
    [ "${scaledobject}" -lt "${applications}" ]
    [ "${applications}" -lt "${backstop}" ]
    [ ! -s "${STUB_DIR}/kubectl.log" ]
}
