{{/*
dfe-common.fullname — canonical resource name: {project}-{component}.
Does NOT include env (env goes in namespace, not resource name) to keep names stable.
*/}}
{{- define "dfe-common.fullname" -}}
{{- printf "%s-%s" .Values.project .Values.component | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
dfe-common.namespace — namespace: {project}-{env}.
Used when a chart needs to reference its own namespace explicitly.
*/}}
{{- define "dfe-common.namespace" -}}
{{- printf "%s-%s" .Values.project .Values.env }}
{{- end }}

{{/*
dfe-common.serviceAccountName — K8s ServiceAccount name: {project}-{component}.
Same as fullname, explicit for clarity.
*/}}
{{- define "dfe-common.serviceAccountName" -}}
{{- printf "%s-%s" .Values.project .Values.component }}
{{- end }}
