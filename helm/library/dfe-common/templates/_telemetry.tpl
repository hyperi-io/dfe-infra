{{/*
Self-monitoring telemetry helpers. ONE seam for the whole stack: every app's
deployment resolves its OTLP endpoint through dfe-common.otelEndpoint and adds
dfe-common.prometheusAnnotations to its pods, so the destination (HyperDX-direct
by default, the receiver pipeline, an external OTLP backend, or Prometheus scrape)
is a single values choice -- telemetry.mode -- not a per-chart edit.
*/}}

{{/*
Resolve the OTLP exporter endpoint. An explicit .Values.otel.endpoint wins
(per-deploy escape hatch); otherwise resolve from telemetry.mode. Returns "" for
prometheus mode (no OTLP push) -- callers should guard the OTEL env on non-empty.
*/}}
{{- define "dfe-common.otelEndpoint" -}}
{{- if .Values.otel.endpoint -}}
{{- .Values.otel.endpoint -}}
{{- else -}}
{{- $t := .Values.telemetry | default dict -}}
{{- $mode := $t.mode | default "hyperdx" -}}
{{- if eq $mode "hyperdx" -}}{{ $t.hyperdxEndpoint }}
{{- else if eq $mode "receiver" -}}{{ $t.receiverEndpoint }}
{{- else if eq $mode "external" -}}{{ $t.externalEndpoint }}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Pod annotations for Prometheus scrape -- emitted only when telemetry.mode is
"prometheus" (the external-scrape path). Usage in a pod template:
  metadata:
    annotations:
      {{- include "dfe-common.prometheusAnnotations" . | nindent 8 }}
*/}}
{{- define "dfe-common.prometheusAnnotations" -}}
{{- $t := .Values.telemetry | default dict -}}
{{- if eq ($t.mode | default "hyperdx") "prometheus" -}}
prometheus.io/scrape: "true"
prometheus.io/port: {{ ($t.prometheus).port | default 9090 | quote }}
prometheus.io/path: {{ ($t.prometheus).path | default "/metrics" | quote }}
{{- end -}}
{{- end -}}
