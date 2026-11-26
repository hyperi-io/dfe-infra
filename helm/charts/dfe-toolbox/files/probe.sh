#!/usr/bin/env bash
#
#  Project:      dfe-infra
#  File:         helm/charts/dfe-toolbox/files/probe.sh
#  Purpose:      Report the toolbox pod's activity for the scale-down check.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Runs inside the toolbox pod through kubectl exec and prints three lines:
#  activity= (the epoch the activity file holds, empty when there is none), busy= (1 when
#  a process other than a shell holds a terminal, so a long query or a log tail
#  counts) and warned= (the token the last notice was written for).

set -euo pipefail

# The two overrides exist for the table test, which points them at a scratch tree.
readonly PROC_ROOT="${DFE_TOOLBOX_PROC:-/proc}"
readonly STATE_DIR="${DFE_TOOLBOX_STATE:-/tmp}"
readonly ACTIVITY_FILE="${STATE_DIR}/.dfe-toolbox-activity"
readonly WARNED_FILE="${STATE_DIR}/.dfe-toolbox-warned"

main() {
    local activity="" warned="" busy=0 pid tty comm

    if [[ -r "${ACTIVITY_FILE}" ]]; then
        activity=$(cat "${ACTIVITY_FILE}" 2>/dev/null || true)
    fi
    # A torn or hand-edited file is no activity, so the idle clock falls back to the pod's start.
    [[ "${activity}" =~ ^[0-9]+$ ]] || activity=""
    if [[ -r "${WARNED_FILE}" ]]; then
        warned=$(cat "${WARNED_FILE}" 2>/dev/null || true)
    fi
    [[ "${warned}" =~ ^[it][0-9]+$ ]] || warned=""

    for pid in "${PROC_ROOT}"/[0-9]*; do
        tty=$(readlink "${pid}/fd/0" 2>/dev/null) || continue
        [[ "${tty}" == /dev/pts/* ]] || continue
        comm=$(cat "${pid}/comm" 2>/dev/null) || continue
        case "${comm}" in
            sh | bash | dash) ;;
            *)
                busy=1
                break
                ;;
        esac
    done

    printf 'activity=%s\nbusy=%s\nwarned=%s\n' "${activity}" "${busy}" "${warned}"
}

main
