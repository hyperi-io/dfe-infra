# Provider logs in HyperDX

HyperDX already received the stack's own OTLP. Provider logs add a second self-monitoring feed: EKS audit logs, CloudTrail and Rancher API server audit logs.

Each provider's native log service feeds a receiver in the otel collector. The collector writes rows to the otel database HyperDX reads. Provider logs bypass DFE ingest without a fetcher source or customer table.

Every row carries `cloud.provider` and `dfe.provider_log.source` for filtering in HyperDX. Rows use the generic OTel log format without typed columns.

## Flavours

`clusterTelemetry.providerLogs.flavour` defaults to `none`.

| Flavour | Source | Receiver | State |
|---|---|---|---|
| `aws` | EKS control-plane log group, CloudTrail's CloudWatch group | `aws_cloudwatch` (alpha) | Built. The cloud root sets it under `telemetry.sink: otel`. |
| `rancher` | kube-apiserver audit log on the control-plane nodes | `filelog` | Built. The deployer sets the flavour. |
| `azure` | Diagnostic settings through an Event Hub | `azure_event_hub` | Chart only. No Azure cloud root exists. |
| `gcp` | A log sink through a Pub/Sub subscription | `googlecloudpubsub` | Chart only. No GCP cloud root exists. |

The receivers run separately from the gateway in `<project>-otel-collector-provider-logs`. `aws_cloudwatch` dereferences an STS identity call's result without checking it. A failed call exits the collector. This restarts the provider-logs pod without restarting the gateway.

## AWS

The cloud root outputs the two log group names and the flavour. Bootstrap writes them to the cluster secret. `layer2-data.yaml` passes them to the chart. The receiver accesses the two groups through a Pod Identity role scoped to them.

CloudWatch retention is `telemetry.forced_retention_days`, its minimum retention period. `telemetry.aws.eks_log_types` selects the EKS log types and defaults to `audit`.

The receiver logs missing groups or denied role access on every poll without exiting.

## Rancher

The audit log path, node selector and tolerations come from values. RKE2 defaults use `/var/lib/rancher/rke2/server/logs/audit.log` and the `node-role.kubernetes.io/control-plane` label. Other distributions need their own audit log path, node selector and tolerations.

The operator must turn on API server auditing. RKE2 writes the audit log only when an audit policy is set. With auditing off, the file is absent. The collector stays up when the receiver has no file to read.

## Why this path

dfe-fetcher supports only a static AWS key pair and cannot use Pod Identity. A reserved dfe-fetcher source would route self-monitoring through managed ingest. Self-monitoring must keep reporting when ingest breaks.

The collector ships a receiver per cloud. Its Go SDK supports Pod Identity.

CloudWatch keeps only a catch-up window and the otel database holds the history, so retention is pre-trimmed.

The AWS receivers are alpha. The collector version is pinned and tested by the cloud cycle. GuardDuty and Security Hub findings are excluded because they have no receiver.
