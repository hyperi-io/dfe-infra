# Why DFE

> **Coming from Splunk, Elastic or a lakehouse?** DFE doesn't ask upstream suppliers for a data contract. It accepts every field, and one the schema doesn't map yet lands in the `_json` column with its own typed sub-column. A supplier changing a field doesn't become your outage, and you add the mapping later through the `dfe-schemas` meta-schema. The rest of what's different is in [coming from Splunk, Elastic or a lakehouse](../docs-site/content-root/coming-from.md).

DFE is a Data *Fusion* Engine. Plenty of tools can land a big pile of telemetry. The value is joining that pile with other data (context, state, reference), asking questions across all of it, and coping with change reliably.

Telemetry (events, metrics, logs, syslog, netflow, Windows event logs) is the usual source of the huge *fact* table (the observability data). On its own it's only half the value, as any Elastic or Splunk user knows. You get what happened without *what the world looked like around it at the time*, and that context is what lets you narrow the field and save time and resources. The questions that matter correlate telemetry with something else: an asset inventory, an identity directory, a partner feed, a point-in-time snapshot. That cross-source JOIN is what index engines can't do and what data lakes are too slow to keep current. DFE is built to do exactly that, and to keep doing it as the sources change.

## The index-engine and data-lake taxes

Index engines (Elastic and Splunk, for example) index data on the way in, whether or not anyone will ever search it. The indexes are large, expensive to build and expensive to keep hot. Retention becomes a budget decision instead of a capability or security one, and scaling out scales the bill badly. The flexibility is real. You pay for it on every byte, up front, forever, and you still can't JOIN cleanly across sources.

Data lakes (Snowflake and Databricks, for example) make storage cheap. The path to queryable data isn't. Schema management, schema-on-write and multi-stage ELT make change slow. A new source or a renamed field ripples through ingestion, transformation and modelling before anyone can query it. Flexible on paper, rigid in practice, and query time is metered.

## What DFE does instead

> **Coming from Splunk, Elastic or a lakehouse?** DFE builds no index over each field. It lands each source as ClickHouse columns, and `dfe-loader` parses and enriches in flight, so there's no separate ETL stage to run first. The August 2025 test below measured both the cost and the retention.

DFE is cake and eat it too. Here's what most people do, and what we do instead.

| What most do | What we do |
|---|---|
| Index everything up front | Land it as queryable, optimised columnar data in ClickHouse to start with. Cheap to write, fast to query, and you keep more for less. |
| Bolt on a separate ETL tier | `dfe-loader` routes, parses and enriches in flight, then writes RowBinary so the store skips re-parsing. That's the common 80/20 of ETL on a batch-optimised hot path. Reflecting the destination schema automates the routine ETL-for-the-engine work, so you don't need it. Only the bespoke 20% goes to the optional `dfe-transform-*` apps. Pick your engine: VRL, Vector or Elastic. |
| Model first, query later | A versioned meta-schema (`dfe-schemas`) compiles to table DDL. Change the meta-schema, not a chain of pipelines. You work with high-level primitives and use cases instead of engine DDL, so your data SMEs don't need to be tool and DB-engine experts. |
| Rip and replace to migrate | Keep your sources. Existing Elastic ingest pipelines compile to native transforms (`dfe-transform-elastic`). |
| Generic, sub-optimised everything | Heavily optimised, opinionated bookends in `dfe-receiver`, `dfe-fetcher` and `dfe-loader` instead of poor-performing generics. Generics stay where they're *required and beneficial*. |
| An all-in walled garden | Components that each do one thing *completely*, and mix and match with other technology without friction. |
| Medium scale, then try to scale up | Designed and proven at PB scale *first*, then *scaled down* for medium and small workloads. That's usually the secret sauce a SaaS provider keeps to itself. |
| GitOps wedging square apps into round pods | Built for Kubernetes autoscaling from the ground up, with self-regulating vertically scaling pods. |
| Cloud and infra spend is the deployer's problem | Efficiency on the hot path is core to every component, and that cuts your spend. It's lean enough to run in the field or a theatre data centre, not only a cloud region. |
| Constant serde, re-processing and row processing, which burns CPU cycles for no value | serde is the enemy. What can be zero copy, batched, SIMD and collapsed into a single app or cycle is. |
| Wire in and babysit your own enrichment | GeoIP, ASN, IP reputation and risk, Community ID and user-agent parsing are built in, with the reference data kept current for you. Opt in per source. No fragile DIY feeds and cron jobs to maintain. |
| Dump everything into the engine and fix it there (index engines, ELT) | Put the work close to what it's working on. Filter and correct at the edge, receiver, transform and loader, so a bad record goes where it costs least and ClickHouse stores only what survives. |

That collapses three layers (index, ETL and object-relational mapping) into one native-Rust pass over the data, landed where you can JOIN it.

Most pipelines make you bolt capabilities on and then babysit them. We build them in and keep their moving parts current. IP enrichment is the obvious case. GeoIP, ASN, IP reputation and risk, Community ID and user-agent parsing are normally a DIY collection of feeds, lookups and cron jobs that quietly rot and leave the pipeline fragile. In DFE they ship in the loader, the reference data is refreshed for you, and you opt in per source. Turn on what you need, leave the rest off.

Schema mapping by reflection, Elastic compatibility, OpenTelemetry observability and the generated deployment artefacts work the same way. You add capability by switching it on, not by wiring in and owning another part that can break.

Efficiency is a design goal from day one, and it shows up on the bill and in how much you can keep queryable. Working with AWS and ClickHouse in August 2025, we ran two tests on DFE 2.1 to prove its schema models and ingest scale, with a fully tuned OpenSearch cluster running alongside. At petabyte-a-day throughput, OpenSearch couldn't keep up and DFE did. The 270-versus-14-days result came from the long-term analysis test, at terabyte scale. DFE with ClickHouse also used 10-25% of the resources there, counting the engine and ingestion pipeline together.

Those tests drove a good part of the DFE 2.2 redesign over 2.1. Much of it came straight out of what they showed about where the spend is and where the cycles get consumed.

## Fusion in practice

Sources aren't just streams. DFE treats databases and point-in-time state dumps as sources in their own right. A complete dump is loaded as a small *dimension* table (the context) and joined against the large fact table (the observability data). ClickHouse does that well as long as the dimension stays small.

- Cyber (HyperI XDR). Pull a full point-in-time inventory of the network (a RunZero export, say, which is "what the estate looked like at time X", timestamped but not time-series) and JOIN it against the Windows, syslog and netflow fact table. That answers the question every defender has: *which hosts on my network are not sending me audit logs?* Index engines can't do that JOIN at all.
- Defence and the field. Fuse loosely controlled, disparate sources (sensors, coalition partner feeds, weather, order of battle, damage assessments) alongside telemetry and streams, and keep working as they change. DFE is lean, so the same stack deploys in a theatre data centre or at the edge, not only a cloud region.

More worked examples are in [Use cases](use-cases.md).

## Land "it works", then make it better

Adoption doesn't start with a rewrite. The compatibility transform (`dfe-transform-elastic`) accepts the data your existing Elastic integrations already produce, so the first milestone is "it works against my sources". From there you gain native-Rust throughput and a schema that adapts as those sources change. Improvement and change without re-platforming. That's the pitch.

Source data is rarely tightly controlled, and even when it is, nasty surprises happen. When they do, everything should keep working, lose nothing, and let you correct in minutes, not days or months of schema management. So all inbound data works by default, and schema management moves from an accept/reject hard gate to a *performance improvement*.
