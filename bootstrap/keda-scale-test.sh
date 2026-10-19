#!/usr/bin/env bash
# Project:   dfe-infra
# File:      bootstrap/keda-scale-test.sh
# Purpose:   ARTIFICIAL +1-pod KEDA scale proof (the real "Phase C" test)
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# Proves KEDA scales Deployments through the fail-safe dfe-keda-shim, on demand,
# WITHOUT waiting for real load, in two phases.
#
# Phase 1 stands up a throwaway target whose ONLY trigger is the shim's pressure query
# for a test ServiceName, holds a high dfe_scaling_pressure row in ClickHouse, asserts
# the target scales OUT (1 -> 2), then holds zeros and asserts it scales back IN (2 -> 1)
# -- isolated from every real dfe-* app.
#
# Phase 2 does the same, all at once, for every app whose ScaledObject carries the shim's
# pressure trigger: each goes from its floor to one above it and back. A ScaledObject with
# no such trigger, or whose Deployment this profile does not run, is a reported SKIP.
#
# At scale-out every HPA's readings are logged, and the pressure reading must be at or
# over its target, so a scale that cpu drove cannot pass for one the pressure drove.
#
# An external ClickHouse (ClickHouse Cloud) has no pod to exec into, so the rows go in
# through the engine's own container, to the server the shim reads.
#
# Both phases are self-cleaning, and every wait is BOUNDED. Above a floor of zero the HPA
# reads the metric itself once a sync period and KEDA's pollingInterval plays no part, so
# the scale-out bound is that period twice over plus a pod-start allowance.
#
# Three verdicts: exit 0 proved it, exit 1 is a scaler that did not work, and exit 3 is
# UNPROVEN -- the HPA read the pressure over target and left the replicas alone, or a
# pressure-driven app was off its floor before the proof started. run-all-smoke-tests.sh
# fails the run on 3 as it does on 1.
#
# Usage:  bootstrap/keda-scale-test.sh [--namespace dfe] [--ch-namespace clickhouse]
#                                      [--ch-selector 'app.kubernetes.io/name in (dfe-clickhouse,clickhouse-server)']
#                                      [--shim dfe-keda-shim.dfe.svc.cluster.local:8080]
#                                      [--real-deployment dfe-receiver]   (phase 2 proves that app alone)
set -euo pipefail

NS="${DFE_NS:-${DFE_NAMESPACE:-dfe}}"
CH_NS="${DFE_CH_NS:-clickhouse}"
# Both layouts: the chart's StatefulSet labels pods dfe-clickhouse, the operator
# labels them clickhouse-server.
CH_SELECTOR="${DFE_CH_SELECTOR:-app.kubernetes.io/name in (dfe-clickhouse,clickhouse-server)}"
SHIM="${DFE_KEDA_SHIM:-dfe-keda-shim.${NS}.svc.cluster.local:8080}"
# Unique per run. Cleanup deletes the injected rows through an ALTER ... DELETE
# mutation, which is asynchronous, so a fixed name lets the previous run's zeros
# sit inside the shim's 60s averaging window and hold the next run below target.
TARGET="keda-scale-test-$$-${RANDOM}"
OTEL_DB="dfe"
# Every injected row carries this attribute: cleanup deletes on it, and an app's own rows lack it.
SCALE_TEST_ATTR="dfe_scale_test"
TEST_TARGET_VALUE=50
# How often a bounded wait re-reads the Deployments it is waiting on.
WAIT_INTERVAL="${DFE_WAIT_INTERVAL:-5}"
# How often held pressure writes its next row, against the shim's 60s average.
PRESSURE_INTERVAL="${DFE_KEDA_PRESSURE_INTERVAL:-10}"
# Seconds of zeros before scale-in is asserted, one full shim averaging window.
ZERO_HOLD="${DFE_KEDA_ZERO_HOLD:-60}"
# Above minReplicaCount 0 the HPA's own scale-down stabilization window governs,
# and its default is 300s -- cooldownPeriod only applies to scale-to-zero.
SCALE_IN_TIMEOUT="${DFE_SCALE_IN_TIMEOUT:-420}"
# The HPA re-reads its metrics once a controller-manager period (`--horizontal-pod-autoscaler-sync-period`).
HPA_SYNC="${DFE_HPA_SYNC_PERIOD:-15}"
# A new pod may wait on a node, then has to pull and report Ready before readyReplicas moves.
POD_READY_ALLOWANCE="${DFE_POD_READY_ALLOWANCE:-120}"
# KEDA names the HPA it owns after the ScaledObject.
HPA_PREFIX="keda-hpa-"
# Empty proves every app the shim's pressure drives.
ONLY_DEP="${DFE_KEDA_REAL_DEPLOYMENT:-}"
# KEDA's own default when a ScaledObject declares no pollingInterval, which only a floor of zero waits on.
KEDA_DEFAULT_POLL=30
# The stack's own rollouts contend for the scheduler and the metrics API, and a
# run started inside one missed its bound (#275).
SETTLE_TIMEOUT="${DFE_SETTLE_TIMEOUT:-300}"
SETTLE_INTERVAL="${DFE_SETTLE_INTERVAL:-10}"
# The verdict for a scale this run could neither prove nor disprove, which run-all-smoke-tests.sh fails on.
EXIT_UNPROVEN=3
HOLD_PID=""

# The apps one proof drives together: index N of every array is the same app.
SET_SO=()
SET_DEP=()
SET_SERVICE=()
SET_MIN=()
SET_POLL=()
# The pressure trigger's position in its ScaledObject, which KEDA names the HPA metric after (s<N>-...).
SET_INDEX=()
SET_TARGET=()
SET_WAITED=()
# UNPROVEN when phase 2 found a pressure-driven app it could not start from its floor.
DISCOVERY_VERDICT=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --namespace) NS="$2"; shift 2 ;;
        --ch-namespace) CH_NS="$2"; shift 2 ;;
        --ch-selector) CH_SELECTOR="$2"; shift 2 ;;
        --shim) SHIM="$2"; shift 2 ;;
        --real-deployment) ONLY_DEP="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

log() { echo "  [scale-test] $*"; }

# cluster_fact() -- one annotation off the dfe-cluster Secret, shared with the other bootstrap scripts.
# shellcheck source-path=SCRIPTDIR source=scripts/cluster-facts.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/cluster-facts.sh"

# engine_ch_query() -- an external server queried from the engine's own container, shared with the other smoke tests.
# shellcheck source-path=SCRIPTDIR source=scripts/engine-clickhouse.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/engine-clickhouse.sh"

# The deploy's own env first, else the cluster secret, as smoke-test-integration.sh reads it.
CH_MODE="${DFE_CLICKHOUSE_MODE:-$(cluster_fact clickhouse_mode)}"

ch_query() {
    # The shim reads the engine's server, so an external one (ClickHouse Cloud, no pod here) is written through the engine.
    if [[ "${CH_MODE}" == external ]]; then
        engine_ch_query "${NS}" "$1"
        return
    fi
    # In-cluster: clickhouse-client in a ClickHouse pod.
    local pod
    # One name from kubectl itself: piping a multi-pod listing into head closes
    # the pipe early, and SIGPIPE under pipefail aborts the run with no output.
    pod="$(kubectl -n "${CH_NS}" get pod -l "${CH_SELECTOR}" -o jsonpath='{.items[0].metadata.name}')"
    if [[ -z "${pod}" ]]; then
        echo "ERROR: no ClickHouse pod found in ns=${CH_NS} selector=${CH_SELECTOR}" >&2
        return 1
    fi
    kubectl -n "${CH_NS}" exec "${pod}" -- clickhouse-client --query "$1"
}

reset_set() {
    SET_SO=(); SET_DEP=(); SET_SERVICE=(); SET_MIN=(); SET_POLL=(); SET_INDEX=(); SET_TARGET=(); SET_WAITED=()
}

add_to_set() {
    # ScaledObject, Deployment, shim ServiceName, floor, pollingInterval, pressure trigger index, target.
    local n="${#SET_DEP[@]}"
    SET_SO[n]="$1"; SET_DEP[n]="$2"; SET_SERVICE[n]="$3"; SET_MIN[n]="$4"
    SET_POLL[n]="$5"; SET_INDEX[n]="$6"; SET_TARGET[n]="$7"; SET_WAITED[n]=""
}

set_services() {
    # The set's ServiceNames, comma separated, for a log line.
    local i=0 out=""
    while (( i < ${#SET_SERVICE[@]} )); do
        out="${out}${out:+, }'${SET_SERVICE[i]}'"
        i=$((i + 1))
    done
    echo "${out}"
}

inject_set() {
    # INSERT one dfe_scaling_pressure gauge row at value ${1} for every service in the set
    # (partial-column insert -- the other otel_metrics_gauge columns take their defaults).
    # Cleanup deletes on the run marker, so a real app's own rows survive.
    local value="$1" i=0 rows=""
    while (( i < ${#SET_SERVICE[@]} )); do
        rows="${rows}${rows:+, }('${SET_SERVICE[i]}', 'dfe_scaling_pressure', ${value}, now64(), map('${SCALE_TEST_ATTR}', '${TARGET}'))"
        i=$((i + 1))
    done
    ch_query "INSERT INTO ${OTEL_DB}.otel_metrics_gauge (ServiceName, MetricName, Value, TimeUnix, Attributes) VALUES ${rows}"
}

hold_set() {
    # Sustain every service in the set at value ${1} for ${2}s: a row every PRESSURE_INTERVAL
    # holds the shim's 60s average at the value. A write that fails is said, and the hold goes on.
    local value="$1" seconds="$2" elapsed=0
    while (( elapsed < seconds )); do
        if ! inject_set "${value}" >/dev/null; then
            log "WARN: a pressure row at ${value} for $(set_services) was not written"
        fi
        sleep "${PRESSURE_INTERVAL}"; elapsed=$((elapsed + PRESSURE_INTERVAL))
    done
}

stop_hold() {
    if [[ -n "${HOLD_PID}" ]]; then
        kill "${HOLD_PID}" >/dev/null 2>&1 || true
        wait "${HOLD_PID}" 2>/dev/null || true
    fi
    HOLD_PID=""
}

ready_replicas() {
    local have
    have="$(kubectl -n "${NS}" get deploy "$1" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
    echo "${have:-0}"
}

deploy_exists() {
    kubectl -n "${NS}" get deploy "$1" -o name >/dev/null 2>&1
}

wait_for_set() {
    # BOUNDED poll of every app in the set: ${1} `out` waits for readyReplicas above the
    # floor, `in` for readyReplicas back at it, for at most ${2}s, and never past that.
    # Each app's elapsed seconds land in SET_WAITED, empty for one that never got there.
    local mode="$1" timeout="$2" waited=0 i pending have reached
    i=0
    while (( i < ${#SET_DEP[@]} )); do SET_WAITED[i]=""; i=$((i + 1)); done
    while :; do
        pending=0
        i=0
        while (( i < ${#SET_DEP[@]} )); do
            if [[ -z "${SET_WAITED[i]}" ]]; then
                have="$(ready_replicas "${SET_DEP[i]}")"
                reached=no
                if [[ "${mode}" == out ]] && (( have > SET_MIN[i] )); then reached=yes; fi
                if [[ "${mode}" == in ]] && (( have <= SET_MIN[i] )); then reached=yes; fi
                if [[ "${reached}" == yes ]]; then SET_WAITED[i]="${waited}"; else pending=$((pending + 1)); fi
            fi
            i=$((i + 1))
        done
        if (( pending == 0 )); then return 0; fi
        if (( waited >= timeout )); then return 1; fi
        sleep "${WAIT_INTERVAL}"; waited=$((waited + WAIT_INTERVAL))
    done
}

scale_out_bound() {
    # Above a floor of zero the HPA alone reads the metric, once a sync period, so a reading
    # gets two periods to land plus the pod's own start. From zero, KEDA's poll activates it first.
    local min="$1" poll="${2:-${KEDA_DEFAULT_POLL}}" bound
    bound=$(( HPA_SYNC * 2 + POD_READY_ALLOWANCE ))
    if (( min == 0 )); then bound=$(( bound + poll * 2 )); fi
    echo "${bound}"
}

rolling_workloads() {
    # A Deployment is mid-roll while its controller has not observed the current
    # generation or the pods it wants are not all Ready. custom-columns rather
    # than jsonpath: a missing status field must keep its column, not shift one.
    kubectl -n "${NS}" get deploy --no-headers -o \
        custom-columns=N:.metadata.name,W:.spec.replicas,U:.status.updatedReplicas,R:.status.readyReplicas,G:.metadata.generation,O:.status.observedGeneration 2>/dev/null \
        | awk '{ for (i = 2; i <= 6; i++) if ($i == "<none>") $i = 0 } $2 != $3 || $2 != $4 || $5 != $6 { print $1 }'
}

wait_for_settled() {
    # Advisory, never a verdict: a stack that keeps rolling is reported and the
    # proof runs anyway, because the roll may be what an operator wants proven.
    local waited=0 rolling
    while (( waited < SETTLE_TIMEOUT )); do
        rolling="$(rolling_workloads || true)"
        if [[ -z "${rolling}" ]]; then
            log "ns=${NS} settled after ${waited}s; no deployment mid-roll"
            return 0
        fi
        sleep "${SETTLE_INTERVAL}"; waited=$((waited + SETTLE_INTERVAL))
    done
    log "WARN: still rolling after ${SETTLE_TIMEOUT}s: $(rolling_workloads | tr '\n' ' ')"
}

quantity_value() {
    # An HPA renders an external metric as a Quantity, so a fractional reading
    # arrives in milli-units.
    case "$1" in
        "") echo "" ;;
        *m) echo $(( ${1%m} / 1000 )) ;;
        *) echo "${1%%.*}" ;;
    esac
}

hpa_field() {
    kubectl -n "${NS}" get hpa "${HPA_PREFIX}$1" -o jsonpath="$2" 2>/dev/null || true
}

hpa_metrics() {
    # One line per reading and per target the HPA holds: current|<metric>|<value> and target|<metric>|<value>.
    hpa_field "$1" "{range .status.currentMetrics[*]}current|{.external.metric.name}{.resource.name}|{.external.current.value}{.external.current.averageValue}{.resource.current.averageUtilization}{'\n'}{end}{range .spec.metrics[*]}target|{.external.metric.name}{.resource.name}|{.external.target.value}{.external.target.averageValue}{.resource.target.averageUtilization}{'\n'}{end}"
}

worse() {
    # The worse of two verdicts: FAIL over UNPROVEN over PASS.
    if [[ "$1" != 0 && "$1" != "${EXIT_UNPROVEN}" ]] || [[ "$2" != 0 && "$2" != "${EXIT_UNPROVEN}" ]]; then
        echo 1
    elif [[ "$1" == "${EXIT_UNPROVEN}" || "$2" == "${EXIT_UNPROVEN}" ]]; then
        echo "${EXIT_UNPROVEN}"
    else
        echo 0
    fi
}

scale_out_verdict() {
    # Why the replicas did not move, decided from what the HPA could see rather
    # than from the clock alone.
    local so="$1" dep="$2" want="$3" service="$4" bound="$5" target="$6"
    local metric desired active value rows
    metric="$(hpa_field "${so}" '{.status.currentMetrics[0].external.current.value}')"
    if [[ -z "${metric}" ]]; then
        # metricType AverageValue renders the same reading in its own field.
        metric="$(hpa_field "${so}" '{.status.currentMetrics[0].external.current.averageValue}')"
    fi
    desired="$(hpa_field "${so}" '{.status.desiredReplicas}')"
    active="$(hpa_field "${so}" '{.status.conditions[?(@.type=="ScalingActive")].status}')"
    value="$(quantity_value "${metric}")"
    rows="$(ch_query "SELECT count() FROM ${OTEL_DB}.otel_metrics_gauge WHERE ServiceName = '${service}' AND MetricName = 'dfe_scaling_pressure' AND TimeUnix >= now() - INTERVAL 120 SECOND" 2>/dev/null || echo 0)"
    log "${HPA_PREFIX}${so}: metric ${metric:-none} against target ${target}, desiredReplicas ${desired:-none}, ScalingActive ${active:-none}, ${rows:-0} pressure row(s) in the last 120s"

    if [[ -n "${desired}" ]] && (( desired >= want )) 2>/dev/null; then
        echo "=== FAIL: the HPA asked for ${desired} and ${dep} had no Ready pod for it within ${bound}s ==="
        return 1
    fi
    if [[ "${active}" != "True" || -z "${value}" ]]; then
        echo "=== FAIL: the shim's pressure never reached the HPA (ScalingActive ${active:-none}, metric ${metric:-none}) ==="
        return 1
    fi
    if (( value < target )) 2>/dev/null; then
        echo "=== FAIL: the HPA read ${metric} against target ${target}, so the injected pressure never carried the shim's average ==="
        return 1
    fi
    echo "=== UNPROVEN: the HPA read ${metric} against target ${target} and left ${dep} at ${desired:-1} for ${bound}s ==="
    echo "    autoscaling is NOT proven by this run and is not disproven either."
    return "${EXIT_UNPROVEN}"
}

scale_out_reading() {
    # Log what set entry ${1}'s HPA read once it scaled out, and FAIL when the pressure reading is under target.
    local i="$1" so kind name value pressure="" target="" cpu="" cpu_target="" desired why
    so="${SET_SO[i]}"
    while IFS='|' read -r kind name value; do
        case "${kind}|${name}" in
            "current|s${SET_INDEX[i]}-"*) pressure="$(quantity_value "${value}")" ;;
            "target|s${SET_INDEX[i]}-"*) target="$(quantity_value "${value}")" ;;
            "current|cpu") cpu="${value}" ;;
            "target|cpu") cpu_target="${value}" ;;
        esac
    done <<< "$(hpa_metrics "${so}")"
    target="${target:-${SET_TARGET[i]}}"
    desired="$(hpa_field "${so}" '{.status.desiredReplicas}')"
    why="$(hpa_field "${so}" '{.status.conditions[?(@.type=="ScalingActive")].message}')"
    log "${HPA_PREFIX}${so} at scale-out: pressure ${pressure:-none} against target ${target}, cpu ${cpu:-none}% against ${cpu_target:-none}%, desiredReplicas ${desired:-none}. HPA: ${why%%(*}"
    if [[ -z "${pressure}" ]] || (( pressure < target )); then
        echo "=== FAIL: ${SET_DEP[i]} scaled out with its pressure reading ${pressure:-absent} under target ${target}, so the shim's pressure did not drive it ==="
        return 1
    fi
    if [[ -n "${cpu}" && -n "${cpu_target}" ]] && (( cpu >= cpu_target )); then
        log "NOTE: ${SET_DEP[i]}'s cpu was at or over its target too, so cpu alone could have asked for the same replicas"
    fi
    return 0
}

own_pressure_warn() {
    # Say when set entry ${1} wrote no pressure row of its own: outside this proof its trigger reads 0.
    local service="${SET_SERVICE[$1]}" own
    own="$(ch_query "SELECT count() FROM ${OTEL_DB}.otel_metrics_gauge WHERE ServiceName = '${service}' AND (MetricName = 'scaling_pressure' OR endsWith(MetricName, '_scaling_pressure')) AND NOT mapContains(Attributes, '${SCALE_TEST_ATTR}') AND TimeUnix >= now() - INTERVAL 120 SECOND" 2>/dev/null || true)"
    if [[ "${own:-0}" == 0 ]]; then
        log "WARN: ${service} wrote no pressure row of its own in the last 120s, so outside this proof its pressure trigger reads 0 and only its other triggers scale it"
    fi
}

prove_set() {
    # Drive every app in the set from its floor to one above it and back, together.
    # ${1} yes also checks each app writes pressure of its own. Returns 0, 1 or EXIT_UNPROVEN.
    local own_check="$1" i bound=0 one verdict=0 code
    i=0
    while (( i < ${#SET_DEP[@]} )); do
        one="$(scale_out_bound "${SET_MIN[i]}" "${SET_POLL[i]}")"
        if (( one > bound )); then bound="${one}"; fi
        i=$((i + 1))
    done
    log "holding dfe_scaling_pressure=100 for $(set_services), asserting SCALE-OUT (bounded ${bound}s = HPA sync ${HPA_SYNC}s x2 + ${POD_READY_ALLOWANCE}s pod start)..."
    # Held past the bound, so a verdict read after it still sees the pressure.
    hold_set 100 $(( bound + ZERO_HOLD )) &
    HOLD_PID=$!
    wait_for_set out "${bound}" || true
    i=0
    while (( i < ${#SET_DEP[@]} )); do
        if [[ -n "${SET_WAITED[i]}" ]]; then
            log "PASS: ${SET_DEP[i]} scaled out to $(( SET_MIN[i] + 1 )) in ${SET_WAITED[i]}s"
            code=0
            scale_out_reading "${i}" || code=$?
            verdict="$(worse "${verdict}" "${code}")"
            if [[ "${own_check}" == yes ]]; then own_pressure_warn "${i}"; fi
        else
            code=0
            scale_out_verdict "${SET_SO[i]}" "${SET_DEP[i]}" "$(( SET_MIN[i] + 1 ))" "${SET_SERVICE[i]}" "${bound}" "${SET_TARGET[i]}" || code=$?
            verdict="$(worse "${verdict}" "${code}")"
        fi
        i=$((i + 1))
    done
    stop_hold
    if (( verdict != 0 )); then return "${verdict}"; fi

    log "holding zeros for ${ZERO_HOLD}s to drag each average back under target"
    hold_set 0 "${ZERO_HOLD}"
    log "asserting SCALE-IN to each floor (bounded ${SCALE_IN_TIMEOUT}s)..."
    wait_for_set in "${SCALE_IN_TIMEOUT}" || true
    i=0
    while (( i < ${#SET_DEP[@]} )); do
        if [[ -n "${SET_WAITED[i]}" ]]; then
            log "PASS: ${SET_DEP[i]} back to ${SET_MIN[i]} in ${SET_WAITED[i]}s"
        else
            echo "=== FAIL: ${SET_DEP[i]} did not scale back in to ${SET_MIN[i]} within ${SCALE_IN_TIMEOUT}s ==="
            verdict=1
        fi
        i=$((i + 1))
    done
    return "${verdict}"
}

pressure_trigger() {
    # "<index>|<service>|<targetValue>" for ScaledObject ${1}'s shim pressure trigger, empty when it carries none.
    local index=0 type url target service
    while IFS='|' read -r type url target; do
        case "${type}|${url}" in
            "metrics-api|"*"/keda/pressure?"*"service="*)
                service="${url##*service=}"
                echo "${index}|${service%%&*}|${target}"
                return 0
                ;;
        esac
        index=$((index + 1))
    done <<< "$(kubectl -n "${NS}" get scaledobject "$1" -o "jsonpath={range .spec.triggers[*]}{.type}|{.metadata.url}|{.metadata.targetValue}{'\n'}{end}" 2>/dev/null || true)"
}

discover_pressure_set() {
    # Fill the set with every app whose ScaledObject carries the shim's pressure trigger,
    # and report each ScaledObject left out, and why.
    local so dep min max poll found index service target have
    reset_set
    while IFS='|' read -r so dep min max poll; do
        if [[ -z "${so}" || "${so}" == "${TARGET}-scaler" ]]; then continue; fi
        if [[ -n "${ONLY_DEP}" && "${dep}" != "${ONLY_DEP}" ]]; then continue; fi
        found="$(pressure_trigger "${so}")"
        if [[ -z "${found}" ]]; then
            log "SKIP ${dep}: ${so} carries no shim pressure trigger, so pressure does not drive it here"
            continue
        fi
        IFS='|' read -r index service target <<< "${found}"
        # KEDA's own defaults for a ScaledObject that declares neither.
        min="${min:-0}"
        max="${max:-100}"
        if (( max <= min )); then
            log "SKIP ${dep}: ${so} pins it at ${min} replicas (maxReplicaCount ${max}), so there is no scale to prove"
            continue
        fi
        if ! deploy_exists "${dep}"; then
            log "SKIP ${dep}: ${so} targets a Deployment ns=${NS} does not run on this profile"
            continue
        fi
        # The name goes into SQL, so anything but a plain name is refused rather than quoted.
        case "${service}" in
            "" | *[!A-Za-z0-9._-]*)
                echo "=== UNPROVEN: ${so} asks the shim for service '${service}', which is not a plain name ==="
                DISCOVERY_VERDICT="${EXIT_UNPROVEN}"
                continue
                ;;
        esac
        have="$(ready_replicas "${dep}")"
        if (( have != min )); then
            echo "=== UNPROVEN: ${dep} has ${have} Ready replica(s), not its floor of ${min}, so a step from the floor cannot be measured ==="
            DISCOVERY_VERDICT="${EXIT_UNPROVEN}"
            continue
        fi
        add_to_set "${so}" "${dep}" "${service}" "${min}" "${poll}" "${index}" "${target:-${TEST_TARGET_VALUE}}"
        log "${dep}: floor ${min}, ceiling ${max}, target ${target:-${TEST_TARGET_VALUE}}, shim ServiceName '${service}', HPA metric s${index}-*"
    done <<< "$(kubectl -n "${NS}" get scaledobject -o "jsonpath={range .items[*]}{.metadata.name}|{.spec.scaleTargetRef.name}|{.spec.minReplicaCount}|{.spec.maxReplicaCount}|{.spec.pollingInterval}{'\n'}{end}" 2>/dev/null || true)"
}

set_summary() {
    # "<deployment> <floor>-><floor+1>-><floor>" per app, for the PASS line.
    local i=0 out=""
    while (( i < ${#SET_DEP[@]} )); do
        out="${out}${out:+, }${SET_DEP[i]} ${SET_MIN[i]}->$(( SET_MIN[i] + 1 ))->${SET_MIN[i]}"
        i=$((i + 1))
    done
    echo "${out}"
}

cleanup() {
    log "cleanup: removing test target + injected metric"
    stop_hold
    kubectl -n "${NS}" delete scaledobject "${TARGET}-scaler" --ignore-not-found >/dev/null 2>&1 || true
    kubectl -n "${NS}" delete deploy "${TARGET}" --ignore-not-found >/dev/null 2>&1 || true
    # Age-out handles the metric, but tombstone both phases' rows so re-runs start clean.
    ch_query "ALTER TABLE ${OTEL_DB}.otel_metrics_gauge DELETE WHERE Attributes['${SCALE_TEST_ATTR}'] = '${TARGET}'" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "=== DFE KEDA artificial +1-pod scale test ==="
wait_for_settled
echo "--- Phase 1: throwaway target ---"

log "creating throwaway target + shim-driven ScaledObject (min 1, max 2)"
kubectl -n "${NS}" apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${TARGET}
  labels: { app.kubernetes.io/name: ${TARGET} }
spec:
  replicas: 1
  selector: { matchLabels: { app.kubernetes.io/name: ${TARGET} } }
  template:
    metadata: { labels: { app.kubernetes.io/name: ${TARGET} } }
    spec:
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.9@sha256:7031c1b283388d2c2e09b57badb803c05ebed362dc88d84b480cc47f72a21097
          resources: { requests: { cpu: 10m, memory: 16Mi }, limits: { cpu: 50m, memory: 32Mi } }
---
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: ${TARGET}-scaler
spec:
  scaleTargetRef: { name: ${TARGET} }
  minReplicaCount: 1
  maxReplicaCount: 2
  triggers:
    - type: metrics-api
      metricType: Value
      metadata:
        targetValue: "${TEST_TARGET_VALUE}"
        url: "http://${SHIM}/keda/pressure?service=${TARGET}"
        valueLocation: "value"
EOF

reset_set
add_to_set "${TARGET}-scaler" "${TARGET}" "${TARGET}" 1 "" 0 "${TEST_TARGET_VALUE}"
PHASE_RC=0
prove_set no || PHASE_RC=$?
if (( PHASE_RC != 0 )); then exit "${PHASE_RC}"; fi
echo "=== PASS: KEDA scaled 1->2->1 via the dfe-keda-shim on an injected metric ==="

echo "--- Phase 2: every app the shim's pressure drives ---"
discover_pressure_set
if (( ${#SET_DEP[@]} == 0 )); then
    if (( DISCOVERY_VERDICT != 0 )); then
        echo "=== UNPROVEN: no pressure-driven app in ns=${NS} could start from its floor, so none was proven ==="
        exit "${DISCOVERY_VERDICT}"
    fi
    log "SKIP: no app ns=${NS} runs is scaled by the shim's pressure${ONLY_DEP:+ (asked for ${ONLY_DEP} alone)}"
    exit 0
fi

PHASE_RC=0
prove_set yes || PHASE_RC=$?
PHASE_RC="$(worse "${PHASE_RC}" "${DISCOVERY_VERDICT}")"
if (( PHASE_RC == 0 )); then
    echo "=== PASS: KEDA scaled the real $(set_summary) on injected pressure ==="
elif (( PHASE_RC == EXIT_UNPROVEN )); then
    echo "=== UNPROVEN: KEDA scaled the real $(set_summary), and the app(s) named UNPROVEN above were not proven ==="
fi
exit "${PHASE_RC}"
