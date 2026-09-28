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

oidcProvider -- the spec.oidc.provider block for one oidc.providers entry, the
  same for the engine's policy and every infra route's. Takes (dict "provider" $p).
*/}}

{{- define "envoy-gateway-config.oidcProvider" -}}
issuer: {{ .provider.issuerUrl }}
{{- /* Naming both endpoints skips Envoy Gateway's own .well-known fetch,
       which its CONTROL PLANE performs against the system trust store with
       no field anywhere to hand it a CA bundle. An IdP served by this
       deployment's dfe-internal-ca is therefore undiscoverable and the
       policy is rejected outright (dfe-infra #222). */}}
{{- with .provider.authorizationEndpoint }}
authorizationEndpoint: {{ . | quote }}
{{- end }}
{{- with .provider.tokenEndpoint }}
tokenEndpoint: {{ . | quote }}
{{- end }}
{{- with .provider.endSessionEndpoint }}
endSessionEndpoint: {{ . | quote }}
{{- end }}
{{- /* The token exchange then goes to the IdP's Service over the pod
       network instead of back out through the gateway's public hostname.
       A ref outside the policy's namespace needs a ReferenceGrant in the
       IdP's namespace. */}}
{{- with .provider.backendRefs }}
backendRefs:
  {{- range . }}
  - name: {{ .name }}
    {{- with .namespace }}
    namespace: {{ . }}
    {{- end }}
    {{- with .port }}
    port: {{ . }}
    {{- end }}
    kind: {{ .kind | default "Service" }}
    group: {{ .group | default "" | quote }}
  {{- end }}
{{- end }}
{{- /* Cluster settings for that backend -- timeouts, retries, health
       checks. NOT a place to put a CA: backendSettings carries no TLS
       field, so an HTTPS IdP Service needs a BackendTLSPolicy targeting
       the Service instead. */}}
{{- with .provider.backendSettings }}
backendSettings:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

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
