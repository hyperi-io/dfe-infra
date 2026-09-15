# The deployment wizard -- `dfe-ops init`

`dfe-ops init` is a prompt-driven walk over `deployment.example.yaml`'s own
fields. It writes a `deployment.yaml` dial, never a new schema: every field
and every default it writes already exists in the committed template, and
every non-trivial answer is validated by calling `render_dial.py`'s or
`resolve_sizing.py`'s own functions -- there is no second copy of their
rules to drift out of step.

```
python3 scripts/dfe-ops init                          # interactive
python3 scripts/dfe-ops init --answers answers.env     # non-interactive (CI)
python3 scripts/dfe-ops init --dry-run                 # print the dial, write nothing
```

Every question shows its default in brackets; Enter takes it. A refused
answer re-prompts with the validator's own message -- there is only one
place a `kafka.provider` token or an `az_count` gets judged, and it is the
renderer, not the wizard.

## The nine questions

1. **Target.** `on-prem` (an existing cluster) or a cloud -- `aws` today;
   `gcp`/`azure` are named but refused, since neither has a tofu root yet.
   A cloud target also asks the AWS account, region and VPC CIDR.
2. **Profile/tier, ingest, focus.** The three Kubernetes tiers, labelled
   Small/Medium/Large for the prompt (`slim`/`single`/`scale` is the real
   dial value -- see `scripts/profiles.py`). Estimated ingest in GB/day,
   blank for "no estimate" (the tyre-kick floor `resolve_sizing.py`
   reports rather than targets). Focus is `economy` (default, 40%
   headroom), `balanced` (60%) or `performance` (100%, high-IOPS storage)
   -- straight from `sizing/sizing.yaml`'s own `focus:` table.
3. **Kafka provider.** One of `strimzi`, `redpanda`, `msk`,
   `confluent-cloud`, `redpanda-cloud`, pre-selected by tier: `msk` on AWS
   Small/Medium, `confluent-cloud` at Large, `strimzi` on-prem. `msk` asks
   the broker count; the two SaaS providers ask for an extra landing topic
   beyond `main_land`, if you need one.
4. **ClickHouse storage model.** `sizing.storage_model`, default `auto`: the
   chart derives it, `cached-object` when an object-store endpoint exists
   (always on a populated cloud, on-prem once MinIO or similar is supplied)
   and `local` otherwise. `cached-object` forces the object store and refuses
   to render with no endpoint; `local` forces local storage even with an
   endpoint configured (switching later is a data migration, not a values
   edit -- see [storage.md](storage.md)). The wizard also offers the one
   other lever that touches ClickHouse sizing: an instance-type override.
5. **Public UIs, OIDC, CIDR, DNS.** `dfe-ui` is public by default; every
   admin UI is opt-in, one at a time. The moment any UI is public, the
   wizard demands confirmation that OIDC will be wired up for it
   ([gateway-oidc.md](gateway-oidc.md)) and **refuses to continue without
   one** -- there is no OIDC dial field, so this is a wizard-side gate, not
   a renderer validation. Then an optional CIDR allow-list with its
   required trusted-proxy CIDRs, and the public DNS zone name.
6. **Telemetry sink.** AWS only -- `otel` by default (DFE's own policy);
   `cloudwatch` is opt-in and asks for the one-line reason, which lands as
   a comment beside the field.
7. **Lifecycle and AZ count.** `ephemeral` or `persistent`, and
   `network.az_count` (2-6, default 3).
8. **Toolbox.** The in-cluster pod (any target) and, on AWS, the on-demand
   SSM-managed instance -- opt-in, asking for the operator IAM role ARN
   once enabled. See [toolbox.md](toolbox.md).
9. **Sizing overrides.** "Override any sizing?" -- only then does it walk
   the documented workload names (`eks-system`, `general`, `kafka-broker`,
   `kraft-controller`, `clickhouse`, `keeper`, `ci-burst`, `msk-broker`,
   `toolbox`) and their fields (cpu, memory, disk_gb, replicas,
   instance_type, iops, throughput_mibs), validated through the exact
   function a hand-edited dial is checked against.

## What it writes, and what it deliberately does not

The dial it writes is the schema's own shape: nothing invented, and every
default copied from `deployment.example.yaml`. Estate-specific fields the
template ships blank on purpose -- `k8s.repo_url`, `state.bucket`,
`state.region`, `tags.owner`, `tags.cost-center` -- stay blank. Those are
"hyperi-infra's thin caller injects them from OpenBao at deploy time, or an
operator fills them by hand" fields, not the wizard's to invent, and
`dfe-ops init` names them explicitly in its final output on an AWS target.

## Sizing, end to end

Pass `--fixtures <dir>` (captured API answers, `scripts/resolve_sizing.py
capture`) or `--live` and, once the dial is written, `dfe-ops init` runs
`scripts/resolve_sizing.py resolve` against it and prints the report path
plus the monthly compute line. This only happens for the `scale` tier --
`resolve_sizing.py` sizes the scale tier only, and every other profile is
skipped with a one-line note rather than a crash.

## Non-interactive mode

`--answers PATH` reads a flat `key=value` file (`envfile.py`, the same
reader `bootstrap/.env` uses -- no YAML lists) and drives the identical
flow with no prompts: an answer that fails validation refuses immediately,
since there is nobody to re-ask. This is what CI and
`scripts/tests/test_dfe_ops_init.py` use to exercise the wizard without a
terminal. `--dry-run` prints the resulting dial and writes nothing, for
previewing an answers file before it lands.

## Related

- [../../deployment.example.yaml](../../deployment.example.yaml) -- the
  schema this wizard walks; every field and default traces back here
- [../../scripts/render_dial.py](../../scripts/render_dial.py) -- the
  validators the wizard calls, and what actually reads the written dial
- [index.md](index.md) -- the three layers and the values cascade
- [toolbox.md](toolbox.md) -- the two troubleshooting surfaces question 8 asks about
- [gateway-oidc.md](gateway-oidc.md) -- what question 5's OIDC gate is asking you to configure
