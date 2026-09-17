{{/*
dfe-common.image -- constructs the full container image reference.
Uses global.registry + image component name + tag (or Chart.AppVersion), with an
optional image.digest appended as tag@sha256:... .
Allows override via image.repository for customers using a different naming convention.

Usage:
  image: {{ include "dfe-common.image" . }}
*/}}

{{/*
The component the image is published under, which is not always the one the
Kubernetes objects are named after.

`component` names objects and so must be unique per instance for a multi-instance
app; the image belongs to the app, not the instance. Defaults to `component`, so
a single-instance chart renders the same reference as before.

Not derived from `.Chart.Name`: clickhouse-cluster and envoy-gateway-config both
publish under a name their chart does not carry.
*/}}
{{- define "dfe-common.imageComponent" -}}
{{- .Values.imageComponent | default .Values.component -}}
{{- end -}}

{{- define "dfe-common.image" -}}
{{- /* global is nil (not just its .registry) when a chart carries no global:
       block, e.g. bare `helm lint`; `with` guards that so the else-branch below
       yields a registry-less ref rather than a nil-pointer panic. */ -}}
{{- $registry := "" -}}
{{- with .Values.global }}{{- $registry = .registry | default "" -}}{{- end -}}
{{- $repo := .Values.image.repository | default "" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- $name := include "dfe-common.imageComponent" . -}}
{{- /* Optional, and it wins over the tag it is pulled with: an overlay moving image.tag must move image.digest too. */ -}}
{{- with .Values.image.digest }}{{- $tag = printf "%s@%s" $tag . -}}{{- end -}}
{{- if $repo -}}
  {{- printf "%s:%s" $repo $tag -}}
{{- else if $registry -}}
  {{- printf "%s/dfe-%s:%s" $registry $name $tag -}}
{{- else -}}
  {{- printf "dfe-%s:%s" $name $tag -}}
{{- end -}}
{{- end }}
