---
sidebar_position: 1
sidebar_label: The demo
---

# Quickstart: the demo

Raw firewall syslog in, one parsed ClickHouse table per vendor out, on one host.

You need Docker with Compose v2, Python 3, git and make. It runs the newest DFE release, and `VERSION=` on the make line pins another.

<!-- landing -->
```bash
git clone https://github.com/hyperi-io/dfe-docker && cd dfe-docker
make demo      # start the stack, self test, then stream firewall syslog until make demo-stop
```

`make demo` starts the stack first, and its self test prints `PASS` as each check's events reach ClickHouse. Then it replays Cisco ASA and FortiGate logs as raw syslog, 20 lines a second over TCP. dfe-transform-elastic works out which device sent each line, parses it, and lands it in `dfe.cisco-asa` or `dfe.fortinet`. A line no vendor claims goes to `dfe.demo-syslog`.

It returns once every vendor table has rows, and the stream keeps going. Each vendor has about 6,000 sample lines, so it runs roughly ten minutes before a line repeats. Watch it in the console at http://localhost:3000 with the login `make up` printed (`make creds` prints it again).

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
