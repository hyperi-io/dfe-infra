---
sidebar_position: 1
sidebar_label: The demo
---

# Quickstart: the demo

Raw firewall syslog in, one parsed ClickHouse table per vendor out, on one host.

<!-- docker-quickstart: demo -->
You need Docker with Compose v2, Python 3, git and make.

<!-- landing -->
```bash
git clone https://github.com/hyperi-io/dfe-docker && cd dfe-docker
make demo      # start the stack, self test, then stream firewall syslog until make demo-stop
```

Watch it in the console at http://localhost:3000 with the login `make demo` printed.
<!-- /docker-quickstart -->

## From a checkout

Run the demo from the dfe-docker repo to change its rate or protocol, or to keep the stack afterwards. You need Python 3, git and make as well as Docker.

```bash
git clone https://github.com/hyperi-io/dfe-docker && cd dfe-docker
make demo                       # newest release, 20 lines a second over TCP
make demo DEMO_RATE=100         # a faster stream
make demo DEMO_PROTOCOL=udp     # syslog over UDP
```

`make demo` starts the stack first, and its self test prints `PASS` as each check's events reach ClickHouse. Then it replays Cisco ASA and FortiGate logs as raw syslog. dfe-transform-elastic works out which device sent each line, parses it, and lands it in `dfe.cisco-asa` or `dfe.fortinet`. A line no vendor claims goes to `dfe.demo-syslog`.

It returns once every vendor table has rows, and the stream keeps going. Each vendor has about 6,000 sample lines, so it runs roughly ten minutes before a line repeats. `make creds` prints the console login again.

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
