#!/usr/bin/env bash
# Project:   dfe-infra
# File:      bootstrap/keda-scale-test.sh
# Purpose:   ARTIFICIAL +1-pod KEDA scale proof (the real "Phase C" test)
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# Proves KEDA actually scales a Deployment via the fail-safe dfe-keda-shim, on demand,
# WITHOUT waiting for real load, in two phases.
#
# Phase 1 stands up a throwaway target whose ONLY trigger is the shim's pressure query
# for a test ServiceName, INJECTS a high dfe_scaling_pressure row into ClickHouse,
# asserts the target scales OUT (>= 2), then drags the average back down and asserts it
# scales IN (== 1) -- isolated from every real dfe-* app.
#
# Phase 2 runs the same injection against a REAL app (dfe-receiver by default): sustained
# pressure takes the shipped Deployment above its own floor, zeros bring it back. It SKIPS
# where that app has no ScaledObject or no shim trigger, so a slim tier still passes.
#
# Both phases are self-cleaning. Every wait is BOUNDED and never waits forever, and the
# scale-out bound is DERIVED from the ScaledObject's own polling interval plus the HPA
# sync period rather than fixed, so a busy cluster does not fail the proof on the clock.
#
# Three verdicts, not two: exit 0 proved it, exit 1 is a scaler that did not work, and
# exit 3 is UNPROVEN -- the HPA read the injected pressure above target and left the
# replicas alone, which is dfe-infra #134 and is neither a working seam nor a broken one.
#
# Usage:  bootstrap/keda-scale-test.sh [--namespace dfe] [--ch-namespace clickhouse]
#                                      [--ch-selector app.kubernetes.io/name=clickhouse]
#                                      [--shim dfe-keda-shim.dfe.svc.cluster.local:8080]
#                                      [--real-deployment dfe-receiver]
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
# Phase 1 authors its own ScaledObject, so its poll is known rather than read.
TEST_POLL_INTERVAL="${DFE_KEDA_TEST_POLL:-10}"
TEST_TARGET_VALUE=50
# How often a bounded wait re-reads the Deployment it is waiting on.
WAIT_INTERVAL="${DFE_WAIT_INTERVAL:-5}"
# Above minReplicaCount 0 the HPA's own scale-down stabilization window governs,
# and its default is 300s -- cooldownPeriod only applies to scale-to-zero.
SCALE_IN_TIMEOUT="${DFE_SCALE_IN_TIMEOUT:-420}"
# The two loops that have to notice the injected pressure: KEDA polls the scaler
# on the ScaledObject's own interval, and the HPA resyncs on the
# controller-manager's period (`--horizontal-pod-autoscaler-sync-period`).
HPA_SYNC="${DFE_HPA_SYNC_PERIOD:-15}"
# A scheduled pod still has to pull and report Ready before readyReplicas moves.
POD_READY_ALLOWANCE="${DFE_POD_READY_ALLOWANCE:-90}"
# KEDA names the HPA it owns after the ScaledObject.
HPA_PREFIX="keda-hpa-"
# Phase 2: the real app whose shipped ScaledObject carries the shim trigger.
REAL_DEP="${DFE_KEDA_REAL_DEPLOYMENT:-dfe-receiver}"
# KEDA's own default when a ScaledObject declares no pollingInterval.
KEDA_DEFAULT_POLL=30
REAL_ZERO_HOLD=60       # seconds of zeros, one full shim averaging window
# The stack's own rollouts contend for the scheduler and the metrics API, and a
# run started inside one missed its bound (#275).
SETTLE_TIMEOUT="${DFE_SETTLE_TIMEOUT:-300}"
SETTLE_INTERVAL="${DFE_SETTLE_INTERVAL:-10}"
# This proof cannot tell an unreproduced KEDA stall from a broken scaler, so that
# verdict has its own exit code and run-all-smoke-tests.sh prints it as its own word.
EXIT_UNPROVEN=3
HOLD_PID=""

while [ $# -gt 0 ]; do
    case "$1" in
        --namespace) NS="$2"; shift 2 ;;
        --ch-namespace) CH_NS="$2"; shift 2 ;;
        --ch-selector) CH_SELECTOR="$2"; shift 2 ;;
        --shim) SHIM="$2"; shift 2 ;;
        --real-deployment) REAL_DEP="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

log() { echo "  [scale-test] $*"; }

ch_query() {
    # Run a ClickHouse query via the CH pod (HTTP :8123 plaintext, in-cluster).
    local pod
    # One name from kubectl itself: piping a multi-pod listing into head closes
    # the pipe early, and SIGPIPE under pipefail aborts the run with no output.
    pod="$(kubectl -n "${CH_NS}" get pod -l "${CH_SELECTOR}" -o jsonpath='{.items[0].metadata.name}')"
    if [ -z "${pod}" ]; then
        echo "ERROR: no ClickHouse pod found in ns=${CH_NS} selector=${CH_SELECTOR}" >&2
        return 1
    fi
    kubectl -n "${CH_NS}" exec "${pod}" -- clickhouse-client --query "$1"
}

inject_pressure_for() {
    # INSERT one dfe_scaling_pressure gauge row for service ${1} at value ${2}
    # (partial-column insert -- the other otel_metrics_gauge columns take their
    # defaults). Cleanup deletes on the run marker, so a real app's own rows survive.
    ch_query "INSERT INTO ${OTEL_DB}.otel_metrics_gauge (ServiceName, MetricName, Value, TimeUnix, Attributes) VALUES ('$1', 'dfe_scaling_pressure', $2, now64(), map('dfe_scale_test', '${TARGET}'))"
}

inject_pressure() { inject_pressure_for "${TARGET}" "$1"; }

hold_pressure() {
    # Sustain service ${1} at value ${2} for ${3}s: the shim averages the last 60s,
    # so a row every 10s holds that average at the injected value.
    local service="$1" value="$2" seconds="$3" elapsed=0
    while [ "${elapsed}" -lt "${seconds}" ]; do
        inject_pressure_for "${service}" "${value}"
        sleep 10; elapsed=$((elapsed + 10))
    done
}

WAITED_SECONDS=0
wait_for_replicas() {
    # BOUNDED poll: succeed when the Deployment's readyReplicas satisfies ${4} (ge|le)
    # ${2} within ${3}s, else FAIL (return 1). The backstop -- never blocks past timeout.
    # Leaves the elapsed seconds in WAITED_SECONDS for the caller to report.
    local dep="$1" want="$2" timeout="$3" mode="$4" waited=0 have
    while [ "${waited}" -lt "${timeout}" ]; do
        have="$(kubectl -n "${NS}" get deploy "${dep}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
        have="${have:-0}"
        WAITED_SECONDS="${waited}"
        if [ "${mode}" = "ge" ] && [ "${have}" -ge "${want}" ]; then return 0; fi
        if [ "${mode}" = "le" ] && [ "${have}" -le "${want}" ]; then return 0; fi
        sleep "${WAIT_INTERVAL}"; waited=$((waited + WAIT_INTERVAL))
    done
    WAITED_SECONDS="${waited}"
    return 1
}

scale_out_bound() {
    # Two unsynchronised loops have to notice before a pod is even created, so
    # each is allowed one full period twice over, plus the pod's own start.
    local poll="${1:-${KEDA_DEFAULT_POLL}}"
    echo $(( (poll + HPA_SYNC) * 2 + POD_READY_ALLOWANCE ))
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
    while [ "${waited}" -lt "${SETTLE_TIMEOUT}" ]; do
        rolling="$(rolling_workloads || true)"
        if [ -z "${rolling}" ]; then
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

scale_out_verdict() {
    # Why the replicas did not move, decided from what the HPA could see rather
    # than from the clock alone.
    local so="$1" dep="$2" want="$3" service="$4" bound="$5" target="$6"
    local metric desired active value rows
    metric="$(hpa_field "${so}" '{.status.currentMetrics[0].external.current.value}')"
    if [ -z "${metric}" ]; then
        # metricType AverageValue renders the same reading in its own field.
        metric="$(hpa_field "${so}" '{.status.currentMetrics[0].external.current.averageValue}')"
    fi
    desired="$(hpa_field "${so}" '{.status.desiredReplicas}')"
    active="$(hpa_field "${so}" '{.status.conditions[?(@.type=="ScalingActive")].status}')"
    value="$(quantity_value "${metric}")"
    rows="$(ch_query "SELECT count() FROM ${OTEL_DB}.otel_metrics_gauge WHERE ServiceName = '${service}' AND MetricName = 'dfe_scaling_pressure' AND TimeUnix >= now() - INTERVAL 120 SECOND" 2>/dev/null || echo 0)"
    log "${HPA_PREFIX}${so}: metric ${metric:-none} against target ${target}, desiredReplicas ${desired:-none}, ScalingActive ${active:-none}, ${rows:-0} pressure row(s) in the last 120s"

    if [ -n "${desired}" ] && [ "${desired}" -ge "${want}" ] 2>/dev/null; then
        echo "=== FAIL: the HPA asked for ${desired} and ${dep} had no Ready pod for it within ${bound}s ==="
        return 1
    fi
    if [ "${active}" != "True" ] || [ -z "${value}" ]; then
        echo "=== FAIL: the shim's pressure never reached the HPA (ScalingActive ${active:-none}, metric ${metric:-none}) ==="
        return 1
    fi
    if [ "${value}" -lt "${target}" ] 2>/dev/null; then
        echo "=== FAIL: the HPA read ${metric} against target ${target}, so the injected pressure never carried the shim's average ==="
        return 1
    fi
    # dfe-infra #134: the same seam scaled on demand once and has not reproduced,
    # with the injection readable, the shim serving it and the scaler built.
    echo "=== UNPROVEN: the HPA read ${metric} against target ${target} and left ${dep} at ${desired:-1} for ${bound}s ==="
    echo "    autoscaling is NOT proven by this run and is not disproven either (#134)."
    return "${EXIT_UNPROVEN}"
}

assert_scale_out() {
    # One bounded assertion for both phases, with the same verdict either side.
    local so="$1" dep="$2" want="$3" poll="$4" service="$5" target="$6" bound
    bound="$(scale_out_bound "${poll}")"
    log "asserting SCALE-OUT to ${want} (bounded ${bound}s = (poll ${poll}s + HPA sync ${HPA_SYNC}s) x2 + ${POD_READY_ALLOWANCE}s pod start)..."
    if wait_for_replicas "${dep}" "${want}" "${bound}" ge; then
        log "PASS: ${dep} scaled out to ${want} in ${WAITED_SECONDS}s"
        return 0
    fi
    scale_out_verdict "${so}" "${dep}" "${want}" "${service}" "${bound}" "${target}"
}

cleanup() {
    log "cleanup: removing test target + injected metric"
    if [ -n "${HOLD_PID}" ]; then kill "${HOLD_PID}" >/dev/null 2>&1 || true; fi
    kubectl -n "${NS}" delete scaledobject "${TARGET}-scaler" --ignore-not-found >/dev/null 2>&1 || true
    kubectl -n "${NS}" delete deploy "${TARGET}" --ignore-not-found >/dev/null 2>&1 || true
    # Age-out handles the metric, but tombstone both phases' rows so re-runs start clean.
    ch_query "ALTER TABLE ${OTEL_DB}.otel_metrics_gauge DELETE WHERE Attributes['dfe_scale_test'] = '${TARGET}'" >/dev/null 2>&1 || true
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
  cooldownPeriod: 30
  pollingInterval: ${TEST_POLL_INTERVAL}
  triggers:
    - type: metrics-api
      metricType: Value
      metadata:
        targetValue: "${TEST_TARGET_VALUE}"
        url: "http://${SHIM}/keda/pressure?service=${TARGET}"
        valueLocation: "value"
EOF

log "injecting dfe_scaling_pressure=100 for service '${TARGET}' (> target ${TEST_TARGET_VALUE})"
inject_pressure 100

assert_scale_out "${TARGET}-scaler" "${TARGET}" 2 "${TEST_POLL_INTERVAL}" "${TARGET}" "${TEST_TARGET_VALUE}" || exit $?

log "dragging the 60s average back under target (batch of zeros) to force SCALE-IN"
for _ in $(seq 1 20); do inject_pressure 0; done

log "asserting SCALE-IN to 1 (bounded ${SCALE_IN_TIMEOUT}s)..."
if wait_for_replicas "${TARGET}" 1 "${SCALE_IN_TIMEOUT}" le; then
    log "PASS: scaled back in to 1 in ${WAITED_SECONDS}s"
else
    echo "=== FAIL: target did not scale in within ${SCALE_IN_TIMEOUT}s ==="; exit 1
fi

echo "=== PASS: KEDA scaled 1->2->1 via the dfe-keda-shim on an injected metric ==="

echo "--- Phase 2: the real ${REAL_DEP} ---"

REAL_SO="${REAL_DEP}-scaler"
if ! kubectl -n "${NS}" get scaledobject "${REAL_SO}" >/dev/null 2>&1; then
    log "SKIP: no ScaledObject ${REAL_SO} in ns=${NS}"
    exit 0
fi
REAL_URL="$(kubectl -n "${NS}" get scaledobject "${REAL_SO}" -o jsonpath='{.spec.triggers[?(@.type=="metrics-api")].metadata.url}')"
if [ -z "${REAL_URL}" ]; then
    log "SKIP: ${REAL_SO} has no metrics-api trigger, so pressure does not drive it here"
    exit 0
fi
# The ServiceName to inject is the one the shim is asked for, not the deployment
# name. `##*service=` returns the URL unchanged when there is no match, so a
# trigger URL with no service= param would otherwise inject pressure for the
# whole URL string instead of skipping.
case "${REAL_URL}" in
    *service=*) ;;
    *)
        log "SKIP: ${REAL_SO}'s metrics-api trigger URL has no service= param"
        exit 0
        ;;
esac
REAL_SERVICE="${REAL_URL##*service=}"
REAL_SERVICE="${REAL_SERVICE%%&*}"
REAL_MIN="$(kubectl -n "${NS}" get scaledobject "${REAL_SO}" -o jsonpath='{.spec.minReplicaCount}')"
REAL_MIN="${REAL_MIN:-1}"
REAL_OUT=$((REAL_MIN + 1))
# The app's own dials, so the bound and the verdict are read from what is
# deployed rather than from a second copy of it here.
REAL_POLL="$(kubectl -n "${NS}" get scaledobject "${REAL_SO}" -o jsonpath='{.spec.pollingInterval}')"
REAL_POLL="${REAL_POLL:-${KEDA_DEFAULT_POLL}}"
REAL_TARGET="$(kubectl -n "${NS}" get scaledobject "${REAL_SO}" -o jsonpath='{.spec.triggers[?(@.type=="metrics-api")].metadata.targetValue}')"
REAL_TARGET="${REAL_TARGET:-${TEST_TARGET_VALUE}}"
REAL_OUT_TIMEOUT="$(scale_out_bound "${REAL_POLL}")"
log "${REAL_DEP}: floor ${REAL_MIN} replicas, poll ${REAL_POLL}s, target ${REAL_TARGET}, shim ServiceName '${REAL_SERVICE}'"

# Held for the full scale-out window: the shim averages the last 60s, so
# pressure that stops early lets the average decay before a slow HPA sync
# finishes and the assertion runs.
log "holding dfe_scaling_pressure=100 for '${REAL_SERVICE}' (${REAL_OUT_TIMEOUT}s)"
hold_pressure "${REAL_SERVICE}" 100 "${REAL_OUT_TIMEOUT}" &
HOLD_PID=$!

assert_scale_out "${REAL_SO}" "${REAL_DEP}" "${REAL_OUT}" "${REAL_POLL}" "${REAL_SERVICE}" "${REAL_TARGET}" || exit $?

kill "${HOLD_PID}" >/dev/null 2>&1 || true
HOLD_PID=""
log "flooding zeros for ${REAL_ZERO_HOLD}s to drag the average back under target"
hold_pressure "${REAL_SERVICE}" 0 "${REAL_ZERO_HOLD}"

log "asserting SCALE-IN to ${REAL_MIN} (bounded ${SCALE_IN_TIMEOUT}s)..."
if wait_for_replicas "${REAL_DEP}" "${REAL_MIN}" "${SCALE_IN_TIMEOUT}" le; then
    log "PASS: ${REAL_DEP} back to ${REAL_MIN} in ${WAITED_SECONDS}s"
else
    echo "=== FAIL: ${REAL_DEP} did not scale in within ${SCALE_IN_TIMEOUT}s ==="; exit 1
fi

echo "=== PASS: KEDA scaled the real ${REAL_DEP} ${REAL_MIN}->${REAL_OUT}->${REAL_MIN} on injected pressure ==="
