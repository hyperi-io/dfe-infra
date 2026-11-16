# The otel collector chart

`helm/charts/otel-collector` runs the collector that carries the stack's own telemetry: OTLP from the services, scrapes of the cluster and its dependencies, and each node's container logs. Everything ends in the `otel` tables of ClickHouse, which dfe-hyperdx reads. The provider's own logs run in a third workload, described in [provider logs](provider-logs.md).

## Workloads

| Workload | Runs | Does |
|---|---|---|
| `<project>-otel-collector-gateway` Deployment | `gateway.replicas` pods | Takes OTLP on 4317 (gRPC) and 4318 (HTTP), runs the cluster, ClickHouse, Kafka and Prometheus scrapes, and writes ClickHouse. Serves what it received on a Prometheus endpoint, 8889. |
| `<project>-otel-collector-daemonset` DaemonSet | one pod per node | Reads container logs and host metrics, and kubelet stats where enabled, then pushes OTLP to the gateway. |
| `<project>-otel-collector-provider-logs` | one pod, or a DaemonSet on Rancher | The provider's log service. A failed receiver restarts only this pod. |

The gateway's `exporters.clickhouse` is the default sink. `exporters.hyperdx` pushes OTLP to a dfe-hyperdx collector instead and stays off, because the dfe-hyperdx image has no OTLP listener.

The gateway has an init container that waits for dfe-engine's `/readyz`, up to `waitForEngine.timeoutSeconds`. The engine creates the otel tables, and the exporter's `create_schema` is off, so its only SQL is INSERT. The collector image has no shell, so the wait runs on busybox's `wget`. It renders only while the gateway talks to ClickHouse.

With `gateway.replicas` of 2 or more the chart adds a PodDisruptionBudget (`gateway.pdb`, `maxUnavailable` 1). A budget on one replica would block every drain, so nothing renders below 2. The DaemonSet has none.

`gateway.debugExporter` adds the `debug` exporter to the traces and logs pipelines. It prints a line per batch, so it is off.

## Memory and retry limits

`limits.memory` is the only memory setting a workload takes, in `gateway.resources`, `daemonset.resources` and the provider-logs `resources`. The chart derives the rest from it (`_memory.tpl`):

- `memory_limiter` `limit_mib` is 80% of it, and `spike_limit_mib` is 20%.
- `GOMEMLIMIT` is the same 80%. The Go runtime otherwise sizes its heap against the node, not the container.
- The DaemonSet's and provider-logs' export queue holds one batch per 4 MiB of the limit. A batch is 1024 records.

Resizing a container resizes what it may buffer. The chart fails to render if the limit is missing.

Every exporter has a retry ceiling, and data queued past it is dropped. The alternative is a collector that retries behind a full queue until it is OOMKilled.

| Exporter | Ceiling |
|---|---|
| Gateway to ClickHouse (and the hyperdx push) | `gateway.export.maxElapsedTime`, 120s |
| DaemonSet to the gateway | `daemonset.export.maxElapsedTime`, 60s |
| Provider-logs to the gateway | `clusterTelemetry.providerLogs.deployment.export.maxElapsedTime`, 60s |

## The node agent

- It runs as root. Pod log files on the node are root-owned, so a non-root `filelog` receiver opens none. The root filesystem stays read-only and all capabilities are dropped.
- It mounts `/var/log/pods` and the node root at `/hostfs`, both read-only, and a checkpoint directory that is writable. The `host_metrics` filesystem and disk scrapers read through `/hostfs`, and without it they report the container's own overlay.
- It tolerates every taint. A node it cannot run on reports no host metrics.
- `filelog` keeps its read offsets in `daemonset.checkpoint.hostPath` on the node, so they outlive the pod. Without a checkpoint, a restart re-ingests every pod log on the node, which fills the export queue and OOMs the collector. `start_at: end` applies to the first run only.
- `daemonset.priorityClass` ranks the pod above pods that name no class, so a full node evicts one of those to fit it. The value stays under the 1,000,000,000 ceiling on a user-defined class, and the API server refuses a change to it, so a new value means a new name. `create: false` uses a class the cluster already has, and with it an empty `name` renders no class.
- The container name is `otel-collector`, and the `filelog` exclude pattern is built from the same helper. Kubelet builds the log path from the container name, and a mismatch makes the collector ship its own logs.
- The `kubeletstats` receiver runs with the `receiver.kubeletstats.cpuUsageScrapeBased` gate off. That keeps CPU usage on the kubelet's `UsageNanoCores`. The gate derives it from `cpu.time` and drops the first scrape after a start.

Probes use the `health_check` extension on `healthCheckPort`. Each config enables the extension in `service.extensions`, and a probe against a collector without it dials a closed port and kills the pod.

## Cluster telemetry

The pre-built HyperDX dashboards are the same on every tier, so the cost of collection is what varies. The expensive scrapers are switched per tier in `argocd/values/profile-*.yaml`.

| Block | Where | Default | Notes |
|---|---|---|---|
| `k8sCluster` | gateway | on | Node conditions, allocatable, replica counts and pod phase. One instance per cluster, because it reads the API server. |
| `kubeletStats` | DaemonSet | off in the chart, on in single, scale and mesh | Container series multiply by every container on every node, so slim leaves it off. `volume` is omitted: it is a series per mounted volume. |
| `clickhouse` | gateway | on | `sql_query` over `system.metrics`, `system.events` and `system.parts`, as the collector's own user (`clickhouse.serviceUsers.otelWriter`, never the admin account), whose role grants those reads. The `:9363` Prometheus endpoint is not used: the clickhouse-cluster chart does not expose it and managed ClickHouse does not offer it. |
| `kafka` | gateway | off in the chart, on in single and scale | `kafka_metrics` speaks the Kafka protocol to `kafka.bootstrapServers`, so one config covers a single broker, Strimzi and managed brokers. Broker-side counts complement the consumers' own `rdkafka_*` lag, which stops reporting when a consumer dies. Brokerless tiers (slim, mesh) have nothing to scrape. |
| `mskOpenMonitoring` | gateway | on, renders only for `kafka.external.provider: msk` | The JMX and node exporters AWS runs on every broker, which cover JVM and OS metrics that `kafka_metrics` does not. The targets are the brokers' own hosts (`kafka.brokerHosts`). |
| `selfTelemetry` | gateway | on | The collector's own `otelcol_*` metrics (queue depth, enqueue and send failures), served on loopback and scraped back into the pipeline. |
| `dfeServices` | gateway | on | Scrapes the DFE services' `/metrics` by the `prometheus.io/*` pod annotations, so a new service needs no edit. |
| `platformComponents` | gateway | on | The ClickHouse, Strimzi and CloudNativePG operators' controller metrics, found by Service endpoint in `operatorNamespaces`. |

Kafka's SASL user is `dfe-kafka-user`, and the kafka chart has to project its Secret into the collector's namespace through `user.password.extraAppNamespaces`. The mechanism derives from the managed provider's table, else SCRAM-SHA-512. A managed provider always dials TLS, and `kafka.tls.enabled` covers a self-run broker on a TLS listener.

`platformComponents` finds the operators by Service endpoint and not by pod annotation. The ClickHouse chart's scraping annotations omit `prometheus.io/scheme` while its port serves HTTPS, so an annotation-driven scrape fails the TLS handshake. The job sends the collector's ServiceAccount token, so it stays inside `operatorNamespaces`.

KEDA needs no receiver. The operator pushes its own metrics over OTLP to the gateway, enabled in `argocd/appsets/layer1-addons.yaml`.

A service that pushes the same metrics over OTLP with the pod as `service.instance.id`, and is also scraped, lands in the `otel_metrics_*` tables twice, and a sum counts it twice. `dfeServices.pushedCharts` is an anchored regex over the pods' `helm.sh/chart` label (`<chart>-<version>`) and drops those pods from the scrape. A chart belongs there only once its image pushes with replica identity, because before that the push merges every replica into one series. Empty drops nothing.

## Rows and writes

The container runtime's log envelope has no severity, so without `logSeverity.enabled` every row stores an empty `SeverityText` and no severity filter matches. The `filelog` receiver routes each line by format (JSON, logfmt, klog, ClickHouse, bracketed, a bare token). A line matching none keeps an empty severity and is never guessed at. Every parser is `on_error: send`, so a regex miss does not drop the record.

`exporters.clickhouse.connectionParams` merges into the exporter's DSN. `type_json_skip_duplicated_paths: "1"` is set because a log body that repeats a JSON path fails its whole batch with code 117, and a retry never drains it. The skip keeps the first occurrence and the raw `Body` column still holds the original text.

`clickhouse.tls` follows the shared block in `argocd/values/common.yaml`: on when it is enabled and there is a CA to sign with (an `issuerRef`, or the internal CA). An in-cluster server then serves the native protocol on `clickhouse.tls.nativePort`, and the collector verifies it against the CA copy in `ca.secretName`. An external server keeps its own port and trust: set `ca.configMapName` for a corporate bundle, or `verify: false` for a certificate that cannot be verified.

## OTLP ingest from outside the cluster

With `otel.ingress.enabled`, the gateway adds a second OTLP/HTTP receiver on `otel.ingress.port` (4319) that admits only a bearer token, and feeds the same pipelines. 4317 and 4318 stay as they are for in-cluster senders. The token comes from the deployment's secret store (`otel.ingress.auth.remoteKey`, `property`) into a Secret the gateway mounts. Each line's first word is a valid token, so a rotation can hold the old and new at once, and the extension re-reads the file without a restart. The switch is in `argocd/values/common.yaml` or the deploy repo's `infra/common.yaml`. See [shared values](../deployment/values-shared.md#telemetry).
