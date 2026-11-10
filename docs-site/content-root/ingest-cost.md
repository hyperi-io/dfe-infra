# What parsing costs: an ingest tier or the DFE transform

**Elastic-equivalent ingest on the same sources for up to 5x less CPU and 19x less memory, with zero wrong fields.**

No ingest tier to buy, size or babysit. DFE parses in the pipeline the data already flows through, and two production deployments and our PB-scale tests proved it costs less.

- Up to 5x less CPU than Elasticsearch ingest for the same ECS fields, same data, same enriched output.
- Up to 19x less memory: a few hundred MiB where an ingest node runs 5-6 GiB.
- Up to 6x less provisioned CPU over a working year, because DFE scales down at the lows and an ingest tier can't.
- Zero wrong fields: full field parity with Elastic's own pipelines on every source we tested.

OpenSearch does the same as Elastic by default, so all of this covers both.

## How DFE does it

An Elastic stack parses on ingest nodes ('cluster members that run a pipeline on a document before it is indexed'). DFE puts the work right next to the data. [dfe-receiver](dfe-receiver/architecture.md) takes it in, [dfe-transform-elastic](dfe-transform-elastic/README.md) parses it in one process, and [dfe-loader](dfe-loader/README.md) lands it. There's no extra tier.

dfe-transform-elastic compiles Elastic's ingest pipelines into Rust, one module per source. Native [parsers](dfe-transform-elastic/parsers.md) replace grok where they can, and [parity](dfe-transform-elastic/parity.md) shows it matching Elastic field for field.

## Measured

We ran dfe-transform-elastic v1.1.28, the DFE 2.1 pipeline and Elastic 9.5.6 running Elastic's own integration pipelines over the same rows, on the same pinned cores. That's four sources, one per parser family, each clean and again with corrupted rows, three runs a cell.

- Up to 5x the records per CPU-second of an Elastic ingest node, parse plus index, and more with corrupted rows in the mix.
- Up to 4x the records per CPU-second of the DFE 2.1 pipeline.
- A few hundred MiB of memory at peak, against 5-6 GiB for the ingest node.
- Zero wrong fields on all four sources.

Each pod runs on about one core, and you add capacity by adding pods. The per-source numbers, the method and how to re-run it are in the dfe-transform-elastic docs, linked at the end.

## Proven in production

On DFE 2.1, parsing Filebeat and Winlogbeat data in the 2.1 pipeline cost less than Elastic's Painless pipelines, and by a lot. Two live deployments proved it, a large telco and a large-scale government agency. Each ran OpenSearch at 10-100 TB a day as the analyst discovery service, a sidecar to the ClickHouse instance doing the heavy lifting. Running both side by side gave us a bake-off of the two approaches as a bonus.

The PB-scale tests proved it again. We ran two tests on DFE 2.1 to prove its schema models and ingest scale, the design that became DFE 2.2. That design moved the hot path into purpose-built Rust, which is dfe-transform-elastic, and it beats the 2.1 pipeline by up to another 4x.

A real cluster pays for the tier the parse runs on, sized for the peak. A single-node bench can't show that, and production does.

## Scaling with the load

dfe-transform-elastic scales with the data. KEDA ('the autoscaler that watches the backlog', set up in [autoscaling](dfe-infra/autoscaling.md)) adds pods when Kafka input queues up and removes them as it drains. Few pods at the lows, more at a spike.

Dedicated ingest nodes can't. On Elastic Cloud on Kubernetes, Elastic Cloud Hosted and Elastic Cloud Enterprise, autoscaling covers data tiers and machine learning nodes only: https://www.elastic.co/docs/deploy-manage/autoscaling. So ingest nodes are sized for the peak and sit there at the lows. Elastic Cloud Serverless is the one exception.

## Over a working year (estimate)

Security and ops data follows the working day. Take a typical Australian organisation. Overnight, weekends and 11 weekday public holidays run at 25% of peak. The morning logins and the post-lunch return hit 100%. Over a year that averages 44% of peak, and only 11% of hours run at 100%.

Ingest nodes sized for the peak carry 1 / 0.44 = 2.26x the capacity that load needs. DFE doesn't.

With autoscaling on both sides of that, DFE runs on up to 6x less provisioned CPU than an ingest node over the year, and about 4x less than the DFE 2.1 pipeline, which autoscaled too.

Plug in your own profile: divide peak by mean load and multiply.

## What a real cluster adds (estimate)

From the documented write path, on top of the parse:

- An ingest node is a cluster member. It holds cluster state, takes part in fault detection and runs its own JVM heap, busy or not.
- Every parsed document crosses the cluster again to its primary shard, and each replica indexes it again. With `n` replicas that's `1 + n` indexes per record.
- OpenSearch segment replication and Elastic Cloud Serverless index once and copy segments. Self-managed Elastic indexes on every replica.

DFE has hops too, laid out in the [overview](README.md), but no DFE stage holds a cluster seat just to parse.

The full method, the per-source numbers and how to re-run it: https://github.com/hyperi-io/dfe-transform-elastic/blob/main/docs/ingest-cost-bench-elastic.md

No ingest tier. That's the saving.
