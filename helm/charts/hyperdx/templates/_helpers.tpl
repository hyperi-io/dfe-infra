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
The dfe-engine image the dashboard init container runs.

dfe-common.image reads .Values.component, which is "hyperdx" here, so the engine
reference is built rather than borrowed. An explicit dashboards.image.repository
wins, for a mirrored or renamed registry.
*/}}
{{- define "hyperdx.dashboardsImage" -}}
{{- $tag := .Values.dashboards.image.tag -}}
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
