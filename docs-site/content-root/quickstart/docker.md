---
sidebar_position: 2
sidebar_label: Docker
---

# Quickstart: Docker

The whole DFE stack under Docker Compose on one host: a laptop, a demo box or a proof of concept. It runs the same images as a Kubernetes deployment, just packaged differently.

You need Docker with Compose v2, Python 3, git and make.

```bash
git clone https://github.com/hyperi-io/dfe-docker && cd dfe-docker
make up     # pull, start, self test, print the console login
```

`make up` runs the newest DFE release with every image pinned, on the `single-kafka` profile. It ends with a self test that sends marked events through and prints `PASS` as each check lands. `VERSION=` pins another release.

`make up PROFILE=<name>` picks another profile. A name not in this table is refused with the valid list.

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

Next:

1. The full first look, including reading the event back: [Taking DFE for a first look](../dfe-docker/evaluating.md).
2. What each profile runs: [profiles](../dfe-docker/profiles.md).
3. Set the deployment's configuration: [configuration](../dfe-docker/configuration.md).
4. Deploy, and move to a newer release: [deploying](../dfe-docker/deploying.md).
5. Run it day to day: [operating](../dfe-docker/operating.md).
6. When something does not come up: [troubleshooting](../dfe-docker/troubleshooting.md).
