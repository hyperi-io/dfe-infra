#  Project:      dfe-infra
#  File:         engine-clickhouse.sh
#  Purpose:      Reach ClickHouse from dfe-engine's own container, with the engine's own
#                host, port, scheme and credential.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

# Sourced, never executed, so it carries a shell directive not a shebang.
# shellcheck shell=bash

# Sets ch_url as the engine's settings resolve it (dfe-engine api/v1/hyperdx.py _ch_host_url), the URL HyperDX's proxy is handed too.
# An unset DFE_CLICKHOUSE_SECURE is https, the engine's own default.
# shellcheck disable=SC2016  # Expanded inside the engine container, not here.
ENGINE_CH_URL_SH='case "${DFE_CLICKHOUSE_SECURE:-true}" in [Tt][Rr][Uu][Ee] | 1 | [Yy][Ee][Ss]) scheme=https ;; *) scheme=http ;; esac
ch_url="${DFE_CLICKHOUSE_HOST:+${scheme}://${DFE_CLICKHOUSE_HOST}:${DFE_CLICKHOUSE_PORT:-8123}}"'

# Run by the engine container's own shell, so its host, port, scheme and credential never leave the pod.
# The credential reaches curl as an escaped config line on stdin, and the query on fd 3, so no argv carries it.
# shellcheck disable=SC2016  # Expanded inside the engine container, not here.
ENGINE_CH_QUERY="${ENGINE_CH_URL_SH}"'
insecure=
[ "${DFE_CLICKHOUSE_VERIFY:-}" = false ] && insecure=--insecure
exec 3<&0
{
  printf "user = \""
  printf "%s:%s" "${DFE_CLICKHOUSE_USERNAME:-default}" "${DFE_CLICKHOUSE_PASSWORD:-}" | sed "s/[\\\\\"]/\\\\&/g"
  printf "\"\n"
} | curl -fsS --max-time 30 $insecure ${DFE_CLICKHOUSE_CA_CERT:+--cacert "$DFE_CLICKHOUSE_CA_CERT"} -K - --data-binary @/dev/fd/3 "$ch_url/"'

# engine_ch_query NS QUERY: ClickHouse's answer to QUERY, asked from the engine in namespace NS.
engine_ch_query() {
    printf '%s' "${2}" | kubectl -n "${1}" exec -i "deploy/${DFE_ENGINE_DEPLOY:-dfe-engine}" -c dfe-engine -- sh -c "${ENGINE_CH_QUERY}" 2>/dev/null
}

# engine_ch_url NS: the ClickHouse URL the engine in namespace NS dials, empty when its environment cannot be read.
engine_ch_url() {
    kubectl -n "${1}" exec "deploy/${DFE_ENGINE_DEPLOY:-dfe-engine}" -c dfe-engine -- sh -c "${ENGINE_CH_URL_SH}"'
printf "%s" "$ch_url"' 2>/dev/null
}
