{{/*
dfe-kafka.credentialKey -- where the DFE service user's credential lives in the
secrets store.

ONE definition, because every ExternalSecret that reads the credential reads it
here; if two ever disagree the credential silently fails to arrive and the broker
just never authenticates.

<project>/<env>/kafka/<provider>: the name the deploy layer seeds (tf-secrets
`seed_kafka_user`, and the secrets/aws-sm `kafka/<provider>` seed), RELATIVE to
the store's own root. The store supplies that root itself (the OpenBao mount, the
Secrets Manager prefix), so the key must NOT repeat it. <provider> is the broker
the deploy layer was handed: kafka.provider for one this chart runs,
kafka.external.provider for a managed one at mode=external.

Usage:
  remoteKey: {{ include "dfe-kafka.credentialKey" . }}
*/}}
{{- define "dfe-kafka.credentialKey" -}}
{{ .Values.project }}/{{ .Values.env }}/kafka/{{ ternary .Values.kafka.external.provider .Values.kafka.provider (eq .Values.kafka.mode "external") }}
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

The deployed identity follows what the chart actually stands up:
kafka-single.yaml and redpanda.yaml take the Redpanda path only at
provider=redpanda and the Apache Kafka path otherwise. The MSK bootstrap Job
gates on kafka.external.provider=msk at mode=external, never on kafka.provider.

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
dfe-kafka.brokerMilliCpu -- the cores the broker may run on, in millicores: its
CPU LIMIT, or its request where no limit is set.

The limit, because a dedicated broker node's kubelet and DaemonSets keep the
request under the node's cores while the broker still runs on all of them.
Millicores rather than cores so the thread-dial gate compares integers: a float
comparison on "500m" is where that gate would quietly stop biting.
*/}}
{{- define "dfe-kafka.brokerMilliCpu" -}}
{{- $r := .Values.kafka.resources -}}
{{- $c := (dig "limits" "cpu" "" $r) | default (dig "requests" "cpu" "0" $r) | toString -}}
{{- if hasSuffix "m" $c -}}{{ int64 (trimSuffix "m" $c) }}
{{- else -}}{{ int64 (mulf (float64 $c) 1000.0) }}
{{- end -}}
{{- end }}

{{/*
dfe-kafka.poolSelector -- the node labels one workload's pods require, as YAML:
its pool's dfe.hyperi.io/workload label where nodeScheduling.dedicatedPools names
the pool, then a deployer's nodeScheduling.nodeSelector over it. Empty where the
cluster runs no such pool and the deployer names no selector.

layer2-data hands dedicatedPools the tainted pools on the cluster secret, so a
cluster without them never gets a requirement its pods could wait on.

Usage:
  {{- with include "dfe-kafka.poolSelector" (dict "root" $ "pool" "kafka-broker") | fromYaml }}
*/}}
{{- define "dfe-kafka.poolSelector" -}}
{{- $scheduling := .root.Values.nodeScheduling | default dict -}}
{{- $selector := dict -}}
{{- if has .pool ($scheduling.dedicatedPools | default list) -}}
{{- $_ := set $selector "dfe.hyperi.io/workload" .pool -}}
{{- end -}}
{{- with $scheduling.nodeSelector -}}
{{- $selector = mergeOverwrite $selector (deepCopy .) -}}
{{- end -}}
{{- with $selector }}{{ toYaml . }}{{ end -}}
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
dfe-kafka.autoscalerRendered -- "true" when kafka-autoscaler.yaml renders its
ScaledObject, empty otherwise. ONE definition: the autoscaler renders on it, and
the broker pool carries the dfe.hyperi.io/replicas-owner label on it, which is
the only pool layer2-data then leaves spec.replicas to KEDA on.
*/}}
{{- define "dfe-kafka.autoscalerRendered" -}}
{{- $a := .Values.kafka.autoscaling -}}
{{- $hasPrometheusTrigger := and $a.metrics.usePrometheus $a.metrics.prometheusUrl -}}
{{- if and (eq .Values.kafka.provider "strimzi") (eq .Values.kafka.mode "cluster") $a.enabled (or .Values.kafka.landingTopics.sources $hasPrometheusTrigger) -}}
true
{{- end -}}
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
dfe-kafka.validateBrokerProvider -- kafka.provider names the broker this chart
DEPLOYS, so it is strimzi or redpanda. A managed vendor belongs in
kafka.external.provider, and one named here would deploy an Apache broker under
the vendor's name.
*/}}
{{- define "dfe-kafka.validateBrokerProvider" -}}
{{- if not (has .Values.kafka.provider (list "strimzi" "redpanda")) -}}
{{- fail (printf "kafka.provider is %q, but it names only the broker DFE deploys: strimzi or redpanda. Name a managed broker (msk, confluent-cloud, redpanda-cloud) in kafka.external.provider with kafka.mode=external." .Values.kafka.provider) -}}
{{- end -}}
{{- end }}
