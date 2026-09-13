{{/*
Route exposure and classification helpers. Local to this chart: classification
is an edge concern, and dfe-common is shared by every DFE chart.

Each takes (dict "ctx" $ "key" "<routes values key>").

routeEnabled -- does this route render? First match wins:
  1. class infra and exposure.infraUisExternal false -> "" (the kill switch is
     absolute for the class, so a route's own enabled:true does not beat it)
  2. the backend is another chart's opt-in workload that the deployment is not
     running                                         -> ""
  3. enabled false                                   -> ""
  4. otherwise                                       -> "true"
cruiseControlUi -- is the kafka chart deploying the Cruise Control UI? The
  gateway cannot see another chart's render, so both read the same kafka.* keys
  from the deploy-config SSoT and agree by reading one set of values.
routeHost    -- subdomain label, from the route's hostname or hostnames[hostnameKey].
routeNs      -- namespace of the route and of any SecurityPolicy targeting it.
infraPolicyRoutes -- JSON array of the keys that render AND take the infra edge
  policy, so the SecurityPolicy and the ExternalSecret feeding it cannot
  disagree about which routes are covered. Read it with fromJsonArray.
*/}}

{{- define "envoy-gateway-config.cruiseControlUi" -}}
{{- $k := .ctx.Values.kafka -}}
{{- /* All four, because all four gate the kafka chart's own render: Redpanda
       has no Cruise Control, a single broker has nothing to rebalance, and the
       UI follows the rebalancer rather than switching on alone. */ -}}
{{- if and (eq ($k.provider | default "strimzi") "strimzi") (eq ($k.mode | default "disabled") "cluster") $k.rebalancing.enabled $k.rebalancing.ui.enabled -}}
true
{{- end -}}
{{- end -}}

{{- define "envoy-gateway-config.routeEnabled" -}}
{{- $r := index .ctx.Values.routes .key -}}
{{- $class := $r.class | default "infra" -}}
{{- if and (eq $class "infra") (not .ctx.Values.exposure.infraUisExternal) -}}
{{- else if and (eq .key "cruiseControl") (not (include "envoy-gateway-config.cruiseControlUi" (dict "ctx" .ctx))) -}}
{{- /* The one route whose backend is a workload of another chart. Without this
       a deployment with no rebalancer publishes a hostname whose Service never
       exists, and the route reports BackendNotFound for the life of the
       deployment. */ -}}
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
publicHost    -- the fully qualified public hostname of a route.
cidrList      -- a comma-separated dial scalar as a YAML list of trimmed entries.
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
{{- end -}}
{{- $keys | sortAlpha | toJson -}}
{{- end -}}

{{- define "envoy-gateway-config.publicHost" -}}
{{- include "envoy-gateway-config.routeHost" . -}}.{{ .ctx.Values.ui.public_domain -}}
{{- end -}}

{{- define "envoy-gateway-config.validateUi" -}}
{{- $ui := .ctx.Values.ui -}}

{{- /* Every admin UI on a public load balancer with no edge auth and no CIDR
       fence is the exact misconfiguration a cloud overlay can reintroduce by
       flipping exposure.infraUisExternal back on -- checked here, not only in
       argocd/values/aws.yaml's default, because a values overlay can undo that
       default without ever touching this file. envoyGateway.service.
       internetFacing is the chart's own cloud-agnostic signal (see
       values.yaml); it says nothing about ui.public_domain, so this fires
       whether or not any UI is ALSO published on its own public hostname. */ -}}
{{- if and .ctx.Values.envoyGateway.service.internetFacing .ctx.Values.exposure.infraUisExternal -}}
{{- if and (not .ctx.Values.oidc.enabled) (not $ui.allowed_cidrs) -}}
{{- fail "envoyGateway.service.internetFacing is true and exposure.infraUisExternal is true, with oidc.enabled false and ui.allowed_cidrs empty -- every admin UI (argocd, kafbat, hyperdx, forgejo, links, cruise-control) would render on a public load balancer with no edge authentication and no CIDR fence. Set oidc.enabled: true, set ui.allowed_cidrs (with ui.trusted_proxy_cidrs), or leave exposure.infraUisExternal: false" -}}
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
{{- fail (printf "ui.public.%s is %v (a %s), not a bool -- copy deployment.example.yaml's ui: block in unquoted (true/false), not \"true\"/\"false\": a quoted string is truthy here no matter what it says" $name $val (kindOf $val)) -}}
{{- end -}}
{{- end -}}
{{- if not (kindIs "bool" $ui.rate_limit.enabled) -}}
{{- fail (printf "ui.rate_limit.enabled is %v (a %s), not a bool -- see the ui.public.* refusal above for why this is refused rather than coerced" $ui.rate_limit.enabled (kindOf $ui.rate_limit.enabled)) -}}
{{- end -}}
{{- if not (kindIs "bool" $ui.tls.hsts) -}}
{{- fail (printf "ui.tls.hsts is %v (a %s), not a bool -- see the ui.public.* refusal above" $ui.tls.hsts (kindOf $ui.tls.hsts)) -}}
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

{{- /* Envoy infers the client IP from X-Forwarded-For. With no trusted proxy
       range it believes the leftmost entry, which the caller writes, so the
       CIDR filter admits anyone who sends the right header. */ -}}
{{- $allowed := include "envoy-gateway-config.cidrList" (dict "value" $ui.allowed_cidrs) | fromJsonArray -}}
{{- $trusted := include "envoy-gateway-config.cidrList" (dict "value" $ui.trusted_proxy_cidrs) | fromJsonArray -}}
{{- if and $allowed (not $trusted) -}}
{{- fail "ui.allowed_cidrs is set and ui.trusted_proxy_cidrs is empty -- Envoy would take the client address from the leftmost X-Forwarded-For entry, which the caller writes, so the filter would admit anyone who sends the right header. Name the load balancer's subnet CIDRs (and any CDN in front of it)" -}}
{{- end -}}

{{- /* Authentication on a public route is edge OIDC, or one of the three
       app-side schemes that were read in the code and are admitted by name.
       Anything else is an unauthenticated public UI. */ -}}
{{- $appSide := dict
      "dfeUi" "dfe-ui authenticates app-side through NextAuth"
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
{{- fail (printf "route %s is public and carries no authentication -- it takes no edge OIDC policy (routes.%s.edgePolicy, or oidc.targetRoutes naming it, with oidc.enabled and a provider set) and only dfe-ui, kafbat and hyperdx are admitted on app-side auth. A public UI with no login is refused" $r.routeName $key) -}}
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
