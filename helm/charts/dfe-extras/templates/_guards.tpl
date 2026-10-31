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
dfe-extras.telemetryModeGuard -- the refusal of a telemetry.mode no OTLP endpoint
resolves from.

apps/_common.yaml's otlpEndpointTemplate names an endpoint for hyperdx, receiver and
external, and none for prometheus by design. Any other value also resolves to an
empty endpoint, which every app reads as "export nothing", so a typo would switch
the stack's self-monitoring off with no error. Unset reads as hyperdx, as the
template reads it.
*/}}
{{- define "dfe-extras.telemetryModeGuard" -}}
{{- $mode := toString ((.Values.telemetry | default dict).mode | default "hyperdx") -}}
{{- $known := list "hyperdx" "receiver" "external" "prometheus" -}}
{{- if not (has $mode $known) -}}
{{- fail (printf "dfe-extras: telemetry.mode is %q, which resolves no OTLP endpoint, so every app would export nothing -- set it to one of %s in the deploy repo's infra/common.yaml" $mode (join ", " $known)) -}}
{{- end -}}
{{- end -}}

{{/*
dfe-extras.receiverExposureGuard -- the receiver's door, refused where it would
not do what it says.

The door is exposure.groups.ingest (argocd/values/common.yaml), which the
gateway chart reads too. exposure.mode is retired and refused by name, with no
shim, so an overlay still carrying it fails rather than silently taking the
new default door.

Door loadbalancer is the receiver's own load balancer, which
argocd/values/apps/dfe-receiver renders from exposure.groups.ingest.allow and
exposure.public as docs/ingest-edge.md documents them. scalo-service's own
publicService reads none of them, so switched on it renders a second load
balancer of the same name with no allow list, and any other key set under it
reaches nothing. loadBalancerIP is the appset's, carrying the cluster secret's
receiver address.

Kubernetes filters on loadBalancerSourceRanges for a LoadBalancer only, a named
AWS security group drops them, and a deny list on that door could land only in
the receiver's server.ip_filter, which no values layer reaches, so each of
those is refused rather than ignored.

Door gateway routes through the cluster Gateway, whose only path to the
receiver is an HTTPRoute serving the listener named `http`. Any OTHER listener
marked exposed has no path at all, so the render fails rather than deploy a
listener that silently receives nothing. Door tunnel is not caught by this: the
tunnel delivers a client onto the pod network and it dials the ClusterIP.
*/}}
{{- define "dfe-extras.receiverExposureGuard" -}}
{{- if not (kindIs "invalid" .Values.exposure.mode) }}
{{- fail (printf "dfe-receiver: exposure.mode is %q and retired -- the receiver's door is exposure.groups.ingest.door: loadbalancer (its own load balancer, rendered while exposure.groups.ingest.allow names the senders; was public), gateway (receiver.{domain} on the gateway; was internal) or tunnel (no public door; was vpn). Move it there, in the deploy repo's infra/common.yaml (docs/ingest-edge.md)" (toString .Values.exposure.mode)) }}
{{- end }}
{{- $ingest := .Values.exposure.groups.ingest }}
{{- if not (kindIs "map" $ingest) }}
{{- fail (printf "dfe-receiver: exposure.groups.ingest is %v (a %s) -- it is a map of enabled, allow, deny and door, which argocd/values/common.yaml supplies and a deployment changes in its deploy repo's infra/common.yaml" $ingest (kindOf $ingest)) }}
{{- end }}
{{- if not (kindIs "bool" $ingest.enabled) }}
{{- fail (printf "dfe-receiver: exposure.groups.ingest.enabled is %v (a %s), not a bool -- a quoted \"false\" is truthy and would open the door. Write true or false unquoted" $ingest.enabled (kindOf $ingest.enabled)) }}
{{- end }}
{{- $door := toString $ingest.door }}
{{- if not (has $door (list "loadbalancer" "gateway" "tunnel")) }}
{{- fail (printf "dfe-receiver: exposure.groups.ingest.door is %q -- it takes loadbalancer, gateway or tunnel" $door) }}
{{- end }}
{{- range $list := list "allow" "deny" }}
{{- $value := index $ingest $list }}
{{- if not (or (kindIs "slice" $value) (kindIs "invalid" $value)) }}
{{- fail (printf "dfe-receiver: exposure.groups.ingest.%s is %v (a %s) -- it is a YAML list of CIDR ranges, such as [203.0.113.0/24], never a comma-separated string" $list $value (kindOf $value)) }}
{{- end }}
{{- range $entry := $value | default list }}
{{- if not (regexMatch "^[0-9A-Fa-f.:]+/(0|[1-9][0-9]{0,2})$" (toString $entry)) }}
{{- fail (printf "dfe-receiver: exposure.groups.ingest.%s carries %q, which is not a CIDR range -- every entry needs an address and a prefix length, such as 203.0.113.7/32" $list (toString $entry)) }}
{{- end }}
{{- end }}
{{- end }}
{{- $pub := .Values.exposure.public | default dict }}
{{- if hasKey $pub "loadBalancerSourceRanges" }}
{{- fail "dfe-receiver: exposure.public.loadBalancerSourceRanges is retired -- the receiver's allow list is exposure.groups.ingest.allow, and an empty one renders no load balancer at all rather than an open one. Move the ranges there, in the deploy repo's infra/common.yaml" }}
{{- end }}
{{- $renamed := dict "type" "serviceType" }}
{{- range $key, $value := .Values.publicService | default dict }}
{{- if eq $key "enabled" }}
{{- if eq (toString $value) "true" }}
{{- fail "dfe-receiver: publicService.enabled is true, which renders scalo-service's own load balancer beside the one exposure.groups.ingest.door: loadbalancer renders, under the same name and with no allow list -- leave it false, and set exposure.groups.ingest with exposure.public (docs/ingest-edge.md)" }}
{{- end }}
{{- else if and (ne $key "loadBalancerIP") (not (empty $value)) }}
{{- fail (printf "dfe-receiver: publicService.%s is set, and nothing renders the receiver's load balancer from it -- set exposure.public.%s instead (docs/ingest-edge.md)" $key (get $renamed $key | default $key)) }}
{{- end }}
{{- end }}
{{- $loadbalancer := and $ingest.enabled (eq $door "loadbalancer") }}
{{- $type := toString ($pub.serviceType | default "LoadBalancer") }}
{{- if and $loadbalancer $ingest.allow (ne $type "LoadBalancer") }}
{{- fail (printf "dfe-receiver: exposure.groups.ingest.allow is set with exposure.public.serviceType %q, and Kubernetes only filters on loadBalancerSourceRanges for a LoadBalancer -- set serviceType: LoadBalancer, or exposure.groups.ingest.door: tunnel and put the allow list in front of the node ports yourself" $type) }}
{{- end }}
{{- if and $loadbalancer $ingest.deny }}
{{- fail (printf "dfe-receiver: exposure.groups.ingest.deny is %v on door loadbalancer, and nothing enforces it there -- cloud load balancer rules are allow-only, and the receiver's server.ip_filter is read from its config file, which no values layer can derive from the ingest group. Narrow exposure.groups.ingest.allow instead, or use door gateway, where Envoy refuses the deny list ahead of the allow list (docs/ingest-edge.md)" $ingest.deny) }}
{{- end }}
{{- $annotations := $pub.annotations | default dict }}
{{- if hasKey $annotations "service.beta.kubernetes.io/aws-load-balancer-security-groups" }}
{{- fail "dfe-receiver: exposure.public.annotations names service.beta.kubernetes.io/aws-load-balancer-security-groups -- a named group replaces the frontend security group the AWS Load Balancer Controller fills from loadBalancerSourceRanges, so exposure.groups.ingest.allow would stop filtering. Remove it" }}
{{- end }}
{{- with $pub.eipAllocations }}
{{- $subnets := $pub.subnets | default list }}
{{- if not (and (kindIs "slice" .) (kindIs "slice" $subnets)) }}
{{- fail "dfe-receiver: exposure.public.eipAllocations and exposure.public.subnets are YAML lists -- the appset writes both from the cluster secret's dfe.hyperi.io/receiver_eip_allocations and dfe.hyperi.io/receiver_subnets" }}
{{- end }}
{{- if ne (len .) (len $subnets) }}
{{- fail (printf "dfe-receiver: exposure.public.eipAllocations names %d Elastic IPs and exposure.public.subnets %d subnets -- the AWS Load Balancer Controller takes one allocation per subnet, in the same order" (len .) (len $subnets)) }}
{{- end }}
{{- if ne (toString (index $annotations "service.beta.kubernetes.io/aws-load-balancer-scheme")) "internet-facing" }}
{{- fail "dfe-receiver: exposure.public.eipAllocations is set and exposure.public.annotations does not set service.beta.kubernetes.io/aws-load-balancer-scheme: internet-facing -- an Elastic IP attaches only to an internet-facing NLB" }}
{{- end }}
{{- end }}
{{- if and $ingest.enabled (eq $door "gateway") }}
{{- range .Values.listeners }}
{{- if and .exposed (ne .name "http") }}
{{- fail (printf "dfe-receiver: listener %q (%v/%s) is exposed but exposure.groups.ingest.door is gateway, and the cluster Gateway routes only the http listener. Either set door: loadbalancer so it gets the receiver's own load balancer, or add a dedicated Gateway listener plus a TCPRoute/UDPRoute for it and mark this listener exposed: false." .name .port (.protocol | default "TCP")) }}
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
dfe-extras.valuesMigrationGuard -- the refusal of a deploy repo still written for
the 2.2.0 charts.

files/values-migrations.yaml maps each key the 2.2.0 charts read to the key the
thin charts read, and `dfe-ops upgrade migrate-values` writes the new key beside
the old. A rule with a `guard` fails the render while its old key holds a value
and its new key holds none (guard empty), or a different one (guard equal, for a
new key an integration layer always sets). Rendered on, such an overlay renames
each per-config workload, prunes the fetcher's cursor claim and empties the
transforms' file sets. A failed render leaves the Application's live objects as
they are, which Argo CD 3.5.x does for any source that fails to render.
*/}}
{{- define "dfe-extras.valuesMigrationGuard" -}}
{{- $map := .Files.Get "files/values-migrations.yaml" | fromYaml -}}
{{- $service := toString .Values.chartName -}}
{{- if eq $service "dfe-hyperdx" }}{{ $service = "hyperdx" }}{{ end -}}
{{- range $id, $rule := $map.rules -}}
{{- $guard := toString ($rule.guard | default "") -}}
{{- $apps := list -}}
{{- range splitList "," (toString $rule.apps) }}{{ $apps = append $apps (trim .) }}{{ end -}}
{{- if and $guard (or (has "*" $apps) (has $service $apps)) -}}
{{- $old := get (include "dfe-extras.valueAt" (dict "values" $.Values "path" (toString $rule.from)) | fromJson) "v" -}}
{{- $new := get (include "dfe-extras.valueAt" (dict "values" $.Values "path" (toString $rule.to)) | fromJson) "v" -}}
{{- $stale := and (not (empty $old)) (empty $new) -}}
{{- if eq $guard "equal" -}}
{{- $stale = and (not (empty $old)) (ne (toJson $old) (toJson $new)) -}}
{{- end -}}
{{- if $stale -}}
{{- fail (printf "%s: %s is set and %s is not -- the overlay was written for the charts before %s, and the charts from %s read %s in its place. Run dfe-ops upgrade migrate-values --deploy <deploy repo checkout>, then commit and push (docs/deployment/upgrades.md#from-220)" $service $rule.from $rule.to (toString $map.since) (toString $map.since) $rule.to) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
dfe-extras.valueAt -- the value at a dotted path through maps, as JSON {"v": value},
or {} where any step is missing. Takes (dict "values" <map> "path" "a.b.c").
*/}}
{{- define "dfe-extras.valueAt" -}}
{{- $node := .values -}}
{{- $found := true -}}
{{- range splitList "." .path -}}
{{- if and $found (kindIs "map" $node) (hasKey $node .) -}}
{{- $node = index $node . -}}
{{- else -}}
{{- $found = false -}}
{{- end -}}
{{- end -}}
{{- if $found }}{{ dict "v" $node | toJson }}{{ else }}{{ dict | toJson }}{{ end -}}
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
