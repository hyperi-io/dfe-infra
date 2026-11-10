# The deployment wizard -- `dfe-ops init`

`dfe-ops init` walks you through the fields in `deployment.example.yaml` and writes a `deployment.yaml` dial. Defaults come from that file. Every non-trivial answer is checked by calling `render_dial.py`'s or `resolve_sizing.py`'s own functions, so there's no second copy of their rules to drift out of step.

```bash
python3 scripts/dfe-ops init                          # interactive
python3 scripts/dfe-ops init --answers answers.env     # non-interactive (CI)
python3 scripts/dfe-ops init --dry-run                 # print the dial, write nothing
```

Every question shows its default in brackets, and Enter takes it. A refused answer re-prompts with the validator's own message.

## The nine questions

1. Target. `on-prem` (an existing cluster) or a cloud. `aws` is the only cloud today, and `gcp`/`azure` are named but refused because neither has a tofu root yet. A cloud target also asks the AWS account, region and VPC CIDR.
2. Profile/tier, ingest, focus. The three Kubernetes tiers are labelled Small/Medium/Large for the prompt. The real dial value is `slim`/`single`/`scale` (see `scripts/profiles.py`). Estimated ingest is in GB/day, and blank means "no estimate" (the tyre-kick floor `resolve_sizing.py` reports rather than targets). Focus is `economy` (default, 40% headroom), `balanced` (60%) or `performance` (100%, high-IOPS storage), straight from `sizing/sizing.yaml`'s own `focus:` table.
3. Kafka provider and quorum. One of `strimzi`, `redpanda`, `msk`, `confluent-cloud`, `redpanda-cloud`, pre-selected by tier: `msk` on AWS Small/Medium, `confluent-cloud` at Large, `strimzi` on-prem. `msk` asks the broker count. An in-cluster broker also asks `kafka.controller_pool`. `combined` (the default, and the chart's own) keeps the KRaft metadata quorum on the brokers, and `separate` gives it a sized pool. Moving it on a live cluster re-forms the quorum, so `sizing.yaml` locks it as `controller_mode`.
4. ClickHouse storage model. `sizing.storage_model` defaults to `auto`, where the chart derives it from whether an object-store endpoint exists. `cached-object` forces the object store and refuses to render without one. `local` forces local storage even with one configured. Switching later is a data migration, not a values edit ([storage.md](storage.md)). The wizard also offers the one other lever on ClickHouse sizing: an instance-type override.
5. Public UIs, OIDC, CIDR, DNS. `dfe-ui` is public by default and every admin UI is opt-in, one at a time. The moment any UI is public, the wizard asks you to confirm OIDC will be wired up for it ([gateway-oidc.md](gateway-oidc.md)) and refuses to continue without that. There's no OIDC dial field, so this is a wizard-side gate, not a renderer validation. Then comes an optional CIDR allow-list, any CDN's ranges, and the public DNS zone name. The answers land in the dial's `edge:` block: `dfe-ui` under `edge.product.public` and the admin UIs under `edge.admin_uis.public.*`. That one block replaced the old top-level `ui:` and `ingest:`.
6. Telemetry sink. AWS only. `otel` is the default (DFE's own policy). `cloudwatch` is opt-in and asks for a one-line reason, which lands as a comment beside the field.
7. Lifecycle and AZ count. `ephemeral` or `persistent`, and `network.az_count` (2-6, default 3).
8. Toolbox. The in-cluster pod (any target) and, on AWS, the on-demand SSM-managed instance. Both are opt-in, and the instance asks for the operator IAM role ARN once enabled. See [toolbox.md](toolbox.md).
9. Sizing overrides. "Override any sizing?" Only if you say yes does it walk the documented workload names and their fields (cpu, memory, disk_gb, replicas, instance_type, iops, throughput_mibs), checked by the exact function a hand-edited dial is checked against.

## What it writes, and what it leaves blank

The template ships some estate-specific fields blank on purpose: `k8s.repo_url`, `state.bucket`, `state.region`, `tags.owner`, `tags.cost-center`. They stay blank. The operator's own tooling injects them from the deployment's secrets backend at deploy time, or an operator fills them by hand. `dfe-ops init` names them in its final output on an AWS target.

## Sizing

Pass `--fixtures <dir>` (captured API answers, `scripts/resolve_sizing.py capture`) or `--live` and, once the dial is written, `dfe-ops init` runs `scripts/resolve_sizing.py resolve` against it. It prints the report path plus the monthly compute line. That only happens for the `scale` tier, because `resolve_sizing.py` sizes the scale tier only. Every other profile is skipped with a one-line note rather than a crash.

## Non-interactive mode

`--answers PATH` reads a flat `key=value` file (`envfile.py`, the same reader `bootstrap/.env` uses, so no YAML lists) and drives the identical flow with no prompts. An answer that fails validation refuses immediately, since there's nobody to re-ask. CI and `scripts/tests/test_dfe_ops_init.py` use it to exercise the wizard without a terminal. `--dry-run` prints the resulting dial and writes nothing, so you can preview an answers file before it lands.

## Related

- [../../deployment.example.yaml](../../deployment.example.yaml) -- the schema this wizard walks, where every field and default traces back
- [../../scripts/render_dial.py](../../scripts/render_dial.py) -- the validators the wizard calls, and what actually reads the written dial
- [README.md](README.md) -- the three layers and the values cascade
- [toolbox.md](toolbox.md) -- the two troubleshooting surfaces question 8 asks about
- [gateway-oidc.md](gateway-oidc.md) -- what question 5's OIDC gate is asking you to configure
