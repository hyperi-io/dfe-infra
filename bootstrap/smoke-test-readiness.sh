#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         smoke-test-readiness.sh
#  Purpose:      DEPLOY READINESS GATE -- the authoritative end-of-deploy check,
#                run BY DEFAULT at the end of bootstrap.sh. It WAITS for the
#                cluster to converge (Argo syncs Layer 2 asynchronously) then
#                fails LOUDLY if anything is not genuinely healthy: not-Ready
#                containers, CrashLoopBackOff/ImagePull/Error, unmet replicas,
#                runaway restarts. "Argo Healthy" + "pod Running" are NOT trusted
#                (a Running pod can be 0/1; an Argo app can be Healthy while a
#                generated workload crashloops). This gate is what lets us TRUST
#                that a deploy is actually up before declaring success.
#  Language:     Bash
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage: ./smoke-test-readiness.sh [kubeconfig]
#  Env:   READINESS_TIMEOUT   (default 900s) -- backstop; convergence wait ceiling.
#         READINESS_INTERVAL  (default 15s)  -- poll cadence.
#         READINESS_RESTART_THRESHOLD (default 10) -- restarts above this fail.
set -uo pipefail
[ -n "${1:-}" ] && export KUBECONFIG="$1"
TIMEOUT="${READINESS_TIMEOUT:-900}"
INTERVAL="${READINESS_INTERVAL:-15}"
THRESH="${READINESS_RESTART_THRESHOLD:-10}"
ISSUES_FILE="$(mktemp)"
trap 'rm -f "$ISSUES_FILE"' EXIT

# One pass over every pod + workload. Appends human-readable issues to
# $ISSUES_FILE and echoes the issue count. Quiet otherwise (we poll this).
run_check() {
  : > "$ISSUES_FILE"

  # Pods: real container readiness, phase, restart sanity.
  while read -r ns name ready status restarts _; do
    case "$status" in Completed|Succeeded) continue ;; esac
    local have="${ready%%/*}" want="${ready##*/}"
    if [ "$status" != "Running" ]; then
      echo "pod $ns/$name status=$status (ready=$ready restarts=$restarts)" >> "$ISSUES_FILE"
    elif [ "$have" != "$want" ]; then
      echo "pod $ns/$name NOT READY ($ready)" >> "$ISSUES_FILE"
    elif [ "${restarts:-0}" -gt "$THRESH" ] 2>/dev/null; then
      echo "pod $ns/$name runaway restarts ($restarts > $THRESH)" >> "$ISSUES_FILE"
    fi
  done < <(kubectl get pods -A --no-headers 2>/dev/null)

  # Deployments + StatefulSets: readyReplicas == spec.replicas.
  local kind
  for kind in deployment statefulset; do
    while read -r ns name want have; do
      [ -z "$ns" ] && continue
      [ "$want" = "<none>" ] && want=0
      [ "${have:-0}" != "${want:-0}" ] && echo "$kind $ns/$name ${have:-0}/${want:-0} ready" >> "$ISSUES_FILE"
    done < <(kubectl get "$kind" -A --no-headers -o custom-columns=NS:.metadata.namespace,N:.metadata.name,W:.spec.replicas,H:.status.readyReplicas 2>/dev/null)
  done
  # DaemonSets: numberReady == desiredNumberScheduled.
  while read -r ns name want have; do
    [ -z "$ns" ] && continue
    [ "${have:-0}" != "${want:-0}" ] && echo "daemonset $ns/$name ${have:-0}/${want:-0} ready" >> "$ISSUES_FILE"
  done < <(kubectl get daemonset -A --no-headers -o custom-columns=NS:.metadata.namespace,N:.metadata.name,W:.status.desiredNumberScheduled,H:.status.numberReady 2>/dev/null)

  grep -c . "$ISSUES_FILE"
}

echo "=== DFE deploy readiness gate (waiting up to ${TIMEOUT}s for convergence) ==="
SECONDS=0
while :; do
  n="$(run_check)"
  if [ "$n" -eq 0 ]; then
    echo "=== READINESS GATE PASSED: every pod Ready, every workload at desired ==="
    exit 0
  fi
  if [ "$SECONDS" -ge "$TIMEOUT" ]; then
    echo "=== READINESS GATE FAILED after ${TIMEOUT}s -- ${n} issue(s): ==="
    sed 's/^/  [FAIL] /' "$ISSUES_FILE"
    exit 1
  fi
  echo "  ...${n} not-ready, ${SECONDS}s elapsed; re-checking in ${INTERVAL}s"
  sleep "$INTERVAL"
done
