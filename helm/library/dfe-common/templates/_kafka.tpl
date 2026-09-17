{{/*
The DFE Kafka provider table -- ONE derivation of (security.protocol,
sasl.mechanism, bundled schema registry) from a provider identity, for every
chart in this repo. It mirrors the canonical table (scalo-rs
transport/kafka/providers.rs, scalo-py scalo.kafka.providers) and the DFE Kafka
credential contract, dfe-engine#98. Kept in sync by hand: change #98 first, then
the canonical table, then here.

  provider          security.protocol   sasl.mechanism   schema registry
  plaintext         PLAINTEXT           (none)           no
  strimzi-no-tls    SASL_PLAINTEXT      SCRAM-SHA-512    no
  redpanda-no-tls   SASL_PLAINTEXT      SCRAM-SHA-512    yes
  strimzi           SASL_SSL            SCRAM-SHA-512    no
  redpanda          SASL_SSL            SCRAM-SHA-512    yes
  msk               SASL_SSL            SCRAM-SHA-512    no
  redpanda-cloud    SASL_SSL            SCRAM-SHA-512    yes
  confluent-cloud   SASL_SSL            PLAIN            yes
  msk_iam           SASL_SSL            OAUTHBEARER      no

The two `-no-tls` keys are dfe-infra's, added for dfe-infra#191: the canonical
table had no entry for SASL_PLAINTEXT with SCRAM-SHA-512, so a DFE-owned broker
-- which this repo deploys with SASL on a TLS-off listener (kafka-single.yaml
`listeners=SASL_PLAINTEXT://:9092`, redpanda.yaml `tls.enabled: false`) -- could
only be named by a key meaning SASL_SSL. scalo does not parse them yet, so they
belong on a chart-side consumer (kafbat, the MSK ACL Job), not in KAFKA_PROVIDER.

PLAIN is the one sanctioned exception to SCRAM-SHA-512 and is permitted only
where the platform offers no SCRAM (Confluent Cloud, API key as the credential).
It always rides SASL_SSL: a PLAIN password must never cross a cleartext
transport, which is the floor scalo's providers::validate enforces and
scripts/tests/test_kafka_credential_chain.py asserts over every key here.
*/}}

{{/*
dfe-common.kafkaProviderNames -- the accepted keys, for a refusal message.
*/}}
{{- define "dfe-common.kafkaProviderNames" -}}
plaintext, strimzi-no-tls, redpanda-no-tls, strimzi, redpanda, msk, redpanda-cloud, confluent-cloud, msk_iam
{{- end }}

{{/*
dfe-common.kafkaSecurityProtocol -- the wire protocol for a provider identity.
Takes the provider key; refuses an unknown one rather than guessing a default.

Usage:
  security.protocol: {{ include "dfe-common.kafkaSecurityProtocol" .Values.kafka.provider }}
*/}}
{{- define "dfe-common.kafkaSecurityProtocol" -}}
{{- $provider := . -}}
{{- if eq $provider "plaintext" -}}
PLAINTEXT
{{- else if or (eq $provider "strimzi-no-tls") (eq $provider "redpanda-no-tls") -}}
SASL_PLAINTEXT
{{- else if or (eq $provider "strimzi") (eq $provider "redpanda") (eq $provider "msk") (eq $provider "redpanda-cloud") (eq $provider "confluent-cloud") (eq $provider "msk_iam") -}}
SASL_SSL
{{- else -}}
{{- fail (printf "kafka: unknown provider %q; expected one of: %s" $provider (include "dfe-common.kafkaProviderNames" .)) -}}
{{- end -}}
{{- end }}

{{/*
dfe-common.kafkaSaslMechanism -- the SASL mechanism for a provider identity.
Empty for plaintext (no SASL). NEVER hand-set by an operator: this is the single
derivation point, matching scalo's KafkaConfig.provider behaviour.

Usage:
  sasl.mechanism: {{ include "dfe-common.kafkaSaslMechanism" .Values.kafka.provider | quote }}
*/}}
{{- define "dfe-common.kafkaSaslMechanism" -}}
{{- $provider := . -}}
{{- if eq $provider "confluent-cloud" -}}
PLAIN
{{- else if eq $provider "msk_iam" -}}
OAUTHBEARER
{{- else if eq $provider "plaintext" -}}
{{- else if or (eq $provider "strimzi-no-tls") (eq $provider "redpanda-no-tls") (eq $provider "strimzi") (eq $provider "redpanda") (eq $provider "msk") (eq $provider "redpanda-cloud") -}}
SCRAM-SHA-512
{{- else -}}
{{- fail (printf "kafka: unknown provider %q; expected one of: %s" $provider (include "dfe-common.kafkaProviderNames" .)) -}}
{{- end -}}
{{- end }}

{{/*
dfe-common.kafkaHasSchemaRegistry -- true when the provider ships a bundled
schema registry, mirroring KnownProvider::schema_registry() in scalo-rs. Renders
"true" or "" so a caller can use it with `if`.
*/}}
{{- define "dfe-common.kafkaHasSchemaRegistry" -}}
{{- $provider := . -}}
{{- if or (eq $provider "confluent-cloud") (eq $provider "redpanda") (eq $provider "redpanda-no-tls") (eq $provider "redpanda-cloud") -}}
true
{{- end -}}
{{- end }}

{{/*
dfe-common.kafkaProviderEnv -- renders the KAFKA_PROVIDER env entry when
.Values.kafka.provider is set (empty = unset, the no-provider behaviour).

WHAT READS IT (verified 2026-09-15 against the vendored scalo-rs): scalo's
KafkaConfig::from_env reads the PROVIDER suffix (transport/kafka/config.rs:1538)
and KafkaTransport::new applies it (transport/kafka/mod.rs:225), where
apply_provider derives security_protocol + sasl_mechanism from the table above
and OVERWRITES whatever the caller set. Of the six Rust consumers only
dfe-transform-elastic builds its config through from_env (src/service.rs:438);
receiver, loader, fetcher, transform-vrl and transform-vector hand-build theirs
and never set provider, so for those five this var is rendered and not consulted
-- tracked in dfe-infra#190.

Two consequences while that is true. A chart must not put a `-no-tls` key here:
scalo's parse rejects it and the transport refuses to construct. And a canonical
key here is a claim about the broker that scalo will act on the day the app is
wired, so it has to match the listener the deployment actually dials.

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
