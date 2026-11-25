---
sidebar_position: 4
sidebar_label: Detections and hunts
---

# Detections and hunts

A rule is one SQL query tied to a source. A hunt runs rules on a schedule and writes what they match to the `alerts` table.

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
flowchart LR
  accTitle: L4 Detail -- Rules and hunts
  accDescr: A rule is SQL on a source's table. The hunt runner runs rules on their schedule and writes matches to the alerts table, which a rule can read in turn.
  R("<b>Rule</b><br/><span>SQL on a source's table</span>")
  H("<b>Hunt runner</b><br/><span>runs rules on their schedule</span>")
  A[("<b>Alerts table</b>")]
  R --> H
  H --> A
  A -.->|a rule can read it| R
  class R part
  class H control
  class A store
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

*Figure 1. L4 Detail -- Rules and hunts.* Up: [L2 Services -- DFE data path](../README.md).

The `alerts` table is itself a source. It has the same schema management, retention and query surface as any other, and a rule written against it is how you express correlation.

> **Coming from Splunk?** Rules are ClickHouse SQL, so there's no separate correlation language to learn. A rule can join `alerts` or any source against another table, such as an asset inventory. [Detections on a source](../dfe-engine/data-plane/source-detections.md) has the rule file, the hunt, which organisations a hunt reads, and the lookup databases a rule condition may read.

## In the console

Rules and Hunts sit under Detect. Matches are under Observe, in Hunt Results. A search in the console can start a rule: the Create Rule trigger sends the search's SQL to the rule editor. [Creating a rule from a search](../dfe-ui/hyperdx-rule-create.md) walks it.

## Sigma

Sigma is off by default in this release. Every path under `/api/v1/sigma` answers 404 until `sigma.enabled` is set (`DFE_ENGINE_SIGMA_ENABLED=true`). A `sigma` entry in a source's `views` is a field-map view and deploys either way. Turned on, [the Sigma pipeline](../dfe-engine/data-plane/sigma.md) pulls community rule feeds into a catalogue you curate, and each rule you select becomes a DFE rule and a hunt. [Field mapping](../dfe-engine/data-plane/field-mapping.md) says how Sigma's field names reach your columns.

## Running the hunts

The hunt runner is one pod by default. Autoscaling is opt-in, through `huntRunner.autoscaling.enabled`. [Hunt-runner scaling](../dfe-engine/data-plane/hunt-runner-scaling.md) says how the due backlog is computed and what the runner does with no engine.

## Conditions

Match conditions use [CEL](https://github.com/cel-expr/cel-spec/blob/master/doc/langdef.md), Google's Common Expression Language. [The expression language](../dfe-engine/data-plane/expressions-cel.md) says why we chose CEL.
