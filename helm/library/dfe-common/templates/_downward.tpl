{{/*
dfe-common.podNamespaceEnv -- the pod's namespace as POD_NAMESPACE, from the
downward API.

scalo reads the namespace from POD_NAMESPACE first and falls back to the
service-account token mount, so a pod that mounts no token needs this entry or
its logs carry no namespace.

Usage (container env block, after dfe-common.extraEnv):
  {{- include "dfe-common.podNamespaceEnv" . | nindent 12 }}
*/}}
{{- define "dfe-common.podNamespaceEnv" -}}
- name: POD_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
{{- end -}}
