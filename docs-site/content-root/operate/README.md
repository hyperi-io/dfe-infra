---
sidebar_position: 1
sidebar_label: Operate
---

# Operate

DFE is deployed. Now it needs people, data and a way to tell it is healthy.

## The first week

In this order:

1. Sign in as `admin` and change the password. Where the logins are: [Users and OIDC](users-and-oidc.md).
2. Connect your identity provider, so your people sign in as themselves. Same page.
3. Add a source. Events land in `main` until you do. [A source](../dfe-engine/data-plane/source.md) is one stream of events, with its routing, its table and its detections in one definition.
4. Reshape the stream if the raw record is not enough: [Transforms](transforms.md).
5. Write the detections: [Detections and hunts](detections-and-hunts.md).
6. Find out how you will know when something breaks: [Monitoring](monitoring.md).

## Sources

A source has one origin. Either the receiver matches pushed events to it, or a fetcher pulls them.

| Origin | How events arrive | Start at |
|---|---|---|
| Pushed | Your agents and devices send to dfe-receiver, which terminates their protocols at the edge | [The source definition](../dfe-engine/data-plane/source-definition.md), then [webhook callers](../dfe-receiver/WEBHOOK-SETUP.md) for products that push alerts |
| Fetched | dfe-fetcher dials out to a SaaS or cloud API on an interval | [Cloud and SaaS setup guides](../dfe-fetcher/cloud-setup/README.md) |

A source is active, dormant or disabled. [Storing and operating a source](../dfe-engine/data-plane/source-registry.md) says what each state does to its table. [Data evolution](../dfe-engine/data-evolution.md) is how a stream goes from landing raw to having its own columns, one field at a time.

## Everything else

| You want to | Go to |
|---|---|
| Reshape events on the way in | [Transforms](transforms.md) |
| Let people in, and decide what they can do | [Users and OIDC](users-and-oidc.md) |
| Search, build dashboards, define rules and hunts | [The console](../dfe-ui/console-and-the-flow.md). Its menu runs in the order the data moves: Observe, Data flow, Detect, Stack, Access |
| Run rules on a schedule | [Detections and hunts](detections-and-hunts.md) |
| Watch the stack's own health | [Monitoring](monitoring.md) |
| Grow it | [Autoscaling](../dfe-infra/AUTOSCALING.md). Pods scale on KEDA on every target, and node scaling differs per target |
| Keep it running, and fix it when it is not | [Day to day and troubleshooting](day-to-day.md) |
| Move to a newer release | [Upgrades](../dfe-infra/deployment/upgrades.md), and the [rollback runbook](../dfe-infra/deployment/upgrade-rollback.md) |
| Keep the raw copy of every record | Set `archive: true` in a source's flow. [The source flow](../dfe-engine/data-plane/source-flow.md) says what that needs |
