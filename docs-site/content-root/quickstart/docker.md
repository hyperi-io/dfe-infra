---
sidebar_position: 2
sidebar_label: Docker
---

# Quickstart: Docker

The whole DFE stack under Docker Compose on one host: a laptop, a demo box or a proof of concept. Same images as a Kubernetes deployment, packaged differently.

You need Docker with Compose v2 and Python 3. `<version>` is the DFE release you are running, the one in the version menu at the top of this page. Pick a profile first:

| Profile | Transport | Runs |
|---|---|---|
| `slim-grpc` | gRPC | The core path alone: receiver, loader and ClickHouse, with the engine, the console and HyperDX. The default. |
| `single-kafka` | Kafka | All of that, plus Kafka, the archiver, and the fetcher and transforms, one per source. |

```bash
git clone https://github.com/hyperi-io/dfe-docker && cd dfe-docker
make init                                   # write .env and mint the logins
make stack VERSION=<version>                # pin every image to that release
echo 'DFE_PROFILE=slim-grpc' >> .env        # or single-kafka
make up                                     # pull, start, self test, print the console login
```

`make stack` is not optional. The stack refuses to start on an unpinned image rather than pull `latest`. `make up` ends with a self test that sends marked events through and prints `PASS` as each check lands.

Then send it an event of your own:

```bash
curl -X POST http://localhost:8080/ingest \
  -H 'Content-Type: application/json' \
  -d '{"_source":"main","message":"hello dfe"}'
```

`_source` routes it, so `"main"` lands in `dfe.main` a couple of seconds later. The console is at http://localhost:3000, ClickHouse HTTP at http://localhost:8123 and the engine API at http://localhost:8003. `single-kafka` adds the Kafka UI at http://localhost:8081. The ingest ports listen on every interface, and everything else on this host only.

Next:

1. The full first look, including reading the event back: [Taking DFE for a first look](../dfe-docker/evaluating.md).
2. What each profile runs: [profiles](../dfe-docker/profiles.md).
3. Set the deployment's configuration: [configuration](../dfe-docker/configuration.md).
4. Deploy, and move to a newer release: [deploying](../dfe-docker/deploying.md).
5. Run it day to day: [operating](../dfe-docker/operating.md).
6. When something does not come up: [troubleshooting](../dfe-docker/troubleshooting.md).
