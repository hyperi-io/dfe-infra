{{/*
dfe-common.rollout -- how a DFE app replaces its pods.

Every config change rolls the pod, so the roll has to be safe. Kubernetes'
default allows 25% unavailable, which at one replica tears the old pod down
before the new one is Ready and leaves the stage absent for a container start.

maxUnavailable 0 with maxSurge 1 inverts that: the replacement passes its
readiness probe before the pod it replaces goes away, at the cost of one extra
pod's headroom for the length of the roll.

A workload holding a ReadWriteOnce volume cannot surge past itself, so those
charts keep their own `strategy: Recreate` and do not call this.

Usage (Deployment spec, beside replicas):
  {{- include "dfe-common.rollout" . | nindent 2 }}
*/}}
{{- define "dfe-common.rollout" -}}
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 0
    maxSurge: 1
{{- end -}}
