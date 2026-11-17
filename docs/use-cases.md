# Use cases

DFE earns the *Fusion* in its name when a large *fact* table (the observability data) is joined with a *dimension* table (the context). Telemetry is usually the fact table. The context is whatever answers the question: an inventory, a directory, a feed, a snapshot. The pattern is the same each time. Load the context as a small dimension table and JOIN it against the fact table in ClickHouse SQL.

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
    accTitle: L4 Detail -- Fact and dimension join
    accDescr: Large time-series fact tables, such as device events, application logs and metrics, and small point-in-time dimension tables, such as an asset inventory, an identity directory and a CMDB, meet in one JOIN in ClickHouse SQL that answers the question.

    subgraph facts["Fact tables, observability"]
        direction LR
        events[("<b>Device events</b>")]
        applogs[("<b>Application logs</b>")]
        metrics[("<b>Metrics</b>")]
    end

    subgraph dims["Dimension tables, context"]
        direction LR
        assets[("<b>Asset inventory</b>")]
        directory[("<b>Identity directory</b>")]
        cmdb[("<b>CMDB</b>")]
    end

    join("<b>JOIN</b><br/><span>in ClickHouse SQL</span>")
    answer("<b>Which devices stopped reporting?</b>")

    facts -->|events| join
    dims -->|context| join
    join -->|answer| answer

    class events,applogs,metrics,assets,directory,cmdb,join store
    class answer part
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
  style facts fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style dims fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L4 Detail -- Fact and dimension join.* Up: [dfe-infra documentation](README.md).

## Cyber: hosts not sending audit logs

> **Coming from Splunk or Elastic?** Matching logs against an inventory isn't what an index engine does. DFE keeps both as ClickHouse tables, so it's one anti-join. More in [what's different](../docs-site/content-root/coming-from.md#sources-join).

The question is *which Windows hosts on my network are not sending me audit logs?* Telemetry alone can't answer, because a host that sends nothing is absent from the log fact table by definition. You have to know what *should* be there.

DFE fuses two sources:

- The fact table (the observability data): the Windows event logs (or syslog, or netflow) flowing in, large and time-series.
- A dimension table (the context): a point-in-time inventory of the estate, for example a RunZero export pulled via API. That's "what the network looked like at time X", timestamped but *not* time-series.

Load the inventory as a small dimension table and anti-join it against the hosts seen in the log fact table over the same window. The gap is the answer: assets that exist but are silent. ClickHouse handles this as long as the dimension stays small. A data lake would make you model and pipeline the inventory first.

This is a HyperI XDR use case, and XDR is DFE with the cyber add-ons. More `dfe-fetcher` sources widen the state-of-the-world dumps it can load.

## Defence and the field: fusing disparate sources

Operational situational awareness rarely comes from one clean source. It comes from many loosely controlled ones: sensors, coalition partner feeds, weather, order of battle, damage assessments. That's a mix of telemetry, streams and periodic dumps, of varying quality and structure.

DFE fuses them into one queryable store and keeps working as they change. All inbound data works by default, so a new or altered source is a *performance improvement* to reconcile later, not a hard gate that drops data today. DFE is lean, native Rust over ClickHouse rather than a resource-heavy monolith, so the same stack runs in a theatre data centre or at the edge, not in a distant cloud region alone.
