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
# Both phases are self-cleaning. Every wait is BOUNDED and fails the test on timeout --
# never waits forever.
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
SCALE_OUT_TIMEOUT=120   # seconds to reach 2 replicas (pollingInterval + KEDA reaction)
# Above minReplicaCount 0 the HPA's own scale-down stabilization window governs,
# and its default is 300s -- cooldownPeriod only applies to scale-to-zero.
SCALE_IN_TIMEOUT="${DFE_SCALE_IN_TIMEOUT:-420}"
# Phase 2: the real app whose shipped ScaledObject carries the shim trigger.
REAL_DEP="${DFE_KEDA_REAL_DEPLOYMENT:-dfe-receiver}"
REAL_OUT_TIMEOUT=150    # the app's 15s pollingInterval plus the HPA's own sync
REAL_HOLD=90            # seconds of sustained pressure, a row every 10s
REAL_ZERO_HOLD=60       # seconds of zeros, one full shim averaging window
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
        sleep 5; waited=$((waited + 5))
    done
    WAITED_SECONDS="${waited}"
    return 1
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
          image: registry.k8s.io/pause:3.9
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
  pollingInterval: 10
  triggers:
    - type: metrics-api
      metricType: Value
      metadata:
        targetValue: "50"
        url: "http://${SHIM}/keda/pressure?service=${TARGET}"
        valueLocation: "value"
EOF

log "injecting dfe_scaling_pressure=100 for service '${TARGET}' (> target 50)"
inject_pressure 100

log "asserting SCALE-OUT to 2 (bounded ${SCALE_OUT_TIMEOUT}s)..."
if wait_for_replicas "${TARGET}" 2 "${SCALE_OUT_TIMEOUT}" ge; then
    log "PASS: scaled out to 2 in ${WAITED_SECONDS}s"
else
    echo "=== FAIL: target did not scale out within ${SCALE_OUT_TIMEOUT}s ==="; exit 1
fi

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
# The ServiceName to inject is the one the shim is asked for, not the deployment name.
REAL_SERVICE="${REAL_URL##*service=}"
REAL_SERVICE="${REAL_SERVICE%%&*}"
REAL_MIN="$(kubectl -n "${NS}" get scaledobject "${REAL_SO}" -o jsonpath='{.spec.minReplicaCount}')"
REAL_MIN="${REAL_MIN:-1}"
REAL_OUT=$((REAL_MIN + 1))
log "${REAL_DEP}: floor ${REAL_MIN} replicas, shim ServiceName '${REAL_SERVICE}'"

log "holding dfe_scaling_pressure=100 for '${REAL_SERVICE}' (${REAL_HOLD}s)"
hold_pressure "${REAL_SERVICE}" 100 "${REAL_HOLD}" &
HOLD_PID=$!

log "asserting SCALE-OUT to ${REAL_OUT} (bounded ${REAL_OUT_TIMEOUT}s)..."
if wait_for_replicas "${REAL_DEP}" "${REAL_OUT}" "${REAL_OUT_TIMEOUT}" ge; then
    log "PASS: ${REAL_DEP} scaled out to ${REAL_OUT} in ${WAITED_SECONDS}s"
else
    echo "=== FAIL: ${REAL_DEP} stayed at ${REAL_MIN} for ${REAL_OUT_TIMEOUT}s ==="; exit 1
fi

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
