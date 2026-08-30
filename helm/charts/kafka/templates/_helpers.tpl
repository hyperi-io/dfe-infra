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
{{- if eq .Values.kafka.storageModel "tiered" -}}
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
