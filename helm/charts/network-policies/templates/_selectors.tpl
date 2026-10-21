{{/*
network-policies.appSelectors -- the policies one app's grant renders as, as a
YAML list of {suffix, podSelector}. Takes (dict "root" $ "app" <name>).

Selectors are ANDed, so one policy cannot match two label keys. The first copy
selects app.kubernetes.io/name, which a workload outside the thin charts carries,
and the second selects appLabel, which every instance of a thin-chart app carries
whatever it is named. "*" is every pod in the namespace, and renders once.

Usage:
  {{- range $sel := include "network-policies.appSelectors" (dict "root" $ "app" $app) | fromYamlArray }}
*/}}
{{- define "network-policies.appSelectors" -}}
{{- $app := .app -}}
{{- if eq $app "*" -}}
{{- list (dict "suffix" "" "podSelector" (dict)) | toYaml -}}
{{- else -}}
{{- $out := list (dict "suffix" "" "podSelector" (dict "matchLabels" (dict "app.kubernetes.io/name" $app))) -}}
{{- with .root.Values.appLabel -}}
{{- $out = append $out (dict "suffix" "-instances" "podSelector" (dict "matchLabels" (dict . $app))) -}}
{{- end -}}
{{- toYaml $out -}}
{{- end -}}
{{- end }}
