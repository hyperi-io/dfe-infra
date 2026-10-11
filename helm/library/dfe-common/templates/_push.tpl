{{/*
dfe-common.pushPort -- the scalo Push listener port, chart-overridable via
service.pushPort. Port 6000 is the platform convention apps.yaml records under
`endpoints`.
*/}}
{{- define "dfe-common.pushPort" -}}
{{- if .Values.service -}}
{{- default 6000 .Values.service.pushPort -}}
{{- else -}}
6000
{{- end -}}
{{- end -}}
