# Kafka broker scale kind proof -- controller footprint and broker throughput

Run: 2026-09-13, on a 12-core laptop running kind. Strimzi-managed Kafka:
three KRaft controllers (1 vCPU / 4 GiB each), three brokers (500m CPU
limit each), Cruise Control on. A synthetic producer/consumer pair held a
steady ~1.95 MB/s throughout. Both figures below are single-run spot
checks, LOW confidence: they support the existing vendor floors and
benchmark, not a replacement for either.

## Measurements

- Controller CPU/RAM flat from 36 to 96 partitions on our own test topics
  (179 to 239 cluster-wide): 27/21/17m CPU and 690/540/536Mi RAM at 36,
  25/18/19m and 691/541/536Mi at 96 -- no material change, consistent with
  the KRaft metadata log not growing with ingest at this scale.
- Broker throughput at steady state, post scale-in (3 brokers): 1.95 MB/s
  producer throughput over 98+106+102m = 306m summed broker CPU = 6.37 MB/s
  per vCPU.

## Caveats

The controller check is one step (36 to 96 partitions) on one quorum --
support for the 1 vCPU / 4 GiB floor, not proof it holds at a much larger
partition count.

The throughput figure is client-facing send rate at far below CPU
saturation (306m of 1500m available broker CPU) -- the cost of a light
load, not a right-sizing ceiling like the existing 8.3 MB/s per vCPU
benchmark (AWS, RF3 on Graviton, closer to saturation), so it sits beside
that figure rather than replacing it. RF3 replication also means
broker-side write I/O already runs roughly 3x this rate.
