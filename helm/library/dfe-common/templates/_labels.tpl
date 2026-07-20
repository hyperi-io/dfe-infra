{{/*
dfe-common.labels — standard Kubernetes labels per DFE spec Section 9.4.
Requires .Values.project, .Values.component, .Values.env, .Values.cloud.
Usage:
  labels:
    {{- include "dfe-common.labels" . | nindent 4 }}
*/}}
{{- define "dfe-common.labels" -}}
app.kubernetes.io/name: {{ printf "%s-%s" .Values.project .Values.component | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/part-of: {{ .Values.project | quote }}
app.kubernetes.io/managed-by: "helm"
app.kubernetes.io/version: {{ .Chart.AppVersion | default "0.0.0" | quote }}
dfe.hyperi.io/env: {{ .Values.env | quote }}
dfe.hyperi.io/cloud: {{ .Values.cloud | quote }}
{{- end }}

{{/*
dfe-common.selectorLabels — minimal stable labels for Deployment selectors.
Only app.kubernetes.io/name — must not change after first deploy (immutable selector).
*/}}
{{- define "dfe-common.selectorLabels" -}}
app.kubernetes.io/name: {{ printf "%s-%s" .Values.project .Values.component | quote }}
{{- end }}
