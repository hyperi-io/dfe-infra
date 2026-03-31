{{/*
dfe-common.nodeSelector — renders nodeSelector block for pod specs.
Reads from .Values.nodeScheduling.nodeSelector (map of label key→value).
Usage:
  spec:
    {{- include "dfe-common.nodeSelector" . | nindent 6 }}
*/}}
{{- define "dfe-common.nodeSelector" -}}
{{- if .Values.nodeScheduling }}
{{- if .Values.nodeScheduling.nodeSelector }}
nodeSelector:
  {{- toYaml .Values.nodeScheduling.nodeSelector | nindent 2 }}
{{- end }}
{{- end }}
{{- end }}

{{/*
dfe-common.tolerations — renders tolerations block for pod specs.
Reads from .Values.nodeScheduling.tolerations (list of toleration objects).
Usage:
  spec:
    {{- include "dfe-common.tolerations" . | nindent 6 }}
*/}}
{{- define "dfe-common.tolerations" -}}
{{- if .Values.nodeScheduling }}
{{- if .Values.nodeScheduling.tolerations }}
tolerations:
  {{- toYaml .Values.nodeScheduling.tolerations | nindent 2 }}
{{- end }}
{{- end }}
{{- end }}

{{/*
dfe-common.imagePullSecrets — renders imagePullSecrets block for pod specs.
Reads from .Values.imagePullSecrets (list of secret names).
Usage:
  spec:
    {{- include "dfe-common.imagePullSecrets" . | nindent 6 }}
*/}}
{{- define "dfe-common.imagePullSecrets" -}}
{{- if .Values.imagePullSecrets }}
imagePullSecrets:
  {{- range .Values.imagePullSecrets }}
  - name: {{ . }}
  {{- end }}
{{- end }}
{{- end }}

{{/*
dfe-common.scheduling — renders imagePullSecrets, nodeSelector, and tolerations.
Single include for pod specs that need full scheduling config.
Usage:
  spec:
    {{- include "dfe-common.scheduling" . | nindent 6 }}
*/}}
{{- define "dfe-common.scheduling" -}}
{{- include "dfe-common.imagePullSecrets" . }}
{{- include "dfe-common.nodeSelector" . }}
{{- include "dfe-common.tolerations" . }}
{{- end }}
