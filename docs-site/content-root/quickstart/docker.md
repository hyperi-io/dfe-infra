---
sidebar_position: 2
sidebar_label: Docker
---

# Quickstart: Docker

The whole DFE stack on one host, and one event through it. Four steps.

You need Docker with Compose v2, Python 3 and [uv](https://docs.astral.sh/uv/). Ports 3000, 8003, 8080 and 8123 need to be free. `<version>` is the DFE release in the version menu at the top of this page.

## 1. Start DFE

<!--
ONE-COMMAND START: replace the block below with this once the stack ships as a compose artefact
(hyperi-io/dfe-docker#337, hyperi-io/dfe-engine#1034). Keep the paragraph under it.

```bash
docker compose -f oci://ghcr.io/hyperi-io/dfe:<version> up
```
-->

```bash
git clone https://github.com/hyperi-io/dfe-docker && cd dfe-docker
make init                      # write .env and mint the logins
make stack VERSION=<version>   # pin every image to that release
make up                        # pull, start, self test, print the console login
```

`make up` ends on a self test that sends marked events through and prints `PASS` as each one lands. The default profile, `slim-grpc`, runs the core path over gRPC. Put `DFE_PROFILE=single-kafka` in `.env` before `make up` for Kafka and the per-source apps.

## 2. Open the console

Go to http://localhost:3000 and log in as `admin` with the password `make up` printed. `make creds` prints it again.

The first login walks you through setup: name your organisation, pick how people log in, and add your first user.

## 3. Send an event

```bash
curl -X POST http://localhost:8080/ingest \
  -H 'Content-Type: application/json' \
  -d '{"_source":"main","message":"hello dfe"}'
```

`_source` routes it. `"main"` lands in the ClickHouse table `dfe.main`.

## 4. See it land

In the console, open **Observe**, then **Search**. Pick the `main` source and search for `hello dfe`. It shows up a couple of seconds after the `curl`.

Prefer SQL? [Taking DFE for a first look](../dfe-docker/evaluating.md) reads it straight out of ClickHouse.

## Next steps

| You want to | Read |
|---|---|
| Send from your agents: Beats, OTLP, Splunk HEC | [Receiver ports](../dfe-docker/configuration.md#default-ports) |
| Route and store each feed by where it came from | [Sources](../dfe-engine/data-plane/source.md) |
| Run the same images on your own cluster | [Kubernetes on-premises](kubernetes.md) |
| Run it day to day and move to a newer release | [Operating](../dfe-docker/operating.md) |

`make down` stops the stack and keeps the data. `make clean` deletes the data too.
