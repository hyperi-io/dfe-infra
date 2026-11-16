---
sidebar_position: 1
sidebar_label: The demo
---

# Quickstart: the demo

Raw firewall syslog in, one parsed ClickHouse table per product out, on one host. The fetcher pulls two live API feeds beside it: Wikipedia's recent changes, and the latest GitHub release of each public DFE component.

<!-- docker-quickstart: demo -->
<!-- landing -->
```bash
docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo up -d
docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo run --rm secrets-init login
```

A throwaway stack that seeds its own sources and streams vendor syslog into one table per vendor, beside its fetchers. Point your browser to http://localhost:3000 and log in with the name and password the second command prints. The console makes you change it at first login. It takes the Docker install's ports, so stop that first, and `docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo down -v` removes it all.

Other demo use cases:

- [Cyber](https://hyperi.io/solutions/security-monitoring/): `docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo-cyber up -d && docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo-cyber run --rm secrets-init login`
- [Telco](https://hyperi.io/solutions/telco/): `docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo-telco up -d && docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo-telco run --rm secrets-init login`
- [Theatre and battlespace](https://hyperi.io/solutions/theatre-and-battlespace/): `docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo-theatre up -d && docker compose -f oci://ghcr.io/hyperi-io/dfe-docker:demo-theatre run --rm secrets-init login`
<!-- /docker-quickstart -->

If `docker compose version` shows a release below 2.34, as Debian's own packages do, install Docker from [Docker's own repository](https://docs.docker.com/engine/install/) first.

The second command prints the login again whenever you run it.

## From a checkout

To change the demo's rate or protocol, or to keep the stack afterwards, run it from a dfe-docker checkout. That needs Python 3, git and make as well as Docker.

```bash
git clone https://github.com/hyperi-io/dfe-docker && cd dfe-docker
make demo                       # newest release, 20 lines a second over TCP
make demo DEMO_RATE=200         # a faster stream
make demo DEMO_PROTOCOL=udp     # syslog over UDP
```

When no stack is running, `make demo` starts one and runs its self test, which prints `PASS` per check. Then it replays Cisco ASA and FortiGate logs as raw syslog. dfe-transform-elastic works out which device sent each line, parses it, and lands it in `dfe.cisco-asa` or `dfe.fortinet`. A line no product parser claims goes to `dfe.demo-syslog`.

It returns once every product table has gained rows, and the stream keeps going. Each product has about 6,000 sample lines, so it runs roughly ten minutes before a line repeats. On a stack already running the demo, run `make demo-stop` before `make up`.

When you're done:

```bash
make demo-stop    # stop the stream
make down         # stop the stack too, keep the data
make clean        # stop everything and DELETE the volumes, ClickHouse included
```

Next:

- Feed it your own data: [Docker quickstart](docker.md).
- What the demo creates, and its rate and protocol settings: [Taking DFE for a first look](../dfe-docker/evaluating.md).
- How the pieces fit: [how the Compose stack fits together](../dfe-docker/architecture.md).
