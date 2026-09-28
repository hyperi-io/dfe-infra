{{/*
dfe-common.poolListener -- the pool balancer for one stage, on the direct
transport.

A Kubernetes Service balances per CONNECTION. gRPC opens one connection and
keeps it, so a sender that dials a pool's Service pins itself to whichever pod
it landed on and the rest of the pool stays idle however many replicas KEDA
adds. Putting a Gateway API listener in front of the pool moves the decision to
the proxy, which balances per REQUEST across the pool's endpoints.

Three objects, and no more:

  Service <fullname>-mesh, in the gateway namespace, selecting the mesh
    Gateway's proxy pods. It is the NAME the senders dial; a Gateway's own
    Service carries a generated hash suffix, so nothing can render its address.
  GRPCRoute <fullname>-mesh, beside the pool, matching that name as the request
    authority and sending it to the pool's own Service.
  BackendTrafficPolicy <fullname>-mesh, beside the route and targeting it: the
    request timeout, the retries and the outlier ejection for this pool
    (mesh.routePolicy). It is an Envoy Gateway resource, so a deployment on
    another Gateway API implementation sets mesh.routePolicy.enabled false and
    brings that implementation's equivalent.

The route attaches to ONE wildcard listener on the shared mesh Gateway rather
than a listener per pool. A listener per pool would put every pool's name in the
Gateway object, and the transform pools are one deployment per source -- so the
Gateway would have to be re-rendered whenever a source is added. The hostname on
the route carries the pool identity instead, and a new pool is one more route.

Both the class and the proxy selector are values (argocd/values/common.yaml
`mesh:`), so a cloud provider's Gateway API class is a values change.

Renders only where the transport is direct AND mesh.enabled: the brokered tiers
balance through the broker's consumer group, and a single-replica tier has
nothing to spread.

Usage (a chart opts in by shipping a templates/pool-listener.yaml containing):
  {{- include "dfe-common.poolListener" . }}
*/}}
{{- define "dfe-common.poolListener" -}}
{{- if (include "dfe-common.meshEnabled" .) -}}
{{- $mesh := .Values.mesh -}}
{{- $gw := $mesh.gateway -}}
{{- $alias := include "dfe-common.meshAlias" . -}}
{{- $port := include "dfe-common.pushPort" . -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ $alias }}
  namespace: {{ $gw.namespace }}
  labels:
    {{- include "dfe-common.labels" . | nindent 4 }}
spec:
  type: ClusterIP
  ports:
    - port: {{ $port }}
      targetPort: {{ $mesh.port }}
      protocol: TCP
      name: mesh
  selector:
    {{- toYaml $gw.proxySelector | nindent 4 }}
---
apiVersion: gateway.networking.k8s.io/v1
kind: GRPCRoute
metadata:
  name: {{ $alias }}
  labels:
    {{- include "dfe-common.labels" . | nindent 4 }}
spec:
  parentRefs:
    # Only the parentRef crosses namespaces, which the mesh Gateway's
    # allowedRoutes permits and which needs no ReferenceGrant. The route sits
    # beside its backend, as the edge HTTPRoutes do.
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: {{ $gw.name }}
      namespace: {{ $gw.namespace }}
  hostnames:
    # Both in-cluster spellings of the alias Service, because a gRPC client's
    # authority is whatever name it was handed and the short form resolves
    # inside the gateway namespace.
    - {{ include "dfe-common.meshHost" . }}
    - {{ printf "%s.%s" $alias $gw.namespace }}
  rules:
    - backendRefs:
        - group: ""
          kind: Service
          name: {{ include "dfe-common.fullname" . }}
          port: {{ $port }}
{{- $policy := $mesh.routePolicy | default dict }}
{{- if $policy.enabled }}
{{- $retry := required "mesh.routePolicy.retry is required" $policy.retry }}
{{- $outlier := required "mesh.routePolicy.outlier is required" $policy.outlier }}
---
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: BackendTrafficPolicy
metadata:
  name: {{ $alias }}
  labels:
    {{- include "dfe-common.labels" . | nindent 4 }}
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: GRPCRoute
      name: {{ $alias }}
  # Without it the route's policy replaces the mesh Gateway's, and the pool loses
  # the balancing and keepalive that policy sets.
  mergeType: StrategicMerge
  timeout:
    http:
      requestTimeout: {{ required "mesh.routePolicy.requestTimeout is required" $policy.requestTimeout }}
  retry:
    numRetries: {{ required "mesh.routePolicy.retry.numRetries is required" $retry.numRetries }}
    retryOn:
      # Only the failures where no pod took the request: a retry after one did,
      # unavailable included, delivers the batch twice.
      triggers:
        - connect-failure
        - resource-exhausted
    perRetry:
      backOff:
        baseInterval: {{ $retry.baseInterval }}
        maxInterval: {{ $retry.maxInterval }}
  healthCheck:
    passive:
      consecutive5XxErrors: {{ $outlier.consecutive5xxErrors }}
      interval: {{ $outlier.interval }}
      baseEjectionTime: {{ $outlier.baseEjectionTime }}
      maxEjectionPercent: {{ $outlier.maxEjectionPercent }}
{{- end }}
{{- end -}}
{{- end -}}

{{/*
dfe-common.meshEnabled -- non-empty when this deployment balances its pools
behind a listener, so it reads as a boolean in an `if`.

Two facts have to hold: the deployment carries records point to point (a broker
balances its own consumers), and the profile asked for the balancer. `mesh` is
absent from a chart rendered without the deploy-config values, so the map is
guarded before the key is read.
*/}}
{{- define "dfe-common.meshEnabled" -}}
{{- if eq (include "dfe-common.transport" .) "direct" -}}
{{- if and .Values.mesh .Values.mesh.enabled -}}
true
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
dfe-common.meshAlias -- the Service name a sender dials to reach this pool
through the balancer, distinct from the pool's own Service so both keep meaning
one thing.
*/}}
{{- define "dfe-common.meshAlias" -}}
{{- printf "%s-mesh" (include "dfe-common.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
dfe-common.meshHost -- the alias's cluster DNS name: what the engine compiles
into a sender's destination, and what the route matches as the authority.
*/}}
{{- define "dfe-common.meshHost" -}}
{{- printf "%s.%s.svc.cluster.local" (include "dfe-common.meshAlias" .) .Values.mesh.gateway.namespace -}}
{{- end -}}
