{{/*
The engine's JWKS endpoint, which oidc-proxy mode verifies every token against.

An explicit dfeAuth.engineJwksUrl wins, for an engine outside the cluster. Otherwise
it is derived from the engine's service, defaulting to this release's namespace so a
co-deployed engine needs no configuration at all.
*/}}
{{- define "hyperdx.engineJwksUrl" -}}
{{- with .Values.dfeAuth.engineJwksUrl -}}
{{ . }}
{{- else -}}
{{- $ns := .Values.dfeAuth.engineNamespace | default .Release.Namespace -}}
{{- printf "http://%s.%s.svc.cluster.local:%v/.well-known/jwks.json" .Values.dfeAuth.engineService $ns .Values.dfeAuth.enginePort -}}
{{- end -}}
{{- end -}}

{{/*
hyperdx.clickhouseCa -- "true" when a ClickHouse CA is mounted for Node to
trust, empty otherwise.
*/}}
{{- define "hyperdx.clickhouseCa" -}}
{{- $tls := .Values.clickhouse.tls | default dict -}}
{{- if eq (toString $tls.enabled) "true" -}}
{{- $ca := $tls.ca | default dict -}}
{{- if and $ca.secretName $ca.configMapName -}}
{{- fail "clickhouse.tls.ca takes a secretName or a configMapName, not both" -}}
{{- end -}}
{{- if or $ca.secretName $ca.configMapName -}}true{{- end -}}
{{- end -}}
{{- end -}}

{{/*
The dfe-engine image the dashboard init container runs.

dfe-common.image reads .Values.component, which is "hyperdx" here, so the engine
reference is built rather than borrowed. An explicit dashboards.image.repository
wins, for a mirrored or renamed registry.

dashboards.image.digest is appended as tag@sha256, the same immutable half
dfe-common.image carries.
*/}}
{{- define "hyperdx.dashboardsImage" -}}
{{- $tag := .Values.dashboards.image.tag -}}
{{- /* Optional, and it wins over the tag it is pulled with: an overlay moving dashboards.image.tag must move dashboards.image.digest too. */ -}}
{{- with .Values.dashboards.image.digest }}{{- $tag = printf "%s@%s" $tag . -}}{{- end -}}
{{- with .Values.dashboards.image.repository -}}
{{- printf "%s:%s" . $tag -}}
{{- else -}}
{{- $registry := "" -}}
{{- with .Values.global }}{{- $registry = .registry | default "" -}}{{- end -}}
{{- if $registry -}}
{{- printf "%s/dfe-engine:%s" $registry $tag -}}
{{- else -}}
{{- printf "dfe-engine:%s" $tag -}}
{{- end -}}
{{- end -}}
{{- end -}}
