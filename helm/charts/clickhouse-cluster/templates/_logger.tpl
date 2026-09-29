{{/*
dfe-clickhouse.logger -- the logger fragment for one server, from a values map
carrying level, size and count (clickhouse.serverLog or clickhouse.keeper.log).

The operator CRs' settings.logger and ClickHouse's own <logger> section use the
same three keys, so one fragment serves the ClickHouseCluster, the KeeperCluster
and the single-mode config.d file, and the three cannot drift apart.
*/}}
{{- define "dfe-clickhouse.logger" -}}
level: {{ .level | toString | quote }}
size: {{ .size | toString | quote }}
count: {{ .count | int }}
{{- end }}

{{/*
dfe-clickhouse.validateLogger -- refuse a log setting the server would reject at
start, or one that drops the bound.

Called from validate.yaml so it fires in every mode. The level list is the
operator CRD's enum, which ClickHouse itself also accepts; a level outside it is
a CR the API server refuses in cluster mode and a crashloop in single mode. A
count below 1 is refused because the archive count is what bounds the disk. A
systemLogTTLDays value that is not whole days is refused because `int` would
read it as 0, which is no TTL at all.
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
dfe-clickhouse.systemLogs -- the system log sections the chart sets, as server
config, both modes: a TTL for each table in clickhouse.systemLogTTLDays, and a
removal for each table in clickhouse.systemLogsOff.

The server appends a table's `ttl` key to the engine it creates that table with,
and creates no table whose section is missing (ClickHouse server settings, "system
log tables": https://clickhouse.com/docs/operations/server-configuration-parameters/settings).
`"@remove": remove` deletes the section from the merged config. A table set to 0
days renders nothing and keeps what its base config gives it.
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
