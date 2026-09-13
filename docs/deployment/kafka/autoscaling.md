# Kafka broker autoscaling

`kafka.autoscaling` drives a KEDA `ScaledObject` that scales the broker
`KafkaNodePool`'s replica count directly -- Strimzi 1.2.0 put a `/scale`
subresource on `KafkaNodePool` (`specReplicasPath: .spec.replicas`,
`statusReplicasPath: .status.replicas`), and moving `spec.replicas` is exactly
what triggers Strimzi's own `autoRebalance` (`kafka-rebalance-templates.yaml`).
It renders only at `kafka.provider: strimzi` and `kafka.mode: cluster` -- a
single broker has nothing to scale -- and is ON by default
(`kafka.autoscaling.enabled`).

**Signal.** The default and always-available triggers are one per
`kafka.landingTopics.sources` entry: Kafka consumer lag on `<source>_land`, in
that source's transform consumer group (`dfe-transform-<kind>-<source>`,
`<kind>` from `landingTopics.sources[].transform` or
`kafka.autoscaling.transformKind`), against the shared
`kafka.autoscaling.lagThreshold` (default 10). A second, opt-in trigger compares total
broker bytes-in against `kafka.autoscaling.bytesInPerBrokerThresholdMBs`, but
it needs two things this stack does not ship: a queryable PromQL endpoint
(`kafka.autoscaling.metrics.prometheusUrl` -- the otel-collector's own
`prometheus` block is an exposition endpoint feeding ClickHouse, not a server
KEDA can query) and Strimzi's Kafka JMX metrics wired onto the Kafka CR,
neither of which exists yet. Set `kafka.autoscaling.metrics.usePrometheus:
true` only once both do; the chart refuses the render otherwise.

**Dials.** `kafka.autoscaling.maxBrokers` is the ceiling (default 6, kept
under the sizing-derived partition count so an added broker still gets
partitions). There is no floor dial: `minReplicaCount` is always the resolved
`kafka.replicas`. `kafka.autoscaling.pollingInterval` (default 30s) covers
both triggers.

**Scale-in is off by default**, deliberately: `kafka.autoscaling.scaleIn.enabled`
disables the underlying HPA's scale-down direction outright
(`autoscaling/v2` `selectPolicy: Disabled`) until set true, because shrinking
the pool means Strimzi has to evacuate the departing broker's partitions
first (the `remove-brokers` auto-rebalance template) and that is worth
proving safe before it runs unattended. Once enabled,
`kafka.autoscaling.scaleIn.cooldownSeconds` (default 1800, capped at 3600 by
the Kubernetes HPA API) sets the scale-down stabilisation window.

**Argo.** KEDA owns the `KafkaNodePool`'s `spec.replicas` once it is scaling,
so Argo's `selfHeal` must not revert it as drift -- the same
`ignoreDifferences` + `RespectIgnoreDifferences=true` pattern
`dfe-scale-apps` already uses for Deployment replicas. The kafka chart's
Application actually lives in `argocd/appsets/layer2-data.yaml`
(`dfe-layer2-data`), not `layer-scale.yaml` (which only installs the Strimzi
operator); that ApplicationSet carries `ignoreDifferences: [{group:
kafka.strimzi.io, kind: KafkaNodePool, jsonPointers: [/spec/replicas]}]` and
`RespectIgnoreDifferences=true` in its `syncOptions`.

**Redpanda.** The Redpanda CRD (operator v26.2.3,
`cluster.redpanda.com/v1alpha2`) declares no scale subresource, only
`status: {}` -- so Kubernetes' HPA cannot target it at all.
`spec.clusterSpec.statefulset.replicas` exists as a plain field; scaling it
stays a manual edit-and-apply until Redpanda ships a scale subresource.

**MSK.** [`aws-msk.md`](aws-msk.md#broker-count-autoscaling) covers its own
CloudWatch-alarm-and-Lambda scaler.
