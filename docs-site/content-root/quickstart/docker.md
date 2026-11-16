---
sidebar_position: 2
sidebar_label: Docker
---

# Quickstart: Docker

The whole DFE stack under Docker Compose on one host: a laptop, a demo box or a proof of concept. It runs the same images as a Kubernetes deployment, just packaged differently.

<!-- docker-quickstart: up -->
<!-- landing -->
```bash
docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:compose up -d
docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:compose run --rm secrets-init login
```

Point your browser to http://localhost:3000 and log in with the name and password the second command prints. The console makes you change it at first login. Every port listens on 127.0.0.1 only. `docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:compose down` stops the stack and keeps its data.
<!-- /docker-quickstart -->

If `docker compose version` shows a release below 2.34, as Debian's own packages do, install Docker from [Docker's own repository](https://docs.docker.com/engine/install/) first.

The default stack idles at about 2.5 GB of RAM, and its memory limits are sized for a 16 GB box.

| Profile | Transport | Runs |
|---|---|---|
| `slim-grpc` | gRPC, low latency | The core path alone: receiver, loader and ClickHouse, with the engine and the console. |
| `slim-kafka` | Kafka message bus | The same core path over Kafka, plus a Kafka UI. |
| `single-grpc` | gRPC, low latency | The core path plus the fetcher and the transforms. |
| `single-kafka` | Kafka message bus | The core path over Kafka, plus the archiver, the fetcher and the transforms. The default. |

Then send it an event of your own:

```bash
curl -X POST http://localhost:8080/ingest \
  -H 'Content-Type: application/json' \
  -d '{"_source":"main","message":"hello dfe"}'
```

`_source` routes it, so `"main"` lands in `dfe.main` a couple of seconds later.

> **Coming from Splunk or Elastic?** You didn't create an index or a mapping first, and you won't need to. `main` takes whatever fields you send and they all work straight away, so a sender adding or changing a field breaks nothing. You tune the fields that matter later. More in [what's different](../coming-from.md#nothing-breaks-when-a-source-changes).

Where to find it: the console is at http://localhost:3000, ClickHouse HTTP at http://localhost:8123 and the engine API at http://localhost:8003. `single-kafka` adds the Kafka UI at http://localhost:8081. The ingest ports listen on every interface, and everything else on this host only.

The second install command prints the login again whenever you run it. To stop the stack and delete its data, the minted secrets included:

```bash
docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:compose down -v
```

Next:

1. The full first look, including reading the event back: [Taking DFE for a first look](../dfe-docker/evaluating.md).
2. What each profile runs: [profiles](../dfe-docker/profiles.md).
3. Set the deployment's configuration: [configuration](../dfe-docker/configuration.md).
4. Deploy, and move to a newer release: [deploying](../dfe-docker/deploying.md).
5. Run it day to day: [operating](../dfe-docker/operating.md).
6. When something does not come up: [troubleshooting](../dfe-docker/troubleshooting.md).
