# Apache NiFi

NiFi feeds DFE in two ways. It pushes records to `dfe-receiver` over HTTP with InvokeHTTP, or it writes NDJSON files that `dfe-fetcher` tails. DFE is not a NiFi node, and Site-to-Site is out of scope.

Everything below was measured on 2026-11-13 with NiFi 2.12.0 on the Docker install, running the app versions stack 2.2.2 pins (`dfe-receiver` v1.15.67, `dfe-fetcher` v1.4.54).

## Push with InvokeHTTP

Mint an ingest credential through the engine, `PUT /api/v1/orgs/{org}/ingest-credentials/{name}` with `If-None-Match: *`. The engine renders it into the receiver's config and the receiver needs no restart: a POST with no token answered 401 and the same POST with the token answered 202.

| InvokeHTTP property | Value |
|---|---|
| `HTTP Method` | `POST` |
| `HTTP URL` | the receiver's `/ingest`, `http://dfe-receiver:8080/ingest` on Docker |
| `Request Content-Type` | `application/x-ndjson` |
| `Request Content-Encoding` | `GZIP` |
| `Socket Read Timeout` | `60 secs`, over the receiver's 25 s hold |
| `Authorization` (dynamic, sensitive) | `Bearer <token>` |
| Penalty | `5 sec`, with 4 concurrent tasks |

NDJSON, a JSON array and gzipped NDJSON all answered 202.

A record lands only in an org its token is bound to, and a token bound to one org puts every record there. A record sent with `"_org_id":"default"` in its body answered 202 and landed under the token's org, because `routing.source_org_id` is `disallow` by default.

The receiver's answers, and the relationship each lands on in NiFi:

| Answer | Meaning | NiFi relationship |
|---|---|---|
| `202` | Every destination confirmed the records, or with `server.acknowledgements.enabled: false` the receiver queued them in memory, and disk spillover is opt-in through `buffer.spillover.enabled`. A record refused for good that the DLQ holds also answers 202 | Original |
| `400` | Invalid JSON, or a record refused for good with no DLQ to hold it | No Retry |
| `503` with `Retry-After: 5` | Not confirmed within the hold. Re-send, and a record a broker took late arrives twice | Retry |
| `413` | The batch is over `server.max_body_size` | No Retry |
| `429` | The per-client `server.rate_limit`, off by default, is exhausted | No Retry |
| connection error | The receiver was down | Failure |

Wire Retry and Failure back into InvokeHTTP. Wire No Retry to somewhere that keeps the batch, such as PutFile. InvokeHTTP doesn't penalise a 4xx by default (`Request Failure Penalization Enabled` is false), so a 413 goes straight there and is never retried in a loop. With the rate limit on, a 429 arrives on No Retry too. Set the property to true, test `${invokehttp.status.code}` and send the 429 batches back into InvokeHTTP, so each waits out the penalty first. A request stops at the first record the receiver refuses, so a retained 400 batch can hold records it already took. Deduplicate before replaying it.

The receiver's default `server.max_body_size` is 10,485,760 bytes and the Docker install sets 8,000,000 (`DFE_RECEIVER_MAX_BODY_SIZE`). It measures the body after gzip is undone, so compression doesn't get a batch past it. NiFi doesn't split a refused batch, so cap batch size upstream of InvokeHTTP. Batches of 152 to 154 KB (about 1,000 records) were all accepted.

A stopped loader doesn't cause a 503, because Kafka holds the records and the receiver confirms them. A broker that stops answering does.

On a Kubernetes deployment the receiver's ingest door is the deployment's own edge, described in [the ingest edge](../ingest-edge.md). The Docker install serves `/ingest` over plain HTTP, and the receiver logs `tls=false` at startup.

## Pull with the fetcher

NiFi's PutFile writes `*.ndjson` into a directory. A fetcher source tails it. Create it with `POST /api/v1/sources`, then `POST /api/v1/sources/{name}/deploy`, with this `fetcher` block and the `header` and `schema` any source carries:

```yaml
fetcher:
  source_type: file
  topic: own
  config:
    interval_secs: 5
    units:
      - unit: nifi
        tail:
          include: ["/data/nifi/*.ndjson"]
          read_from: beginning
          decoder: ndjson
          data_dir: /var/lib/dfe-fetcher/tail/nifi
```

`data_dir` is required. The engine accepts and deploys a tail unit without it, and the fetcher refuses it and keeps its old config.

The Docker `dfe-fetcher` service mounts only `dfe-app-config`, `dlq-spool` and `fetcher-cursors` (dfe-docker `docker-compose.yml:202-205`), so the directory NiFi writes to needs a compose overlay that mounts it read-only. Apply it with `make apply SERVICES=dfe-fetcher STORAGE_FLAGS="-f <overlay>"`, because a bare `docker compose up dfe-fetcher` has no Kafka broker list and exits.

Rows from this source landed in the `default` org, because the engine renders `org_id: default` into the fetcher config and no ingest credential is involved.

## What was measured

| Item | Result | Number | Date |
|---|---|---|---|
| Push, six runs | every record arrived, 4,555 of them twice | 1,220,000 sent, 1,220,000 distinct, 1,224,555 rows | 2026-11-13 |
| Broker paused during a 100,000 record run | every batch answered 503 and was retried, then accepted | 400 x 503, 100,000 distinct rows, 555 duplicates | 2026-11-13 |
| One oversize batch | refused with 413, kept whole in NiFi, 0 rows | 8,988,890 bytes | 2026-11-13 |
| Receiver restarted mid-flow | nothing lost, requests re-sent | 800,000 sent, 800,000 distinct, 4,000 duplicates | 2026-11-13 |
| Pull, NDJSON files | every row landed | 30,000 of 30,000 | 2026-11-13 |
