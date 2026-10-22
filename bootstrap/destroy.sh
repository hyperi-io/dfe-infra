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
# The most any one delete below waits; kubectl's own default is to wait forever.
DELETE_TIMEOUT="120s"
# Seconds step 1 waits for the Argo CD Applications to go.
APPLICATIONS_WAIT=300
# Seconds step 1 gives the load balancer controller to delete what it created.
LOAD_BALANCERS_WAIT=300
# Seconds step 1 gives Karpenter to drain and terminate the nodes its pools launched.
NODECLAIMS_WAIT=600
# The namespaces step 4 deletes, then steps 7 and 8. With dfe-* and keda they
# are every namespace this teardown removes, and the only ones step 1 touches.
DATA_NAMESPACES=(strimzi kafka clickhouse clickhouse-operator-system clickhouse-operator cnpg cnpg-system documentdb-operator otel hyperdx reloader external-dns redpanda-operator forgejo gitea links)
PLATFORM_NAMESPACES=(argocd envoy-gateway-system cert-manager external-secrets)
# Facts off the Argo CD cluster secret, which step 7 deletes, or the caller's DFE_* environment.
CLUSTER_CLOUD=""
CLUSTER_REGION=""
CLUSTER_NAME=""
CLUSTER_VPC=""
CLUSTER_RUN_TAGS=""
# Where those facts came from, for step 9's report.
CLUSTER_FACTS_SOURCE="the cluster secret"
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

# Bounded by the clock, kubectl calls included, and blind to the names in ${1},
# which are held back. The poll under way at the deadline finishes, so the wait
# ends within one poll of APPLICATIONS_WAIT.
await_applications_gone() {
    local held="${1:-}" started now deadline nap app remaining
    started="$(date +%s)"
    deadline=$((started + APPLICATIONS_WAIT))
    while :; do
        clear_stranded_keda_finalizers
        remaining=""
        while read -r app; do
            if [[ -n "${app}" && " ${held} " != *" ${app#*/} "* ]]; then
                remaining="${remaining} ${app}"
            fi
        done < <(kubectl -n argocd get applications.argoproj.io -o name 2>/dev/null || true)
        if [[ -z "${remaining}" ]]; then
            return 0
        fi
        now="$(date +%s)"
        (( now < deadline )) || break
        nap=$((deadline - now))
        (( nap <= 5 )) || nap=5
        sleep "${nap}"
    done
    echo "  WARN: ArgoCD Applications still present after $((now - started))s, continuing"
}

# Nothing clears resources-finalizer.argocd.argoproj.io once Argo is gone, and an
# Application still carrying it holds the argocd namespace in Terminating for good.
release_stranded_applications() {
    local app
    while read -r app; do
        [[ -n "${app}" ]] || continue
        if kubectl -n argocd patch "${app}" --type merge \
            -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1; then
            echo "  Argo is about to go: cleared the finalizers on ${app}, which never finished deleting its resources"
        else
            echo "  WARN: could not clear the finalizers on ${app}"
        fi
    done < <(kubectl -n argocd get applications.argoproj.io -o name 2>/dev/null || true)
}

# Bounded, so a namespace held by a finalizer whose controller is already gone
# cannot stall the teardown; one still terminating says what holds it.
delete_namespace() {
    local ns="${1}" held
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "[DRY-RUN] kubectl delete ns ${ns} --ignore-not-found --timeout=${DELETE_TIMEOUT}"
        return 0
    fi
    kubectl delete ns "${ns}" --ignore-not-found --timeout="${DELETE_TIMEOUT}" 2>/dev/null && return 0
    held="$(kubectl get ns "${ns}" -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.message}{" "}{end}' 2>/dev/null || true)"
    echo "  WARN: namespace ${ns} still terminating after ${DELETE_TIMEOUT}, continuing: ${held:-nothing on it says why}"
}

# Success when this teardown deletes the namespace; anything elsewhere belongs to
# another tenant on the cluster and is never touched.
torn_down_namespace() {
    local listed
    [[ "${1}" == dfe-* || "${1}" == keda ]] && return 0
    for listed in "${DATA_NAMESPACES[@]}" "${PLATFORM_NAMESPACES[@]}"; do
        [[ "${1}" == "${listed}" ]] && return 0
    done
    return 1
}

# The cluster secret lives in the argocd namespace step 7 deletes, so it is read first.
# A bootstrap that stopped before writing it, or a second teardown, leaves none, and
# then the caller's DFE_* facts stand in: `dfe-ops teardown --from-terraform` passes them.
read_cluster_facts() {
    local facts
    if facts="$(kubectl -n argocd get secret dfe-cluster -o jsonpath='{.metadata.annotations.dfe\.hyperi\.io/cloud}{"\n"}{.metadata.annotations.dfe\.hyperi\.io/region}{"\n"}{.metadata.annotations.dfe\.hyperi\.io/cluster_name}{"\n"}{.metadata.annotations.dfe\.hyperi\.io/vpc_id}{"\n"}{.metadata.annotations.dfe\.hyperi\.io/run_tags}{"\n"}' 2>/dev/null)"; then
        { read -r CLUSTER_CLOUD; read -r CLUSTER_REGION; read -r CLUSTER_NAME; read -r CLUSTER_VPC; read -r CLUSTER_RUN_TAGS; } <<<"${facts}" || true
    fi
    [[ -z "${CLUSTER_CLOUD}" ]] || return 0
    CLUSTER_CLOUD="${DFE_CLOUD:-}"
    CLUSTER_REGION="${DFE_REGION:-}"
    CLUSTER_NAME="${DFE_KUBE_CLUSTER_NAME:-}"
    CLUSTER_VPC="${DFE_VPC_ID:-}"
    CLUSTER_RUN_TAGS="${DFE_RUN_TAGS:-}"
    CLUSTER_FACTS_SOURCE="the DFE_* environment"
}

# An Application that syncs ApplicationSets recreates any one deleted, and its own delete then
# cascades through every Application the copy adopted, both controllers included.
release_applicationset_owners() {
    local name kinds
    while read -r name kinds; do
        [[ -n "${name}" && " ${kinds} " == *" ApplicationSet "* ]] || continue
        if kubectl -n argocd patch applications.argoproj.io "${name}" --type merge \
            -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1 \
            && kubectl -n argocd delete applications.argoproj.io "${name}" --wait=false >/dev/null 2>&1; then
            echo "  ${name} syncs ApplicationSets: deleted it without its finalizer, so it neither puts them back nor cascades through them"
        else
            echo "  WARN: could not delete ${name}, which syncs ApplicationSets, without its finalizer"
        fi
    done < <(kubectl -n argocd get applications.argoproj.io -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.resources[*].kind}{"\n"}{end}' 2>/dev/null || true)
}

# Success when ${1}, an Application's sources, names chart ${2}. A digest-pinned
# chart is an OCI source with no chart field, so its name ends the repoURL. A git
# repoURL ending the same way names a repo.
names_chart() {
    local source sources
    read -ra sources <<<"${1}"
    for source in "${sources[@]}"; do
        if [[ "${source}" == "${2}" || ( "${source}" == oci://* && "${source}" == */"${2}" ) ]]; then
            return 0
        fi
    done
    return 1
}

# "<role> <name>" per Application -- lb, karpenter, pools, operator (a data
# operator) or other -- and nothing when they cannot be listed. pools goes by
# the Application's name, which layer2-platform.yaml keeps while its chart moves.
application_roles() {
    local listing name sources role
    listing="$(kubectl -n argocd get applications.argoproj.io -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.source.chart}{" "}{.spec.source.path}{" "}{.spec.source.repoURL}{" "}{.spec.sources[*].chart}{" "}{.spec.sources[*].path}{" "}{.spec.sources[*].repoURL}{"\n"}{end}' 2>/dev/null)" || return 0
    while read -r name sources; do
        [[ -n "${name}" ]] || continue
        if [[ "${name}" == karpenter-pools-* ]]; then
            role=pools
        elif names_chart "${sources}" aws-load-balancer-controller; then
            role=lb
        elif names_chart "${sources}" karpenter; then
            role=karpenter
        elif names_chart "${sources}" strimzi-kafka-operator || names_chart "${sources}" clickhouse-operator-helm \
            || names_chart "${sources}" documentdb-operator || [[ " ${sources} " == *"//charts.redpanda.com"* ]]; then
            role=operator
        else
            role=other
        fi
        echo "${role} ${name}"
    done <<<"${listing}"
}

# The names of the Applications in ${2} with role ${1}, space-separated.
names_in_role() {
    local want="${1}" role name names=""
    while read -r role name; do
        if [[ "${role}" == "${want}" && -n "${name}" ]]; then
            names="${names} ${name}"
        fi
    done <<<"${2}"
    echo "${names# }"
}

# Deletes without waiting every Application in ${1} but the data operators',
# which step 3 deletes once their CRs are gone, or all of them when none is one.
delete_applications_but_operators() {
    local role name names=()
    if [[ -z "$(names_in_role operator "${1}")" ]]; then
        kubectl -n argocd delete applications.argoproj.io --all --wait=false 2>/dev/null || true
        return 0
    fi
    while read -r role name; do
        if [[ "${role}" != operator && -n "${name}" ]]; then
            names+=("${name}")
        fi
    done <<<"${1}"
    (( ${#names[@]} > 0 )) || return 0
    kubectl -n argocd delete applications.argoproj.io "${names[@]}" --wait=false 2>/dev/null || true
}

# Only its operator clears a data CR's finalizer, and while a CR stands its
# operator puts back any PodDisruptionBudget deleted from under it, so each CR
# goes while step 1 keeps the operators running, before the drain. Bounded all the same.
delete_data_resources() {
    run kubectl -n strimzi delete kafka --all --timeout="${DELETE_TIMEOUT}" 2>/dev/null || true
    # The kafka chart's Redpanda cluster, on the redpanda provider.
    run kubectl -n strimzi delete redpandas.cluster.redpanda.com --all --timeout="${DELETE_TIMEOUT}" 2>/dev/null || true
    # HyperDX's store. Its operator clears the CR's finalizer, and the CloudNativePG
    # Cluster it owns goes with it, or with the delete after it.
    run kubectl -n cnpg delete dbs.documentdb.io --all --timeout="${DELETE_TIMEOUT}" 2>/dev/null || true
    run kubectl -n cnpg delete clusters.postgresql.cnpg.io --all --timeout="${DELETE_TIMEOUT}" 2>/dev/null || true
    # ClickHouseCluster and KeeperCluster are the clickhouse.com operator's kinds,
    # clickhouseinstallation the Altinity operator's, and any left keeps its
    # finalizer and wedges step 4's namespace delete.
    run kubectl -n clickhouse delete clickhousecluster --all --timeout="${DELETE_TIMEOUT}" 2>/dev/null || true
    run kubectl -n clickhouse delete keepercluster --all --timeout="${DELETE_TIMEOUT}" 2>/dev/null || true
    run kubectl -n clickhouse delete clickhouseinstallation --all --timeout="${DELETE_TIMEOUT}" 2>/dev/null || true
}

# A PodDisruptionBudget or karpenter.sh/do-not-disrupt in a namespace this
# teardown removes protects nothing now but still holds a draining node, so
# both go before the pools.
release_drain_blockers() {
    local ns name hold
    while IFS='|' read -r ns name; do
        if [[ -z "${name}" ]] || ! torn_down_namespace "${ns}"; then
            continue
        fi
        kubectl -n "${ns}" delete poddisruptionbudgets.policy "${name}" --ignore-not-found --wait=false >/dev/null 2>&1 \
            || echo "  WARN: could not delete PodDisruptionBudget ${ns}/${name}"
    done < <(kubectl get poddisruptionbudgets.policy -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"|"}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)
    while IFS='|' read -r ns name hold; do
        if [[ -z "${hold}" ]] || ! torn_down_namespace "${ns}"; then
            continue
        fi
        kubectl -n "${ns}" annotate pod "${name}" karpenter.sh/do-not-disrupt- >/dev/null 2>&1 \
            || echo "  WARN: could not take karpenter.sh/do-not-disrupt off pod ${ns}/${name}"
    done < <(kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"|"}{.metadata.name}{"|"}{.metadata.annotations.karpenter\.sh/do-not-disrupt}{"\n"}{end}' 2>/dev/null || true)
}

# Deletes without waiting the Applications in ${2} with role ${1}, failing when there are none.
delete_applications_in_role() {
    local want="${1}" role name names=()
    while read -r role name; do
        if [[ "${role}" == "${want}" && -n "${name}" ]]; then
            names+=("${name}")
        fi
    done <<<"${2}"
    (( ${#names[@]} > 0 )) || return 1
    kubectl -n argocd delete applications.argoproj.io "${names[@]}" --wait=false 2>/dev/null || true
}

# Bounded, and an unreadable answer never reads as gone: a node still running when Karpenter goes holds the subnets tofu destroy deletes.
await_nodeclaims_gone() {
    local started now deadline nap claims=""
    started="$(date +%s)"
    deadline=$((started + NODECLAIMS_WAIT))
    while :; do
        if claims="$(kubectl get nodeclaims.karpenter.sh -o name 2>/dev/null)" && [[ -z "${claims}" ]]; then
            return 0
        fi
        now="$(date +%s)"
        (( now < deadline )) || break
        nap=$((deadline - now))
        (( nap <= 5 )) || nap=5
        sleep "${nap}"
    done
    claims="${claims//$'\n'/ }"
    echo "  WARN: Karpenter still holds ${claims:-NodeClaims the cluster would not list} after $((now - started))s, continuing; step 9 terminates a test run's nodes"
}

# Prints resource|namespace|name|deletionTimestamp|finalizers for each Gateway,
# LoadBalancer Service, Ingress and TargetGroupBinding. Fails when the Services
# or Ingresses cannot be listed; the other two kinds exist only where the edge
# module or the load balancer controller installed their CRDs.
load_balancer_objects() {
    local fields='|{.metadata.namespace}|{.metadata.name}|{.metadata.deletionTimestamp}|{.metadata.finalizers}{"\n"}{end}'
    local status=0
    kubectl get gateways.gateway.networking.k8s.io -A -o jsonpath="{range .items[*]}gateways.gateway.networking.k8s.io${fields}" 2>/dev/null || true
    kubectl get services -A -o jsonpath="{range .items[?(@.spec.type==\"LoadBalancer\")]}services${fields}" 2>/dev/null || status=1
    kubectl get ingresses.networking.k8s.io -A -o jsonpath="{range .items[*]}ingresses.networking.k8s.io${fields}" 2>/dev/null || status=1
    kubectl get targetgroupbindings.elbv2.k8s.aws -A -o jsonpath="{range .items[*]}targetgroupbindings.elbv2.k8s.aws${fields}" 2>/dev/null || true
    return "${status}"
}

# The AWS Load Balancer Controller deletes a load balancer, its target groups and
# its security groups only while it runs, and a security group it leaves behind
# stops tofu destroy deleting the VPC. So everything it reconciles in the
# namespaces this teardown removes is deleted, and it is kept until its *.k8s.aws/
# finalizers clear. Bounded like the Application wait, and an unreadable answer
# never reads as released.
release_load_balancers() {
    local started now deadline nap objects held resource ns name deleting finalizers
    started="$(date +%s)"
    deadline=$((started + LOAD_BALANCERS_WAIT))
    while :; do
        held=""
        objects="$(load_balancer_objects)" || held=" (the cluster did not list every Service and Ingress)"
        while IFS='|' read -r resource ns name deleting finalizers; do
            if [[ -z "${name}" ]] || ! torn_down_namespace "${ns}"; then
                continue
            fi
            if [[ -z "${deleting}" ]]; then
                kubectl -n "${ns}" delete "${resource}" "${name}" --ignore-not-found --wait=false 2>/dev/null \
                    || echo "  WARN: could not delete ${resource} ${ns}/${name}"
            fi
            if [[ "${finalizers}" == *k8s.aws/* ]]; then
                held="${held} ${resource%%.*}/${ns}/${name}"
            fi
        done <<<"${objects}"
        [[ -n "${held}" ]] || return 0
        now="$(date +%s)"
        (( now < deadline )) || break
        nap=$((deadline - now))
        (( nap <= 5 )) || nap=5
        sleep "${nap}"
    done
    echo "  WARN: the load balancer controller still holds${held} after $((now - started))s, continuing"
}

# The aws CLI's error code alone: its full message can name the caller's account and role.
aws_error_code() {
    local pattern='\(([A-Za-z.]+)\)'
    if [[ "${1}" =~ ${pattern} ]]; then
        echo "${BASH_REMATCH[1]}"
    else
        echo "an error with no code"
    fi
}

# "delete <id>" or "keep <id> <why>" for each security group, network interface or
# instance in the aws JSON on stdin. Only one in the VPC named by ${2}, detached,
# not already terminating, and carrying every run tag in the JSON object ${1},
# every key=value after ${2} and every bare key after ${2} with any value, is ever
# marked delete, so a filter the API did not apply widens nothing.
select_run_owned() {
    python3 -c '
import json, sys
try:
    need = json.loads(sys.argv[1])
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
except ValueError:
    sys.exit(2)
if not isinstance(need, dict) or not need or not all(isinstance(v, str) and v for v in need.values()):
    sys.exit(2)
need.update(arg.split("=", 1) for arg in sys.argv[3:] if "=" in arg)
present = [arg for arg in sys.argv[3:] if "=" not in arg]
items = data.get("SecurityGroups", []) + data.get("NetworkInterfaces", [])
items += [i for r in data.get("Reservations", []) for i in r.get("Instances", [])]
for item in items:
    ident = item.get("GroupId") or item.get("NetworkInterfaceId") or item.get("InstanceId")
    tags = {t.get("Key"): t.get("Value") for t in item.get("Tags") or item.get("TagSet") or []}
    missing = sorted([k for k, v in need.items() if tags.get(k) != v] + [k for k in present if not tags.get(k)])
    if item.get("VpcId") != sys.argv[2]:
        print("keep", ident, "it is outside the run VPC")
    elif item.get("Status", "available") != "available":
        print("keep", ident, "it is still attached")
    elif (item.get("State") or {}).get("Name") in ("shutting-down", "terminated"):
        print("keep", ident, "it is already terminating")
    elif missing:
        print("keep", ident, "it does not carry", ",".join(missing))
    else:
        print("delete", ident)
' "$@"
}

# A detached network interface still holding the group stops it being deleted.
delete_detached_interfaces() {
    local group="${1}" interfaces verdicts verdict id why error
    if ! interfaces="$(aws ec2 describe-network-interfaces --region "${CLUSTER_REGION}" --output json \
        --filters "Name=vpc-id,Values=${CLUSTER_VPC}" "Name=group-id,Values=${group}" 2>/dev/null)" \
        || ! verdicts="$(select_run_owned "${CLUSTER_RUN_TAGS}" "${CLUSTER_VPC}" <<<"${interfaces}")"; then
        echo "  WARN: could not list the network interfaces holding ${group}"
        return 0
    fi
    while read -r verdict id why; do
        if [[ -z "${id}" ]]; then
            continue
        elif [[ "${verdict}" != "delete" ]]; then
            echo "  left network interface ${id} on ${group}: ${why}"
        elif error="$(aws ec2 delete-network-interface --region "${CLUSTER_REGION}" --network-interface-id "${id}" 2>&1 >/dev/null)"; then
            echo "  deleted network interface ${id}, which held ${group}"
        else
            echo "  WARN: could not delete network interface ${id}: $(aws_error_code "${error}")"
        fi
    done <<<"${verdicts}"
}

# Success when the cluster facts name an AWS test run step 9 can sweep, and says why not otherwise.
test_run_sweepable() {
    if [[ "${CLUSTER_FACTS_SOURCE}" != "the cluster secret" ]]; then
        echo "  the cluster secret names no cloud, so the run's facts come from DFE_CLOUD, DFE_REGION, DFE_KUBE_CLUSTER_NAME, DFE_VPC_ID and DFE_RUN_TAGS"
    fi
    if [[ -z "${CLUSTER_CLOUD}" ]]; then
        echo "  neither the cluster secret nor DFE_CLOUD names a cloud, so nothing marks a test run to sweep"
        return 1
    fi
    if [[ "${CLUSTER_CLOUD}" != "aws" ]]; then
        echo "  ${CLUSTER_FACTS_SOURCE} names ${CLUSTER_CLOUD}, not aws, so there is nothing to sweep"
        return 1
    fi
    if [[ -z "${CLUSTER_RUN_TAGS}" || -z "${CLUSTER_VPC}" || -z "${CLUSTER_NAME}" || -z "${CLUSTER_REGION}" ]]; then
        echo "  ${CLUSTER_FACTS_SOURCE} names no run tags, VPC, cluster or region, so nothing is marked as a test run's"
        return 1
    fi
    if ! command -v aws >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
        echo "  WARN: no aws CLI or python3 on PATH, so nothing was swept"
        return 1
    fi
}

# Only a test run's nodes are terminated: in its VPC, owned by its cluster, launched from a Karpenter pool and carrying every run tag.
terminate_karpenter_nodes() {
    local reservations verdicts verdict id why error nodes=()
    if ! reservations="$(aws ec2 describe-instances --region "${CLUSTER_REGION}" --output json \
        --filters "Name=vpc-id,Values=${CLUSTER_VPC}" "Name=tag:kubernetes.io/cluster/${CLUSTER_NAME},Values=owned" \
        "Name=tag-key,Values=karpenter.sh/nodepool" "Name=instance-state-name,Values=pending,running,stopping,stopped" 2>/dev/null)"; then
        echo "  WARN: could not list the instances in ${CLUSTER_VPC}"
        return 0
    fi
    if ! verdicts="$(select_run_owned "${CLUSTER_RUN_TAGS}" "${CLUSTER_VPC}" "kubernetes.io/cluster/${CLUSTER_NAME}=owned" karpenter.sh/nodepool <<<"${reservations}")"; then
        echo "  WARN: the run tags in ${CLUSTER_FACTS_SOURCE} are not a JSON object of strings, so no instance was terminated"
        return 0
    fi
    while read -r verdict id why; do
        if [[ -z "${id}" ]]; then
            continue
        elif [[ "${verdict}" != "delete" ]]; then
            echo "  left instance ${id}: ${why}"
        else
            nodes+=("${id}")
        fi
    done <<<"${verdicts}"
    if (( ${#nodes[@]} == 0 )); then
        echo "  no node a Karpenter pool launched for this run is left in ${CLUSTER_VPC}"
        return 0
    fi
    if ! error="$(aws ec2 terminate-instances --region "${CLUSTER_REGION}" --instance-ids "${nodes[@]}" 2>&1 >/dev/null)"; then
        echo "  WARN: could not terminate ${nodes[*]}: $(aws_error_code "${error}")"
        return 0
    fi
    echo "  terminating nodes Karpenter left running: ${nodes[*]}"
    # The waiter gives up after its own 40 polls 15s apart, so this cannot stall the teardown.
    if error="$(aws ec2 wait instance-terminated --region "${CLUSTER_REGION}" --instance-ids "${nodes[@]}" 2>&1 >/dev/null)"; then
        echo "  terminated ${nodes[*]}"
    else
        echo "  WARN: ${nodes[*]} not yet terminated, continuing: $(aws_error_code "${error}")"
    fi
}

# A security group the load balancer controller did not get to delete stops tofu
# destroy deleting the VPC. Only a test run's are swept: in its VPC, carrying the
# controller's cluster tag and every run tag the controller was told to apply.
sweep_load_balancer_security_groups() {
    local groups verdicts verdict id why error swept=0
    if ! groups="$(aws ec2 describe-security-groups --region "${CLUSTER_REGION}" --output json \
        --filters "Name=vpc-id,Values=${CLUSTER_VPC}" "Name=tag:elbv2.k8s.aws/cluster,Values=${CLUSTER_NAME}" 2>/dev/null)"; then
        echo "  WARN: could not list the security groups in ${CLUSTER_VPC}"
        return 0
    fi
    if ! verdicts="$(select_run_owned "${CLUSTER_RUN_TAGS}" "${CLUSTER_VPC}" "elbv2.k8s.aws/cluster=${CLUSTER_NAME}" <<<"${groups}")"; then
        echo "  WARN: the run tags in ${CLUSTER_FACTS_SOURCE} are not a JSON object of strings, so no security group was swept"
        return 0
    fi
    while read -r verdict id why; do
        if [[ -z "${id}" ]]; then
            continue
        elif [[ "${verdict}" != "delete" ]]; then
            echo "  left security group ${id}: ${why}"
            continue
        fi
        swept=$((swept + 1))
        delete_detached_interfaces "${id}"
        if error="$(aws ec2 delete-security-group --region "${CLUSTER_REGION}" --group-id "${id}" 2>&1 >/dev/null)"; then
            echo "  deleted security group ${id}"
        else
            echo "  WARN: could not delete security group ${id}: $(aws_error_code "${error}")"
        fi
    done <<<"${verdicts}"
    (( swept > 0 )) || echo "  no security group of this run's load balancers is left in ${CLUSTER_VPC}"
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

if [[ "${DRY_RUN}" != "true" ]]; then
    read_cluster_facts
fi

echo "==> [1/9] Deleting ArgoCD Applications + ApplicationSets, and the DFE data CRs"
# Orphaned, so Argo stops generating Applications while each one stays, still
# carrying the finalizer that deletes what it deployed, until it is deleted below.
run kubectl -n argocd delete applicationset --all --cascade=orphan 2>/dev/null || true
# Then whatever syncs ApplicationSets, and once more for any it put back in between.
if [[ "${DRY_RUN}" != "true" ]]; then
    release_applicationset_owners
    kubectl -n argocd delete applicationset --all --cascade=orphan 2>/dev/null || true
else
    echo "[DRY-RUN] delete every Application that syncs ApplicationSets without its finalizer, then orphan the ApplicationSets again"
fi
# KEDA clears its own finalizer.keda.sh, so its resources go while it still runs.
run kubectl delete scaledobject --all -A --timeout=30s 2>/dev/null || true
run kubectl delete scaledjob --all -A --timeout=30s 2>/dev/null || true
run kubectl delete triggerauthentication --all -A --timeout=30s 2>/dev/null || true
# The load balancer controller's Application goes last, once it has released
# every load balancer the others took. The data CRs, then Karpenter's pools,
# follow the load balancers, its controller their nodes, the operators step 3.
if [[ "${DRY_RUN}" != "true" ]]; then
    roles="$(application_roles)"
    delete_applications_in_role other "${roles}" || true
    release_load_balancers
    delete_data_resources
    release_drain_blockers
    if delete_applications_in_role pools "${roles}"; then
        await_nodeclaims_gone
    fi
    # Fully qualified: on a Rancher-managed cluster the bare `app` resolves to
    # app.catalog.cattle.io and every Argo Application survives the teardown.
    delete_applications_but_operators "${roles}"
    await_applications_gone "$(names_in_role operator "${roles}")"
else
    echo "[DRY-RUN] delete every Application but the AWS Load Balancer Controller's, Karpenter's, karpenter-pools and the data operators', then every Gateway, LoadBalancer Service, Ingress and TargetGroupBinding in the namespaces this teardown removes, and wait up to ${LOAD_BALANCERS_WAIT}s for the controller's finalizers to clear"
    delete_data_resources
    echo "[DRY-RUN] delete every PodDisruptionBudget and take karpenter.sh/do-not-disrupt off every pod in the namespaces this teardown removes"
    echo "[DRY-RUN] delete karpenter-pools, and wait up to ${NODECLAIMS_WAIT}s for Karpenter to remove every NodeClaim before its controller goes"
    echo "[DRY-RUN] kubectl -n argocd delete applications.argoproj.io --all --wait=false, the data operators' excepted"
    echo "[DRY-RUN] wait for the Applications to go, clearing finalizer.keda.sh from any KEDA resource still terminating once no KEDA operator pod is running"
fi

echo "==> [2/9] Waiting for ArgoCD to clean up managed resources..."
if [[ "${DRY_RUN}" != "true" ]]; then
    sleep 10
fi

echo "==> [3/9] Deleting the data operators"
# Step 1 deleted the CRs they finalize, so they go once the nodes have drained.
if [[ "${DRY_RUN}" != "true" ]]; then
    if delete_applications_in_role operator "${roles}"; then
        await_applications_gone
    fi
    sleep 5
else
    echo "[DRY-RUN] delete the data operators' Applications, and wait up to ${APPLICATIONS_WAIT}s for them to go"
fi

echo "==> [4/9] Deleting DFE namespaces"
# DATA_NAMESPACES: data-plane + operator + bundled deploy-repo (Forgejo) namespaces.
# The operator namespaces (clickhouse-operator-system/redpanda-operator) and kafka
# exist only in some profiles; --ignore-not-found makes listing them harmless when a
# profile did not create them. Missing any there strands the namespace (+ its
# finalizers) after a teardown, which then blocks a clean redeploy.
# clickhouse-operator is the pre-rc.7 namespace, kept so a teardown of an older
# deploy cannot leave a second operator reconciling the same CRs.
# Deleting the external-dns namespace here does not itself clean up the DNS
# records it published on AWS: sync policy only removes a record once it has
# reconciled the Service/Ingress deletion from step 1, and this script gives
# it no guaranteed time to do that before its pod is gone. That is what
# terraform/modules/kubernetes-cluster/aws/dns.tf's destroy-time cleanup is
# for -- it empties the private zone itself, independent of whether
# external-dns ever got the chance.
for ns in "${DATA_NAMESPACES[@]}"; do
    delete_namespace "${ns}"
done
# KEDA registers the external-metrics APIService cluster-wide; deleting its
# namespace strands the registration, which wedges the metrics API and every
# later readiness/teardown pass that touches it.
run kubectl delete apiservice v1beta1.external.metrics.k8s.io --ignore-not-found 2>/dev/null || true
# Delete any dfe-* namespaces. grep exits 1 on no match, which pipefail
# would turn into an abort on an already-clean cluster.
if [[ "${DRY_RUN}" != "true" ]]; then
    { kubectl get ns -o name 2>/dev/null | grep "namespace/dfe-" || true; } | while read -r ns; do
        delete_namespace "${ns#namespace/}"
    done
else
    echo "[DRY-RUN] kubectl delete ns dfe-*"
fi
# Last, once no ScaledObject can still need its operator.
delete_namespace keda

echo "==> [5/9] Deleting the Strimzi CRDs"
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

echo "==> [6/9] Uninstalling ArgoCD + Valkey"
if [[ "${DRY_RUN}" != "true" ]]; then
    release_stranded_applications
else
    echo "[DRY-RUN] clear the finalizers on any Application still present, which nothing clears once Argo is gone"
fi
run helm uninstall argocd -n argocd 2>/dev/null || true
run helm uninstall dfe-valkey -n argocd 2>/dev/null || true

echo "==> [7/9] Uninstalling Layer 1 (cert-manager, ESO)"
# Only ESO clears an ExternalSecret's finalizer and only cert-manager an ACME
# challenge's, so a namespace goes while both still run, and a controller's own
# namespace once its release is uninstalled.
delete_namespace argocd
delete_namespace envoy-gateway-system
run helm uninstall cert-manager -n cert-manager 2>/dev/null || true
delete_namespace cert-manager
run helm uninstall external-secrets -n external-secrets 2>/dev/null || true

echo "==> [8/9] Cleaning up namespaces"
delete_namespace external-secrets
# destroy.sh cannot tell a MetalLB bootstrap installed from one the cluster
# already carried, and removing an adopted LoadBalancer provider would strand
# every other tenant's Service on the cluster.
echo "  metallb-system and its address pool left in place -- an adopted LoadBalancer provider is never removed"

echo "==> [9/9] Removing the nodes and load balancer security groups left in a test run's VPC"
if [[ "${DRY_RUN}" != "true" ]]; then
    if test_run_sweepable; then
        terminate_karpenter_nodes
        sweep_load_balancer_security_groups
    fi
else
    echo "[DRY-RUN] terminate each instance in the run's VPC that a Karpenter pool launched for its cluster and that carries every run tag"
    echo "[DRY-RUN] delete each security group in the run's VPC carrying the load balancer controller's cluster tag and every run tag, with its detached network interfaces"
fi

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
