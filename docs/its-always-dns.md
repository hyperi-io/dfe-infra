---
sidebar_position: 1
---

# It's always DNS

Most "DFE is down" tickets that open on a connection error are a name that stopped resolving. Several DFE components don't say so. The loader logs `client error (Connect)` and the proxy logs `remote connection failure`, and neither mentions DNS. So ask the failing container before you read any logs.

Run the lookup from inside the component that is failing, never from your laptop. It uses the same resolver the component does.

```bash
# Docker Compose
docker exec dfe-loader getent hosts clickhouse; echo "exit=$?"

# Kubernetes
kubectl -n dfe-local exec deploy/dfe-loader -- \
  getent hosts dfe-clickhouse-clickhouse-headless.clickhouse.svc.cluster.local; echo "exit=$?"
```

An address and `exit=0` means the name resolves, so it isn't DNS and the last table below is for you. No output and `exit=2` means it doesn't. The engine, proxy, console, receiver, loader, archiver, fetcher and UI images all carry `getent`.

## Docker Compose

Each row removes one service from the Compose network while its container keeps running, then posts an event and waits. That's what a stopped, renamed or detached service looks like to everything that dials it.

| Name | Fails first | What it shows | What stays green |
|---|---|---|---|
| `clickhouse` | dfe-engine | `/readyz` answers 503 with `"checks":{"clickhouse":false,...}`. The log repeats `NameResolutionError ... Failed to resolve 'clickhouse' ([Errno -3] Temporary failure in name resolution)` about every 10 seconds. | The loader's `/readyz`, and the receiver's `202` on `/ingest`. |
| `clickhouse` | dfe-loader, on the first insert | `ERROR dfe_loader::clickhouse::inserter: Insert failed, returning the batch for retry ... network error: client error (Connect)`, then `Schema issue during the insert, retrying` with growing delays. No mention of DNS. The batch is held and the Kafka commit stays below it. | `/readyz`. Nothing is dead-lettered (`messages_dlq_total` stays 0). |
| `kafka` | dfe-loader and dfe-archiver, within seconds | `librdkafka: FAIL [thrd:kafka:9092/bootstrap]: kafka:9092/bootstrap: Failed to resolve 'kafka:9092': Temporary failure in name resolution` | `/readyz` on both. |
| `kafka` | dfe-receiver, on the first event | `POST /ingest` answers `503 {"error":"service unavailable"}` after about 20 seconds, the producer's delivery timeout. The log says `Kafka delivery failed -- no broker confirmed a record ... MessageTimedOut`, then the same `Failed to resolve` and `AllBrokersDown`. | The receiver's `/readyz`. |
| `hyperdx-documentdb` | dfe-hyperdx, at the next scheduled task | After a 30 second wait: `MongooseServerSelectionError: getaddrinfo EAI_AGAIN hyperdx-documentdb`. | The container's health check and `/health`. A `Lost connection to MongoDB server` line also appears on healthy runs, so look for `EAI_AGAIN`. |
| `dfe-engine` | dfe-proxy | The console's proxy answers `503 upstream connect error or disconnect/reset before headers. reset reason: remote connection failure` after about 3 seconds. | The proxy's `/`, which still answers `307` to the login page. |

A readiness probe that doesn't test a dependency stays green while the dependency is gone, so read the first column, not the green ticks. A loader that starts while `clickhouse` is gone logs `WARN ... ClickHouse refused the startup probe, staying not ready ... network error: client error (Connect)` with growing retry delays. An expired or untrusted certificate logs the identical line, so a loader whose lookup passes goes to the PKI page next. Confirm the cause from the container that fails:

```bash
docker exec dfe-loader getent hosts clickhouse            # exit=2
docker exec dfe-receiver getent hosts kafka               # exit=2
docker exec dfe-hyperdx getent hosts hyperdx-documentdb   # exit=2
```

To find which components were hit, grep every log once for the three spellings of the same libc error:

```bash
docker compose logs --no-color 2>&1 | grep -i -E 'name resolution|EAI_AGAIN|Failed to resolve'
```

The receiver, engine and dfe-hyperdx matched. The loader never did, because it reports `client error (Connect)` and nothing more. A component that is failing and absent from that list still needs the `getent` check above.

To fix it, find the service that owns the name and bring it back under that name with `docker compose ps`, then `docker compose up -d`. That recreated the detached container with its network aliases and the name resolved again. It also re-runs the stack's certificate one-shot, which rewrites the DocumentDB CA once its leaf is inside 30 days of expiry. On a stack that old, read the Compose part of [...and if it's not DNS, it's PKI](and-if-its-not-dns-its-pki.md) before you run it. Then check four things. The engine's `/readyz` returns 200 with `"clickhouse":true`. The held batch lands, so `rows_inserted_total` on the loader's `/metrics` counts it. The receiver answers `202` in milliseconds. And dfe-hyperdx logs `Connection established to MongoDB` on the next minute.

A dead upstream resolver breaks only the names outside the stack. Docker answers service names itself, and a `dns:` entry on a service changes where outside names go. Point one at an address that never answers and the service keeps working until it needs the internet:

```bash
docker inspect -f '{{.HostConfig.Dns}}' dfe-loader
docker exec dfe-loader getent hosts example.com          # any outside name, exit=2 after about 16 seconds
docker exec dfe-loader getent hosts clickhouse           # exit=0, instantly
```

The loader logged `WARN scalo::version_check: version check failed (non-fatal) error=http: error sending request for url (https://<release-check-host>/api/v1/check)` and stayed ready. Every outside name the stack dials, a cloud ClickHouse or an OIDC issuer included, goes through that same upstream. Remove the bad `dns:` line and run `docker compose up -d`.

## Kubernetes

Run the lookups from a pod in the namespace that is failing. Its `/etc/resolv.conf` carries the cluster resolver, `ndots:5` and a search list of `<namespace>.svc.cluster.local svc.cluster.local cluster.local` plus the node's own domain. Three lookups fail.

| You ran | You got | Why |
|---|---|---|
| `getent hosts dfe-clickhouse-clickhouse-headless` from `dfe-local` | `exit=2` | The short name is searched in your own namespace only. Use `<service>.<namespace>`, which returned an address. |
| `getent hosts` on a headless Service whose pods aren't Ready | `exit=2` | A headless Service publishes pod addresses and nothing else. No ready pod, no name. |
| Any lookup from a pod whose resolver points at an address that never answers | `exit=2` after tens of seconds, and `curl` exits 6 | Cluster names fail too. That's the difference from Compose. |

An empty `ENDPOINTS` column from `kubectl -n <namespace> get endpointslices` is a Service with nothing behind it. Fix the pods, not the DNS.

Now the lookalikes, where the name resolved and something else is wrong.

| Symptom | Cause | Check |
|---|---|---|
| `getent` returns an address and `curl` exits 7 straight away | A ClusterIP Service with no ready pod. The name resolves and nothing listens. | `kubectl get endpointslices` |
| `getent` returns an address and `curl` exits 28 at its timeout | A NetworkPolicy drops the connection. The ClickHouse namespace admits only the namespaces its policy lists, so a pod anywhere else hangs. | `kubectl -n clickhouse get networkpolicy` |
| A mistyped console host still resolves | With a wildcard edge record, `*.<domain>`, every name under it resolves to the gateway. The gateway answers `404` to a host it has no route for. | `getent hosts nosuch.<domain>` returns the gateway address. |

A hang with a good address is the network. A refusal with a good address is the pod. No address is DNS. When the name resolves and the next error is about a certificate, read [...and if it's not DNS, it's PKI](and-if-its-not-dns-its-pki.md).
