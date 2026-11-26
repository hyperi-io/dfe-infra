#!/usr/bin/env bash
#
#  Project:      dfe-infra
#  File:         helm/charts/dfe-toolbox/files/warn.sh
#  Purpose:      Tell every open terminal in the toolbox pod when it scales to zero.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage: warn.sh <seconds-left> <idle|ttl> <token>
#  Runs inside the toolbox pod through kubectl exec. The token is recorded so the
#  next check does not repeat the notice until activity moves the idle clock.

set -euo pipefail

# The two overrides exist for the table test, which points them at a scratch tree.
readonly PROC_ROOT="${DFE_TOOLBOX_PROC:-/proc}"
readonly WARNED_FILE="${DFE_TOOLBOX_STATE:-/tmp}/.dfe-toolbox-warned"

main() {
    local left="${1:?seconds left}" reason="${2:?idle or ttl}" token="${3:?token}"
    local mins=$(((left + 59) / 60)) what pid tty

    if [[ "${reason}" == idle ]]; then
        what="This pod has been idle and scales to zero in about ${mins} min. Run any command to keep it."
    else
        what="This pod reaches its time limit and scales to zero in about ${mins} min. A command does not extend it."
    fi

    # Every terminal is the stdin of its shell, and a terminal a process holds is one a person has open.
    for pid in "${PROC_ROOT}"/[0-9]*; do
        readlink "${pid}/fd/0" 2>/dev/null || true
    done | sort -u | while IFS= read -r tty; do
        [[ "${tty}" == /dev/pts/* ]] || continue
        printf '\r\n[dfe-toolbox] %s\r\n' "${what}" >"${tty}" 2>/dev/null || true
    done

    printf '%s' "${token}" >"${WARNED_FILE}"
}

main "$@"
