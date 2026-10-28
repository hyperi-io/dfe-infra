---
sidebar_position: 5
sidebar_label: Monitoring
---

# Monitoring

Three questions, three places.

| Question | Where |
|---|---|
| Is this process alive, and can it work right now? | `/livez`, `/readyz` and `/metrics` on every DFE service. HyperDX keeps its own `/health` |
| What has the stack been doing? | The `dfe.otel_*` tables in ClickHouse, read in HyperDX |
| Did an event actually get through? | `make post` on Docker. On Kubernetes, `dfe-ops stack-deploy` ends by sending an event through to ClickHouse and checking the stack's own telemetry landed |

## Where the telemetry goes

The DFE apps push metrics and traces over OTLP to one OpenTelemetry Collector. Container logs reach the same collector from stdout. The collector writes `otel_logs`, `otel_traces` and the `otel_metrics_*` tables into the `dfe` database, and HyperDX reads them there. Telemetry leaves by a different door from ingested data, so a broken ingest pipeline cannot take the reporting on it down.

Every app also serves Prometheus `/metrics`, whatever its OTLP push is set to. A Prometheus estate and the HyperDX estate are both served.

To send telemetry somewhere else, set the destination:

- Kubernetes: `telemetry.mode` in `argocd/values/common.yaml` takes `hyperdx` (the default), `receiver`, `external` or `prometheus`. `external` needs `telemetry.externalEndpoint`.
- Docker: `DFE_OTEL_EXPORTER_ENDPOINT`. Empty exports nothing.

A misspelt `telemetry.mode` fails silently, because nothing rejects an unknown value. An app with an empty endpoint exports nothing.

## Dashboards

Eight ship with the engine: Overview, DFE Services, DFE Pipeline Health, DFE Throughput, DFE Kafka, and three on ClickHouse (Health, Internals, Overview). They arrive in HyperDX read-only, provisioned within a minute. To change one, export it and import your own copy. A dashboard seeds only for a team that holds the sources it reads, so a tenant's team does not get the platform's.

## Read next

- [How the Compose stack reports on itself](../dfe-docker/observability.md): the health ports, which services push, and what a passing self test proves.
- [The observability standard](../dfe-engine/deployment/observability-standard.md): the contract for both targets, and the acceptance checks.
