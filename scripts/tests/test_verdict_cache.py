#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/tests/test_verdict_cache.py
#  Purpose:      Prove the multi-arch gate reads a digest-pinned image once and
#                recalls its verdict after that, and that it still reads every
#                image a cache cannot vouch for.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The verdict cache behind scripts/check_image_arch.py --verdict-cache.

A fake registry stands in for docker and helm, as in test_registry_retry.py:
`subprocess.run` is the only seam faked, so the gate's own reads, its verdicts and
the file it writes all run for real. A call past the end of a tool's script fails
the test, which is how a test proves a reference was NOT read.

    python3 -m pytest scripts/tests/test_verdict_cache.py
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
# A single-platform manifest lists no platforms, so its config is read for the one it has.
SINGLE = '{"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json"}'
DIGEST = "sha256:" + "a" * 64
PINNED = f"public.ecr.aws/eks/aws-load-balancer-controller:v3.6.0@{DIGEST}"
OTHER_PINNED = f"docker.io/library/busybox:1.38.0@sha256:{'b' * 64}"
BY_TAG = "public.ecr.aws/karpenter/controller:1.14.1"
KARPENTER = "oci://public.ecr.aws/karpenter/karpenter"
ECR_429 = "ERROR: toomanyrequests: Data limit exceeded"


class FakeRegistry:
    """Answers docker and helm from a script per tool and records every call."""

    def __init__(self, **scripts: list[tuple[int, str, str]]) -> None:
        self.scripts = {tool: list(entries) for tool, entries in scripts.items()}
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], *_args: object, **_kwargs: object):
        self.calls.append(cmd)
        script = self.scripts.get(cmd[0], [])
        assert script, f"{cmd[0]} was called more often than scripted: {cmd}"
        returncode, stdout, stderr = script.pop(0)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    def read(self, ref: str) -> bool:
        """Whether any docker call read this reference."""
        return any(cmd[0] == "docker" and ref in cmd for cmd in self.calls)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pins, "_sleep", lambda _wait: None)
    monkeypatch.setattr(pins, "_random", lambda: 0.5)


def run_gate(
    monkeypatch: pytest.MonkeyPatch,
    registry: FakeRegistry,
    cache: Path | None,
    *refs: str,
    seed: Path | None = None,
) -> int:
    monkeypatch.setattr(subprocess, "run", registry)
    argv = ["check_image_arch.py"]
    for ref in refs:
        argv += ["--image", ref]
    if cache is not None:
        argv += ["--verdict-cache", str(cache)]
    if seed is not None:
        argv += ["--seed", str(seed)]
    monkeypatch.setattr(sys, "argv", argv)
    return arch.main()


def recorded(cache: Path) -> dict[str, list[str]]:
    return json.loads(cache.read_text(encoding="utf-8"))["images"]


def test_a_digest_pinned_image_is_read_once_and_recalled_after(
    monkeypatch, tmp_path, capsys
) -> None:
    cache = tmp_path / "verdicts.json"
    first = FakeRegistry(docker=[(0, BOTH, "")])
    assert run_gate(monkeypatch, first, cache, PINNED) == 0
    assert first.read(PINNED)
    assert recorded(cache) == {PINNED: ["linux/amd64", "linux/arm64"]}
    capsys.readouterr()

    second = FakeRegistry()
    assert run_gate(monkeypatch, second, cache, PINNED) == 0

    out = capsys.readouterr().out
    assert second.calls == []
    assert f"  ok     {PINNED} {PINNED} (proven by an earlier run)" in out
    assert "verdict cache: 1 hit, 0 miss, 0 by tag (never cached); " in out
    assert "; registry reads: 0 (none)" in out
    assert recorded(cache) == {PINNED: ["linux/amd64", "linux/arm64"]}


def test_an_image_pinned_by_tag_alone_is_read_every_run_and_never_recorded(
    monkeypatch, tmp_path, capsys
) -> None:
    cache = tmp_path / "verdicts.json"
    for _ in range(2):
        registry = FakeRegistry(docker=[(0, BOTH, "")])
        assert run_gate(monkeypatch, registry, cache, BY_TAG) == 0
        assert registry.read(BY_TAG)
        assert recorded(cache) == {}

    out = capsys.readouterr().out
    assert "verdict cache: 0 hit, 0 miss, 1 by tag (never cached); " in out
    assert "; registry reads: 1 (public.ecr.aws=1)" in out


def test_a_cache_miss_reads_the_registry_for_real(monkeypatch, tmp_path, capsys) -> None:
    cache = tmp_path / "verdicts.json"
    images = {OTHER_PINNED: ["linux/amd64", "linux/arm64"]}
    cache.write_text(json.dumps({"schema": 1, "images": images}))
    registry = FakeRegistry(docker=[(0, BOTH, "")])

    assert run_gate(monkeypatch, registry, cache, PINNED, OTHER_PINNED) == 0

    assert registry.read(PINNED)
    assert not registry.read(OTHER_PINNED)
    assert set(recorded(cache)) == {PINNED, OTHER_PINNED}
    assert "verdict cache: 1 hit, 1 miss, 0 by tag" in capsys.readouterr().out


def test_a_record_missing_a_required_platform_is_read_again(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "verdicts.json"
    cache.write_text(json.dumps({"schema": 1, "images": {PINNED: ["linux/amd64"]}}))
    registry = FakeRegistry(docker=[(0, BOTH, "")])

    assert run_gate(monkeypatch, registry, cache, PINNED) == 0

    assert registry.read(PINNED)
    assert recorded(cache) == {PINNED: ["linux/amd64", "linux/arm64"]}


def test_a_failed_verdict_is_not_recorded_and_the_proven_ones_still_are(
    monkeypatch, tmp_path, capsys
) -> None:
    cache = tmp_path / "verdicts.json"
    registry = FakeRegistry(
        docker=[
            (0, BOTH, ""),
            (0, SINGLE, ""),
            (0, '{"os": "linux", "architecture": "amd64"}', ""),
        ]
    )

    assert run_gate(monkeypatch, registry, cache, PINNED, OTHER_PINNED) == 1

    assert recorded(cache) == {PINNED: ["linux/amd64", "linux/arm64"]}
    out = capsys.readouterr().out
    assert f"FAIL   {OTHER_PINNED} {OTHER_PINNED}: missing ['linux/arm64']" in out


def test_a_rate_limited_read_is_not_recorded(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "verdicts.json"
    registry = FakeRegistry(docker=[(1, "", ECR_429)] * 5)

    assert run_gate(monkeypatch, registry, cache, PINNED) == 1

    assert recorded(cache) == {}


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        json.dumps({"schema": 99, "images": {PINNED: ["linux/amd64", "linux/arm64"]}}),
        json.dumps({"schema": 1, "images": [PINNED]}),
        json.dumps([PINNED]),
    ],
)
def test_a_cache_it_cannot_parse_vouches_for_nothing(monkeypatch, tmp_path, capsys, text) -> None:
    cache = tmp_path / "verdicts.json"
    cache.write_text(text)
    registry = FakeRegistry(docker=[(0, BOTH, "")])

    assert run_gate(monkeypatch, registry, cache, PINNED) == 0

    assert registry.read(PINNED)
    assert "every reference is read" in capsys.readouterr().out
    assert recorded(cache) == {PINNED: ["linux/amd64", "linux/arm64"]}


def test_no_cache_file_yet_reads_everything_and_writes_one(monkeypatch, tmp_path, capsys) -> None:
    cache = tmp_path / "restored" / "verdicts.json"
    registry = FakeRegistry(docker=[(0, BOTH, "")])

    assert run_gate(monkeypatch, registry, cache, PINNED) == 0

    assert f"no verdict cache at {cache}" in capsys.readouterr().out
    assert recorded(cache) == {PINNED: ["linux/amd64", "linux/arm64"]}


def test_a_record_under_a_tag_is_ignored_even_when_the_file_carries_one(
    monkeypatch, tmp_path
) -> None:
    """Only a digest names fixed content, so a tag entry in the file proves nothing."""
    cache = tmp_path / "verdicts.json"
    cache.write_text(json.dumps({"schema": 1, "images": {BY_TAG: ["linux/amd64", "linux/arm64"]}}))
    registry = FakeRegistry(docker=[(0, BOTH, "")])

    assert run_gate(monkeypatch, registry, cache, BY_TAG) == 0

    assert registry.read(BY_TAG)
    assert recorded(cache) == {}


def test_without_the_flag_nothing_is_recalled_or_written(monkeypatch, tmp_path, capsys) -> None:
    registry = FakeRegistry(docker=[(0, BOTH, "")])

    assert run_gate(monkeypatch, registry, None, PINNED) == 0

    assert registry.read(PINNED)
    assert list(tmp_path.iterdir()) == []
    assert "verdict cache: 0 hit, 1 miss, 0 by tag" in capsys.readouterr().out


def test_the_read_count_names_each_registry_and_counts_retries_and_chart_reads(monkeypatch) -> None:
    registry = FakeRegistry(
        docker=[(1, "", ECR_429), (0, BOTH, "")],
        helm=[(0, "name: karpenter\nappVersion: 1.14.1\n", "")],
    )
    monkeypatch.setattr(subprocess, "run", registry)
    budget = pins.RetryBudget(arch.RETRY_BUDGET_SECONDS)

    arch.chart_app_version(KARPENTER, None, "1.14.1", budget)
    pins.ref_platforms(OTHER_PINNED, budget)

    assert budget.reads == {"public.ecr.aws": 1, "docker.io": 2}
    line = arch.cache_summary(arch.VerdictCache(), budget.reads)
    assert line.endswith("registry reads: 3 (docker.io=2, public.ecr.aws=1)")


@pytest.mark.parametrize(
    ("ref", "pinned"),
    [
        (PINNED, True),
        (f"ghcr.io/x/y@{DIGEST}", True),
        (BY_TAG, False),
        ("ghcr.io/x/y:v1@sha256:abc", False),
        (f"ghcr.io/x/y:v1@sha512:{'a' * 64}", False),
    ],
)
def test_only_a_full_sha256_digest_counts_as_pinned(ref: str, pinned: bool) -> None:
    assert arch.is_digest_pinned(ref) is pinned


def write_seed(path: Path, images: dict[str, list[str]]) -> Path:
    doc = {"schema": arch.VERDICT_CACHE_SCHEMA, "images": images, "charts": {}}
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_a_seeded_digest_is_not_read_even_with_no_cache_yet(monkeypatch, tmp_path, capsys) -> None:
    seed = write_seed(tmp_path / "seed.json", {PINNED: ["linux/amd64", "linux/arm64"]})
    before = seed.read_text(encoding="utf-8")
    registry = FakeRegistry()

    assert run_gate(monkeypatch, registry, tmp_path / "verdicts.json", PINNED, seed=seed) == 0

    out = capsys.readouterr().out
    assert registry.calls == []
    assert f"  ok     {PINNED} {PINNED} (proven by an earlier run)" in out
    assert recorded(tmp_path / "verdicts.json") == {PINNED: ["linux/amd64", "linux/arm64"]}
    assert seed.read_text(encoding="utf-8") == before


def test_a_seed_entry_under_a_tag_is_ignored_and_read(monkeypatch, tmp_path) -> None:
    seed = write_seed(tmp_path / "seed.json", {BY_TAG: ["linux/amd64", "linux/arm64"]})
    registry = FakeRegistry(docker=[(0, BOTH, "")])

    assert run_gate(monkeypatch, registry, None, BY_TAG, seed=seed) == 0
    assert registry.read(BY_TAG)


def test_a_seed_missing_a_required_platform_is_read_again(monkeypatch, tmp_path) -> None:
    seed = write_seed(tmp_path / "seed.json", {PINNED: ["linux/amd64"]})
    registry = FakeRegistry(docker=[(0, BOTH, "")])

    assert run_gate(monkeypatch, registry, None, PINNED, seed=seed) == 0
    assert registry.read(PINNED)


def test_the_committed_seed_holds_only_digest_verdicts_carrying_both_platforms() -> None:
    seed, note = arch.load_verdicts(REPO_ROOT / "scripts" / "image-arch-proven.json")
    doc = json.loads((REPO_ROOT / "scripts" / "image-arch-proven.json").read_text(encoding="utf-8"))

    assert note == ""
    assert len(seed.recalled) == len(doc["images"])
    assert len(seed.recalled_charts) == len(doc["charts"])
    for ref, platforms in seed.recalled.items():
        assert {"linux/amd64", "linux/arm64"} <= platforms, ref
