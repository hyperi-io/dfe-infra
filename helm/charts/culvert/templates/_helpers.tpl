{{/*
culvert.clientPrefix -- the first two octets of vpn.clientCIDR.

Every tunnel gets a /24 carved out of the one reserved range, so the range is
declared once and the listeners cannot collide with each other or with the
cluster. Anything longer than /16, or a base with a non-zero third or fourth
octet, has no room for that carve and fails here rather than at the pod.
*/}}
{{- define "culvert.clientPrefix" -}}
{{- $cidr := .Values.vpn.clientCIDR -}}
{{- $parts := splitList "/" $cidr -}}
{{- if ne (len $parts) 2 -}}
{{- fail (printf "culvert: vpn.clientCIDR %q is not a CIDR" $cidr) -}}
{{- end -}}
{{- $octets := splitList "." (index $parts 0) -}}
{{- if ne (len $octets) 4 -}}
{{- fail (printf "culvert: vpn.clientCIDR %q is not an IPv4 CIDR" $cidr) -}}
{{- end -}}
{{- if gt (int (index $parts 1)) 16 -}}
{{- fail (printf "culvert: vpn.clientCIDR %q is longer than /16, which leaves no room for one /24 per tunnel" $cidr) -}}
{{- end -}}
{{- if or (ne (int (index $octets 2)) 0) (ne (int (index $octets 3)) 0) -}}
{{- fail (printf "culvert: vpn.clientCIDR %q must end in .0.0 -- the chart carves the third octet per tunnel" $cidr) -}}
{{- end -}}
{{- printf "%s.%s" (index $octets 0) (index $octets 1) -}}
{{- end -}}

{{/*
culvert.subnet -- the /24 network address for tunnel number N (0-based).
Call as: include "culvert.subnet" (dict "ctx" . "index" 0)
*/}}
{{- define "culvert.subnet" -}}
{{- printf "%s.%d.0" (include "culvert.clientPrefix" .ctx) (int .index) -}}
{{- end -}}

{{/*
culvert.listener -- the listener entry with the given name, or an empty dict.
Call as: include "culvert.listener" (dict "ctx" . "name" "wireguard")
Returns YAML, so callers wrap it in fromYaml.
*/}}
{{- define "culvert.listener" -}}
{{- $found := dict -}}
{{- range .ctx.Values.listeners -}}
{{- if eq .name $.name -}}{{- $found = . -}}{{- end -}}
{{- end -}}
{{- toYaml $found -}}
{{- end -}}

{{/*
culvert.env -- the CULVERT_* environment, built from this chart's values.

One place turns DFE values into the app's own names, so a template never spells
a CULVERT_ key twice and the deployment carries no hand-written env block.
*/}}
{{- define "culvert.env" -}}
{{- $wg := fromYaml (include "culvert.listener" (dict "ctx" . "name" "wireguard")) -}}
{{- $udp := fromYaml (include "culvert.listener" (dict "ctx" . "name" "openvpn-udp")) -}}
{{- $tcp := fromYaml (include "culvert.listener" (dict "ctx" . "name" "openvpn-tcp")) -}}
{{- if and (not $wg) (not $udp) -}}
{{- fail "culvert: listeners carries neither wireguard nor openvpn-udp, so the server would accept no tunnel at all" -}}
{{- end -}}
{{- $protocol := "both" -}}
{{- if not $wg -}}{{- $protocol = "openvpn" -}}{{- else if not $udp -}}{{- $protocol = "wireguard" -}}{{- end -}}
{{- /* Unset destinations fall back to the deployment-wide service range. */ -}}
{{- $destinations := .Values.routes.destinations | default (list (.Values.networkModel).serviceCIDR) -}}
{{- $routes := join "," (compact $destinations) -}}
{{- /* The name clients dial, off the canonical hostname map + the deployment's
     own domain, so it cannot drift from what the certificate is issued for. */ -}}
{{- $cn := .Values.vpn.serverCN -}}
{{- if and (not $cn) .Values.domain -}}
{{- $cn = printf "%s.%s" (dig "vpn" "vpn" (.Values.hostnames | default dict)) .Values.domain -}}
{{- end -}}
{{- /* CULVERT_PROFILE names the mounted tuning ConfigMap: without it the file
     is mounted and never read. */ -}}
{{- $env := dict
  "CULVERT_PROTOCOL" $protocol
  "CULVERT_SERVER_CN" $cn
  "CULVERT_LOG_MODE" "stdout"
  "CULVERT_PROFILE" "culvert"
  "CULVERT_UDP_ENABLED" (ternary "true" "false" (not (not $udp)))
  "CULVERT_TCP_ENABLED" (ternary "true" "false" (not (not $tcp)))
  "CULVERT_HTTPS_ENABLED" (ternary "true" "false" .Values.vpn.httpsTunnel)
  "CULVERT_WG_HTTPS_TUNNEL_ENABLED" (ternary "true" "false" .Values.vpn.httpsTunnel)
  "CULVERT_FULL_TUNNEL" (ternary "true" "false" .Values.vpn.fullTunnel)
  "CULVERT_PUSH_ROUTES" $routes
  "CULVERT_ROUTING_CONTROL_ENABLED" "true"
  "CULVERT_CLIENT_ISOLATION" "true"
  "CULVERT_ALLOWED_DESTINATIONS" $routes
  "CULVERT_BLOCK_LINK_LOCAL" "true"
  "CULVERT_PKI_MODE" .Values.pki.mode
-}}
{{- /* One /24 per tunnel out of the reserved range, in listener order. */ -}}
{{- if $udp -}}
{{- $_ := set $env "CULVERT_UDP_PORT" (toString (int $udp.port)) -}}
{{- $_ := set $env "CULVERT_UDP_NETWORK" (include "culvert.subnet" (dict "ctx" . "index" 0)) -}}
{{- $_ := set $env "CULVERT_UDP_NETMASK" "255.255.255.0" -}}
{{- end -}}
{{- if $tcp -}}
{{- $_ := set $env "CULVERT_TCP_PORT" (toString (int $tcp.port)) -}}
{{- $_ := set $env "CULVERT_TCP_NETWORK" (include "culvert.subnet" (dict "ctx" . "index" 1)) -}}
{{- $_ := set $env "CULVERT_TCP_NETMASK" "255.255.255.0" -}}
{{- end -}}
{{- if $wg -}}
{{- $_ := set $env "CULVERT_WG_PORT" (toString (int $wg.port)) -}}
{{- $_ := set $env "CULVERT_WG_NETWORK" (printf "%s/24" (include "culvert.subnet" (dict "ctx" . "index" 2))) -}}
{{- end -}}
{{- range $i, $ns := .Values.vpn.dns -}}
{{- $_ := set $env (printf "CULVERT_DNS%d" (add1 $i)) $ns -}}
{{- end -}}
{{- if .Values.pki.existingSecret -}}
{{- $mount := .Values.pki.mountPath -}}
{{- $_ := set $env "CULVERT_SECRETS_PROVIDER" "file" -}}
{{- $_ := set $env "CULVERT_SECRETS_CA_CERT_PATH" (printf "%s/ca.crt" $mount) -}}
{{- $_ := set $env "CULVERT_SECRETS_SERVER_CERT_PATH" (printf "%s/server.crt" $mount) -}}
{{- $_ := set $env "CULVERT_SECRETS_SERVER_KEY_PATH" (printf "%s/server.key" $mount) -}}
{{- $_ := set $env "CULVERT_SECRETS_CRL_PATH" (printf "%s/crl.pem" $mount) -}}
{{- $_ := set $env "CULVERT_SECRETS_TC_KEY_PATH" (printf "%s/tc.key" $mount) -}}
{{- end -}}
{{- if .Values.vpn.oidc.enabled -}}
{{- $_ := set $env "CULVERT_OAUTH2_ENABLED" "true" -}}
{{- $_ := set $env "CULVERT_OAUTH2_ISSUER" .Values.vpn.oidc.issuer -}}
{{- $_ := set $env "CULVERT_OAUTH2_CLIENT_ID" .Values.vpn.oidc.clientId -}}
{{- $_ := set $env "CULVERT_OAUTH2_VALIDATE_GROUPS" .Values.vpn.oidc.validateGroups -}}
{{- end -}}
{{- $otel := include "dfe-common.otelEndpoint" . -}}
{{- if $otel -}}
{{- $_ := set $env "CULVERT_OTEL_ENABLED" "true" -}}
{{- $_ := set $env "CULVERT_OTEL_ENDPOINT" $otel -}}
{{- $_ := set $env "CULVERT_OTEL_PROTOCOL" "grpc" -}}
{{- end -}}
{{- toYaml $env -}}
{{- end -}}
