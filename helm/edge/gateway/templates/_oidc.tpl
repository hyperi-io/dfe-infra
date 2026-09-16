{{/*
OIDC provider helpers. Local to this chart: which provider owns a route's
interactive login is an edge concern.

Takes (dict "ctx" $).

loginProviderName -- the name of the ONE provider whose oidc: block a route's
  SecurityPolicy carries. A SecurityPolicy takes a single OIDC provider
  (spec.oidc is an object, not a list), and two policies naming the same
  HTTPRoute with no sectionName conflict: Envoy Gateway keeps the oldest by
  creationTimestamp and drops the rest, with nothing in the apply output to say
  so. So the designation is declared in oidc.loginProvider rather than falling
  out of list order, and an unset value takes the first entry.
*/}}

{{- define "envoy-gateway-config.loginProviderName" -}}
{{- $names := list -}}
{{- range $p := .ctx.Values.oidc.providers -}}
{{- $names = append $names $p.name -}}
{{- end -}}
{{- $named := .ctx.Values.oidc.loginProvider -}}
{{- if $named -}}
{{- if not (has $named $names) -}}
{{- fail (printf "oidc.loginProvider is %s, which names no entry in oidc.providers (have: %s)" $named (join ", " $names)) -}}
{{- end -}}
{{- $named -}}
{{- else -}}
{{- first $names -}}
{{- end -}}
{{- end -}}
