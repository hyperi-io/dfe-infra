# Use cases

DFE earns the *Fusion* in its name when a large *fact* table (the observability data) is joined with a *dimension* table (the context). Telemetry is usually the fact table. The context is whatever answers the question: an inventory, a directory, a feed, a snapshot. The pattern is the same each time. Load the context as a small dimension table and JOIN it against the fact table in ClickHouse SQL.

```mermaid
flowchart LR
    accTitle: L4 Detail -- Fact and dimension join
    accDescr: Large time-series fact tables, such as device events, application logs and metrics, and small point-in-time dimension tables, such as an asset inventory, an identity directory and a CMDB, meet in one JOIN in ClickHouse SQL that answers the question.

    subgraph facts["Fact tables, observability"]
        direction LR
        events[("Device events")]
        applogs[("Application logs")]
        metrics[("Metrics")]
    end

    subgraph dims["Dimension tables, context"]
        direction LR
        assets[("Asset inventory")]
        directory[("Identity directory")]
        cmdb[("CMDB")]
    end

    join["JOIN in<br/>ClickHouse SQL"]
    answer["Which devices<br/>stopped reporting?"]

    facts -->|events| join
    dims -->|context| join
    join -->|answer| answer

    classDef default fill:#F8FAFC,stroke:#64748B,stroke-width:1px,color:#0F172A
    classDef core fill:#DCEBF7,stroke:#0072B2,stroke-width:2px,color:#0B2A3F
    classDef store fill:#DDF3EC,stroke:#00785A,stroke-width:1px,color:#0B3327
    classDef group fill:none,stroke:#64748B,stroke-width:1px,stroke-dasharray:2 4
    class events,applogs,metrics,assets,directory,cmdb store
    class join core
    class facts,dims group
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
