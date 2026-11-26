#!/usr/bin/env bash
#
#  Project:      dfe-infra
#  File:         helm/charts/dfe-toolbox/files/decide.sh
#  Purpose:      Decide whether the toolbox pod is kept, warned or scaled to zero.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Reads NOW START ACTIVITY BUSY WARNED IDLE WARN TTL from the environment (epoch
#  seconds, ACTIVITY and WARNED empty when the pod has none) and prints
#  "<action> <reason> <seconds-left> <token>" with action keep, warn or scale.
#  The idle clock starts at the newest of START and ACTIVITY, and a running
#  command resets it. The ttl ceiling counts from START and wins over activity.
#
#  Plain bash with no Python because the toolbox image carries none, and it is
#  the one place the rule that decides a scale-down lives, so it has a table test
#  (scripts/tests/test_toolbox_idle.py).

set -euo pipefail

die() {
    echo "decide: ${1}" >&2
    exit 2
}

# A value read from the pod is data, so a non-number stops the check before it reaches the arithmetic.
require_number() {
    [[ "${2}" =~ ^[0-9]+$ ]] || die "${1} is not a whole number: '${2}'"
}

optional_number() {
    [[ -z "${2}" ]] || require_number "${1}" "${2}"
}

main() {
    local now="${NOW:-}" start="${START:-}" activity="${ACTIVITY:-}" busy="${BUSY:-0}"
    local warned="${WARNED:-}" idle="${IDLE:-}" warn="${WARN:-0}" ttl="${TTL:-}"

    require_number NOW "${now}"
    require_number START "${start}"
    require_number BUSY "${busy}"
    require_number WARN "${warn}"
    optional_number ACTIVITY "${activity}"
    optional_number IDLE "${idle}"
    optional_number TTL "${ttl}"

    # Base 10 explicitly, because bash reads a leading 0 as octal and a limit written 0300 or 08 would fail or shift.
    now=$((10#${now}))
    start=$((10#${start}))
    warn=$((10#${warn}))
    busy=$((10#${busy}))
    [[ -z "${activity}" ]] || activity=$((10#${activity}))
    [[ -z "${idle}" ]] || idle=$((10#${idle}))
    [[ -z "${ttl}" ]] || ttl=$((10#${ttl}))

    local ref="${start}"
    if [[ -n "${activity}" ]] && ((activity > ref)); then
        ref="${activity}"
    fi
    if ((busy != 0)); then
        ref="${now}"
    fi
    # A pod clock ahead of the CronJob's would otherwise read as negative idle time.
    if ((ref > now)); then
        ref="${now}"
    fi

    local left="" reason="" token=""
    if [[ -n "${idle}" ]]; then
        left=$((idle - (now - ref)))
        reason=idle
        token="i${ref}"
    fi
    if [[ -n "${ttl}" ]]; then
        local ttl_left=$((ttl - (now - start)))
        if [[ -z "${left}" ]] || ((ttl_left < left)); then
            left="${ttl_left}"
            reason=ttl
            token="t${start}"
        fi
    fi

    if [[ -z "${left}" ]]; then
        echo "keep none 0 -"
    elif ((left <= 0)); then
        echo "scale ${reason} 0 ${token}"
    elif ((left <= warn)) && [[ "${warned}" != "${token}" ]]; then
        echo "warn ${reason} ${left} ${token}"
    else
        echo "keep ${reason} ${left} ${token}"
    fi
}

main
