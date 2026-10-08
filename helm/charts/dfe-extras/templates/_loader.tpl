{{/*
dfe-loader.clickhouseTls -- "true" when the loader dials ClickHouse over HTTPS,
empty otherwise.

clickhouse-cluster's own rule, so both ends agree: on when tls.enabled AND the
server has a CA to sign with (an issuerRef, or the edge module's internal CA),
or when the server is external and brings its own certificate. Refuses verify
false, which the loader cannot honour: its only ClickHouse TLS setting is the
scheme.
*/}}
{{- define "dfe-loader.clickhouseTls" -}}
{{- $tls := .Values.clickhouse.tls | default dict -}}
{{- $signed := or (eq (toString .Values.clickhouse.mode) "external") (dig "issuerRef" "name" "" $tls) (eq (toString (dig "internalCA" "present" false $tls)) "true") -}}
{{- if and (eq (toString $tls.enabled) "true") $signed -}}
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

{{/*
dfe-loader.env -- the loader container's env list, as the 2.2.0 deployment
wrote it, less the entries the thin chart's own env carries: the SASL and
ClickHouse password references (the contract's kafka and clickhouse groups),
the namespace, the OTLP identity and endpoint, and the version-check overrides.
dfe-loader-env carries what is left, so each variable stays absent where 2.2.0
left it out, and the render keeps 2.2.0's refusals of a ClickHouse TLS setup
the loader cannot dial.
*/}}
{{- define "dfe-loader.env" -}}
{{- $caBundle := include "dfe-loader.clickhouseCaBundle" . }}
{{- if eq (include "dfe-common.transport" .) "direct" }}
            # Receiver to loader over gRPC on the push port; the DLQ's kafka backend goes with the broker.
            - name: DFE_LOADER_TRANSPORT
              value: "grpc"
            - name: DFE_LOADER_GRPC__LISTEN
              value: {{ printf "0.0.0.0:%v" .Values.service.grpcPort | quote }}
            - name: DFE_LOADER_DLQ_MODE
              value: "disabled"
{{- else }}
            # A bare KAFKA_BOOTSTRAP_SERVERS matches nothing; DFE_LOADER is the app's prefix.
            - name: DFE_LOADER_KAFKA_BROKERS
              value: {{ .Values.kafka.bootstrapServers | quote }}
            {{- if contains "SSL" (upper (toString .Values.kafka.securityProtocol)) }}
            # TLS listeners only: the app turns kafka.tls on from an SSL value and ignores any other.
            - name: DFE_LOADER_KAFKA_SECURITY_PROTOCOL
              value: {{ .Values.kafka.securityProtocol | quote }}
            {{- end }}
{{- end }}
            # host:port in one string; hosts and protocol move together, and `__` nests protocol in the figment cascade.
            - name: DFE_LOADER_CLICKHOUSE_HOSTS
              value: {{ include "dfe-loader.clickhouseHosts" . | quote }}
            - name: DFE_LOADER_CLICKHOUSE__PROTOCOL
              value: "http"
            - name: DFE_LOADER_CLICKHOUSE_DATABASE
              value: {{ .Values.clickhouse.database | quote }}
            {{- with .Values.clickhouse.user }}
            - name: DFE_LOADER_CLICKHOUSE_USERNAME
              value: {{ . | quote }}
            {{- end }}
{{- if (include "dfe-common.transportIsBus" .) }}
            # The per-app topic dfe-engine creates from the dfe-schemas topic set.
            - name: DFE_LOADER_DLQ_TOPIC
              value: {{ .Values.dlq.topic | quote }}
            - name: DFE_LOADER_DLQ_MODE
              value: {{ .Values.dlq.mode | quote }}
{{- end }}
{{- if $caBundle }}
            # The merged store the clickhouse-ca-bundle init container writes, read in place of the system store.
            - name: SSL_CERT_FILE
              value: /etc/dfe-trust/ca-bundle.pem
{{- end }}
{{- end -}}
