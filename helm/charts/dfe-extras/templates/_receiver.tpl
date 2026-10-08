{{/*
dfe-receiver.env -- the receiver container's env list, as the 2.2.0 deployment
wrote it, less the entries the thin chart's own env carries: the SASL secret
references (the contract's kafka group), the namespace, the OTLP identity and
endpoint, and the version-check overrides. DFE_RECEIVER_BIND_ADDRESS goes too:
the contract fixes the http port at 8080, the app's own default bind address.
dfe-receiver-env carries what is left, so each variable stays absent where
2.2.0 left it out.
*/}}
{{- define "dfe-receiver.env" -}}
{{- if (include "dfe-common.transportIsBus" .) }}
            # A bare KAFKA_BOOTSTRAP_SERVERS is read by nothing; DFE_RECEIVER is the app's prefix.
            - name: DFE_RECEIVER_KAFKA_BROKERS
              value: {{ .Values.kafka.bootstrapServers | quote }}
            {{- if contains "SSL" (upper (toString .Values.kafka.securityProtocol)) }}
            # TLS listeners only: the app sets kafka.tls.enabled from this, so a plaintext value would turn TLS off.
            - name: DFE_RECEIVER_KAFKA_SECURITY_PROTOCOL
              value: {{ .Values.kafka.securityProtocol | quote }}
            {{- end }}
{{- end }}
{{- if eq (include "dfe-common.transport" .) "direct" }}
            # The DLQ's kafka backend goes with the broker; the receiver's DLQ modes have no "disabled".
            - name: DFE_RECEIVER_DLQ_ENABLED
              value: "false"
{{- else }}
            # The per-app topic dfe-engine creates from the dfe-schemas topic set.
            - name: DFE_RECEIVER_DLQ_TOPIC
              value: {{ .Values.dlq.topic | quote }}
            - name: DFE_RECEIVER_DLQ_MODE
              value: {{ .Values.dlq.mode | quote }}
{{- end }}
{{- end -}}
