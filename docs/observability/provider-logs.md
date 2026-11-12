# Provider logs in dfe-hyperdx

Self-monitoring has two feeds: the stack's own OTLP, and the infra provider's own logs (the EKS audit log, CloudTrail, a Rancher API server audit log). Both land in dfe-hyperdx.

Receivers in the otel collector read the provider's cloud log service, or the Rancher audit file, and send the rows through the collector gateway to the otel database dfe-hyperdx reads. They bypass DFE ingest: no fetcher source, no customer table.

Every row carries `cloud.provider` and `dfe.provider_log.source`, so dfe-hyperdx can filter on them. The rows are generic OTel logs, not typed columns.

## Flavours

`clusterTelemetry.providerLogs.flavour` defaults to `none`.

| Flavour | Source | Receiver | State |
|---|---|---|---|
| `aws` | EKS control-plane log group, CloudTrail's CloudWatch group | `aws_cloudwatch` (alpha) | Built. The cloud root sets it when `telemetry.sink` is `otel` and at least one of the two groups exists. |
| `rancher` | kube-apiserver audit log on the control-plane nodes | `filelog` | Built. The deployer sets the flavour. |
| `azure` | Diagnostic settings through an Event Hub | `azure_event_hub` | Chart only. No Azure cloud root exists. |
| `gcp` | A log sink through a Pub/Sub subscription | `googlecloudpubsub` | Chart only. No GCP cloud root exists. |

The receivers run in a workload of their own, `<project>-otel-collector-provider-logs`, with their own ServiceAccount. `aws_cloudwatch` makes an STS identity call and dereferences the result without checking it. A failed call exits the collector process. In its own pod that restarts the provider-logs container and leaves the gateway alone.

## AWS

The cloud root outputs the flavour and the two log group names. Bootstrap writes them to the cluster secret, `layer2-data.yaml` passes them to the chart, and the receiver reads the groups through a Pod Identity role scoped to those two.

Under the `otel` sink both groups keep `telemetry.aws.forced_retention_days` in the dial. That defaults to CloudWatch's shortest period and can be set to another it accepts. Under the `cloudwatch` sink the groups keep `retention_days` and nothing here reads them.

`telemetry.aws.eks_log_types` picks the EKS streams. It defaults to `audit`.

The role is created under the account's permissions boundary when the cloud guard configures one, and the boundary has to allow `logs:FilterLogEvents`. Without it the role's own grant has no effect, the receiver is denied on every poll, and no row arrives. The cloud guard's preflight refuses a run whose boundary lacks it.

The receiver keeps its read offsets on a small PersistentVolumeClaim, one replica, replaced with `Recreate`. A replacement pod resumes from the checkpoint. The lookback (`aws.initialLookback`) only applies when there is no checkpoint yet.

Recovery after an outage has three limits:
- The checkpoint. Events before it are already delivered.
- The CloudWatch retention. Events older than it are gone, whatever the checkpoint says.
- The exporter retry ceiling. If the gateway is down for longer than `maxElapsedTime`, the queued batches are dropped.

A missing group or a role without access is logged by the receiver on every poll and nothing more.

## Rancher

The audit log path, the node selector and the tolerations all come from values. The defaults are RKE2's: `/var/lib/rancher/rke2/server/logs/audit.log` and the `node-role.kubernetes.io/control-plane` label. Another distribution sets its own.

Turning API server auditing on is the operator's job. RKE2 writes that file only once an audit policy is set. With auditing off the file isn't there, the receiver finds nothing to read, and the collector stays up.

## Why this path

A dfe-fetcher source on the normal data path doesn't fit. dfe-fetcher reads only a static AWS key pair, so it can't use Pod Identity. It would also put self-monitoring on the managed ingest path, and self-monitoring has to keep reporting when ingest breaks.

The collector already ships a receiver per cloud, and its Go SDK picks up Pod Identity natively.

Under the `otel` sink, CloudWatch is a catch-up window and the otel database holds the history, so retention is pre-trimmed. Hence the shortest period by default.

What it costs. The AWS receivers are alpha, so we pin the collector and the cloud cycle tests it. The rows are generic OTel logs. GuardDuty and Security Hub findings have no receiver, so they stay out.

## What the cloud cycle checks

The integration smoke test requires a fresh EKS audit row from the AWS flavour. CloudTrail is reported and never fails the run, because management events are sparse on a quiet account.
