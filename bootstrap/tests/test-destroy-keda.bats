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
#   call-seconds       how long every kubectl call takes, in seconds of the clock
#   applications       "<name> <charts>" lines the Application listing prints
#   lb-services        "<namespace>|<name>|<finalizers>" LoadBalancer Services
#   held-polls         how many Service listings a deleted Service still shows
#   services-unreadable  listing the Services fails
#   cluster-facts      the five annotation lines read off the cluster secret
# aws is a stub too: it logs every call to aws.log, answers the two describes
# from security-groups.json and interfaces.json, and refuses every delete with
# an error naming an account when aws-refuses exists.
# The clock is a file: `date +%s` reads it, sleep and each kubectl call advance
# it, and trace.log stamps every kubectl call with it, so a test sees how long
# a wait ran without waiting.
# No cluster and no cloud account is involved.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    DESTROY="${REPO_ROOT}/bootstrap/destroy.sh"
    STUB_DIR="${BATS_TEST_TMPDIR}/stub"
    mkdir -p "${STUB_DIR}/bin"
    : > "${STUB_DIR}/kubectl.log"
    : > "${STUB_DIR}/trace.log"
    : > "${STUB_DIR}/aws.log"
    echo 1000000 > "${STUB_DIR}/clock"
    cat > "${STUB_DIR}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DIR}/kubectl.log"
now="$(cat "${STUB_DIR}/clock")"
printf '%s %s\n' "${now}" "$*" >> "${STUB_DIR}/trace.log"
if [[ -f "${STUB_DIR}/call-seconds" ]]; then
    echo $((now + $(cat "${STUB_DIR}/call-seconds"))) > "${STUB_DIR}/clock"
fi
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
    "-n argocd get applications.argoproj.io -o jsonpath="*)
        [[ -f "${STUB_DIR}/applications" ]] && cat "${STUB_DIR}/applications"
        ;;
    "-n argocd get secret dfe-cluster "*)
        [[ -f "${STUB_DIR}/cluster-facts" ]] && cat "${STUB_DIR}/cluster-facts"
        ;;
    "get services -A "*)
        [[ -f "${STUB_DIR}/services-unreadable" ]] && exit 1
        while IFS='|' read -r ns name finalizers; do
            gone="${STUB_DIR}/deleted-${ns}-${name}"
            if [[ ! -f "${gone}" ]]; then
                printf 'services|%s|%s||%s\n' "${ns}" "${name}" "${finalizers}"
            elif (( $(cat "${gone}") > 0 )); then
                echo $(($(cat "${gone}") - 1)) > "${gone}"
                printf 'services|%s|%s|2026-10-11T09:17:22Z|%s\n' "${ns}" "${name}" "${finalizers}"
            fi
        done < <(cat "${STUB_DIR}/lb-services" 2>/dev/null)
        ;;
    "-n "*" delete services "*)
        cat "${STUB_DIR}/held-polls" > "${STUB_DIR}/deleted-${2}-${5}" 2>/dev/null || echo 0 > "${STUB_DIR}/deleted-${2}-${5}"
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
    cat > "${STUB_DIR}/bin/aws" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DIR}/aws.log"
case "${1} ${2}" in
    "ec2 describe-security-groups")
        cat "${STUB_DIR}/security-groups.json" 2>/dev/null || echo '{"SecurityGroups": []}'
        ;;
    "ec2 describe-network-interfaces")
        cat "${STUB_DIR}/interfaces.json" 2>/dev/null || echo '{"NetworkInterfaces": []}'
        ;;
    "ec2 delete-"*)
        if [[ -f "${STUB_DIR}/aws-refuses" ]]; then
            echo "An error occurred (DependencyViolation) when calling the operation: arn:aws:iam::000000000000:role/runner" >&2
            exit 254
        fi
        ;;
esac
exit 0
STUB
    # The Helm uninstalls are not under test.
    printf '#!/bin/sh\nexit 0\n' > "${STUB_DIR}/bin/helm"
    # A pause costs the clock what it asks for and no real time.
    cat > "${STUB_DIR}/bin/sleep" <<'STUB'
#!/usr/bin/env bash
echo $(($(cat "${STUB_DIR}/clock") + ${1%%.*})) > "${STUB_DIR}/clock"
STUB
    cat > "${STUB_DIR}/bin/date" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" == "+%s" ]]; then
    cat "${STUB_DIR}/clock"
else
    exec /bin/date "$@"
fi
STUB
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

# Line number of the last line of ${2} containing the fixed string ${1}.
last_line_of() {
    grep -nF -- "${1}" <<<"${2}" | tail -n 1 | cut -d: -f1
}

RUN_TAGS='{"dfe-e2e":"r20261011t084205z-8e3968","expires-at":"2026-10-11T11:57:05Z"}'
# What the load balancer controller tags a run's security groups with.
RUN_SG_TAGS='[{"Key":"elbv2.k8s.aws/cluster","Value":"dfe-cycle"},{"Key":"dfe-e2e","Value":"r20261011t084205z-8e3968"},{"Key":"expires-at","Value":"2026-10-11T11:57:05Z"}]'
RUN_ENI_TAGS='[{"Key":"dfe-e2e","Value":"r20261011t084205z-8e3968"},{"Key":"expires-at","Value":"2026-10-11T11:57:05Z"}]'

# The edge gateway's Service behind the controller, still held for ${1} listings
# after it is deleted.
gateway_behind_the_controller() {
    printf '%s\n' \
        'aws-load-balancer-controller-dfe-aws-test aws-load-balancer-controller' \
        'envoy-gateway-config-dfe-aws-test' \
        'envoy-gateway gateway-helm' > "${STUB_DIR}/applications"
    echo 'envoy-gateway-system|envoy-gw|["service.k8s.aws/resources"]' > "${STUB_DIR}/lb-services"
    echo "${1}" > "${STUB_DIR}/held-polls"
}

# The cluster secret of an AWS test run, with ${1} as its run tags when given.
a_test_run() {
    printf '%s\n' aws us-west-2 dfe-cycle vpc-0run "${1-${RUN_TAGS}}" > "${STUB_DIR}/cluster-facts"
}

# The run's own group, one carrying no run tag, one from another run, and one
# with every tag in another VPC.
security_groups() {
    printf '{"SecurityGroups": [%s, %s, %s, %s]}\n' \
        "{\"GroupId\": \"sg-run\", \"VpcId\": \"vpc-0run\", \"Tags\": ${RUN_SG_TAGS}}" \
        '{"GroupId": "sg-untagged", "VpcId": "vpc-0run", "Tags": [{"Key": "elbv2.k8s.aws/cluster", "Value": "dfe-cycle"}]}' \
        '{"GroupId": "sg-other-run", "VpcId": "vpc-0run", "Tags": [{"Key": "elbv2.k8s.aws/cluster", "Value": "dfe-cycle"}, {"Key": "dfe-e2e", "Value": "r20261012t000000z-000000"}, {"Key": "expires-at", "Value": "2026-10-11T11:57:05Z"}]}' \
        "{\"GroupId\": \"sg-elsewhere\", \"VpcId\": \"vpc-other\", \"Tags\": ${RUN_SG_TAGS}}" \
        > "${STUB_DIR}/security-groups.json"
}

# A detached interface of the run's and an attached one, both holding the group.
interfaces() {
    printf '{"NetworkInterfaces": [%s, %s]}\n' \
        "{\"NetworkInterfaceId\": \"eni-detached\", \"VpcId\": \"vpc-0run\", \"Status\": \"available\", \"TagSet\": ${RUN_ENI_TAGS}}" \
        "{\"NetworkInterfaceId\": \"eni-attached\", \"VpcId\": \"vpc-0run\", \"Status\": \"in-use\", \"TagSet\": ${RUN_ENI_TAGS}}" \
        > "${STUB_DIR}/interfaces.json"
}

aws_log() {
    cat "${STUB_DIR}/aws.log"
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
    # shellcheck disable=SC2016 # the stub expands $* when it runs, not here
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

# Seconds of the clock the Application wait ran: from its first kubectl call to
# the last settle pause before step 3, which is the 10 the script sleeps between.
applications_wait_seconds() {
    local first_poll step3
    first_poll="$(awk '$3 == "keda" { print $1; exit }' "${STUB_DIR}/trace.log")"
    step3="$(awk '$3 == "strimzi" && $4 == "delete" { print $1; exit }' "${STUB_DIR}/trace.log")"
    [ -n "${first_poll}" ]
    [ -n "${step3}" ]
    echo $((step3 - 10 - first_poll))
}

@test "the Application wait ends at its 300s budget when every kubectl call takes time" {
    echo 2 > "${STUB_DIR}/call-seconds"
    stick '["example.com/other"]'
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    local waited
    waited="$(applications_wait_seconds)"
    # A poll is five calls of 2s, and the one under way at the deadline finishes.
    [ "${waited}" -ge 300 ]
    [ "${waited}" -le 310 ]
    [[ "${output}" =~ WARN:\ ArgoCD\ Applications\ still\ present\ after\ ([0-9]+)s ]]
    [ "${BASH_REMATCH[1]}" -eq "${waited}" ]
}

@test "quick polls still get the whole 300s budget" {
    stick '["example.com/other"]'
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    local waited
    waited="$(applications_wait_seconds)"
    [ "${waited}" -ge 300 ]
    [ "${waited}" -le 305 ]
}

@test "Applications that are gone end the wait at the first poll, however slow the calls" {
    echo 40 > "${STUB_DIR}/call-seconds"
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    # One operator check per poll.
    [ "$(grep -c -- '^-n keda get pods' "${STUB_DIR}/kubectl.log")" -eq 1 ]
    [[ "${output}" != *"WARN"* ]]
}

@test "the load balancer controller outlives every load balancer it reconciles" {
    gateway_behind_the_controller 2
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    local log_text orphaned others released polled controller
    log_text="$(log)"
    orphaned="$(line_of '-n argocd delete applicationset --all --cascade=orphan' "${log_text}")"
    others="$(line_of '-n argocd delete applications.argoproj.io envoy-gateway-config-dfe-aws-test envoy-gateway --wait=false' "${log_text}")"
    released="$(line_of '-n envoy-gateway-system delete services envoy-gw' "${log_text}")"
    polled="$(last_line_of 'get services -A' "${log_text}")"
    controller="$(line_of '-n argocd delete applications.argoproj.io --all' "${log_text}")"
    [ -n "${orphaned}" ]
    [ -n "${others}" ]
    [ -n "${released}" ]
    [ -n "${controller}" ]
    [ "${orphaned}" -lt "${others}" ]
    [ "${others}" -lt "${released}" ]
    [ "${released}" -lt "${polled}" ]
    [ "${polled}" -lt "${controller}" ]
    # The first listing deletes it, two more still show the finalizer, the fourth finds it gone.
    [ "$(grep -c -- '^get services -A' <<<"${log_text}")" -eq 4 ]
    [ "$(grep -c -- 'delete applications.argoproj.io .*aws-load-balancer-controller' <<<"${log_text}")" -eq 0 ]
    [[ "${output}" != *"WARN"* ]]
}

@test "a load balancer the controller never releases is named, and the teardown goes on at 300s" {
    gateway_behind_the_controller 1000
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    [[ "${output}" =~ WARN:\ the\ load\ balancer\ controller\ still\ holds\ services/envoy-gateway-system/envoy-gw\ after\ ([0-9]+)s ]]
    [ "${BASH_REMATCH[1]}" -ge 300 ]
    [ "${BASH_REMATCH[1]}" -le 305 ]
    [[ "$(log)" == *"-n argocd delete applications.argoproj.io --all --wait=false"* ]]
    [[ "${output}" == *"=== Teardown complete ==="* ]]
}

@test "a load balancer in a namespace the teardown keeps is never touched" {
    echo 'other-tenant|their-lb|["service.k8s.aws/resources"]' > "${STUB_DIR}/lb-services"
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    [[ "$(log)" != *"-n other-tenant delete"* ]]
    [[ "${output}" != *"WARN"* ]]
}

@test "a Service list the cluster will not answer is never read as released" {
    touch "${STUB_DIR}/services-unreadable"
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    [[ "${output}" =~ WARN:\ the\ load\ balancer\ controller\ still\ holds\ \(the\ cluster\ did\ not\ list\ every\ Service\ and\ Ingress\)\ after\ ([0-9]+)s ]]
    [ "${BASH_REMATCH[1]}" -ge 300 ]
}

@test "the sweep deletes the run's load balancer security group and refuses one without the run tags" {
    a_test_run
    security_groups
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    local aws_text
    aws_text="$(aws_log)"
    [[ "${aws_text}" == *"ec2 describe-security-groups --region us-west-2 --output json --filters Name=vpc-id,Values=vpc-0run Name=tag:elbv2.k8s.aws/cluster,Values=dfe-cycle"* ]]
    [[ "${aws_text}" == *"ec2 delete-security-group --region us-west-2 --group-id sg-run"* ]]
    [ "$(grep -c -- 'delete-security-group' <<<"${aws_text}")" -eq 1 ]
    [[ "${output}" == *"left security group sg-untagged: it does not carry dfe-e2e,expires-at"* ]]
    [[ "${output}" == *"left security group sg-other-run: it does not carry dfe-e2e"* ]]
    [[ "${output}" == *"left security group sg-elsewhere: it is outside the run VPC"* ]]
    [[ "${output}" == *"deleted security group sg-run"* ]]
    # Last, once every namespace and its load balancers are gone.
    [ "$(line_of '==> [8/9]' "${output}")" -lt "$(line_of 'deleted security group sg-run' "${output}")" ]
}

@test "a detached interface of the run's goes before its group, and an attached one stays" {
    a_test_run
    security_groups
    interfaces
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    local aws_text eni group
    aws_text="$(aws_log)"
    [[ "${aws_text}" == *"ec2 describe-network-interfaces --region us-west-2 --output json --filters Name=vpc-id,Values=vpc-0run Name=group-id,Values=sg-run"* ]]
    eni="$(line_of 'ec2 delete-network-interface --region us-west-2 --network-interface-id eni-detached' "${aws_text}")"
    group="$(line_of 'ec2 delete-security-group --region us-west-2 --group-id sg-run' "${aws_text}")"
    [ -n "${eni}" ]
    [ -n "${group}" ]
    [ "${eni}" -lt "${group}" ]
    [[ "${aws_text}" != *"--network-interface-id eni-attached"* ]]
    [[ "${output}" == *"left network interface eni-attached on sg-run: it is still attached"* ]]
}

@test "a cluster secret without run tags is never swept" {
    a_test_run ""
    security_groups
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    [ ! -s "${STUB_DIR}/aws.log" ]
    [[ "${output}" == *"names no run tags"* ]]
}

@test "a cluster that is not on AWS makes no aws call" {
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    [ ! -s "${STUB_DIR}/aws.log" ]
    [[ "${output}" == *"not an AWS cluster"* ]]
}

@test "a delete AWS refuses is reported by its error code, never its message" {
    a_test_run
    security_groups
    touch "${STUB_DIR}/aws-refuses"
    run bash "${DESTROY}" --force
    [ "$status" -eq 0 ]
    [[ "${output}" == *"WARN: could not delete security group sg-run: DependencyViolation"* ]]
    [[ "${output}" != *"000000000000"* ]]
    [[ "${output}" == *"=== Teardown complete ==="* ]]
}

@test "the dry run names the load balancer release and the sweep, and calls neither kubectl nor aws" {
    a_test_run
    run env DFE_DRY_RUN=true bash "${DESTROY}"
    [ "$status" -eq 0 ]
    local release controller sweep
    release="$(line_of 'wait up to 300s for the controller' "${output}")"
    controller="$(line_of 'kubectl -n argocd delete applications.argoproj.io --all' "${output}")"
    sweep="$(line_of "delete each security group in the run's VPC" "${output}")"
    [ -n "${release}" ]
    [ -n "${controller}" ]
    [ -n "${sweep}" ]
    [ "${release}" -lt "${controller}" ]
    [ "${controller}" -lt "${sweep}" ]
    [ ! -s "${STUB_DIR}/kubectl.log" ]
    [ ! -s "${STUB_DIR}/aws.log" ]
}
