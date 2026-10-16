#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         wait-rest-ready.sh
#  Purpose:      Hold a Confluent Cloud apply until the cluster's REST endpoint
#                completes TLS handshakes in a row, so the keys, ACLs and topics
#                written over it are not failed by a reset from an ingress that
#                has no route to a new cluster yet.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage: wait-rest-ready.sh <url> <handshakes> <timeout-seconds> <interval-seconds>
#
#  Any HTTP status is a success: the probe sends no credential, and a 401 proves
#  the route. A reset, a TLS failure or a timeout is not. Needs bash 3.2 and
#  curl, so it runs on any Linux or macOS deployer.

set -euo pipefail

# One attempt's ceiling, so a dropped connection counts as a failure instead of stalling the apply.
readonly ATTEMPT_SECONDS=10

die() {
    printf 'wait-rest-ready: %s\n' "$*" >&2
    exit 1
}

main() {
    [[ $# -eq 4 ]] || die "usage: wait-rest-ready.sh <url> <handshakes> <timeout-seconds> <interval-seconds>"
    local url="$1" want="$2" timeout="$3" interval="$4"
    local number
    for number in "${want}" "${timeout}" "${interval}"; do
        [[ "${number}" =~ ^[0-9]+$ ]] || die "not a whole number: ${number}"
    done
    (( want > 0 )) || die "handshakes must be at least 1"
    command -v curl >/dev/null 2>&1 || die "curl is required to probe ${url}"

    local host="${url#*://}"
    host="${host%%/*}"
    host="${host%:*}"

    local deadline=$(( SECONDS + timeout ))
    local attempt=0 streak=0 out answer last
    while :; do
        attempt=$(( attempt + 1 ))
        # --disable comes first: a ~/.curlrc carrying --fail would turn the 401 that proves the route into a failure.
        if out="$(curl --disable --silent --show-error --output /dev/null --max-time "${ATTEMPT_SECONDS}" \
            --write-out '%{http_code} %{remote_ip}' "${url}" 2>&1)"; then
            streak=$(( streak + 1 ))
            answer="${out##*$'\n'}"
            last="HTTP ${answer%% *} from ${answer#* }"
        else
            streak=0
            last="${out%$'\n'*}"
            last="${last//$'\n'/ }"
        fi
        printf 'wait-rest-ready: %s attempt %d: %s (%d/%d in a row)\n' "${host}" "${attempt}" "${last}" "${streak}" "${want}"
        if (( streak >= want )); then
            return 0
        fi
        if (( SECONDS + interval >= deadline )); then
            die "${host} did not answer ${want} TLS handshakes in a row within ${timeout}s (${attempt} attempts, last: ${last}). Its REST endpoint is not routable from here: re-run the apply once it is, or check egress to ${host}:443."
        fi
        sleep "${interval}"
    done
}

main "$@"
