{{/*
Route exposure and classification helpers. Local to this chart: classification
is an edge concern, and dfe-common is shared by every DFE chart.

Each takes (dict "ctx" $ "key" "<routes values key>").

routeEnabled -- does this route render? First match wins:
  1. class infra and exposure.infraUisExternal false -> "" (the kill switch is
     absolute for the class, so a route's own enabled:true does not beat it)
  2. class infra, ownLogin false, and no edgeLogin    -> ""
  3. the backend is another chart's opt-in workload that the deployment is not
     running                                         -> ""
  4. enabled false                                   -> ""
  5. otherwise                                       -> "true"
edgeLogin -- does the infra edge policy render in front of this route? Only then
  is a backend with no login of its own safe to publish.
cruiseControlUi -- is the kafka chart deploying the Cruise Control UI? The
  gateway cannot see another chart's render, so both read the same kafka.* keys
  from the deploy-config SSoT and agree by reading one set of values.
otelIngress  -- is the otel-collector chart serving its bearer-token OTLP
  receiver? Same reason: both charts read otel.ingress.enabled.
validateOtelIngress -- the switch's render guards, which templates/validate.yaml runs.
routeHost    -- subdomain label, from the route's hostname or hostnames[hostnameKey].
routeNs      -- namespace of the route and of any SecurityPolicy targeting it.
infraPolicyRoutes -- JSON array of the keys that render AND take the infra edge
  policy, so the SecurityPolicy and the ExternalSecret feeding it cannot
  disagree about which routes are covered. Read it with fromJsonArray.
dnsMarker    -- the annotation external-dns is told to filter on. Takes no
  argument, because it is a constant this chart and layer1-addons.yaml share.
engineDocs   -- does the engine serve /docs and /redoc? Takes (dict "ctx" $).
responseHeaders -- a rule's `filters:` block for the response headers it sets,
  HSTS included when asked. Takes (dict "hsts" <bool> "set" <list of name/value>
  "indent" <n>).
internalRootPersisted -- does the internal CA root survive a rebuild?
  internal-ca-persist.yaml's own render guard, and edgeHsts's derived
  default when the switch is unset. Takes (dict "ctx" $).
edgeHsts     -- does the wildcard listener send HSTS? tls.edge.hsts when it is a
  bool, else derived from the edge issuer. Takes (dict "ctx" $).
hiddenRule   -- the rule answering 404 on routes.<key>.hiddenPaths, or nothing.
  Takes (dict "ctx" $ "key" <route key> "indent" <n>).
hiddenNamespaces -- JSON array of the namespaces that need the 404 filter.
*/}}

{{/*
The Gateway admits routes from EVERY namespace, so without this marker any
namespace-scoped actor could attach an HTTPRoute to the wildcard listener and
have a name published under the deployment's own domain. external-dns is given
`annotationFilter: dfe.hyperi.io/publish-dns=true`, which it applies to the
routes and Services it reads, so only what this chart renders is published.
*/}}
{{- define "envoy-gateway-config.dnsMarker" -}}
dfe.hyperi.io/publish-dns: "true"
{{- end -}}

{{- define "envoy-gateway-config.cruiseControlUi" -}}
{{- $k := .ctx.Values.kafka -}}
{{- /* All four, because all four gate the kafka chart's own render: Redpanda
       has no Cruise Control, a single broker has nothing to rebalance, and the
       UI follows the rebalancer rather than switching on alone. */ -}}
{{- if and (eq ($k.provider | default "strimzi") "strimzi") (eq ($k.mode | default "disabled") "cluster") $k.rebalancing.enabled $k.rebalancing.ui.enabled -}}
true
{{- end -}}
{{- end -}}

{{- define "envoy-gateway-config.otelIngress" -}}
{{- if .ctx.Values.otel.ingress.enabled -}}
true
{{- end -}}
{{- end -}}

{{- /* The collector serves no authenticated receiver without the token, so the
       route that would front it is refused rather than rendered. The message is
       the collector chart's own, word for word, so both refusals read the same. */ -}}
{{- define "envoy-gateway-config.validateOtelIngress" -}}
{{- $ingress := .ctx.Values.otel.ingress -}}
{{- if not (kindIs "bool" $ingress.enabled) -}}
{{- fail (printf "otel.ingress.enabled is %v (a %s), not a bool -- a quoted \"false\" is a non-empty string and truthy, so it would publish OTLP ingest. Write true or false unquoted" $ingress.enabled (kindOf $ingress.enabled)) -}}
{{- end -}}
{{- if and $ingress.enabled (not $ingress.auth.remoteKey) -}}
{{- fail "otel.ingress.enabled is true and otel.ingress.auth.remoteKey is empty -- OTLP ingest from outside the cluster admits only a bearer token, and this key names where the deployment's secret store holds it. Store the token (property token) at a path such as <project>/<env>/otel/ingress and set otel.ingress.auth.remoteKey to it in the deploy repo's infra/common.yaml. There is no unauthenticated mode" -}}
{{- end -}}
{{- end -}}

{{- /* The engine's own rule (dfe-engine settings.py, api.docs_enabled and
       is_dev_posture): true serves them, false does not, and unset serves them
       on a dev posture only. The links chart and the engine chart apply the
       same list, and scripts/tests/test_engine_docs_surface.py holds all three
       to it. */ -}}
{{- define "envoy-gateway-config.engineDocs" -}}
{{- $v := toString (dig "docsEnabled" "" (.ctx.Values.api | default dict)) -}}
{{- if eq $v "true" -}}
true
{{- else if has $v (list "" "<nil>") -}}
{{- if has (lower (trim (toString .ctx.Values.env))) (list "dev" "development" "local" "test" "ci") -}}
true
{{- end -}}
{{- else if ne $v "false" -}}
{{- fail (printf "api.docsEnabled is %q -- it takes true, false or empty (served on a dev posture only), because the engine, the gateway and the links page each read it and must agree" $v) -}}
{{- end -}}
{{- end -}}

{{- /* The Gateway API takes one filter of each type per rule, so HSTS and a
       route's own headers share one ResponseHeaderModifier. Renders its own
       leading newline at .indent, so an empty result leaves no blank line. */ -}}
{{- define "envoy-gateway-config.responseHeaders" -}}
{{- $set := .set | default list -}}
{{- if .hsts -}}
{{- $set = append $set (dict "name" "Strict-Transport-Security" "value" "max-age=31536000; includeSubDomains") -}}
{{- end -}}
{{- if $set -}}
{{- include "envoy-gateway-config.responseHeaderFilter" $set | nindent (int .indent) -}}
{{- end -}}
{{- end -}}

{{- /* The one place the "root is persisted" condition is written down --
       internal-ca-persist.yaml's render guard reuses it verbatim. */ -}}
{{- define "envoy-gateway-config.internalRootPersisted" -}}
{{- $ca := .ctx.Values.tls.internalCA -}}
{{- if and $ca.enabled $ca.persist.enabled $ca.persist.secretStoreName (not .ctx.Values.tls.vault.server) -}}
true
{{- end -}}
{{- end -}}

{{- /* Unset is off only on an internal CA root that is not persisted: every
       rebuild re-mints it, and a browser holding HSTS for these names then
       gets no click-through. */ -}}
{{- define "envoy-gateway-config.edgeHsts" -}}
{{- $tls := .ctx.Values.tls -}}
{{- $set := dig "edge" "hsts" "" $tls -}}
{{- if kindIs "bool" $set -}}
{{- if $set -}}
true
{{- end -}}
{{- else -}}
{{- $ca := $tls.internalCA -}}
{{- $persisted := include "envoy-gateway-config.internalRootPersisted" . -}}
{{- if not (and (eq $tls.issuerName $ca.issuerName) (not $persisted)) -}}
true
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "envoy-gateway-config.responseHeaderFilter" -}}
filters:
  - type: ResponseHeaderModifier
    responseHeaderModifier:
      set:
        {{- range . }}
        - name: {{ .name }}
          value: {{ .value | quote }}
        {{- end }}
{{- end -}}

{{- define "envoy-gateway-config.notFoundFilter" -}}
{{ .Values.gateway.name }}-not-found
{{- end -}}

{{- /* A backend path the gateway must not publish answers 404 here, before the
       backend sees it: the longer PathPrefix wins over the route's own. Takes
       (dict "ctx" $ "key" <route key> "indent" <n>), and indents itself like
       responseHeaders. */ -}}
{{- define "envoy-gateway-config.hiddenRule" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- if $r.hiddenPaths -}}
{{- include "envoy-gateway-config.hiddenRuleBody" (dict "paths" $r.hiddenPaths "filter" (include "envoy-gateway-config.notFoundFilter" .ctx)) | nindent (int .indent) -}}
{{- end -}}
{{- end -}}

{{- define "envoy-gateway-config.hiddenRuleBody" -}}
- matches:
    {{- range .paths }}
    - path:
        type: PathPrefix
        value: {{ . }}
    {{- end }}
  filters:
    - type: ExtensionRef
      extensionRef:
        group: gateway.envoyproxy.io
        kind: HTTPRouteFilter
        name: {{ .filter }}
{{- end -}}

{{- /* An ExtensionRef resolves in the route's own namespace, so the filter is
       rendered once in each namespace a hiding route lives in. */ -}}
{{- define "envoy-gateway-config.hiddenNamespaces" -}}
{{- $namespaces := list -}}
{{- range $key, $r := .ctx.Values.routes -}}
{{- if and $r.hiddenPaths (include "envoy-gateway-config.routeEnabled" (dict "ctx" $.ctx "key" $key)) -}}
{{- $namespaces = append $namespaces (include "envoy-gateway-config.routeNs" (dict "ctx" $.ctx "key" $key)) -}}
{{- end -}}
{{- end -}}
{{- $namespaces | uniq | sortAlpha | toJson -}}
{{- end -}}

{{- /* The same three facts security-policy-infra.yaml renders on, so a route
       admitted here always has its policy. */ -}}
{{- define "envoy-gateway-config.edgeLogin" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- if and .ctx.Values.oidc.enabled .ctx.Values.oidc.providers $r.edgePolicy -}}
true
{{- end -}}
{{- end -}}

{{- define "envoy-gateway-config.routeEnabled" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- $class := $r.class | default "infra" -}}
{{- if and (eq $class "infra") (not .ctx.Values.exposure.infraUisExternal) -}}
{{- else if and (eq $class "infra") (not $r.ownLogin) (not (include "envoy-gateway-config.edgeLogin" .)) -}}
{{- /* A backend with no login of its own is never published bare. */ -}}
{{- else if and (eq .key "cruiseControl") (not (include "envoy-gateway-config.cruiseControlUi" (dict "ctx" .ctx))) -}}
{{- /* Without this a deployment with no rebalancer publishes a hostname whose
       Service never exists, and the route reports BackendNotFound for the life
       of the deployment. */ -}}
{{- else if and (eq .key "forgejo") (not .ctx.Values.deployRepo.bundled) -}}
{{- /* Forgejo is deployed only when the deploy repo is the bundled one. */ -}}
{{- else if and (eq .key "otel") (not (include "envoy-gateway-config.otelIngress" (dict "ctx" .ctx))) -}}
{{- /* Without the switch the collector has no authenticated receiver, and the
       only port left to route to takes OTLP from anyone. */ -}}
{{- else if $r.enabled -}}
true
{{- end -}}
{{- end -}}

{{- define "envoy-gateway-config.routeHost" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- $host := $r.hostname | default (index .ctx.Values.hostnames $r.hostnameKey) -}}
{{- if not $host -}}
{{- fail (printf "routes.%s has no hostname: set routes.%s.hostname, or add hostnames.%s to the deploy-config SSoT (argocd/values/common.yaml) -- an empty subdomain label renders a hostname starting with a literal dot, which ACME and DNS both reject" .key .key $r.hostnameKey) -}}
{{- end -}}
{{- $host -}}
{{- end -}}

{{- define "envoy-gateway-config.routeNs" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- $r.backendNamespace | default .ctx.Values.appNamespace | default .ctx.Release.Namespace -}}
{{- end -}}

{{/*
Public-exposure helpers. Same dict argument as above.

uiRouteKey    -- the routes.<key> a ui.public.<name> flag names; "" when the
  chart has no route for that name.
publicRoutes  -- JSON array of the route keys that answer on a public hostname,
  so the listener, the certificate, the route, the policies and the rate limit
  cannot disagree about which UIs are exposed. Empty while ui.public_domain is.
publicListenerRoutes -- the subset of those that own a listener and a
  certificate. A route carrying publicListenerOf answers on another route's, so
  it appears in publicRoutes and not here.
publicListenerOwner -- which route key supplies a public route's listener,
  certificate and hostname: itself, or the one publicListenerOf names.
publicHost    -- the fully qualified public hostname of a route, taken from its
  listener owner so the listener, the certificate and the route share one name.
publicPaths   -- JSON array of the PathPrefix values a public route matches.
cidrList      -- a comma-separated dial scalar as a YAML list of trimmed entries.
webFence      -- who the web routes admit: "deny" (internet-facing, no list),
  "listed", "allow-all" (an entry is a /0), or "" (not internet-facing, no list).
fenceAuthorization -- a SecurityPolicy authorization block that denies by
  default and allows .cidrs; takes (dict "cidrs" <list> "indent" <n>).
ingestRoutes  -- JSON array of the ingest-class route keys that render.
validateUi    -- the render guards; templates/validate.yaml runs them.
*/}}

{{- /* hasKey first, deliberately: a missing key makes `index` yield "<no
       value>", which Helm strips from the OUTPUT but which is a non-empty,
       truthy string inside the template -- so testing the index result alone
       would let an unknown UI name through the guard below. */}}
{{- define "envoy-gateway-config.uiRouteKey" -}}
{{- $map := dict
      "dfe_ui" "dfeUi"
      "kafbat" "kafbat"
      "cruise_control" "cruiseControl"
      "hyperdx" "hyperdx"
      "argocd" "argocd"
      "links" "links"
      "forgejo" "forgejo" -}}
{{- if hasKey $map .name -}}
{{- index $map .name -}}
{{- end -}}
{{- end -}}

{{- define "envoy-gateway-config.cidrList" -}}
{{- $out := list -}}
{{- range splitList "," (.value | default "") -}}
{{- if trim . -}}
{{- $out = append $out (trim .) -}}
{{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{- /* A /0 of either family is every address, whatever the address part says. */ -}}
{{- define "envoy-gateway-config.webFence" -}}
{{- $allowed := include "envoy-gateway-config.cidrList" (dict "value" .ctx.Values.ui.allowed_cidrs) | fromJsonArray -}}
{{- if $allowed -}}
{{- $state := "listed" -}}
{{- range $allowed -}}
{{- if hasSuffix "/0" . -}}
{{- $state = "allow-all" -}}
{{- end -}}
{{- end -}}
{{- $state -}}
{{- else if .ctx.Values.envoyGateway.service.internetFacing -}}
deny
{{- end -}}
{{- end -}}

{{- /* Envoy Gateway's own CIDR alternatives (shared_types.go at v1.9.2),
       anchored, with the prefix length bounded by the family. Its zone-indexed
       fe80 form is left out: the Service API refuses a zone in a source range. */ -}}
{{- define "envoy-gateway-config.cidrValid" -}}
{{- $v4 := `^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)/([0-9]|[12][0-9]|3[0-2])$` -}}
{{- $v6 := `^(([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,7}:|([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})|:((:[0-9a-fA-F]{1,4}){1,7}|:)|::(ffff(:0{1,4})?:)?((25[0-5]|(2[0-4]|1?[0-9])?[0-9])\.){3}(25[0-5]|(2[0-4]|1?[0-9])?[0-9])|([0-9a-fA-F]{1,4}:){1,4}:((25[0-5]|(2[0-4]|1?[0-9])?[0-9])\.){3}(25[0-5]|(2[0-4]|1?[0-9])?[0-9]))/([0-9]|[1-9][0-9]|1[01][0-9]|12[0-8])$` -}}
{{- if or (regexMatch $v4 .) (regexMatch $v6 .) -}}
true
{{- end -}}
{{- end -}}

{{- /* No rules with an empty list, so the policy refuses every request. */ -}}
{{- define "envoy-gateway-config.fenceAuthorization" -}}
{{- $block := dict "defaultAction" "Deny" -}}
{{- if .cidrs -}}
{{- $_ := set $block "rules" (list (dict "name" "allow-listed-cidrs" "action" "Allow" "principal" (dict "clientCIDRs" .cidrs))) -}}
{{- end -}}
{{- dict "authorization" $block | toYaml | nindent (int .indent) -}}
{{- end -}}

{{- define "envoy-gateway-config.ingestRoutes" -}}
{{- $keys := list -}}
{{- range $key, $r := .ctx.Values.routes -}}
{{- if and (eq ($r.class | default "infra") "ingest") (include "envoy-gateway-config.routeEnabled" (dict "ctx" $.ctx "key" $key)) -}}
{{- $keys = append $keys $key -}}
{{- end -}}
{{- end -}}
{{- $keys | sortAlpha | toJson -}}
{{- end -}}

{{- define "envoy-gateway-config.publicRoutes" -}}
{{- $keys := list -}}
{{- if .ctx.Values.ui.public_domain -}}
{{- range $name, $public := .ctx.Values.ui.public -}}
{{- if $public -}}
{{- $key := include "envoy-gateway-config.uiRouteKey" (dict "name" $name) -}}
{{- if not $key -}}
{{- fail (printf "ui.public.%s is true and this chart carries no route for it -- every name must map to a routes.<key> entry" $name) -}}
{{- end -}}
{{- /* The infra kill switch is absolute for its class, so a route it has
       already taken off the edge is skipped here rather than forced back on. */}}
{{- if include "envoy-gateway-config.routeEnabled" (dict "ctx" $.ctx "key" $key) -}}
{{- $keys = append $keys $key -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- /* The engine API rides dfe-ui's hostname rather than a flag of its own,
       because the browser calls it at window.location.origin -- so it is public
       exactly when dfe-ui is, and ui.engine_api.with_product is the opt-out. */}}
{{- if and (has "dfeUi" $keys) .ctx.Values.ui.engine_api.with_product -}}
{{- if include "envoy-gateway-config.routeEnabled" (dict "ctx" .ctx "key" "dfeEngine") -}}
{{- $keys = append $keys "dfeEngine" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $keys | sortAlpha | toJson -}}
{{- end -}}

{{- define "envoy-gateway-config.publicListenerRoutes" -}}
{{- $keys := list -}}
{{- range $key := include "envoy-gateway-config.publicRoutes" (dict "ctx" .ctx) | fromJsonArray -}}
{{- if not (index $.ctx.Values.routes $key).publicListenerOf -}}
{{- $keys = append $keys $key -}}
{{- end -}}
{{- end -}}
{{- $keys | toJson -}}
{{- end -}}

{{- define "envoy-gateway-config.publicListenerOwner" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- $r.publicListenerOf | default .key -}}
{{- end -}}

{{- define "envoy-gateway-config.publicHost" -}}
{{- $owner := include "envoy-gateway-config.publicListenerOwner" . -}}
{{- include "envoy-gateway-config.routeHost" (dict "ctx" .ctx "key" $owner) -}}.{{ .ctx.Values.ui.public_domain -}}
{{- end -}}

{{- /* A route carrying browserFamilies is matched family by family; every other
       one answers on the single pathPrefix it already declares. One rule carries
       the lot, within the 64 the HTTPRoute CRD caps rules[].matches at (Gateway
       API v1.6.1, as bundled by envoy-gateway v1.9.1); validateUi checks it. */}}
{{- define "envoy-gateway-config.publicPaths" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- $paths := list -}}
{{- if $r.browserFamilies -}}
{{- $engine := .ctx.Values.ui.engine_api -}}
{{- range $family := $r.browserFamilies -}}
{{- $paths = append $paths (printf "%s/%s" $r.pathPrefix $family) -}}
{{- end -}}
{{- $paths = concat $paths ($r.browserPaths | default list) -}}
{{- if $engine.cli_families_public -}}
{{- range $family := $r.privateFamilies -}}
{{- $paths = append $paths (printf "%s/%s" $r.pathPrefix $family) -}}
{{- end -}}
{{- $paths = concat $paths ($r.privatePaths | default list) -}}
{{- end -}}
{{- if $engine.scim_public -}}
{{- $paths = append $paths $r.scimPrefix -}}
{{- end -}}
{{- else if $r.pathPrefix -}}
{{- $paths = append $paths $r.pathPrefix -}}
{{- end -}}
{{- $paths | toJson -}}
{{- end -}}

{{- define "envoy-gateway-config.validateUi" -}}
{{- $ui := .ctx.Values.ui -}}

{{- /* Every edge policy admits exactly these groups, so none is refused rather
       than rendered into a policy whose groups claim names no group. */ -}}
{{- $groups := .ctx.Values.adminGroups -}}
{{- if not (and (kindIs "slice" $groups) $groups) -}}
{{- fail "adminGroups is empty or missing -- it names the OIDC groups the edge policy admits to every admin UI, and argocd/values/common.yaml sets it. Give it a list of group names; one deployment changes it in its deploy repo's infra/common.yaml" -}}
{{- end -}}
{{- /* An overlay still naming the retired key would otherwise be ignored without
       a word, and the deployment would admit the default groups instead. */ -}}
{{- if hasKey .ctx.Values.oidc "adminGroups" -}}
{{- fail "oidc.adminGroups is retired -- the admin groups are the top-level adminGroups, which the gateway and Kafbat both read (argocd/values/common.yaml). Move the list there, in the deploy repo's infra/common.yaml" -}}
{{- end -}}

{{- /* Every admin UI on a public load balancer with no edge auth and no CIDR
       fence is the exact misconfiguration a cloud overlay can reintroduce by
       flipping exposure.infraUisExternal back on -- checked here, not only in
       argocd/values/aws.yaml's default, because a values overlay can undo that
       default without ever touching this file. envoyGateway.service.
       internetFacing is the chart's own cloud-agnostic signal (see
       values.yaml); it says nothing about ui.public_domain, so this fires
       whether or not any UI is ALSO published on its own public hostname. */ -}}
{{- $facing := .ctx.Values.envoyGateway.service.internetFacing -}}
{{- if not (kindIs "bool" $facing) -}}
{{- fail (printf "envoyGateway.service.internetFacing is %v (a %s), not a bool -- it decides whether every web route is fenced to ui.allowed_cidrs, and a quoted \"false\" is truthy. Write true or false unquoted" $facing (kindOf $facing)) -}}
{{- end -}}
{{- if and $facing .ctx.Values.exposure.infraUisExternal -}}
{{- /* oidc.enabled with no provider renders no edge policy at all. */ -}}
{{- if and (not (and .ctx.Values.oidc.enabled .ctx.Values.oidc.providers)) (not $ui.allowed_cidrs) -}}
{{- fail "envoyGateway.service.internetFacing is true and exposure.infraUisExternal is true, with no edge OIDC provider (oidc.enabled and an oidc.providers entry) and ui.allowed_cidrs empty -- every admin UI with a login of its own (argocd, kafbat, forgejo) would render on a public load balancer with no edge authentication and no CIDR fence. Set oidc.enabled: true with an oidc.providers entry, set ui.allowed_cidrs (with ui.trusted_proxy_cidrs), or leave exposure.infraUisExternal: false" -}}
{{- end -}}
{{- end -}}

{{- /* ui.public.* is documented (deployment.example.yaml) to copy verbatim
       into this chart's values, where an unquoted true/false is a native YAML
       bool and a quoted "true"/"false" unmarshals as a Go string -- non-empty,
       so always truthy to the `if $public` test in publicRoutes above.
       Refused rather than coerced: a string here is more likely a stale
       copy-paste from the dial's own (string-typed) format than a deliberate
       choice, and coercing it would hide that a UI meant to stay private was
       about to publish anyway. */ -}}
{{- range $name, $val := $ui.public -}}
{{- if not (kindIs "bool" $val) -}}
{{- fail (printf "ui.public.%s is %v (a %s), not a bool -- this chart's ui.public is the dial's edge.admin_uis.public (and ui.public.dfe_ui is edge.product.public); copy those booleans across unquoted (true/false), not \"true\"/\"false\": a quoted string is truthy here no matter what it says" $name $val (kindOf $val)) -}}
{{- end -}}
{{- end -}}
{{- if not (kindIs "bool" $ui.rate_limit.enabled) -}}
{{- fail (printf "ui.rate_limit.enabled is %v (a %s), not a bool -- see the ui.public.* refusal above for why this is refused rather than coerced" $ui.rate_limit.enabled (kindOf $ui.rate_limit.enabled)) -}}
{{- end -}}
{{- if not (kindIs "bool" $ui.tls.hsts) -}}
{{- fail (printf "ui.tls.hsts is %v (a %s), not a bool -- see the ui.public.* refusal above" $ui.tls.hsts (kindOf $ui.tls.hsts)) -}}
{{- end -}}
{{- /* Empty or null takes edgeHsts's derived default; any other non-bool is refused. */ -}}
{{- $edgeHsts := dig "edge" "hsts" "" .ctx.Values.tls -}}
{{- if not (or (kindIs "bool" $edgeHsts) (kindIs "invalid" $edgeHsts) (eq (toString $edgeHsts) "")) -}}
{{- fail (printf "tls.edge.hsts is %v (a %s), not a bool or empty -- empty follows the edge issuer; see the ui.public.* refusal above" $edgeHsts (kindOf $edgeHsts)) -}}
{{- end -}}
{{- /* A quoted "false" is truthy, so it would publish a login-less backend bare. */ -}}
{{- range $key, $r := .ctx.Values.routes -}}
{{- if and (hasKey $r "ownLogin") (not (kindIs "bool" $r.ownLogin)) -}}
{{- fail (printf "routes.%s.ownLogin is %v (a %s), not a bool -- see the ui.public.* refusal above" $key $r.ownLogin (kindOf $r.ownLogin)) -}}
{{- end -}}
{{- end -}}
{{- if not (kindIs "bool" .ctx.Values.deployRepo.bundled) -}}
{{- fail (printf "deployRepo.bundled is %v (a %s), not a bool -- see the ui.public.* refusal above" .ctx.Values.deployRepo.bundled (kindOf .ctx.Values.deployRepo.bundled)) -}}
{{- end -}}

{{- /* A WAF terminates TLS above Envoy, so the public certificate moves to the
       cloud's own store and the Certificates below stop being what a browser
       sees. Nothing here renders that shape, so it refuses rather than
       rendering a certificate the edge will not present. */ -}}
{{- if ne $ui.waf.mode "none" -}}
{{- fail (printf "ui.waf.mode is %q and only \"none\" is landed here -- a WAF terminates TLS above Envoy, which moves the public certificate to the cloud's certificate store (us-east-1 for CloudFront) and makes this chart's Let's Encrypt issuer dead weight. That edge is a separate change; set ui.waf.mode: none" $ui.waf.mode) -}}
{{- end -}}

{{- if $ui.rate_limit.enabled -}}
{{- if eq $ui.rate_limit.scope "global" -}}
{{- fail "ui.rate_limit.scope: global counts per attacker across every proxy replica, and Envoy Gateway does that only with a Redis backend named in the EnvoyGateway install config -- neither of which this chart installs. Use scope: local, and read its limit as per route per replica" -}}
{{- else if ne $ui.rate_limit.scope "local" -}}
{{- fail (printf "ui.rate_limit.scope is %q -- the only scope this chart renders is local" $ui.rate_limit.scope) -}}
{{- end -}}
{{- end -}}

{{- /* The Cruise Control UI is a workload of the KAFKA chart, so a flag here
       cannot conjure it. Silently dropping the route would publish the intent
       and nothing else, which is the failure a deployer finds months later. */ -}}
{{- $wantsCruiseControl := false -}}
{{- with $ui.public -}}{{- $wantsCruiseControl = .cruise_control -}}{{- end -}}
{{- if and $wantsCruiseControl (not (include "envoy-gateway-config.cruiseControlUi" (dict "ctx" .ctx))) -}}
{{- fail "ui.public.cruise_control is true and the kafka chart is not deploying that UI -- it needs kafka.provider strimzi, kafka.mode cluster, kafka.rebalancing.enabled and kafka.rebalancing.ui.enabled, all of which come from the same deploy-config values both charts read. Set them, or leave this flag false" -}}
{{- end -}}

{{- /* routeEnabled withholds a login-less route with no edge login, and a public
       flag on it would otherwise be dropped without a word. */ -}}
{{- if and $ui.public_domain .ctx.Values.exposure.infraUisExternal -}}
{{- range $name, $public := $ui.public -}}
{{- $key := include "envoy-gateway-config.uiRouteKey" (dict "name" $name) -}}
{{- if and $public $key -}}
{{- $r := index $.ctx.Values.routes $key -}}
{{- if and (eq ($r.class | default "infra") "infra") $r.enabled (not $r.ownLogin) (not (include "envoy-gateway-config.edgeLogin" (dict "ctx" $.ctx "key" $key))) -}}
{{- fail (printf "ui.public.%s is true and routes.%s has no login of its own, so it renders only behind the edge login -- set oidc.enabled with an oidc.providers entry and keep routes.%s.edgePolicy true, or leave this flag false" $name $key $key) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- /* Envoy infers the client IP from X-Forwarded-For. With no trusted proxy
       range it believes the leftmost entry, which the caller writes, so the
       CIDR filter admits anyone who sends the right header. */ -}}
{{- $allowed := include "envoy-gateway-config.cidrList" (dict "value" $ui.allowed_cidrs) | fromJsonArray -}}
{{- $trusted := include "envoy-gateway-config.cidrList" (dict "value" $ui.trusted_proxy_cidrs) | fromJsonArray -}}
{{- /* Envoy Gateway's CIDR type (api/v1alpha1/shared_types.go at v1.9.2) and
       the Service's loadBalancerSourceRanges both refuse an entry with no
       prefix length, but only at admission, where it surfaces as an Argo
       SyncFailed on the whole edge. Same shape, anchored, checked here. */ -}}
{{- range $field, $list := dict "ui.allowed_cidrs" $allowed "ui.trusted_proxy_cidrs" $trusted -}}
{{- range $entry := $list -}}
{{- if not (include "envoy-gateway-config.cidrValid" $entry) -}}
{{- fail (printf "%s carries %q, which is not a CIDR range -- every entry needs an address and a prefix length, such as 203.0.113.7/32 or 2001:db8::/64. Envoy Gateway and the load balancer refuse it at admission, which fails the whole gateway sync" $field $entry) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and $allowed (not $trusted) -}}
{{- fail "ui.allowed_cidrs is set and ui.trusted_proxy_cidrs is empty -- Envoy would take the client address from the leftmost X-Forwarded-For entry, which the caller writes, so the filter would admit anyone who sends the right header. Name the load balancer's subnet CIDRs (and any CDN in front of it)" -}}
{{- end -}}

{{- /* Authentication on a public route is edge OIDC, or one of the three
       app-side schemes that were read in the code and are admitted by name.
       Anything else is an unauthenticated public UI. */ -}}
{{- $appSide := dict
      "dfeUi" "dfe-ui authenticates app-side through NextAuth"
      "dfeEngine" "the engine authenticates every call itself, from a DFE token or an API key"
      "kafbat" "kafbat runs its own OIDC against the same provider"
      "hyperdx" "hyperdx authenticates from the dfe_token cookie, because an OIDC redirect cannot complete inside the dfe-ui iframe" -}}
{{- $edgeRoutes := list -}}
{{- range $route := .ctx.Values.oidc.targetRoutes -}}
{{- $edgeRoutes = append $edgeRoutes $route.name -}}
{{- end -}}
{{- range $key := include "envoy-gateway-config.publicRoutes" (dict "ctx" .ctx) | fromJsonArray -}}
{{- $r := index $.ctx.Values.routes $key -}}
{{- $hasProviders := and $.ctx.Values.oidc.enabled $.ctx.Values.oidc.providers -}}
{{- $edge := and $hasProviders (or $r.edgePolicy (has $r.routeName $edgeRoutes)) -}}
{{- if not (or $edge (hasKey $appSide $key)) -}}
{{- fail (printf "route %s is public and carries no authentication -- it takes no edge OIDC policy (routes.%s.edgePolicy, or oidc.targetRoutes naming it, with oidc.enabled and a provider set) and only dfe-ui, the engine API, kafbat and hyperdx are admitted on app-side auth. A public UI with no login is refused" $r.routeName $key) -}}
{{- end -}}
{{- /* One rule carries every match, and the CRD rejects the object above 64
       rather than truncating it, so the count is checked before it is written. */}}
{{- $paths := include "envoy-gateway-config.publicPaths" (dict "ctx" $.ctx "key" $key) | fromJsonArray -}}
{{- if gt (len $paths) 64 -}}
{{- fail (printf "route %s matches %d paths on its public route and the Gateway API HTTPRoute CRD caps rules[].matches at 64 -- split routes.%s's path lists across several rules carrying the same backendRef and the same filters" $r.routeName (len $paths) $key) -}}
{{- end -}}
{{- end -}}
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
