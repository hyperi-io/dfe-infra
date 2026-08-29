{{/*
Route exposure and classification helpers. Local to this chart: classification
is an edge concern, and dfe-common is shared by every DFE chart.

Each takes (dict "ctx" $ "key" "<routes values key>").

routeEnabled -- does this route render? First match wins:
  1. class infra and exposure.infraUisExternal false -> "" (the kill switch is
     absolute for the class, so a route's own enabled:true does not beat it)
  2. enabled false                                   -> ""
  3. otherwise                                       -> "true"
routeHost    -- subdomain label, from the route's hostname or hostnames[hostnameKey].
routeNs      -- namespace of the route and of any SecurityPolicy targeting it.
infraPolicyRoutes -- JSON array of the keys that render AND take the infra edge
  policy, so the SecurityPolicy and the ExternalSecret feeding it cannot
  disagree about which routes are covered. Read it with fromJsonArray.
*/}}

{{- define "envoy-gateway-config.routeEnabled" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- $class := $r.class | default "infra" -}}
{{- if and (eq $class "infra") (not .ctx.Values.exposure.infraUisExternal) -}}
{{- else if $r.enabled -}}
true
{{- end -}}
{{- end -}}

{{- define "envoy-gateway-config.routeHost" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- $r.hostname | default (index .ctx.Values.hostnames $r.hostnameKey) -}}
{{- end -}}

{{- define "envoy-gateway-config.routeNs" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- $r.backendNamespace | default .ctx.Values.appNamespace | default .ctx.Release.Namespace -}}
{{- end -}}

{{- define "envoy-gateway-config.infraPolicyRoutes" -}}
{{- $keys := list -}}
{{- range $key, $r := .ctx.Values.routes -}}
{{- if and (eq ($r.class | default "infra") "infra") $r.edgePolicy -}}
{{- if include "envoy-gateway-config.routeEnabled" (dict "ctx" $.ctx "key" $key) -}}
{{- $keys = append $keys $key -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $keys | sortAlpha | toJson -}}
{{- end -}}
