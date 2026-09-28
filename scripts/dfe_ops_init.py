#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe_ops_init.py
#  Purpose:      `dfe-ops init` -- the prompt-driven deployment-dial wizard.
#                Walks an operator through deployment.example.yaml's own
#                fields in a fixed order, validates every answer with the
#                SAME functions render_dial.py and resolve_sizing.py use at
#                render time (imported, never re-implemented), and writes a
#                deployment.yaml the renderer accepts. Split into its own
#                module the way tester_idp.py and dfe_ops_bastion.py already
#                are, and imported into dfe-ops's build_parser() the same way.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-ops init -- the deployment-dial wizard.

    dfe-ops init [--out deployment.yaml] [--force]
    dfe-ops init --answers wizard-answers.env [--dry-run]
    dfe-ops init --answers wizard-answers.env --fixtures shapes/fixtures/aws

Walks nine questions -- target, tier/ingest/focus, Kafka provider and quorum, the
ClickHouse storage model, public UIs + OIDC + CIDR allow-list, telemetry sink,
lifecycle + AZ count, the toolbox, and a deployer sizing override -- in the
order docs/deployment/wizard.md documents, over deployment.example.yaml's own
field set. Nothing here invents a dial field or a default: every value this
module writes is copied from deployment.example.yaml's own committed default,
and every non-trivial answer is validated by CALLING render_dial.py's or
resolve_sizing.py's own private validators (``render_dial._az_count``,
``render_dial._telemetry``, ``resolve_sizing._dial_number``,
``resolve_sizing._read_overrides``, ...) rather than re-implementing their
rules -- the renderer is the one source of truth for what a dial field means.
The one exception is ``version.pin``: it is versions.yaml's ``current`` pointer,
the value the template's own pin is drift-checked against.

Interactive mode prints each question with its default in brackets; Enter
takes the default. A refused answer re-prompts with the validator's own
message. ``--answers PATH`` (a flat ``key=value`` file, read with envfile.py
-- the same reader bootstrap/.env uses) drives the same flow non-interactively
for tests and CI: an answer that fails validation refuses immediately rather
than looping, since there is no one to re-ask. ``--dry-run`` prints the
resulting dial to stdout and writes nothing.

When ``--fixtures`` or ``--live`` is given AND the chosen profile is ``scale``
(resolve_sizing.py sizes the scale tier only -- deployment.example.yaml's own
comment says so), this also runs ``scripts/resolve_sizing.py resolve`` against
the written dial and prints its report path plus the monthly compute line.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import envfile  # noqa: E402
import profiles  # noqa: E402
import render_dial  # noqa: E402
import resolve_sizing  # noqa: E402
import yaml_subset  # noqa: E402

DIAL_TEMPLATE = REPO_ROOT / "deployment.example.yaml"
DEFAULT_DIAL_OUT = REPO_ROOT / "deployment.yaml"
RESOLVE_SIZING = SCRIPTS / "resolve_sizing.py"


class InitError(RuntimeError):
    """The wizard cannot continue -- the message is what `dfe-ops init` prints."""


# ---------------------------------------------------------------------------
# Reusable dial-value tables. Every choice list here is either the renderer's
# own public constant, or a set of tokens read straight from
# deployment.example.yaml's own comments -- never a value invented here.
# ---------------------------------------------------------------------------

TARGET_CHOICES = ("on-prem", "aws", "gcp", "azure")
NOT_YET_AVAILABLE = ("gcp", "azure")

# The three Kubernetes-brokered tiers from profiles.py, labelled the way the
# wizard's flow spec names them (Small/Medium/Large) purely for the prompt --
# the dial field itself is always the real token (slim/single/scale).
TIER_CHOICES = ("slim", "single", "scale")
TIER_LABELS = {"slim": "Small", "single": "Medium", "scale": "Large"}

# sizing.yaml's own focus.<level>.headroom values (2026-09-13): 0.40/0.60/1.00.
FOCUS_HELP = {
    "economy": "the minimum that could do it -- 40% headroom over the sized peak.",
    "balanced": "60% headroom, one generation newer where the price step is under 10%.",
    "performance": "100% headroom (2x), high-IOPS storage with an instance-store cache.",
}

KAFKA_TOKENS = ("strimzi", "redpanda", "msk", "confluent-cloud", "redpanda-cloud")

LIFECYCLE_CHOICES = render_dial.LIFECYCLE_VALUES  # ("ephemeral", "persistent")

OVERRIDE_FIELDS = resolve_sizing.NODE_OVERRIDES + resolve_sizing.SHAPE_OVERRIDES


def _default_kafka_provider(target: str, tier: str) -> str:
    """msk on AWS Small/Medium, confluent-cloud on AWS Large, strimzi on-prem."""
    if target != "aws":
        return "strimzi"
    return "confluent-cloud" if tier == "scale" else "msk"


def _focus_choices() -> tuple[str, ...]:
    """The focus levels sizing.yaml declares, read from the SSoT itself."""
    try:
        sizing = resolve_sizing._load(resolve_sizing.SIZING_FILE)
    except (OSError, yaml_subset.YamlSubsetError):
        return tuple(FOCUS_HELP)
    node = sizing.get("focus")
    if not isinstance(node, dict):
        return tuple(FOCUS_HELP)
    return tuple(k for k in node if k != "invariants")


def _wrap(path: tuple[str, ...], value: str) -> dict[str, object]:
    """A single-leaf nested dict, for calling a renderer validator on one field."""
    node: dict[str, object] = {path[-1]: value}
    for key in reversed(path[:-1]):
        node = {key: node}
    return node


# ---------------------------------------------------------------------------
# Prompting core
# ---------------------------------------------------------------------------


@dataclass
class WizardIO:
    """The wizard's one boundary to the outside world -- stdin/stdout for an
    interactive run, or a pre-loaded answers dict for --answers/tests."""

    answers: dict[str, str] | None = None
    read_line: object = input
    write_out: object = None  # defaults to print(..., file=sys.stderr)

    def write(self, text: str) -> None:
        if self.write_out is not None:
            self.write_out(text)
        else:
            print(text, file=sys.stderr)


def ask(
    io: WizardIO,
    key: str,
    question: str,
    default: str,
    *,
    validate: object = None,
) -> str:
    """One question. Interactive: prompt, re-ask on refusal. --answers: look
    the key up, validate once, refuse immediately (no one to re-ask)."""
    if io.answers is not None:
        raw = io.answers.get(key, default)
        if validate is None:
            return raw
        try:
            return validate(raw)
        except ValueError as err:
            raise InitError(f"--answers {key}={raw!r}: {err}") from err
    while True:
        raw = io.read_line(f"{question} [{default}]: ").strip()
        if not raw:
            raw = default
        if validate is None:
            return raw
        try:
            return validate(raw)
        except ValueError as err:
            io.write(f"  {err}")
            continue


def ask_bool(
    io: WizardIO,
    key: str,
    question: str,
    default: bool,
    *,
    require_true_message: str | None = None,
) -> bool:
    """A yes/no question, stored and re-asked as true/false. When
    `require_true_message` is set, only a true answer is accepted -- a
    wizard-level policy gate (e.g. the OIDC confirmation), not a dial field."""
    hint = "[Y/n]" if default else "[y/N]"

    def validate(raw: str) -> str:
        token = raw.strip().lower()
        if token in ("y", "yes", "true"):
            value = True
        elif token in ("n", "no", "false"):
            value = False
        else:
            raise ValueError(f"{key} must be y/yes/true or n/no/false, got {raw!r}")
        if require_true_message and not value:
            raise ValueError(require_true_message)
        return "true" if value else "false"

    result = ask(io, key, f"{question} {hint}", "true" if default else "false", validate=validate)
    return result == "true"


def ask_choice(io: WizardIO, key: str, question: str, default: str, choices: tuple[str, ...]) -> str:
    """A plain membership prompt -- validated against `choices`, no renderer
    call needed because the renderer places no format constraint of its own."""

    def validate(raw: str) -> str:
        if raw not in choices:
            raise ValueError(f"{key} must be one of {', '.join(choices)}, got {raw!r}")
        return raw

    return ask(io, key, f"{question} ({'/'.join(choices)})", default, validate=validate)


def ask_required(io: WizardIO, key: str, question: str, default: str = "") -> str:
    """A prompt that refuses a blank answer -- for the dial fields render_dial.py
    marks `_required` once the surrounding choice makes them apply."""

    def validate(raw: str) -> str:
        if not raw.strip():
            raise ValueError(f"{key} is required, cannot be blank")
        return raw.strip()

    return ask(io, key, question, default, validate=validate)


# ---------------------------------------------------------------------------
# The collected answers
# ---------------------------------------------------------------------------


@dataclass
class Answers:
    name: str = "dfe"
    target: str = "on-prem"
    existing_cluster_ref: str = ""
    existing_namespace: str = "dfe"
    aws_account: str = ""
    aws_region: str = "ap-southeast-2"
    aws_cidr: str = "10.42.0.0/16"
    profile: str = "single"
    ingest_gb_per_day: str = ""
    focus: str = "economy"
    kafka_provider: str = "strimzi"
    controller_pool: str = "combined"
    msk_broker_count: str = "3"
    kafka_extra_topic: str = ""
    clickhouse_storage_model: str = "auto"
    clickhouse_instance_override: str = ""
    ui_public: dict[str, bool] = field(
        default_factory=lambda: {
            "dfe_ui": True,
            "kafbat": False,
            "cruise_control": False,
            "hyperdx": False,
            "argocd": False,
            "links": False,
        }
    )
    ui_allowed_cidrs: str = ""
    ui_trusted_proxy_cidrs: str = ""
    public_zone: str = ""
    telemetry_sink: str = "otel"
    telemetry_reason: str = ""
    lifecycle: str = "ephemeral"
    az_count: str = "3"
    toolbox_pod_enabled: bool = False
    toolbox_aws_enabled: bool = False
    toolbox_operator_role_arn: str = ""
    overrides: dict[str, dict[str, str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The nine steps (plus the target preamble every other step depends on)
# ---------------------------------------------------------------------------


def step_target(io: WizardIO, a: Answers) -> None:
    a.name = ask(io, "name", "Deployment name (metadata.name)", a.name)

    def validate_target(raw: str) -> str:
        if raw not in TARGET_CHOICES:
            raise ValueError(f"target must be one of {', '.join(TARGET_CHOICES)}, got {raw!r}")
        if raw in NOT_YET_AVAILABLE:
            raise ValueError(f"{raw}: not yet available -- terraform/environments/{raw} does not exist yet")
        if raw == "aws" and not (render_dial.TOFU_ROOTS / raw).is_dir():
            raise ValueError(f"{raw}: no tofu root at terraform/environments/{raw}")
        return raw

    a.target = ask(
        io, "target",
        "Target (on-prem or a cloud -- aws today; gcp/azure not yet available)",
        a.target, validate=validate_target,
    )

    if a.target == "on-prem":
        a.existing_cluster_ref = ask(
            io, "existing_cluster_ref",
            "Existing cluster ref (kubeconfig context / Argo cluster secret; blank = current context)",
            a.existing_cluster_ref,
        )
        a.existing_namespace = ask_required(
            io, "existing_namespace", "Target namespace", a.existing_namespace,
        )
        return

    # aws
    a.aws_account = ask_required(io, "aws_account", "AWS account id (target.provision.account)", a.aws_account)
    a.aws_region = ask_required(io, "aws_region", "AWS region", a.aws_region)
    a.aws_cidr = ask_required(io, "aws_cidr", "VPC CIDR (must not overlap what you peer with, nor 172.31.0.0/16)", a.aws_cidr)


def step_tier(io: WizardIO, a: Answers) -> None:
    io.write("Tiers: " + ", ".join(
        f"{TIER_LABELS[t]} ({t}) -- {profiles.PROFILES[t].description}" for t in TIER_CHOICES
    ))
    a.profile = ask_choice(io, "profile", "Profile/tier", a.profile, TIER_CHOICES)

    def validate_ingest(raw: str) -> str:
        if not raw.strip():
            return ""
        try:
            resolve_sizing._dial_number(_wrap(("sizing", "ingest_gb_per_day"), raw), "sizing", "ingest_gb_per_day")
        except resolve_sizing.ResolveError as err:
            raise ValueError(str(err)) from err
        return raw.strip()

    a.ingest_gb_per_day = ask(
        io, "ingest_gb_per_day",
        "Estimated ingest GB/day (blank = no estimate, the tyre-kick floor)",
        a.ingest_gb_per_day, validate=validate_ingest,
    )

    focus_choices = _focus_choices() or tuple(FOCUS_HELP)
    io.write("Focus: " + ", ".join(f"{f} -- {FOCUS_HELP.get(f, '')}" for f in focus_choices))
    a.focus = ask_choice(io, "focus", "Focus", a.focus, focus_choices)


def step_kafka(io: WizardIO, a: Answers) -> None:
    default_provider = _default_kafka_provider(a.target, a.profile)

    def validate_provider(raw: str) -> str:
        if raw not in resolve_sizing.KAFKA_PROVIDERS:
            raise ValueError(
                f"kafka provider must be one of {', '.join(KAFKA_TOKENS)}, got {raw!r}"
            )
        return raw

    a.kafka_provider = ask(
        io, "kafka_provider",
        f"Kafka provider ({', '.join(KAFKA_TOKENS)})",
        default_provider, validate=validate_provider,
    )

    if a.kafka_provider not in render_dial.MANAGED_KAFKA_PROVIDERS:
        io.write(
            "KRaft metadata quorum: combined runs it on the brokers and is the chart's own "
            "default; separate gives it a controller pool of its own, which the resolver "
            "then sizes. Moving it later on a live cluster re-forms the quorum."
        )
        a.controller_pool = ask_choice(
            io, "controller_pool", "KRaft metadata quorum",
            a.controller_pool, render_dial.CONTROLLER_POOLS,
        )

    if a.kafka_provider == "msk":
        def validate_count(raw: str) -> str:
            try:
                render_dial._number(_wrap(("kafka", "msk", "broker_count"), raw), ("kafka", "msk", "broker_count"))
            except render_dial.DialError as err:
                raise ValueError(str(err)) from err
            return raw

        a.msk_broker_count = ask(
            io, "msk_broker_count", "MSK broker count", a.msk_broker_count, validate=validate_count,
        )
    elif a.kafka_provider in ("confluent-cloud", "redpanda-cloud"):
        a.kafka_extra_topic = ask(
            io, "kafka_extra_topic",
            "Extra landing topic beyond main_land (blank = main_land only)",
            a.kafka_extra_topic,
        )


STORAGE_MODEL_CHOICES = ("auto", "cached-object", "local")


def step_clickhouse_storage(io: WizardIO, a: Answers) -> None:
    io.write(
        "ClickHouse storage model: auto -- the chart derives it, cached-object when an "
        "object-store endpoint exists (always on a populated cloud, on-prem once MinIO "
        "or similar is supplied) and local otherwise. cached-object -- force the object "
        "store; refuses to render with no endpoint. local -- force local storage even "
        "with an endpoint configured; switching to cached-object later is a data "
        "migration, not a values edit."
    )

    def validate_storage_model(raw: str) -> str:
        value = raw.lower()
        if value not in STORAGE_MODEL_CHOICES:
            raise ValueError(
                f"clickhouse storage model must be one of {', '.join(STORAGE_MODEL_CHOICES)}, got {raw!r}"
            )
        return value

    a.clickhouse_storage_model = ask(
        io, "clickhouse_storage_model",
        f"ClickHouse storage model ({', '.join(STORAGE_MODEL_CHOICES)})",
        a.clickhouse_storage_model, validate=validate_storage_model,
    )
    a.clickhouse_instance_override = ask(
        io, "clickhouse_instance_override",
        "Override the ClickHouse instance type used for sizing (blank = let the resolver choose)",
        a.clickhouse_instance_override,
    )


def step_public_uis(io: WizardIO, a: Answers) -> None:
    a.ui_public["dfe_ui"] = ask_bool(io, "ui_dfe_ui", "Public: dfe-ui", a.ui_public["dfe_ui"])
    for name in ("kafbat", "cruise_control", "hyperdx", "argocd", "links"):
        a.ui_public[name] = ask_bool(io, f"ui_{name}", f"Public admin UI: {name}", a.ui_public[name])

    if any(a.ui_public.values()):
        ask_bool(
            io, "oidc_confirmed",
            "At least one UI is public -- confirm OIDC will be configured for it "
            "(docs/deployment/gateway-oidc.md) before this goes live",
            False,
            require_true_message=(
                "a public UI needs OIDC wired up (dfe-engine + envoy-gateway-config oidc.* "
                "values in your deploy-repo overlay) -- confirm oidc_confirmed=true, or turn "
                "every ui_* flag off"
            ),
        )

    a.ui_allowed_cidrs = ask(
        io, "ui_allowed_cidrs", "UI allow-list CIDRs, comma-separated (blank = any address)", a.ui_allowed_cidrs,
    )
    if a.ui_allowed_cidrs:
        a.ui_trusted_proxy_cidrs = ask_required(
            io, "ui_trusted_proxy_cidrs",
            "Trusted proxy CIDRs (required once an allow-list is set -- the load balancer's "
            "subnets, plus any CDN in front)",
            a.ui_trusted_proxy_cidrs or a.ui_allowed_cidrs,
        )
    a.public_zone = ask(io, "public_zone", "Public DNS zone (blank = no public UI is reachable yet)", a.public_zone)


def step_telemetry(io: WizardIO, a: Answers) -> None:
    if a.target != "aws":
        io.write("Telemetry: no cloud telemetry block for on-prem -- otel default kept.")
        return

    def validate_sink(raw: str) -> str:
        try:
            render_dial._telemetry(_wrap(("telemetry", "aws", "sink"), raw), "aws")
        except render_dial.DialError as err:
            raise ValueError(str(err)) from err
        return raw

    a.telemetry_sink = ask(
        io, "telemetry_sink", "Telemetry sink (otel | cloudwatch)", a.telemetry_sink, validate=validate_sink,
    )
    if a.telemetry_sink == "cloudwatch":
        a.telemetry_reason = ask_required(
            io, "telemetry_reason", "One-line reason for CloudWatch over otel", a.telemetry_reason,
        )


def step_lifecycle(io: WizardIO, a: Answers) -> None:
    a.lifecycle = ask_choice(io, "lifecycle", "Lifecycle", a.lifecycle, LIFECYCLE_CHOICES)

    def validate_az(raw: str) -> str:
        try:
            render_dial._az_count(_wrap(("network", "az_count"), raw))
        except render_dial.DialError as err:
            raise ValueError(str(err)) from err
        return raw

    a.az_count = ask(io, "az_count", "network.az_count (2-6)", a.az_count, validate=validate_az)


def step_toolbox(io: WizardIO, a: Answers) -> None:
    a.toolbox_pod_enabled = ask_bool(
        io, "toolbox_pod_enabled", "Enable the in-cluster toolbox pod", a.toolbox_pod_enabled,
    )
    if a.target != "aws":
        return
    a.toolbox_aws_enabled = ask_bool(
        io, "toolbox_aws_enabled", "Enable the on-demand SSM-managed toolbox instance", a.toolbox_aws_enabled,
    )
    if a.toolbox_aws_enabled:
        a.toolbox_operator_role_arn = ask_required(
            io, "toolbox_operator_role_arn",
            "Operator IAM role ARN (the EKS access entry toolbox up grants)",
            a.toolbox_operator_role_arn,
        )


def step_overrides_interactive(io: WizardIO, a: Answers) -> None:
    do_override = ask_bool(io, "override_sizing", "Override any sizing?", False)
    if not do_override:
        return
    while True:
        workload = ask(
            io, "override_workload_next",
            f"Workload to override (blank to finish -- one of {', '.join(resolve_sizing.USE_CASES)})",
            "",
        )
        if not workload:
            return
        if workload not in resolve_sizing.USE_CASES:
            io.write(f"  not a use case -- one of {', '.join(resolve_sizing.USE_CASES)}")
            continue
        fields: dict[str, str] = {}
        for name in OVERRIDE_FIELDS:
            value = ask(io, f"override_{workload}_{name}_next", f"  {workload}.{name} (blank = skip)", "")
            if value:
                fields[name] = value
        if fields:
            a.overrides[workload] = fields


def _overrides_from_answers(raw: dict[str, str]) -> dict[str, dict[str, str]]:
    """--answers mode: collect every `override.<workload>.<field>=value` key."""
    out: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if not key.startswith("override.") or not value:
            continue
        parts = key.split(".", 2)
        if len(parts) != 3:
            continue
        _, workload, name = parts
        out.setdefault(workload, {})[name] = value
    return out


def step_overrides(io: WizardIO, a: Answers) -> None:
    if io.answers is not None:
        a.overrides = _overrides_from_answers(io.answers)
    else:
        step_overrides_interactive(io, a)
    if not a.overrides:
        return
    # The final, authoritative pass: the SAME function resolve_sizing.py's own
    # read_dial calls, so a wizard-collected override is refused exactly the
    # way a hand-edited one would be -- never a second copy of its rules.
    tree = {"sizing": {"overrides": a.overrides}}
    try:
        a.overrides = resolve_sizing._read_overrides(tree)
    except resolve_sizing.ResolveError as err:
        raise InitError(str(err)) from err


STEPS = (
    step_target,
    step_tier,
    step_kafka,
    step_clickhouse_storage,
    step_public_uis,
    step_telemetry,
    step_lifecycle,
    step_toolbox,
    step_overrides,
)


def run_wizard(io: WizardIO) -> Answers:
    a = Answers()
    for step in STEPS:
        step(io, a)
    return a


# ---------------------------------------------------------------------------
# Rendering the dial -- every field and default below is copied from
# deployment.example.yaml; nothing here is a field the renderer does not read.
# ---------------------------------------------------------------------------


def _bool(value: bool) -> str:
    return "true" if value else "false"


def current_stack() -> str:
    """The stack a new dial pins: versions.yaml's `current`, as `dfe-stack current` reads it."""
    path = render_dial.VERSIONS_FILE
    try:
        tree = yaml_subset.parse(path.read_text(encoding="utf-8", errors="replace"), source=str(path))
    except (OSError, yaml_subset.YamlSubsetError) as err:
        raise InitError(f"cannot read the stack to pin from {path}: {err}") from err
    stack = render_dial._scalar(tree, ("current",))
    if stack is None:
        raise InitError(f"{path} has no `current:` pointer, so there is no stack to pin")
    return stack


def _kafka_block(a: Answers) -> str:
    if a.kafka_provider not in render_dial.MANAGED_KAFKA_PROVIDERS:
        return f"kafka:\n  provider: {a.kafka_provider}\n  controller_pool: {a.controller_pool}\n"

    if a.kafka_provider == "msk":
        return (
            "kafka:\n"
            f"  provider: msk\n"
            "  msk:\n"
            "    shape_ref: msk-broker\n"
            f"    broker_count: {a.msk_broker_count}\n"
            "    broker_version: 4.2.x.kraft\n"
            "    num_partitions: 12\n"
            "    log_retention_ms: 259200000\n"
            "    message_max_bytes: 16777216\n"
            "    scram_username: dfe-kafka-user\n"
            "    bootstrap_job:\n"
            "      namespace: strimzi\n"
            "      service_account: dfe-kafka-bootstrap\n"
            "    autoscaling:\n"
            '      enabled: "true"\n'
            "      max_brokers: 6\n"
            '      step: ""\n'
            "      per_broker_capacity_mb_s: 50\n"
            "      headroom: 1.3\n"
        )

    # confluent-cloud / redpanda-cloud: tuning (still nested under kafka.msk --
    # render_dial.py reads it from there for every managed body) + landing_topics.
    topics = "    main_land: {}\n"
    if a.kafka_extra_topic:
        topics += f"    {a.kafka_extra_topic}: {{}}\n"
    return (
        "kafka:\n"
        f"  provider: {a.kafka_provider}\n"
        "  msk:\n"
        "    num_partitions: 12\n"
        "    log_retention_ms: 259200000\n"
        "    message_max_bytes: 16777216\n"
        "  landing_topics:\n" + topics
    )


def _overrides_block(overrides: dict[str, dict[str, str]]) -> str:
    if not overrides:
        return "  overrides: {}\n"
    lines = ["  overrides:\n"]
    for workload, fields in overrides.items():
        lines.append(f"    {workload}:\n")
        for name, value in fields.items():
            lines.append(f"      {name}: {value}\n")
    return "".join(lines)


def build_dial_text(a: Answers) -> str:
    """The deployment.yaml text this wizard writes -- one field per line this
    module's steps or template defaults set, in deployment.example.yaml's own
    order and quoting convention (`_flag()` reads quoted true/false; the edge:
    and toolbox.pod: blocks are unquoted, matching the chart values they are
    pasted into verbatim)."""
    is_aws = a.target == "aws"

    target_block = (
        "target:\n"
        "  existing:\n"
        f'    clusterRef: "{a.existing_cluster_ref}"\n'
        f"    namespace: {a.existing_namespace}\n"
    )
    if is_aws:
        target_block += (
            "  provision:\n"
            "    cloud: aws\n"
            f'    account: "{a.aws_account}"\n'
            f"    region: {a.aws_region}\n"
            f"    cidr: {a.aws_cidr}\n"
        )

    ui_public = a.ui_public
    # The wizard asks nothing about forgejo or the tunnel, so both take the
    # template's own defaults rather than a question nobody can answer yet.
    edge_block = (
        "edge:\n"
        "  enabled: true\n"
        f"  flavour: {'aws' if is_aws else 'onprem'}\n"
        "  product:\n"
        f"    public: {_bool(ui_public['dfe_ui'])}\n"
        f'    domain: "{a.public_zone}"\n'
        "    tls:\n"
        '      min_version: "1.2"\n'
        "      hsts: true\n"
        "    rate_limit:\n"
        "      enabled: true\n"
        '      requests: "300"\n'
        "      unit: Minute\n"
        "      scope: local\n"
        f'    allowed_cidrs: "{a.ui_allowed_cidrs}"\n'
        f'    trusted_proxy_cidrs: "{a.ui_trusted_proxy_cidrs}"\n'
        "    waf:\n"
        "      mode: none\n"
        '      plan: ""\n'
        '      managed_rules: ""\n'
        "  engine_api:\n"
        "    with_product: true\n"
        "    path_prefix: /api/v1\n"
        "    cli_families_public: false\n"
        "    scim_public: false\n"
        "  admin_uis:\n"
        "    external: false\n"
        "    public:\n"
        f"      kafbat: {_bool(ui_public['kafbat'])}\n"
        f"      cruise_control: {_bool(ui_public['cruise_control'])}\n"
        f"      hyperdx: {_bool(ui_public['hyperdx'])}\n"
        f"      argocd: {_bool(ui_public['argocd'])}\n"
        f"      links: {_bool(ui_public['links'])}\n"
        "      forgejo: false\n"
        "    oidc:\n"
        "      enabled: true\n"
        "      providers: []\n"
        "      admin_groups: [dfe-admins, dfe-infra]\n"
        "  ingest:\n"
        "    receiver:\n"
        f"      mode: {'vpn' if is_aws else 'internal'}\n"
        "      public:\n"
        "        serviceType: LoadBalancer\n"
        '        loadBalancerIP: ""\n'
        '        loadBalancerClass: ""\n'
        "        annotations: {}\n"
        "        loadBalancerSourceRanges: []\n"
        "      vpn:\n"
        "        podLabel: dfe-culvert\n"
        "      networkPolicy:\n"
        "        enabled: true\n"
        "    tunnel:\n"
        "      enabled: false\n"
        f"      serviceType: {'NodePort' if is_aws else 'LoadBalancer'}\n"
        f"      externalTrafficPolicy: {'Cluster' if is_aws else 'Local'}\n"
        f"      pki_mode: {'external' if is_aws else 'local'}\n"
        "      address:\n"
        "        mode: byo\n"
        "      admin_peer:\n"
        f"        enabled: {_bool(is_aws)}\n"
        "        ttl_minutes: 60\n"
        '        peer_cidr: ""\n'
        "        reach: [22, 443]\n"
        "    otel:\n"
        "      public: false\n"
        "      auth: required\n"
        "  aws:\n"
        "    load_balancer_controller: true\n"
        f'    public_zone: "{a.public_zone if is_aws else ""}"\n'
        "    cloudfront:\n"
        "      mode: none\n"
    )

    telemetry_line = f"    sink: {a.telemetry_sink}"
    if a.telemetry_sink == "cloudwatch" and a.telemetry_reason:
        telemetry_line += f"  # {a.telemetry_reason}"
    telemetry_block = "telemetry:\n  aws:\n" + telemetry_line + '\n    retention_days: ""\n'

    toolbox_block = (
        "toolbox:\n"
        "  pod:\n"
        f"    enabled: {_bool(a.toolbox_pod_enabled)}\n"
        "    kubeApiAccess: false\n"
        '    ttlSeconds: ""\n'
        f'  enabled: "{_bool(a.toolbox_aws_enabled)}"\n'
        "  aws:\n"
        "    instance_type: t4g.small\n"
        f'    operator_role_arn: "{a.toolbox_operator_role_arn}"\n'
        "  ttl_minutes: 60\n"
        "  session:\n"
        "    idle_timeout_minutes: 15\n"
        "    max_duration_minutes: 240\n"
        "  session_log_retention_days: 90\n"
    )

    sizing_block = (
        "sizing:\n"
        f'  ingest_gb_per_day: "{a.ingest_gb_per_day}"\n'
        f"  focus: {a.focus}\n"
        '  peak_factor: ""\n'
        '  avg_event_bytes: ""\n'
        '  compression_ratio: ""\n'
        '  hunt_window_days: ""\n'
        '  archiver_lag_hours: ""\n'
        '  spend_warn_usd_month: ""\n'
        '  allow_undersized: "false"\n'
        f"  storage_model: {a.clickhouse_storage_model}\n"
    )
    overrides = dict(a.overrides)
    if a.clickhouse_instance_override:
        overrides = dict(overrides)
        overrides["clickhouse"] = dict(overrides.get("clickhouse", {}))
        overrides["clickhouse"]["instance_type"] = a.clickhouse_instance_override
    sizing_block += _overrides_block(overrides)

    k8s_block = (
        "k8s:\n"
        "  env: local\n"
        f"  cloud: {'aws' if is_aws else 'local'}\n"
        f"  region: {a.aws_region if is_aws else 'local'}\n"
        '  domain: ""\n'
        f"  storage_class: {'gp3' if is_aws else 'local-path'}\n"
        '  repo_url: ""\n'
        "  target_revision: main\n"
        '  workload_identity_annotations: "{}"\n'
        f"  load_balancer_type: \"{'nlb' if is_aws else ''}\"\n"
        f"  config_storage: \"{'s3' if is_aws else ''}\"\n"
    )

    return "".join(
        [
            "## Written by `dfe-ops init` -- the schema is deployment.example.yaml; do not\n",
            "## hand-add a field it does not already declare.\n",
            "apiVersion: dfe.hyperi.io/v1\n",
            "kind: DeployContext\n",
            "substrate: k8s\n\n",
            "metadata:\n",
            f"  name: {a.name}\n",
            '  owner: ""\n',
            f"  lifecycle: {a.lifecycle}\n",
            "  ttl: 48h\n\n",
            target_block, "\n",
            'kubernetes_version: "1.36"\n\n',
            "network:\n",
            "  nat: single\n",
            f"  az_count: {a.az_count}\n\n",
            "endpoint:\n",
            '  public: "false"\n',
            '  allowed_cidrs: ""\n\n',
            "dns:\n",
            "  private_zone: dfe.internal\n",
            f'  public_zone: "{a.public_zone}"\n\n',
            edge_block, "\n",
            toolbox_block, "\n",
            "node_pools:\n",
            "  system:\n",
            "    shape_ref: eks-system\n",
            "    min_size: 2\n",
            "    max_size: 3\n",
            "    desired_size: 2\n",
            "    capacity_type: ON_DEMAND\n",
            "    disk_gb: 40\n\n",
            telemetry_block, "\n",
            sizing_block, "\n",
            _kafka_block(a), "\n",
            "state:\n",
            '  bucket: ""\n',
            "  key: dfe/test/aws.tfstate\n",
            '  region: ""\n\n',
            "tags:\n",
            "  service-name: dfe\n",
            "  service-namespace: dfe\n",
            "  environment: test\n",
            '  owner: ""\n',
            '  cost-center: ""\n',
            f"  lifecycle: {a.lifecycle}\n\n",
            "registry: ghcr.io/hyperi-io\n\n",
            "version:\n",
            f"  pin: {current_stack()}\n\n",
            f"profile: {a.profile}\n\n",
            "steps:\n",
            f'  provision: "{_bool(is_aws)}"\n',
            '  layer1: "true"\n',
            '  layer2: "true"\n',
            '  ddl: "true"\n',
            '  smoke: "true"\n\n',
            "secrets:\n",
            "  backend: openbao\n",
            '  ref: ""\n\n',
            'overlay: ""\n\n',
            "retention:\n",
            "  default_ttl_days: 90\n\n",
            k8s_block, "\n",
            "endpoints:\n",
            '  clickhouse_host: ""\n',
            '  kafka_bootstrap: ""\n',
            '  otel_endpoint: ""\n',
            '  vault_addr: ""\n',
        ]
    )


# ---------------------------------------------------------------------------
# The resolve_sizing.py boundary -- mocked in tests the same way
# dfe_ops_bastion.py's `_run` is.
# ---------------------------------------------------------------------------


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)  # type: ignore[call-overload]


def _total_compute_line(report_text: str) -> str | None:
    for line in report_text.splitlines():
        if "total compute" in line:
            return line.strip()
    return None


def run_resolve(dial_path: Path, *, fixtures: Path | None, live: bool, out_dir: Path) -> int:
    """Run `resolve_sizing.py resolve` against the written dial, then print its
    report path and total-compute line, which carries a cost bucket and no
    figure. scale tier only -- every other profile is skipped with a one-line
    explanation, never a crash."""
    tier = resolve_sizing.read_dial(dial_path).tier
    if tier != resolve_sizing.SCALE_TIER:
        print(
            f"dfe-ops init: resolve_sizing.py sizes the {resolve_sizing.SCALE_TIER} tier only "
            f"-- skipping the sizing resolve for profile {tier!r}",
            file=sys.stderr,
        )
        return 0

    cmd = [sys.executable, str(RESOLVE_SIZING), "resolve", "--dial", str(dial_path), "--out", str(out_dir)]
    if live:
        cmd.append("--live")
    elif fixtures:
        cmd += ["--fixtures", str(fixtures)]
    else:
        print("dfe-ops init: no --fixtures/--live -- skipping the sizing resolve", file=sys.stderr)
        return 0

    result = _run(cmd, capture_output=True, text=True)
    sys.stderr.write(result.stderr or "")

    # A non-zero exit can still mean the report was written -- resolve_sizing.py
    # writes every artefact before it checks findings for a fatal one, so the
    # report is the more reliable signal than the exit code alone.
    report_path = out_dir / "sizing" / f"{tier}.report.md"
    if not report_path.is_file():
        print(f"dfe-ops init: resolve_sizing.py exited {result.returncode}, no report written", file=sys.stderr)
        return result.returncode

    print(f"dfe-ops init: sizing report at {report_path}", file=sys.stderr)
    total = _total_compute_line(report_path.read_text(encoding="utf-8"))
    if total:
        print(f"dfe-ops init: {total}", file=sys.stderr)
    if result.returncode != 0:
        print(f"dfe-ops init: resolve_sizing.py refused (exit {result.returncode}) -- see the report above", file=sys.stderr)
    return result.returncode


# ---------------------------------------------------------------------------
# The next-commands footer
# ---------------------------------------------------------------------------


def _next_commands(dial_out: Path, a: Answers) -> str:
    rel = dial_out.relative_to(REPO_ROOT) if dial_out.is_relative_to(REPO_ROOT) else dial_out
    if a.target == "aws":
        return "\n".join(
            (
                f"  python3 scripts/render_dial.py --dial {rel} --tofu",
                "  tofu -chdir=terraform/environments/aws init",
                "  tofu -chdir=terraform/environments/aws plan -out=deployment.tfplan",
                "  tofu -chdir=terraform/environments/aws apply deployment.tfplan",
                "  eval \"$(tofu -chdir=terraform/environments/aws output -raw kubeconfig_command)\"",
                f"  python3 scripts/dfe-ops stack-deploy --stack <version> --mode {a.profile} \\",
                "      --from-terraform terraform/environments/aws --kubeconfig <path>",
            )
        )
    return "\n".join(
        (
            f"  python3 scripts/render_dial.py --dial {rel}",
            "  python3 scripts/dfe-ops stack-deploy --stack <version> --mode <mode> --kubeconfig <path>",
        )
    )


# Estate context deployment.example.yaml ships blank -- injected by the
# operator's own tooling or filled by hand, never invented here.
# render_dial.py --tofu refuses on k8s.repo_url until one of those happens.
_ESTATE_BLANKS_AWS = ("k8s.repo_url", "state.bucket", "state.region", "tags.owner", "tags.cost-center")


def _estate_blanks_note(a: Answers) -> str | None:
    if a.target != "aws":
        return None
    return (
        "Before tofu apply, fill these estate-specific fields by hand (left blank on "
        "purpose -- the wizard never invents estate context): " + ", ".join(_ESTATE_BLANKS_AWS)
    )


# ---------------------------------------------------------------------------
# The subcommand
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    answers_dict: dict[str, str] | None = None
    if args.answers:
        try:
            answers_dict = envfile.parse_env_file(Path(args.answers))
        except FileNotFoundError:
            print(f"dfe-ops init: no answers file at {args.answers}", file=sys.stderr)
            return 2

    io = WizardIO(answers=answers_dict)
    try:
        a = run_wizard(io)
        dial_text = build_dial_text(a)
    except InitError as error:
        print(f"dfe-ops init: {error}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(dial_text, end="")
        return 0

    out_path = Path(args.out)
    if out_path.exists() and not args.force:
        print(
            f"dfe-ops init: {out_path} already exists -- pass --force to overwrite",
            file=sys.stderr,
        )
        return 2

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(dial_text, encoding="utf-8")
    print(f"dfe-ops init: wrote {out_path}", file=sys.stderr)

    if args.fixtures or args.live:
        sizing_out = Path(args.sizing_out) if args.sizing_out else REPO_ROOT
        rc = run_resolve(
            out_path,
            fixtures=Path(args.fixtures) if args.fixtures else None,
            live=args.live,
            out_dir=sizing_out,
        )
        if rc != 0:
            return 1

    note = _estate_blanks_note(a)
    if note:
        print(file=sys.stderr)
        print(note, file=sys.stderr)

    print(file=sys.stderr)
    print("Next:", file=sys.stderr)
    print(_next_commands(out_path, a), file=sys.stderr)
    return 0


def add_init_subparser(sub: argparse._SubParsersAction) -> None:
    """Register `dfe-ops init` and its flags."""
    init = sub.add_parser(
        "init",
        help="prompt-driven wizard that writes a deployment.yaml dial",
        description="Walks the nine deployment.example.yaml decisions (target, tier/ingest/focus, "
                    "Kafka provider and quorum, ClickHouse storage, public UIs + OIDC, telemetry, "
                    "lifecycle + AZ count, toolbox, sizing overrides) and writes a dial "
                    "render_dial.py accepts. "
                    "docs/deployment/wizard.md is the field-by-field reference.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    init.add_argument("--out", default=str(DEFAULT_DIAL_OUT), help="where to write the dial")
    init.add_argument("--force", action="store_true", help="overwrite --out if it already exists")
    init.add_argument(
        "--answers", default=None, metavar="PATH",
        help="a flat key=value file driving the wizard non-interactively (CI/tests)",
    )
    init.add_argument("--dry-run", action="store_true", help="print the resulting dial, write nothing")
    init.add_argument(
        "--fixtures", default=None, metavar="DIR",
        help="resolve sizing against captured fixtures once the dial is written (scale tier only)",
    )
    init.add_argument("--live", action="store_true", help="resolve sizing against the live cloud API")
    init.add_argument(
        "--sizing-out", default=None, metavar="DIR",
        help="where resolve_sizing.py writes its artefacts (default: repo root, as a normal resolve does)",
    )
    init.set_defaults(func=cmd_init)
