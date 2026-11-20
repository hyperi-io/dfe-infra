{{/*
dfe-clickhouse.poolSelector -- the node labels one workload's pods require, as YAML: its pool's dfe.hyperi.io/workload label where nodeScheduling.dedicatedPools names the pool or `always` is true, then nodeScheduling.nodeSelector over it.

Usage:
  {{- with include "dfe-clickhouse.poolSelector" (dict "root" $ "pool" "keeper") | fromYaml }}
*/}}
{{- define "dfe-clickhouse.poolSelector" -}}
{{- $scheduling := .root.Values.nodeScheduling | default dict -}}
{{- $selector := dict -}}
{{- if or .always (has .pool ($scheduling.dedicatedPools | default list)) -}}
{{- $_ := set $selector "dfe.hyperi.io/workload" .pool -}}
{{- end -}}
{{- with $scheduling.nodeSelector -}}
{{- $selector = mergeOverwrite $selector (deepCopy .) -}}
{{- end -}}
{{- with $selector }}{{ toYaml . }}{{ end -}}
{{- end }}
