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
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage: ./smoke-test-readiness.sh [kubeconfig]
#  Env:   DFE_NS              -- the app namespace; empty skips the presence check
#                                and the default-credentials check.
#         DFE_ENV             -- the deployment's posture; a dev posture (dev,
#                                development, local, test, ci) is the only one
#                                allowed to run the shipped admin password.
#         READINESS_ENGINE_TARGET (default deploy/dfe-engine) -- what the
#                                default-credentials check asks setup-status; a
#                                probe that cannot run fails outside a dev posture.
#         READINESS_TIMEOUT   (default 900s) -- backstop; convergence wait ceiling.
#         READINESS_INTERVAL  (default 15s)  -- poll cadence.
#         READINESS_RESTART_THRESHOLD (default 10) -- restarts above this fail.
#         READINESS_CHURN_MINUTES (default 15) -- a restart older than this is
#                                history, not live churn, whatever the count.
#         READINESS_WATCH_NS  -- space-separated namespace globs the gate judges
#                                (default: the namespaces destroy.sh removes,
#                                which is what this deploy creates).
set -uo pipefail
[ -n "${1:-}" ] && export KUBECONFIG="$1"
DFE_NS="${DFE_NS:-}"
TIMEOUT="${READINESS_TIMEOUT:-900}"
INTERVAL="${READINESS_INTERVAL:-15}"
THRESH="${READINESS_RESTART_THRESHOLD:-10}"
CHURN_MINUTES="${READINESS_CHURN_MINUTES:-15}"
DFE_ENV="${DFE_ENV:-}"
ENGINE_TARGET="${READINESS_ENGINE_TARGET:-deploy/dfe-engine}"
# The postures the engine's is_dev_posture accepts (dfe-engine #300).
DEV_POSTURES="dev development local test ci"
# Allowlist of the namespaces this deploy creates (destroy.sh's teardown list) --
# a denylist of the cluster's own is per-distribution and misses calico-system,
# tigera-operator, longhorn-system and metallb-system.
WATCH_NS="${READINESS_WATCH_NS:-argocd cert-manager external-secrets envoy-gateway-system keda strimzi kafka clickhouse clickhouse-operator-system clickhouse-operator cnpg cnpg-system ferretdb otel hyperdx reloader external-dns redpanda-operator forgejo gitea links dfe-*}"
[ -n "$DFE_NS" ] && WATCH_NS="$WATCH_NS $DFE_NS"

# True when a namespace matches one of the WATCH_NS globs.
watched_ns() {
  local ns="$1" pat
  for pat in $WATCH_NS; do
    # Unquoted on purpose: the pattern is a glob.
    # shellcheck disable=SC2254
    case "$ns" in $pat) return 0 ;; esac
  done
  return 1
}

# True when a restart is recent enough to be live churn: seconds always, minutes
# only inside CHURN_MINUTES, and an `h`/`d` recency -- `5h12m` also ends in `m`
# -- never.
recent_restart() {
  local mins
  case "$1" in
    *h*|*d*) return 1 ;;
    \(*[0-9]s) return 0 ;;
    \(*[0-9]m)
      mins="${1#\(}"
      # A non-numeric recency leaves `[` failing, which reads as not churn.
      [ "${mins%m}" -lt "$CHURN_MINUTES" ] 2>/dev/null
      ;;
    *) return 1 ;;
  esac
}

# The engine's own answer to "is this deployment running the shipped admin
# password" (#233, dfe-engine #300). Three verdicts, kept apart: a probe that
# could not run (exec exit non-zero) fails outside a dev posture, a missing
# field warns, and default credentials outside a dev posture fail.
check_default_credentials() {
  local posture answer rc dev=0
  if [ -z "$DFE_NS" ]; then
    echo "  [skip] default-credentials check: no DFE_NS"
    return 0
  fi
  for posture in $DEV_POSTURES; do
    [ "$DFE_ENV" = "$posture" ] && dev=1
  done
  answer="$(kubectl -n "$DFE_NS" exec "$ENGINE_TARGET" -- python3 -c \
    'import json,urllib.request
with urllib.request.urlopen("http://localhost:8000/api/v1/auth/setup-status", timeout=10) as r:
    print(json.load(r).get("default_credentials", "unknown"))' 2>/dev/null)"
  rc=$?
  # Strip surrounding whitespace before comparing, the way the engine does.
  answer="$(printf '%s' "$answer" | tail -1 | tr -d '[:space:]')"
  if [ "$rc" -ne 0 ]; then
    if [ "$dev" -eq 1 ]; then
      echo "  [warn] default-credentials check: could not ask $ENGINE_TARGET"
      echo "         (kubectl exec exit $rc); DFE_ENV=$DFE_ENV is a dev posture"
      return 0
    fi
    echo "=== READINESS GATE FAILED: the default-credentials check could not RUN"
    echo "  [FAIL] kubectl exec $ENGINE_TARGET in $DFE_NS exited $rc -- an RBAC denial,"
    echo "  [FAIL] a wrong READINESS_ENGINE_TARGET, or setup-status not answering 200."
    echo "  [FAIL] DFE_ENV='${DFE_ENV:-unset}' is not a dev posture, so the shipped"
    echo "  [FAIL] admin password cannot be left unchecked."
    return 1
  fi
  case "$answer" in
    True|true)
      if [ "$dev" -eq 1 ]; then
        echo "  [warn] the engine reports default_credentials, allowed here:"
        echo "         DFE_ENV=$DFE_ENV is a dev posture"
        return 0
      fi
      echo "=== READINESS GATE FAILED: the engine reports default_credentials on"
      echo "  [FAIL] setup-status with DFE_ENV='${DFE_ENV:-unset}' -- this deploy is"
      echo "  [FAIL] serving the shipped admin password. Mint one:"
      echo "  [FAIL]   helm value dfe-engine.auth.adminSecretName (ESO Password generator)"
      return 1
      ;;
    False|false)
      echo "  [pass] the engine reports a minted admin password"
      return 0
      ;;
    *)
      echo "  [warn] default-credentials check: the engine did not answer"
      echo "         setup-status with default_credentials (pre-#300 image?)"
      return 0
      ;;
  esac
}

# The edge issuer mode and, in self-signed mode, whether the root was restored or
# newly minted (#238). Informational: a fresh root is a working deploy, so it
# never fails the gate. `dfe-ops ca --status` owns the wording.
report_issuer_mode() {
  local repo_root
  repo_root="$(cd "$(dirname "$0")/.." && pwd)"
  local report
  report="$(python3 "${repo_root}/scripts/dfe-ops" ca --status 2>/dev/null)"
  if [ -z "$report" ]; then
    echo "  [warn] issuer mode: dfe-ops ca --status could not read the cluster"
    return 0
  fi
  printf '%s\n' "$report" | sed 's/^/  [info] /'
}

ISSUES_FILE="$(mktemp)"
trap 'rm -f "$ISSUES_FILE"' EXIT

# One pass over every pod + workload. Appends human-readable issues to
# $ISSUES_FILE and echoes the issue count. Quiet otherwise (we poll this).
run_check() {
  : > "$ISSUES_FILE"

  # Pods: real container readiness, phase, restart sanity. Restart counts are
  # lifetime, so only a restart kubectl dates inside the churn window counts as
  # live churn; a bare count has the AGE column next to it, not a recency.
  while read -r ns name ready status restarts recency _; do
    watched_ns "$ns" || continue
    case "$status" in Completed|Succeeded) continue ;; esac
    local have="${ready%%/*}" want="${ready##*/}"
    if [ "$status" != "Running" ]; then
      echo "pod $ns/$name status=$status (ready=$ready restarts=$restarts)" >> "$ISSUES_FILE"
    elif [ "$have" != "$want" ]; then
      echo "pod $ns/$name NOT READY ($ready)" >> "$ISSUES_FILE"
    elif [ "${restarts:-0}" -gt "$THRESH" ] 2>/dev/null && recent_restart "$recency"; then
      echo "pod $ns/$name runaway restarts ($restarts > $THRESH, last $recency ago)" >> "$ISSUES_FILE"
    fi
  done < <(kubectl get pods -A --no-headers 2>/dev/null)

  # Deployments + StatefulSets: readyReplicas == spec.replicas.
  local kind
  for kind in deployment statefulset; do
    while read -r ns name want have; do
      [ -z "$ns" ] && continue
      watched_ns "$ns" || continue
      [ "$want" = "<none>" ] && want=0
      [ "${have:-0}" != "${want:-0}" ] && echo "$kind $ns/$name ${have:-0}/${want:-0} ready" >> "$ISSUES_FILE"
    done < <(kubectl get "$kind" -A --no-headers -o custom-columns=NS:.metadata.namespace,N:.metadata.name,W:.spec.replicas,H:.status.readyReplicas 2>/dev/null)
  done
  # DaemonSets: numberReady == desiredNumberScheduled.
  while read -r ns name want have; do
    [ -z "$ns" ] && continue
    watched_ns "$ns" || continue
    [ "${have:-0}" != "${want:-0}" ] && echo "daemonset $ns/$name ${have:-0}/${want:-0} ready" >> "$ISSUES_FILE"
  done < <(kubectl get daemonset -A --no-headers -o custom-columns=NS:.metadata.namespace,N:.metadata.name,W:.status.desiredNumberScheduled,H:.status.numberReady 2>/dev/null)

  # PRESENCE. Every check above judges what exists, so a deploy that produced
  # nothing passes them all. An unseeded or unreadable deploy repo generates zero
  # Applications and leaves this namespace empty.
  if [ -n "$DFE_NS" ]; then
    local workloads
    workloads=$(kubectl -n "$DFE_NS" get deployment,statefulset --no-headers 2>/dev/null | grep -c .)
    if [ "${workloads:-0}" -eq 0 ]; then
      echo "namespace $DFE_NS has NO app workloads -- the deploy repo enabled no apps (expected one values/<svc>-<inst>-values.yaml per app)" >> "$ISSUES_FILE"
    fi
  fi

  grep -c . "$ISSUES_FILE"
}

echo "=== DFE deploy readiness gate (waiting up to ${TIMEOUT}s for convergence) ==="
SECONDS=0
while :; do
  n="$(run_check)"
  if [ "$n" -eq 0 ]; then
    # Ready is not the same as safe: a healthy stack on the shipped admin
    # password is open, so the credential verdict decides the exit code too.
    check_default_credentials || exit 1
    report_issuer_mode
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
