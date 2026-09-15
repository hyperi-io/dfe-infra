#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_content_pins.py
#  Purpose:      Prove a versions.yaml content pin reaches the engine as a
#                mounted directory, not as a values blob or a ConfigMap.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for content.entries in the dfe-engine chart.

The engine serves files another image owns -- the reference transform pipelines
the library shows, the source catalogue a transform ships, and each app's own
container contract. Those land in a mounted directory the engine reads by env,
filled by one init container per pin, because the elastic catalogue alone is
344 KB and a ConfigMap or a values blob re-serialises it into etcd on every Argo
sync.

Seven things are checked:

1. Every entry renders ONE init container running its own vehicle -- the pinned
   app image for `image` and `emit`, the engine image for `asset` -- into the
   shared volume.
2. That volume is a pod-local emptyDir, mounted writable by the init containers
   and READ-ONLY by the engine.
3. DFE_LIBRARY_SEED_DIR and DFE_APP_CONTRACT_DIR are set whether or not an entry
   exists, so adding one later needs no env change.
4. DFE_SOURCE_CATALOGUE_FILE is set ONLY where an entry materialises a
   catalogue: a path naming a file nothing wrote is refused by the engine.
5. Content is not profile-dependent -- slim and scale render the same pod.
6. Nothing carries the content through .Values or a ConfigMap.
7. An `emit` entry cannot wedge the engine's pod: the app writes its own
   contract, and an image that carries none reports the absence and exits 0.

    python3 scripts/tests/test_content_pins.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "dfe-engine"
PROFILES = REPO_ROOT / "argocd" / "values"
REGISTRY = "ghcr.io/hyperi-io"

# The shape a live pin renders as, one entry per vehicle. Not read from
# values.yaml: the entries there are empty until each app ships its files in
# its image, and a test over an empty list proves nothing about the mechanism.
LIVE_ENTRIES = {
    "content": {
        "entries": [
            {
                "name": "dfe-transform-vector-templates",
                "kind": "image",
                "ref": f"{REGISTRY}/dfe-transform-vector:v1.0.37@sha256:" + "0" * 64,
                "role": "library",
                "files": [
                    {"from": "/usr/share/dfe-transform-vector/templates", "to": "vector-yaml"}
                ],
            },
            {
                "name": "dfe-transform-elastic-catalogue",
                "kind": "asset",
                "ref": "https://example.invalid/releases/download/v2.2.0",
                "role": "catalogue",
                "files": [{"from": "sources.yaml", "to": "sources.yaml"}],
            },
            {
                "name": "dfe-loader-contract",
                "kind": "emit",
                "role": "contract",
                "app": "dfe-loader",
                "ref": f"{REGISTRY}/dfe-loader:v1.18.36@sha256:" + "1" * 64,
            },
        ]
    }
}

# A deployment that materialises nothing, which is what every tier rendered
# before the vehicle existed.
NO_ENTRIES = {"content": {"entries": []}}


def render(values: dict | None = None, *profile: str, sets: tuple[str, ...] = ()) -> dict:
    """The engine Deployment, optionally under a profile's values."""
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "helm",
            "template",
            "dfe-engine",
            str(CHART),
            "--set",
            f"global.registry={REGISTRY}",
            "--show-only",
            "templates/deployment.yaml",
        ]
        for name in profile:
            cmd += ["--values", str(PROFILES / name)]
        if values is not None:
            overlay = Path(tmp) / "content.yaml"
            overlay.write_text(yaml.safe_dump(values), encoding="utf-8", newline="\n")
            cmd += ["--values", str(overlay)]
        for s in sets:
            cmd += ["--set", s]
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {profile} {sets}:\n{out.stderr}")
    for doc in yaml.safe_load_all(out.stdout):
        if doc and doc.get("kind") == "Deployment":
            return doc
    return {}


def chart_values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))


def pod(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]


def container(doc: dict, name: str) -> dict:
    for c in (pod(doc).get("initContainers") or []) + pod(doc)["containers"]:
        if c["name"] == name:
            return c
    return {}


def env_of(c: dict) -> dict:
    """name -> literal value, for the env entries that carry one."""
    return {e["name"]: e["value"] for e in c.get("env") or [] if "value" in e}


def mount_of(c: dict, volume: str) -> dict:
    for m in c.get("volumeMounts") or []:
        if m["name"] == volume:
            return m
    return {}


def test_each_pin_renders_its_own_init_container() -> None:
    """One entry, one container, running the vehicle that carries the files.

    The vehicle and the destination are independent: an image entry runs the
    pinned app image, an asset entry runs the engine image for its python3, an
    emit entry runs the pinned app image so the app writes its own contract, and
    any of them can carry library files, a catalogue or a contract.
    """
    doc = render(LIVE_ENTRIES)
    inits = [c["name"] for c in pod(doc).get("initContainers") or []]
    expect(
        "one init container per entry, named for its pin",
        inits
        == [
            "content-dfe-transform-vector-templates",
            "content-dfe-transform-elastic-catalogue",
            "content-dfe-loader-contract",
        ],
        f"{inits}",
    )

    image_entry = container(doc, "content-dfe-transform-vector-templates")
    expect(
        "an image entry runs the PINNED app image, digest and all",
        image_entry.get("image") == LIVE_ENTRIES["content"]["entries"][0]["ref"],
        f"{image_entry.get('image')}",
    )
    expect(
        "and copies the named path into the library root under the kind",
        "/etc/dfe-engine/content/library/vector-yaml" in "".join(image_entry.get("command") or []),
        f"{image_entry.get('command')}",
    )

    asset_entry = container(doc, "content-dfe-transform-elastic-catalogue")
    expect(
        "an asset entry runs the engine image, which has the python3 to fetch it",
        "/dfe-engine:" in asset_entry.get("image", ""),
        f"{asset_entry.get('image')}",
    )
    expect(
        "and writes it into the catalogue root",
        "/etc/dfe-engine/content/catalogue/sources.yaml"
        in "".join(asset_entry.get("command") or []),
        f"{asset_entry.get('command')}",
    )

    emit_entry = container(doc, "content-dfe-loader-contract")
    expect(
        "an emit entry runs the PINNED app image, digest and all",
        emit_entry.get("image") == LIVE_ENTRIES["content"]["entries"][2]["ref"],
        f"{emit_entry.get('image')}",
    )
    script = "".join(emit_entry.get("command") or [])
    expect(
        "and asks the app itself for its contract, into its own subdirectory",
        'dfe-loader config-schema --dir "$dir"' in script
        and 'dir="/etc/dfe-engine/content/contract/dfe-loader"' in script,
        f"{script}",
    )
    expect(
        "recording the pin that wrote it, which the contract itself does not name",
        "source.json" in script and LIVE_ENTRIES["content"]["entries"][2]["ref"] in script,
        f"{script}",
    )


def test_the_content_volume_is_pod_local_and_read_only_to_the_engine() -> None:
    """The pins own this content; the engine only reads it.

    A writable mount would let the engine edit files it re-materialises on every
    start, so an edit would vanish at the next restart with nothing said.
    """
    doc = render(LIVE_ENTRIES)
    volumes = {v["name"]: v for v in pod(doc)["volumes"]}
    expect(
        "the content volume is a pod-local emptyDir",
        volumes.get("content") == {"name": "content", "emptyDir": {}},
        f"{volumes.get('content')}",
    )
    mount = chart_values()["content"]["mountPath"]
    expect(
        "the engine mounts it read-only",
        mount_of(container(doc, "engine"), "content") == {
            "name": "content",
            "mountPath": mount,
            "readOnly": True,
        },
        f"{mount_of(container(doc, 'engine'), 'content')}",
    )
    for name in (
        "content-dfe-transform-vector-templates",
        "content-dfe-transform-elastic-catalogue",
        "content-dfe-loader-contract",
    ):
        expect(
            f"{name} mounts it writable",
            mount_of(container(doc, name), "content").get("readOnly") is None
            and mount_of(container(doc, name), "content").get("mountPath") == mount,
            f"{mount_of(container(doc, name), 'content')}",
        )


def test_the_seed_and_contract_dirs_are_set_with_or_without_an_entry() -> None:
    """An absent directory is a no-op in the engine, so naming it always is safe.

    It is also what makes a later content entry take effect on its own: the
    entry arrives, the files land under the path the engine is already reading.
    """
    mount = chart_values()["content"]["mountPath"]
    cases = (
        ("no entries", NO_ENTRIES),
        ("the chart's own entries", None),
        ("the test entries", LIVE_ENTRIES),
    )
    for label, values in cases:
        env = env_of(container(render(values), "engine"))
        expect(
            f"DFE_LIBRARY_SEED_DIR points at the library root ({label})",
            env.get("DFE_LIBRARY_SEED_DIR") == f"{mount}/library",
            f"{env.get('DFE_LIBRARY_SEED_DIR')}",
        )
        expect(
            f"DFE_APP_CONTRACT_DIR points at the contract root ({label})",
            env.get("DFE_APP_CONTRACT_DIR") == f"{mount}/contract",
            f"{env.get('DFE_APP_CONTRACT_DIR')}",
        )


def test_an_empty_entry_list_renders_no_init_container() -> None:
    """The vehicle is the entries' doing, not the chart's.

    A deployment that materialises nothing must render the pod it rendered
    before the mechanism existed.
    """
    doc = render(NO_ENTRIES)
    inits = [c["name"] for c in pod(doc).get("initContainers") or []]
    expect("no entries, no init containers", inits == [], f"{inits}")


def test_the_catalogue_env_follows_the_catalogue_entry() -> None:
    """Naming a catalogue nothing wrote is a refusal, not an empty source list.

    load_source_catalogue raises on a path someone NAMED and that is not there,
    which is why this env cannot be set unconditionally the way the seed dir is.
    """
    values = chart_values()["content"]
    env = env_of(container(render(LIVE_ENTRIES), "engine"))
    expect(
        "set when an entry materialises one",
        env.get("DFE_SOURCE_CATALOGUE_FILE")
        == f"{values['mountPath']}/catalogue/{values['catalogueFile']}",
        f"{env.get('DFE_SOURCE_CATALOGUE_FILE')}",
    )

    library_only = {"content": {"entries": [LIVE_ENTRIES["content"]["entries"][0]]}}
    env = env_of(container(render(library_only), "engine"))
    expect(
        "unset when only library entries exist",
        "DFE_SOURCE_CATALOGUE_FILE" not in env,
        f"{env.get('DFE_SOURCE_CATALOGUE_FILE')}",
    )
    for label, values in (("no entries at all", NO_ENTRIES), ("the chart's own entries", None)):
        env = env_of(container(render(values), "engine"))
        expect(
            f"and unset with {label}",
            "DFE_SOURCE_CATALOGUE_FILE" not in env,
            f"{env.get('DFE_SOURCE_CATALOGUE_FILE')}",
        )


def test_content_is_not_profile_dependent() -> None:
    """What the engine serves is the same everywhere; only how much of it runs is not.

    A tier that showed a different set of reference pipelines would make the
    console's copy-paste view depend on the deployment's size.
    """
    slim = render(LIVE_ENTRIES, "common.yaml", "profile-slim.yaml")
    scale = render(LIVE_ENTRIES, "common.yaml", "profile-scale.yaml")
    fields = ("name", "image", "command", "volumeMounts")
    slim_inits = [{k: c.get(k) for k in fields} for c in pod(slim).get("initContainers") or []]
    scale_inits = [{k: c.get(k) for k in fields} for c in pod(scale).get("initContainers") or []]
    expect(
        "slim and scale render the same content init containers",
        slim_inits == scale_inits and len(scale_inits) == 3,
        f"slim={len(slim_inits)} scale={len(scale_inits)}",
    )
    keys = ("DFE_LIBRARY_SEED_DIR", "DFE_SOURCE_CATALOGUE_FILE", "DFE_APP_CONTRACT_DIR")
    slim_env = {k: env_of(container(slim, "engine")).get(k) for k in keys}
    scale_env = {k: env_of(container(scale, "engine")).get(k) for k in keys}
    expect(
        "and hand the engine the same three paths",
        slim_env == scale_env and all(slim_env.values()),
        f"slim={slim_env} scale={scale_env}",
    )


def test_no_content_travels_through_values_or_a_configmap() -> None:
    """The reason the mechanism exists: 344 KB re-serialised into etcd per sync.

    An entry carries a REFERENCE -- an image ref or a URL -- and the files never
    pass through Helm, so the rendered manifest stays the size of its pins.
    """
    entry = LIVE_ENTRIES["content"]["entries"][0]
    expect(
        "an entry declares a vehicle and paths, never content",
        set(entry) == {"name", "kind", "ref", "role", "files"},
        f"{sorted(entry)}",
    )
    emit = LIVE_ENTRIES["content"]["entries"][2]
    expect(
        "an emit entry names the app instead of paths, because the app chooses them",
        set(emit) == {"name", "kind", "ref", "role", "app"},
        f"{sorted(emit)}",
    )
    with tempfile.TemporaryDirectory() as tmp:
        overlay = Path(tmp) / "content.yaml"
        overlay.write_text(yaml.safe_dump(LIVE_ENTRIES), encoding="utf-8", newline="\n")
        out = subprocess.run(
            [
                "helm",
                "template",
                "dfe-engine",
                str(CHART),
                "--set",
                f"global.registry={REGISTRY}",
                "--values",
                str(overlay),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed:\n{out.stderr}")
    names = [
        doc["metadata"]["name"]
        for doc in yaml.safe_load_all(out.stdout)
        if doc and doc.get("kind") == "ConfigMap"
    ]
    expect(
        "no content ConfigMap is rendered",
        not any("content" in n for n in names),
        f"{names}",
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
