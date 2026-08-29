{{/*
dfe-common.reloadAnnotations — restart the workload when a Secret or ConfigMap
it consumes changes.

Env taken from a secretKeyRef resolves once at pod start, so a rotated Secret
never reaches a running pod and surfaces later as an auth failure on whichever
side restarted most recently. Reloader ships with the stack; this annotation is
what makes it act. Off by default so a deployment opts in (`reload.enabled`).

Usage (deployment/statefulset metadata, NOT the pod template):
  metadata:
    annotations:
      {{- include "dfe-common.reloadAnnotations" . | nindent 4 }}
*/}}
{{- define "dfe-common.reloadAnnotations" -}}
{{- if .Values.reload }}
{{- if .Values.reload.enabled }}
reloader.stakater.com/auto: "true"
{{- with .Values.reload.secrets }}
secret.reloader.stakater.com/reload: {{ join "," . | quote }}
{{- end }}
{{- with .Values.reload.configmaps }}
configmap.reloader.stakater.com/reload: {{ join "," . | quote }}
{{- end }}
{{- end }}
{{- end }}
{{- end -}}
