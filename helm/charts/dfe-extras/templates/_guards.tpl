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
dfe-extras.projectGuard -- fail the render when the project is not dfe.

The suite's service names (dfe-receiver, dfe-loader, ...), the engine's dfe- name
prefix and the push and wait-for-engine addresses all assume it, so a renamed
project renders objects nothing points at. `project` is dfe-extras' own key
(extras.defaults) and `global.project` the thin chart's (argocd/values/common.yaml
sets it to dfe), and a deployer reaches both from $values/infra/common.yaml or the
instance overlay.
*/}}
{{- define "dfe-extras.projectGuard" -}}
{{- $global := .Values.global | default dict -}}
{{- range $path, $holder := dict "project" .Values "global.project" $global -}}
{{- if hasKey $holder "project" -}}
{{- $value := index $holder "project" -}}
{{- if and (not (kindIs "invalid" $value)) (ne (toString $value) "dfe") -}}
{{- fail (printf "dfe-extras: %s is %q, but the suite's service names, the engine's dfe- prefix and the push and wait-for-engine addresses all assume the project is \"dfe\" -- leave project and global.project unset in the deploy repo's values, which keeps argocd/values/common.yaml's global.project: dfe" $path (toString $value)) -}}
{{- end -}}
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

Public mode is the receiver's load balancer, which argocd/values/apps/dfe-receiver
renders from exposure.mode and exposure.public as docs/INGEST-EDGE.md documents
them. scalo-service's own publicService reads none of
exposure.public, so switched on it renders a second load balancer of the same
name with no allow-list, and any other key set under it reaches nothing.
loadBalancerIP is the appset's, carrying the cluster secret's receiver address.

Kubernetes filters on loadBalancerSourceRanges for a LoadBalancer only, so an
allow-list beside another serviceType is refused rather than ignored.
*/}}
{{- define "dfe-extras.receiverExposureGuard" -}}
{{- $mode := .Values.exposure.mode | default "public" }}
{{- $renamed := dict "type" "serviceType" }}
{{- range $key, $value := .Values.publicService | default dict }}
{{- if eq $key "enabled" }}
{{- if eq (toString $value) "true" }}
{{- fail "dfe-receiver: publicService.enabled is true, which renders scalo-service's own load balancer beside the one exposure.mode public renders, under the same name and with none of the exposure.public keys -- leave it false, and set exposure.mode: public with exposure.public (docs/INGEST-EDGE.md)" }}
{{- end }}
{{- else if and (ne $key "loadBalancerIP") (not (empty $value)) }}
{{- fail (printf "dfe-receiver: publicService.%s is set, and nothing renders the receiver's load balancer from it -- set exposure.public.%s instead (docs/INGEST-EDGE.md)" $key (get $renamed $key | default $key)) }}
{{- end }}
{{- end }}
{{- $pub := .Values.exposure.public | default dict }}
{{- $type := toString ($pub.serviceType | default "LoadBalancer") }}
{{- if and (eq $mode "public") (ne $type "LoadBalancer") $pub.loadBalancerSourceRanges }}
{{- fail (printf "dfe-receiver: exposure.public.loadBalancerSourceRanges is set with exposure.public.serviceType %q, and Kubernetes only filters on that field for a LoadBalancer -- put the allow-list in front of the node ports, or set serviceType: LoadBalancer" $type) }}
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
dfe-extras.kafkaSecretGuard -- the refusal of an empty kafka.saslSecretName while
the pod still reads the contract's kafka credential.

argocd/values/apps names the kafka group's Secret through a secrets.kafka.existingSecret
that renders kafka.saslSecretName, so an empty name becomes a secretKeyRef with no
name, which the API server rejects. scalo-service reads secrets.kafka.enabled as
written, so a deployment without the credential sets it false beside the name.
It reads the layered values alone, so it runs for every component, those with no
defaults under extras.defaults included.
*/}}
{{- define "dfe-extras.kafkaSecretGuard" -}}
{{- $group := dig "kafka" dict (.Values.secrets | default dict) -}}
{{- $reads := and (ne (toString (dig "enabled" true $group)) "false") (contains "saslSecretName" (toString (dig "existingSecret" "" $group))) -}}
{{- if and $reads (empty (dig "saslSecretName" "unset" (.Values.kafka | default dict))) -}}
{{- fail (printf "%s: kafka.saslSecretName is empty, which names no Secret for the kafka credential the pod still reads -- set secrets.kafka.enabled: false beside it to run without one, or name the Secret" (toString .Values.chartName)) -}}
{{- end -}}
{{- end -}}

{{/*
dfe-extras.engineContentGuard -- the refusal of a content entry no init container
materialises.

The engine's content init containers are a fixed list in
argocd/values/apps/dfe-engine, one named content-<entry> per pin, because
scalo-service renders initContainers from values and cannot range over
content.entries. An entry without one leaves the engine reading a directory
nothing fills. A container whose entry is dropped runs and materialises nothing.
A render with no initContainers at all carries no engine values to hold the
entries to, as when this chart renders on its own defaults.
*/}}
{{- define "dfe-extras.engineContentGuard" -}}
{{- if .Values.initContainers -}}
{{- $names := list -}}
{{- range .Values.initContainers }}{{ $names = append $names (toString .name) }}{{ end -}}
{{- range (.Values.content | default dict).entries | default list -}}
{{- if not (has (printf "content-%s" .name) $names) -}}
{{- fail (printf "dfe-engine: content.entries carries %q, and no init container named content-%s materialises it -- the content init containers are a fixed list in argocd/values/apps/dfe-engine/values.yaml, so restate initContainers with one for it beside the entry" .name .name) -}}
{{- end -}}
{{- end -}}
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
dfe-extras.culvertProtocolGuard -- culvert's refusal of a protocol in its profile.

dfe-extras drives culvert's protocol from `listeners`, through CULVERT_PROTOCOL in
dfe-culvert-env, which beats any profile key. A config.protocol (or
configOverrides.protocol) of wireguard or both changes nothing culvert runs, but
it opens the contract's gated wireguard port beside the one apps/culvert/values.yaml
declares in extraPorts, and the API server refuses a pod with two ports of one name.
*/}}
{{- define "dfe-extras.culvertProtocolGuard" -}}
{{- $config := mergeOverwrite (deepCopy (.Values.config | default dict)) (deepCopy (.Values.configOverrides | default dict)) -}}
{{- $protocol := toString ($config.protocol | default "") -}}
{{- if has $protocol (list "wireguard" "both") -}}
{{- fail (printf "culvert: config.protocol is %q, which opens the contract's wireguard port beside the one apps/culvert/values.yaml declares, so the pod carries two ports named wireguard and the API server refuses it. dfe-extras sets the protocol from listeners, through CULVERT_PROTOCOL, which beats the profile anyway -- add or drop the wireguard listener instead, and leave config.protocol unset." $protocol) -}}
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
