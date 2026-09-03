{{/*
Self-monitoring telemetry helpers. ONE seam for the whole stack: every app's
deployment resolves its OTLP endpoint through dfe-common.otelEndpoint and adds
dfe-common.prometheusAnnotations to its pods, so the destination (HyperDX-direct
by default, the receiver pipeline, an external OTLP backend, or Prometheus scrape)
is a single values choice -- telemetry.mode -- not a per-chart edit.

WHY BOTH PROMETHEUS *AND* OTEL, rather than picking one:
The observability world is still genuinely split between the two, and DFE is a
PRODUCT that other organisations deploy into estates we do not control. A deployer
on AWS may want CloudWatch/CloudTrail; another runs a Prometheus + Grafana estate
and will not take an OTLP push; our own hosted deploy uses HyperDX. Backing every
service with both an OTLP exporter and a Prometheus exposition means the deployer
chooses at deploy time -- telemetry.mode -- instead of us choosing for them, and
neither camp is locked out. Same reasoning as the ClickHouse/Kafka/secrets-manager
seams: code to the seam, let the estate pick the implementation.

So the rule for EVERY DFE-owned service: honour OTEL_EXPORTER_OTLP_ENDPOINT (push,
opt-in -- empty means the deploy chose scrape-only, so export nothing and do NOT
fall back to an OTel default), AND expose /metrics for scrape. Both, always.
*/}}

{{/*
Resolve the OTLP exporter endpoint. An explicit .Values.otel.endpoint wins
(per-deploy escape hatch); otherwise resolve from telemetry.mode. Returns "" for
prometheus mode (no OTLP push) -- callers should guard the OTEL env on non-empty.

A chart-declared .Values.otelProtocol picks the port of the DERIVED gateway
endpoint: grpc (default) 4317, http 4318. Like metricsPort below, the protocol
is a property of the APP, not of the deployment -- the scalo services export
over gRPC, while dfe-ui runs @vercel/otel, which ships no gRPC exporter at all
and speaks only http/protobuf. Handing that a :4317 URL loses every trace with
nothing logged.

Only the derived endpoint is portted. A deployer-supplied otel.endpoint,
telemetry.hyperdxEndpoint, receiverEndpoint or externalEndpoint is theirs and
is left exactly as given -- which does mean a deployer who hand-sets ONE
endpoint for a fleet that speaks both protocols has to front it with something
that serves both.
*/}}
{{- define "dfe-common.otelEndpoint" -}}
{{- $ep := "" -}}
{{- if .Values.otel.endpoint -}}
{{- $ep = .Values.otel.endpoint -}}
{{- else -}}
{{- $t := .Values.telemetry | default dict -}}
{{- $mode := $t.mode | default "hyperdx" -}}
{{- if eq $mode "hyperdx" -}}
{{- $collectorNs := $t.collectorNamespace | default "otel" -}}
{{- $otlpPort := ternary "4318" "4317" (eq (.Values.otelProtocol | default "grpc") "http") -}}
{{/* Empty hyperdxEndpoint derives the deploy-layer collector gateway --
     the one OTLP door; its exporters own the write into the tables
     hyperdx reads (the fork image ships no OTLP receiver). */}}
{{- $ep = $t.hyperdxEndpoint | default (printf "%s-otel-collector-gateway.%s.svc.cluster.local:%s" .Values.project $collectorNs $otlpPort) -}}
{{- else if eq $mode "receiver" -}}{{- $ep = $t.receiverEndpoint -}}
{{- else if eq $mode "external" -}}{{- $ep = $t.externalEndpoint -}}
{{- end -}}
{{- end -}}
{{/* OTEL_EXPORTER_OTLP_ENDPOINT must be a URL with a scheme -- a bare host:port is
     an InvalidUri to the gRPC (tonic) exporter and every export fails. Scheme a
     bare endpoint from otel.tls; an endpoint that already carries :// is left as-is
     (a deployer-supplied external/override URL owns its own scheme). */}}
{{- if and $ep (not (contains "://" $ep)) -}}
{{- $ep = printf "%s://%s" (ternary "https" "http" (.Values.otel.tls | default false)) $ep -}}
{{- end -}}
{{- $ep -}}
{{- end -}}

{{/*
Pod annotations for Prometheus scrape. Emitted WHENEVER the app serves metrics --
independent of telemetry.mode. telemetry.mode selects the OTLP push destination
only; it must not gate scrape, or the two observability pathways become mutually
exclusive and the "both, always" rule above is broken (a Prometheus-estate deployer
running telemetry.mode=hyperdx would get no scrape annotation at all). Every
DFE-owned service exposes /metrics, so scrape is on by default; a deployer that
genuinely wants push-only sets telemetry.prometheus.scrape=false.

Usage in a pod template:
  metadata:
    annotations:
      {{- include "dfe-common.prometheusAnnotations" . | nindent 8 }}
*/}}
{{- define "dfe-common.prometheusAnnotations" -}}
{{- $t := .Values.telemetry | default dict -}}
{{- $prom := $t.prometheus | default dict -}}
{{- $scrape := true -}}
{{- if hasKey $prom "scrape" -}}{{- $scrape = $prom.scrape -}}{{- end -}}
{{- if $scrape -}}
prometheus.io/scrape: "true"
{{/*
A chart-declared .Values.metricsPort WINS over the deploy-wide telemetry.prometheus
port. The port is a property of the APP, not of the deployment: the scalo services
host metrics on their own 9090 server, but dfe-ui is Next.js with exactly one
listener, so it serves /metrics on 3000. The deploy-wide value cannot express that,
and it cannot be fixed in dfe-ui's values.yaml either -- the argocd cascade
(common.yaml) is merged AFTER the chart's own values and would just overwrite it.
Hence the override lives here, where the chart can state its own truth.
*/}}
prometheus.io/port: {{ .Values.metricsPort | default $prom.port | default 9090 | quote }}
prometheus.io/path: {{ $prom.path | default "/metrics" | quote }}
{{- end -}}
{{- end -}}
