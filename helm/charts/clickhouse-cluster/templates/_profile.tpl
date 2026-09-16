{{/*
dfe-clickhouse.cpuRequestCores -- the replica's CPU request as a plain number of
cores, so a Kubernetes millicore string ("500m") can size a ClickHouse thread
pool. Requests, not limits: the request is what the scheduler guarantees the
replica, and a pool sized off a limit it never gets is oversubscription.
*/}}
{{- define "dfe-clickhouse.cpuRequestCores" -}}
{{- $cpu := .Values.clickhouse.resources.requests.cpu | toString -}}
{{- if hasSuffix "m" $cpu -}}
{{- divf (float64 (trimSuffix "m" $cpu)) 1000.0 -}}
{{- else -}}
{{- float64 $cpu -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.backgroundPoolSize -- max(floor, perCore x cores), rounded up.

A fixed 16 under-merges a large node and a bare multiple starves a small one, so
the pool is derived from the replica's own CPU request rather than pinned. 16 is
also ClickHouse's own default, so the floor changes nothing on the shapes this
chart ships and only the large ones move.
*/}}
{{- define "dfe-clickhouse.backgroundPoolSize" -}}
{{- $cores := float64 (include "dfe-clickhouse.cpuRequestCores" .) -}}
{{- $scaled := int (ceil (mulf .Values.clickhouse.serverProfile.backgroundPoolSizePerCore $cores)) -}}
{{- max (int .Values.clickhouse.serverProfile.backgroundPoolSizeFloor) $scaled -}}
{{- end }}

{{/*
dfe-clickhouse.serverProfile -- the server-config fragment for the cluster tier,
merged into the operator CR's settings.extraConfig.

CLUSTER MODE ONLY, and the gate is here rather than at the call site so the
fragment cannot be pulled into single mode by accident: the thread ratio and the
merge pool are sized for a multi-core replica and would starve the 2-core single
node, which runs no operator to take them anyway.
*/}}
{{- define "dfe-clickhouse.serverProfile" -}}
{{- if eq .Values.clickhouse.mode "cluster" -}}
concurrent_threads_soft_limit_ratio_to_cores: {{ .Values.clickhouse.serverProfile.concurrentThreadsSoftLimitRatioToCores }}
max_server_memory_usage_to_ram_ratio: {{ .Values.clickhouse.serverProfile.maxServerMemoryUsageToRamRatio }}
background_pool_size: {{ include "dfe-clickhouse.backgroundPoolSize" . }}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.validateUserProfile -- refuse a user profile that would let the
loader acknowledge an insert the server has not flushed.

Called from validate.yaml so it fires in every mode. wait_for_async_insert = 0
turns every INSERT into fire-and-forget, and the loader commits its Kafka offset
on the ack -- so the rows are lost with no error anywhere. Checked against every
falsy spelling YAML and ClickHouse both accept, not just the literal string "0":
a values file writing an unquoted `false` renders the Go bool false, and
toString on THAT gives "false", which the old string-only check let straight
through.
*/}}
{{- define "dfe-clickhouse.validateUserProfile" -}}
{{- $v := .Values.clickhouse.userProfile.waitForAsyncInsert -}}
{{- if or (and (kindIs "bool" $v) (not $v)) (has (toString $v | lower) (list "0" "false" "no" "off")) -}}
{{- fail (printf "clickhouse.userProfile.waitForAsyncInsert must not be %v -- the loader would commit a Kafka offset on an insert the server never flushed" $v) -}}
{{- end -}}
{{- end }}
