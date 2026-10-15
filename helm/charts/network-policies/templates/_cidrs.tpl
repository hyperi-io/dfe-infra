{{/*
network-policies.internalCIDRs -- the cluster's own ranges, carved OUT of every
internet allow so a compromised pod cannot reach pod or service space.

Read from the shared networkModel (argocd/values/common.yaml), so the ranges are
declared once for the whole deployment rather than repeated per egress policy.
networkModel.carveOut names which of its ranges are carved: a cloud whose pod
range is shared with the backends pods dial leaves that range out. A purpose
that pins its own exceptCIDRs overrides this; an empty networkModel key drops
that range from the carve-out.

Usage:
  {{- $except := $eg.exceptCIDRs | default (include "network-policies.internalCIDRs" . | fromYamlArray) }}
*/}}
{{- define "network-policies.internalCIDRs" -}}
{{- $model := .Values.networkModel | default dict -}}
{{- if not (kindIs "slice" $model.carveOut) -}}
{{- fail "networkModel.carveOut must list the networkModel ranges carved out of every internet allow, e.g. [podCIDR, serviceCIDR, linkLocalCIDR]" -}}
{{- end -}}
{{- $out := list -}}
{{- range $key := $model.carveOut -}}
{{- if not (hasKey $model $key) -}}
{{- fail (printf "networkModel.carveOut names %q, which networkModel does not declare, so that range would go uncarved" $key) -}}
{{- end -}}
{{- with index $model $key -}}
{{- $out = append $out . -}}
{{- end -}}
{{- end -}}
{{- toYaml $out -}}
{{- end }}
