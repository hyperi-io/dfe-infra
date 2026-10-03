#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_local_path_dir.py
#  Purpose:      Prove the local-path config rewrite retargets the default node
#                path and keeps everything else the manifest ships
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for bootstrap/local_path_dir.py and bootstrap/local_path_image.py.

The rewrite runs against a live ConfigMap, so the tests feed it the document
upstream's manifest actually ships and assert the result is still a config the
provisioner can read: the default entry moved, a hand-added per-node entry kept,
and any key beside nodePathMap passed through. The image pin is held to the same
standard: only the provisioner reference moves, and a manifest it cannot pin is
refused.

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
                {"node": "node-1", "paths": ["/mnt/fast"]},
                {"node": "DEFAULT_PATH_FOR_NON_LISTED_NODES", "paths": ["/opt/local-path-provisioner"]},
            ]
        }
    )
    got = json.loads(local_path_dir.retarget(config, DISK))
    assert got["nodePathMap"][0] == {"node": "node-1", "paths": ["/mnt/fast"]}
    assert got["nodePathMap"][1]["paths"] == [DISK]


def test_keys_beside_the_node_map_survive():
    """A newer manifest carries more than nodePathMap, and dropping it breaks the provisioner."""
    config = json.dumps({"nodePathMap": [], "storageClassConfigs": {"local-path": {}}})
    got = json.loads(local_path_dir.retarget(config, DISK))
    assert got["storageClassConfigs"] == {"local-path": {}}


def test_a_config_with_no_default_entry_gains_one():
    config = json.dumps({"nodePathMap": [{"node": "node-1", "paths": ["/mnt/fast"]}]})
    got = json.loads(local_path_dir.retarget(config, DISK))
    assert {"node": "DEFAULT_PATH_FOR_NON_LISTED_NODES", "paths": [DISK]} in got["nodePathMap"]


def test_only_the_default_entry_is_rewritten_when_several_nodes_are_listed():
    config = json.dumps(
        {
            "nodePathMap": [
                {"node": "DEFAULT_PATH_FOR_NON_LISTED_NODES", "paths": ["/opt/local-path-provisioner"]},
                {"node": "node-2", "paths": ["/mnt/a", "/mnt/b"]},
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


# helperPod.yaml from the same manifest, which names busybox with no tag.
UPSTREAM_HELPER_POD = """apiVersion: v1
kind: Pod
metadata:
  name: helper-pod
spec:
  priorityClassName: system-node-critical
  tolerations:
    - key: node.kubernetes.io/disk-pressure
      operator: Exists
      effect: NoSchedule
  containers:
  - name: helper-pod
    image: docker.io/library/busybox
    imagePullPolicy: IfNotPresent"""

HELPER_TEMPLATE = BOOTSTRAP / "templates" / "local-path-helper-pod.yaml"


def test_the_helper_pod_template_is_upstreams_with_only_the_image_pinned():
    """bootstrap.sh writes this over upstream's, so any other difference is a fork."""
    lines = [
        line for line in HELPER_TEMPLATE.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    image = [i for i, line in enumerate(lines) if line.strip().startswith("image:")]
    assert len(image) == 1
    pinned = lines[image[0]].split("image:", 1)[1].strip()
    assert pinned.startswith("docker.io/library/busybox:")
    assert "@sha256:" in pinned
    lines[image[0]] = "    image: docker.io/library/busybox"
    assert "\n".join(lines) == UPSTREAM_HELPER_POD


def test_bootstrap_writes_the_helper_template_into_the_configmap():
    """A template nothing applies would leave upstream's untagged image running."""
    script = (BOOTSTRAP / "bootstrap.sh").read_text(encoding="utf-8")
    assert '{"data": {"helperPod.yaml": sys.stdin.read()}}' in script
    assert '<"${TEMPLATES_DIR}/local-path-helper-pod.yaml"' in script


local_path_image = _load("local_path_image")

PINNED_DIGEST = "sha256:" + "1" * 64

# The provisioner Deployment's container from the same manifest, plus the helper
# pod's busybox line, which must come through untouched.
UPSTREAM_MANIFEST = """      containers:
        - name: local-path-provisioner
          image: docker.io/rancher/local-path-provisioner:v0.0.36
          imagePullPolicy: IfNotPresent
  helperPod.yaml: |-
      containers:
      - name: helper-pod
        image: docker.io/library/busybox
"""


def test_the_provisioner_image_is_pinned_and_nothing_else_moves():
    pinned = local_path_image.pin(UPSTREAM_MANIFEST, "v0.0.36", PINNED_DIGEST)
    want = UPSTREAM_MANIFEST.replace(
        "local-path-provisioner:v0.0.36", f"local-path-provisioner:v0.0.36@{PINNED_DIGEST}"
    )
    assert pinned == want


def test_a_manifest_that_does_not_name_the_pinned_tag_is_refused():
    """A moved upstream manifest must fail the install, not apply an unpinned image."""
    for manifest in (UPSTREAM_MANIFEST.replace("v0.0.36", "v0.0.37"), UPSTREAM_MANIFEST * 2):
        try:
            local_path_image.pin(manifest, "v0.0.36", PINNED_DIGEST)
        except ValueError:
            continue
        raise AssertionError("expected a ValueError")


def test_a_malformed_digest_is_refused():
    for digest in ("", "sha256:abc", "1" * 64, PINNED_DIGEST.upper()):
        try:
            local_path_image.pin(UPSTREAM_MANIFEST, "v0.0.36", digest)
        except ValueError:
            continue
        raise AssertionError(f"{digest!r} was accepted")


def test_bootstrap_applies_the_pinned_manifest_not_the_upstream_url():
    """Applying the URL directly would start a pod pulling the bare tag."""
    script = (BOOTSTRAP / "bootstrap.sh").read_text(encoding="utf-8")
    assert "services-digests.local-path-provisioner" in script
    assert 'local_path_image.py" --version "${LOCAL_PATH_VERSION}" --digest "${local_path_digest}"' in script
    assert "| kubectl apply -f -" in script
    assert "kubectl apply -f \"https://raw.githubusercontent.com/rancher/local-path-provisioner" not in script


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
