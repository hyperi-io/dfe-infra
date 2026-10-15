#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/tests/test_registry_retry.py
#  Purpose:      Prove the multi-arch gate waits out a rate limited registry
#                instead of failing the stack release on the first 429, and that
#                it still fails, naming the registry, once the retries run out.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Retry behaviour of the registry reads behind scripts/check_image_arch.py.

A fake registry stands in for docker and helm: each tool gets a script of the
results its calls return, in order, so a registry that answers 429 twice and then
serves the manifest is a three-entry list. `subprocess.run` is the only seam
faked, so the real command lines, the retry loop and the messages all run. The
sleeps are captured instead of taken.

    python3 -m pytest scripts/tests/test_registry_retry.py
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_image_arch as arch  # noqa: E402

# The instance check_image_arch bound, so a patch lands on the code under test even
# when another test module has loaded its own copy of registry_pins.
pins = arch.registry_pins

# What public.ecr.aws and the OCI client printed in the Stack Release run that failed.
ECR_429_DOCKER = "ERROR: toomanyrequests: Data limit exceeded"
ECR_429_HELM = (
    'Error: failed to perform "FetchReference" on source: GET '
    '"https://public.ecr.aws/v2/karpenter/karpenter/manifests/1.14.1": '
    "response status code 429: toomanyrequests: Data limit exceeded"
)
INDEX = (
    '{"schemaVersion": 2, "manifests": ['
    '{"platform": {"os": "linux", "architecture": "amd64"}},'
    '{"platform": {"os": "linux", "architecture": "arm64"}}]}'
)
ECR_IMAGE = "public.ecr.aws/eks/aws-load-balancer-controller:v3.6.0"
KARPENTER = "oci://public.ecr.aws/karpenter/karpenter"


class FakeRegistry:
    """Answers docker and helm from a script per tool and records every call.

    Each entry is the (returncode, stdout, stderr) one call receives. A call past
    the end of its script fails the test, which proves a read did not retry more
    than the script allows.
    """

    def __init__(self, **scripts: list[tuple[int, str, str]]) -> None:
        self.scripts = {tool: list(entries) for tool, entries in scripts.items()}
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], *_args: object, **_kwargs: object):
        self.calls.append(cmd)
        script = self.scripts[cmd[0]]
        assert script, f"{cmd[0]} was called more often than scripted"
        returncode, stdout, stderr = script.pop(0)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture the backoff instead of taking it, with jitter fixed at 1.0x."""
    waits: list[float] = []
    monkeypatch.setattr(pins, "_sleep", waits.append)
    monkeypatch.setattr(pins, "_random", lambda: 0.5)
    return waits


def serve(monkeypatch: pytest.MonkeyPatch, registry: FakeRegistry) -> FakeRegistry:
    monkeypatch.setattr(subprocess, "run", registry)
    return registry


def gate_budget() -> pins.RetryBudget:
    """The budget check_image_arch.main gives a run: patient waits, capped in total."""
    return pins.RetryBudget(arch.RETRY_BUDGET_SECONDS)


def test_an_image_read_waits_out_two_429s_and_then_answers(monkeypatch, slept) -> None:
    registry = serve(
        monkeypatch,
        FakeRegistry(
            docker=[
                (1, "", ECR_429_DOCKER),
                (1, "", ECR_429_DOCKER),
                (0, INDEX, ""),
            ]
        ),
    )

    found, err = pins.ref_platforms(ECR_IMAGE, gate_budget())

    assert (found, err) == ({"linux/amd64", "linux/arm64"}, "")
    assert len(registry.calls) == 3
    assert slept == [4.0, 8.0]


def test_a_chart_lookup_waits_out_two_429s_and_then_answers(monkeypatch, slept) -> None:
    registry = serve(
        monkeypatch,
        FakeRegistry(
            helm=[
                (1, "", ECR_429_HELM),
                (1, "", ECR_429_HELM),
                (0, "name: karpenter\nappVersion: 1.14.1\n", ""),
            ]
        ),
    )

    assert arch.chart_app_version(KARPENTER, None, "1.14.1", gate_budget()) == "1.14.1"
    assert len(registry.calls) == 3
    assert slept == [4.0, 8.0]


def test_a_chart_pull_waits_out_a_429_and_then_unpacks(monkeypatch, slept, tmp_path) -> None:
    attempts: list[list[str]] = []

    def helm_pull(cmd: list[str], *_args: object, **_kwargs: object):
        attempts.append(cmd)
        if len(attempts) == 1:
            return subprocess.CompletedProcess(cmd, 1, "", ECR_429_HELM)
        (tmp_path / "karpenter").mkdir()
        (tmp_path / "karpenter" / "Chart.yaml").write_text("name: karpenter\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", helm_pull)

    chart = arch.pull_chart(KARPENTER, None, "1.14.1", tmp_path, gate_budget())

    assert chart == tmp_path / "karpenter"
    assert len(attempts) == 2
    assert slept == [4.0]


def test_a_registry_that_keeps_refusing_fails_the_image_read_naming_it(
    monkeypatch, slept
) -> None:
    registry = serve(monkeypatch, FakeRegistry(docker=[(1, "", ECR_429_DOCKER)] * 5))

    found, err = pins.ref_platforms(ECR_IMAGE, gate_budget())

    assert found is None
    assert "public.ecr.aws rate limited this read and gave up after 5 attempts" in err
    assert "toomanyrequests" in err
    assert len(registry.calls) == 5
    assert slept == [4.0, 8.0, 16.0, 32.0]


def test_a_registry_that_keeps_refusing_fails_the_chart_lookup_naming_it(
    monkeypatch, slept
) -> None:
    registry = serve(monkeypatch, FakeRegistry(helm=[(1, "", ECR_429_HELM)] * 5))

    with pytest.raises(RuntimeError) as failure:
        arch.chart_app_version(KARPENTER, None, "1.14.1", gate_budget())

    assert "public.ecr.aws rate limited this read and gave up after 5 attempts" in str(
        failure.value
    )
    assert len(registry.calls) == 5


def test_the_failure_reaches_the_gate_as_a_named_error_not_a_pass(
    monkeypatch, slept
) -> None:
    serve(monkeypatch, FakeRegistry(helm=[(1, "", ECR_429_HELM)] * 5))
    pinned = {"operators": {"karpenter": "1.14.1"}}

    refs, _skipped, broken = arch.image_refs(
        pinned, resolve_charts=True, third_party_only=True, budget=gate_budget()
    )

    assert not [label for label, _ref in refs if "karpenter" in label]
    assert len(broken) == 1
    assert broken[0].startswith(
        "operators.karpenter: chart 1.14.1 images karpenter controller did not resolve"
    )
    assert "public.ecr.aws rate limited" in broken[0]


def test_a_server_error_that_never_clears_is_not_called_a_rate_limit(monkeypatch, slept) -> None:
    serve(monkeypatch, FakeRegistry(docker=[(1, "", "ERROR: 503 Service Unavailable")] * 5))

    _found, err = pins.ref_platforms("quay.io/strimzi/operator:1.2.0", gate_budget())

    assert "quay.io kept failing and gave up after 5 attempts" in err
    assert "rate limited" not in err


@pytest.mark.parametrize(
    ("tool", "stderr"),
    [
        ("docker", "ERROR: public.ecr.aws/eks/x:v9: not found"),
        ("docker", "ERROR: 401 Unauthorized"),
        ("helm", "Error: chart not found: karpenter version 9.9.9"),
    ],
)
def test_an_absence_or_an_auth_error_is_not_retried(monkeypatch, slept, tool, stderr) -> None:
    registry = serve(monkeypatch, FakeRegistry(**{tool: [(1, "", stderr)]}))

    if tool == "docker":
        found, err = pins.ref_platforms(ECR_IMAGE, gate_budget())
        assert found is None
        assert err == stderr
    else:
        with pytest.raises(RuntimeError):
            arch.chart_app_version(KARPENTER, None, "9.9.9", gate_budget())

    assert len(registry.calls) == 1
    assert slept == []


def test_a_spent_budget_ends_the_retries_early_and_says_so(monkeypatch, slept) -> None:
    registry = serve(monkeypatch, FakeRegistry(docker=[(1, "", ECR_429_DOCKER)] * 2))
    budget = pins.RetryBudget(5.0)

    _found, err = pins.ref_platforms(ECR_IMAGE, budget)

    assert "gave up after 2 attempts (the run's retry budget is spent)" in err
    assert len(registry.calls) == 2
    assert slept == [4.0]
    assert budget.seconds == 1.0


def test_one_budget_is_shared_by_every_read_of_a_run(monkeypatch, slept) -> None:
    registry = serve(monkeypatch, FakeRegistry(docker=[(1, "", ECR_429_DOCKER)] * 3))
    budget = pins.RetryBudget(0.0)

    for _ in range(3):
        _found, err = pins.ref_platforms(ECR_IMAGE, budget)
        assert "gave up after 1 attempt (the run's retry budget is spent)" in err

    assert len(registry.calls) == 3
    assert slept == []


def test_a_read_with_no_budget_keeps_the_quick_schedule(monkeypatch, slept) -> None:
    """dfe-stack's reads cap nothing in total, so they stay at three short attempts."""
    registry = serve(monkeypatch, FakeRegistry(docker=[(1, "", ECR_429_DOCKER)] * 3))

    _found, err = pins.ref_platforms(ECR_IMAGE)

    assert "public.ecr.aws rate limited this read and gave up after 3 attempts" in err
    assert len(registry.calls) == 3
    assert slept == [1.0, 2.0]


def test_the_gate_gives_every_read_of_its_run_the_patient_schedule(
    monkeypatch, slept, capsys
) -> None:
    registry = serve(
        monkeypatch,
        FakeRegistry(
            docker=[
                (1, "", ECR_429_DOCKER),
                (1, "", ECR_429_DOCKER),
                (0, INDEX, ""),
            ]
        ),
    )
    monkeypatch.setattr(sys, "argv", ["check_image_arch.py", "--image", ECR_IMAGE])

    status = arch.main()

    assert status == 0
    assert f"  ok     {ECR_IMAGE} {ECR_IMAGE}" in capsys.readouterr().out
    assert len(registry.calls) == 3
    assert slept == [4.0, 8.0]


def test_a_missing_helm_is_reported_and_not_retried(monkeypatch, slept) -> None:
    def no_helm(cmd: list[str], *_args: object, **_kwargs: object):
        raise FileNotFoundError("helm")

    monkeypatch.setattr(subprocess, "run", no_helm)

    with pytest.raises(RuntimeError, match="cannot run helm"):
        arch.chart_app_version(KARPENTER, None, "1.14.1")

    assert slept == []


@pytest.mark.parametrize(
    ("ref", "host"),
    [
        ("public.ecr.aws/eks/aws-load-balancer-controller:v3.6.0", "public.ecr.aws"),
        ("docker.io/rancher/local-path-provisioner:v0.0.37@sha256:ab", "docker.io"),
        ("valkey/valkey:8.1.10-alpine@sha256:ab", "docker.io"),
        ("busybox:1.38.0", "docker.io"),
        ("registry.k8s.io/git-sync/git-sync:v4.7.1", "registry.k8s.io"),
        ("localhost:5000/dfe/x:v1", "localhost:5000"),
        ("localhost/dfe/x:v1", "localhost"),
    ],
)
def test_the_registry_host_of_an_image_reference(ref: str, host: str) -> None:
    assert pins.registry_host(ref) == host


@pytest.mark.parametrize(
    ("chart_ref", "repo_url", "host"),
    [
        (KARPENTER, None, "public.ecr.aws"),
        ("keda", "https://kedacore.github.io/charts", "kedacore.github.io"),
        ("oci://ghcr.io/clickhouse/clickhouse-operator-helm", None, "ghcr.io"),
    ],
)
def test_the_registry_a_chart_is_fetched_from(chart_ref, repo_url, host) -> None:
    assert arch.chart_registry(chart_ref, repo_url) == host
