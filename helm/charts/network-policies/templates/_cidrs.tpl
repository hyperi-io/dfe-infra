{{/*
network-policies.internalCIDRs — the cluster's own ranges, carved OUT of every
internet allow so a compromised pod cannot reach pod or service space.

Read from the shared networkModel (argocd/values/common.yaml), so the ranges are
declared once for the whole deployment rather than repeated per egress policy. A
purpose that pins its own exceptCIDRs overrides this; an empty networkModel key
drops that range from the carve-out.

Usage:
  {{- $except := $eg.exceptCIDRs | default (include "network-policies.internalCIDRs" . | fromYamlArray) }}
*/}}
{{- define "network-policies.internalCIDRs" -}}
{{- $model := .Values.networkModel | default dict -}}
{{- $out := list -}}
{{- range $key := list "podCIDR" "serviceCIDR" "linkLocalCIDR" -}}
{{- with index $model $key -}}
{{- $out = append $out . -}}
{{- end -}}
{{- end -}}
{{- toYaml $out -}}
{{- end }}
