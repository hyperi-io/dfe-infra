# Why DFE

DFE is a Data *Fusion* Engine. Plenty of approaches can
land a big pile of telemetry. The value is being able to JOIN that pile with
other data -- context, state, reference -- and ask questions across all of it, then cope with change reliably.

Telemetry (events, metrics, logs, syslog, netflow, Windows event logs) is
the most common source of the huge *fact* table. Its value on its
own is only half value, as any user of an index approach such as Elastic or Splunk user knows. You have what happened without context instead of *what happened and what the world looked like around it at the time* allowing you to narrow the field to save time and resources. The questions that matter correlate it with something else: an asset
inventory, an identity directory, a partner feed, a point-in-time snapshot of
what the world looked like. That cross-source JOIN is what index engines cannot
do and what data lakes are too slow to keep current. DFE is built to do exactly
that, and to keep doing it as the sources change.

## The index-engine tax

Index engines (for example Elastic, Splunk) index data on the way in, whether or
not anyone will ever search it. Indexes are large, expensive to build, and
expensive to keep hot. Retention turns into a budget decision rather than a capability or 
security one, and scaling out scales the bill in an ugly way. The flexibility is real, but you
pay for it on every byte, up front, forever -- and you still cannot JOIN cleanly
across sources.

## The data-lake tax

Data lakes (for example Snowflake, Databricks) make storage cheap, but the path
to queryable data is not. Schema management, schema-on-write and multi-stage ELT
make change slow: a new source or a renamed field ripples through ingestion,
transformation and modelling before anyone can query it. Flexible on paper,
rigid in practice -- and query time is metered.

## What DFE does instead

DFE is cake and eat it too.

| The habit | The DFE answer |
|---|---|
| Index everything up front | Land as queryable, optimised columnar data in ClickHouse as the *starter*: cheap to write, fast to query, more retained for less |
| Bolt on a separate ETL tier | `dfe-loader` routes, parses and enriches in flight, then writes RowBinary so the store skips re-parsing. It holds the common 80/20 of ETL on a batch-optimised hot path. Reflecting the destination schema automates the routine ETL-for-the-engine actions, so they are not needed. Only the bespoke 20% goes to the optional `dfe-transform-*` apps -- pick your engine (VRL, Vector, WASM, Elastic, Splunk) |
| Model first, query later | A versioned meta-schema contract (`dfe-schemas`) compiles to table DDL -- change the contract, not a chain of pipelines, and work at a high-level primitive and use-case level, not low-level engine DDL. Your data SMEs need not be tool and DB-engine experts |
| Rip-and-replace to migrate | Keep your sources: compile existing Elastic ingest pipelines and Splunk TAs to native transforms |
| Genericised, sub-optimised | Heavily optimised, opinionated bookends in `dfe-receiver`, `dfe-fetcher` and `dfe-loader` instead of poor-performing generics, leaving generics for where they are *required and beneficial* |
| All-in walled garden | Deliberate components that each do one thing *completely* and mix and match with other technologies without friction |
| Medium scale attempting to scale up | Designed and proven at PB scale *first*, then *scaled down* for medium-small workloads -- usually the secret sauce a SaaS provider keeps to itself |
| GitOps wedging square apps into round pods | Designed from the ground up for Kubernetes autoscaling, with self-regulating vertically scaling pods |
| Making cloud and infra spend the deployer's problem | Efficiency on the hot path is core to every component, reducing your spend -- lean enough to run in the field or a theatre data centre, not only a cloud region |
| Constant serde, re-processing and row processing burns CPU cycles for no value | serde is the enemy. What can be zero copy, batched, SIMD and collapsed into a single app or cycle is. | 
| Wire in and babysit your own enrichment | GeoIP, ASN, IP reputation and risk, Community ID and user-agent parsing are built in, with the reference data kept current for you -- opt in per source, no fragile DIY feeds and cron jobs to maintain |

The effect is to collapse three layers -- index, ETL and object-relational
mapping -- into one native-Rust pass over the data, landed where you can JOIN it.

## Included, maintained, opt-in

The capabilities most pipelines make you bolt on and then babysit are built in,
and DFE keeps their moving parts current for you. IP enrichment is the obvious
case: GeoIP, ASN, IP reputation and risk, Community ID and user-agent parsing
are normally a DIY collection of feeds, lookups and cron jobs that quietly rot
and leave the pipeline fragile. In DFE they ship in the loader, the reference
data is refreshed for you, and you opt in per source -- turn on what you need,
leave the rest off.

The same principle runs through the platform: schema mapping by reflection,
Elastic and Splunk compatibility, OpenTelemetry observability and the generated
deployment artefacts are all included and maintained, not assembled and
babysat. You add capability by switching it on, not by wiring in and owning
another moving part that can break.

## What it costs at scale

Efficiency is a key design goal, not an afterthought, and it shows up on the bill --
and in how much you can keep queryable. In an at-scale test with AWS and 
ClickHouse (August 2025), DFE deployments ran at **10-25%** (varying by data shape and volume) of the cloud
infrastructure spend of a comparable index-based deployment, and at that
lower cost it kept a far larger window live: **270 days** of emulated data
queryable, against **14 days** on the indexed system.

That test also drove a good part of the DFE 2.2 redesign over 2.1, much of which came
straight out of what it showed about where the spend is and the cycles actually get consumed.

## Fusion in practice

Sources are not just streams. DFE treats databases and point-in-time state dumps
as sources in their own right. A complete dump is loaded as a small *dimension* table and joined
against the large fact table -- which ClickHouse does well as long as the
dimension stays small.

- **Cyber (HyperI XDR).** Pull a full point-in-time inventory of the network
  (for example a RunZero export -- "what the estate looked like at time X",
  timestamped but not time-series) and JOIN it against the Windows / syslog /
  netflow fact table to answer the question every defender has: *which hosts on
  my network are not sending me audit logs?* Index engines cannot do that JOIN
  at all.
- **Defence and the field.** Fuse loosely controlled, disparate sources --
  sensors, coalition partner feeds, weather, order of battle, damage assessments
  -- alongside telemetry and streams, and keep working as they change. Because
  DFE is lean, the same stack deploys in a theatre data centre or at the edge,
  not only a cloud region.

More worked examples are in [Use cases](use-cases.md).

## Land "it works", then make it better

DFE is designed so adoption does not begin with a rewrite. The compatibility
transforms (`dfe-transform-elastic` and
`dfe-transform-splack`) accept the data your existing Elastic and Splunk
integrations already produce, so the first milestone is "it works against
my sources". From there you gain native-Rust throughput and a schema contract
that adapts as those sources change. Improvement and change without
re-platforming -- that is the whole pitch.

DFE is built for the world where source data is rarely tightly controlled, and
even when it is, nasty surprises happen. When they do, everything should keep
working, lose nothing, and allow corrections in minutes -- not days or months of
schema management. Because all inbound data works by default, schema management
shifts from an accept/reject hard gate to a *performance improvement*.
