#!/usr/bin/env bash
# fix-harbor-trivy.sh -- DRAFT 2026-07-07. NEEDS-OK (live mutation; run attended).
#
# harbor-trivy CrashLoopBackOff root cause (diagnosed):
#   mkdir /home/scanner/.cache/trivy: permission denied
# The pod runs as uid/gid 10000 with fsGroup:10000, BUT the cache PVC is on the
# `local-path` storageClass (Rancher local-path-provisioner) which does NOT apply
# fsGroup -> the volume stays root-owned and uid 10000 cannot write.
# Harbor is a raw helm release (chart harbor-1.18.2), NOT Argo-managed.
#
# This script offers the reliable fix (init-container chown) and prints the
# alternatives. Review before running.
set -euo pipefail
CTX="${KUBE_CONTEXT:-devex}"
NS=harbor

echo "== current trivy state =="
kubectl --context "$CTX" -n "$NS" get pod harbor-trivy-0 2>/dev/null || true

# --- FIX A (recommended, repeatable-ish): add a root init-container that chowns the
#     cache dir on start. Survives pod restarts; reverted by `helm upgrade harbor`,
#     so ALSO fold this into the Harbor values (see FIX C) for permanence.
patch_initcontainer() {
  kubectl --context "$CTX" -n "$NS" patch statefulset harbor-trivy --type=json -p '[
    {"op":"add","path":"/spec/template/spec/initContainers","value":[
      {"name":"fix-cache-perms","image":"busybox:1.36",
       "command":["sh","-c","chown -R 10000:10000 /home/scanner/.cache && chmod -R u+rwX /home/scanner/.cache"],
       "securityContext":{"runAsUser":0,"runAsNonRoot":false},
       "volumeMounts":[{"name":"data","mountPath":"/home/scanner/.cache"}]}
    ]}
  ]'
  kubectl --context "$CTX" -n "$NS" rollout restart statefulset harbor-trivy
  kubectl --context "$CTX" -n "$NS" rollout status statefulset harbor-trivy --timeout=120s
}

# --- FIX B (fastest, one-time): chown the local-path dir on the node. Needs the
#     node hostPath; find it via the PV, SSH the node (Vault), chown, restart.
#   PVDIR=$(kubectl --context $CTX get pv $(kubectl --context $CTX -n harbor get pvc data-harbor-trivy-0 -o jsonpath='{.spec.volumeName}') -o jsonpath='{.spec.hostPath.path}{.spec.local.path}')
#   ssh <node> "sudo chown -R 10000:10000 $PVDIR"
#   kubectl --context $CTX -n harbor delete pod harbor-trivy-0

# --- FIX C (permanent, helm-tracked): fold FIX A's init-container into the Harbor
#     values and `helm upgrade harbor harbor/harbor -f <values>`; OR set the trivy
#     cache to an emptyDir (cache is ephemeral - re-downloads the vuln DB). Confirm
#     the chart 1.18.2 trivy persistence toggle first. This is the real fix; pairs
#     with bringing Harbor under GitOps (Argo) later.

echo "Applying FIX A (init-container chown). Ctrl-C to abort."
patch_initcontainer
echo "== trivy after fix =="
kubectl --context "$CTX" -n "$NS" get pod -l component=trivy 2>/dev/null || true
echo "NOTE: fold FIX A into Harbor values (FIX C) so a helm upgrade doesn't revert it."
