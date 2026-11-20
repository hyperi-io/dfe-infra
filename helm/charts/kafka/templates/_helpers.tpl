{{/* Credential key in the secrets store: <project>/<env>/kafka/<provider>, relative to the store's own root. One definition, so every ExternalSecret reads the same key. */}}
{{- define "dfe-kafka.credentialKey" -}}
{{ .Values.project }}/{{ .Values.env }}/kafka/{{ ternary .Values.kafka.external.provider .Values.kafka.provider (eq .Values.kafka.mode "external") }}
{{- end }}

{{/* Brokers this deploy has: 1 at mode=single whatever kafka.replicas says. */}}
{{- define "dfe-kafka.brokerCount" -}}
{{- ternary 1 (int .Values.kafka.replicas) (eq .Values.kafka.mode "single") -}}
{{- end }}

{{/* A Kubernetes storage quantity (20Gi, 500Mi, 1024) as a byte count. */}}
{{- define "dfe-kafka.quantityBytes" -}}
{{- $q := . | toString -}}
{{- if hasSuffix "Ti" $q -}}{{ mul (int64 (trimSuffix "Ti" $q)) 1099511627776 }}
{{- else if hasSuffix "Gi" $q -}}{{ mul (int64 (trimSuffix "Gi" $q)) 1073741824 }}
{{- else if hasSuffix "Mi" $q -}}{{ mul (int64 (trimSuffix "Mi" $q)) 1048576 }}
{{- else if hasSuffix "Ki" $q -}}{{ mul (int64 (trimSuffix "Ki" $q)) 1024 }}
{{- else -}}{{ int64 $q }}
{{- end -}}
{{- end }}

{{/* Key in the dfe-common provider table (_kafka.tpl) for the broker this deploy dials. A DFE-owned broker is the -no-tls key, because both CRs serve SASL on a TLS-off listener (dfe-infra#191). mode=external takes kafka.external.provider verbatim. */}}
{{- define "dfe-kafka.providerIdentity" -}}
{{- if eq .Values.kafka.mode "external" -}}
{{ .Values.kafka.external.provider }}
{{- else if or (eq .Values.kafka.mode "single") (eq .Values.kafka.mode "cluster") -}}
{{ ternary "redpanda-no-tls" "strimzi-no-tls" (eq .Values.kafka.provider "redpanda") }}
{{- end -}}
{{- end }}

{{/* Broker CPU in millicores: its limit, else its request. The limit because the broker runs on every core of its node. Millicores so the thread-dial gate compares integers. */}}
{{- define "dfe-kafka.brokerMilliCpu" -}}
{{- $r := .Values.kafka.resources -}}
{{- $c := (dig "limits" "cpu" "" $r) | default (dig "requests" "cpu" "0" $r) | toString -}}
{{- if hasSuffix "m" $c -}}{{ int64 (trimSuffix "m" $c) }}
{{- else -}}{{ int64 (mulf (float64 $c) 1000.0) }}
{{- end -}}
{{- end }}

{{/* Node selector as YAML: the pool's dfe.hyperi.io/workload label where nodeScheduling.dedicatedPools names the pool, with nodeScheduling.nodeSelector merged over it. Empty when neither applies. */}}
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

{{/* The one partition count, for num.partitions and every landing topic. Formula: docs/deployment/kafka/README.md, "Sizing dials". At mode=external it is kafka.external.numPartitions when set, because kafka.replicas is not the managed cluster's broker count. */}}
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

{{/* messageMaxBytes as a string: the topic configs round-trip through JSON, and a large float64 renders in scientific notation, which the broker CLI and topic config reject. */}}
{{- define "dfe-kafka.messageMaxBytes" -}}
{{- int64 .Values.kafka.messageMaxBytes -}}
{{- end }}

{{/* log.retention.ms: (assumedConsumerDowntimeH + archiverLagH) x 3600000. Milliseconds because it is the only dynamically updatable retention form. */}}
{{- define "dfe-kafka.retentionMs" -}}
{{- $r := .Values.kafka.retention -}}
{{- mul (add (int $r.assumedConsumerDowntimeH) (int $r.archiverLagH)) 3600000 -}}
{{- end }}

{{/* Per-partition size bound, floor(pvcBytes x usableFraction / partition replicas per broker), because log.retention.bytes is per partition. */}}
{{- define "dfe-kafka.retentionBytes" -}}
{{- $pvc := int64 (include "dfe-kafka.quantityBytes" .Values.kafka.storage.size) -}}
{{- $brokers := int (include "dfe-kafka.brokerCount" .) -}}
{{- $partitions := int (include "dfe-kafka.partitions" .) -}}
{{- $rf := min (int .Values.kafka.profile.scale.replicationFactor) $brokers -}}
{{- $perBroker := div (sub (add (mul $partitions $rf) $brokers) 1) $brokers -}}
{{- int64 (floor (divf (mulf (float64 $pvc) (float64 .Values.kafka.retention.usableFraction)) (float64 $perBroker))) -}}
{{- end }}

{{/* The broker NIC for Strimzi's brokerCapacity, in KB/s. Left unset, Strimzi drops the network goals and reports a network-blind rebalance as successful. */}}
{{- define "dfe-kafka.brokerCapacityNetwork" -}}
{{- printf "%dKB/s" (mul (int .Values.kafka.broker.networkMbS) 1000) -}}
{{- end }}

{{/* The broker KafkaNodePool's name, shared with kafka-autoscaler.yaml's scaleTargetRef. */}}
{{- define "dfe-kafka.nodePoolName" -}}
{{- printf "%s-pool" .Values.kafka.name -}}
{{- end }}

{{/* "true" when kafka-autoscaler.yaml renders its ScaledObject. The broker pool's dfe.hyperi.io/replicas-owner label keys on it, which is how layer2-data leaves spec.replicas to KEDA. */}}
{{- define "dfe-kafka.autoscalerRendered" -}}
{{- $a := .Values.kafka.autoscaling -}}
{{- $hasPrometheusTrigger := and $a.metrics.usePrometheus $a.metrics.prometheusUrl -}}
{{- if and (eq .Values.kafka.provider "strimzi") (eq .Values.kafka.mode "cluster") $a.enabled (or .Values.kafka.landingTopics.sources $hasPrometheusTrigger) -}}
true
{{- end -}}
{{- end }}

{{/* Strimzi's in-namespace bootstrap Service for the plain SASL/SCRAM listener, port 9092. */}}
{{- define "dfe-kafka.bootstrapServers" -}}
{{- printf "%s-kafka-bootstrap:9092" .Values.kafka.name -}}
{{- end }}

{{/* Fails the render on an autoscaling config KEDA cannot run. A no-op outside strimzi cluster mode. */}}
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

{{/* Keeps msk_iam, which has no username/password and so renders no credential Secret, paired on both dials. A half-IAM, half-SCRAM deploy only shows as an auth failure. */}}
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

{{/* kafka.provider is strimzi or redpanda. A managed broker goes in kafka.external.provider, or this chart deploys an Apache broker under its name. */}}
{{- define "dfe-kafka.validateBrokerProvider" -}}
{{- if not (has .Values.kafka.provider (list "strimzi" "redpanda")) -}}
{{- fail (printf "kafka.provider is %q, but it names only the broker DFE deploys: strimzi or redpanda. Name a managed broker (msk, confluent-cloud, redpanda-cloud) in kafka.external.provider with kafka.mode=external." .Values.kafka.provider) -}}
{{- end -}}
{{- end }}
