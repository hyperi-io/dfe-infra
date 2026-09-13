#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_platform.py
#  Purpose:      Prove the platform gate: what parse_requirement makes of each
#                shape versions.yaml writes, that a cluster below the floor is
#                REFUSED with exit 3 and one above the ceiling only warns, that
#                a missing stage is an error rather than a pass, and that the
#                rke2 floor fires on an RKE2 cluster alone.
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

    python3 -m pytest scripts/tests/test_check_platform.py -q
    python3 scripts/tests/test_check_platform.py
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import sys
import tempfile
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap"
VERSIONS = REPO_ROOT / "versions.yaml"
sys.path.insert(0, str(BOOTSTRAP))

import check_platform  # noqa: E402

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
"""


@contextlib.contextmanager
def _fixture():
    """A versions.yaml holding the three stack shapes the check has to tell apart."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "versions.yaml"
        path.write_text(FIXTURE, encoding="utf-8", newline="\n")
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
    loader = importlib.machinery.SourceFileLoader("dfeops_platform", str(REPO_ROOT / "scripts" / "dfe-ops"))
    spec = importlib.util.spec_from_loader("dfeops_platform", loader)
    dfeops = importlib.util.module_from_spec(spec)
    sys.modules["dfeops_platform"] = dfeops
    loader.exec_module(dfeops)
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
