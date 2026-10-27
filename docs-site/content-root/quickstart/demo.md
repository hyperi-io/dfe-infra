---
sidebar_position: 1
sidebar_label: The demo
---

# Quickstart: the demo

Raw firewall syslog in, one parsed ClickHouse table per vendor out, on one host.

You need Docker with Compose v2, Python 3 and [uv](https://docs.astral.sh/uv/). `<version>` is the DFE release you are running, the one in the version menu at the top of this page.

```bash
git clone https://github.com/hyperi-io/dfe-docker && cd dfe-docker
make init                                   # write .env and mint the logins
make stack VERSION=<version>                # pin every image to that release
echo 'DFE_PROFILE=single-kafka' >> .env     # the profile that runs the Elastic transform
make up                                     # pull, start, self test, print the console login
make demo                                   # stream firewall syslog until make demo-stop
```

`make up` ends with a self test, and each check prints `PASS` once its events reach ClickHouse. Then `make demo` replays Cisco ASA and FortiGate logs as raw syslog, 20 lines a second over TCP. dfe-transform-elastic works out which device sent each line, parses it, and lands it in `dfe.cisco-asa` or `dfe.fortinet`. A line no vendor claims goes to `dfe.demo-syslog`.

It returns once every vendor table has gained rows, and the stream keeps going. Each vendor has about 6,000 sample lines, so it runs roughly ten minutes before a line repeats. Look at it in the console at http://localhost:3000 with the login `make up` printed (`make creds` prints it again).

When you are done:

```bash
make demo-stop    # stop the stream
make down         # stop the stack too, keep the data
make clean        # stop everything and DELETE the volumes, ClickHouse included
```

Next:

- Feed it your own data: [Docker quickstart](docker.md).
- What the demo creates, and its rate and protocol settings: [Taking DFE for a first look](../dfe-docker/evaluating.md).
- How the pieces fit: [how the Compose stack fits together](../dfe-docker/architecture.md).
