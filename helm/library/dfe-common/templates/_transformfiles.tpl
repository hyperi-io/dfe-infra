{{/*
Delivery of the transform files an app reads off disk.

dfe-engine writes them into the per-instance $values overlay as a list of
{name, content}, because Helm cannot read a raw file out of an Argo $values
source -- a chart-rendered ConfigMap can only carry what is already in the values.
These helpers turn that list into a ConfigMap, a volume, and the mount.

The mount path is also exported as DFE_TRANSFORM_TRANSFORMS_DIR. The flat env
override outranks the config file, so the app reads the directory the chart
actually mounted rather than whatever the opaque config blob happens to say.
*/}}

{{/*
The directory the transform files are mounted into. The default is a sibling of
the config mount rather than a child, because nesting one ConfigMap volume inside
another leaves the parent's visible contents dependent on mount ordering. An
overlay setting transformFilesDir below the config mount reintroduces that.
*/}}
{{- define "dfe-common.transformFilesDir" -}}
{{- .Values.transformFilesDir | default (printf "/etc/dfe-%s-transforms" .Values.component) -}}
{{- end -}}

{{/*
ConfigMap name holding the transform files.
*/}}
{{- define "dfe-common.transformFilesName" -}}
{{- printf "%s-transforms" (include "dfe-common.fullname" .) -}}
{{- end -}}

{{/*
The ConfigMap data block. Each entry becomes one file keyed by its name.

Serialised through toYaml rather than emitted as a hand-rolled block scalar: a
bare `|` block takes its indentation from the first non-empty line, so a file
whose first line is indented further than a later one terminates the block early
and breaks the whole ConfigMap render. Letting the YAML library choose the
representation also keeps leading tabs, CRLF and trailing whitespace intact.

Usage in a chart template:
  data:
    {{- include "dfe-common.transformFilesData" . | nindent 2 }}
*/}}
{{- define "dfe-common.transformFilesData" -}}
{{- $data := dict -}}
{{- range .Values.transformFiles -}}
{{- $_ := set $data .name .content -}}
{{- end -}}
{{- toYaml $data -}}
{{- end -}}

{{/*
Volume for the transform files. Emits nothing when there are none, so a chart
can include it unconditionally.
*/}}
{{- define "dfe-common.transformFilesVolume" -}}
{{- if .Values.transformFiles }}
- name: transforms
  configMap:
    name: {{ include "dfe-common.transformFilesName" . }}
{{- end }}
{{- end -}}

{{/*
Volume mount for the transform files. Emits nothing when there are none.
*/}}
{{- define "dfe-common.transformFilesMount" -}}
{{- if .Values.transformFiles }}
- name: transforms
  mountPath: {{ include "dfe-common.transformFilesDir" . }}
  readOnly: true
{{- end }}
{{- end -}}

{{/*
Env telling the app where the files were mounted. Emits nothing when there are
none, leaving the app's own config to decide.
*/}}
{{- define "dfe-common.transformFilesEnv" -}}
{{- if .Values.transformFiles }}
- name: DFE_TRANSFORM_TRANSFORMS_DIR
  value: {{ include "dfe-common.transformFilesDir" . | quote }}
{{- end }}
{{- end -}}
