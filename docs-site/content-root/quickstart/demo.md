---
sidebar_position: 1
sidebar_label: The demo
---

# Quickstart: the demo

Raw firewall syslog in, one parsed ClickHouse table per vendor out, on one host. The fetcher pulls two live API feeds beside it: Wikipedia's recent changes, and the latest GitHub release of each DFE component.

<!-- docker-quickstart: demo -->
You need Docker with Compose v2, Python 3, git and make. It runs the newest DFE release, and `VERSION=` on the make line pins another.

```bash
git clone https://github.com/hyperi-io/dfe-docker && cd dfe-docker
make demo      # start the stack, self test, stream firewall syslog
```

Watch it in the console at http://localhost:3000. `make creds` in a terminal prints the login.
<!-- /docker-quickstart -->

## From a checkout

To change the demo's rate or protocol, or to keep the stack afterwards, run it from a dfe-docker checkout. That needs Python 3, git and make as well as Docker.

```bash
git clone https://github.com/hyperi-io/dfe-docker && cd dfe-docker
make demo                       # newest release, 20 lines a second over TCP
make demo DEMO_RATE=200         # a faster stream
make demo DEMO_PROTOCOL=udp     # syslog over UDP
```

When no stack is running, `make demo` starts one and runs its self test, which prints `PASS` per check. Then it replays Cisco ASA and FortiGate logs as raw syslog. dfe-transform-elastic works out which device sent each line, parses it, and lands it in `dfe.cisco-asa` or `dfe.fortinet`. A line no vendor claims goes to `dfe.demo-syslog`.

It returns once every vendor table has gained rows, and the stream keeps going. Each vendor has about 6,000 sample lines, so it runs roughly ten minutes before a line repeats. On a stack already running the demo, run `make demo-stop` before `make up`.

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
