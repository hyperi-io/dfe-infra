{{/*
dfe-common.kafkaProviderEnv — renders the KAFKA_PROVIDER env entry when
.Values.kafka.provider is set (empty = unset, current no-provider behaviour
unchanged). scalo's KafkaConfig.provider DERIVES security_protocol +
sasl_mechanism from this identity (strimzi/redpanda/msk/redpanda-cloud/
confluent-cloud/msk_iam/plaintext) — see scalo-rs transport/kafka/providers.rs
and dfe-engine#98 (the DFE Kafka credential contract). The chart never hand-sets
the mechanism.

NOTE (2026-07-12): scalo-rs's KafkaConfig::from_env() does not yet read a
PROVIDER suffix (transport/kafka/config.rs) — this env var is a companion to a
pending scalo-rs change, not yet consumed at runtime. Verify against the scalo
version actually vendored before relying on it.

Usage:
  env:
    {{- include "dfe-common.kafkaProviderEnv" . | nindent 12 }}
*/}}
{{- define "dfe-common.kafkaProviderEnv" -}}
{{- if .Values.kafka.provider }}
- name: KAFKA_PROVIDER
  value: {{ .Values.kafka.provider | quote }}
{{- end }}
{{- end }}
