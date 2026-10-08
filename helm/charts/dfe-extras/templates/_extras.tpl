{{/*
dfe-extras.render -- render one body for the components that carry it.

Takes (dict "root" . "charts" (list <chartName>...) "body" <define name>). The
body runs only when chartName is one of `charts`, against the component context
(dfe-extras.context), so a template ported from a 2.2.0 chart reads .Values,
.Chart.AppVersion and $ exactly as it did there.
*/}}
{{- define "dfe-extras.render" -}}
{{- $root := .root -}}
{{- if has (toString $root.Values.chartName) .charts -}}
{{- $ctx := dict -}}
{{- include "dfe-extras.context" (dict "root" $root "ctx" $ctx) -}}
{{- include .body $ctx -}}
{{- end -}}
{{- end -}}

{{/*
dfe-extras.context -- fill (dict "root" . "ctx" <empty dict>) with the root
context of the component chartName names.

Its Values are the layered values with that component's defaults
(extras.defaults.<chartName>) filled in under them, key by key, so a layer that
sets a key -- false and empty included -- wins as it did over the 2.2.0 chart's
own values.yaml. Its Chart.AppVersion is extras.appVersions.<chartName>, the
version the 2.2.0 chart labelled its objects with.

fullnameOverride, when set, names the component the way the thin chart names
it: the component is fullnameOverride less the "<project>-" prefix, so every
dfe-common name helper agrees with the thin chart's objects.
*/}}
{{- define "dfe-extras.context" -}}
{{- $root := .root -}}
{{- $name := toString $root.Values.chartName -}}
{{- $extras := $root.Values.extras | default dict -}}
{{- $values := deepCopy $root.Values -}}
{{- include "dfe-extras.fill" (dict "dst" $values "src" (dig "defaults" $name dict $extras)) -}}
{{- $override := toString ($values.fullnameOverride | default "") -}}
{{- if $override -}}
{{- $prefix := printf "%s-" (toString $values.project) -}}
{{- if not (hasPrefix $prefix $override) -}}
{{- fail (printf "dfe-extras: fullnameOverride %q does not start with %q, so the %s objects cannot carry the name the thin chart gives its workload" $override $prefix $name) -}}
{{- end -}}
{{- $_ := set $values "component" (trimPrefix $prefix $override) -}}
{{- end -}}
{{- $chart := dict "Name" $name "AppVersion" (toString (dig "appVersions" $name "" $extras)) -}}
{{- $_ := set .ctx "Values" $values -}}
{{- $_ := set .ctx "Chart" $chart -}}
{{- $_ := set .ctx "Release" $root.Release -}}
{{- $_ := set .ctx "Files" $root.Files -}}
{{- $_ := set .ctx "Capabilities" $root.Capabilities -}}
{{- $_ := set .ctx "Template" $root.Template -}}
{{- end -}}

{{/*
dfe-extras.fill -- copy into (dict "dst" <map> "src" <map>) every src key dst
lacks, recursing where both hold a map. A key dst already holds wins whatever
its value, which is how Helm coalesces a chart's values.yaml under a layer.
*/}}
{{- define "dfe-extras.fill" -}}
{{- $dst := .dst -}}
{{- range $key, $value := .src -}}
{{- if not (hasKey $dst $key) -}}
{{- if kindIs "invalid" $value -}}
{{- $_ := set $dst $key $value -}}
{{- else -}}
{{- $_ := set $dst $key (deepCopy $value) -}}
{{- end -}}
{{- else if and (kindIs "map" $value) (kindIs "map" (index $dst $key)) -}}
{{- include "dfe-extras.fill" (dict "dst" (index $dst $key) "src" $value) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
dfe-extras.envConfigMap -- the <fullname>-env ConfigMap: every literal entry of
an env list, as data. Takes (dict "ctx" <component context> "env" <env list YAML>).

The list is the 2.2.0 container's env block, conditionals and all, so a variable
stays absent where 2.2.0 omits it. An entry the kubelet resolves (valueFrom, or a
value carrying $(VAR), which envFrom never expands) is left to the thin chart's
env. Names are read in order and the last one wins, as the kubelet reads env.
*/}}
{{- define "dfe-extras.envConfigMap" -}}
{{- $ctx := .ctx -}}
{{- $entries := fromYamlArray .env -}}
{{- $data := dict -}}
{{- range $entries -}}
{{- if not (and (kindIs "map" .) (hasKey . "name")) -}}
{{- fail (printf "dfe-extras: the %s env list did not parse into entries: %v" (include "dfe-common.fullname" $ctx) .) -}}
{{- end -}}
{{- $value := "" -}}
{{- if not (kindIs "invalid" .value) -}}
{{- $value = toString .value -}}
{{- end -}}
{{- if or (hasKey . "valueFrom") (contains "$(" $value) -}}
{{- $_ := unset $data .name -}}
{{- else -}}
{{- $_ := set $data .name $value -}}
{{- end -}}
{{- end }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "dfe-common.fullname" $ctx }}-env
  labels:
    {{- include "dfe-common.labels" $ctx | nindent 4 }}
data:
  {{- toYaml $data | nindent 2 }}
{{- end -}}
