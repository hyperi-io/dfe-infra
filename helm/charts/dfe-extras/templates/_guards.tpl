{{/*
dfe-extras.transportGuard -- fail the render when kafka.mode runs a transport the
profile was not built for.

Each profile's per-app values configure one transport statically (direct gRPC
endpoints on slim and mesh, broker topics on single and scale), and kafka.mode
is also a deployer key ($values/infra/common.yaml, the cluster secret's
kafka_mode). A deployer value that moves the transport under a profile would
otherwise leave every stage wired for the other one. extras.transports names
each profile's transport; a profile it does not list is not checked.
*/}}
{{- define "dfe-extras.transportGuard" -}}
{{- $profile := toString (.Values.profile | default "") -}}
{{- $declared := toString (dig "transports" $profile "" (.Values.extras | default dict)) -}}
{{- if $declared -}}
{{- $runs := include "dfe-common.transport" . -}}
{{- if ne $runs $declared -}}
{{- $mode := "" -}}
{{- with .Values.kafka }}{{ $mode = toString (.mode | default "") }}{{ end -}}
{{- fail (printf "dfe-extras: kafka.mode %q runs the %s transport, but the %s profile is built for %s -- set kafka.mode back in the deploy repo's infra/common.yaml or the cluster secret's dfe.hyperi.io/kafka_mode, or deploy a profile built for %s" $mode $runs $profile $declared $runs) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
dfe-extras.receiverExposureGuard -- the receiver's internal-mode refusal.

Internal mode routes through the cluster Gateway, and the only gateway path the
receiver has is an HTTPRoute (envoy-gateway-config, routes.receiver), which
serves the listener named `http`. Any OTHER listener marked exposed has no path
at all in this mode, so fail the render rather than deploy a listener that
silently receives nothing.

vpn mode is not caught by this: the tunnel delivers a client onto the pod
network and it dials the ClusterIP directly, so every exposed listener is
reachable there without a gateway route.

Public mode is the receiver's load balancer, which the thin chart renders from
publicService.enabled while the ingest NetworkPolicy reads exposure.mode, so
the two must agree: a load balancer with no policy admitting its ports drops
every sender, and a policy opened for a load balancer that does not exist
reads as exposed.
*/}}
{{- define "dfe-extras.receiverExposureGuard" -}}
{{- $mode := .Values.exposure.mode | default "public" }}
{{- $balanced := eq (toString (dig "enabled" false (.Values.publicService | default dict))) "true" }}
{{- if and (eq $mode "public") (not $balanced) }}
{{- fail "dfe-receiver: exposure.mode is \"public\" but publicService.enabled is not true, so no load balancer renders -- set publicService.enabled: true beside it, or choose exposure.mode internal or vpn" }}
{{- end }}
{{- if and $balanced (ne $mode "public") }}
{{- fail (printf "dfe-receiver: publicService.enabled is true but exposure.mode is %q, which reaches the receiver without a load balancer -- set exposure.mode: public, or publicService.enabled: false" $mode) }}
{{- end }}
{{- if eq $mode "internal" }}
{{- range .Values.listeners }}
{{- if and .exposed (ne .name "http") }}
{{- fail (printf "dfe-receiver: listener %q (%v/%s) is exposed but exposure.mode is %q, and the cluster Gateway routes only the http listener. Either set exposure.mode: public so it gets a LoadBalancer, or add a dedicated Gateway listener plus a TCPRoute/UDPRoute for it and mark this listener exposed: false." .name .port (.protocol | default "TCP") $.Values.exposure.mode) }}
{{- end }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
dfe-extras.receiverTelemetryGuard -- the receiver's refusal of telemetry.mode
receiver with its OTLP listener off.

With no receiverEndpoint, every app in receiver mode exports to this receiver's
OTLP port, which it binds only while config.otlp.enabled is true. The contract
marks both OTLP ports public, so turning them on also serves them on the
receiver's load balancer.
*/}}
{{- define "dfe-extras.receiverTelemetryGuard" -}}
{{- $telemetry := .Values.telemetry | default dict -}}
{{- $listening := eq (toString (dig "otlp" "enabled" false (.Values.config | default dict))) "true" -}}
{{- if and (eq (toString ($telemetry.mode | default "hyperdx")) "receiver") (not $telemetry.receiverEndpoint) (not $listening) -}}
{{- fail "dfe-receiver: telemetry.mode is \"receiver\" but config.otlp.enabled is not true, so every app exports to a port the receiver does not bind -- set config.otlp.enabled: true in the receiver's values (which also serves OTLP on the receiver's load balancer), name telemetry.receiverEndpoint, or choose another telemetry.mode" -}}
{{- end -}}
{{- end -}}

{{/*
dfe-extras.culvertListenerGuard -- culvert's refusal of a listener list with no
tunnel in it. local.yaml sets the receiver's list on the same key, so a culvert
that does not restate its own would otherwise open the receiver's ports.
*/}}
{{- define "dfe-extras.culvertListenerGuard" -}}
{{- $names := list -}}
{{- range .Values.listeners }}{{ $names = append $names .name }}{{ end -}}
{{- if not (or (has "wireguard" $names) (has "openvpn-udp" $names)) -}}
{{- fail "culvert: listeners carries neither wireguard nor openvpn-udp, so the server would accept no tunnel at all" -}}
{{- end -}}
{{- end -}}

{{/*
dfe-extras.culvertPolicyGuard -- keep culvert's NetworkPolicy to one renderer.

culvert's tunnel policy is named <fullname> and switched by networkPolicy.enabled,
which is also the switch and the name of the thin chart's own ingress policy.
The layers reach both charts, so a layer setting it true would render two
NetworkPolicies of one name in one Application. The tunnel policy is on unless
a layer sets it false.
*/}}
{{- define "dfe-extras.culvertPolicyGuard" -}}
{{- if eq (toString .Values.chartName) "culvert" -}}
{{- $policy := .Values.networkPolicy | default dict -}}
{{- if and (hasKey $policy "enabled") (eq (toString $policy.enabled) "true") -}}
{{- fail "dfe-extras: networkPolicy.enabled is true in a values layer, which also turns on the culvert chart's own NetworkPolicy of the same name -- leave it unset (the tunnel policy is on by default) or set it false to drop the tunnel policy" -}}
{{- end -}}
{{- end -}}
{{- end -}}
