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
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sizing"
CATALOGUE = FIXTURES / "aws-catalogue-us-west-2.json"
GOLDEN = FIXTURES / "golden-matrix.json"

sys.path.insert(0, str(SCRIPTS))
import resolve_sizing  # noqa: E402

WRITE_GOLDEN = os.environ.get("RESOLVE_SIZING_GOLDEN") == "write"

# cloud x tier x focus x estimate band. slim and single carry fixed shapes and
# must be REFUSED; 150,000 GB/day is above the generator cap and must be refused
# too, with the professional-services message rather than an extrapolation.
BANDS = (None, 1000, 10000, 100000)
FOCUSES = ("economy", "balanced", "performance")
CLOUDS = ("aws", "onprem")


def _dial(
    tmp_path: Path,
    *,
    tier: str = "scale",
    cloud: str = "aws",
    focus: str = "economy",
    estimate: int | None = 10000,
    provider: str = "strimzi",
    extra: str = "",
) -> Path:
    """Write one deployment dial and answer its path."""
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
    tfvars = out / "sizing" / f"{tier}.auto.tfvars.json"
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
    return built


def test_the_matrix_matches_its_snapshots(matrix: dict[str, object]) -> None:
    """The deployment matrix IS the test matrix -- see the module docstring."""
    if WRITE_GOLDEN:
        GOLDEN.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip("golden matrix rewritten -- review the diff before committing it")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert matrix == expected


def test_every_case_in_the_matrix_produced_a_snapshot(matrix: dict[str, object]) -> None:
    assert sorted(matrix) == sorted([*(_case_name(*case) for case in _cases()), OVERRIDE_CASE])


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
    doc = json.loads((tmp_path / "sizing" / "scale.auto.tfvars.json").read_text(encoding="utf-8"))
    assert doc["node_pools"]["kafka-broker"]["desired_size"] == 3


def test_the_broker_count_grows_with_the_estimate(tmp_path: Path) -> None:
    """Cookie-cutter scale-out: past the per-broker vCPU cap the cluster grows by broker."""
    counts = []
    for band in (1000, 100000):
        out = tmp_path / str(band)
        out.mkdir()
        _run(_dial(out, estimate=band), out)
        doc = json.loads((out / "sizing" / "scale.auto.tfvars.json").read_text(encoding="utf-8"))
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
        doc = json.loads((out / "sizing" / "scale.auto.tfvars.json").read_text(encoding="utf-8"))
        brokers = doc["node_pools"]["kafka-broker"]["desired_size"]
        assert partitions % brokers == 0, (band, partitions, brokers)


def test_economy_never_trades_the_durability_floor(tmp_path: Path) -> None:
    """The cheapest deployment still runs three brokers, three replicas, three controllers."""
    _run(_dial(tmp_path, focus="economy", estimate=None), tmp_path)
    doc = json.loads((tmp_path / "sizing" / "scale.auto.tfvars.json").read_text(encoding="utf-8"))
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


def test_a3_catches_volumes_that_sum_past_the_instance_baseline(catalogue) -> None:
    """The attached instance caps every volume behind it, and says nothing."""
    choice = _choice(
        baseline_iops=12000,
        maximum_iops=12000,
        volumes={
            "root": {"type": "gp3", "size_gib": 40, "iops": 3000, "throughput_mib_s": 125},
            "data": {"type": "gp3", "size_gib": 2000, "iops": 20000, "throughput_mib_s": 500},
        },
    )
    findings = resolve_sizing.assert_caps(choice, {}, catalogue, 0, gp3_usd_per_gib_month=0.08)
    assert any(f.rule == "A3" for f in findings)
    assert any("the instance is the ceiling" in f.message for f in findings)


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
    """A different region's gp3 price must move the same warning threshold."""
    choice = _choice(
        volumes={"data": {"type": "gp3", "size_gib": 100000, "iops": 3000, "throughput_mib_s": 500}}
    )
    cheap = resolve_sizing.assert_caps(choice, {}, catalogue, 1, gp3_usd_per_gib_month=0.01)
    dear = resolve_sizing.assert_caps(choice, {}, catalogue, 1, gp3_usd_per_gib_month=1.00)
    cheap_a5 = next(f for f in cheap if f.rule == "A5")
    dear_a5 = next(f for f in dear if f.rule == "A5")
    assert cheap_a5.message != dear_a5.message


def test_gp3_price_per_gib_month_reads_the_regions_own_value() -> None:
    shapes = resolve_sizing._load(resolve_sizing.SHAPES_FILE)
    assert resolve_sizing._gp3_price_per_gib_month(shapes, "aws", "us-west-2") == 0.08


def test_a_regions_missing_storage_price_is_refused_by_name() -> None:
    """The estimator refuses to borrow another region's gp3 rate silently."""
    shapes = resolve_sizing._load(resolve_sizing.SHAPES_FILE)
    with pytest.raises(resolve_sizing.ResolveError, match="ap-southeast-2"):
        resolve_sizing._gp3_price_per_gib_month(shapes, "aws", "ap-southeast-2")


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
        resolve_sizing.select_shape("general", entry, demand, stripped, "newest", [])


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
        resolve_sizing.select_shape("general", {"family": "m", "size": "large"}, demand, broken, "newest", [])


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
    assert doc["kafka-broker"]["price_usd_hour"] > 0


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
    doc = json.loads((tmp_path / "sizing" / "scale.auto.tfvars.json").read_text(encoding="utf-8"))
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
    doc = json.loads((tmp_path / "sizing" / "scale.auto.tfvars.json").read_text(encoding="utf-8"))
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
        if not stripped or stripped.startswith(("##", "- ")):
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
    doc = json.loads((tmp_path / "sizing" / "scale.auto.tfvars.json").read_text(encoding="utf-8"))
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


def test_the_msk_path_resolves_a_broker_shape_with_its_price(tmp_path: Path) -> None:
    _run(_dial(tmp_path, provider="msk", estimate=1000), tmp_path)
    doc = json.loads((tmp_path / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8"))
    entry = doc["msk-broker"]
    assert entry["instance_types"][0].startswith("express.m7g.")
    assert entry["price_usd_hour"] > 0


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
    doc = json.loads((tmp_path / "sizing" / "scale.auto.tfvars.json").read_text(encoding="utf-8"))
    assert "msk-broker" not in doc["node_pools"]
    assert "msk-broker" in doc["resolved_shapes"]


def test_the_msk_path_sizes_no_broker_pvc_or_controller_pool(tmp_path: Path) -> None:
    """Express manages its own storage and runs its own metadata quorum."""
    _run(_dial(tmp_path, provider="msk", estimate=1000), tmp_path)
    doc = json.loads((tmp_path / "sizing" / "scale.auto.tfvars.json").read_text(encoding="utf-8"))
    assert "kraft-controller" not in doc["resolved_shapes"]
    values = (tmp_path / "sizing" / "scale.values.yaml").read_text(encoding="utf-8")
    assert "replicas" not in values.split("clickhouse:")[0]


# ---------------------------------------------------------------------------
# Sizing up, rather than reporting a volume the instance cannot carry
# ---------------------------------------------------------------------------


def test_the_instance_steps_up_until_it_sustains_its_own_volumes(tmp_path: Path) -> None:
    """A3 fires only when no size in the family can carry the profile."""
    _run(_dial(tmp_path), tmp_path)
    doc = json.loads((tmp_path / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8"))
    for use_case, entry in doc.items():
        if use_case.startswith("_") or use_case == "msk-broker":
            continue
        volumes = [v for v in entry["volumes"].values() if v.get("type") == "gp3"]
        assert sum(v["iops"] for v in volumes) <= entry["ceilings"]["baseline_iops"], use_case
        assert (
            sum(v["throughput_mib_s"] for v in volumes)
            <= entry["ceilings"]["baseline_throughput_mib_s"]
        ), use_case


def test_the_step_up_is_reported_with_its_price_delta(tmp_path: Path) -> None:
    _run(_dial(tmp_path), tmp_path)
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    assert "stepped up from" in report
    assert "an hour a node" in report


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
    """storage_model and controller_mode are deployer-set, never derived here."""
    sizing = {"locked": {"storage_model": "a migration, not a setting", "cloud_token": "a new deployment"}}
    previous = {"locked": {"cloud_token": "aws"}}
    resolved = {"locked": {"cloud_token": "gcp"}}
    changes = resolve_sizing.find_locked_changes(sizing, previous, resolved)
    assert [c.field for c in changes] == ["cloud_token"]
    assert changes[0].old == "aws"
    assert changes[0].new == "gcp"
    assert changes[0].reason == "a new deployment"


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
    assert (second / "sizing" / "scale.auto.tfvars.json").is_file()
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
    doc = json.loads((tmp_path / "sizing" / "scale.auto.tfvars.json").read_text(encoding="utf-8"))
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
