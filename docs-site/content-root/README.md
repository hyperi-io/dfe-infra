---
sidebar_position: 1
sidebar_label: Overview
slug: /overview
---

# Data Fusion Engine overview

Land it. Use it. Scale it. The Data Fusion Engine (DFE) does what you used Splunk, Elastic and a lakehouse for, without the limitations. Every source in, the whole picture out, from gigabytes to petabytes, in your own environment.

DFE 2.2 is the first open-source release of a platform that has run in production since 2018. [How we got here](history.md) explains the history and the version numbers.

Under the hood, DFE is a data pipeline you run yourself. Your agents and shippers send events in over HTTP or HTTPS. They travel over a Kafka message bus, or a low-latency gRPC hop, and land in ClickHouse, where you search them in the DFE console. It runs on any Kubernetes cluster, on-premises or in your cloud account, or on Docker on one host.

> **Coming from Splunk or Elastic?** DFE takes every field without a mapping first, so a source changing upstream breaks nothing. It doesn't index every byte or keep a raw copy of every event either, and every record carries an `_org_id`, so who sees what is built in. [Coming from Splunk or Elastic](coming-from.md) says what's different, why, and what it does for you, and [coming from a lakehouse](coming-from-lakehouse.md) does the same for Snowflake, Databricks, BigQuery and Athena. Read yours before the rest.

Every page here comes from a suite repository's own `docs/`, at the version one DFE release pins. The version menu switches the whole suite between releases. Each repository has its own section in the sidebar.

Data moves one way, from your senders through Kafka or gRPC into ClickHouse, where the DFE console reads it.

```mermaid
---
config:
  theme: base
  look: neo
  fontFamily: Inter, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif
  flowchart:
    curve: linear
    nodeSpacing: 40
    rankSpacing: 64
    padding: 18
    wrappingWidth: 280
    htmlLabels: true
  elk:
    preset: modelOrder
  themeVariables:
    fontSize: 15px
    lineColor: "#64748B"
    primaryTextColor: "#0F172A"
    clusterBorder: "#0072B2"
    edgeLabelBackground: "#FFFFFF"
  themeCSS: >-
    p{margin:0}
    .node[data-look] rect,.node[data-look] path,.node[data-look] polygon,.node[data-look] .outer-path{filter:drop-shadow(0 1px 2px rgba(15,23,42,.10)) drop-shadow(0 6px 14px rgba(15,23,42,.10))}
    .node rect{rx:10px;ry:10px}
    .cluster rect{rx:16px;ry:16px}
    .node .label div{min-width:150px}
    .node.decision .label div{min-width:0}
    .cluster[data-look] rect{filter:none}
    .nodeLabel{line-height:1.35}
    .nodeLabel b{font-weight:600;font-size:15px;letter-spacing:-0.01em}
    .nodeLabel span{font-size:12.5px;font-weight:400;color:#334155}
    .cluster-label span.nodeLabel{display:inline-block;background:#FFFFFF;color:#0B2A3F;border:1.5px solid #0072B2;border-radius:999px;padding:2px 12px;font-size:11.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}
    .flowchart-link{stroke-width:1.75px}
    .labelBkg{background:transparent!important}
    span.edgeLabel{display:inline-block;background:#FFFFFF!important;color:#1E293B;border:1px solid #94A3B8;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:500}
    span.edgeLabel p{background:transparent!important}
    .label-icon{width:20px;height:20px;vertical-align:-4px;margin-right:6px}
    .ingest .label-icon{color:#0E8A7D}
    .xform .label-icon{color:#7B4FB0}
    .load .label-icon{color:#0072B2}
    .bus .label-icon{color:#A8517F}
    .store .label-icon{color:#00785A}
    .ui .label-icon{color:#D9480F}
    .ext .label-icon{color:#64748B}
---
flowchart TB
  accTitle: L2 Services -- DFE data path
  accDescr: Senders post JSON to dfe-receiver and dfe-fetcher pulls from APIs, databases and files. Both write to a source topic that dfe-loader inserts into ClickHouse, optionally through a transform, and the DFE console reads ClickHouse.
  S("<b>Your senders</b><br/><span>JSON over HTTP or HTTPS</span>")
  F("<b>dfe-fetcher</b><br/><span>pulls from APIs, databases and files</span>")
  R("<b>dfe-receiver</b><br/><span>takes what is sent</span>")
  K1("<b>Source topic</b><br/><span>*_land</span>")
  T("<b>dfe-transform</b><br/><span>optional</span>")
  K2("<b>Transformed topic</b><br/><span>*_load</span>")
  L("<b>dfe-loader</b><br/><span>inserts into tables</span>")
  C[("<b>ClickHouse</b>")]
  H("<b>DFE console</b><br/><span>search, dashboards, rules</span>")
  S --> R
  R -->|Kafka| K1
  F --> K1
  K1 --> L
  K1 -.-> T -.-> K2 -.-> L
  R -->|direct gRPC| L
  R -.->|direct gRPC| T -.->|direct gRPC| L
  L --> C
  C --> H
  class S ext
  class R,F ingest
  class K1,K2 bus
  class T xform
  class L load
  class C store
  class H ui
  classDef ext fill:#F4F6F8,stroke:#94A3B8,stroke-width:1.5px,stroke-dasharray:5 4,color:#0F172A
  classDef ingest fill:#FFFFFF,stroke:#0E8A7D,stroke-width:3px,color:#0F172A
  classDef xform fill:#FFFFFF,stroke:#7B4FB0,stroke-width:3px,color:#0F172A
  classDef load fill:#FFFFFF,stroke:#0072B2,stroke-width:3px,color:#0F172A
  classDef bus fill:#FFFFFF,stroke:#A8517F,stroke-width:3px,color:#0F172A
  classDef store fill:#FFFFFF,stroke:#00785A,stroke-width:3px,color:#0F172A
  classDef ui fill:#FFFFFF,stroke:#D9480F,stroke-width:3px,color:#0F172A
  classDef decision fill:#FFFFFF,stroke:#9A6700,stroke-width:3px,color:#0F172A
  classDef error fill:#FFFFFF,stroke:#C2410C,stroke-width:3px,color:#0F172A
  classDef control fill:#FFFFFF,stroke:#334155,stroke-width:3px,color:#0F172A
  classDef part fill:#FFFFFF,stroke:#94A3B8,stroke-width:1.5px,color:#0F172A
```

*Figure 1. L2 Services -- DFE data path.* Dotted lines are optional paths. Up: top of the DFE docs.

This is the default path. The leaner profiles skip Kafka and go receiver to loader over gRPC. A transform app (dfe-transform-vrl, dfe-transform-vector or dfe-transform-elastic) is optional. On Kafka a source that needs grok or your own logic gets one: it reads the source's `*_land` topic and writes a `*_load` one, and the loader reads that one instead. Over gRPC the receiver sends to the transform and the transform sends to the loader, and the lean gRPC profiles run only the fixed transforms a deployment ships, not one per source, so a source that needs its own is refused there. Every component also sends its own logs and metrics over OpenTelemetry into ClickHouse, so the console shows the stack's health before your first event arrives.

## Purpose-built where it's hot, generic where it has to be

Every event lands through dfe-loader, and most enter through dfe-receiver or dfe-fetcher: parse, validate, route, enrich, batch, write. A cycle spent there is spent on nearly every event. So those bookends are purpose-built Rust, not a general transform engine pointed at the job.

A general-purpose transform engine comes in only where the job needs one: a product's message body that needs grok, or your own logic. That's the optional dfe-transform-* apps.

Such an engine interprets your program. VRL, for one, compiles it at startup into a tree it walks for every event, and the Rust compiler only ever sees the interpreter.

Release builds add profile-guided optimisation (PGO) and BOLT, which means 'run it under real load, record what's hot, rebuild around it'. A bookend's hot path is the same work in every deployment, so a profile trained on it carries over. An interpreter's profile only matches the programs it was trained on.

Parsing is where this pays most, because grok and regex over a product's message body are some of the most expensive per-event work in a pipeline. So dfe-transform-elastic doesn't interpret Elastic ingest pipelines. Their processors compile to Rust, one module per source, and so do their Painless scripts. A script it doesn't recognise is counted and skipped.

Zero-copy parsers replace grok for the patterns they cover.

## Who does what

| Repository | Role |
|---|---|
| dfe-engine | Decides what every other component does, and writes it to your git deploy repo. It's also the only thing that changes the ClickHouse schema and Kafka topics. |
| dfe-ui | The console. Every change you make there goes through the engine. |
| dfe-infra | Puts the whole stack, backing services included, onto Kubernetes through Argo CD. |
| dfe-docker | Runs the same stack under Docker Compose. |

Then start with a [quickstart](quickstart/README.md): Docker, Kubernetes on-premises or AWS.
