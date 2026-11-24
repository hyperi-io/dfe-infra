---
sidebar_position: 5
sidebar_label: Monitoring
---

# Monitoring

Three questions, three places.

| Question | Where |
|---|---|
| Is this process alive, and can it work right now? | `/livez`, `/readyz` and `/metrics` on every DFE service. The console's search service keeps its own `/health` |
| What has the stack been doing? | The `dfe.otel_*` tables in ClickHouse, read in the console |
| Did an event actually get through? | `make post` on Docker. On Kubernetes, `dfe-ops stack-deploy` ends by sending an event through to ClickHouse and checking the stack's own telemetry landed |

## Where the telemetry goes

The DFE apps push metrics and traces over OTLP to one OpenTelemetry Collector. Container logs reach the same collector from stdout. The collector writes `otel_logs`, `otel_traces` and the `otel_metrics_*` tables into the `dfe` database, and the console reads them there. Telemetry goes through the collector, not the ingest path, so a broken ingest pipeline doesn't stop the reporting on it.

Every app also serves Prometheus `/metrics`, whatever its OTLP push is set to. So it works with an existing Prometheus as well as the console.

To send telemetry somewhere else, set the destination:

- Kubernetes: `telemetry.mode` in your deploy repo's `infra/common.yaml` takes `hyperdx` (the default), `receiver`, `external` or `prometheus`. `external` needs `telemetry.externalEndpoint`.
- Docker: `DFE_OTEL_EXPORTER_ENDPOINT`. Empty exports nothing.

A misspelt `telemetry.mode` fails silently, because nothing rejects an unknown value. An app with an empty endpoint exports nothing.

### What reaches HyperI

DFE sends HyperI no telemetry from a deployment. The stack's logs, metrics and traces go to the destination you set above, your ClickHouse by default.

The one call to HyperI is a release check, made at startup by dfe-engine and each Rust app. The console and the hunt runner make none. The check POSTs the product name, the version, the OS, the CPU architecture and an install id to `releases.hyperi.io`, and logs when a newer version exists. Unless you set `instanceId`, the id is a one-way UUIDv5 of the cluster's CA certificate and the namespace on Kubernetes, or of the machine id on a host. Failing both, it's a random UUID, kept on disk where the app can write one. Deployment names and your data are never sent, and a failed check costs one warning line.

The check is on by default, and one setting turns it off in all of them:

- Kubernetes: `versionCheck.enabled: false` in your deploy repo's `infra/common.yaml`.
- Docker: `DFE_VERSION_CHECK_ENABLED=false` in `.env`.

`versionCheck.sendInstanceId: false`, or `DFE_VERSION_CHECK_SEND_INSTANCE_ID=false` on Docker, keeps the check and drops the id. The console's usage reporting, which goes to a third party, is off unless you turn it on. The third-party images, ClickHouse and Kafka among them, are set separately, and this setting doesn't reach them.

## Dashboards

Eight ship with the engine: Overview, DFE Services, DFE Pipeline Health, DFE Throughput, DFE Kafka, and three on ClickHouse (Health, Internals, Overview). They arrive in the console read-only, provisioned within a minute. To change one, export it and import your own copy. A dashboard seeds only for a team that holds the sources it reads, so a tenant's team doesn't get the platform's.

## Read next

- [How the Compose stack reports on itself](../dfe-docker/observability.md): the health ports, which services push, and what a passing self test proves.
- [The observability standard](../dfe-engine/deployment/observability-standard.md): the contract for both targets, and the acceptance checks.
