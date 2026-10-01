{{/*
dfe-common.extraEnv -- the operator's own environment keys, as env entries.

An app's settings surface is wider than the dials its chart declares, so a key
the chart does not model is written into the overlay's `extraEnv` block and
lands here. Values are quoted, so a number or a boolean reaches the container as
the string env demands.

Keys render in name order, and the block goes FIRST in every env block: the
entries below it are the deployment's own wiring -- broker addresses, SASL
credentials, transform paths -- and Kubernetes keeps the last of two entries
with one name, so on a collision the chart's own value wins.

Usage:
  env:
    {{- include "dfe-common.extraEnv" . | nindent 12 }}
    ...every derived entry...
*/}}
{{- define "dfe-common.extraEnv" -}}
{{- range $name, $value := .Values.extraEnv }}
- name: {{ $name }}
  value: {{ $value | quote }}
{{- end }}
{{- end -}}
