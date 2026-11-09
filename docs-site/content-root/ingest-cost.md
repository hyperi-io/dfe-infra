# What parsing costs: an ingest tier or the DFE transform

**Elastic-equivalent ingest for up to 5x less CPU and 17x less memory, on the exact same sources, with zero wrong fields.**

No ingest tier to buy, size or babysit. DFE parses in the pipeline the data already flows through, and two production deployments and our PB-scale tests proved it costs less.

- Up to 5x less CPU per record than Elasticsearch ingest, measured.
- Up to 17x less memory for the same throughput, measured.
- About 5x less provisioned CPU over a working year, because DFE scales down at the lows and an ingest tier can't.
- Zero wrong fields against Elastic's own pipelines.

OpenSearch does the same as Elastic by default, so all of this covers both.

## How DFE does it

An Elastic stack parses on ingest nodes ('cluster members that run a pipeline on a document before it is indexed'). DFE puts the work right next to the data. [dfe-receiver](dfe-receiver/architecture.md) takes it in, [dfe-transform-elastic](dfe-transform-elastic/README.md) parses it in one process, and [dfe-loader](dfe-loader/README.md) lands it. There's no extra tier.

dfe-transform-elastic compiles Elastic's ingest pipelines into Rust, one module per source. Native [parsers](dfe-transform-elastic/parsers.md) replace grok where they can, and [parity](dfe-transform-elastic/parity.md) shows it matching Elastic field for field.

## Measured

On 2026-11-09 we ran three engines over the same rows on the same four pinned cores: dfe-transform-elastic v1.1.27, the DFE 2.1 pipeline, and Elastic 9.5.6 running Elastic's own integration pipelines. Elastic ran twice, once through `_simulate` ('parse, index nothing', what an ingest node pays) and once through `_bulk` ('parse, then index'). Four sources, one per parser family, each clean and again with 20% of rows corrupted.

Records per CPU-second, clean rows, mean of three runs:

| Engine | Cisco IOS | CrowdStrike Falcon | Okta System | Fortinet FortiGate |
|---|---|---|---|---|
| dfe-transform-elastic | 12,502 | 8,510 | 6,154 | 8,102 |
| The DFE 2.1 pipeline | 3,644 | 2,385 | 2,336 | 2,870 |
| Elastic ingest, parse only | 6,656 | 6,995 | 3,613 | 3,615 |
| Elastic ingest, parse plus index | 3,427 | 2,387 | 1,289 | 2,247 |

Peak memory: 222-622 MiB for dfe-transform-elastic, 5.1-5.7 GiB for Elastic. Zero wrong fields on all four sources.

Source by source, dfe-transform-elastic uses:

- 2.6-3.6x less CPU per record than the DFE 2.1 pipeline
- 1.2-2.2x less than an ingest node parsing alone
- 3.6-4.8x less than parse plus index
- 5.0-17.1x less memory than an ingest node for the same throughput

Each pod runs on about one core, and you add capacity by adding pods.

## Proven in production

On DFE 2.1, parsing Filebeat and Winlogbeat data in the 2.1 pipeline cost less than Elastic's Painless pipelines, and by a lot. Two live deployments proved it, a large telco and a large-scale government agency. Each ran OpenSearch at 10-100 TB a day as the analyst discovery service, a sidecar to the ClickHouse instance doing the heavy lifting. Running both side by side gave us a bake-off of the two approaches as a bonus.

The PB-scale tests proved it again. We ran two tests on DFE 2.1 to prove its schema models and ingest scale, the design that became DFE 2.2. That design moved the hot path into purpose-built Rust, which is dfe-transform-elastic, and it beats the 2.1 pipeline by another 2.6-3.6x.

A real cluster pays for the tier the parse runs on, sized for the peak. A single-node bench can't show that, and production does.

## Scaling with the load

dfe-transform-elastic scales with the data. KEDA ('the autoscaler that watches the backlog', set up in [autoscaling](dfe-infra/autoscaling.md)) adds pods when Kafka input queues up and removes them as it drains. Few pods at the lows, more at a spike.

Dedicated ingest nodes can't. On Elastic Cloud on Kubernetes, Elastic Cloud Hosted and Elastic Cloud Enterprise, autoscaling covers data tiers and machine learning nodes only: https://www.elastic.co/docs/deploy-manage/autoscaling. So ingest nodes are sized for the peak and sit there at the lows. Elastic Cloud Serverless is the one exception.

## Over a working year (estimate)

Security and ops data follows the working day. Take a typical Australian organisation. Overnight, weekends and 11 weekday public holidays run at 25% of peak. The morning logins and the post-lunch return hit 100%. Over a year that averages 44% of peak, and only 11% of hours run at 100%.

Ingest nodes sized for the peak carry 1 / 0.44 = 2.26x the capacity that load needs. DFE doesn't.

| dfe-transform-elastic against | Per record, measured | Over the year |
|---|---|---|
| An ingest node | 1.2-2.2x less CPU | about 2.8-5.1x less provisioned CPU |
| The DFE 2.1 pipeline | 2.6-3.6x less CPU | 2.6-3.6x, because 2.1 autoscaled too |

Plug in your own profile: divide peak by mean load and multiply.

## What a real cluster adds (estimate)

From the documented write path, on top of the parse:

- An ingest node is a cluster member. It holds cluster state, takes part in fault detection and runs its own JVM heap, busy or not.
- Every parsed document crosses the cluster again to its primary shard, and each replica indexes it again. With `n` replicas that's `1 + n` indexes per record.
- OpenSearch segment replication and Elastic Cloud Serverless index once and copy segments. Self-managed Elastic indexes on every replica.

DFE has hops too, laid out in the [overview](README.md), but no DFE stage holds a cluster seat just to parse.

The full method, the per-source numbers and how to re-run it: https://github.com/hyperi-io/dfe-transform-elastic/blob/main/docs/ingest-cost-bench-elastic.md

No ingest tier. That's the saving.
