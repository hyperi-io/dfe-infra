{{/*
Memory budget helpers. Every collector memory dial -- memory_limiter, GOMEMLIMIT
and the export queue depth -- derives from the container's own
`resources.limits.memory`, so a deployer that resizes a collector resizes what it
is allowed to buffer at the same time. A literal here goes stale the first time
someone changes the limit, and the collector then OOMKills at a ceiling its own
config never knew about.
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
memory_limiter hard limit: 80% of the container limit, leaving the remaining
fifth for the allocations the limiter cannot see (Go runtime overhead, the
receivers' own read buffers).
*/}}
{{- define "otel-collector.limitMib" -}}
{{- div (mul (atoi (include "otel-collector.memMib" .)) 80) 100 -}}
{{- end -}}

{{/*
memory_limiter spike allowance: 20% of the container limit, so the soft limit
that starts refusing data sits at 60% and the refusal lands before the kernel
does.
*/}}
{{- define "otel-collector.spikeMib" -}}
{{- div (mul (atoi (include "otel-collector.memMib" .)) 20) 100 -}}
{{- end -}}

{{/*
GOMEMLIMIT: the Go soft heap ceiling, the same 80% the memory_limiter enforces.
Without it the runtime sizes its heap against the NODE's memory and only collects
when the heap doubles, which overshoots a small container limit between two
memory_limiter checks.
*/}}
{{- define "otel-collector.goMemLimit" -}}
{{- printf "%dMiB" (atoi (include "otel-collector.limitMib" .)) -}}
{{- end -}}

{{/*
sending_queue depth in batches: a quarter of the container limit in MiB, on the
basis that a 1024-record batch of container log lines is about a MiB.
*/}}
{{- define "otel-collector.queueSize" -}}
{{- div (atoi (include "otel-collector.memMib" .)) 4 -}}
{{- end -}}

{{/*
The memory budget each workload derives from. Required: a collector with no
memory limit has no ceiling to size its buffers against, and the one that
shipped without these dials was OOMKilled 1555 times in 13 days.
*/}}
{{- define "otel-collector.daemonsetMemory" -}}
{{- required "daemonset.resources.limits.memory is required: memory_limiter, GOMEMLIMIT and the export queue derive from it" (dig "resources" "limits" "memory" "" .Values.daemonset) -}}
{{- end -}}

{{- define "otel-collector.gatewayMemory" -}}
{{- required "gateway.resources.limits.memory is required: memory_limiter and GOMEMLIMIT derive from it" (dig "resources" "limits" "memory" "" .Values.gateway) -}}
{{- end -}}
