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
