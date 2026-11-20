{{/*
dfe-clickhouse.logger -- the logger fragment from a map of level, size and count (clickhouse.serverLog or clickhouse.keeper.log). The operator CRs and the single-mode config.d file share these keys.
*/}}
{{- define "dfe-clickhouse.logger" -}}
level: {{ .level | toString | quote }}
size: {{ .size | toString | quote }}
count: {{ .count | int }}
{{- end }}

{{/*
dfe-clickhouse.validateLogger -- refuse a log setting the server would reject at start, or one that drops the disk bound.

The level list is the operator CRD's enum. A count below 1 drops the bound, and a systemLogTTLDays value that is not whole days would read as 0, which is no TTL. Called from validate.yaml so it fires in every mode.
*/}}
{{- define "dfe-clickhouse.validateLogger" -}}
{{- $levels := list "test" "trace" "debug" "information" "notice" "warning" "error" "critical" "fatal" -}}
{{- $logs := dict "clickhouse.serverLog" .Values.clickhouse.serverLog "clickhouse.keeper.log" .Values.clickhouse.keeper.log -}}
{{- range $name, $log := $logs -}}
{{- if not (has (toString $log.level) $levels) -}}
{{- fail (printf "%s.level must be one of %s, not %q" $name (join ", " $levels) (toString $log.level)) -}}
{{- end -}}
{{- if lt (int $log.count) 1 -}}
{{- fail (printf "%s.count must be 1 or more, not %v -- the archive count is the disk bound" $name $log.count) -}}
{{- end -}}
{{- end -}}
{{- range $table, $days := .Values.clickhouse.systemLogTTLDays -}}
{{- if not (regexMatch "^[0-9]+$" (toString $days)) -}}
{{- fail (printf "clickhouse.systemLogTTLDays.%s must be whole days, 0 or more, not %v" $table $days) -}}
{{- end -}}
{{- if and (gt (int $days) 0) (has $table $.Values.clickhouse.systemLogsOff) -}}
{{- fail (printf "clickhouse.systemLogTTLDays.%s is set but clickhouse.systemLogsOff switches %s off; drop one" $table $table) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
dfe-clickhouse.systemLogs -- the system log sections, both modes: a TTL for each table in clickhouse.systemLogTTLDays and a removal for each in clickhouse.systemLogsOff. A table at 0 days renders nothing.
https://clickhouse.com/docs/operations/server-configuration-parameters/settings
*/}}
{{- define "dfe-clickhouse.systemLogs" -}}
{{- range $table, $days := .Values.clickhouse.systemLogTTLDays }}
{{- if gt (int $days) 0 }}
{{ $table }}:
  ttl: event_date + INTERVAL {{ int $days }} DAY DELETE
{{- end }}
{{- end }}
{{- range .Values.clickhouse.systemLogsOff }}
{{ . }}:
  "@remove": remove
{{- end }}
{{- end }}
