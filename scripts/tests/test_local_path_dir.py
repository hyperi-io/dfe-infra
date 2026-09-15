#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_local_path_dir.py
#  Purpose:      Prove the local-path config rewrite retargets the default node
#                path and keeps everything else the manifest ships
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for bootstrap/local_path_dir.py.

The rewrite runs against a live ConfigMap, so the tests feed it the document
upstream's manifest actually ships and assert the result is still a config the
provisioner can read: the default entry moved, a hand-added per-node entry kept,
and any key beside nodePathMap passed through.

Runs offline. Under pytest, and standalone via the main() runner at the bottom
(matching the other tests in this dir).

    python3 -m pytest scripts/tests/test_local_path_dir.py
    python3 scripts/tests/test_local_path_dir.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap"

DISK = "/var/lib/rancher/local-path-provisioner"

# deploy/local-path-storage.yaml at v0.0.36, the pin bootstrap.sh applies.
UPSTREAM = """{
        "nodePathMap":[
        {
                "node":"DEFAULT_PATH_FOR_NON_LISTED_NODES",
                "paths":["/opt/local-path-provisioner"]
        }
        ]
}"""


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, BOOTSTRAP / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


local_path_dir = _load("local_path_dir")


def test_the_upstream_default_moves_to_the_chosen_disk():
    got = json.loads(local_path_dir.retarget(UPSTREAM, DISK))
    assert got["nodePathMap"] == [
        {"node": "DEFAULT_PATH_FOR_NON_LISTED_NODES", "paths": [DISK]}
    ]


def test_the_result_is_the_json_the_provisioner_reads():
    """The ConfigMap value is a JSON document, not a fragment."""
    json.loads(local_path_dir.retarget(UPSTREAM, DISK))


def test_a_per_node_entry_is_left_alone():
    config = json.dumps(
        {
            "nodePathMap": [
                {"node": "dfe-k8s-1", "paths": ["/mnt/fast"]},
                {"node": "DEFAULT_PATH_FOR_NON_LISTED_NODES", "paths": ["/opt/local-path-provisioner"]},
            ]
        }
    )
    got = json.loads(local_path_dir.retarget(config, DISK))
    assert got["nodePathMap"][0] == {"node": "dfe-k8s-1", "paths": ["/mnt/fast"]}
    assert got["nodePathMap"][1]["paths"] == [DISK]


def test_keys_beside_the_node_map_survive():
    """A newer manifest carries more than nodePathMap, and dropping it breaks the provisioner."""
    config = json.dumps({"nodePathMap": [], "storageClassConfigs": {"local-path": {}}})
    got = json.loads(local_path_dir.retarget(config, DISK))
    assert got["storageClassConfigs"] == {"local-path": {}}


def test_a_config_with_no_default_entry_gains_one():
    config = json.dumps({"nodePathMap": [{"node": "dfe-k8s-1", "paths": ["/mnt/fast"]}]})
    got = json.loads(local_path_dir.retarget(config, DISK))
    assert {"node": "DEFAULT_PATH_FOR_NON_LISTED_NODES", "paths": [DISK]} in got["nodePathMap"]


def test_only_the_default_entry_is_rewritten_when_several_nodes_are_listed():
    config = json.dumps(
        {
            "nodePathMap": [
                {"node": "DEFAULT_PATH_FOR_NON_LISTED_NODES", "paths": ["/opt/local-path-provisioner"]},
                {"node": "dfe-k8s-2", "paths": ["/mnt/a", "/mnt/b"]},
            ]
        }
    )
    got = json.loads(local_path_dir.retarget(config, DISK))
    assert got["nodePathMap"][1]["paths"] == ["/mnt/a", "/mnt/b"]


def test_a_relative_dir_is_refused():
    """local-path resolves the path on the node, so a relative one misplaces every volume."""
    import subprocess

    reply = subprocess.run(
        [sys.executable, str(BOOTSTRAP / "local_path_dir.py"), "--dir", "var/lib/rancher"],
        input=UPSTREAM, capture_output=True, text=True, check=False,
    )
    assert reply.returncode == 2
    assert "absolute" in reply.stderr


def test_the_cli_reads_stdin_and_writes_the_document():
    import subprocess

    reply = subprocess.run(
        [sys.executable, str(BOOTSTRAP / "local_path_dir.py"), "--dir", DISK],
        input=UPSTREAM, capture_output=True, text=True, check=False,
    )
    assert reply.returncode == 0
    assert json.loads(reply.stdout)["nodePathMap"][0]["paths"] == [DISK]


# --- standalone runner (mirrors the other tests in this dir) ------------------
def main() -> int:
    failures = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:
            failures += 1
            print(f"FAIL  {name}  {exc}")
    print(f"\n{'FAILED' if failures else 'ALL PASSED'} -- {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
