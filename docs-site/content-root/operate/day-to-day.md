---
sidebar_position: 6
sidebar_label: Day to day
---

# Day to day and troubleshooting

Start with the target you run.

| Target | Run it | Fix it |
|---|---|---|
| Docker Compose | [Operating the stack](../dfe-docker/operating.md): exposure, resource limits, secrets, what survives `make clean` | [Troubleshooting](../dfe-docker/troubleshooting.md) |
| Kubernetes | [Upgrades](../dfe-infra/deployment/upgrades.md), [autoscaling](../dfe-infra/autoscaling.md) | [The troubleshooting toolbox](../dfe-infra/deployment/toolbox.md): an opt-in shell inside the cluster's network |
| AWS | [DFE on AWS, operations](../dfe-infra/deployment/aws-operations.md) | The toolbox again, on an on-demand bastion |

## When events are accepted and nothing lands

The ingest edge returns 2xx once it has accepted an event, which says nothing about ClickHouse. [Events are accepted but nothing lands](../dfe-docker/troubleshooting.md#events-are-accepted-but-nothing-lands) walks the order to check on Compose: the loader's readiness, the table, the topic, then the dead letters. The loader's own page covers [its subcommands, metrics and dead-letter queue](../dfe-loader/OPERATIONS.md).

## Where the logs are

On Compose with self-monitoring on, `docker compose logs dfe-loader` fails with "configured logging driver does not support reading logs". The lines are in ClickHouse. Search the `otel_logs` source in HyperDX, or query `dfe.otel_logs` by `ServiceName`. Kubernetes ships stdout to the same table through the collector's daemonset.

## Passwords

Reset the `admin` and `breakglass` passwords in the console, under Settings, Account Management, Security accounts. [Users and OIDC](users-and-oidc.md) says where the first logins are.
