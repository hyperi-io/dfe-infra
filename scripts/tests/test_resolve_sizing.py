#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_resolve_sizing.py
#  Purpose:      Guard the sizing resolver: the deployment matrix IS the test
#                matrix, so a ratio change shows every deployment it moves.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/resolve_sizing.py.

The matrix below is cloud x tier x focus x estimate band, snapshotted against
`fixtures/sizing/golden-matrix.json`. A change to any ratio in `sizing.yaml`, to
a shape in `compute-shapes.yaml`, or to a formula in the resolver MOVES those
snapshots -- which is the point: it shows every deployment the change touches
before anyone deploys one.

    python3 -m pytest scripts/tests/test_resolve_sizing.py -q

REGENERATING THE SNAPSHOTS IS A DELIBERATE ACT, never a way past a red test.
Read the failing diff first and satisfy yourself the new numbers are the ones you
meant to change, then:

    RESOLVE_SIZING_GOLDEN=write python3 -m pytest scripts/tests/test_resolve_sizing.py

and commit the golden file in the SAME commit as the ratio that moved it, so the
review sees the cause and the effect together.

The cloud answers come from `fixtures/sizing/aws-catalogue-us-west-2.json`,
captured once from the live EC2 and Pricing APIs in us-west-2 and scrubbed.
Re-capture with `python3 scripts/resolve_sizing.py capture --region us-west-2
--fixtures scripts/tests/fixtures/sizing`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sizing"
CATALOGUE = FIXTURES / "aws-catalogue-us-west-2.json"
GOLDEN = FIXTURES / "golden-matrix.json"

sys.path.insert(0, str(SCRIPTS))
import render_dial  # noqa: E402
import resolve_sizing  # noqa: E402
from yaml_subset import parse as parse_dial  # noqa: E402

WRITE_GOLDEN = os.environ.get("RESOLVE_SIZING_GOLDEN") == "write"

# cloud x tier x focus x estimate band. slim and single carry fixed shapes and
# must be REFUSED; 150,000 GB/day is above the generator cap and must be refused
# too, with the professional-services message rather than an extrapolation.
BANDS = (None, 1000, 10000, 100000)
FOCUSES = ("economy", "balanced", "performance")
CLOUDS = ("aws", "onprem")

# Read once, from sizing.yaml itself, rather than hardcoded here -- a test
# call site that does not care about the root volume's own demand still has
# to pass SOMETHING, and the real ratio is what select_shape and
# select_msk_shape actually thread through in production.
_SIZING = resolve_sizing._load(resolve_sizing.SIZING_FILE)
ROOT_VOLUME_DEMAND_MIB_S = resolve_sizing._ratio(
    _SIZING, "floors", "root_volume_demand_mib_s"
).number
ROOT_VOLUME_DEMAND_IOPS = int(
    resolve_sizing._ratio(_SIZING, "floors", "root_volume_demand_iops").number
)


def _dial(
    tmp_path: Path,
    *,
    tier: str = "scale",
    cloud: str = "aws",
    focus: str = "economy",
    estimate: int | None = 10000,
    provider: str = "strimzi",
    controller_pool: str | None = None,
    extra: str = "",
) -> Path:
    """Write one deployment dial and answer its path.

    ``controller_pool`` is left OUT of the kafka block unless a caller names
    one, so the matrix keeps proving what a dial that omits the field resolves
    to -- which is the case every committed deployment is in.
    """
    lines = [
        "apiVersion: dfe.hyperi.io/v1",
        "kind: DeployContext",
        "substrate: k8s",
        "metadata:",
        "  name: dfe-test",
        "target:",
        "  provision:",
        f"    cloud: {cloud}",
        "    region: us-west-2",
        f"profile: {tier}",
        "kafka:",
        f"  provider: {provider}",
    ]
    if controller_pool is not None:
        lines.append(f"  controller_pool: {controller_pool}")
    lines += [
        "sizing:",
        f"  focus: {focus}",
    ]
    if estimate is not None:
        lines.append(f"  ingest_gb_per_day: {estimate}")
    if extra:
        lines.extend(extra.splitlines())
    lines.append("  spend_warn_usd_month: 5000")
    lines += ["retention:", "  default_ttl_days: 90", "k8s:", "  env: test", f"  cloud: {cloud}"]
    path = tmp_path / "deployment.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run(
    dial: Path,
    out: Path,
    cloud: str | None = None,
    target: str | None = None,
    previous: Path | None = None,
    migrate: bool = False,
) -> int:
    argv = ["--dial", str(dial), "--fixtures", str(FIXTURES), "--out", str(out)]
    if cloud:
        argv += ["--cloud", cloud]
    if target:
        argv += ["--target", target]
    if previous is not None:
        argv += ["--previous", str(previous)]
    if migrate:
        argv += ["--migrate"]
    return resolve_sizing.main(argv)


def _summary(out: Path, status: int, tier: str = "scale") -> dict[str, object]:
    """The snapshot: the numbers a ratio change would move, and nothing else."""
    values = (out / "sizing" / f"{tier}.values.yaml").read_text(encoding="utf-8")
    report = (out / "sizing" / f"{tier}.report.md").read_text(encoding="utf-8")
    snapshot: dict[str, object] = {
        # 0 clean, 1 a fatal assertion. A4 is fatal only at focus performance, so
        # this is where the warn-versus-fatal split shows up in the diff.
        "status": status,
        "values": [line for line in values.splitlines() if line and not line.startswith("##")],
        "partitions": re.search(r"Partitions: \*\*(\d+)\*\*", report)[1],
        "assertions": sorted(
            f"{rule} {use_case}"
            for rule, use_case in re.findall(r"^\| (A\d)(?: \(warn\))? \| ([\w-]+) \|", report, re.M)
        ),
    }
    tfvars = out / "sizing.auto.tfvars.json"
    if tfvars.is_file():
        doc = json.loads(tfvars.read_text(encoding="utf-8"))
        snapshot["shapes"] = {
            name: body["instance_types"][0] for name, body in doc["resolved_shapes"].items()
        }
        snapshot["pool_sizes"] = {name: body["desired_size"] for name, body in doc["node_pools"].items()}
    return snapshot


# One case carries deployer overrides, so a change to how they are applied moves
# a snapshot like any other change.
OVERRIDE_CASE = "aws-scale-economy-10000-overridden"
OVERRIDE_BLOCK = """  overrides:
    kafka-broker:
      cpu: 32
      replicas: 6
      instance_type: m9g.8xlarge
      throughput_mibs: 700
    clickhouse:
      memory: 128"""


def _cases() -> list[tuple[str, str, int | None]]:
    return [(cloud, focus, band) for cloud in CLOUDS for focus in FOCUSES for band in BANDS]


def _case_name(cloud: str, focus: str, band: int | None) -> str:
    return f"{cloud}-scale-{focus}-{band if band is not None else 'no-estimate'}"


# A kafka.provider AXIS, alongside the cloud x focus x band cross-product --
# not multiplied through it, same reasoning as MSK_CI_BURST_CASE further down:
# provider is orthogonal, and a full cross-product would be disproportionate
# to what these need to prove. msk already gets its own dedicated fixture
# (msk_case) with its own tests; these are the two tokens
# gate-3-correctness.md P2-1 found the resolver refusing outright --
# confluent-cloud and redpanda-cloud never reached read_dial at all before.
PROVIDER_AXIS_CASES = {
    "aws-scale-economy-10000-confluent-cloud": "confluent-cloud",
    "aws-scale-economy-10000-redpanda-cloud": "redpanda-cloud",
}


@pytest.fixture(scope="module")
def matrix(tmp_path_factory) -> dict[str, object]:
    """Resolve every case in the matrix once, and answer the snapshots."""
    built: dict[str, object] = {}
    for cloud, focus, band in _cases():
        out = tmp_path_factory.mktemp(_case_name(cloud, focus, band))
        dial = _dial(out, cloud=cloud, focus=focus, estimate=band)
        status = _run(dial, out, cloud=cloud)
        built[_case_name(cloud, focus, band)] = _summary(out, status)
    out = tmp_path_factory.mktemp("overridden")
    status = _run(_dial(out, estimate=10000, extra=OVERRIDE_BLOCK), out, cloud="aws")
    built[OVERRIDE_CASE] = _summary(out, status)
    for name, provider in PROVIDER_AXIS_CASES.items():
        out = tmp_path_factory.mktemp(name)
        status = _run(
            _dial(out, cloud="aws", focus="economy", estimate=10000, provider=provider), out, cloud="aws"
        )
        built[name] = _summary(out, status)
    return built


def test_the_matrix_matches_its_snapshots(matrix: dict[str, object]) -> None:
    """The deployment matrix IS the test matrix -- see the module docstring."""
    if WRITE_GOLDEN:
        GOLDEN.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip("golden matrix rewritten -- review the diff before committing it")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert matrix == expected


def test_every_case_in_the_matrix_produced_a_snapshot(matrix: dict[str, object]) -> None:
    assert sorted(matrix) == sorted(
        [*(_case_name(*case) for case in _cases()), OVERRIDE_CASE, *PROVIDER_AXIS_CASES]
    )


@pytest.mark.parametrize("tier", ["slim", "single"])
def test_the_fixed_tiers_are_refused(tmp_path: Path, tier: str, capsys) -> None:
    """slim and single are one node of everything; nothing here applies to them."""
    dial = _dial(tmp_path, tier=tier)
    assert _run(dial, tmp_path) == 1
    assert "scale tier only" in capsys.readouterr().err


def test_above_the_cap_the_resolver_refuses_rather_than_extrapolating(tmp_path: Path, capsys) -> None:
    dial = _dial(tmp_path, estimate=150000)
    assert _run(dial, tmp_path) == resolve_sizing.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "professional-services" in err
    assert "100,000 GB/day" in err


def test_the_refusal_names_the_provider_whose_ceiling_binds(tmp_path: Path, capsys) -> None:
    """MSK Express caps at 95,040 GB/day, below the generator, so its name prints."""
    dial = _dial(tmp_path, estimate=96000, provider="msk")
    assert _run(dial, tmp_path) == resolve_sizing.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "msk-express" in err
    assert "95,040 GB/day" in err


def test_no_estimate_reports_the_carried_throughput_rather_than_targeting_it(tmp_path: Path) -> None:
    dial = _dial(tmp_path, estimate=None)
    _run(dial, tmp_path)
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    assert "No estimate" in report
    assert "REPORTED, not targeted" in report


def test_the_floor_holds_the_broker_count_at_three(tmp_path: Path) -> None:
    dial = _dial(tmp_path, estimate=None)
    _run(dial, tmp_path)
    doc = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
    assert doc["node_pools"]["kafka-broker"]["desired_size"] == 3


def test_the_broker_count_grows_with_the_estimate(tmp_path: Path) -> None:
    """Cookie-cutter scale-out: past the per-broker vCPU cap the cluster grows by broker."""
    counts = []
    for band in (1000, 100000):
        out = tmp_path / str(band)
        out.mkdir()
        _run(_dial(out, estimate=band), out)
        doc = json.loads((out / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
        counts.append(doc["node_pools"]["kafka-broker"]["desired_size"])
    assert counts[1] > counts[0]
    assert all(count % 3 == 0 for count in counts)


def test_partitions_divide_evenly_by_the_broker_count(tmp_path: Path) -> None:
    """3, 6 and 12 brokers must all divide the count, or a scale-out skews leadership."""
    for band in (None, 1000, 10000, 100000):
        out = tmp_path / str(band)
        out.mkdir()
        _run(_dial(out, estimate=band), out)
        report = (out / "sizing" / "scale.report.md").read_text(encoding="utf-8")
        partitions = int(re.search(r"Partitions: \*\*(\d+)\*\*", report)[1])
        doc = json.loads((out / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
        brokers = doc["node_pools"]["kafka-broker"]["desired_size"]
        assert partitions % brokers == 0, (band, partitions, brokers)


def test_economy_never_trades_the_durability_floor(tmp_path: Path) -> None:
    """The cheapest deployment still runs three brokers, three replicas, three
    controllers. The dial asks for a separate controller pool because that is
    the only mode with a controller pool to count -- a combined quorum runs on
    those same three brokers."""
    _run(_dial(tmp_path, focus="economy", estimate=None, controller_pool="separate"), tmp_path)
    doc = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
    for pool in ("kafka-broker", "kraft-controller", "clickhouse", "keeper"):
        assert doc["node_pools"][pool]["desired_size"] >= 3


def test_headroom_rises_with_focus(tmp_path: Path) -> None:
    required: list[float] = []
    for focus in FOCUSES:
        out = tmp_path / focus
        out.mkdir()
        _run(_dial(out, focus=focus), out)
        report = (out / "sizing" / "scale.report.md").read_text(encoding="utf-8")
        required.append(float(re.search(r"built for ([\d,.]+) MB/s", report)[1].replace(",", "")))
    assert required == sorted(required)


# ---------------------------------------------------------------------------
# The silent-cap assertions
# ---------------------------------------------------------------------------


def _choice(**overrides) -> resolve_sizing.Choice:
    base = {
        "use_case": "kafka-broker",
        "instance_type": "m9g.8xlarge",
        "fallbacks": [],
        "vcpu": 32,
        "memory_gib": 128.0,
        "generation": 9,
        "generation_policy": "newest",
        "price_policy": "newest",
        "price_usd_hour": 1.5,
        "physical_processor": "AWS Graviton5 Processor",
        "instance_store_gb": 0,
        "baseline_iops": 60000,
        "baseline_throughput_mib_s": 2500.0,
        "maximum_iops": 60000,
        "maximum_throughput_mib_s": 2500.0,
        "volumes": {},
        "count": 3,
    }
    base.update(overrides)
    return resolve_sizing.Choice(**base)


@pytest.fixture(scope="module")
def catalogue() -> resolve_sizing.Catalogue:
    return resolve_sizing.fetch_fixtures(FIXTURES, "us-west-2")


def test_fetch_fixtures_refuses_an_uncaptured_region_by_name() -> None:
    with pytest.raises(resolve_sizing.ResolveError, match="capture --region ap-southeast-2"):
        resolve_sizing.fetch_fixtures(FIXTURES, "ap-southeast-2")


def test_a_resolve_writes_the_resolved_shapes_file_keyed_by_region(tmp_path: Path) -> None:
    """One region's Graviton availability is never committed under another's name."""
    _run(_dial(tmp_path), tmp_path)
    resolved = tmp_path / "shapes" / "resolved"
    assert (resolved / "aws-us-west-2.json").is_file()
    assert not (resolved / "aws.json").is_file()


def test_a1_catches_iops_above_what_the_volume_size_allows(catalogue) -> None:
    """gp3 gives 500 IOPS a GiB; asking past it is rejected at CreateVolume."""
    choice = _choice(
        volumes={"data": {"type": "gp3", "size_gib": 20, "iops": 16000, "throughput_mib_s": 125}}
    )
    rules = {f.rule for f in resolve_sizing.assert_caps(choice, {}, catalogue, 0, gp3_usd_per_gib_month=0.08)}
    assert "A1" in rules


def test_a2_catches_throughput_the_provisioned_iops_cannot_carry(catalogue) -> None:
    """gp3 gives 0.25 MiB/s an IOPS, so 1,000 MiB/s needs 4,000 provisioned IOPS."""
    choice = _choice(
        volumes={"data": {"type": "gp3", "size_gib": 4000, "iops": 3000, "throughput_mib_s": 1000}}
    )
    rules = {f.rule for f in resolve_sizing.assert_caps(choice, {}, catalogue, 0, gp3_usd_per_gib_month=0.08)}
    assert "A2" in rules


def test_a3_catches_the_workloads_demand_summed_past_the_instance_baseline(catalogue) -> None:
    """The attached instance caps every volume behind it, and says nothing.

    A3 sums `demand_iops` / `demand_throughput_mib_s`, never the plain
    `iops` / `throughput_mib_s` a volume is merely provisioned for -- see
    test_a3_ignores_a_provisioned_ceiling_the_workload_never_demands below
    for the regression this used to be (gate-3 remedy 2): a resolve used to
    fail this exact check from two idle volumes at their own free minimum,
    with nothing actually driving either of them.
    """
    choice = _choice(
        baseline_iops=12000,
        maximum_iops=12000,
        volumes={
            "root": {
                "type": "gp3",
                "size_gib": 40,
                "iops": 3000,
                "throughput_mib_s": 125,
                "demand_iops": 3000,
                "demand_throughput_mib_s": 125,
            },
            "data": {
                "type": "gp3",
                "size_gib": 2000,
                "iops": 20000,
                "throughput_mib_s": 500,
                "demand_iops": 20000,
                "demand_throughput_mib_s": 500,
            },
        },
    )
    findings = resolve_sizing.assert_caps(choice, {}, catalogue, 0, gp3_usd_per_gib_month=0.08)
    assert any(f.rule == "A3" for f in findings)
    assert any("the instance is the ceiling" in f.message for f in findings)


def test_a3_ignores_a_provisioned_ceiling_the_workload_never_demands(catalogue) -> None:
    """Two idle gp3 volumes at gp3's own free minimum must never force a
    bigger instance on their own -- the exact bug gate-3 remedy 2 fixed. A3
    used to sum `iops` / `throughput_mib_s` (what a volume is PROVISIONED
    for), so a root and a data volume each sitting at gp3's unavoidable
    3,000 IOPS / 125 MiB/s summed to 6,000 / 250 and failed against any
    instance below m9g.2xlarge, even at zero real ingest. Root's demand is
    its own mandatory floor (nothing ever asks less of it than that), but
    the data volume's provisioned 500 MiB/s ceiling here is money the
    deployer has not asked to spend, so it must not count.
    """
    choice = _choice(
        baseline_iops=6000,
        baseline_throughput_mib_s=187.5,
        volumes={
            "root": {
                "type": "gp3",
                "size_gib": 40,
                "iops": 3000,
                "throughput_mib_s": 125,
                "demand_iops": 3000,
                "demand_throughput_mib_s": 125,
            },
            "data": {
                "type": "gp3",
                "size_gib": 20,
                "iops": 3000,
                "throughput_mib_s": 500,
                "demand_iops": 0,
                "demand_throughput_mib_s": 0,
            },
        },
    )
    findings = [
        f
        for f in resolve_sizing.assert_caps(choice, {}, catalogue, 0, gp3_usd_per_gib_month=0.08)
        if f.rule == "A3"
    ]
    assert not findings, findings


@pytest.mark.parametrize(
    ("focus", "fatal"), [("economy", False), ("balanced", False), ("performance", True)]
)
def test_a4_is_fatal_only_where_the_focus_promises_the_sustained_maximum(
    catalogue, focus: str, fatal: bool
) -> None:
    """Below performance, A3 already holds the profile to the instance baseline."""
    choice = _choice(
        instance_type="m9g.4xlarge",
        baseline_iops=24000,
        baseline_throughput_mib_s=750.0,
        maximum_iops=48000,
        maximum_throughput_mib_s=1500.0,
        volumes={"root": {"type": "gp3", "size_gib": 40, "iops": 3000, "throughput_mib_s": 125}},
    )
    findings = [
        f
        for f in resolve_sizing.assert_caps(choice, {}, catalogue, 0, focus, gp3_usd_per_gib_month=0.08)
        if f.rule == "A4"
    ]
    assert findings
    assert "m9g.8xlarge" in findings[0].message
    assert findings[0].fatal is fatal


def test_a4_leaves_a_burstable_size_alone_when_the_workload_is_not_sustained(catalogue) -> None:
    choice = _choice(
        use_case="ci-burst",
        instance_type="c9g.4xlarge",
        baseline_iops=24000,
        maximum_iops=48000,
        volumes={"root": {"type": "gp3", "size_gib": 40, "iops": 3000, "throughput_mib_s": 125}},
    )
    assert not [f for f in resolve_sizing.assert_caps(choice, {}, catalogue, 0, gp3_usd_per_gib_month=0.08) if f.rule == "A4"]


def test_a4_honours_a_sustained_field_on_the_shape_entry(catalogue) -> None:
    """When compute-shapes.yaml declares `sustained`, the entry's answer wins."""
    choice = _choice(
        use_case="ci-burst",
        instance_type="c9g.4xlarge",
        baseline_iops=24000,
        maximum_iops=48000,
        volumes={"root": {"type": "gp3", "size_gib": 40, "iops": 3000, "throughput_mib_s": 125}},
    )
    findings = resolve_sizing.assert_caps(
        choice, {"sustained": "true"}, catalogue, 0, gp3_usd_per_gib_month=0.08
    )
    assert any(f.rule == "A4" for f in findings)


def test_a5_warns_on_spend_without_failing_the_resolve(catalogue) -> None:
    choice = _choice(
        volumes={"data": {"type": "gp3", "size_gib": 100000, "iops": 3000, "throughput_mib_s": 500}}
    )
    findings = [
        f
        for f in resolve_sizing.assert_caps(choice, {}, catalogue, 1000, gp3_usd_per_gib_month=0.08)
        if f.rule == "A5"
    ]
    assert findings
    assert not findings[0].fatal


def test_a5_prices_storage_at_the_regions_own_rate_not_a_flat_08(catalogue) -> None:
    """A different region's gp3 price must move the same warning threshold.

    The finding's text states no amount any more, so the proof is which side of
    one threshold each region lands on rather than two messages differing.
    """
    choice = _choice(
        volumes={"data": {"type": "gp3", "size_gib": 100000, "iops": 3000, "throughput_mib_s": 500}}
    )
    cheap = resolve_sizing.assert_caps(choice, {}, catalogue, 10000, gp3_usd_per_gib_month=0.01)
    dear = resolve_sizing.assert_caps(choice, {}, catalogue, 10000, gp3_usd_per_gib_month=1.00)
    assert not [f for f in cheap if f.rule == "A5"]
    assert [f for f in dear if f.rule == "A5"]


def test_a5_states_no_amount_at_all(catalogue) -> None:
    """The report carries no cost figure, the deployer's own threshold included."""
    choice = _choice(
        volumes={"data": {"type": "gp3", "size_gib": 100000, "iops": 3000, "throughput_mib_s": 500}}
    )
    finding = next(
        f
        for f in resolve_sizing.assert_caps(choice, {}, catalogue, 1000, gp3_usd_per_gib_month=0.08)
        if f.rule == "A5"
    )
    assert "$" not in finding.message
    assert "1,000" not in finding.message


def test_gp3_price_per_gib_month_reads_the_regions_own_value() -> None:
    shapes = resolve_sizing._load(resolve_sizing.SHAPES_FILE)
    assert resolve_sizing._gp3_price_per_gib_month(shapes, "aws", "us-west-2") == 0.08


def test_a_regions_missing_storage_price_is_refused_by_name() -> None:
    """The estimator refuses to borrow another region's gp3 rate silently."""
    shapes = resolve_sizing._load(resolve_sizing.SHAPES_FILE)
    with pytest.raises(resolve_sizing.ResolveError, match="ap-southeast-2"):
        resolve_sizing._gp3_price_per_gib_month(shapes, "aws", "ap-southeast-2")


def test_a7_catches_a_demand_above_what_the_volume_is_provisioned_for(catalogue) -> None:
    """A demand above the provisioned ceiling is refused BY NAME, never
    silently raised the way an earlier pass here used to (gate-3 remedy 2):
    it names the exact override the deployer has to raise."""
    choice = _choice(
        volumes={
            "data": {
                "type": "gp3",
                "size_gib": 2000,
                "iops": 3000,
                "throughput_mib_s": 500,
                "demand_iops": 0,
                "demand_throughput_mib_s": 900,
            }
        }
    )
    findings = [
        f
        for f in resolve_sizing.assert_caps(choice, {}, catalogue, 0, gp3_usd_per_gib_month=0.08)
        if f.rule == "A7"
    ]
    assert findings
    assert findings[0].fatal
    assert "sizing.overrides.kafka-broker.throughput_mibs" in findings[0].message


def test_an_override_above_demand_lifts_the_demand(catalogue) -> None:
    """The deployer asked for it: raising a volume's provisioned ceiling
    above what the workload was going to demand anyway raises the DEMAND to
    match, so A3's instance-baseline check sees what was actually asked
    for, and A7 does not turn straight around and refuse the same number."""
    choice = _choice(
        volumes={
            "data": {
                "type": "gp3",
                "size_gib": 2000,
                "iops": 3000,
                "throughput_mib_s": 500,
                "demand_iops": 0,
                "demand_throughput_mib_s": 200,
            }
        }
    )
    core = resolve_sizing.Core(
        tier="scale",
        focus="economy",
        headroom=0.0,
        estimated=True,
        ingest_gb_per_day=0.0,
        avg_mb_s=0.0,
        peak_mb_s=0.0,
        peak_factor=1.0,
        required_mb_s=0.0,
        carried_mb_s=0.0,
    )
    resolve_sizing._apply_shape_overrides(
        choice, {"kafka-broker": {"throughput_mibs": "900"}}, core, catalogue
    )
    assert choice.volumes["data"]["throughput_mib_s"] == 900
    assert choice.volumes["data"]["demand_throughput_mib_s"] == 900
    assert not [
        f
        for f in resolve_sizing.assert_caps(choice, {}, catalogue, 0, gp3_usd_per_gib_month=0.08)
        if f.rule == "A7"
    ]


def test_a6_catches_a_storage_class_that_leaves_throughput_unset(catalogue) -> None:
    """Unset, the EBS CSI driver takes 125 MiB/s whatever the volume size."""
    choice = _choice(
        volumes={"data": {"type": "gp3", "size_gib": 500, "iops": 3000, "throughput_mib_s": 0}}
    )
    rules = {f.rule for f in resolve_sizing.assert_caps(choice, {}, catalogue, 0, gp3_usd_per_gib_month=0.08)}
    assert "A6" in rules


def test_the_emitted_storage_classes_never_use_iops_per_gb(tmp_path: Path) -> None:
    """iopsPerGB is silently clamped and allowAutoIOPSPerGBIncrease is silently dearer."""
    _run(_dial(tmp_path), tmp_path)
    body = (tmp_path / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8")
    assert "iopsPerGB" not in body
    assert "allowAutoIOPSPerGBIncrease" not in body
    doc = json.loads(body)
    for use_case, entry in doc.items():
        if use_case.startswith("_"):
            continue
        for name, storage_class in entry["storage_classes"].items():
            assert storage_class["throughput"], (use_case, name)
            assert "iops" in storage_class


# ---------------------------------------------------------------------------
# Selection, prices and the fail-safe
# ---------------------------------------------------------------------------


def test_a_candidate_with_no_price_fails_the_selection(catalogue, tmp_path: Path) -> None:
    """An unpriced candidate must fail loudly rather than win by default."""
    stripped = resolve_sizing.Catalogue(
        region=catalogue.region,
        azs=catalogue.azs,
        types={
            name: resolve_sizing.InstanceType(**{**vars_of(t), "price_usd_hour": None})
            for name, t in catalogue.types.items()
        },
        offerings=catalogue.offerings,
        msk_families=catalogue.msk_families,
        msk_prices=catalogue.msk_prices,
        unparsed=0,
        seen=catalogue.seen,
        source="test",
        captured=catalogue.captured,
    )
    entry = {
        "family": "m",
        "modifiers": "",
        "generation_policy": "newest",
        "generation_pin": "",
        "price_policy": "newest",
        "price_step_max_pct": "",
        "price_generations": "",
        "size": "large",
        "volumes": {},
    }
    demand = resolve_sizing.Node("general", 1, 2, 4, 0, 0, 0, "test")
    with pytest.raises(resolve_sizing.ResolveError, match="no on-demand price"):
        resolve_sizing.select_shape(
            "general",
            entry,
            demand,
            stripped,
            "newest",
            [],
            ROOT_VOLUME_DEMAND_MIB_S,
            ROOT_VOLUME_DEMAND_IOPS,
        )


def vars_of(instance: resolve_sizing.InstanceType) -> dict[str, object]:
    """Field values of an InstanceType, which carries slots and so no __dict__."""
    return {field: getattr(instance, field) for field in instance.__slots__}


def test_too_many_unreadable_type_names_fail_the_selection_loudly(catalogue) -> None:
    """A naming change must not downgrade a pool by silently skipping candidates."""
    broken = resolve_sizing.Catalogue(
        region=catalogue.region,
        azs=catalogue.azs,
        types=catalogue.types,
        offerings=catalogue.offerings,
        msk_families=(),
        msk_prices={},
        unparsed=100,
        seen=400,
        source="test",
        captured=catalogue.captured,
    )
    demand = resolve_sizing.Node("general", 1, 2, 4, 0, 0, 0, "test")
    with pytest.raises(resolve_sizing.ResolveError, match="could not be parsed"):
        resolve_sizing.select_shape(
            "general",
            {"family": "m", "size": "large"},
            demand,
            broken,
            "newest",
            [],
            ROOT_VOLUME_DEMAND_MIB_S,
            ROOT_VOLUME_DEMAND_IOPS,
        )


def test_the_chosen_type_is_offered_in_every_availability_zone(tmp_path: Path, catalogue) -> None:
    """AZs diverge inside one region, and a type missing from one cannot place."""
    _run(_dial(tmp_path), tmp_path)
    doc = json.loads((tmp_path / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8"))
    for use_case, entry in doc.items():
        if use_case.startswith("_"):
            continue
        for name in entry["instance_types"]:
            assert catalogue.offered_everywhere(name), (use_case, name)


def test_the_resolved_answer_records_the_generation_the_pricing_api_names(tmp_path: Path) -> None:
    """EC2's ProcessorInfo carries no generation; physicalProcessor is the only source."""
    _run(_dial(tmp_path), tmp_path)
    doc = json.loads((tmp_path / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8"))
    assert "Graviton" in doc["kafka-broker"]["physical_processor"]
    # The Pricing API supplies the rate as well as the generation, and only the
    # generation is written down.
    assert "price_usd_hour" not in doc["kafka-broker"]


def test_the_clickhouse_shape_keeps_its_local_nvme(tmp_path: Path) -> None:
    """The cache wants local NVMe, so the generation policy may not drop the d modifier."""
    _run(_dial(tmp_path), tmp_path)
    doc = json.loads((tmp_path / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8"))
    assert "d" in doc["clickhouse"]["instance_types"][0].split(".")[0]
    assert doc["clickhouse"]["instance_store_gb"] > 0


def _clickhouse_core() -> resolve_sizing.Core:
    """The minimal Core build_values needs to reach its clickhouse branch."""
    core = resolve_sizing.Core(
        tier="scale",
        focus="economy",
        headroom=1.0,
        estimated=True,
        ingest_gb_per_day=0.0,
        avg_mb_s=0.0,
        peak_mb_s=0.0,
        peak_factor=1.0,
        required_mb_s=0.0,
        carried_mb_s=0.0,
    )
    core.nodes["clickhouse"] = resolve_sizing.Node("clickhouse", 3, 8, 32, 200, 0, 0, "test")
    return core


def test_the_cache_volume_follows_the_shapes_instance_store() -> None:
    """An r9gd-class choice puts the cache on its own local NVMe, at the size
    the shape resolved; a choice with no instance store (no populated cloud
    does this for clickhouse today, but a future one might) falls back to the
    chart's own pvc default rather than presenting a large gp3 cache as one."""
    core = _clickhouse_core()

    r9gd = _choice(
        use_case="clickhouse",
        instance_type="r9gd.2xlarge",
        instance_store_gb=474,
        volumes={"cache": {"type": "nvme-instance-store", "size_gib": 474, "source": "instance-provided"}},
    )
    values = resolve_sizing.build_values(core, "strimzi", {"clickhouse": r9gd}, None)
    assert values["clickhouse"]["objectStore"] == {
        "cache": {"volume": "instance-store"},
        "cacheSize": "474Gi",
    }

    ebs_only = _choice(use_case="clickhouse", instance_type="r9g.2xlarge", instance_store_gb=0, volumes={})
    values = resolve_sizing.build_values(core, "strimzi", {"clickhouse": ebs_only}, None)
    assert values["clickhouse"]["objectStore"] == {"cache": {"volume": "pvc"}}


# ---------------------------------------------------------------------------
# The storage_model dial. auto is the default and omits the chart key
# entirely; cached-object and local are explicit overrides, and only they
# reach the values fragment.
# ---------------------------------------------------------------------------


def test_storage_model_auto_omits_the_key_regardless_of_the_cache_choice() -> None:
    core = _clickhouse_core()
    no_cache = _choice(use_case="clickhouse", instance_type="r9g.2xlarge", instance_store_gb=0, volumes={})
    values = resolve_sizing.build_values(core, "strimzi", {"clickhouse": no_cache}, None, "auto")
    assert "storageModel" not in values["clickhouse"]

    r9gd = _choice(
        use_case="clickhouse",
        instance_type="r9gd.2xlarge",
        instance_store_gb=474,
        volumes={"cache": {"type": "nvme-instance-store", "size_gib": 474, "source": "instance-provided"}},
    )
    values = resolve_sizing.build_values(core, "strimzi", {"clickhouse": r9gd}, None, "auto")
    assert "storageModel" not in values["clickhouse"]
    assert values["clickhouse"]["objectStore"]["cache"]["volume"] == "instance-store"


def test_storage_model_explicit_cached_object_is_written_through() -> None:
    core = _clickhouse_core()
    no_cache = _choice(use_case="clickhouse", instance_type="r9g.2xlarge", instance_store_gb=0, volumes={})
    values = resolve_sizing.build_values(core, "strimzi", {"clickhouse": no_cache}, None, "cached-object")
    assert values["clickhouse"]["storageModel"] == "cached-object"


def test_storage_model_explicit_local_overrides_an_nvme_cache_choice() -> None:
    """The explicit opt-out wins even on a shape that would otherwise get an
    instance-store cache -- forcing the cache onto the pvc too, so the two
    never disagree (instance-store needs storageBulk object, _storage.tpl)."""
    core = _clickhouse_core()
    r9gd = _choice(
        use_case="clickhouse",
        instance_type="r9gd.2xlarge",
        instance_store_gb=474,
        volumes={"cache": {"type": "nvme-instance-store", "size_gib": 474, "source": "instance-provided"}},
    )
    values = resolve_sizing.build_values(core, "strimzi", {"clickhouse": r9gd}, None, "local")
    assert values["clickhouse"]["storageModel"] == "local"
    assert values["clickhouse"]["objectStore"] == {"cache": {"volume": "pvc"}}


def test_dial_storage_model_defaults_to_auto(tmp_path: Path) -> None:
    dial = resolve_sizing.read_dial(_dial(tmp_path))
    assert dial.storage_model == "auto"


@pytest.mark.parametrize("value", ["cached-object", "local", "AUTO"])
def test_dial_storage_model_reads_an_explicit_value(tmp_path: Path, value: str) -> None:
    dial = resolve_sizing.read_dial(_dial(tmp_path, extra=f"  storage_model: {value}"))
    assert dial.storage_model == value.lower()


def test_dial_storage_model_refuses_an_unknown_value(tmp_path: Path, capsys) -> None:
    dial = _dial(tmp_path, extra="  storage_model: glacier")
    assert _run(dial, tmp_path) == 1
    err = capsys.readouterr().err
    assert "sizing.storage_model must be auto, cached-object or local" in err
    assert "'glacier'" in err


# ---------------------------------------------------------------------------
# The controller_pool dial. combined is the default and writes no chart key at
# all, so the kafka chart's own default decides and syncing a fragment onto a
# live cluster cannot move its metadata quorum; separate is the explicit opt-in
# that sizes a controller pool and turns it on.
# ---------------------------------------------------------------------------


def _controller_core() -> resolve_sizing.Core:
    """The minimal Core build_values needs to reach its controller branch.

    The broker node is here because a controller is only ever sized beside
    one -- without it the whole kafka fragment is absent and the controller
    branch proves nothing.
    """
    core = resolve_sizing.Core(
        tier="scale",
        focus="economy",
        headroom=1.0,
        estimated=True,
        ingest_gb_per_day=0.0,
        avg_mb_s=0.0,
        peak_mb_s=0.0,
        peak_factor=1.0,
        required_mb_s=0.0,
        carried_mb_s=0.0,
        partitions=12,
    )
    core.nodes["kafka-broker"] = resolve_sizing.Node("kafka-broker", 3, 2, 8, 100, 0, 0, "test")
    core.nodes["kraft-controller"] = resolve_sizing.Node("kraft-controller", 3, 1, 4, 64, 0, 0, "test")
    return core


def test_controller_pool_combined_writes_no_chart_key_at_all() -> None:
    core = _controller_core()
    values = resolve_sizing.build_values(core, "strimzi", None, None, "auto", "combined")
    assert "controllerPool" not in values["kafka"]


def test_controller_pool_separate_enables_the_pool_and_sizes_it() -> None:
    core = _controller_core()
    values = resolve_sizing.build_values(core, "strimzi", None, None, "auto", "separate")
    pool = values["kafka"]["controllerPool"]
    assert pool["enabled"] is True
    assert pool["replicas"] == 3
    assert pool["storage"] == {"size": "64Gi"}
    assert pool["resources"]["requests"] == {"cpu": "1", "memory": "4Gi"}


def test_the_charts_own_default_is_what_combined_falls_back_to() -> None:
    """Omitting the key only means combined while the chart's default says so,
    and the chart is a different file in a different language."""
    chart = REPO_ROOT / "helm" / "charts" / "kafka" / "values.yaml"
    body = chart.read_text(encoding="utf-8")
    block = body[body.index("  controllerPool:") :]
    enabled = re.search(r"^\s+enabled:\s*(\S+)", block, re.MULTILINE)
    assert enabled is not None, "the kafka chart declares no controllerPool.enabled"
    assert enabled.group(1) == "false"


def test_dial_controller_pool_defaults_to_combined(tmp_path: Path) -> None:
    dial = resolve_sizing.read_dial(_dial(tmp_path))
    assert dial.controller_pool == "combined"


@pytest.mark.parametrize("value", ["combined", "separate", "SEPARATE"])
def test_dial_controller_pool_reads_an_explicit_value(tmp_path: Path, value: str) -> None:
    dial = resolve_sizing.read_dial(_dial(tmp_path, controller_pool=value))
    assert dial.controller_pool == value.lower()


def test_dial_controller_pool_refuses_an_unknown_value(tmp_path: Path, capsys) -> None:
    dial = _dial(tmp_path, controller_pool="dedicated")
    assert _run(dial, tmp_path) == 1
    err = capsys.readouterr().err
    assert "kafka.controller_pool must be one of combined, separate" in err
    assert "'dedicated'" in err


def test_combined_sizes_no_controller_node_pool_or_shape(tmp_path: Path) -> None:
    """A combined quorum runs on the brokers, so a controller node group, a
    Karpenter pool and a resolved shape would all be capacity no pod can land
    on -- and a report row for a node this deployment does not have."""
    assert _run(_dial(tmp_path, estimate=1000), tmp_path) == 0
    doc = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
    assert "kraft-controller" not in doc["node_pools"]
    assert "kraft-controller" not in doc["resolved_shapes"]
    pools = json.loads((tmp_path / "sizing" / "scale.karpenter.json").read_text(encoding="utf-8"))
    assert "kraft-controller" not in pools
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    assert "| kraft-controller |" not in report
    assert "the quorum runs on the brokers themselves" in report


def test_separate_sizes_the_controller_node_pool_and_shape(tmp_path: Path) -> None:
    """The opt-in is what makes the controller a real node class, so every
    artefact the combined dial leaves out is back."""
    assert _run(_dial(tmp_path, estimate=1000, controller_pool="separate"), tmp_path) == 0
    doc = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
    assert doc["node_pools"]["kraft-controller"]["desired_size"] == 3
    assert doc["resolved_shapes"]["kraft-controller"]["instance_types"]
    pools = json.loads((tmp_path / "sizing" / "scale.karpenter.json").read_text(encoding="utf-8"))
    assert "kraft-controller" in pools
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    assert "| kraft-controller |" in report


def test_combined_refuses_a_node_override_on_the_controller(tmp_path: Path, capsys) -> None:
    """The override has nothing to replace once the node is gone, so it is
    refused by name rather than accepted and silently dropped."""
    extra = "  overrides:\n    kraft-controller:\n      cpu: 8"
    assert _run(_dial(tmp_path, extra=extra), tmp_path) == 1
    err = capsys.readouterr().err
    assert "sizing.overrides.kraft-controller" in err
    assert "no derived node to override" in err


def test_a_controller_pool_change_is_refused_without_migrate(tmp_path: Path, capsys) -> None:
    """find_locked_changes compares resolved.yaml entries, so the lock fires
    only on a field the resolve itself records."""
    first = tmp_path / "first"
    first.mkdir()
    _run(_dial(first, estimate=1000), first)
    previous = first / "sizing" / "resolved.yaml"

    second = tmp_path / "second"
    second.mkdir()
    status = _run(_dial(second, estimate=1000, controller_pool="separate"), second, previous=previous)

    assert status == resolve_sizing.EXIT_LOCKED_CHANGE
    err = capsys.readouterr().err
    assert "LOCKED controller_mode: combined -> separate" in err
    assert "re-forms the metadata quorum" in err


def test_a_resolve_preserves_an_entry_it_did_not_size(tmp_path: Path) -> None:
    """The file is committed and diffed, so one resolve must not delete another's work."""
    resolved = tmp_path / "shapes" / "resolved"
    resolved.mkdir(parents=True)
    (resolved / "aws-us-west-2.json").write_text(
        json.dumps({"_provenance": "hand-written", "msk-broker": {"instance_types": ["express.m7g.large"]}}),
        encoding="utf-8",
    )
    _run(_dial(tmp_path), tmp_path)
    doc = json.loads((resolved / "aws-us-west-2.json").read_text(encoding="utf-8"))
    assert doc["msk-broker"]["instance_types"] == ["express.m7g.large"]
    assert "kafka-broker" in doc


# ---------------------------------------------------------------------------
# The emitted artefacts
# ---------------------------------------------------------------------------


def test_the_tfvars_names_only_variables_the_root_declares(tmp_path: Path) -> None:
    """A key the root does not declare is an error there, so it never gets emitted."""
    _run(_dial(tmp_path), tmp_path)
    doc = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
    declared = set(
        re.findall(
            r'^variable "([a-z_]+)"',
            (REPO_ROOT / "terraform" / "environments" / "aws" / "variables.tf").read_text(
                encoding="utf-8"
            ),
            re.M,
        )
    )
    assert set(doc) <= declared


def test_the_node_pool_objects_carry_every_attribute_the_variable_type_demands(tmp_path: Path) -> None:
    _run(_dial(tmp_path), tmp_path)
    doc = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
    demanded = {"shape_ref", "min_size", "max_size", "desired_size", "capacity_type", "disk_gb", "labels", "taints"}
    for pool in doc["node_pools"].values():
        assert set(pool) == demanded
    for shape in doc["resolved_shapes"].values():
        assert set(shape) == {"instance_types", "arch"}


# karpenter-pools' own values.yaml documents its per-pool fields in a COMMENT
# above `pools: {}` (empty on purpose -- every field is filled by the
# resolver), so those names never appear as literal `key:` lines there the way
# kafka's and clickhouse-cluster's own defaults do. Checked against this fixed
# contract instead, which mirrors the render guard in
# helm/charts/karpenter-pools/templates/_pools.tpl exactly.
KARPENTER_VALUES_FIELDS = {
    "karpenter",
    "pools",
    "families",
    "arch",
    "capacityTypes",
    "consolidation",
    "policy",
    "after",
    "budgetNodes",
    "expireAfter",
    "labels",
    "limits",
    "cpu",
    "memory",
    "weight",
    "nodeClass",
    "rootVolume",
    "sizeGi",
    "type",
    "iops",
    "throughputMibS",
    "instanceStorePolicy",
    "generationGt",
    "generationIn",
    # The NoSchedule taint a dedicated pool carries, and the three fields one
    # taint entry is made of.
    "taints",
    "key",
    "value",
    "effect",
    # The pool label every workload carries, matching build_tfvars' own
    # node_pools labels convention.
    "dfe.hyperi.io/workload",
}


def test_the_values_overlay_names_only_keys_the_charts_declare(tmp_path: Path) -> None:
    """A values key the chart does not read is a number nobody applies."""
    _run(_dial(tmp_path), tmp_path)
    emitted = (tmp_path / "sizing" / "scale.values.yaml").read_text(encoding="utf-8")
    charts = "\n".join(
        (REPO_ROOT / "helm" / "charts" / chart / "values.yaml").read_text(encoding="utf-8")
        for chart in ("kafka", "clickhouse-cluster")
    )
    declared = set(re.findall(r"^\s*([A-Za-z][A-Za-z0-9]*):", charts, re.M))
    for line in emitted.splitlines():
        stripped = line.strip()
        # A list ITEM (Karpenter's families, capacityTypes, generationIn) has
        # no key of its own -- it belongs to the key the line above it named.
        # A list ITEM opener carries no key of its own -- a scalar item is
        # `- <value>`, and a map item is a bare `-` with its keys below.
        if not stripped or stripped == "-" or stripped.startswith(("##", "- ")):
            continue
        key = line.split(":", 1)[0].strip()
        # Under karpenter.pools, the key IS the use case (eks-system,
        # kafka-broker, ...) -- a map keyed by name, not a chart-declared field.
        assert key in declared or key in KARPENTER_VALUES_FIELDS or key in resolve_sizing.USE_CASES, key


def test_every_karpenter_pool_carries_every_field_the_chart_guard_requires(tmp_path: Path) -> None:
    """Mirrors helm/charts/karpenter-pools/templates/_pools.tpl's own render guard."""
    _run(_dial(tmp_path), tmp_path)
    values = (tmp_path / "sizing" / "scale.values.yaml").read_text(encoding="utf-8")
    karpenter_block = values.split("\nkarpenter:", 1)[1]
    for required in (
        "families:",
        "arch:",
        "capacityTypes:",
        "consolidation:",
        "policy:",
        "after:",
        "budgetNodes:",
        "expireAfter:",
        "limits:",
    ):
        assert required in karpenter_block, required
    # Every pool names a generation floor or a pin -- never neither, and never
    # both -- exactly as templates/_pools.tpl's guard demands.
    for pool_body in re.split(r"^    \S+:$", karpenter_block.split("pools:", 1)[1], flags=re.M)[1:]:
        assert ("generationGt:" in pool_body) != ("generationIn:" in pool_body)


def test_the_dedicated_node_groups_are_tainted_and_the_shared_ones_are_not(tmp_path: Path) -> None:
    """An untainted dedicated group is what let cnpg, dfe-ui and forgejo take the
    CPU ClickHouse and Kafka were sized for. The dial asks for a separate
    controller pool so every dedicated use case has a group to check."""
    _run(_dial(tmp_path, controller_pool="separate"), tmp_path)
    pools = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))["node_pools"]
    for use_case in resolve_sizing.DEDICATED_USE_CASES:
        assert pools[use_case]["taints"] == [
            {"key": resolve_sizing.WORKLOAD_LABEL, "value": use_case, "effect": "NO_SCHEDULE"}
        ], use_case
        assert pools[use_case]["labels"] == {resolve_sizing.WORKLOAD_LABEL: use_case}, use_case
    for use_case, body in pools.items():
        if use_case not in resolve_sizing.DEDICATED_USE_CASES:
            assert body["taints"] == [], use_case


def test_every_dedicated_karpenter_pool_carries_the_same_taint(tmp_path: Path) -> None:
    """Karpenter takes NoSchedule where the managed group takes NO_SCHEDULE, so
    a node the pool adds refuses what the managed group refuses. The dial asks
    for a separate controller pool so every dedicated use case has one to
    check."""
    _run(_dial(tmp_path, controller_pool="separate"), tmp_path)
    pools = json.loads((tmp_path / "sizing" / "scale.karpenter.json").read_text(encoding="utf-8"))
    for use_case in resolve_sizing.DEDICATED_USE_CASES:
        assert pools[use_case]["taints"] == [
            {"key": resolve_sizing.WORKLOAD_LABEL, "value": use_case, "effect": "NoSchedule"}
        ], use_case
    for use_case, body in pools.items():
        if use_case not in resolve_sizing.DEDICATED_USE_CASES:
            assert "taints" not in body, use_case


def test_the_karpenter_pools_are_written_where_bootstrap_can_read_them(tmp_path: Path) -> None:
    """bootstrap.sh ships no YAML parser, and the values overlay only reaches a
    cluster through a deploy repo -- so the pools are written again as one line
    of JSON for the cluster-secret annotation."""
    _run(_dial(tmp_path), tmp_path)
    body = (tmp_path / "sizing" / "scale.karpenter.json").read_text(encoding="utf-8")
    assert body.count("\n") == 1, "the annotation takes one line"
    pools = json.loads(body)
    values = (tmp_path / "sizing" / "scale.values.yaml").read_text(encoding="utf-8")
    for name in pools:
        assert f"    {name}:" in values, name
    assert "'" not in body, "a single quote would break the annotation's own quoting"


def test_the_report_cites_a_source_and_a_confidence_for_every_ratio(tmp_path: Path) -> None:
    """A number nobody can check later is how a wrong cluster gets built confidently."""
    _run(_dial(tmp_path), tmp_path)
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| `([\w.]+)` \| .+ \| ([\w-]+) \| (\S+) ", report, re.M)
    assert len(rows) > 20
    for path, confidence, source in rows:
        assert confidence in resolve_sizing.__dict__.get("CONFIDENCE", ()) or confidence in (
            "vendor-documented",
            "benchmark-named-hardware",
            "rule-of-thumb",
            "dfe-core-evidence",
            "measured",
            "measure-only",
        ), path
        assert source.startswith(("http", "sizing/", "helm/", "docs/", "dfe-")), (path, source)


def test_the_report_states_where_the_ceiling_is(tmp_path: Path) -> None:
    _run(_dial(tmp_path), tmp_path)
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    assert "100,000 GB/day" in report
    assert "professional-services" in report


# ---------------------------------------------------------------------------
# On-prem: demand, and the refusal that validates it
# ---------------------------------------------------------------------------


def test_onprem_emits_the_node_requirements_table(tmp_path: Path) -> None:
    _run(_dial(tmp_path, cloud="onprem"), tmp_path, cloud="onprem")
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    assert "On-prem node requirements" in report
    assert "Bootstrap REFUSES" in report
    assert "| kafka-broker |" in report


def test_onprem_states_the_override_and_its_warning(tmp_path: Path) -> None:
    dial = _dial(tmp_path, cloud="onprem", extra="  allow_undersized: \"true\"")
    _run(dial, tmp_path, cloud="onprem")
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    assert "OVERRIDES the refusal" in report
    assert "THE WARNING STANDS" in report


def test_onprem_resolves_no_instance_type(tmp_path: Path) -> None:
    """We do not create these nodes, so there is no shape to pick -- only a demand."""
    _run(_dial(tmp_path, cloud="onprem"), tmp_path, cloud="onprem")
    assert not (tmp_path / "shapes" / "resolved" / "onprem.json").exists()
    assert (tmp_path / "sizing" / "scale.values.yaml").is_file()


# ---------------------------------------------------------------------------
# Deployer overrides
# ---------------------------------------------------------------------------


def test_an_override_replaces_the_derived_value_and_says_so(tmp_path: Path) -> None:
    _run(_dial(tmp_path, extra=OVERRIDE_BLOCK), tmp_path)
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    assert "Overridden by the deployer" in report
    assert "| kafka-broker | `replicas` | 3 | **6** |" in report
    doc = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
    assert doc["node_pools"]["kafka-broker"]["desired_size"] == 6
    assert doc["resolved_shapes"]["kafka-broker"]["instance_types"] == ["m9g.8xlarge"]


def test_an_override_still_runs_through_the_assertions(tmp_path: Path) -> None:
    """An override is a different answer, never an exemption from a ceiling."""
    extra = "  overrides:\n    keeper:\n      iops: 60000"
    _run(_dial(tmp_path, extra=extra), tmp_path)
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    assert "| A1" in report
    assert "60,000 IOPS" in report


def test_an_override_naming_a_type_a_zone_does_not_offer_is_refused(tmp_path: Path, capsys) -> None:
    """AZs diverge inside one region, and a node pool cannot place in the gap."""
    extra = "  overrides:\n    kafka-broker:\n      instance_type: a1.xlarge"
    assert _run(_dial(tmp_path, extra=extra), tmp_path) == 1
    assert "missing from at least one" in capsys.readouterr().err


def test_an_override_naming_a_type_the_region_has_never_heard_of_is_refused(
    tmp_path: Path, capsys
) -> None:
    extra = "  overrides:\n    kafka-broker:\n      instance_type: m99g.4xlarge"
    assert _run(_dial(tmp_path, extra=extra), tmp_path) == 1
    assert "is not offered in" in capsys.readouterr().err


def test_an_unknown_override_field_is_refused(tmp_path: Path, capsys) -> None:
    extra = "  overrides:\n    kafka-broker:\n      cores: 8"
    assert _run(_dial(tmp_path, extra=extra), tmp_path) == 1
    assert "not a field" in capsys.readouterr().err


def test_an_override_on_an_unknown_use_case_is_refused(tmp_path: Path, capsys) -> None:
    extra = "  overrides:\n    kafka-brokers:\n      cpu: 8"
    assert _run(_dial(tmp_path, extra=extra), tmp_path) == 1
    assert "not a use case" in capsys.readouterr().err


def test_the_committed_dial_template_parses_with_no_overrides(tmp_path: Path) -> None:
    """`overrides: {}` in the shipped template must not read as a broken map."""
    dial = resolve_sizing.read_dial(REPO_ROOT / "deployment.example.yaml")
    assert dial.overrides == {}


# ---------------------------------------------------------------------------
# MSK, whose broker types are not EC2 types
# ---------------------------------------------------------------------------


def test_the_msk_path_resolves_a_broker_shape(tmp_path: Path) -> None:
    _run(_dial(tmp_path, provider="msk", estimate=1000), tmp_path)
    doc = json.loads((tmp_path / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8"))
    entry = doc["msk-broker"]
    assert entry["instance_types"][0].startswith("express.m7g.")
    assert "price_usd_hour" not in entry


def test_an_msk_family_with_no_price_fails_the_selection(catalogue) -> None:
    """The broker namespace is its own, so an unpriced family must fail by name."""
    unpriced = resolve_sizing.Catalogue(
        region=catalogue.region,
        azs=catalogue.azs,
        types=catalogue.types,
        offerings=catalogue.offerings,
        msk_families=catalogue.msk_families,
        msk_prices={},
        unparsed=0,
        seen=catalogue.seen,
        source="test",
        captured=catalogue.captured,
    )
    demand = resolve_sizing.Node("msk-broker", 3, 2, 8, 0, 0, 0, "test")
    with pytest.raises(resolve_sizing.ResolveError, match=r"lists no express\.m7g"):
        resolve_sizing.select_msk_shape(
            "msk-broker",
            {"family": "m", "generation_pin": "7", "size": "large", "volumes": {}},
            demand,
            unpriced,
            ROOT_VOLUME_DEMAND_MIB_S,
            ROOT_VOLUME_DEMAND_IOPS,
        )


def test_the_msk_broker_reads_the_same_demand_the_self_hosted_broker_would(tmp_path: Path) -> None:
    """A managed broker is the same requirement, asked of a vendor."""
    for band, smallest in ((1000, "express.m7g.large"), (10000, "express.m7g.4xlarge")):
        out = tmp_path / str(band)
        out.mkdir()
        _run(_dial(out, provider="msk", estimate=band), out)
        doc = json.loads((out / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8"))
        assert doc["msk-broker"]["instance_types"][0] == smallest, band


def test_a_managed_broker_is_never_a_kubernetes_node_pool(tmp_path: Path) -> None:
    """AWS runs those brokers, so no node group of ours creates them."""
    _run(_dial(tmp_path, provider="msk", estimate=10000), tmp_path)
    doc = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
    assert "msk-broker" not in doc["node_pools"]
    assert "msk-broker" in doc["resolved_shapes"]


def test_the_msk_path_sizes_no_broker_pvc_or_controller_pool(tmp_path: Path) -> None:
    """Express manages its own storage and runs its own metadata quorum."""
    _run(_dial(tmp_path, provider="msk", estimate=1000), tmp_path)
    doc = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
    assert "kraft-controller" not in doc["resolved_shapes"]
    values = (tmp_path / "sizing" / "scale.values.yaml").read_text(encoding="utf-8")
    assert "replicas" not in values.split("clickhouse:")[0]


# ---------------------------------------------------------------------------
# Sizing up, rather than reporting a volume the instance cannot carry
# ---------------------------------------------------------------------------


def test_the_instance_steps_up_until_it_sustains_its_own_volumes(tmp_path: Path) -> None:
    """A3 fires only when no size in the family can carry the DEMANDED IO.

    gate-3 remedy 2: this used to assert `sum(provisioned) <= baseline` read
    straight off the committed `shapes/resolved/aws-us-west-2.json` -- which
    is exactly the bug that file's own A3 fix corrected, since a volume's
    provisioned ceiling (clickhouse's data volume asks for 500 MiB/s at every
    scale) is money the deployer is free to leave unused, not traffic the
    instance has to carry. The persisted file no longer even carries a
    volume's demand (see _shape_volumes -- it is deployment-specific, like a
    formula-derived size), so this re-derives it the same way the resolver
    itself does and checks the REAL invariant `_size_up_to_carry_the_volumes`
    guarantees: every chosen instance's OWN _volumes_fit against its OWN
    demand, never mind what it happens to be provisioned for.
    """
    dial_path = _dial(tmp_path)
    _run(dial_path, tmp_path)
    doc = json.loads((tmp_path / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8"))

    sizing = resolve_sizing._load(resolve_sizing.SIZING_FILE)
    shapes = resolve_sizing._load(resolve_sizing.SHAPES_FILE)
    dial = resolve_sizing.read_dial(dial_path, cloud="aws")
    core = resolve_sizing.size_core(sizing, dial)
    live_catalogue = resolve_sizing.fetch_fixtures(FIXTURES, "us-west-2")

    for use_case, entry in doc.items():
        if use_case.startswith("_") or use_case == "msk-broker":
            continue
        chosen_name = entry["instance_types"][0]
        chosen = live_catalogue.types[chosen_name]
        shape_entry = resolve_sizing._at(shapes, "clouds", "aws", "use_cases", use_case)
        demand = core.nodes.get(
            resolve_sizing.BROKER_DEMAND.get(use_case, use_case)
        ) or resolve_sizing.Node(use_case, 1, 0, 0, 0, 0, 0, "cluster overhead")
        built = resolve_sizing._resolve_volumes(
            shape_entry, demand, chosen, core.root_volume_demand_mib_s, core.root_volume_demand_iops
        )
        assert resolve_sizing._volumes_fit(built, chosen), use_case


def test_the_step_up_is_reported_with_its_price_delta(catalogue) -> None:
    """A demand no size in the family can carry at its own floor size steps up
    within the family, and the note says by how much and at what price.

    Built from a synthetic demand and a hand-shaped entry rather than a dial
    band, so the step-up path stays covered whatever sizing.yaml's own ratios
    resolve to -- Keeper's fixed 6,000-IOPS target used to be the one thing in
    the whole golden matrix that forced this path, and scaling it to the
    resolved ingest means nothing in that matrix triggers it any more.
    """
    entry = {
        "family": "m",
        "modifiers": "",
        "generation_policy": "newest",
        "generation_pin": "",
        "price_policy": "newest",
        "price_step_max_pct": "",
        "price_generations": "",
        "size": "large",
        "volumes": {
            "root": {
                "type": "gp3",
                "size_gib": "40",
                "size_formula": "fixed",
                "iops": "3000",
                "throughput_mib_s": "125",
                "throughput_policy": "instance-baseline",
            },
            "data": {
                "type": "gp3",
                "size_gib": "",
                "size_formula": "iops-derived",
                "iops": "6000",
                "throughput_mib_s": "125",
            },
        },
    }
    # m9g.large's own baseline is 3,600 IOPS; m9g.xlarge's is 6,000. 5,000
    # demanded plus root's 300 is 5,300 -- above large, comfortably under
    # xlarge, so exactly one step.
    demand = resolve_sizing.Node("keeper", 3, 1, 3, 20, 5000, 0, "test demand above the floor size's baseline")
    notes: list[str] = []
    choice = resolve_sizing.select_shape(
        "keeper",
        entry,
        demand,
        catalogue,
        "newest",
        notes,
        ROOT_VOLUME_DEMAND_MIB_S,
        ROOT_VOLUME_DEMAND_IOPS,
    )
    assert choice.instance_type == "m9g.xlarge", choice.instance_type
    assert any("stepped up from" in note for note in notes), notes
    assert any("an hour a node" in note for note in notes), notes


def test_a_root_volume_takes_what_the_instance_has_left_to_give(tmp_path: Path) -> None:
    """A fixed 125 MiB/s root on a size whose baseline is 95 is a silent clamp."""
    _run(_dial(tmp_path), tmp_path)
    doc = json.loads((tmp_path / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8"))
    root = doc["kafka-broker"]["volumes"]["root"]
    assert root["source"] == "instance-baseline"
    assert root["throughput_mib_s"] >= 125


# ---------------------------------------------------------------------------
# The fixture itself
# ---------------------------------------------------------------------------


def test_the_captured_fixture_carries_nothing_account_specific() -> None:
    """dfe-infra goes public at GA, so a committed fixture holds no account detail."""
    assert not resolve_sizing._scrub_findings(CATALOGUE.read_text(encoding="utf-8"))


def test_the_fixture_parsed_every_type_name_it_was_given() -> None:
    """The fail-safe guard is only meaningful while the parser reads what AWS returns."""
    doc = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    assert doc["unparsed"] / doc["seen"] <= resolve_sizing.MAX_UNPARSED_FRACTION


# ---------------------------------------------------------------------------
# The locked-change classifier
# ---------------------------------------------------------------------------


def test_locked_change_detection_skips_a_field_missing_from_either_document() -> None:
    """A resolved.yaml written before a field was recorded carries no entry for
    it, so there is nothing to diff it against and it is skipped rather than
    reported as a change."""
    sizing = {"locked": {"controller_mode": "the quorum re-forms", "cloud_token": "a new deployment"}}
    previous = {"locked": {"cloud_token": "aws"}}
    resolved = {"locked": {"controller_mode": "combined", "cloud_token": "gcp"}}
    changes = resolve_sizing.find_locked_changes(sizing, previous, resolved)
    assert [c.field for c in changes] == ["cloud_token"]
    assert changes[0].old == "aws"
    assert changes[0].new == "gcp"
    assert changes[0].reason == "a new deployment"


def test_the_storage_model_is_recorded_so_the_lock_on_it_can_fire(tmp_path: Path) -> None:
    """The resolver derives clickhouse.storageModel from sizing.storage_model, so
    the field sizing.yaml locks has to appear in resolved.yaml to be compared --
    moving parts between the PVC and the object store is a data migration."""
    dial = _dial(tmp_path, provider="strimzi", estimate=1000)
    assert _run(dial, tmp_path) == 0
    doc = resolve_sizing._load(tmp_path / "sizing" / "resolved.yaml")
    assert doc["locked"]["storage_model"] == "auto"


def test_a_storage_model_change_is_refused_without_migrate(tmp_path: Path, capsys) -> None:
    first = tmp_path / "first"
    first.mkdir()
    _run(_dial(first, provider="strimzi", estimate=1000), first)
    previous = first / "sizing" / "resolved.yaml"

    second = tmp_path / "second"
    second.mkdir()
    dial = _dial(second, provider="strimzi", estimate=1000, extra="  storage_model: local")
    status = _run(dial, second, previous=previous)

    assert status == resolve_sizing.EXIT_LOCKED_CHANGE
    err = capsys.readouterr().err
    assert "LOCKED storage_model: auto -> local" in err
    assert "--migrate" in err


def test_the_compute_bucket_is_the_reports_own_total_row(tmp_path: Path) -> None:
    """One bucket, two readers: the banner reads this field and an operator
    reads the table, so the two may never disagree."""
    dial = _dial(tmp_path, provider="msk", estimate=1000)
    assert _run(dial, tmp_path) == 0
    doc = resolve_sizing._load(tmp_path / "sizing" / "resolved.yaml")
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")

    total_row = next(line for line in report.splitlines() if "**total compute**" in line)
    assert total_row.split("|")[-2].strip().strip("*") == doc["compute_bucket"]
    assert doc["compute_bucket"] in {"XS", "S", "M", "L", "XL"}
    assert "compute_usd_per_hour" not in doc


def _priced_choice(
    use_case: str, price: float, count: int, vcpu: int = 2
) -> resolve_sizing.Choice:
    return resolve_sizing.Choice(
        use_case=use_case, instance_type=f"{use_case}.large", fallbacks=[], vcpu=vcpu,
        memory_gib=8.0, generation=9, generation_policy="floor", price_policy="on-demand",
        price_usd_hour=price, physical_processor="Graviton", instance_store_gb=0,
        baseline_iops=3000, baseline_throughput_mib_s=125.0, maximum_iops=48000,
        maximum_throughput_mib_s=1500.0, volumes={}, count=count,
    )


def test_a_shape_resolving_to_zero_adds_nothing_to_the_compute_bucket(tmp_path: Path) -> None:
    """A Karpenter pool holding no node stands up no vCPU, so it must not push
    the deployment a rung up the ladder."""
    core = resolve_sizing.Core(
        tier="scale", focus="economy", headroom=0.0, estimated=True, ingest_gb_per_day=0.0,
        avg_mb_s=0.0, peak_mb_s=0.0, peak_factor=1.0, required_mb_s=0.0, carried_mb_s=0.0,
    )
    dial = resolve_sizing.read_dial(_dial(tmp_path))
    choices = {
        "kafka-broker": _priced_choice("kafka-broker", 0.5, 3),
        "ci-burst": _priced_choice("ci-burst", 4.0, 0, vcpu=32),
    }
    doc = resolve_sizing.build_resolved(core, dial, None, choices)
    assert doc["compute_bucket"] == "XS"
    # The rung the empty pool would have crossed, had its one node been counted.
    assert resolve_sizing._vcpu_bucket(3 * 2 + 32) == "S"


def test_an_unchanged_previous_behaves_exactly_as_today(tmp_path: Path) -> None:
    """Re-resolving the same dial against its own committed resolved.yaml changes nothing."""
    dial = _dial(tmp_path, provider="strimzi", estimate=1000)
    first_status = _run(dial, tmp_path)
    previous = tmp_path / "sizing" / "resolved.yaml"
    assert previous.is_file()

    second_status = _run(dial, tmp_path, previous=previous)
    assert second_status == first_status == 0
    assert (tmp_path / "sizing" / "scale.report.md").is_file()
    assert (tmp_path / "sizing" / "resolved.yaml").is_file()


def test_a_locked_change_is_refused_without_migrate(tmp_path: Path, capsys) -> None:
    """kafka.provider moving from strimzi to msk is msk_broker_type -- LOCKED."""
    first = tmp_path / "first"
    first.mkdir()
    _run(_dial(first, provider="strimzi", estimate=1000), first)
    previous = first / "sizing" / "resolved.yaml"
    assert previous.is_file()

    second = tmp_path / "second"
    second.mkdir()
    dial = _dial(second, provider="msk", estimate=1000)
    status = _run(dial, second, previous=previous)

    assert status == resolve_sizing.EXIT_LOCKED_CHANGE
    assert not (second / "sizing").exists()
    err = capsys.readouterr().err
    assert "LOCKED msk_broker_type: strimzi -> msk-express" in err
    assert "Standard to Express is a cluster replacement" in err
    assert "--migrate" in err


def test_a_locked_change_is_accepted_and_written_with_migrate(tmp_path: Path, capsys) -> None:
    first = tmp_path / "first"
    first.mkdir()
    _run(_dial(first, provider="strimzi", estimate=1000), first)
    previous = first / "sizing" / "resolved.yaml"

    second = tmp_path / "second"
    second.mkdir()
    dial = _dial(second, provider="msk", estimate=1000)
    status = _run(dial, second, previous=previous, migrate=True)

    assert status == 0
    assert (second / "sizing" / "resolved.yaml").is_file()
    assert (second / "sizing" / "scale.report.md").is_file()
    assert (second / "sizing.auto.tfvars.json").is_file()
    err = capsys.readouterr().err
    assert "MIGRATING msk_broker_type: strimzi -> msk-express" in err
    assert "Standard to Express is a cluster replacement" in err


def test_migrate_without_previous_is_an_argparse_error(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        resolve_sizing.main(["--dial", "deployment.yaml", "--migrate"])
    assert excinfo.value.code == 2
    assert "--migrate requires --previous" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Target overlays -- the per-target knob matrix
# ---------------------------------------------------------------------------

TARGET_OVERLAY_NAMES = (
    "onprem",
    "aws",
    "gcp",
    "azure",
    "msk",
    "confluent-cloud",
    "redpanda-cloud",
    "clickhouse-cloud",
)


@pytest.mark.parametrize("name", TARGET_OVERLAY_NAMES)
def test_every_target_overlay_parses_and_every_knob_carries_a_state_and_reason(name: str) -> None:
    path = resolve_sizing.TARGETS_DIR / f"{name}.yaml"
    assert path.is_file(), path
    tree = resolve_sizing._load(path)
    knobs = {key: value for key, value in tree.items() if isinstance(value, dict)}
    assert knobs, f"{name}.yaml carries no knobs"
    for knob, body in knobs.items():
        assert body.get("state") in resolve_sizing.TARGET_KNOB_STATES, (name, knob)
        assert body.get("reason", "").strip(), (name, knob)


def test_the_report_renders_the_aws_overlay_matrix(tmp_path: Path) -> None:
    _run(_dial(tmp_path), tmp_path)
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    assert "sizing/targets/aws.yaml" in report
    assert "`node_shapes`" in report
    assert "managed" in report


def test_an_unbuilt_target_overlay_states_its_stub_status(tmp_path: Path) -> None:
    """target and cloud resolve independently -- an aws dial may still report
    against an unbuilt target's overlay when --target names one."""
    _run(_dial(tmp_path), tmp_path, target="gcp")
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    assert "validated-stub" in report
    assert "target not built yet" in report


def test_an_unrecognised_target_states_there_is_no_matrix(tmp_path: Path) -> None:
    _run(_dial(tmp_path), tmp_path, target="a-target-nobody-built")
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    assert "There is none for `a-target-nobody-built`" in report


# ---------------------------------------------------------------------------
# Karpenter -- rendered from the same shape entries the resolver chose
# ---------------------------------------------------------------------------


def test_karpenter_generation_floor_is_one_below_the_oldest_named_family() -> None:
    choice = _choice(
        use_case="general",
        instance_type="m9g.8xlarge",
        fallbacks=["m8g.8xlarge", "m7g.8xlarge", "m6g.8xlarge"],
    )
    pools = resolve_sizing.build_karpenter_pools({"general": choice}, {})
    assert pools["general"]["families"] == ["m9g", "m8g", "m7g", "m6g"]
    assert pools["general"]["generationGt"] == "5"


def test_karpenter_generation_floor_with_one_family_is_its_own_generation_minus_one() -> None:
    choice = _choice(use_case="clickhouse", instance_type="r9gd.2xlarge", fallbacks=[])
    pools = resolve_sizing.build_karpenter_pools({"clickhouse": choice}, {})
    assert pools["clickhouse"]["families"] == ["r9gd"]
    assert pools["clickhouse"]["generationGt"] == "8"


def test_karpenter_places_nothing_for_msk_broker() -> None:
    """MSK runs its own brokers -- there is no EC2 instance to place."""
    choice = _choice(use_case="msk-broker", instance_type="express.m7g.large", fallbacks=[])
    assert resolve_sizing.build_karpenter_pools({"msk-broker": choice}, {}) == {}


def test_karpenter_ci_burst_pool_is_spot_first() -> None:
    choice = _choice(use_case="ci-burst", instance_type="c9gd.4xlarge", fallbacks=["c8gd.4xlarge"])
    pool = resolve_sizing.build_karpenter_pools({"ci-burst": choice}, {})["ci-burst"]
    assert pool["capacityTypes"] == ["spot", "on-demand"]
    assert pool["budgetNodes"] == "100%"


def test_karpenter_a_pinned_policy_emits_generation_in_not_generation_gt() -> None:
    choice = _choice(
        use_case="general",
        instance_type="m7g.large",
        fallbacks=[],
        generation=7,
        generation_policy="pinned",
    )
    cloud_entry = {
        "use_cases": {"general": {"generation_policy": "pinned", "generation_pin": "7", "arch": "arm64"}}
    }
    pool = resolve_sizing.build_karpenter_pools({"general": choice}, cloud_entry)["general"]
    assert pool["generationIn"] == ["7"]
    assert "generationGt" not in pool


def test_karpenter_root_volume_carries_raid0_only_where_the_shape_asks_for_it() -> None:
    choice = _choice(
        use_case="clickhouse",
        instance_type="r9gd.2xlarge",
        fallbacks=[],
        volumes={"root": {"type": "gp3", "size_gib": 40, "iops": 3000, "throughput_mib_s": 250}},
    )
    cloud_entry = {
        "use_cases": {
            "clickhouse": {"volumes": {"cache": {"instance_store": "required"}}},
        }
    }
    pool = resolve_sizing.build_karpenter_pools({"clickhouse": choice}, cloud_entry)["clickhouse"]
    assert pool["nodeClass"]["instanceStorePolicy"] == "RAID0"
    assert pool["nodeClass"]["rootVolume"]["sizeGi"] == 40


# ---------------------------------------------------------------------------
# ci-burst -- joins every populated resolve, Karpenter-only
# ---------------------------------------------------------------------------

# A dedicated case alongside the cloud x focus x band matrix above: provider is
# orthogonal to that cross-product, and multiplying it just to cover
# ci-burst-plus-msk together would be disproportionate to what this one case
# needs to prove.
MSK_CI_BURST_CASE = "aws-scale-economy-10000-msk"


@pytest.fixture(scope="module")
def msk_case(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp(MSK_CI_BURST_CASE)
    dial = _dial(out, cloud="aws", focus="economy", estimate=10000, provider="msk")
    status = _run(dial, out, cloud="aws")
    assert status == 0
    return out


def test_ci_burst_is_not_a_dial_knob(tmp_path: Path) -> None:
    """Every populated resolve gets one -- there is no sizing field that opts out."""
    _run(_dial(tmp_path), tmp_path)
    doc = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
    assert "ci-burst" in doc["resolved_shapes"]
    # A fixed EKS managed node group cannot express "spot, 100% disruption
    # budget" -- ci-burst is Karpenter-only.
    assert "ci-burst" not in doc["node_pools"]


def test_msk_plus_ci_burst_carries_exactly_five_karpenter_pools(msk_case: Path) -> None:
    values = (msk_case / "sizing" / "scale.values.yaml").read_text(encoding="utf-8")
    karpenter_block = values.split("\nkarpenter:", 1)[1]
    pools_block = karpenter_block.split("pools:", 1)[1]
    pool_names = set(re.findall(r"^    (\S+):$", pools_block, re.M))
    assert pool_names == {"eks-system", "general", "clickhouse", "keeper", "ci-burst"}


def test_msk_broker_keeps_its_ec2_shape_out_of_karpenter(msk_case: Path) -> None:
    doc = json.loads((msk_case / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8"))
    assert doc["msk-broker"]["instance_types"][0].startswith("express.")
    values = (msk_case / "sizing" / "scale.values.yaml").read_text(encoding="utf-8")
    assert "msk-broker" not in values.split("\nkarpenter:", 1)[1]


# ---------------------------------------------------------------------------
# The sixth artefact -- the machine-readable on-prem node demand
# ---------------------------------------------------------------------------


def test_onprem_writes_the_nodes_json_artefact(tmp_path: Path) -> None:
    _run(_dial(tmp_path, cloud="onprem"), tmp_path, cloud="onprem")
    doc = json.loads((tmp_path / "sizing" / "scale.nodes.json").read_text(encoding="utf-8"))
    assert set(doc) >= {"kafka-broker", "clickhouse", "keeper"}
    for use_case, body in doc.items():
        assert set(body) == {"count", "cpu", "memory_gib", "disk_gb"}, use_case
        assert body["count"] >= 3, use_case


def test_a_populated_cloud_never_writes_a_nodes_json(tmp_path: Path) -> None:
    """The demand-only artefact is on-prem's alone -- a cloud creates its own nodes."""
    _run(_dial(tmp_path), tmp_path)
    assert not (tmp_path / "sizing" / "scale.nodes.json").exists()


# ---------------------------------------------------------------------------
# Fragment-to-chart render -- gate-3-correctness.md P1-1 and the review's own
# recommended test (a): nothing else runs `helm template` against the
# resolver's own output, which is exactly how the ClickHouse storageModel
# fragment shipped broken.
# ---------------------------------------------------------------------------

HELM_BIN = shutil.which("helm")
CHARTS_DIR = REPO_ROOT / "helm" / "charts"

# The minimal values a REAL AWS deploy's OTHER overlays supply -- the S3
# endpoint (a bucket only terraform knows), the Karpenter cluster facts (the
# kubernetes-cluster module's own output) -- so the render is judged on the
# UNION argocd actually applies, never the resolver's fragment alone. Never
# the resolver's job to fill in: it derives sizing, not bucket URLs or IAM
# ARNs.
CHART_RENDER_BASES: dict[str, dict[str, object]] = {
    "kafka": {"appNamespace": "dfe", "kafka": {"mode": "cluster", "provider": "strimzi"}},
    "clickhouse-cluster": {
        "appNamespace": "dfe",
        "clickhouse": {
            "mode": "cluster",
            "replicas": 3,
            "shardsCount": 1,
            "objectStore": {"endpoint": "https://s3.us-west-2.amazonaws.com/dfe-clickhouse-test/"},
        },
    },
    "karpenter-pools": {
        "karpenter": {
            "cluster": {
                "discoveryTag": "dfe-test",
                "instanceProfile": "dfe-test-karpenter",
                "kmsKeyId": "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000",
            }
        }
    },
}
CHART_RENDER_CHARTS = tuple(CHART_RENDER_BASES)


@pytest.fixture(scope="module")
def aws_golden_outputs(tmp_path_factory) -> dict[str, Path]:
    """The output directory for every AWS golden-matrix case (cloud x focus x
    band, strimzi), so the chart render tests below have the same
    `scale.values.yaml` fragment the golden snapshot already covers."""
    outputs: dict[str, Path] = {}
    for focus in FOCUSES:
        for band in BANDS:
            name = _case_name("aws", focus, band)
            out = tmp_path_factory.mktemp("render-" + name.replace("-", "_"))
            _run(_dial(out, cloud="aws", focus=focus, estimate=band), out, cloud="aws")
            outputs[name] = out
    return outputs


def _helm_template(chart: str, base: dict[str, object], fragment: Path) -> subprocess.CompletedProcess:
    base_path = fragment.with_name(f"_render_base_{chart}.json")
    # JSON is valid YAML, so this needs no writer beyond the stdlib's own.
    base_path.write_text(json.dumps(base), encoding="utf-8")
    chart_dir = CHARTS_DIR / chart
    return subprocess.run(
        ["helm", "template", "t", str(chart_dir), "-f", str(base_path), "-f", str(fragment)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("chart", CHART_RENDER_CHARTS)
@pytest.mark.parametrize(
    "case", [_case_name("aws", focus, band) for focus in FOCUSES for band in BANDS]
)
def test_the_resolved_fragment_renders_against_its_chart(
    aws_golden_outputs: dict[str, Path], chart: str, case: str
) -> None:
    """The values fragment resolve_sizing.py writes must not break the chart
    it targets. `helm lint` never catches this class of bug -- it has no
    notion of what the resolver derives -- so this is the one place the two
    halves actually meet. P1-1: the resolver used to emit
    clickhouse.objectStore.cache.volume: instance-store with no
    clickhouse.storageModel beside it, which _storage.tpl's own guard
    refuses outright.
    """
    if HELM_BIN is None:
        pytest.skip("helm is not on PATH")
    fragment = aws_golden_outputs[case] / "sizing" / "scale.values.yaml"
    result = _helm_template(chart, CHART_RENDER_BASES[chart], fragment)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# P1-3 -- the two tfvars producers must not collide on the same variable
# ---------------------------------------------------------------------------

# The same shape scripts/tests/test_render_dial_tofu.py's own fixture dial
# uses, trimmed to what _tofu_vars needs -- render_dial.py is a sibling's
# file and is called here AS IT IS, never edited, so this test proves (or
# disproves) the composition rather than asserting a behaviour of its own.
RENDER_DIAL_TOFU_FIXTURE = """
substrate: k8s
metadata:
  name: dfe-example
profile: scale
registry: ghcr.io/hyperi-io
target:
  provision:
    cloud: aws
    account: "000000000000"
    region: us-west-2
    cidr: 10.90.0.0/16
kubernetes_version: "1.36"
network:
  nat: single
endpoint:
  public: "false"
  allowed_cidrs: ""
dns:
  private_zone: dfe-example.internal
  public_zone: ""
node_pools:
  system:
    shape_ref: eks-system
    min_size: 2
    max_size: 3
    desired_size: 2
    capacity_type: ON_DEMAND
    disk_gb: 40
telemetry:
  aws:
    sink: otel
    retention_days: 2
kafka:
  provider: strimzi
  landing_topics:
    main_land:
state:
  bucket: example-tfstate
  key: dfe/test/aws.tfstate
  region: us-west-2
tags:
  service-name: dfe
  service-namespace: example
  environment: test
  owner: owner@example.com
  cost-center: experiments
  lifecycle: ephemeral
secrets:
  backend: aws-sm
  ref: dfe
k8s:
  env: test
  storage_class: gp3
  repo_url: https://github.com/example/dfe-infra.git
  target_revision: main
endpoints:
  clickhouse_host: ""
  kafka_bootstrap: ""
  otel_endpoint: ""
"""


def _render_dial_tofu_vars() -> dict[str, object]:
    """The variables render_dial.py --tofu produces today, called directly --
    it reads shapes/resolved/aws-us-west-2.json, the same committed file this
    script's own resolve merges into."""
    tree = parse_dial(RENDER_DIAL_TOFU_FIXTURE, source="test-tofu-fixture")
    _, variables = render_dial._tofu_vars(tree)
    return variables


def _required_tf_variables(path: Path) -> set[str]:
    """Every `variable` this root declares with no `default` -- OpenTofu
    prompts (or fails outright, unattended) on a plan that leaves one of
    these unset, so the two tfvars producers together have to cover all of
    them. Brace-matched per variable block, not a flat regex over the whole
    file, because `type = object({...})` nests its own braces and an
    `optional(type, value)` default inside one is not a `default =` line."""
    text = path.read_text(encoding="utf-8")
    required: set[str] = set()
    for match in re.finditer(r'variable\s+"([^"]+)"\s*\{', text):
        name = match.group(1)
        depth = 1
        i = match.end()
        while depth > 0 and i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        body = text[match.end() : i]
        if not re.search(r"^\s*default\s*=", body, re.MULTILINE):
            required.add(name)
    return required


def test_the_two_tfvars_producers_compose_with_no_variable_defined_twice(tmp_path: Path) -> None:
    """OpenTofu auto-loads every *.auto.tfvars.json in the root directory in
    ALPHABETICAL ORDER, and a later file replaces a top-level key WHOLESALE
    rather than merging it -- gate-3-correctness.md P1-3, where
    dial.auto.tfvars.json and the sizing tfvars both declared node_pools and
    the sized brokers vanished under the system group's own file (or vice
    versa, whichever file sorted last). resolve_sizing.py now writes
    sizing.auto.tfvars.json at the root of --out, beside dial.auto.tfvars.json
    ('d' before 's'), and merges the dial's own node_pools.system into what it
    derives -- so it is the SINGLE writer of that variable and there is
    nothing left for the two files to collide on there.

    gate-3 remedy 2 left this red for a test-construction reason, not a
    resolver bug: `_dial()`'s own minimal dial never writes a `node_pools:`
    block at all, so nothing here ever gave the resolver a `system` group to
    merge -- there was nothing wrong to find. RENDER_DIAL_TOFU_FIXTURE above
    (a realistic dial) carries exactly this block, so the fix gives the
    RESOLVER-facing dial the same one -- appended after everything `_dial()`
    writes, never through its own `extra` splice: that lands mid-`sizing:`,
    and a `node_pools:` block deep enough to carry disk_gb re-dents back to
    2 spaces, which is exactly where `_dial()` places its own next line
    (`spend_warn_usd_month`), so it would read as a sixth node_pools.system
    FIELD rather than a sizing one.
    """
    dial_path = _dial(tmp_path, cloud="aws", estimate=10000)
    with dial_path.open("a", encoding="utf-8") as f:
        f.write(
            "node_pools:\n"
            "  system:\n"
            "    shape_ref: eks-system\n"
            "    min_size: 2\n"
            "    max_size: 3\n"
            "    desired_size: 2\n"
            "    capacity_type: ON_DEMAND\n"
            "    disk_gb: 40\n"
        )
    dial_vars = _render_dial_tofu_vars()
    _run(dial_path, tmp_path, cloud="aws")
    sizing_vars = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))

    overlap = set(dial_vars) & set(sizing_vars)
    assert not overlap, (
        f"dial.auto.tfvars.json and sizing.auto.tfvars.json both declare {sorted(overlap)} -- "
        "tofu's later-file-wins load order silently drops whichever one lost"
    )
    assert "system" in sizing_vars["node_pools"], sizing_vars["node_pools"]
    assert sizing_vars["node_pools"]["system"]["shape_ref"] == "eks-system"

    # The two producers TOGETHER have to cover every variable the root
    # declares with no default -- a gap here is an unattended plan prompting
    # for a value neither file ever supplies.
    required = _required_tf_variables(
        REPO_ROOT / "terraform" / "environments" / "aws" / "variables.tf"
    )
    covered = set(dial_vars) | set(sizing_vars)
    missing = required - covered
    assert not missing, (
        f"variables.tf declares {sorted(missing)} with no default, and neither "
        "dial.auto.tfvars.json nor sizing.auto.tfvars.json writes it"
    )


# ---------------------------------------------------------------------------
# P1-4 -- the no-estimate floor must not size up on a fixed volume profile
# ---------------------------------------------------------------------------


def test_the_no_estimate_floor_does_not_size_up_on_a_fixed_volume_profile(tmp_path: Path) -> None:
    """Q27/Q29: 3 brokers, 3 ClickHouse replicas plus Keeper, economy, the
    smallest shape that does not OOM.

    gate-3 remedy 1 cut this floor by deriving the data volume's throughput
    from demand instead of a fixed scale assumption.
    gate-3 remedy 2 cut it again by summing `demand_iops` /
    `demand_throughput_mib_s` in A3 instead of the PROVISIONED figures every
    gp3 volume carries -- but it still gave the mandatory root volume gp3's
    own free minimum (3,000 IOPS / 125 MiB/s) AS its demand, on the theory
    that root's floor is never optional so its demand had to equal that
    resolved figure. That is the same ceiling-as-demand error the "data"
    volume's own fix had just corrected, one level up: 125 MiB/s is above
    m9g.large's and r9gd.large's own baseline throughput (95 MiB/s), which
    forced every use case with a gp3 root past a PHYSICAL floor of xlarge
    regardless of what it demanded.

    gate-4 (this fix) gives root a small, honest, documented demand instead
    (sizing.yaml's floors.root_volume_demand_mib_s / _iops, 10 MiB/s / 300
    IOPS -- what a quiet root disk carrying the OS, container images and
    logs actually asks of the instance, capped at whatever it is
    provisioned for). That is comfortably under every candidate's baseline,
    so the physical xlarge floor is gone and every use case falls through to
    its OWN real floor:

    - kafka-broker and eks-system land on `m9g.large` (2 vCPU / 8 GiB) --
      sizing.yaml's own kafka_broker_vcpu/_ram_gib floor, unreachable before
      because root's inflated demand always forced xlarge first.
    - kraft-controller lands on `m9g.medium` (1 vCPU / 4 GiB) --
      kraft_controller.vcpu_floor/ram_gib_floor, its true floor since it is
      sized by PARTITION COUNT, never throughput, and root is now the only
      demand it carries.
    - clickhouse lands on `r9gd.large` (2 vCPU / 16 GiB) for the same reason
      as kafka-broker.
    - keeper now lands on `m9g.large` too: its IOPS demand tracks the
      resolved ingest instead of a flat target independent of it -- parts a
      second (ingest over the loader's flush size) times the measured
      appends a part (sizing.yaml's keeper.raft_appends_per_part), floored
      at floors.keeper_idle_iops for what an idle quorum still asks for. At
      no estimate that is 100 IOPS plus root's 300 -- 400 against large's
      3,600 baseline, comfortably under, so the xlarge/2xlarge step-up the
      old flat 6,000-IOPS target used to force here is gone.
    - general and ci-burst are unchanged: both declare a `size:` floor
      (`xlarge`, `4xlarge`) above what root's demand ever forced, so they
      were never inflated by this bug in the first place.

    Verified against the fixture catalogue: total compute S, the shape table
    reading eks-system XS, general S, kafka-broker S, kraft-controller S,
    keeper S, clickhouse M and ci-burst M. Both earlier gates cut this floor on
    the same counting error one level up: a figure standing in for a demand
    that was never actually tied to what the deployment carries. The dial asks
    for a separate controller pool because the controller line is one of the
    seven above, and a combined quorum sizes no controller shape at all.
    """
    _run(_dial(tmp_path, estimate=None, controller_pool="separate"), tmp_path, cloud="aws")
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    rows = re.findall(r"\| \*\*total compute\*\* \| .*?\*\*([A-Z]{1,2})\*\* \|", report)
    assert rows, report
    # The verified honest floor, not a hint: every line's bucket, so a
    # regression that moves any one of them shows up as a failing assertion
    # rather than a silent pass inside a total.
    assert rows[0] == "S", report
    assert dict(
        re.findall(r"^\| ([a-z-]+) \| `[\w.]+` \|.*\| ([A-Z]{1,2}) \|$", report, re.M)
    ) == {
        "eks-system": "XS",
        "general": "S",
        "kafka-broker": "S",
        "kraft-controller": "S",
        "clickhouse": "M",
        "keeper": "S",
        "ci-burst": "M",
    }, report

    doc = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
    shapes = {name: body["instance_types"][0] for name, body in doc["resolved_shapes"].items()}
    assert shapes["kafka-broker"] == "m9g.large", shapes
    assert shapes["kraft-controller"] == "m9g.medium", shapes
    assert shapes["clickhouse"] == "r9gd.large", shapes
    # Keeper's IOPS demand now scales with ingest: at the floor that is the
    # idle floor (100) plus root's 300, comfortably under m9g.large's 3,600
    # baseline, so nothing forces a step past compute-shapes.yaml's own
    # keeper floor size (large -- 2 vCPU / 8 GiB, above the 1 vCPU / 3 GiB
    # sizing.yaml itself would ask for).
    assert shapes["keeper"] == "m9g.large", shapes


# ---------------------------------------------------------------------------
# P2-4 -- the instance-store cache size is decimal GB at the read, GiB
# everywhere else, and the two must not be mixed
# ---------------------------------------------------------------------------


def test_the_instance_store_cache_converts_gb_to_gib(catalogue) -> None:
    """InstanceStorageInfo.TotalSizeInGB is AWS's own decimal GB. A 950 GB
    device is an 884 GiB device, and the resolver's own `size_gib` field name
    promises GiB -- copying the GB number in unconverted told ClickHouse's
    filesystem cache it had 7.5% more room than the device holds."""
    entry = {
        "volumes": {
            "cache": {
                "type": "nvme-instance-store",
                "size_gib": "",
                "size_formula": "instance-provided",
                "iops": "",
                "throughput_mib_s": "",
            }
        }
    }
    chosen = resolve_sizing.InstanceType(
        name="r9gd.2xlarge",
        family="r",
        generation=9,
        modifiers="d",
        size="2xlarge",
        vcpu=8,
        memory_gib=64.0,
        baseline_iops=12000,
        baseline_throughput_mib_s=375.0,
        maximum_iops=48000,
        maximum_throughput_mib_s=1500.0,
        instance_store_gb=950,
        price_usd_hour=0.64072,
        physical_processor="AWS Graviton5 Processor",
    )
    demand = resolve_sizing.Node("clickhouse", 3, 8, 64, 0, 0, 0, "test")
    volumes = resolve_sizing._resolve_volumes(
        entry, demand, chosen, ROOT_VOLUME_DEMAND_MIB_S, ROOT_VOLUME_DEMAND_IOPS
    )
    assert volumes["cache"]["size_gib"] == 884


# ---------------------------------------------------------------------------
# P2-6 -- a node-field override on a use case size_core derives no node for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("use_case", ["eks-system", "general", "ci-burst", "msk-broker", "toolbox"])
def test_a_node_override_on_a_nodeless_use_case_is_refused_by_name(
    tmp_path: Path, use_case: str, capsys
) -> None:
    """size_core derives a Node for only kafka-broker, kraft-controller,
    clickhouse and keeper -- a cpu/memory/disk_gb/replicas override for one of
    the other five use cases had nothing to replace and was silently dropped,
    absent from the report's own 'Overridden by the deployer' table."""
    extra = f"  overrides:\n    {use_case}:\n      cpu: 8"
    assert _run(_dial(tmp_path, extra=extra), tmp_path) == 1
    err = capsys.readouterr().err
    assert use_case in err
    assert "no derived node to override" in err


def test_a_shape_override_still_applies_to_a_nodeless_use_case(tmp_path: Path) -> None:
    """instance_type, iops and throughput_mibs act on the CHOICE, not a Node,
    so they still apply to eks-system, general, ci-burst, msk-broker and
    toolbox -- only the node-field overrides above are refused."""
    extra = "  overrides:\n    eks-system:\n      instance_type: m9g.2xlarge"
    assert _run(_dial(tmp_path, extra=extra), tmp_path) == 0
    doc = json.loads((tmp_path / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
    assert doc["resolved_shapes"]["eks-system"]["instance_types"][0] == "m9g.2xlarge"


# ---------------------------------------------------------------------------
# P2-7 -- network.az_count must reach the resolver
# ---------------------------------------------------------------------------


def test_az_count_reaches_the_catalogues_own_zone_slice(tmp_path: Path) -> None:
    """The catalogue fixture carries 3 zones; asking for 2 must slice down to
    2, not silently keep 3 -- proving the dial's az_count is read at all."""
    extra = "network:\n  az_count: 2"
    _run(_dial(tmp_path, extra=extra), tmp_path, cloud="aws")
    doc = json.loads((tmp_path / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8"))
    assert doc["_provenance"]["availability_zones"] == ["us-west-2a", "us-west-2b"]


def test_az_count_above_the_fixtures_own_zone_count_is_refused_by_name(tmp_path: Path, capsys) -> None:
    """The committed fixture carries 3 zones; a dial asking for 4 cannot be
    validated against a zone the fixture never captured."""
    extra = "network:\n  az_count: 4"
    assert _run(_dial(tmp_path, extra=extra), tmp_path, cloud="aws") == 1
    err = capsys.readouterr().err
    assert "fewer than the 4" in err


def test_az_count_out_of_the_tofu_roots_own_range_is_refused(tmp_path: Path, capsys) -> None:
    extra = "network:\n  az_count: 7"
    assert _run(_dial(tmp_path, extra=extra), tmp_path) == 1
    assert "network.az_count must be between 2 and 6" in capsys.readouterr().err


def test_the_broker_count_multiple_follows_az_count(tmp_path: Path) -> None:
    """A deployment spread over more zones than the floor's 3 brokers gets a
    broker count that actually spreads one-per-zone, not the floor's own
    multiple regardless of how many zones the VPC spans."""
    extra = "network:\n  az_count: 5"
    _run(_dial(tmp_path, cloud="onprem", extra=extra), tmp_path, cloud="onprem")
    doc = json.loads((tmp_path / "sizing" / "scale.nodes.json").read_text(encoding="utf-8"))
    assert doc["kafka-broker"]["count"] == 5


# ---------------------------------------------------------------------------
# P2-10 -- a fallback instance type must never be smaller than the chosen one
# ---------------------------------------------------------------------------


def test_fallbacks_never_undercut_the_volume_step_up(tmp_path: Path) -> None:
    """select_shape's fallback ladder used to list the smallest type PER
    GENERATION that met the raw demand, built BEFORE the volume step-up
    replaced the chosen type with a bigger one -- so EKS's own capacity
    fallback could silently halve the node the step-up existed to avoid. Every
    fallback in the committed answer must carry at least the chosen type's own
    vCPU and memory."""
    _run(_dial(tmp_path), tmp_path, cloud="aws")
    doc = json.loads((tmp_path / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8"))
    live_catalogue = resolve_sizing.fetch_fixtures(FIXTURES, "us-west-2")
    for use_case, entry in doc.items():
        if use_case.startswith("_") or use_case == "msk-broker":
            continue
        chosen_name, *fallbacks = entry["instance_types"]
        chosen_type = live_catalogue.types[chosen_name]
        for name in fallbacks:
            candidate = live_catalogue.types[name]
            assert candidate.vcpu >= chosen_type.vcpu, (use_case, name)
            assert candidate.memory_gib >= chosen_type.memory_gib, (use_case, name)

    # The direct unit-level proof: a family whose older generation's smallest
    # size is BELOW the volume-stepped-up chosen type must not appear.
    catalogue_obj = resolve_sizing.fetch_fixtures(FIXTURES, "us-west-2")
    entry = {
        "family": "m",
        "modifiers": "",
        "generation_policy": "newest",
        "generation_pin": "",
        "price_policy": "newest",
        "price_step_max_pct": "",
        "price_generations": "",
        "size": "large",
        "volumes": {
            "root": {
                "type": "gp3",
                "size_gib": "40",
                "size_formula": "fixed",
                "iops": "3000",
                "throughput_mib_s": "125",
                "throughput_policy": "instance-baseline",
            },
            "data": {
                "type": "gp3",
                "size_gib": "",
                "size_formula": "fixed",
                "iops": "3000",
                "throughput_mib_s": "500",
            },
        },
    }
    demand = resolve_sizing.Node("kafka-broker", 3, 2, 8, 40, 0, 0, "test")
    choice = resolve_sizing.select_shape(
        "kafka-broker",
        entry,
        demand,
        catalogue_obj,
        "newest",
        [],
        ROOT_VOLUME_DEMAND_MIB_S,
        ROOT_VOLUME_DEMAND_IOPS,
    )
    for name in choice.fallbacks:
        candidate = catalogue_obj.types[name]
        assert candidate.vcpu >= choice.vcpu, (name, candidate.vcpu, choice.vcpu)
        assert candidate.memory_gib >= choice.memory_gib, (name, candidate.memory_gib, choice.memory_gib)


# ---------------------------------------------------------------------------
# P2-5 -- --migrate accepts the locked diff, never the caps
# ---------------------------------------------------------------------------


def test_migrate_still_fails_on_a_fatal_assertion(tmp_path: Path, capsys) -> None:
    """A locked-field change plus a fatal A1 (an override past the size-derived
    IOPS ceiling) must exit 1 even with --migrate -- migrating the locked diff
    is not the same thing as the caps having passed."""
    first = tmp_path / "first"
    first.mkdir()
    _run(_dial(first, provider="strimzi", estimate=1000), first)
    previous = first / "sizing" / "resolved.yaml"

    second = tmp_path / "second"
    second.mkdir()
    extra = "  overrides:\n    keeper:\n      iops: 60000"
    dial = _dial(second, provider="msk", estimate=1000, extra=extra)
    status = _run(dial, second, previous=previous, migrate=True)

    assert status == 1
    err = capsys.readouterr().err
    assert "MIGRATING msk_broker_type" in err
    assert "A1 keeper" in err


# ---------------------------------------------------------------------------
# P3 -- the dead A6 check, checked against the shape entry it can actually see
# ---------------------------------------------------------------------------


def test_a6_catches_iops_per_gb_on_the_raw_shape_entry(catalogue) -> None:
    """_resolve_volumes only ever copies type/size_gib/iops/throughput_mib_s/
    demand_iops/demand_throughput_mib_s/source into the built volume dict, so
    a check against THAT dict can never see iopsPerGB or
    allowAutoIOPSPerGBIncrease -- checked against the shape entry
    compute-shapes.yaml declares instead, which is where a future mistaken
    field name would actually appear."""
    choice = _choice(
        volumes={"data": {"type": "gp3", "size_gib": 500, "iops": 3000, "throughput_mib_s": 500}}
    )
    entry = {"volumes": {"data": {"iopsPerGB": "50"}}}
    findings = resolve_sizing.assert_caps(choice, entry, catalogue, 0, gp3_usd_per_gib_month=0.08)
    assert any(f.rule == "A6" and "iopsPerGB" in f.message for f in findings)


# ---------------------------------------------------------------------------
# P2-12 -- k8s.cloud: local maps onto the onprem shape key
# ---------------------------------------------------------------------------


def test_k8s_cloud_local_resolves_as_onprem(tmp_path: Path) -> None:
    """compute-shapes.yaml has no `local` key, only `onprem` -- deployment
    .example.yaml's own default (k8s.cloud: local) failed the resolve outright
    unless the operator knew to pass --cloud onprem by hand."""
    dial = _dial(tmp_path, cloud="local")
    dial_obj = resolve_sizing.read_dial(dial)
    assert dial_obj.cloud == "onprem"
