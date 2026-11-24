# gNMI telemetry from network devices

Network devices stream telemetry over gNMI. The otel collector chart can run gnmic, which subscribes to them and pushes what it reads as OTLP metrics to the collector gateway on 4317. The gateway writes the otel tables dfe-hyperdx reads, so the rows bypass DFE ingest: no fetcher source, no customer table.

`gnmic.enabled` defaults to `false`. Enabled, the chart renders one Deployment, `<project>-otel-collector-gnmic`, in the collector's namespace. It has one replica, because a second would read every device twice. Its ServiceAccount is its own and mounts no token, so the cluster-wide read role the gateway's account holds stays out of reach.

## Values

Name each device under `gnmic.targets`, keyed by the name its rows carry in the `source` attribute. The render fails with none, because gnmic exits when it has nothing to subscribe to.

```yaml
gnmic:
  enabled: true
  targets:
    core-router-1:
      address: core-router-1.example.com:57400
      tls-ca: /etc/gnmic/ca.crt
  credentials:
    secretName: device-login
```

| Value | Does |
|---|---|
| `targets` | gnmic's target keys: `address`, `insecure`, `skip-verify`, `tls-ca`, `username` and the rest of its target settings. |
| `credentials.secretName` | A Secret with `username` and `password` keys. gnmic uses them for each target that sets neither. Empty sends none. |
| `subscriptions` | gnmic's subscriptions map. The default samples `/interfaces/interface/state/counters` every 30s. |
| `output.metricPrefix` | The prefix on every metric name. |
| `output.counterPatterns` | Regexes over a value's name. A match exports as a cumulative sum and the rest as gauges. |

The chart never creates the login Secret. The deployment's secret store supplies it. A certificate authority or client certificate a target names has to be mounted at the path it gives, which the chart does not do either. Devices are reached from the collector's namespace, which carries no network policy.

Gauges go to `otel_metrics_gauge` and counters to `otel_metrics_sum`, under `ServiceName` `gnmic`. `MetricName` is the prefix, then the gNMI path with `/` and `-` as `_`, and the target's name and the path's keys are attributes. dfe-docker runs the same service. Its `make test-gnmi` starts the lemming device emulator and asserts rows in `otel_metrics_gauge`.
