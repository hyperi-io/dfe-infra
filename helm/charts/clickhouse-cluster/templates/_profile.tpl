{{/*
dfe-clickhouse.cpuRequestCores -- the replica's CPU request as a number of cores. The request, not the limit: the limit is not guaranteed, and a pool sized off it oversubscribes.
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
*/}}
{{- define "dfe-clickhouse.backgroundPoolSize" -}}
{{- $cores := float64 (include "dfe-clickhouse.cpuRequestCores" .) -}}
{{- $scaled := int (ceil (mulf .Values.clickhouse.serverProfile.backgroundPoolSizePerCore $cores)) -}}
{{- max (int .Values.clickhouse.serverProfile.backgroundPoolSizeFloor) $scaled -}}
{{- end }}

{{/*
dfe-clickhouse.serverProfile -- the server config for the cluster tier, merged into the operator CR's extraConfig. Cluster mode only, gated here so single mode cannot pull it in.
*/}}
{{- define "dfe-clickhouse.serverProfile" -}}
{{- if eq .Values.clickhouse.mode "cluster" -}}
concurrent_threads_soft_limit_ratio_to_cores: {{ .Values.clickhouse.serverProfile.concurrentThreadsSoftLimitRatioToCores }}
max_server_memory_usage_to_ram_ratio: {{ .Values.clickhouse.serverProfile.maxServerMemoryUsageToRamRatio }}
background_pool_size: {{ include "dfe-clickhouse.backgroundPoolSize" . }}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.validateUserProfile -- refuse wait_for_async_insert = 0, which makes every INSERT fire-and-forget while the loader commits its Kafka offset on the ack. Matches every falsy spelling, including an unquoted `false`. Called from validate.yaml so it fires in every mode.
*/}}
{{- define "dfe-clickhouse.validateUserProfile" -}}
{{- $v := .Values.clickhouse.userProfile.waitForAsyncInsert -}}
{{- if or (and (kindIs "bool" $v) (not $v)) (has (toString $v | lower) (list "0" "false" "no" "off")) -}}
{{- fail (printf "clickhouse.userProfile.waitForAsyncInsert must not be %v -- the loader would commit a Kafka offset on an insert the server never flushed" $v) -}}
{{- end -}}
{{- end }}
