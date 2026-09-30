{{/*
dfe-loader.clickhouseTls -- "true" when the loader dials ClickHouse over HTTPS,
empty otherwise. Refuses verify false, which the loader has no way to honour:
its only ClickHouse TLS setting is the scheme.
*/}}
{{- define "dfe-loader.clickhouseTls" -}}
{{- $tls := .Values.clickhouse.tls | default dict -}}
{{- if eq (toString $tls.enabled) "true" -}}
{{- if eq (toString $tls.verify) "false" -}}
{{- fail "clickhouse.tls.verify is false, and dfe-loader always verifies -- set clickhouse.tls.ca to the CA that signed the server certificate" -}}
{{- end -}}
{{- $ca := $tls.ca | default dict -}}
{{- if and $ca.secretName $ca.configMapName -}}
{{- fail "clickhouse.tls.ca takes a secretName or a configMapName, not both" -}}
{{- end -}}
true
{{- end -}}
{{- end }}

{{/*
dfe-loader.clickhouseCaBundle -- "true" when a ClickHouse CA is merged into the
loader's trust store, empty when the system store verifies the server alone.
*/}}
{{- define "dfe-loader.clickhouseCaBundle" -}}
{{- if include "dfe-loader.clickhouseTls" . -}}
{{- with .Values.clickhouse.tls.ca -}}
{{- if or .secretName .configMapName -}}true{{- end -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
dfe-loader.clickhouseHosts -- the one host:port the loader inserts into. The
address also selects the scheme: the loader turns TLS on for port 8443 or 9440,
or a *.clickhouse.cloud host (dfe-loader src/clickhouse/config.rs
host_implies_tls), so any other TLS address fails the render here.
*/}}
{{- define "dfe-loader.clickhouseHosts" -}}
{{- $port := .Values.clickhouse.port -}}
{{- if include "dfe-loader.clickhouseTls" . -}}
{{- $port = .Values.clickhouse.tls.port -}}
{{- if not (or (has (toString $port) (list "8443" "9440")) (contains "clickhouse.cloud" .Values.clickhouse.host)) -}}
{{- fail (printf "clickhouse.tls.port is %v, and dfe-loader turns TLS on only for 8443, 9440 or a clickhouse.cloud host -- it would dial %s in plaintext" $port .Values.clickhouse.host) -}}
{{- end -}}
{{- end -}}
{{- printf "%s:%v" .Values.clickhouse.host $port -}}
{{- end }}
