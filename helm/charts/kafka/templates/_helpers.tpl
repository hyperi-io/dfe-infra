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
dfe-kafka.brokerCount -- how many brokers this deploy actually has.

ONE definition because the partition and retention derivations below both divide
by it, and the single tier runs one broker whatever kafka.replicas says.
*/}}
{{- define "dfe-kafka.brokerCount" -}}
{{- ternary 1 (int .Values.kafka.replicas) (eq .Values.kafka.mode "single") -}}
{{- end }}

{{/*
dfe-kafka.quantityBytes -- a Kubernetes storage quantity (20Gi, 500Mi, 1024) as
a plain byte count, so the retention derivation can divide by it.

Usage:
  {{ include "dfe-kafka.quantityBytes" .Values.kafka.storage.size }}
*/}}
{{- define "dfe-kafka.quantityBytes" -}}
{{- $q := . | toString -}}
{{- if hasSuffix "Ti" $q -}}{{ mul (int64 (trimSuffix "Ti" $q)) 1099511627776 }}
{{- else if hasSuffix "Gi" $q -}}{{ mul (int64 (trimSuffix "Gi" $q)) 1073741824 }}
{{- else if hasSuffix "Mi" $q -}}{{ mul (int64 (trimSuffix "Mi" $q)) 1048576 }}
{{- else if hasSuffix "Ki" $q -}}{{ mul (int64 (trimSuffix "Ki" $q)) 1024 }}
{{- else -}}{{ int64 $q }}
{{- end -}}
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

The deployed identity follows what the chart actually stands up, not the
kafka.provider string: kafka-single.yaml and redpanda.yaml take the Redpanda path
only at provider=redpanda and the Apache Kafka path for anything else, so a
provider naming a managed vendor (kafka.provider=msk selects the MSK bootstrap
Job at mode=external) still resolves to the Apache broker it really renders here
rather than composing a key the table does not hold.

Usage:
  sasl.mechanism: {{ include "dfe-common.kafkaSaslMechanism" (include "dfe-kafka.providerIdentity" .) | quote }}
*/}}
{{- define "dfe-kafka.providerIdentity" -}}
{{- if eq .Values.kafka.mode "external" -}}
{{ .Values.kafka.external.provider }}
{{- else if or (eq .Values.kafka.mode "single") (eq .Values.kafka.mode "cluster") -}}
{{ ternary "redpanda-no-tls" "strimzi-no-tls" (eq .Values.kafka.provider "redpanda") }}
{{- end -}}
{{- end }}

{{/*
dfe-kafka.brokerMilliCpu -- the broker's CPU REQUEST in millicores.

Millicores rather than cores so the thread-knob gate compares integers: a float
comparison on "500m" is where that gate would quietly stop biting.
*/}}
{{- define "dfe-kafka.brokerMilliCpu" -}}
{{- $c := .Values.kafka.resources.requests.cpu | toString -}}
{{- if hasSuffix "m" $c -}}{{ int64 (trimSuffix "m" $c) }}
{{- else -}}{{ int64 (mulf (float64 $c) 1000.0) }}
{{- end -}}
{{- end }}

{{/*
dfe-kafka.partitions -- the ONE partition count, for the broker's num.partitions
AND for every landing topic the chart creates.

  partitions = roundUpToMultiple(
                 max(consumerCeiling,
                     ceil(peakMbS / perPartitionCapMbS),
                     brokers x minPartitionsPerBroker),
                 brokers)

Rounding up to a multiple of the broker count is what makes the cookie-cutter
scale-out divide evenly every time: at the defaults 3 brokers give 12, which 3,
6 and 12 brokers all share out with no remainder. The count is INCREASE-ONLY --
Kafka can add partitions to a topic and never remove them -- so a deploy that
lowers these values leaves the live topics where they are.

At kafka.mode=external the formula's own $brokers term (dfe-kafka.brokerCount,
which reads kafka.replicas) has nothing to do with the managed cluster's real
broker count -- kafka.replicas is this CHART's own field, never set for a
managed broker. So the external path takes kafka.external.numPartitions
verbatim when it is set, rather than deriving from a broker count the managed
body does not share.
*/}}
{{- define "dfe-kafka.partitions" -}}
{{- if and (eq .Values.kafka.mode "external") .Values.kafka.external.numPartitions -}}
{{- .Values.kafka.external.numPartitions -}}
{{- else -}}
{{- $brokers := int (include "dfe-kafka.brokerCount" .) -}}
{{- $s := .Values.kafka.sizing -}}
{{- $throughput := 0 -}}
{{- if gt (float64 $s.peakMbS) 0.0 -}}
{{- $throughput = int (ceil (divf (float64 $s.peakMbS) (float64 $s.perPartitionCapMbS))) -}}
{{- end -}}
{{- $want := max (int $s.consumerCeiling) $throughput (mul $brokers (int $s.minPartitionsPerBroker)) -}}
{{- mul (div (sub (add (int $want) $brokers) 1) $brokers) $brokers -}}
{{- end -}}
{{- end }}

{{/*
dfe-kafka.messageMaxBytes -- the size chain's one number, as a string.

A string because the topic configs go through a JSON round-trip that turns a
number into a float64, and a bare render of a large float64 is scientific
notation, which both the broker CLI and the topic config reject.
*/}}
{{- define "dfe-kafka.messageMaxBytes" -}}
{{- int64 .Values.kafka.messageMaxBytes -}}
{{- end }}

{{/*
dfe-kafka.retentionMs -- how long a data topic keeps events, in milliseconds.

  (assumedConsumerDowntimeH + archiverLagH) x 3600000

This is the only dynamically-updatable retention form, so the chart writes ms
rather than log.retention.hours.
*/}}
{{- define "dfe-kafka.retentionMs" -}}
{{- $r := .Values.kafka.retention -}}
{{- mul (add (int $r.assumedConsumerDowntimeH) (int $r.archiverLagH)) 3600000 -}}
{{- end }}

{{/*
dfe-kafka.dlqRetentionMs -- the same, for a DLQ topic.
*/}}
{{- define "dfe-kafka.dlqRetentionMs" -}}
{{- mul (int .Values.kafka.retention.dlqRetentionH) 3600000 -}}
{{- end }}

{{/*
dfe-kafka.retentionBytes -- the per-partition size bound, in bytes.

  floor(pvcBytes x usableFraction / partitionsPerBroker)

log.retention.bytes is PER PARTITION, so the PVC is shared out over every
partition REPLICA a broker holds (partitions x RF / brokers, rounded up).
Without it, time retention alone cannot stop a 20Gi PVC filling at 116 MB/s.
*/}}
{{- define "dfe-kafka.retentionBytes" -}}
{{- $pvc := int64 (include "dfe-kafka.quantityBytes" .Values.kafka.storage.size) -}}
{{- $brokers := int (include "dfe-kafka.brokerCount" .) -}}
{{- $partitions := int (include "dfe-kafka.partitions" .) -}}
{{- $rf := min (int .Values.kafka.profile.scale.replicationFactor) $brokers -}}
{{- $perBroker := div (sub (add (mul $partitions $rf) $brokers) 1) $brokers -}}
{{- int64 (floor (divf (mulf (float64 $pvc) (float64 .Values.kafka.retention.usableFraction)) (float64 $perBroker))) -}}
{{- end }}

{{/*
dfe-kafka.brokerCapacityNetwork -- the broker NIC as Strimzi's brokerCapacity
wants it: an integer with a Kubernetes byte unit, then "/s".

MB/s x 1000 = KB/s exactly, so the rendered figure loses nothing on the way in.
Leave inboundNetwork and outboundNetwork unset and Strimzi removes the
network-usage, network-capacity and leader-bytes-in goals from both default.goals
and hard.goals, falls back to a 10000 KiB/s capacity, and reports a successful
rebalance that was network-blind.
*/}}
{{- define "dfe-kafka.brokerCapacityNetwork" -}}
{{- printf "%dKB/s" (mul (int .Values.kafka.broker.networkMbS) 1000) -}}
{{- end }}

{{/*
dfe-kafka.nodePoolName -- the broker KafkaNodePool's name. ONE definition so
kafka-autoscaler.yaml's scaleTargetRef can never drift from the name
kafka-nodepool.yaml actually creates (<kafka.name>-pool, hardcoded there --
this helper exists for the one OTHER reader, since kafka-nodepool.yaml itself
is out of scope for this change).
*/}}
{{- define "dfe-kafka.nodePoolName" -}}
{{- printf "%s-pool" .Values.kafka.name -}}
{{- end }}

{{/*
dfe-kafka.bootstrapServers -- the in-namespace address of Strimzi's own
bootstrap Service for the plain (SASL/SCRAM, non-TLS) listener. Strimzi names
it <cluster>-kafka-bootstrap (kafka.yaml's "plain" listener, port 9092) --
confirmed in argocd/values/profile-scale.yaml and used the same way by kafbat
and the otel-collector's kafka_metrics receiver.
*/}}
{{- define "dfe-kafka.bootstrapServers" -}}
{{- printf "%s-kafka-bootstrap:9092" .Values.kafka.name -}}
{{- end }}

{{/*
dfe-kafka.validateAutoscaling -- refuse an autoscaling configuration KEDA
cannot actually run, at render time rather than at apply time or, worse,
silently at reconcile time. Called from validate.yaml so it fires in every
mode; the guards are no-ops outside kafka.mode=cluster + kafka.provider=strimzi
(kafka-autoscaler.yaml renders nothing there either -- see its own top guard).
*/}}
{{- define "dfe-kafka.validateAutoscaling" -}}
{{- $a := .Values.kafka.autoscaling -}}
{{- if and $a.enabled (eq .Values.kafka.provider "strimzi") (eq .Values.kafka.mode "cluster") -}}
{{- $brokers := int (include "dfe-kafka.brokerCount" .) -}}
{{- if lt (int $a.maxBrokers) $brokers -}}
{{- fail (printf "kafka.autoscaling.maxBrokers (%d) is below the resolved broker count (%d) -- KEDA's maxReplicaCount can never be lower than kafka.replicas" (int $a.maxBrokers) $brokers) -}}
{{- end -}}
{{- if $a.scaleIn.enabled -}}
{{- if le (int $a.scaleIn.cooldownSeconds) 0 -}}
{{- fail (printf "kafka.autoscaling.scaleIn.enabled=true needs kafka.autoscaling.scaleIn.cooldownSeconds > 0, got %d" (int $a.scaleIn.cooldownSeconds)) -}}
{{- end -}}
{{- if gt (int $a.scaleIn.cooldownSeconds) 3600 -}}
{{- fail (printf "kafka.autoscaling.scaleIn.cooldownSeconds (%d) exceeds 3600 -- the Kubernetes HPA API (autoscaling/v2 HPAScalingRules.stabilizationWindowSeconds) caps it at one hour" (int $a.scaleIn.cooldownSeconds)) -}}
{{- end -}}
{{- end -}}
{{- if and $a.metrics.usePrometheus (not $a.metrics.prometheusUrl) -}}
{{- fail "kafka.autoscaling.metrics.usePrometheus=true needs kafka.autoscaling.metrics.prometheusUrl set -- there is no default PromQL-queryable endpoint in this stack for the scaler to query" -}}
{{- end -}}
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
{{/* max.message.bytes is the topic end of the size chain -- a topic left at the
     broker default rejects the very event the broker was raised to accept.
     compression.type=producer is set per topic, not just at the broker,
     so a customer cluster with a different broker default can never make
     the broker recompress what the producer already compressed. Redpanda
     ignores the property, so this is a no-op there rather than a break. */}}
{{- $landingConfig := dict
      "max.message.bytes" (include "dfe-kafka.messageMaxBytes" .)
      "retention.ms" (include "dfe-kafka.retentionMs" .)
      "compression.type" "producer" -}}
{{/* Tiered storage is per-topic as well as per-broker: without
     remote.storage.enable the brokers hold the plugin and move nothing. Only
     the landing topic gets it -- a DLQ is small and read by a human. */}}
{{- if eq (include "dfe-kafka.storageBulk" .) "object" -}}
{{- $landingConfig = merge (dict "remote.storage.enable" "true") $landingConfig -}}
{{- end -}}
{{- if .Values.kafka.defaultTopic.create -}}
{{- $out = append $out (dict
      "name" .Values.kafka.defaultTopic.name
      "partitions" (int (include "dfe-kafka.partitions" .))
      "replicationFactor" (int .Values.kafka.defaultTopic.replicationFactor)
      "config" $landingConfig) -}}
{{- end -}}
{{/* The per-source landing topics. Skipped when a source resolves to the
     default topic's own name, so listing the default source is not an error. */}}
{{- range .Values.kafka.landingTopics.sources -}}
{{- $topic := printf "%s%s" .name $.Values.kafka.landingTopics.suffix -}}
{{- if not (and $.Values.kafka.defaultTopic.create (eq $topic $.Values.kafka.defaultTopic.name)) -}}
{{- $out = append $out (dict
      "name" $topic
      "partitions" (int (.partitions | default (include "dfe-kafka.partitions" $)))
      "replicationFactor" (int $.Values.kafka.defaultTopic.replicationFactor)
      "config" $landingConfig) -}}
{{- end -}}
{{- end -}}
{{- if .Values.kafka.dlqTopics.create -}}
{{/* compression.type=producer here too -- an explicit kafka.dlqTopics.config
     entry still wins, since it is the destination in this merge. */}}
{{- $dlqConfig := merge (deepCopy (.Values.kafka.dlqTopics.config | default (dict))) (dict
      "max.message.bytes" (include "dfe-kafka.messageMaxBytes" .)
      "retention.ms" (include "dfe-kafka.dlqRetentionMs" .)
      "compression.type" "producer") -}}
{{- range .Values.kafka.dlqTopics.names -}}
{{- $out = append $out (dict
      "name" .
      "partitions" (int $.Values.kafka.dlqTopics.partitions)
      "replicationFactor" (int $.Values.kafka.dlqTopics.replicationFactor)
      "config" $dlqConfig) -}}
{{- end -}}
{{- end -}}
{{- toJson $out -}}
{{- end }}
