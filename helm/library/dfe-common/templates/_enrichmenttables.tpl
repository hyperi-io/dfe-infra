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

The engine writes the matching `enrichment_tables[].path` into the config blob;
this only guarantees the files are on disk where those paths point.
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
only, so a DFE_TRANSFORM_ENRICHMENT_DIR would be inert. The engine writes each
`enrichment_tables[].path` under enrichmentTablesDir instead.
*/}}
