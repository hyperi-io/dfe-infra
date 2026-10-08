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
*/}}
{{- define "dfe-extras.receiverExposureGuard" -}}
{{- if eq (.Values.exposure.mode | default "public") "internal" }}
{{- range .Values.listeners }}
{{- if and .exposed (ne .name "http") }}
{{- fail (printf "dfe-receiver: listener %q (%v/%s) is exposed but exposure.mode is %q, and the cluster Gateway routes only the http listener. Either set exposure.mode: public so it gets a LoadBalancer, or add a dedicated Gateway listener plus a TCPRoute/UDPRoute for it and mark this listener exposed: false." .name .port (.protocol | default "TCP") $.Values.exposure.mode) }}
{{- end }}
{{- end }}
{{- end }}
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
