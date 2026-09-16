# ClickHouse spot-test calibration -- synthetic otel_logs replay

Run: 2026-09-13, about 22 minutes, on a three-node on-prem Kubernetes
cluster. ClickHouse 26.3.31.5 and Keeper 26.3.31.5 ran as plain
StatefulSets, each at 4 vCPU/8 GiB, requests equal to limits. Load:
1,346,790 rows, 2,000,001,435 wire bytes (2.000 GB decimal) of SYNTHETIC
`otel_logs`-shaped events from a seeded generator -- no real DFE fixture
existed. Every on-disk size read followed `OPTIMIZE TABLE ... FINAL`.
Insert throughput (JSONEachRow vs Native): two timed runs a format, from
`system.query_log` by `query_id` (rows/s elapsed, rows/cpu-second). Keeper
cost is `mntr` packet-count deltas across a 1 MiB-flush and a 16 MiB-flush
burst.

## Measurements

- LZ4 (default codec) wire-to-disk: 2,000,001,435 / 611,832,026 = 3.27x.
- ZSTD(1) wire-to-disk: 2,000,001,435 / 334,812,515 = 5.97x.
- ZSTD(1) vs LZ4 on-disk size: 334,812,515 / 611,832,026 = 0.547.
- Native insert, two runs: 92,010 and 92,203 rows/cpu-second; sizing.yaml
  adopts 91,000, below both runs.
- JSONEachRow insert, two runs: 85,955 and 86,246 rows/cpu-second, averaging
  86,100.5.
- Parse factor: average Native 92,106.5 / average JSONEachRow 86,100.5 =
  1.07 -- Native costs about 7% fewer CPU-seconds a row here, not the 15.5x
  an Altinity benchmark measured on different hardware and an older
  ClickHouse version.
- Merge fraction, all 4 dfe tables' `system.part_log`: 32,571 ms MergeParts
  / 74,026 ms total (NewPart 41,455 ms + MergeParts 32,571 ms) = 0.440.
- Keeper packets received per GB: 14,782 at 1 MiB flushes, 1,061 at 16 MiB
  flushes; ratio 14,782 / 1,061 = 13.9x.
- Keeper packets received per part: 14.76 (1 MiB), 14.83 (16 MiB) --
  roughly constant.
- Average wire bytes per event: 2,000,001,435 / 1,346,790 = 1,485.

## Caveats

Synthetic Body/TraceId/SpanId/UUID/hex fields are high-entropy hex and word
salad, compressing worse than a real log corpus's repeated substrings, so
3.27x/5.97x/0.547 are a pessimistic floor from this generator, not a
replacement for ClickHouse's vendor-documented 13-16x logging-benchmark
band, which stays default until a real DFE corpus is measured.

RAM at rest (2.49-14.50 GB RAM per compressed GB, not promoted here) is
dominated by ClickHouse's fixed per-process overhead against only about 1.95
GB of compressed data, so it does not recalibrate the 30-130x retention
ratios, which describe hundreds-of-GB-to-TB scale.

Merge fraction is elapsed time, not CPU: `part_log` carries `duration_ms`
but no per-event CPU-time column, so 0.44 is how much elapsed time went to
merges, not CPU consumed. A running cluster's own `part_log` remains the
real input for a production-scale re-measurement.
