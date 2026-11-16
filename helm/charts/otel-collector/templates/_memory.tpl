{{/*
Memory budget helpers. memory_limiter, GOMEMLIMIT and the export queue depth all
derive from the container's `resources.limits.memory`. See
docs/observability/otel-collector.md.
*/}}

{{/*
MiB in a Kubernetes memory quantity. Takes the quantity string, e.g. "512Mi".
*/}}
{{- define "otel-collector.memMib" -}}
{{- $q := . | toString -}}
{{- $mib := 0.0 -}}
{{- if hasSuffix "Gi" $q -}}
{{- $mib = mulf (float64 (trimSuffix "Gi" $q)) 1024.0 -}}
{{- else if hasSuffix "Mi" $q -}}
{{- $mib = float64 (trimSuffix "Mi" $q) -}}
{{- else if hasSuffix "Ki" $q -}}
{{- $mib = divf (float64 (trimSuffix "Ki" $q)) 1024.0 -}}
{{- else if hasSuffix "G" $q -}}
{{- $mib = divf (mulf (float64 (trimSuffix "G" $q)) 1000000000.0) 1048576.0 -}}
{{- else if hasSuffix "M" $q -}}
{{- $mib = divf (mulf (float64 (trimSuffix "M" $q)) 1000000.0) 1048576.0 -}}
{{- else -}}
{{- $mib = divf (float64 $q) 1048576.0 -}}
{{- end -}}
{{- int $mib -}}
{{- end -}}

{{/*
memory_limiter hard limit: 80% of the container limit, leaving a fifth for what
the limiter cannot see (Go runtime overhead, the receivers' read buffers).
*/}}
{{- define "otel-collector.limitMib" -}}
{{- div (mul (atoi (include "otel-collector.memMib" .)) 80) 100 -}}
{{- end -}}

{{/*
memory_limiter spike allowance: 20% of the container limit, so the soft limit
that starts refusing data sits at 60%.
*/}}
{{- define "otel-collector.spikeMib" -}}
{{- div (mul (atoi (include "otel-collector.memMib" .)) 20) 100 -}}
{{- end -}}

{{/*
GOMEMLIMIT: the Go soft heap ceiling, the same 80% the memory_limiter enforces.
*/}}
{{- define "otel-collector.goMemLimit" -}}
{{- printf "%dMiB" (atoi (include "otel-collector.limitMib" .)) -}}
{{- end -}}

{{/*
sending_queue depth in batches: a quarter of the container limit in MiB, taking
a 1024-record batch of log lines as about a MiB.
*/}}
{{- define "otel-collector.queueSize" -}}
{{- div (atoi (include "otel-collector.memMib" .)) 4 -}}
{{- end -}}

{{/*
The memory budget each workload derives from. Required: with no limit there is
nothing to size the buffers against.
*/}}
{{- define "otel-collector.daemonsetMemory" -}}
{{- required "daemonset.resources.limits.memory is required: memory_limiter, GOMEMLIMIT and the export queue derive from it" (dig "resources" "limits" "memory" "" .Values.daemonset) -}}
{{- end -}}

{{- define "otel-collector.gatewayMemory" -}}
{{- required "gateway.resources.limits.memory is required: memory_limiter and GOMEMLIMIT derive from it" (dig "resources" "limits" "memory" "" .Values.gateway) -}}
{{- end -}}

{{/* The provider-logs workloads' budget: the Deployment's, or the rancher DaemonSet's. */}}
{{- define "otel-collector.providerLogsMemory" -}}
{{- $pl := .Values.clusterTelemetry.providerLogs -}}
{{- if eq $pl.flavour "rancher" -}}
{{- required "clusterTelemetry.providerLogs.rancher.resources.limits.memory is required" (dig "resources" "limits" "memory" "" $pl.rancher) -}}
{{- else -}}
{{- required "clusterTelemetry.providerLogs.deployment.resources.limits.memory is required" (dig "resources" "limits" "memory" "" $pl.deployment) -}}
{{- end -}}
{{- end -}}
