{{/*
Version-check deployment seam. The apps ship the check ON by default with
their own releases endpoint baked in, so the chart's job is only the
DEPLOYMENT override: emitted solely when a versionCheck value is set,
leaving today's rendered manifests byte-identical. Set fleet-wide in
argocd/values/common.yaml or per app in its overlay:

  versionCheck:
    enabled: false        # total opt-out -- no check, nothing sent (air-gap)
    sendInstanceId: false # keep the check, strip the install id
    apiUrl: ""            # point the check at a mirror
    instanceId: ""        # operator-chosen id, sent verbatim

Usage in a container env block (prefix = the app's cascade env prefix
INCLUDING its trailing underscore; the engine passes "" -- its cascade
reads bare vars):

  {{- include "dfe-common.versionCheckEnv" (dict "ctx" . "prefix" "DFE_LOADER_") | nindent 12 }}
*/}}
{{- define "dfe-common.versionCheckEnv" -}}
{{- $vc := .ctx.Values.versionCheck | default dict -}}
{{- if hasKey $vc "enabled" }}
- name: {{ .prefix }}VERSION_CHECK__ENABLED
  value: {{ $vc.enabled | quote }}
{{- end }}
{{- if hasKey $vc "sendInstanceId" }}
- name: {{ .prefix }}VERSION_CHECK__SEND_INSTANCE_ID
  value: {{ $vc.sendInstanceId | quote }}
{{- end }}
{{- if $vc.apiUrl }}
- name: {{ .prefix }}VERSION_CHECK__API_URL
  value: {{ $vc.apiUrl | quote }}
{{- end }}
{{- if $vc.instanceId }}
- name: {{ .prefix }}VERSION_CHECK__INSTANCE_ID
  value: {{ $vc.instanceId | quote }}
{{- end }}
{{- end -}}
