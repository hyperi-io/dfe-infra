#!/usr/bin/env bash
#
#  Project:      dfe-infra
#  File:         helm/charts/dfe-toolbox/files/idle-check.sh
#  Purpose:      Scale the toolbox Deployment to zero once its pod has been idle or reached its ceiling.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Run by the dfe-toolbox CronJob. Reads NAMESPACE DEPLOYMENT SELECTOR and the
#  limits IDLE_SECONDS, IDLE_WARN_SECONDS and TTL_SECONDS (empty = not set) from
#  the environment. It lists the toolbox pod, asks it for its activity through
#  probe.sh, hands the numbers to decide.sh and acts on the answer: a notice
#  into every open terminal through warn.sh, or a scale to zero.
#  A pod that cannot be probed counts as busy, so only the ttl ceiling can scale it.

set -euo pipefail

readonly SCRIPTS="${SCRIPTS:-/opt/dfe-idle}"
# argocd/appsets/layer2-platform.yaml ignores the replicas this manager owns, so Argo CD's selfHeal leaves a scale to zero alone.
# The patch goes to the Deployment itself: a write through the scale subresource leaves no managedFields entry to ignore.
readonly FIELD_MANAGER=dfe-toolbox-idle

field() {
    # The probe prints key=value lines, so a field is read by name and never evaluated.
    local key="${1}" probe="${2}" line
    while IFS= read -r line; do
        if [[ "${line}" == "${key}="* ]]; then
            printf '%s' "${line#"${key}="}"
            return 0
        fi
    done <<<"${probe}"
    return 0
}

main() {
    local namespace="${NAMESPACE:?NAMESPACE}" deployment="${DEPLOYMENT:?DEPLOYMENT}" selector="${SELECTOR:?SELECTOR}"
    local rows pod start start_epoch now probe="" activity="" busy=1 warned=""

    rows=$(kubectl get pods -n "${namespace}" -l "${selector}" \
        --field-selector=status.phase=Running \
        -o jsonpath='{range .items[*]}{.metadata.name} {.status.startTime}{"\n"}{end}')
    if [[ -z "${rows}" ]]; then
        echo "no running toolbox pod in ${namespace} -- nothing to scale"
        return 0
    fi
    read -r pod start <<<"${rows}"
    start_epoch=$(date -u -d "${start}" +%s)
    now=$(date -u +%s)

    if probe=$(kubectl exec -n "${namespace}" "${pod}" -c toolbox -- bash "${SCRIPTS}/probe.sh"); then
        activity=$(field activity "${probe}")
        busy=$(field busy "${probe}")
        warned=$(field warned "${probe}")
        # A probe that names no busy state is unknown, and unknown is busy.
        [[ -n "${busy}" ]] || busy=1
    else
        echo "could not probe ${pod} -- treating it as busy, so only the ttl ceiling can scale it" >&2
    fi

    local decision action reason left token
    decision=$(
        env NOW="${now}" START="${start_epoch}" ACTIVITY="${activity}" BUSY="${busy}" WARNED="${warned}" \
            IDLE="${IDLE_SECONDS:-}" WARN="${IDLE_WARN_SECONDS:-0}" TTL="${TTL_SECONDS:-}" \
            bash "${SCRIPTS}/decide.sh"
    )
    read -r action reason left token <<<"${decision}"

    case "${action}" in
        scale)
            echo "${pod}: ${reason} limit reached -- scaling ${deployment} to zero"
            kubectl patch "deployment/${deployment}" -n "${namespace}" \
                --type=merge -p '{"spec":{"replicas":0}}' --field-manager="${FIELD_MANAGER}"
            ;;
        warn)
            echo "${pod}: ${reason} limit in ${left}s -- warning the open terminals"
            kubectl exec -n "${namespace}" "${pod}" -c toolbox -- bash "${SCRIPTS}/warn.sh" "${left}" "${reason}" "${token}"
            ;;
        *)
            echo "${pod}: ${left}s left on the ${reason} limit -- leaving it running"
            ;;
    esac
}

main
