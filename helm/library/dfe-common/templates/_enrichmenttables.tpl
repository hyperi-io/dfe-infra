{{/*
Delivery of the enrichment tables a transform looks up at runtime.

Same shape and same reason as the transform files: dfe-engine writes them into
the per-instance $values overlay as a list of {name, content}, because Helm
cannot read a raw file out of an Argo $values source. These helpers turn that
list into a ConfigMap, a volume and the mount.

Kept a separate volume from the transform files because the app reads the two
differently -- a directory of programs versus named tables referenced by
`get_enrichment_table_record`. The bundled filebeat pipeline needs its timezones
table for the ios and meraki branches, and errors every event out without it.

The matching `enrichment_tables[]` entry is derived here too
(dfe-common.enrichmentTablesConfig), because the mount directory is the chart's
and the engine cannot know it.
*/}}

{{/*
The directory the enrichment tables are mounted into. A sibling of the config
mount, never a child: nesting one ConfigMap volume inside another makes the
parent's visible contents depend on mount ordering.
*/}}
{{- define "dfe-common.enrichmentTablesDir" -}}
{{- .Values.enrichmentTablesDir | default (printf "/etc/dfe-%s-enrichment" .Values.component) -}}
{{- end -}}

{{- define "dfe-common.enrichmentTablesName" -}}
{{- printf "%s-enrichment" (include "dfe-common.fullname" .) -}}
{{- end -}}

{{/*
The ConfigMap data block, one file per entry.

Serialised through toYaml rather than a hand-rolled block scalar: a bare `|`
takes its indentation from the first non-empty line, so a file whose first line
is indented further than a later one terminates the block early and breaks the
whole render.

Usage:
  data:
    {{- include "dfe-common.enrichmentTablesData" . | nindent 2 }}
*/}}
{{- define "dfe-common.enrichmentTablesData" -}}
{{- $data := dict -}}
{{- range .Values.enrichmentTables -}}
{{- $_ := set $data .name .content -}}
{{- end -}}
{{- toYaml $data -}}
{{- end -}}

{{- define "dfe-common.enrichmentTablesVolume" -}}
{{- if .Values.enrichmentTables }}
- name: enrichment
  configMap:
    name: {{ include "dfe-common.enrichmentTablesName" . }}
{{- end }}
{{- end -}}

{{- define "dfe-common.enrichmentTablesMount" -}}
{{- if .Values.enrichmentTables }}
- name: enrichment
  mountPath: {{ include "dfe-common.enrichmentTablesDir" . }}
  readOnly: true
{{- end }}
{{- end -}}

{{/*
No env helper here on purpose. The app has no flat-env key for enrichment --
`enrichment_tables` is a list, and its loader reads it from the config blob
only, so a DFE_TRANSFORM_ENRICHMENT_DIR would be inert.

The `enrichment_tables[]` entries are derived HERE, not by the engine: the
mount directory is this chart's, so the engine cannot know the path. Every
mounted table the config blob does not already name gets a legacy flat entry
{name, path}, the name being the file name without its extension -- the name
a VRL program looks the table up by. A key column cannot be derived from a
file name, so an entry without one is scanned per lookup; a config blob that
names the table with `key_columns` keeps its own entry.

Usage:
  {{- $tables := include "dfe-common.enrichmentTablesConfig" . | fromYamlArray }}
*/}}
{{- define "dfe-common.enrichmentTablesConfig" -}}
{{- $dir := include "dfe-common.enrichmentTablesDir" . -}}
{{- $declared := list -}}
{{- range (default list (get (default dict .Values.config) "enrichment_tables")) -}}
{{- $declared = append $declared (default "" .name) -}}
{{- end -}}
{{- $tables := list -}}
{{- range .Values.enrichmentTables -}}
{{- $name := regexReplaceAll "\\.[^.]+$" .name "" -}}
{{- if not (has $name $declared) -}}
{{- $tables = append $tables (dict "name" $name "path" (printf "%s/%s" $dir .name)) -}}
{{- end -}}
{{- end -}}
{{- toYaml $tables -}}
{{- end -}}
