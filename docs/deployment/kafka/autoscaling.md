# Kafka broker autoscaling

`kafka.autoscaling` drives a KEDA `ScaledObject` that scales the broker `KafkaNodePool`'s replica count directly. Strimzi 1.2.0 put a `/scale` subresource on `KafkaNodePool`, and moving `spec.replicas` is exactly what triggers Strimzi's `autoRebalance` (`kafka-rebalance-templates.yaml`). It renders at `kafka.provider: strimzi` and `kafka.mode: cluster` alone, because a single broker has nothing to scale. It's ON by default (`kafka.autoscaling.enabled`).

## When it renders

KEDA's `ScaledObject` schema rejects an empty `triggers` list. So the whole autoscaler (ConfigMap, TriggerAuthentication and ScaledObject) renders when `kafka.autoscaling.enabled` is true AND at least one trigger would fire, and not otherwise. That's a landing source under `kafka.landingTopics.sources`, or the opt-in Prometheus trigger below. With `enabled: true` and neither, nothing renders, and the chart's `NOTES.txt` says why, so nobody reads "no ScaledObject" as a bug.

The default, always-available triggers are one per `kafka.landingTopics.sources` entry. Each is Kafka consumer lag on `<source>_land`, in that source's transform consumer group (`dfe-transform-<kind>-<source>`), against the shared `kafka.autoscaling.lagThreshold` (default 10). A second, opt-in trigger compares total broker bytes-in against `kafka.autoscaling.bytesInPerBrokerThresholdMBs`. It needs two things this stack doesn't ship. One is a queryable PromQL endpoint (`kafka.autoscaling.metrics.prometheusUrl`), because the otel-collector's `prometheus` block feeds ClickHouse and isn't a server KEDA can query. The other is Strimzi's Kafka JMX metrics on the Kafka CR. Set `kafka.autoscaling.metrics.usePrometheus: true` once both exist and not before, because the chart fails the render otherwise.

`kafka.autoscaling.maxBrokers` is the ceiling (default 6, kept under the sizing-derived partition count so an added broker still gets partitions). There's no floor dial. `minReplicaCount` is always the resolved `kafka.replicas`. `kafka.autoscaling.pollingInterval` (default 30s) covers both triggers.

## Scale-in

Scale-in is off by default, on purpose. Until it's on, the HPA's scale-down direction is disabled outright (`autoscaling/v2` `selectPolicy: Disabled`). Shrinking the pool means Strimzi has to evacuate the departing broker's partitions first (the `remove-brokers` auto-rebalance template), and that's worth testing before it runs unattended.

Turn it on in `deployment.yaml`, for `kafka.provider: strimzi` alone:

```yaml
kafka:
  broker_scale_in:
    enabled: true
    cooldown_seconds: 1800   # the HPA scale-down window, 1 to 3600
```

`scripts/resolve_sizing.py` writes it into the kafka chart as `kafka.autoscaling.scaleIn.enabled` and `kafka.autoscaling.scaleIn.cooldownSeconds`. Off, it writes neither, and the chart's off default holds.

Scale-in has a precondition. The leaving brokers' data must fit on the remaining ones under Cruise Control's `DiskCapacityGoal` (hard, default threshold 0.80). `skipHardGoalCheck: true` does NOT lift that check, because it reports an infeasibility and not a preference. A low `retention.ms` won't free room fast enough by itself either. Kafka never expires an open, active log segment, so `segment.bytes` must be small enough for the active segment to roll, and for retention to then delete it, before you attempt a scale-in.

`kafka.rebalancing.goals` (`helm/charts/kafka/values.yaml`) is Strimzi's documented default priority order for both rebalance templates, with two leader goals moved up. We moved `LeaderReplicaDistributionGoal` and `LeaderBytesInDistributionGoal` up to run straight after the seven hard goals, instead of second-last and last of eighteen. The unmodified default order left leader skew at 57% on a live 6-broker add-brokers rebalance, with one broker holding 17 leaders against a mean of 29.8. Cruise Control satisfies earlier-listed goals first and rarely reaches goals that far down the list. Nothing is removed and nothing else is reordered. Set `kafka.rebalancing.goals: []` to fall back to Cruise Control's untouched default.

Argo: KEDA owns the `KafkaNodePool`'s `spec.replicas` once scaling, so Argo's `selfHeal` must not revert it as drift. It's the same `ignoreDifferences` plus `RespectIgnoreDifferences=true` pattern `dfe-layer2-apps` uses for Deployment replicas. The kafka chart's Application lives in `argocd/appsets/layer2-data.yaml` (`dfe-layer2-data`), not `layer-scale.yaml` (the Strimzi operator alone), and has that `ignoreDifferences` entry on `KafkaNodePool`'s `/spec/replicas`.

Redpanda: the Redpanda CRD (the operator `versions.yaml` pins as `redpanda-operator`, `cluster.redpanda.com/v1alpha2`) declares no scale subresource, so Kubernetes' HPA can't target it. `spec.clusterSpec.statefulset.replicas` exists as a plain field, and scaling it stays manual until Redpanda ships one.

MSK: [`aws-msk-operations.md`](aws-msk-operations.md#broker-count-autoscaling) covers its CloudWatch-alarm-and-Lambda scaler.
