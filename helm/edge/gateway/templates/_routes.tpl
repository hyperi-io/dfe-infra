{{/*
Route exposure and classification helpers. Local to this chart: classification
is an edge concern, and dfe-common is shared by every DFE chart.

Each takes (dict "ctx" $ "key" "<routes values key>").

routeEnabled -- does this route render? First match wins:
  1. its exposure group off                          -> "" (absolute for the
     group, so a route's own enabled:true does not beat it)
  2. class infra, ownLogin false, and no edgeLogin    -> ""
  3. the backend is another chart's opt-in workload that the deployment is not
     running, or the receiver's door is not this gateway -> ""
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
validateReceiver -- refuses the receiver route on an internet-facing Service
  unless routes.receiver.authMode names a mode that checks every caller.
ingestDoor   -- the receiver's door, exposure.groups.ingest.door, or "" while
  the ingest group is off.
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

Exposure-group helpers:
routeGroup   -- the exposure group a route's class puts it in.
groupFence   -- JSON {state, open, allow, deny} for one group: state is off,
  open (no fence), deny (internet-facing, no list), listed or allow-all; allow
  carries selfRanges and is empty while open. Takes (dict "ctx" $ "group" <g>).
groupAuthorization -- the CIDR-only `authorization:` block a group's routes
  carry, deny rules ahead of the allow rule, or nothing for an open group with
  no deny list. Takes (dict "ctx" $ "group" <g> "indent" <n>).
groupSummary -- one line naming every web group's state, for annotations.
groupRanges  -- JSON {console, api, admin, selfRanges}: the CIDRs each web
  group's policies admit, and exposure.selfRanges, for annotations.
lbSourceRanges -- JSON array of the load balancer's source ranges.
oidcFronted  -- JSON array of "<ns>/<route>" an OIDC SecurityPolicy targets, so
  no second policy lands on the same route.
webRouteKeys -- JSON array of the rendered route keys in a web group.
fencedRouteKeys -- webRouteKeys and then the rendered ingest routes: every route
  that carries its own exposure group's CIDR policy.
refuseRetired -- the retired exposure keys, refused by name.
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

{{- /* The receiver checks its own callers by config.server.auth.mode in its own
       values, which this chart cannot read, so routes.receiver.authMode restates
       it. Envoy terminates TLS, so only header and bearer authenticate a caller
       arriving through this route. */ -}}
{{- define "envoy-gateway-config.validateReceiver" -}}
{{- $mode := lower (toString (.ctx.Values.routes.receiver.authMode | default "none")) -}}
{{- if not (has $mode (list "none" "header" "bearer" "mtls" "both")) -}}
{{- fail (printf "routes.receiver.authMode is %q -- it restates dfe-receiver's config.server.auth.mode, which is none, header, bearer, mtls or both" $mode) -}}
{{- end -}}
{{- if and .ctx.Values.envoyGateway.service.internetFacing (include "envoy-gateway-config.routeEnabled" (dict "ctx" .ctx "key" "receiver")) (not (has $mode (list "header" "bearer"))) -}}
{{- fail (printf "exposure.groups.ingest.door is gateway on an internet-facing Service and routes.receiver.authMode is %q -- the route would take ingest from every address exposure.groups.ingest.allow names, with no check on who sends it. Set the receiver's own config.server.auth.mode to header or bearer and restate it here as routes.receiver.authMode (mtls and both need the client certificate, which Envoy terminates), or set exposure.groups.ingest.door: loadbalancer to give the receiver its own load balancer" $mode) -}}
{{- end -}}
{{- end -}}

{{- define "envoy-gateway-config.ingestDoor" -}}
{{- $ingest := index (.ctx.Values.exposure.groups | default dict) "ingest" | default dict -}}
{{- if $ingest.enabled -}}
{{- toString $ingest.door -}}
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
{{- $group := include "envoy-gateway-config.routeGroup" . -}}
{{- $spec := index (.ctx.Values.exposure.groups | default dict) $group | default dict -}}
{{- if not $spec.enabled -}}
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
{{- else if and (eq .key "receiver") (ne (include "envoy-gateway-config.ingestDoor" (dict "ctx" .ctx)) "gateway") -}}
{{- /* Any other door is the receiver's own Service or none, and this route
       would be a second door around it. */ -}}
{{- else if $r.enabled -}}
true
{{- end -}}
{{- end -}}

{{- define "envoy-gateway-config.routeGroup" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- $class := $r.class | default "infra" -}}
{{- $groups := dict "product" "console" "api" "api" "infra" "admin" "ingest" "ingest" -}}
{{- if not (hasKey $groups $class) -}}
{{- fail (printf "routes.%s.class is %q -- it takes product, api, infra or ingest, which put the route in the console, api, admin or ingest exposure group" .key $class) -}}
{{- end -}}
{{- index $groups $class -}}
{{- end -}}

{{- /* Open admits every address and so carries no allow list; the deployer's
       list decides openness, never selfRanges. */ -}}
{{- define "envoy-gateway-config.groupFence" -}}
{{- $spec := index (.ctx.Values.exposure.groups | default dict) .group | default dict -}}
{{- $facing := .ctx.Values.envoyGateway.service.internetFacing -}}
{{- $listed := $spec.allow | default list -}}
{{- $state := "open" -}}
{{- $open := true -}}
{{- if not $spec.enabled -}}
{{- $state = "off" -}}
{{- $open = false -}}
{{- else if $listed -}}
{{- $state = "listed" -}}
{{- $open = false -}}
{{- range $listed -}}
{{- if hasSuffix "/0" (toString .) -}}
{{- $state = "allow-all" -}}
{{- end -}}
{{- end -}}
{{- else if and $facing $spec.allowPublic -}}
{{- $state = "allow-all" -}}
{{- else if $facing -}}
{{- $state = "deny" -}}
{{- $open = false -}}
{{- end -}}
{{- $allow := list -}}
{{- if not $open -}}
{{- $allow = concat $listed (.ctx.Values.exposure.selfRanges | default list) | uniq -}}
{{- end -}}
{{- dict "state" $state "open" $open "allow" $allow "deny" ($spec.deny | default list) | toJson -}}
{{- end -}}

{{- /* Envoy Gateway evaluates rules in order and the first match decides, so
       the deny rule goes first (api/v1alpha1/authorization_types.go at v1.9.2). */ -}}
{{- define "envoy-gateway-config.groupAuthorization" -}}
{{- $f := include "envoy-gateway-config.groupFence" . | fromJson -}}
{{- $rules := list -}}
{{- if $f.deny -}}
{{- $rules = append $rules (dict "name" "deny-listed-cidrs" "action" "Deny" "principal" (dict "clientCIDRs" $f.deny)) -}}
{{- end -}}
{{- if $f.open -}}
{{- /* On an internet-facing gateway the Gateway-level fence refuses every
       address, so an open route has to say Allow rather than say nothing. */ -}}
{{- if or $rules .ctx.Values.envoyGateway.service.internetFacing -}}
{{- $block := dict "defaultAction" "Allow" -}}
{{- if $rules -}}
{{- $_ := set $block "rules" $rules -}}
{{- end -}}
{{- dict "authorization" $block | toYaml | nindent (int .indent) -}}
{{- end -}}
{{- else -}}
{{- if $f.allow -}}
{{- $rules = append $rules (dict "name" "allow-listed-cidrs" "action" "Allow" "principal" (dict "clientCIDRs" $f.allow)) -}}
{{- end -}}
{{- $block := dict "defaultAction" "Deny" -}}
{{- if $rules -}}
{{- $_ := set $block "rules" $rules -}}
{{- end -}}
{{- dict "authorization" $block | toYaml | nindent (int .indent) -}}
{{- end -}}
{{- end -}}

{{- define "envoy-gateway-config.groupSummary" -}}
{{- $parts := list -}}
{{- range $g := list "console" "api" "admin" -}}
{{- $f := include "envoy-gateway-config.groupFence" (dict "ctx" $.ctx "group" $g) | fromJson -}}
{{- $parts = append $parts (printf "%s=%s" $g $f.state) -}}
{{- end -}}
{{- join "," $parts -}}
{{- end -}}

{{- /* An off group renders no route, so it admits no address at all. */ -}}
{{- define "envoy-gateway-config.groupRanges" -}}
{{- $ranges := dict "selfRanges" (.ctx.Values.exposure.selfRanges | default list) -}}
{{- range $g := list "console" "api" "admin" -}}
{{- $f := include "envoy-gateway-config.groupFence" (dict "ctx" $.ctx "group" $g) | fromJson -}}
{{- $_ := set $ranges $g (ternary (list) $f.allow (eq $f.state "off")) -}}
{{- end -}}
{{- toJson $ranges -}}
{{- end -}}

{{- /* One Service carries every listener, so the load balancer admits the union
       of the web groups, and of the ingest group while an ingest route renders.
       An open group needs every address at L4, so the groups add nothing then
       and only an explicit envoyGateway.service.loadBalancerSourceRanges
       narrows the door. */ -}}
{{- define "envoy-gateway-config.lbSourceRanges" -}}
{{- $svc := .ctx.Values.envoyGateway.service.loadBalancerSourceRanges | default list -}}
{{- $web := list -}}
{{- $open := false -}}
{{- $groups := list "console" "api" "admin" -}}
{{- if include "envoy-gateway-config.ingestRoutes" (dict "ctx" .ctx) | fromJsonArray -}}
{{- $groups = append $groups "ingest" -}}
{{- end -}}
{{- range $g := $groups -}}
{{- $f := include "envoy-gateway-config.groupFence" (dict "ctx" $.ctx "group" $g) | fromJson -}}
{{- if ne $f.state "off" -}}
{{- if $f.open -}}
{{- $open = true -}}
{{- else -}}
{{- $web = concat $web $f.allow -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $ranges := list -}}
{{- if or $svc (not $open) -}}
{{- $ranges = concat $svc $web | uniq -}}
{{- end -}}
{{- $ranges | toJson -}}
{{- end -}}

{{- define "envoy-gateway-config.oidcFronted" -}}
{{- $out := list -}}
{{- if and .ctx.Values.oidc.enabled .ctx.Values.oidc.providers -}}
{{- range $route := .ctx.Values.oidc.targetRoutes -}}
{{- $ns := $route.namespace | default $.ctx.Values.appNamespace | default $.ctx.Release.Namespace -}}
{{- $out = append $out (printf "%s/%s" $ns $route.name) -}}
{{- end -}}
{{- range $key := include "envoy-gateway-config.infraPolicyRoutes" (dict "ctx" .ctx) | fromJsonArray -}}
{{- $ns := include "envoy-gateway-config.routeNs" (dict "ctx" $.ctx "key" $key) -}}
{{- $out = append $out (printf "%s/%s" $ns (index $.ctx.Values.routes $key).routeName) -}}
{{- end -}}
{{- end -}}
{{- $out | uniq | toJson -}}
{{- end -}}

{{- define "envoy-gateway-config.webRouteKeys" -}}
{{- $keys := list -}}
{{- range $key, $r := .ctx.Values.routes -}}
{{- if and (ne ($r.class | default "infra") "ingest") (include "envoy-gateway-config.routeEnabled" (dict "ctx" $.ctx "key" $key)) -}}
{{- $keys = append $keys $key -}}
{{- end -}}
{{- end -}}
{{- $keys | sortAlpha | toJson -}}
{{- end -}}

{{- define "envoy-gateway-config.fencedRouteKeys" -}}
{{- $web := include "envoy-gateway-config.webRouteKeys" (dict "ctx" .ctx) | fromJsonArray -}}
{{- $ingest := include "envoy-gateway-config.ingestRoutes" (dict "ctx" .ctx) | fromJsonArray -}}
{{- concat $web $ingest | toJson -}}
{{- end -}}

{{- /* Local rate-limit rules: a bucket per client address in each family, and with
       shared, one bucket every caller spends from. Envoy Gateway v1.9.2 never
       spends the selector-less default bucket once another rule matches
       (internal/xds/translator/local_ratelimit.go), so the shared wall is a rule
       whose path selector every request matches. */ -}}
{{- define "envoy-gateway-config.rateLimitRules" -}}
{{- $rl := .ctx.Values.ui.rate_limit -}}
{{- $rules := list -}}
{{- if .shared -}}
{{- $rules = append $rules (dict "clientSelectors" (list (dict "path" (dict "type" "PathPrefix" "value" "/"))) "limit" (dict "requests" (int64 $rl.requests) "unit" $rl.unit)) -}}
{{- end -}}
{{- range $family := list "0.0.0.0/0" "::/0" -}}
{{- $rules = append $rules (dict "clientSelectors" (list (dict "sourceCIDR" (dict "type" "Distinct" "value" $family))) "limit" (dict "requests" (int64 $rl.per_client_requests) "unit" $rl.unit)) -}}
{{- end -}}
{{- toYaml $rules -}}
{{- end -}}

{{- /* The group a targetRoutes name belongs to: its chart route's, or console
       for a route this chart does not render. */ -}}
{{- define "envoy-gateway-config.groupOfRouteName" -}}
{{- $group := "console" -}}
{{- range $key, $r := .ctx.Values.routes -}}
{{- if eq $r.routeName $.name -}}
{{- $group = include "envoy-gateway-config.routeGroup" (dict "ctx" $.ctx "key" $key) -}}
{{- end -}}
{{- end -}}
{{- $group -}}
{{- end -}}

{{- /* An overlay still naming a retired key would otherwise be ignored without
       a word and the deployment would get the new key's default instead. */ -}}
{{- define "envoy-gateway-config.refuseRetired" -}}
{{- $ui := .ctx.Values.ui | default dict -}}
{{- if hasKey $ui "allowed_cidrs" -}}
{{- fail "ui.allowed_cidrs is retired -- each exposure group carries its own list: exposure.groups.console.allow (dfe-ui, HyperDX, the engine's browser families), exposure.groups.api.allow (the CLI families and /openapi.json) and exposure.groups.admin.allow (the admin UIs), each a YAML list of CIDRs. Move the ranges there, in the deploy repo's infra/common.yaml" -}}
{{- end -}}
{{- if hasKey (.ctx.Values.exposure | default dict) "infraUisExternal" -}}
{{- fail "exposure.infraUisExternal is retired -- the admin UIs' switch is exposure.groups.admin.enabled, which an internet-facing gateway takes only beside exposure.groups.admin.allow or allowPublic. Move it there, in the deploy repo's infra/common.yaml" -}}
{{- end -}}
{{- $retiredAdmin := dict "kafbat" "kafbat" "cruise_control" "cruiseControl" "argocd" "argocd" "links" "links" "forgejo" "forgejo" -}}
{{- range $name, $key := $retiredAdmin -}}
{{- if hasKey ($ui.public | default dict) $name -}}
{{- fail (printf "ui.public.%s is retired -- an admin UI's public hostname is exposure.groups.admin.tools.%s, beside exposure.groups.admin.enabled. Move it there, in the deploy repo's infra/common.yaml" $name $key) -}}
{{- end -}}
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
  chart has no product route for that name.
publicRoutes  -- JSON array of the route keys that answer on a public hostname,
  so the listener, the certificate, the route, the policies and the rate limit
  cannot disagree about which UIs are exposed. Empty while ui.public_domain is.
  Product UIs come from ui.public, admin UIs from exposure.groups.admin.tools,
  and the engine's two routes follow dfe-ui's hostname.
publicListenerRoutes -- the subset of those that own a listener and a
  certificate. A route carrying publicListenerOf answers on another route's, so
  it appears in publicRoutes and not here.
publicListenerOwner -- which route key supplies a public route's listener,
  certificate and hostname: itself, or the one publicListenerOf names.
publicHost    -- the fully qualified public hostname of a route, taken from its
  listener owner so the listener, the certificate and the route share one name.
publicPaths   -- JSON array of the PathPrefix values a public route matches.
enginePaths   -- JSON array of the PathPrefix values one of the engine's two
  routes matches; takes (dict "ctx" $ "key" <route key> "public" <bool>).
cidrList      -- a comma-separated dial scalar as a YAML list of trimmed entries.
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
      "hyperdx" "hyperdx" -}}
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

{{- /* The first helper a render reaches (wildcard-certificate.yaml renders
       before validate.yaml), so the retired keys are refused here too. */ -}}
{{- define "envoy-gateway-config.publicRoutes" -}}
{{- include "envoy-gateway-config.refuseRetired" (dict "ctx" .ctx) -}}
{{- $keys := list -}}
{{- if .ctx.Values.ui.public_domain -}}
{{- range $name, $public := .ctx.Values.ui.public -}}
{{- if $public -}}
{{- $key := include "envoy-gateway-config.uiRouteKey" (dict "name" $name) -}}
{{- if not $key -}}
{{- fail (printf "ui.public.%s is true and this chart carries no product route for it -- ui.public names dfe_ui and hyperdx, and an admin UI's public hostname is exposure.groups.admin.tools.<route key>" $name) -}}
{{- end -}}
{{- /* A group switched off is absolute, so a route it has already taken off
       the edge is skipped here rather than forced back on. */}}
{{- if include "envoy-gateway-config.routeEnabled" (dict "ctx" $.ctx "key" $key) -}}
{{- $keys = append $keys $key -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $admin := index (.ctx.Values.exposure.groups | default dict) "admin" | default dict -}}
{{- range $key, $public := $admin.tools | default dict -}}
{{- if $public -}}
{{- if not (and (hasKey $.ctx.Values.routes $key) (eq ((index $.ctx.Values.routes $key).class | default "infra") "infra")) -}}
{{- fail (printf "exposure.groups.admin.tools.%s is true and this chart carries no admin route of that key -- the tools are the class-infra routes.<key> entries: argocd, kafbat, cruiseControl, forgejo and links" $key) -}}
{{- end -}}
{{- if include "envoy-gateway-config.routeEnabled" (dict "ctx" $.ctx "key" $key) -}}
{{- $keys = append $keys $key -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- /* The engine API rides dfe-ui's hostname rather than a flag of its own,
       because the browser calls it at window.location.origin -- so it is public
       exactly when dfe-ui is, and ui.engine_api.with_product is the opt-out. */}}
{{- $engine := .ctx.Values.ui.engine_api -}}
{{- if and (has "dfeUi" $keys) $engine.with_product -}}
{{- if include "envoy-gateway-config.routeEnabled" (dict "ctx" .ctx "key" "dfeEngine") -}}
{{- $keys = append $keys "dfeEngine" -}}
{{- end -}}
{{- /* A route with no path matches would answer every path on dfe-ui's
       hostname, so the CLI face renders only while it carries one. */}}
{{- if and (include "envoy-gateway-config.publicPaths" (dict "ctx" .ctx "key" "dfeEngineCli") | fromJsonArray) (include "envoy-gateway-config.routeEnabled" (dict "ctx" .ctx "key" "dfeEngineCli")) -}}
{{- $keys = append $keys "dfeEngineCli" -}}
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

{{- /* An engine route is matched family by family; every other one answers on
       the single pathPrefix it already declares. One rule carries the lot,
       within the 64 the HTTPRoute CRD caps rules[].matches at (Gateway API
       v1.6.1, as bundled by envoy-gateway v1.9.1); validateUi checks it. */}}
{{- define "envoy-gateway-config.publicPaths" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- if or $r.browserFamilies $r.cliFamilies -}}
{{- include "envoy-gateway-config.enginePaths" (dict "ctx" .ctx "key" .key "public" true) -}}
{{- else if $r.pathPrefix -}}
{{- list $r.pathPrefix | toJson -}}
{{- else -}}
{{- list | toJson -}}
{{- end -}}
{{- end -}}

{{- /* The internal face carries every family of its group, the Swagger pages
       where the engine serves them, and SCIM with exposure.groups.api.scim; the
       public face carries the CLI families and SCIM only on their own flags. */ -}}
{{- define "envoy-gateway-config.enginePaths" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- $engine := .ctx.Values.ui.engine_api -}}
{{- $api := index (.ctx.Values.exposure.groups | default dict) "api" | default dict -}}
{{- $paths := list -}}
{{- range $family := $r.browserFamilies | default list -}}
{{- $paths = append $paths (printf "%s/%s" $r.pathPrefix $family) -}}
{{- end -}}
{{- $paths = concat $paths ($r.browserPaths | default list) -}}
{{- if $r.cliFamilies -}}
{{- if or (not .public) $engine.cli_families_public -}}
{{- range $family := $r.cliFamilies -}}
{{- $paths = append $paths (printf "%s/%s" $r.pathPrefix $family) -}}
{{- end -}}
{{- $paths = concat $paths ($r.cliPaths | default list) -}}
{{- end -}}
{{- if and (not .public) (include "envoy-gateway-config.engineDocs" (dict "ctx" .ctx)) -}}
{{- $paths = concat $paths (list "/docs" "/redoc") -}}
{{- end -}}
{{- if and $api.scim (or (not .public) $engine.scim_public) -}}
{{- $paths = append $paths $r.scimPrefix -}}
{{- end -}}
{{- end -}}
{{- $paths | toJson -}}
{{- end -}}

{{- define "envoy-gateway-config.validateUi" -}}
{{- $ui := .ctx.Values.ui -}}
{{- include "envoy-gateway-config.refuseRetired" (dict "ctx" .ctx) -}}

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

{{- /* envoyGateway.service.internetFacing is the chart's own cloud-agnostic
       signal (see values.yaml); it says nothing about ui.public_domain, so the
       guards below fire whether or not any UI is ALSO published on its own
       public hostname. */ -}}
{{- $facing := .ctx.Values.envoyGateway.service.internetFacing -}}
{{- if not (kindIs "bool" $facing) -}}
{{- fail (printf "envoyGateway.service.internetFacing is %v (a %s), not a bool -- it decides whether an empty exposure group allow list refuses every address, and a quoted \"false\" is truthy. Write true or false unquoted" $facing (kindOf $facing)) -}}
{{- end -}}
{{- /* internetFacing is declared rather than derived, so false is held against
       the markers the cloud itself reads: an explicit AWS internet-facing scheme,
       or a GKE or AKS LoadBalancer with no internal marker, which both make public. */ -}}
{{- $svc := .ctx.Values.envoyGateway.service -}}
{{- if and (not $facing) (eq ($svc.type | default "LoadBalancer") "LoadBalancer") -}}
{{- $annotations := $svc.annotations | default dict -}}
{{- $clouds := list (toString .ctx.Values.cloud) (toString (dig "cloud" "" (.ctx.Values.global | default dict))) -}}
{{- $consequence := "false turns off the web fence and the receiver route's caller-auth check" -}}
{{- if eq (lower (toString (index $annotations "service.beta.kubernetes.io/aws-load-balancer-scheme" | default ""))) "internet-facing" -}}
{{- fail (printf "envoyGateway.service.internetFacing is false and envoyGateway.service.annotations sets service.beta.kubernetes.io/aws-load-balancer-scheme: internet-facing -- the load balancer is public, and %s. Set internetFacing: true, or make the scheme internal" $consequence) -}}
{{- end -}}
{{- $gke := list (index $annotations "networking.gke.io/load-balancer-type" | default "") (index $annotations "cloud.google.com/load-balancer-type" | default "") -}}
{{- if and (has "gcp" $clouds) (not (has "internal" (list (lower (toString (index $gke 0))) (lower (toString (index $gke 1)))))) -}}
{{- fail (printf "envoyGateway.service.internetFacing is false on a GKE LoadBalancer Service with no networking.gke.io/load-balancer-type: Internal in envoyGateway.service.annotations -- GKE makes that load balancer public, and %s. Set internetFacing: true, or annotate the Service internal" $consequence) -}}
{{- end -}}
{{- if and (has "azure" $clouds) (ne (lower (toString (index $annotations "service.beta.kubernetes.io/azure-load-balancer-internal" | default ""))) "true") -}}
{{- fail (printf "envoyGateway.service.internetFacing is false on an AKS LoadBalancer Service with no service.beta.kubernetes.io/azure-load-balancer-internal: \"true\" in envoyGateway.service.annotations -- AKS makes that load balancer public, and %s. Set internetFacing: true, or annotate the Service internal" $consequence) -}}
{{- end -}}
{{- end -}}
{{- /* Each group is supplied by argocd/values/common.yaml, so a missing one is
       a broken cascade rather than a choice. A quoted "false" is truthy, so
       every switch is refused unless it is a real bool. */ -}}
{{- $exposure := .ctx.Values.exposure | default dict -}}
{{- $groupsSpec := $exposure.groups | default dict -}}
{{- $fields := dict
      "ingest" (list "enabled" "allow" "deny" "door")
      "console" (list "enabled" "allow" "deny")
      "api" (list "enabled" "allow" "deny" "scim")
      "admin" (list "enabled" "allow" "deny" "allowPublic" "tools")
      "vpn" (list "enabled" "allow" "door") -}}
{{- $doors := dict "ingest" (list "loadbalancer" "gateway" "tunnel") "vpn" (list "culvert" "awsClientVpn") -}}
{{- range $g, $want := $fields -}}
{{- $spec := index $groupsSpec $g -}}
{{- if not (kindIs "map" $spec) -}}
{{- fail (printf "exposure.groups.%s is missing -- argocd/values/common.yaml supplies every exposure group (ingest, console, api, admin, vpn) and a deployment changes one in its deploy repo's infra/common.yaml" $g) -}}
{{- end -}}
{{- range $field := keys $spec | sortAlpha -}}
{{- if not (has $field $want) -}}
{{- fail (printf "exposure.groups.%s.%s is not a field of that group -- it takes %s" $g $field (join ", " $want)) -}}
{{- end -}}
{{- end -}}
{{- range $flag := list "enabled" "scim" "allowPublic" -}}
{{- if and (hasKey $spec $flag) (not (kindIs "bool" (index $spec $flag))) -}}
{{- fail (printf "exposure.groups.%s.%s is %v (a %s), not a bool -- a quoted \"false\" is truthy. Write true or false unquoted" $g $flag (index $spec $flag) (kindOf (index $spec $flag))) -}}
{{- end -}}
{{- end -}}
{{- range $list := list "allow" "deny" -}}
{{- if hasKey $spec $list -}}
{{- $value := index $spec $list -}}
{{- if not (or (kindIs "slice" $value) (kindIs "invalid" $value)) -}}
{{- fail (printf "exposure.groups.%s.%s is %v (a %s) -- it is a YAML list of CIDR ranges, such as [203.0.113.0/24], never a comma-separated string" $g $list $value (kindOf $value)) -}}
{{- end -}}
{{- range $entry := $value | default list -}}
{{- if not (include "envoy-gateway-config.cidrValid" (toString $entry)) -}}
{{- fail (printf "exposure.groups.%s.%s carries %q, which is not a CIDR range -- every entry needs an address and a prefix length, such as 203.0.113.7/32 or 2001:db8::/64. Envoy Gateway and the load balancer refuse it at admission, which fails the whole gateway sync" $g $list (toString $entry)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if hasKey $doors $g -}}
{{- if not (has (toString $spec.door) (index $doors $g)) -}}
{{- fail (printf "exposure.groups.%s.door is %q -- it takes %s" $g (toString $spec.door) (join ", " (index $doors $g))) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range $g := keys $groupsSpec | sortAlpha -}}
{{- if not (hasKey $fields $g) -}}
{{- fail (printf "exposure.groups.%s is not an exposure group -- the groups are ingest, console, api, admin and vpn" $g) -}}
{{- end -}}
{{- end -}}
{{- /* helm lint carries on past a fail, so each group read stays nil-safe. */ -}}
{{- $admin := index $groupsSpec "admin" | default dict -}}
{{- $api := index $groupsSpec "api" | default dict -}}
{{- $tools := $admin.tools | default dict -}}
{{- if not (kindIs "map" $tools) -}}
{{- fail (printf "exposure.groups.admin.tools is %v (a %s) -- it maps each admin route key to true or false" $tools (kindOf $tools)) -}}
{{- end -}}
{{- range $key, $on := $tools -}}
{{- if not (kindIs "bool" $on) -}}
{{- fail (printf "exposure.groups.admin.tools.%s is %v (a %s), not a bool -- a quoted \"false\" is truthy, so it would publish the UI anyway. Write true or false unquoted" $key $on (kindOf $on)) -}}
{{- end -}}
{{- if not (and (hasKey $.ctx.Values.routes $key) (eq ((index $.ctx.Values.routes $key).class | default "infra") "infra")) -}}
{{- fail (printf "exposure.groups.admin.tools.%s names no admin route -- the tools are the class-infra routes.<key> entries: argocd, kafbat, cruiseControl, forgejo and links" $key) -}}
{{- end -}}
{{- end -}}
{{- $self := $exposure.selfRanges | default list -}}
{{- if not (kindIs "slice" $self) -}}
{{- fail (printf "exposure.selfRanges is %v (a %s) -- it is a YAML list of CIDR ranges" $self (kindOf $self)) -}}
{{- end -}}
{{- range $entry := $self -}}
{{- if not (include "envoy-gateway-config.cidrValid" (toString $entry)) -}}
{{- fail (printf "exposure.selfRanges carries %q, which is not a CIDR range -- the cluster's own egress addresses are written as /32s" (toString $entry)) -}}
{{- end -}}
{{- /* selfRanges joins every web group's allow list, so a /0 would open all three. */ -}}
{{- if hasSuffix "/0" (toString $entry) -}}
{{- fail (printf "exposure.selfRanges carries %q, which is every address -- it names the cluster's own egress, and every web group admits it, so a /0 opens console, api and admin to the internet" (toString $entry)) -}}
{{- end -}}
{{- end -}}

{{- /* An admin UI on a public load balancer needs someone named to reach it:
       checked here, not only in edge-<cloud>.yaml's default, because a values
       overlay can turn the group on without ever touching that file. */ -}}
{{- if and $facing $admin.enabled (not $admin.allow) -}}
{{- if not $admin.allowPublic -}}
{{- fail "envoyGateway.service.internetFacing is true and exposure.groups.admin.enabled is true with an empty exposure.groups.admin.allow -- every admin UI would render on a public load balancer that refuses every address. Name the ranges that may reach them in exposure.groups.admin.allow, set exposure.groups.admin.allowPublic: true to open them to every address behind their logins, or leave exposure.groups.admin.enabled: false" -}}
{{- end -}}
{{- /* oidc.enabled with no provider renders no edge policy at all. */ -}}
{{- if not (and .ctx.Values.oidc.enabled .ctx.Values.oidc.providers) -}}
{{- $fronted := list -}}
{{- $ownOnly := list -}}
{{- range $key, $r := .ctx.Values.routes -}}
{{- if eq ($r.class | default "infra") "infra" -}}
{{- if $r.edgePolicy -}}
{{- $fronted = append $fronted $key -}}
{{- else if $r.ownLogin -}}
{{- $ownOnly = append $ownOnly $key -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- fail (printf "envoyGateway.service.internetFacing is true and exposure.groups.admin is enabled with allowPublic and no allow list, with no edge OIDC provider (oidc.enabled and an oidc.providers entry) -- every admin UI with a login of its own would render on a public load balancer behind that login alone, with no CIDR fence. An edge provider puts its OIDC and group check in front of the admin routes with edgePolicy true (%s). Those with edgePolicy false (%s) stay behind their own login either way. Set oidc.enabled: true with an oidc.providers entry, name the ranges in exposure.groups.admin.allow, or leave exposure.groups.admin.enabled: false" (join ", " $fronted) (join ", " $ownOnly)) -}}
{{- end -}}
{{- end -}}

{{- /* The CLI face's SCIM path comes from the api group's route, so the public
       flag alone would publish nothing; with the api group off neither public
       flag has a route to publish. */ -}}
{{- $engine := $ui.engine_api -}}
{{- if and $engine.scim_public (not $api.scim) -}}
{{- fail "ui.engine_api.scim_public is true and exposure.groups.api.scim is false -- the SCIM prefix rides the api group's route, so there is none to publish. Set exposure.groups.api.scim: true as well, or leave ui.engine_api.scim_public false" -}}
{{- end -}}
{{- if and (or $engine.cli_families_public $engine.scim_public) (not $api.enabled) -}}
{{- fail "ui.engine_api.cli_families_public or scim_public is true and exposure.groups.api.enabled is false -- both publish the api group's route, which the group being off withdraws. Turn the group on with an allow list, or leave both flags false" -}}
{{- end -}}

{{- /* Envoy Gateway's ConnectionLimit.value has a minimum of 1
       (api/v1alpha1/connection_types.go at v1.9.2), refused only at admission. */ -}}
{{- $limit := .ctx.Values.envoyGateway.connectionLimit | default dict -}}
{{- if not (kindIs "bool" $limit.enabled) -}}
{{- fail (printf "envoyGateway.connectionLimit.enabled is %v (a %s), not a bool -- write true or false unquoted" $limit.enabled (kindOf $limit.enabled)) -}}
{{- end -}}
{{- if and $limit.enabled (not (and (or (kindIs "int" $limit.value) (kindIs "int64" $limit.value) (kindIs "float64" $limit.value)) (ge (int64 $limit.value) 1) (eq (float64 (int64 $limit.value)) (float64 $limit.value)))) -}}
{{- fail (printf "envoyGateway.connectionLimit.value is %v -- it is a whole number of connections, 1 or more, per listener per proxy replica" $limit.value) -}}
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
{{- fail (printf "ui.public.%s is %v (a %s), not a bool -- ui.public.dfe_ui is the dial's edge.product.public and ui.public.hyperdx its edge.admin_uis.public.hyperdx; copy those booleans across unquoted (true/false), not \"true\"/\"false\": a quoted string is truthy here no matter what it says" $name $val (kindOf $val)) -}}
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
{{- /* Envoy Gateway's RateLimitValue.requests runs 0 to 4294967295
       (api/v1alpha1/ratelimit_types.go at v1.9.2), and 0 refuses every caller. */ -}}
{{- range $field := list "requests" "per_client_requests" -}}
{{- $v := index $ui.rate_limit $field -}}
{{- if not (and (or (kindIs "int" $v) (kindIs "int64" $v) (kindIs "float64" $v)) (ge (int64 $v) 1) (le (int64 $v) 4294967295) (eq (float64 (int64 $v)) (float64 $v))) -}}
{{- fail (printf "ui.rate_limit.%s is %v (a %s) -- it is a whole number of requests per unit, 1 to 4294967295, written unquoted: a quoted number is a string here" $field $v (kindOf $v)) -}}
{{- end -}}
{{- end -}}
{{- /* Envoy v1.39.2 spends matched buckets lowest fill rate first
       (local_ratelimit_impl.cc), so a wall at or below one caller's budget is
       spent first and that one caller can empty it. */ -}}
{{- if le (int64 $ui.rate_limit.requests) (int64 $ui.rate_limit.per_client_requests) -}}
{{- fail (printf "ui.rate_limit.requests is %v and ui.rate_limit.per_client_requests is %v -- the shared bucket must be larger than one caller's, or a single caller empties it for every public user. Raise requests above per_client_requests" $ui.rate_limit.requests $ui.rate_limit.per_client_requests) -}}
{{- end -}}
{{- end -}}

{{- /* The Cruise Control UI is a workload of the KAFKA chart, so a flag here
       cannot conjure it. Silently dropping the route would publish the intent
       and nothing else, which is the failure a deployer finds months later. */ -}}
{{- if and $tools.cruiseControl (not (include "envoy-gateway-config.cruiseControlUi" (dict "ctx" .ctx))) -}}
{{- fail "exposure.groups.admin.tools.cruiseControl is true and the kafka chart is not deploying that UI -- it needs kafka.provider strimzi, kafka.mode cluster, kafka.rebalancing.enabled and kafka.rebalancing.ui.enabled, all of which come from the same deploy-config values both charts read. Set them, or leave this tool false" -}}
{{- end -}}

{{- /* routeEnabled withholds a login-less route with no edge login, and a public
       flag on it would otherwise be dropped without a word. */ -}}
{{- if and $ui.public_domain $admin.enabled -}}
{{- range $key, $public := $tools -}}
{{- if $public -}}
{{- $r := index $.ctx.Values.routes $key -}}
{{- if and $r.enabled (not $r.ownLogin) (not (include "envoy-gateway-config.edgeLogin" (dict "ctx" $.ctx "key" $key))) -}}
{{- fail (printf "exposure.groups.admin.tools.%s is true and routes.%s has no login of its own, so it renders only behind the edge login -- set oidc.enabled with an oidc.providers entry and keep routes.%s.edgePolicy true, or leave this tool false" $key $key $key) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- $trusted := include "envoy-gateway-config.cidrList" (dict "value" $ui.trusted_proxy_cidrs) | fromJsonArray -}}
{{- /* Envoy Gateway's CIDR type (api/v1alpha1/shared_types.go at v1.9.2) and
       the Service's loadBalancerSourceRanges both refuse an entry with no
       prefix length, but only at admission, where it surfaces as an Argo
       SyncFailed on the whole edge. Same shape, anchored, checked here. */ -}}
{{- range $entry := $trusted -}}
{{- if not (include "envoy-gateway-config.cidrValid" $entry) -}}
{{- fail (printf "ui.trusted_proxy_cidrs carries %q, which is not a CIDR range -- every entry needs an address and a prefix length, such as 203.0.113.7/32 or 2001:db8::/64. Envoy Gateway and the load balancer refuse it at admission, which fails the whole gateway sync" $entry) -}}
{{- end -}}
{{- end -}}
{{- /* Every peer inside a /0 is trusted, so Envoy takes the client from the
       leftmost X-Forwarded-For entry, which the caller writes. */ -}}
{{- range $entry := $trusted -}}
{{- if hasSuffix "/0" $entry -}}
{{- fail (printf "ui.trusted_proxy_cidrs carries %q, which trusts every address -- Envoy then takes the client from the leftmost X-Forwarded-For entry, which the caller writes, so a /0 trusted list makes the fence a header the client sets. Name only the ranges of the CDN or other L7 proxy in front" $entry) -}}
{{- end -}}
{{- end -}}

{{- /* Authentication on a public route is edge OIDC, or one of the three
       app-side schemes that were read in the code and are admitted by name.
       Anything else is an unauthenticated public UI. */ -}}
{{- $appSide := dict
      "dfeUi" "dfe-ui authenticates app-side through NextAuth"
      "dfeEngine" "the engine authenticates every call itself, from a DFE token or an API key"
      "dfeEngineCli" "the engine authenticates every call itself, from a DFE token or an API key"
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
{{- range $key := list "dfeEngine" "dfeEngineCli" -}}
{{- $paths := include "envoy-gateway-config.enginePaths" (dict "ctx" $.ctx "key" $key "public" false) | fromJsonArray -}}
{{- /* An HTTPRoute rule with no matches defaults to PathPrefix /, so it would
       take every path on the dfe hostname from dfe-ui into this route's group. */ -}}
{{- if and (not $paths) (include "envoy-gateway-config.routeEnabled" (dict "ctx" $.ctx "key" $key)) -}}
{{- fail (printf "routes.%s matches no path on its internal route -- its family and path lists are all empty, and an HTTPRoute rule with no matches answers every path on the dfe hostname. Give it a family or path, or set routes.%s.enabled: false" $key $key) -}}
{{- end -}}
{{- if gt (len $paths) 64 -}}
{{- fail (printf "routes.%s matches %d paths on its internal route and the Gateway API HTTPRoute CRD caps rules[].matches at 64 -- split its path lists across several rules" $key (len $paths)) -}}
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
