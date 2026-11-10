# Kafka broker autoscaling

`kafka.autoscaling` drives a KEDA `ScaledObject` that scales the broker `KafkaNodePool`'s replica count directly. Strimzi 1.2.0 put a `/scale` subresource on `KafkaNodePool`, and moving `spec.replicas` is exactly what triggers Strimzi's own `autoRebalance` (`kafka-rebalance-templates.yaml`). It renders only at `kafka.provider: strimzi` and `kafka.mode: cluster`, because a single broker has nothing to scale. It's ON by default (`kafka.autoscaling.enabled`).

## When it renders

KEDA's `ScaledObject` schema rejects an empty `triggers` list. So the whole autoscaler (ConfigMap, TriggerAuthentication and ScaledObject) renders only when `kafka.autoscaling.enabled` is true AND at least one trigger would actually fire. That's a landing source under `kafka.landingTopics.sources`, or the opt-in Prometheus trigger below. With `enabled: true` and neither, nothing renders, and the chart's `NOTES.txt` says why, so nobody reads "no ScaledObject" as a bug.

The default, always-available triggers are one per `kafka.landingTopics.sources` entry. Each is Kafka consumer lag on `<source>_land`, in that source's transform consumer group (`dfe-transform-<kind>-<source>`), against the shared `kafka.autoscaling.lagThreshold` (default 10). A second, opt-in trigger compares total broker bytes-in against `kafka.autoscaling.bytesInPerBrokerThresholdMBs`. It needs two things this stack doesn't ship. One is a queryable PromQL endpoint (`kafka.autoscaling.metrics.prometheusUrl`), because the otel-collector's `prometheus` block feeds ClickHouse and isn't a server KEDA can query. The other is Strimzi's Kafka JMX metrics on the Kafka CR. Set `kafka.autoscaling.metrics.usePrometheus: true` only once both exist, because the chart refuses the render otherwise.

`kafka.autoscaling.maxBrokers` is the ceiling (default 6, kept under the sizing-derived partition count so an added broker still gets partitions). There's no floor dial. `minReplicaCount` is always the resolved `kafka.replicas`. `kafka.autoscaling.pollingInterval` (default 30s) covers both triggers.

## Scale-in

Scale-in is off by default, on purpose. `kafka.autoscaling.scaleIn.enabled` disables the underlying HPA's scale-down direction outright (`autoscaling/v2` `selectPolicy: Disabled`) until you set it true. Shrinking the pool means Strimzi has to evacuate the departing broker's partitions first (the `remove-brokers` auto-rebalance template), and that's worth proving safe before it runs unattended. Once enabled, `kafka.autoscaling.scaleIn.cooldownSeconds` (default 1800, capped at 3600 by the Kubernetes HPA API) sets the scale-down stabilisation window.

Scale-in has a precondition. The leaving brokers' data must fit on the remaining ones under Cruise Control's `DiskCapacityGoal` (hard, 0.80 default utilization threshold). `skipHardGoalCheck: true` does NOT lift that check, because it reports a real infeasibility and not a preference. A low `retention.ms` won't free room fast enough on its own either. Kafka never expires an open, active log segment, so `segment.bytes` must be small enough for the active segment to roll, and for retention to then delete it, before you attempt a scale-in.

`kafka.rebalancing.goals` (`helm/charts/kafka/values.yaml`) is Strimzi's own documented default priority order for both rebalance templates, with two leader goals moved up. We moved `LeaderReplicaDistributionGoal` and `LeaderBytesInDistributionGoal` up to run straight after the seven hard goals, instead of second-last and last of eighteen. The unmodified default order left leader skew at 57% on a live 6-broker add-brokers rebalance, with one broker holding 17 leaders against a mean of 29.8. Cruise Control satisfies earlier-listed goals first and rarely reaches goals that far down the list. Nothing is removed and nothing else is reordered. Set `kafka.rebalancing.goals: []` to fall back to Cruise Control's untouched default.

Argo: KEDA owns the `KafkaNodePool`'s `spec.replicas` once scaling, so Argo's `selfHeal` must not revert it as drift. It's the same `ignoreDifferences` plus `RespectIgnoreDifferences=true` pattern `dfe-layer2-apps` uses for Deployment replicas. The kafka chart's Application lives in `argocd/appsets/layer2-data.yaml` (`dfe-layer2-data`), not `layer-scale.yaml` (Strimzi operator only), and carries that `ignoreDifferences` entry on `KafkaNodePool`'s `/spec/replicas`.

Redpanda: the Redpanda CRD (operator v26.2.3, `cluster.redpanda.com/v1alpha2`) declares no scale subresource, so Kubernetes' HPA can't target it. `spec.clusterSpec.statefulset.replicas` exists as a plain field, and scaling it stays manual until Redpanda ships one.

MSK: [`aws-msk-operations.md`](aws-msk-operations.md#broker-count-autoscaling) covers its own CloudWatch-alarm-and-Lambda scaler.
