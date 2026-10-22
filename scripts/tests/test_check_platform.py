#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_platform.py
#  Purpose:      Prove the platform gate: what parse_requirement makes of each
#                shape versions.yaml writes, that a cluster below the floor is
#                REFUSED with exit 3 and one above the ceiling only warns, that
#                a missing stage is an error rather than a pass, that the
#                rke2 floor fires on an RKE2 cluster alone, and that a
#                DocumentDB stack below 1.35 is refused without the ImageVolume
#                gate by `dfe-ops preflight` as it is by the gate itself.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for bootstrap/check_platform.py, the only reader of `platform`.

Every case runs offline: the cluster version arrives through `--actual` and the
requirements through a `--file` fixture, so nothing here calls kubectl or reads
a kubeconfig. The two cases that DO read the committed versions.yaml assert the
shipped rc.13 stage and the floor `dfe-ops preflight` defaults to, because a
gate wired to one file and a preflight wired to another is the failure this
pair exists to catch.

The ImageVolume gate is read through a fake `kubectl` first on PATH. The preflight
cases drive `dfe-ops preflight` itself: its other cluster reads are answered
in-process (the `_run_text` seam test_strimzi_stored_versions.py drives) and the
gate read still goes out through the fake, which answers only the argv aimed at
the cluster preflight was given.

    python3 -m pytest scripts/tests/test_check_platform.py -q
    python3 scripts/tests/test_check_platform.py
"""

from __future__ import annotations

import contextlib
import functools
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from unittest import mock

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap"
VERSIONS = REPO_ROOT / "versions.yaml"
sys.path.insert(0, str(BOOTSTRAP))

import argocd_release  # noqa: E402
import check_platform  # noqa: E402

# Answers every cluster read `dfe-ops preflight` makes of a healthy cluster.
from test_strimzi_stored_versions import _cluster  # noqa: E402

FIXTURE = """\
current: "test-stack"

stacks:
  test-stack:
    platform:
      kubernetes: "1.34-1.36"
      rke2: ">=v1.34"
      rancher: ">=2.15"
      eks: ">=1.34"

  no-platform:
    bootstrap:
      cert-manager: "v1.21.2"

  no-kubernetes:
    platform:
      eks: ">=1.34"

  # An rke2 floor above the kubernetes one, so the RKE2 verdict is the only
  # thing that can refuse a 1.35 cluster.
  rke2-ahead:
    platform:
      kubernetes: "1.34-1.36"
      rke2: ">=v1.36"

  # Pins the DocumentDB operator, so a cluster older than 1.35 has to carry the
  # ImageVolume gate. `test-stack` pins none.
  documentdb:
    platform:
      kubernetes: "1.34-1.36"
    operators:
      documentdb-operator: "1.0.0"
"""

# Answers only the one argv it is built for, so any other call it receives fails loudly.
FAKE_KUBECTL = """\
#!/usr/bin/env python3
import sys

if sys.argv[1:] != {want!r}:
    sys.exit("unexpected kubectl call: " + " ".join(sys.argv[1:]))
sys.stdout.write({stdout!r})
sys.stderr.write({stderr!r})
sys.exit({rc})
"""

# The gate read against the current kubecontext, and against the cluster preflight is aimed at.
GATE_READ = ("get", "--raw", "/metrics")
PREFLIGHT_KUBECONFIG = "/nonexistent/kubeconfig"
PREFLIGHT_CONTEXT = "ctx-a"
PREFLIGHT_GATE_READ = ("--kubeconfig", PREFLIGHT_KUBECONFIG, "--context", PREFLIGHT_CONTEXT, *GATE_READ)

GATE_ON = 'kubernetes_feature_enabled{name="ImageVolume",stage="BETA"} 1\n'
GATE_OFF = 'kubernetes_feature_enabled{name="ImageVolume",stage="BETA"} 0\n'
# A scrape that carries other gates and not this one.
GATE_ABSENT = 'kubernetes_feature_enabled{name="WatchList",stage="BETA"} 1\n'
FORBIDDEN = 'forbidden: user cannot get path "/metrics"\n'


@contextlib.contextmanager
def _fixture(current: str = "test-stack"):
    """A versions.yaml holding the stack shapes the check has to tell apart.

    `current` names the stack `dfe-ops preflight` reads, which has no --stack flag.
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "versions.yaml"
        body = FIXTURE.replace('current: "test-stack"', f'current: "{current}"', 1)
        path.write_text(body, encoding="utf-8", newline="\n")
        yield path


def _run(versions: Path, *argv: str) -> tuple[int, str, str]:
    """main() against the fixture, returning (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    previous = sys.argv
    sys.argv = ["check_platform.py", "--file", str(versions), *argv]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = check_platform.main()
    finally:
        sys.argv = previous
    return rc, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def _kubectl(stdout: str = "", stderr: str = "", rc: int = 0, want: tuple[str, ...] = GATE_READ):
    """A `kubectl` first on PATH that answers the argv `want` with the given output."""
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "kubectl"
        body = FAKE_KUBECTL.format(want=list(want), stdout=stdout, stderr=stderr, rc=rc)
        script.write_text(body, encoding="utf-8", newline="\n")
        script.chmod(0o755)
        previous = os.environ["PATH"]
        os.environ["PATH"] = f"{td}{os.pathsep}{previous}"
        try:
            yield
        finally:
            os.environ["PATH"] = previous


@contextlib.contextmanager
def _override(value: str | None):
    """DFE_IMAGE_VOLUME_ENABLED set to `value` for the block, or removed when None."""
    name = check_platform.IMAGE_VOLUME_OVERRIDE_ENV
    previous = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


@functools.cache
def _dfeops() -> ModuleType:
    """scripts/dfe-ops, which has no .py suffix, loaded once."""
    loader = importlib.machinery.SourceFileLoader("dfeops_platform", str(REPO_ROOT / "scripts" / "dfe-ops"))
    spec = importlib.util.spec_from_loader("dfeops_platform", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dfeops_platform"] = module
    loader.exec_module(module)
    return module


def _preflight(versions: Path, minor: str, *argv: str) -> tuple[int, str]:
    """`dfe-ops preflight` against a healthy cluster on Kubernetes 1.<minor>: (exit code, stderr).

    `versions` stands in for the repo's versions.yaml. Preflight is aimed with --kubeconfig and
    --context, so a gate read has to carry both to reach a fake built for PREFLIGHT_GATE_READ.
    """
    dfeops = _dfeops()
    args = dfeops.build_parser().parse_args([
        "preflight", "--kubeconfig", PREFLIGHT_KUBECONFIG, "--context", PREFLIGHT_CONTEXT,
        "--registry", "registry.example.invalid", *argv,
    ])
    healthy = _cluster([])
    server = {"serverVersion": {"gitVersion": f"v1.{minor}.2", "major": "1", "minor": minor}}

    def run_text(cmd: list[str], env: dict | None = None) -> tuple[int, str, str]:
        if cmd[-3:] == ["version", "-o", "json"]:
            return 0, json.dumps(server), ""
        return healthy(cmd, env)

    err = io.StringIO()
    with (
        mock.patch.object(dfeops, "REPO_ROOT", versions.parent),
        mock.patch.object(dfeops, "_run_text", run_text),
        mock.patch.object(argocd_release, "read", return_value=(False, "no release")),
        mock.patch.object(urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")),
        contextlib.redirect_stderr(err),
    ):
        rc = dfeops.cmd_preflight(args)
    return rc, err.getvalue()


def _committed_platform() -> dict:
    root = check_platform.load_versions(VERSIONS)
    return root["stacks"][root["current"]]["platform"]


# --- the parser ---------------------------------------------------------------
def test_a_floor_has_no_ceiling() -> None:
    expect("'>=1.34' is a floor alone", check_platform.parse_requirement(">=1.34") == ("1.34", None))


def test_a_range_is_floor_and_ceiling() -> None:
    expect("'1.34-1.36' is both", check_platform.parse_requirement("1.34-1.36") == ("1.34", "1.36"))


def test_a_bare_version_pins_exactly() -> None:
    expect("'2.15' pins", check_platform.parse_requirement("2.15") == ("2.15", "2.15"))


def test_a_v_prefixed_range_is_still_a_range() -> None:
    """The `v` is a prefix, not a separator -- it must not read as an exact pin."""
    floor, ceiling = check_platform.parse_requirement("v1.34-v1.36")
    expect("'v1.34-v1.36' floor is 1.34", check_platform.as_tuple(floor) == (1, 34))
    expect("'v1.34-v1.36' ceiling is 1.36", check_platform.as_tuple(ceiling) == (1, 36))


def test_build_metadata_is_not_version_parts() -> None:
    """`rke2r1` carries digits; reading them would make v1.36.4 look like 1.36.4.2.1."""
    expect("v1.36.4+rke2r1 is 1.36.4", check_platform.as_tuple("v1.36.4+rke2r1") == (1, 36, 4))
    floor, ceiling = check_platform.parse_requirement("v1.36.4+rke2r1")
    expect("a gitVersion is an exact pin", (floor, ceiling) == ("v1.36.4+rke2r1", "v1.36.4+rke2r1"))


def test_the_eks_minor_parses() -> None:
    """EKS reports a minor of `34+`, which is not a number until the `+` goes."""
    expect("'1.34+' is 1.34", check_platform.as_tuple("1.34+") == (1, 34))


def test_a_patch_is_not_above_a_minor_ceiling() -> None:
    expect("1.36.4 is inside a 1.36 ceiling", not check_platform.above_ceiling((1, 36, 4), (1, 36)))
    expect("1.37 is above it", check_platform.above_ceiling((1, 37), (1, 36)))


# --- the verdicts -------------------------------------------------------------
def test_below_the_floor_is_refused() -> None:
    with _fixture() as versions:
        rc, _, err = _run(versions, "--actual", "1.33")
    expect("1.33 exits 3", rc == 3, f"got {rc}")
    expect("and says it refused", "REFUSED" in err, err)


def test_the_floor_itself_passes() -> None:
    with _fixture() as versions:
        rc, out, err = _run(versions, "--actual", "1.34")
    expect("1.34 exits 0", rc == 0, f"got {rc}")
    expect("with no warning", "WARNING" not in err, err)
    expect("and reports the requirement", "1.34-1.36" in out, out)


def test_above_the_ceiling_warns_and_proceeds() -> None:
    """Untested is not unsupported: the ceiling is a warning, not a refusal."""
    with _fixture() as versions:
        rc, _, err = _run(versions, "--actual", "1.37")
    expect("1.37 exits 0", rc == 0, f"got {rc}")
    expect("with the ceiling warning", "WARNING" in err, err)


def test_a_patch_release_does_not_trip_the_ceiling() -> None:
    with _fixture() as versions:
        rc, _, err = _run(versions, "--actual", "v1.36.4+rke2r1")
    expect("v1.36.4 exits 0", rc == 0, f"got {rc}")
    expect("with no ceiling warning", "WARNING" not in err, err)


def test_a_stack_without_the_stage_is_an_error() -> None:
    """A stack that declares no platform must not read as a cluster that passed."""
    with _fixture() as versions:
        rc, _, err = _run(versions, "--actual", "1.36", "--stack", "no-platform")
    expect("a stageless stack exits 1", rc == 1, f"got {rc}")
    expect("and names the stage", "platform" in err, err)


def test_a_stage_without_kubernetes_is_an_error() -> None:
    with _fixture() as versions:
        rc, _, err = _run(versions, "--actual", "1.36", "--stack", "no-kubernetes")
    expect("a keyless stage exits 1", rc == 1, f"got {rc}")
    expect("and names the key", "platform.kubernetes" in err, err)


def test_an_unknown_stack_is_an_error() -> None:
    with _fixture() as versions:
        rc, _, err = _run(versions, "--actual", "1.36", "--stack", "2.2.0-rc.99")
    expect("an unknown stack exits 1", rc == 1, f"got {rc}")
    expect("and names it", "2.2.0-rc.99" in err, err)


# --- the on-prem half ---------------------------------------------------------
def test_rke2_is_checked_on_an_rke2_cluster() -> None:
    with _fixture() as versions:
        _, out, _ = _run(versions, "--actual", "v1.36.4+rke2r1")
    expect("the rke2 floor is reported", "RKE2 v1.36.4+rke2r1 meets >=v1.34" in out, out)


def test_rke2_below_its_floor_is_refused() -> None:
    """A cluster inside the Kubernetes window that RKE2's own floor still refuses."""
    with _fixture() as versions:
        ok_rc, _, _ = _run(versions, "--actual", "v1.36.4+rke2r1", "--stack", "rke2-ahead")
        rc, _, err = _run(versions, "--actual", "v1.35.8+rke2r1", "--stack", "rke2-ahead")
    expect("an in-window RKE2 passes", ok_rc == 0, f"got {ok_rc}")
    expect("an under-floor RKE2 exits 3", rc == 3, f"got {rc}")
    expect("and names RKE2", "RKE2" in err, err)


def test_rke2_is_not_checked_on_anything_else() -> None:
    """The same 1.35 cluster, not reporting +rke2, has no rke2 floor to meet."""
    with _fixture() as versions:
        rc, out, _ = _run(versions, "--actual", "1.35", "--stack", "rke2-ahead")
    expect("a non-RKE2 1.35 passes", rc == 0, f"got {rc}")
    expect("with no RKE2 verdict", "RKE2" not in out, out)


# --- the ImageVolume gate -----------------------------------------------------
def test_a_134_cluster_with_the_gate_on_passes() -> None:
    with _fixture() as versions, _kubectl(stdout=GATE_ON), _override(None):
        rc, out, err = _run(versions, "--actual", "1.34", "--stack", "documentdb")
    expect("1.34 with the gate on exits 0", rc == 0, f"got {rc}: {err}")
    expect("and says the gate is on", "ImageVolume feature gate is on" in out, out)


def test_a_134_cluster_with_the_gate_off_is_refused() -> None:
    with _fixture() as versions, _kubectl(stdout=GATE_OFF), _override(None):
        rc, out, err = _run(versions, "--actual", "1.34", "--stack", "documentdb")
    expect("1.34 with the gate off exits 3", rc == 3, f"got {rc}: {err}")
    expect("and names the gate", "ImageVolume feature gate off" in err, err)
    expect("and does not report the cluster as meeting the floor", "meets" not in out, out)


def test_a_134_cluster_whose_gate_cannot_be_read_is_refused_naming_the_override() -> None:
    with _fixture() as versions, _override(None):
        with _kubectl(stderr=FORBIDDEN, rc=1):
            rc, _, err = _run(versions, "--actual", "1.34", "--stack", "documentdb")
        expect("a /metrics refusal exits 3", rc == 3, f"got {rc}: {err}")
        expect("and names the override", "DFE_IMAGE_VOLUME_ENABLED=true" in err, err)
        with _kubectl(stdout=GATE_ABSENT):
            rc, _, err = _run(versions, "--actual", "1.34", "--stack", "documentdb")
        expect("a scrape without the series is unreadable too", rc == 3, f"got {rc}: {err}")
        expect("and names the override", "DFE_IMAGE_VOLUME_ENABLED=true" in err, err)


def test_a_134_cluster_without_kubectl_is_refused_naming_the_override() -> None:
    with _fixture() as versions, tempfile.TemporaryDirectory() as empty, _override(None):
        previous = os.environ["PATH"]
        os.environ["PATH"] = empty
        try:
            rc, _, err = _run(versions, "--actual", "1.34", "--stack", "documentdb")
        finally:
            os.environ["PATH"] = previous
    expect("no kubectl exits 3", rc == 3, f"got {rc}: {err}")
    expect("and names the override", "DFE_IMAGE_VOLUME_ENABLED=true" in err, err)


def test_the_override_admits_a_gate_that_cannot_be_read() -> None:
    with _fixture() as versions, _kubectl(stderr=FORBIDDEN, rc=1), _override("true"):
        rc, _, err = _run(versions, "--actual", "1.34", "--stack", "documentdb")
    expect("the override exits 0", rc == 0, f"got {rc}: {err}")
    expect("and warns that the gate was not read", "WARNING" in err and "DFE_IMAGE_VOLUME_ENABLED" in err, err)


def test_only_true_is_an_override() -> None:
    """The message asks for `true`, so `1` or `yes` does not admit."""
    with _fixture() as versions, _kubectl(stderr=FORBIDDEN, rc=1), _override("1"):
        rc, _, _ = _run(versions, "--actual", "1.34", "--stack", "documentdb")
    expect("a value other than true does not admit", rc == 3, f"got {rc}")


def test_a_135_cluster_is_not_checked_for_the_gate() -> None:
    """The kubectl here reports the gate off, so a read on 1.35 would show as a REFUSED."""
    with _fixture() as versions, _kubectl(stdout=GATE_OFF), _override(None):
        rc, out, err = _run(versions, "--actual", "1.35", "--stack", "documentdb")
        rc_later, _, err_later = _run(versions, "--actual", "1.36", "--stack", "documentdb")
    expect("1.35 exits 0", rc == 0, f"got {rc}: {err}")
    expect("without a gate verdict", "ImageVolume" not in out + err, out + err)
    expect("a later minor exits 0 too", rc_later == 0, f"got {rc_later}: {err_later}")


def test_a_stack_without_the_documentdb_operator_is_not_checked_for_the_gate() -> None:
    with _fixture() as versions, _kubectl(stdout=GATE_OFF), _override(None):
        rc, out, err = _run(versions, "--actual", "1.34")
    expect("1.34 on a stack with no DocumentDB exits 0", rc == 0, f"got {rc}: {err}")
    expect("without a gate verdict", "ImageVolume" not in out + err, out + err)


def test_the_override_is_documented_where_a_deployer_reads_it() -> None:
    """bootstrap.sh's header lists the deploy variables, and the on-prem prerequisites name the check."""
    name = check_platform.IMAGE_VOLUME_OVERRIDE_ENV
    header = (BOOTSTRAP / "bootstrap.sh").read_text(encoding="utf-8")
    prerequisites = (REPO_ROOT / "docs" / "deployment" / "on-prem.md").read_text(encoding="utf-8")
    expect("bootstrap.sh's header lists the override", f"#   {name}" in header)
    expect("on-prem.md names the override", name in prerequisites)
    expect("and the gate it reads", check_platform.IMAGE_VOLUME_GATE in prerequisites)


# --- the same gate in dfe-ops preflight ---------------------------------------
def test_preflight_passes_a_134_cluster_with_the_gate_on() -> None:
    with _fixture("documentdb") as versions, _override(None):
        with _kubectl(stdout=GATE_ON, want=PREFLIGHT_GATE_READ):
            rc, err = _preflight(versions, "34")
    expect("1.34 with the gate on passes", rc == 0, f"got {rc}: {err}")
    expect("and says the gate is on", "ImageVolume feature gate is on" in err, err)


def test_preflight_refuses_a_134_cluster_with_the_gate_off() -> None:
    with _fixture("documentdb") as versions, _override(None):
        with _kubectl(stdout=GATE_OFF, want=PREFLIGHT_GATE_READ):
            rc, err = _preflight(versions, "34")
    expect("1.34 with the gate off fails", rc == 1, f"got {rc}: {err}")
    expect("and names the gate", "ImageVolume feature gate off" in err, err)
    expect("as the only blocking issue", "PREFLIGHT FAILED (1 blocking issue(s)" in err, err)


def test_preflight_refuses_a_134_cluster_whose_gate_cannot_be_read_naming_the_override() -> None:
    with _fixture("documentdb") as versions, _override(None):
        with _kubectl(stderr=FORBIDDEN, rc=1, want=PREFLIGHT_GATE_READ):
            rc, err = _preflight(versions, "34")
        expect("a /metrics refusal fails", rc == 1, f"got {rc}: {err}")
        expect("and names the override", "DFE_IMAGE_VOLUME_ENABLED=true" in err, err)
        with _kubectl(stdout=GATE_ABSENT, want=PREFLIGHT_GATE_READ):
            rc, err = _preflight(versions, "34")
        expect("a scrape without the series is unreadable too", rc == 1, f"got {rc}: {err}")
        expect("and names the override", "DFE_IMAGE_VOLUME_ENABLED=true" in err, err)


def test_preflight_reads_the_gate_from_the_cluster_it_is_aimed_at() -> None:
    """The fake here answers only the current kubecontext, which is not the cluster under test."""
    with _fixture("documentdb") as versions, _override(None):
        with _kubectl(stdout=GATE_ON, want=GATE_READ):
            rc, err = _preflight(versions, "34")
    expect("a gate read of the wrong cluster does not pass", rc == 1, f"got {rc}: {err}")
    expect("it reads as unreadable", "could not be read" in err, err)


def test_preflight_override_admits_a_gate_that_cannot_be_read() -> None:
    with _fixture("documentdb") as versions, _override("true"):
        with _kubectl(stderr=FORBIDDEN, rc=1, want=PREFLIGHT_GATE_READ):
            rc, err = _preflight(versions, "34")
    expect("the override passes", rc == 0, f"got {rc}: {err}")
    expect("and warns that the gate was not read", "[warn]" in err and "DFE_IMAGE_VOLUME_ENABLED" in err, err)


def test_preflight_takes_the_override_from_the_env_file() -> None:
    """stack-deploy hands the env file to bootstrap, so the override it carries is bootstrap's."""
    with _fixture("documentdb") as versions, tempfile.TemporaryDirectory() as td, _override(None):
        env_file = Path(td) / "deploy.env"
        env_file.write_text('DFE_IMAGE_VOLUME_ENABLED="true"\n', encoding="utf-8", newline="\n")
        with _kubectl(stderr=FORBIDDEN, rc=1, want=PREFLIGHT_GATE_READ):
            rc, err = _preflight(versions, "34", "--env-file", str(env_file))
    expect("the env file's override passes", rc == 0, f"got {rc}: {err}")
    expect("and warns that the gate was not read", "[warn]" in err and "DFE_IMAGE_VOLUME_ENABLED" in err, err)


def test_preflight_admits_only_true_as_the_override() -> None:
    with _fixture("documentdb") as versions, _override("1"):
        with _kubectl(stderr=FORBIDDEN, rc=1, want=PREFLIGHT_GATE_READ):
            rc, err = _preflight(versions, "34")
    expect("a value other than true does not admit", rc == 1, f"got {rc}: {err}")


def test_preflight_does_not_check_a_135_cluster_for_the_gate() -> None:
    """The fake reports the gate off, so a read on 1.35 would show as a failure."""
    with _fixture("documentdb") as versions, _override(None):
        with _kubectl(stdout=GATE_OFF, want=PREFLIGHT_GATE_READ):
            rc, err = _preflight(versions, "35")
            rc_later, err_later = _preflight(versions, "36")
    expect("1.35 passes", rc == 0, f"got {rc}: {err}")
    expect("without a gate verdict", "ImageVolume" not in err, err)
    expect("a later minor passes too", rc_later == 0, f"got {rc_later}: {err_later}")


def test_preflight_does_not_check_a_stack_without_the_documentdb_operator() -> None:
    with _fixture() as versions, _override(None):
        with _kubectl(stdout=GATE_OFF, want=PREFLIGHT_GATE_READ):
            rc, err = _preflight(versions, "34")
    expect("1.34 on a stack with no DocumentDB passes", rc == 0, f"got {rc}: {err}")
    expect("without a gate verdict", "ImageVolume" not in err, err)


def test_preflight_says_when_it_cannot_tell_which_stack_it_is_checking() -> None:
    """A `current` that names no stack leaves nothing to read the DocumentDB pin from."""
    with _fixture("no-such-stack") as versions, _override(None):
        with _kubectl(stdout=GATE_OFF, want=PREFLIGHT_GATE_READ):
            rc, err = _preflight(versions, "34", "--min-k8s", "1.34")
    expect("the rest of the preflight passes", rc == 0, f"got {rc}: {err}")
    expect("and the skipped check is said out loud", "ImageVolume feature gate was not checked" in err, err)


def test_preflight_and_the_gate_refuse_the_same_clusters() -> None:
    """One verdict from both callers for every stack, minor, /metrics answer and override."""
    answers = {
        "gate on": {"stdout": GATE_ON},
        "gate off": {"stdout": GATE_OFF},
        "metrics refused": {"stderr": FORBIDDEN, "rc": 1},
    }
    for stack in ("documentdb", "test-stack"):
        for minor in ("34", "35"):
            for label, answer in answers.items():
                for value in (None, "true"):
                    with _fixture(stack) as versions, _override(value):
                        with _kubectl(**answer):
                            gate_rc, _, _ = _run(versions, "--actual", f"1.{minor}", "--stack", stack)
                        with _kubectl(want=PREFLIGHT_GATE_READ, **answer):
                            preflight_rc, err = _preflight(versions, minor)
                    cell = f"{stack}, 1.{minor}, {label}, override {value}"
                    expect(
                        f"{cell}: both refuse or both pass",
                        (gate_rc == 3) == (preflight_rc == 1),
                        f"gate exit {gate_rc}, preflight exit {preflight_rc}: {err}",
                    )


# --- the committed stack ------------------------------------------------------
def test_the_current_stack_declares_the_platform() -> None:
    platform = _committed_platform()
    expect("kubernetes is a range", check_platform.parse_requirement(platform["kubernetes"])[1] is not None)
    for key in ("rke2", "rancher", "eks"):
        expect(f"platform.{key} is declared", isinstance(platform.get(key), str), f"got {platform.get(key)!r}")


def test_preflight_defaults_to_the_same_floor() -> None:
    """Two readers, one floor: preflight must not pass what bootstrap refuses.

    The default is resolved when preflight runs, not when the parser is built, so
    what this checks is the resolver -- and that no other subcommand pays for it.
    """
    dfeops = _dfeops()
    args = dfeops.build_parser().parse_args(["preflight"])
    expect("an unset --min-k8s stays unset in the parser", args.min_k8s == "", f"got {args.min_k8s!r}")
    expect(
        "preflight resolves it to platform.kubernetes",
        dfeops._platform_k8s_requirement() == _committed_platform()["kubernetes"],
        f"got {dfeops._platform_k8s_requirement()!r}",
    )


def test_bootstrap_passes_the_stack_through() -> None:
    """dfe-ops names the stack it deploys, so the floor checked is that stack's."""
    script = (BOOTSTRAP / "bootstrap.sh").read_text(encoding="utf-8")
    expect("bootstrap.sh runs the check", "check_platform.py" in script)
    expect("and hands it DFE_STACK_VERSION", '--stack "${DFE_STACK_VERSION}"' in script)


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
