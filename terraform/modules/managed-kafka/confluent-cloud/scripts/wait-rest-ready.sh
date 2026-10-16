#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         wait-rest-ready.sh
#  Purpose:      Hold a Confluent Cloud apply until the cluster's REST endpoint
#                lists its topics for the manager key, with HTTP 200, a number
#                of times in a row, so the ACLs and topics written over it are
#                not failed by a reset from an ingress that has no route to a
#                new cluster yet, or by a 401 for a key that has not synced.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage: CONFLUENT_REST_KEY=... CONFLUENT_REST_SECRET=... \
#             wait-rest-ready.sh <url> <successes> <timeout-seconds> <interval-seconds>
#
#  Only HTTP 200 counts. A 401 or 403 is a key the endpoint has not synced, and
#  a reset, a TLS failure or a timeout is an endpoint with no route yet: each
#  starts the count again. The credential reaches curl on stdin, never in an
#  argument. Needs bash 3.2 and curl, so it runs on any Linux or macOS deployer.

set -euo pipefail

# One attempt's ceiling, so a dropped connection counts as a failure instead of stalling the apply.
readonly ATTEMPT_SECONDS=10

# A curl config value is quoted, so it cannot carry a quote, a backslash or a control character.
readonly UNSAFE='["\\[:cntrl:]]'

die() {
    printf 'wait-rest-ready: %s\n' "$*" >&2
    exit 1
}

main() {
    [[ $# -eq 4 ]] || die "usage: wait-rest-ready.sh <url> <successes> <timeout-seconds> <interval-seconds>"
    local url="$1" want="$2" timeout="$3" interval="$4"
    local number
    for number in "${want}" "${timeout}" "${interval}"; do
        [[ "${number}" =~ ^[0-9]+$ ]] || die "not a whole number: ${number}"
    done
    (( want > 0 )) || die "successes must be at least 1"
    [[ -n "${CONFLUENT_REST_KEY:-}" && -n "${CONFLUENT_REST_SECRET:-}" ]] \
        || die "CONFLUENT_REST_KEY and CONFLUENT_REST_SECRET must carry the API key that lists the topics"
    local credential="${CONFLUENT_REST_KEY}:${CONFLUENT_REST_SECRET}"
    [[ ! "${credential}" =~ ${UNSAFE} ]] \
        || die "the API key or its secret holds a quote, a backslash or a control character, which curl cannot be handed"
    command -v curl >/dev/null 2>&1 || die "curl is required to probe ${url}"

    local host="${url#*://}"
    host="${host%%/*}"
    host="${host%:*}"

    local deadline=$(( SECONDS + timeout ))
    local attempt=0 streak=0 out answer code last
    while :; do
        attempt=$(( attempt + 1 ))
        # --disable comes first: a ~/.curlrc is the deployer's, and none of it belongs in this request.
        if out="$(printf 'user = "%s"\n' "${credential}" \
            | curl --disable --config - --silent --show-error --output /dev/null \
                --max-time "${ATTEMPT_SECONDS}" --write-out '%{http_code} %{remote_ip}' "${url}" 2>&1)"; then
            answer="${out##*$'\n'}"
            code="${answer%% *}"
            last="HTTP ${code} from ${answer#* }"
            if [[ "${code}" == "200" ]]; then
                streak=$(( streak + 1 ))
            else
                streak=0
            fi
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
            die "${host} did not list its topics with HTTP 200 ${want} times in a row within ${timeout}s (${attempt} attempts, last: ${last}). A reset is an endpoint not yet routed to this cluster, and a 401 a key it has not synced: re-run the apply once it answers, or check egress to ${host}:443."
        fi
        sleep "${interval}"
    done
}

main "$@"
