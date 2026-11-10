---
sidebar_position: 1
sidebar_label: Overview
slug: /overview
---

# DFE documentation

Land it. Use it. Scale it. The Data Fusion Engine (DFE) does what you used Splunk, Elastic and a lakehouse for, without the limitations. Every source in, the whole picture out, from gigabytes to petabytes, in your own environment.

DFE 2.2 is the first open-source release of a platform that has run in production since 2018. [How we got here](history.md) explains the history and the version numbers.

Under the hood, DFE is a product suite an organisation deploys to run its own data pipeline. Events arrive over HTTP or HTTPS, travel through Kafka or a direct gRPC hop, and land in ClickHouse, where the DFE console's Search finds them. DFE deploys onto any Kubernetes cluster, on-premises or in a cloud, or under Docker Compose on a single host.

Every page on this site comes from a suite repository's own `docs/`, at the version one DFE release pins. The version menu switches the whole suite between releases, and each repository has its own section in the sidebar. The console's Search, Discovery and Dashboards pages sit inside dfe-ui's section.

```mermaid
flowchart LR
  S[JSON over HTTP or HTTPS] --> R[dfe-receiver]
  R -->|Kafka| L[dfe-loader]
  R -->|direct gRPC| L
  L --> C[(ClickHouse)]
  C --> U[DFE console: Search, Discovery, Dashboards]
```

That is the default data path. The leaner deployment profiles skip Kafka and send events from the receiver to the loader over gRPC. Every component also sends its own logs and metrics over OpenTelemetry into ClickHouse, so the console's Dashboards show the stack's health before any data arrives.

## Who does what

| Repository | Role |
|---|---|
| dfe-engine | The configuration control plane. It decides what each component does and writes that to a git deploy repo. |
| dfe-ui | The console over the engine. |
| dfe-infra | Deploys the backing services and the components onto Kubernetes, through Argo CD. |
| dfe-docker | Runs the same stack under Docker Compose. |

Start with the [quickstarts](quickstart/README.md) for Docker, Kubernetes on-premises and AWS.
