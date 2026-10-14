#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_transform_files.py
#  Purpose:      Prove the transform file-set check passes an instance that mounts
#                what the engine lists and holds, and fails one that mounts
#                something else or other bytes, one with no program to run, and
#                one under the bare chart name.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""scripts/check_transform_files.py, against a kubectl that answers from a spec file.

    python3 -m pytest scripts/tests/test_check_transform_files.py -q

The stand-in kubectl is a script on PATH: it lists the spec's Deployments, cats
the spec's values file for `exec <engine> -- sh -c cat ...`, lists the spec's
files for `exec deploy/<name> -- ls -1 <path>`, and prints the spec's digests
for `exec deploy/<name> -- sha256sum <path>...`. Anything else exits 2, as an
unreachable cluster does.
"""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

TESTS = Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import check_transform_files as ctf  # noqa: E402
import values_migration as vm  # noqa: E402

FIXTURES = TESTS / "fixtures" / "values-2.2.0"
VRL = "dfe-transform-vrl-merakivrl"
OVERLAY = f"{VRL}-values.yaml"

FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys
spec = json.load(open(os.environ["FAKE_KUBECTL_SPEC"], encoding="utf-8"))
args = sys.argv[1:]
if "get" in args and "deployments" in args and "deployments" in spec:
    print(json.dumps({"items": [{"metadata": {"name": n}} for n in spec["deployments"]]}))
    sys.exit(0)
if "exec" in args:
    target = args[args.index("exec") + 1]
    command = args[args.index("--") + 1:]
    if command[0] == "sha256sum":
        keys = ["sha:" + target + ":" + path for path in command[1:]]
        if all(key in spec for key in keys):
            print("\\n".join(spec[key] + "  " + path for key, path in zip(keys, command[1:])))
            sys.exit(0)
        print("sha256sum: no such file", file=sys.stderr)
        sys.exit(1)
    key = "engine:" + command[-1] if command[0] == "sh" else "ls:" + target + ":" + command[-1]
    if key in spec:
        value = spec[key]
        print(value if isinstance(value, str) else "\\n".join(value))
        sys.exit(0)
    print("no such file: " + key, file=sys.stderr)
    sys.exit(1)
sys.exit(2)
"""


@pytest.fixture
def cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where a test writes the spec the stand-in kubectl answers from."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(FAKE_KUBECTL, encoding="utf-8")
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    spec = tmp_path / "spec.json"
    monkeypatch.setenv("FAKE_KUBECTL_SPEC", str(spec))
    return spec


def _carried_vrl(tmp_path: Path) -> str:
    root = tmp_path / "deploy"
    shutil.copytree(FIXTURES, root)
    vm.expand(root, vm.load_map())
    return (root / "values" / OVERLAY).read_text(encoding="utf-8")


MOUNTS = {"transforms": "/etc/dfe-transform-vrl-transforms", "enrichment": "/etc/dfe-transform-vrl-enrichment"}


def _held(overlay: str) -> dict[str, str]:
    """The sha256 of each file the overlay holds, keyed by its mounted path, as a YAML loader reads it."""
    sets = yaml.safe_load(overlay).get("fileSets") or {}
    return {
        f"{MOUNTS[name]}/{item['name']}": hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
        for name, file_set in sets.items()
        for item in file_set.get("files") or []
    }


def _spec(
    path: Path,
    overlay: str,
    transforms: list[str],
    enrichment: list[str],
    digests: dict[str, str] | None = None,
) -> None:
    spec = {
        "deployments": ["dfe-engine", VRL],
        f"engine:{OVERLAY}": overlay,
        f"ls:deploy/{VRL}:{MOUNTS['transforms']}": transforms,
        f"ls:deploy/{VRL}:{MOUNTS['enrichment']}": enrichment,
    }
    held = _held(overlay) if digests is None else digests
    spec.update({f"sha:deploy/{VRL}:{mounted}": digest for mounted, digest in held.items()})
    path.write_text(json.dumps(spec), encoding="utf-8")


def test_an_instance_mounting_what_the_engine_lists_passes(
    cluster: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _spec(cluster, _carried_vrl(tmp_path), ["filebeat.vrl"], ["timezones.csv"])
    assert ctf.main(["--namespace", "dfe"]) == ctf.EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS] dfe-transform-vrl/merakivrl transforms: 1 file(s), as the engine lists and holds them\n" in out
    assert "[PASS] dfe-transform-vrl/merakivrl enrichment: 1 file(s), as the engine lists and holds them\n" in out


def test_a_file_mounted_with_other_bytes_fails_naming_it(
    cluster: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A render that drops `<no value>` from a program mounts the right name with the wrong bytes."""
    overlay = _carried_vrl(tmp_path)
    digests = _held(overlay)
    program = f"{MOUNTS['transforms']}/filebeat.vrl"
    digests[program] = hashlib.sha256(b". = parse_json!(.message)\n").hexdigest()
    _spec(cluster, overlay, ["filebeat.vrl"], ["timezones.csv"], digests)
    assert ctf.main(["--namespace", "dfe"]) == ctf.EXIT_DIFFERS
    out = capsys.readouterr().out
    held = _held(overlay)[program]
    assert (
        "[FAIL] dfe-transform-vrl/merakivrl transforms: the pod mounts other bytes than the engine holds: "
        f"filebeat.vrl (engine {held[:12]}, pod {digests[program][:12]})"
    ) in out
    assert "[PASS] dfe-transform-vrl/merakivrl enrichment: 1 file(s)" in out


def test_a_pod_that_cannot_digest_its_files_is_not_a_pass(
    cluster: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _spec(cluster, _carried_vrl(tmp_path), ["filebeat.vrl"], ["timezones.csv"], {})
    assert ctf.main(["--namespace", "dfe"]) == ctf.EXIT_DIFFERS
    out = capsys.readouterr().out
    assert "[FAIL] dfe-transform-vrl/merakivrl transforms: the pod's files could not be digested" in out
    assert "[FAIL] dfe-transform-vrl/merakivrl enrichment: the pod's files could not be digested" in out


def test_a_file_written_as_a_folded_block_is_named_as_not_compared(
    cluster: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    overlay = _carried_vrl(tmp_path)
    digests = _held(overlay)
    table = "      - name: timezones.csv\n        content: "
    folded = overlay.replace(f"{table}|", f"{table}>")
    assert folded != overlay
    _spec(cluster, folded, ["filebeat.vrl"], ["timezones.csv"], digests)
    assert ctf.main(["--namespace", "dfe"]) == ctf.EXIT_OK
    out = capsys.readouterr().out
    assert "enrichment: 1 file(s), as the engine lists and holds them (1 written as a block not read here" in out


def test_an_instance_mounting_something_else_fails_naming_both_sides(
    cluster: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _spec(cluster, _carried_vrl(tmp_path), [], ["timezones.csv"])
    assert ctf.main(["--namespace", "dfe"]) == ctf.EXIT_DIFFERS
    assert "the engine lists ['filebeat.vrl'], the pod mounts []" in capsys.readouterr().out


def test_an_overlay_whose_programs_sit_under_another_key_fails(cluster: Path, capsys: pytest.CaptureFixture) -> None:
    """Both sides empty: the engine and the chart read the same missing key, and nothing runs."""
    original = (FIXTURES / "values" / OVERLAY).read_text(encoding="utf-8")
    _spec(cluster, original, [], [])
    assert ctf.main(["--namespace", "dfe"]) == ctf.EXIT_DIFFERS
    out = capsys.readouterr().out
    assert "[FAIL] dfe-transform-vrl/merakivrl transforms: the engine lists no file" in out
    assert "[PASS] dfe-transform-vrl/merakivrl enrichment: 0 file(s)" in out


def test_an_instance_under_the_bare_chart_name_fails(cluster: Path, capsys: pytest.CaptureFixture) -> None:
    cluster.write_text(json.dumps({"deployments": ["dfe-engine", "dfe-transform-vrl"]}), encoding="utf-8")
    assert ctf.main(["--namespace", "dfe"]) == ctf.EXIT_DIFFERS
    assert "[FAIL] dfe-transform-vrl: an instance renders under the bare chart name" in capsys.readouterr().out


def test_a_stack_with_no_transform_passes_saying_so(cluster: Path, capsys: pytest.CaptureFixture) -> None:
    cluster.write_text(json.dumps({"deployments": ["dfe-engine", "dfe-receiver"]}), encoding="utf-8")
    assert ctf.main(["--namespace", "dfe"]) == ctf.EXIT_OK
    assert "no instance of an app consuming a file set runs here" in capsys.readouterr().out


def test_a_cluster_that_cannot_be_read_is_not_a_pass(cluster: Path, capsys: pytest.CaptureFixture) -> None:
    cluster.write_text(json.dumps({}), encoding="utf-8")
    assert ctf.main(["--namespace", "dfe"]) == ctf.EXIT_UNREAD
    assert "[UNREAD] cannot list Deployments" in capsys.readouterr().out


def test_an_overlay_mount_path_wins_over_the_catalogue() -> None:
    text = "fileSets:\n  transforms:\n    mountPath: /etc/custom\n    files:\n      - name: a.vrl\n"
    file_set = {"name": "transforms", "values_path": "fileSets.transforms.files", "mount_path": "/etc/x"}
    assert ctf.mount_path(text, file_set) == "/etc/custom"
    assert ctf.mount_path("fileSets: {}\n", file_set) == "/etc/x"


@pytest.mark.parametrize("app", ["dfe-transform-vrl", "dfe-transform-vector"])
def test_the_catalogue_mounts_each_set_where_the_integration_values_do(app: str) -> None:
    """apps.yaml is the engine's map and argocd/values the chart's, so the two must name one directory."""
    sets = ctf.file_sets(ctf.MANIFEST.read_text(encoding="utf-8"))[app]
    values = yaml.safe_load((REPO_ROOT / "argocd" / "values" / "apps" / app / "values.yaml").read_text(encoding="utf-8"))
    for file_set in sets:
        assert file_set["values_path"] == f"fileSets.{file_set['name']}.files"
        assert values["fileSets"][file_set["name"]]["mountPath"] == file_set["mount_path"]


def test_the_smoke_suite_runs_the_check() -> None:
    suite = (REPO_ROOT / "bootstrap" / "run-all-smoke-tests.sh").read_text(encoding="utf-8")
    assert '"smoke-test-transforms.sh"' in suite
    script = (REPO_ROOT / "bootstrap" / "smoke-test-transforms.sh").read_text(encoding="utf-8")
    assert "scripts/check_transform_files.py" in script
