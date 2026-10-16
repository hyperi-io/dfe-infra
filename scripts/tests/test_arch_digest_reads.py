#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/tests/test_arch_digest_reads.py
#  Purpose:      Prove the multi-arch gate reads a chart-run image by tag@sha256
#                wherever the stack or the chart pins its digest, reads it by tag
#                where nothing does, and recalls every digest-pinned read.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The digest read paths of scripts/check_image_arch.py, for karpenter and the AWS
load balancer controller.

A fake registry answers helm and docker: `helm pull` unpacks a chart written from
the test's own Chart.yaml and values, `helm show chart` prints that Chart.yaml,
and every image is multi-arch. The stack is handed to main() in place of
versions.yaml, so the real reads, verdicts and cache file all run.

    python3 -m pytest scripts/tests/test_arch_digest_reads.py
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_image_arch as arch  # noqa: E402

pins = arch.registry_pins

BOTH = (
    '{"schemaVersion": 2, "manifests": ['
    '{"platform": {"os": "linux", "architecture": "amd64"}},'
    '{"platform": {"os": "linux", "architecture": "arm64"}}]}'
)
KARPENTER_CHART = "oci://public.ecr.aws/karpenter/karpenter"
CHART_DIGEST = "sha256:" + "c" * 64
CONTROLLER_DIGEST = "sha256:" + "d" * 64
CONTROLLER = f"public.ecr.aws/karpenter/controller:1.14.1@{CONTROLLER_DIGEST}"
KARPENTER_VALUES = (
    "controller:\n"
    "  image:\n"
    "    repository: public.ecr.aws/karpenter/controller\n"
    "    tag: 1.14.1\n"
    f"    digest: {CONTROLLER_DIGEST}\n"
)
ALB = "public.ecr.aws/eks/aws-load-balancer-controller"
ALB_DIGEST = "sha256:" + "e" * 64


class Registry:
    """helm and docker, answered from the charts a test declares; every image is multi-arch."""

    def __init__(self, charts: dict[str, tuple[str, str]]) -> None:
        self.charts = charts
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], *_args: object, **_kwargs: object):
        self.calls.append(cmd)
        if cmd[0] == "docker":
            return subprocess.CompletedProcess(cmd, 0, BOTH, "")
        source = cmd[3] if cmd[1] == "show" else cmd[2]
        if "--version" in cmd:
            source += " " + cmd[cmd.index("--version") + 1]
        chart_yaml, values = self.charts[source]
        if cmd[1] == "show":
            return subprocess.CompletedProcess(cmd, 0, chart_yaml, "")
        into = Path(cmd[cmd.index("--untardir") + 1])
        chart = into / "chart"
        chart.mkdir()
        (chart / "Chart.yaml").write_text(chart_yaml, encoding="utf-8")
        (chart / "values.yaml").write_text(values, encoding="utf-8")
        if "@" in source:
            # What helm 4.1.1 leaves beside the chart when it pulls by digest.
            (into / f"chart@{source.rsplit('@', 1)[1]}").mkdir()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def images_read(self) -> list[str]:
        return [cmd[4] for cmd in self.calls if cmd[0] == "docker"]

    def charts_read(self) -> list[list[str]]:
        return [cmd for cmd in self.calls if cmd[0] == "helm"]


def karpenter(version: str = "1.14.1") -> tuple[str, str]:
    return f"name: karpenter\nversion: {version}\nappVersion: {version}\n", KARPENTER_VALUES


def alb_chart() -> tuple[str, str]:
    return "name: aws-load-balancer-controller\nversion: 3.6.0\nappVersion: v3.6.0\n", ""


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pins, "_sleep", lambda _wait: None)


def run_gate(
    monkeypatch: pytest.MonkeyPatch, registry: Registry, stack: dict, cache: Path
) -> int:
    monkeypatch.setattr(subprocess, "run", registry)
    monkeypatch.setattr(arch.dfe_stack, "load_root", dict)
    monkeypatch.setattr(arch.dfe_stack, "stack_pins", lambda _root, _name: ("test", stack))
    argv = ["check_image_arch.py", "--third-party-only", "--resolve-charts"]
    monkeypatch.setattr(sys, "argv", [*argv, "--verdict-cache", str(cache)])
    return arch.main()


# ------------------------------------------------------------------ karpenter


def test_the_karpenter_controller_is_read_at_the_digest_its_chart_pins(
    monkeypatch, tmp_path
) -> None:
    registry = Registry({f"{KARPENTER_CHART} 1.14.1": karpenter()})

    status = run_gate(monkeypatch, registry, {"operators": {"karpenter": "1.14.1"}}, tmp_path / "v")

    assert status == 0
    assert CONTROLLER in registry.images_read()
    assert "public.ecr.aws/karpenter/controller:1.14.1" not in registry.images_read()
    pulls = registry.charts_read()
    assert [cmd[:5] for cmd in pulls] == [["helm", "pull", KARPENTER_CHART, "--version", "1.14.1"]]


def test_a_chart_pinned_by_version_is_read_every_run_and_its_controller_once(
    monkeypatch, tmp_path, capsys
) -> None:
    stack = {"operators": {"karpenter": "1.14.1"}}
    cache = tmp_path / "verdicts.json"
    run_gate(monkeypatch, Registry({f"{KARPENTER_CHART} 1.14.1": karpenter()}), stack, cache)
    capsys.readouterr()

    second = Registry({f"{KARPENTER_CHART} 1.14.1": karpenter()})
    assert run_gate(monkeypatch, second, stack, cache) == 0

    assert CONTROLLER not in second.images_read()
    assert len(second.charts_read()) == 1
    assert json.loads(cache.read_text())["charts"] == {}
    assert "charts at a digest: 0 hit, 0 miss" in capsys.readouterr().out


def test_a_chart_pinned_by_digest_is_read_once_and_needs_no_read_after(
    monkeypatch, tmp_path, capsys
) -> None:
    stack = {
        "operators": {"karpenter": "1.14.1"},
        "services-digests": {"karpenter": CHART_DIGEST},
    }
    cache = tmp_path / "verdicts.json"
    first = Registry({f"{KARPENTER_CHART}@{CHART_DIGEST}": karpenter()})
    assert run_gate(monkeypatch, first, stack, cache) == 0
    assert [cmd[:3] for cmd in first.charts_read()] == [
        ["helm", "pull", f"{KARPENTER_CHART}@{CHART_DIGEST}"]
    ]
    assert "--version" not in first.charts_read()[0]
    capsys.readouterr()

    second = Registry({})
    assert run_gate(monkeypatch, second, stack, cache) == 0

    out = capsys.readouterr().out
    assert second.charts_read() == []
    assert CONTROLLER not in second.images_read()
    assert "charts at a digest: 1 hit, 0 miss" in out
    assert "public.ecr.aws" not in out.rsplit("registry reads:", 1)[1].splitlines()[0]
    recorded = json.loads(cache.read_text())["charts"][f"{KARPENTER_CHART}@{CHART_DIGEST}"]
    assert recorded == {"version": "1.14.1", "images": {"karpenter controller": CONTROLLER}}


def test_a_chart_digest_of_another_version_fails_the_gate(monkeypatch, tmp_path, capsys) -> None:
    stack = {
        "operators": {"karpenter": "1.14.1"},
        "services-digests": {"karpenter": CHART_DIGEST},
    }
    registry = Registry({f"{KARPENTER_CHART}@{CHART_DIGEST}": karpenter("1.15.0")})

    assert run_gate(monkeypatch, registry, stack, tmp_path / "v") == 1

    out = capsys.readouterr().out
    assert f"{KARPENTER_CHART}@{CHART_DIGEST} is chart version 1.15.0, not the pinned 1.14.1" in out
    assert json.loads((tmp_path / "v").read_text())["charts"] == {}


def test_a_recalled_chart_is_read_again_when_the_pin_names_another_version(
    monkeypatch, tmp_path
) -> None:
    source = f"{KARPENTER_CHART}@{CHART_DIGEST}"
    cache = tmp_path / "verdicts.json"
    record = {"version": "1.13.0", "images": {"karpenter controller": CONTROLLER}}
    cache.write_text(json.dumps({"schema": 1, "images": {}, "charts": {source: record}}))
    stack = {
        "operators": {"karpenter": "1.14.1"},
        "services-digests": {"karpenter": CHART_DIGEST},
    }
    registry = Registry({source: karpenter()})

    assert run_gate(monkeypatch, registry, stack, cache) == 0

    assert len(registry.charts_read()) == 1


def test_a_digest_on_a_chart_from_a_classic_repository_fails_the_gate(
    monkeypatch, tmp_path, capsys
) -> None:
    stack = {
        "operators": {"aws-load-balancer-controller": "3.6.0"},
        "services-digests": {"aws-load-balancer-controller": CHART_DIGEST},
    }
    registry = Registry({"aws-load-balancer-controller 3.6.0": alb_chart()})

    assert run_gate(monkeypatch, registry, stack, tmp_path / "v") == 1

    assert "comes from a classic repository, which serves no digest" in capsys.readouterr().out


# ------------------------------------------------- aws-load-balancer-controller


def test_the_load_balancer_controller_is_read_by_tag_when_nothing_pins_its_digest(
    monkeypatch, tmp_path
) -> None:
    stack = {"operators": {"aws-load-balancer-controller": "3.6.0"}}
    registry = Registry({"aws-load-balancer-controller 3.6.0": alb_chart()})

    assert run_gate(monkeypatch, registry, stack, tmp_path / "v") == 0

    assert f"{ALB}:v3.6.0" in registry.images_read()


def test_the_load_balancer_controller_is_read_by_the_digest_the_stack_pins(
    monkeypatch, tmp_path, capsys
) -> None:
    stack = {
        "operators": {"aws-load-balancer-controller": "3.6.0"},
        "services": {"aws-load-balancer-controller-image": "v3.6.0"},
        "services-digests": {"aws-load-balancer-controller-image": ALB_DIGEST},
    }
    cache = tmp_path / "verdicts.json"
    first = Registry({"aws-load-balancer-controller 3.6.0": alb_chart()})
    assert run_gate(monkeypatch, first, stack, cache) == 0
    assert f"{ALB}:v3.6.0@{ALB_DIGEST}" in first.images_read()
    assert f"{ALB}:v3.6.0" not in first.images_read()
    capsys.readouterr()

    second = Registry({"aws-load-balancer-controller 3.6.0": alb_chart()})
    assert run_gate(monkeypatch, second, stack, cache) == 0

    assert not [ref for ref in second.images_read() if ref.startswith(ALB)]
    out = capsys.readouterr().out
    assert f"  ok     services.aws-load-balancer-controller-image {ALB}:v3.6.0@{ALB_DIGEST}" in out


def test_a_pinned_tag_the_chart_does_not_render_fails_the_gate(
    monkeypatch, tmp_path, capsys
) -> None:
    stack = {
        "operators": {"aws-load-balancer-controller": "3.6.0"},
        "services": {"aws-load-balancer-controller-image": "v3.5.0"},
        "services-digests": {"aws-load-balancer-controller-image": ALB_DIGEST},
    }
    registry = Registry({"aws-load-balancer-controller 3.6.0": alb_chart()})

    assert run_gate(monkeypatch, registry, stack, tmp_path / "v") == 1

    out = capsys.readouterr().out
    assert f"pins {ALB}:v3.5.0, but operators.aws-load-balancer-controller chart 3.6.0" in out


def test_a_pinned_image_tag_with_no_digest_fails_the_gate(monkeypatch, tmp_path, capsys) -> None:
    stack = {
        "operators": {"aws-load-balancer-controller": "3.6.0"},
        "services": {"aws-load-balancer-controller-image": "v3.6.0"},
    }
    registry = Registry({"aws-load-balancer-controller 3.6.0": alb_chart()})

    assert run_gate(monkeypatch, registry, stack, tmp_path / "v") == 1

    out = capsys.readouterr().out
    assert "pinned with no services-digests.aws-load-balancer-controller-image" in out
    assert f"{ALB}:v3.6.0" not in registry.images_read()
