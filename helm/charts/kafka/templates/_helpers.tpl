{{/*
dfe-kafka.credentialKey -- where the DFE service user's credential lives in the
secrets store.

ONE definition, because the key is written by the PushSecret and read back by two
different ExternalSecrets; if they ever disagree the credential silently fails to
arrive and the broker just never authenticates.

Shape follows the convention the mode=external secret already uses
(externalsecret.yaml): <project>/<env>/kafka/<provider>, RELATIVE to the store's own
mount. dfe-secret-store pins that mount itself (spec.provider.vault.path=secret), so
the key must NOT repeat it -- ESO resolves this to <mount>/data/<key>. Deliberately
provider-agnostic: the same key shape works when the store swaps to aws/gcp/azure.

Usage:
  remoteKey: {{ include "dfe-kafka.credentialKey" . }}
*/}}
{{- define "dfe-kafka.credentialKey" -}}
{{ .Values.project }}/{{ .Values.env }}/kafka/{{ .Values.kafka.provider }}
{{- end }}

{{/*
dfe-kafka.providerIdentity -- the auth identity of the broker this deploy talks
to, as a key in the shared provider table (dfe-common/templates/_kafka.tpl).

It is NOT .Values.kafka.provider: that names the broker to DEPLOY (strimzi or
redpanda), while the identity also carries how the deployed broker is dialled.
Both CRs here serve SASL on a TLS-off listener -- kafka-single.yaml declares
`listeners=SASL_PLAINTEXT://:9092` and redpanda.yaml sets `tls.enabled: false`
-- so a DFE-owned broker is the `-no-tls` key, and the bare provider name would
claim a TLS endpoint nothing in this chart stands up (dfe-infra#191).

mode=external carries a broker somebody else runs, so its identity comes from
kafka.external.provider verbatim. mode=disabled dials nothing and renders empty.

Usage:
  sasl.mechanism: {{ include "dfe-common.kafkaSaslMechanism" (include "dfe-kafka.providerIdentity" .) | quote }}
*/}}
{{- define "dfe-kafka.providerIdentity" -}}
{{- if eq .Values.kafka.mode "external" -}}
{{ .Values.kafka.external.provider }}
{{- else if or (eq .Values.kafka.mode "single") (eq .Values.kafka.mode "cluster") -}}
{{ printf "%s-no-tls" .Values.kafka.provider }}
{{- end -}}
{{- end }}

{{/*
dfe-kafka.validateExternalProvider -- keep the IAM credential shape quarantined
at the mode=external seam.

msk_iam is the one identity with no username/password pair: it authenticates
through IRSA/workload-identity, so it renders no credential Secret. Naming it on
one dial and not the other renders a deploy that is half IAM and half SCRAM, and
the mismatch only surfaces as an auth failure against the broker.
*/}}
{{- define "dfe-kafka.validateExternalProvider" -}}
{{- if eq .Values.kafka.mode "external" -}}
{{- $provider := .Values.kafka.external.provider -}}
{{- $authType := .Values.kafka.external.auth.type -}}
{{- if and (eq $provider "msk_iam") (ne $authType "msk_iam") -}}
{{- fail (printf "kafka: external.provider=msk_iam authenticates through IAM and mints no static credential, so external.auth.type must be msk_iam (got %q)." $authType) -}}
{{- end -}}
{{- if and (eq $authType "msk_iam") $provider (ne $provider "msk_iam") -}}
{{- fail (printf "kafka: external.auth.type=msk_iam renders no credential Secret, so external.provider must be msk_iam or left empty (got %q)." $provider) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
dfe-kafka.bootstrapTopics -- the ONE list of topics a deploy guarantees exist
before the apps start: the default landing topic plus the per-app DLQ topics.

ONE definition because two templates create them -- the Strimzi KafkaTopic CRs
(cluster tier) and the broker-CLI Job (single tier). If those ever iterate
different lists, one tier silently ships without a topic the apps are
configured to write to, and the failure surfaces as produce errors at the
worst possible moment (a DLQ write IS the failure path).

Emits a JSON array of {name, partitions, replicationFactor, config};
consumers parse with fromJsonArray. replicationFactor is UNCLAMPED here --
each tier applies its own broker arithmetic (cluster: min(rf, replicas);
single: hard-coded 1).

Usage:
  {{- $topics := include "dfe-kafka.bootstrapTopics" . | fromJsonArray }}
*/}}
{{- define "dfe-kafka.bootstrapTopics" -}}
{{- $out := list -}}
{{- $landingConfig := dict -}}
{{/* Tiered storage is per-topic as well as per-broker: without
     remote.storage.enable the brokers hold the plugin and move nothing. Only
     the landing topic gets it -- a DLQ is small and read by a human. */}}
{{- if eq (include "dfe-kafka.storageBulk" .) "object" -}}
{{- $landingConfig = dict "remote.storage.enable" "true" -}}
{{- end -}}
{{- if .Values.kafka.defaultTopic.create -}}
{{- $out = append $out (dict
      "name" .Values.kafka.defaultTopic.name
      "partitions" (int .Values.kafka.defaultTopic.partitions)
      "replicationFactor" (int .Values.kafka.defaultTopic.replicationFactor)
      "config" $landingConfig) -}}
{{- end -}}
{{- if .Values.kafka.dlqTopics.create -}}
{{- range .Values.kafka.dlqTopics.names -}}
{{- $out = append $out (dict
      "name" .
      "partitions" (int $.Values.kafka.dlqTopics.partitions)
      "replicationFactor" (int $.Values.kafka.dlqTopics.replicationFactor)
      "config" ($.Values.kafka.dlqTopics.config | default (dict))) -}}
{{- end -}}
{{- end -}}
{{- toJson $out -}}
{{- end }}
