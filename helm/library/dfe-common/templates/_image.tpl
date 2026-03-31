{{/*
dfe-common.image — constructs the full container image reference.
Uses global.registry + component name + tag (or Chart.AppVersion).
Allows override via image.repository for customers using a different naming convention.

Usage:
  image: {{ include "dfe-common.image" . }}
*/}}
{{- define "dfe-common.image" -}}
{{- $registry := .Values.global.registry | default "" -}}
{{- $repo := .Values.image.repository | default "" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if $repo -}}
  {{- printf "%s:%s" $repo $tag -}}
{{- else if $registry -}}
  {{- printf "%s/dfe-%s:%s" $registry .Values.component $tag -}}
{{- else -}}
  {{- printf "dfe-%s:%s" .Values.component $tag -}}
{{- end -}}
{{- end }}
