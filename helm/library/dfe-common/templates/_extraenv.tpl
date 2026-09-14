{{/*
dfe-common.extraEnv -- the operator's own environment keys, as env entries.

An app's settings surface is wider than the dials its chart declares, so a key
the chart does not model is written into the overlay's `extraEnv` block and
lands here. Values are quoted, so a number or a boolean reaches the container as
the string env demands.

Keys render in name order, and the block goes LAST in every env block: the
entries above it are the deployment's own wiring -- broker addresses, SASL
credentials, transform paths -- and keeping the operator's keys in one tail
rather than interleaved is what makes a collision visible in the rendered pod.

Usage:
  env:
    ...every derived entry...
    {{- include "dfe-common.extraEnv" . | nindent 12 }}
*/}}
{{- define "dfe-common.extraEnv" -}}
{{- range $name, $value := .Values.extraEnv }}
- name: {{ $name }}
  value: {{ $value | quote }}
{{- end }}
{{- end -}}
