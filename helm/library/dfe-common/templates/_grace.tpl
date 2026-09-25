{{/*
dfe-common.terminationGrace -- how long the kubelet waits between SIGTERM and
SIGKILL for an app pod.

A stage holding source acks has to release them inside this window. Killed
first, it leaves records it already delivered unacknowledged, and the source
sends them again. The drain is scalo's 5 s pre-stop while the endpoints drop
the pod, then up to its 20 s gRPC drain deadline. 45 s leaves the rest for the
final commit and flush, where the Kubernetes default of 30 s leaves 5 s.

A chart whose process waits on something slower sets
`terminationGracePeriodSeconds` in its own values. Read with hasKey, so an
explicit 0 is honoured rather than taken for unset.

Usage (pod spec):
  {{- include "dfe-common.terminationGrace" . | nindent 6 }}
*/}}
{{- define "dfe-common.terminationGrace" -}}
{{- $grace := 45 -}}
{{- if hasKey .Values "terminationGracePeriodSeconds" -}}
{{- $grace = .Values.terminationGracePeriodSeconds -}}
{{- end -}}
terminationGracePeriodSeconds: {{ $grace }}
{{- end -}}
