#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_content_pins.py
#  Purpose:      Prove a versions.yaml content pin reaches the engine as a
#                mounted directory, not as a values blob or a ConfigMap.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for content.entries on the deployed dfe-engine Application.

The engine serves files another image owns -- each app's own container contract.
Those land in a mounted directory the engine reads by env, filled by one init
container per pin, because a ConfigMap or a values blob re-serialises them into
etcd on every Argo sync.

The entries and their init containers are argocd/values/apps/dfe-engine/values.yaml's,
rendered by the engine's thin chart; dfe-extras reads the entries' roles for the
env it hands the engine, and refuses an entry with no init container of its name.
A container whose entry is dropped runs the engine image and materialises nothing.
Eight things are checked, beside those two:

1. Every entry renders ONE init container, named for its pin, running the pinned
   app image, and a moved pin moves its container.
2. That volume is a pod-local emptyDir, mounted writable by the init containers
   and READ-ONLY by the engine.
3. DFE_LIBRARY_SEED_DIR and DFE_APP_CONTRACT_DIR are set whether or not an entry
   exists, so adding one later needs no env change.
4. Every content init container in a deployed render runs an image its entry pins.
5. Content is not profile-dependent -- every tier renders the same init containers.
6. Nothing carries the content through .Values or a ConfigMap.
7. An `emit` contract entry cannot wedge the engine's pod: the app writes its own
   contract, and an image that carries none reports the absence and exits 0.

    python3 scripts/tests/test_content_pins.py

Needs `helm` on PATH; the cases that render the engine pod need the scalo-service
library (_weave.library). No test runner, matching the other checks here.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _expect import expect, standalone, summary
from _gate import CLOUDS, PROFILES, golden
from _weave import REPO_ROOT, render_app, render_extras, weave

ENGINE = "dfe-engine"
ENGINE_VALUES = REPO_ROOT / "argocd" / "values" / "apps" / ENGINE / "values.yaml"
CONTENT_PREFIX = "content-"
DROPPED_ENTRY = "dfe-transform-elastic-contract"
# A pin no release carries, so a container running it can only have read it here.
MOVED_REF = "registry.example.com/dfe/dfe-loader:v0.0.1@sha256:" + "1" * 64

def content_values() -> dict:
    """The engine's content block as its integration values deploy it."""
    return yaml.safe_load(ENGINE_VALUES.read_text(encoding="utf-8"))["content"]


def entries() -> list[dict]:
    return content_values()["entries"]


def engine(docs: list[dict]) -> dict:
    return next(
        d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == ENGINE
    )


def deployed(profile: str = "slim", cloud: str = "local") -> dict:
    """The engine Deployment a default deployment renders."""
    return engine(golden(ENGINE, profile, cloud))


def pod(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]


def container(doc: dict, name: str) -> dict:
    for c in (pod(doc).get("initContainers") or []) + pod(doc)["containers"]:
        if c["name"] == name:
            return c
    return {}


def content_inits(doc: dict) -> list[dict]:
    return [c for c in pod(doc).get("initContainers") or [] if c["name"].startswith(CONTENT_PREFIX)]


def env_data(docs: list[dict]) -> dict[str, str]:
    """The literal env dfe-extras hands the engine, from its dfe-engine-env ConfigMap."""
    return next(
        d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == f"{ENGINE}-env"
    )["data"]


def literal_env(content: dict | None = None) -> dict[str, str]:
    """The literal env the engine is handed under an instance's content block."""
    overlay = {"content": content} if content is not None else None
    return env_data(render_extras(ENGINE, "slim", "local", overlay=overlay))


def deployed_env() -> dict[str, str]:
    """The literal env a default deployment hands the engine."""
    return env_data(golden(ENGINE, "slim", "local"))


def mount_of(c: dict, volume: str) -> dict:
    for m in c.get("volumeMounts") or []:
        if m["name"] == volume:
            return m
    return {}


def test_each_pin_renders_its_own_init_container() -> None:
    """One entry, one container, running the vehicle that carries the files.

    An emit entry runs the pinned app image so the app writes its own contract,
    """
    doc = deployed()
    mount = content_values()["mountPath"]
    inits = {c["name"]: c for c in content_inits(doc)}
    expect(
        "one init container per entry, named for its pin, in entry order",
        list(inits) == [f"{CONTENT_PREFIX}{e['name']}" for e in entries()],
        f"{list(inits)}",
    )
    for entry in entries():
        init = inits.get(f"{CONTENT_PREFIX}{entry['name']}", {})
        expect(
            f"{entry['name']} runs the PINNED app image, digest and all",
            init.get("image") == entry["ref"],
            f"{init.get('image')}",
        )
        if entry["role"] != "contract":
            continue
        script = "".join(init.get("command") or [])
        app = entry["app"]
        expect(
            f"{entry['name']} asks the app itself for its contract, into its own subdirectory",
            f'{app} config-schema --dir "$dir"' in script
            and f'dir="{mount}/contract/{app}"' in script,
            f"{script}",
        )
        expect(
            f"{entry['name']} records the pin that wrote it, which the contract does not name",
            "source.json" in script and entry["ref"] in script,
            f"{script}",
        )

    moved = [dict(e, ref=MOVED_REF) if e["name"] == "dfe-loader-contract" else e for e in entries()]
    doc = engine(render_app(ENGINE, "slim", "local", overlay={"content": {"entries": moved}}))
    loader = container(doc, "content-dfe-loader-contract")
    expect(
        "a moved pin moves its own container's image and the pin it records",
        loader.get("image") == MOVED_REF and MOVED_REF in "".join(loader.get("command") or []),
        f"{loader.get('image')}",
    )
    expect(
        "and no other container's",
        all(
            c["image"] == e["ref"]
            for c, e in zip(content_inits(doc), entries(), strict=True)
            if e["name"] != "dfe-loader-contract"
        ),
        f"{[c['image'] for c in content_inits(doc)]}",
    )


def test_the_content_volume_is_pod_local_and_read_only_to_the_engine() -> None:
    """The pins own this content; the engine only reads it.

    A writable mount would let the engine edit files it re-materialises on every
    start, so an edit would vanish at the next restart with nothing said.
    """
    doc = deployed()
    volumes = {v["name"]: v for v in pod(doc)["volumes"]}
    expect(
        "the content volume is a pod-local emptyDir",
        volumes.get("content") == {"name": "content", "emptyDir": {}},
        f"{volumes.get('content')}",
    )
    mount = content_values()["mountPath"]
    main = pod(doc)["containers"][0]
    expect(
        "the engine mounts it read-only",
        mount_of(main, "content") == {"name": "content", "mountPath": mount, "readOnly": True},
        f"{mount_of(main, 'content')}",
    )
    for init in content_inits(doc):
        expect(
            f"{init['name']} mounts it writable",
            mount_of(init, "content").get("readOnly") is None
            and mount_of(init, "content").get("mountPath") == mount,
            f"{mount_of(init, 'content')}",
        )


def test_the_seed_and_contract_dirs_are_set_with_or_without_an_entry() -> None:
    """An absent directory is a no-op in the engine, so naming it always is safe.

    It is also what makes a later content entry take effect on its own: the
    entry arrives, the files land under the path the engine is already reading.
    """
    mount = content_values()["mountPath"]
    cases = (
        ("no entries", literal_env({"entries": []})),
        ("the deployed entries", deployed_env()),
        ("contract entries alone", literal_env({"entries": entries()})),
    )
    for label, env in cases:
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


def test_every_content_init_container_has_its_entry() -> None:
    """The vehicle is the entries' doing: every container the deployed entries keep
    runs a non-empty image."""
    for profile in PROFILES:
        for cloud in CLOUDS:
            images = [c.get("image") for c in content_inits(deployed(profile, cloud))]
            expect(
                f"{profile}/{cloud}: every content init container runs a pinned image",
                bool(images) and all(images),
                f"{images}",
            )




def test_content_is_not_profile_dependent() -> None:
    """What the engine serves is the same everywhere; only how much of it runs is not.

    A tier that showed a different set of reference pipelines would make the
    console's copy-paste view depend on the deployment's size.
    """
    fields = ("name", "image", "command", "volumeMounts")
    keys = ("DFE_LIBRARY_SEED_DIR", "DFE_SOURCE_CATALOGUE_FILE", "DFE_APP_CONTRACT_DIR")

    def shape(profile: str) -> tuple[list[dict], dict]:
        docs = golden(ENGINE, profile, "local")
        inits = [{k: c.get(k) for k in fields} for c in content_inits(engine(docs))]
        literal = env_data(docs)
        return inits, {k: literal.get(k) for k in keys}

    slim_inits, slim_env = shape("slim")
    for profile in PROFILES:
        inits, env = shape(profile)
        expect(
            f"slim and {profile} render the same content init containers",
            inits == slim_inits and len(inits) == len(entries()),
            f"slim={len(slim_inits)} {profile}={len(inits)}",
        )
        expect(
            f"and hand the engine the same three paths on {profile}",
            env == slim_env and all(env.values()),
            f"slim={slim_env} {profile}={env}",
        )


def test_no_content_travels_through_values_or_a_configmap() -> None:
    """The reason the mechanism exists: 344 KB re-serialised into etcd per sync.

    An entry carries a REFERENCE -- an image ref -- and the files never pass
    through Helm, so the rendered manifest stays the size of its pins.
    """
    for entry in entries():
        expect(
            f"{entry['name']} names the app instead of paths, because the app chooses them",
            set(entry) == {"name", "kind", "ref", "role", "app"} and entry["kind"] == "emit",
            f"{sorted(entry)}",
        )
        expect(
            f"{entry['name']} carries a reference pinned by digest, never content",
            "@sha256:" in entry["ref"],
            f"{entry['ref']}",
        )
    for profile in PROFILES:
        for cloud in CLOUDS:
            names = [
                d["metadata"]["name"]
                for d in golden(ENGINE, profile, cloud)
                if d.get("kind") == "ConfigMap"
            ]
            expect(
                f"{profile}/{cloud}: no content ConfigMap is rendered",
                not any("content" in n for n in names),
                f"{names}",
            )




def test_a_dropped_entry_leaves_its_container_materialising_nothing() -> None:
    """The container list is fixed, so a container whose entry is gone runs the
    engine's own image and exits before it calls an app that image does not carry."""
    dropped = [e for e in entries() if e["name"] != DROPPED_ENTRY]
    doc = engine(render_app(ENGINE, "slim", "local", overlay={"content": {"entries": dropped}}))
    init = container(doc, f"{CONTENT_PREFIX}{DROPPED_ENTRY}")
    expect(
        "the container runs the engine's own image",
        bool(init) and init.get("image") == pod(doc)["containers"][0]["image"],
        f"{init.get('image')}",
    )
    # An absolute shell, because the PATH below holds no app and no shell either.
    busybox = shutil.which("busybox")
    shell = [busybox, "sh"] if busybox else ["/bin/sh"]
    done = subprocess.run(
        [*shell, "-c", (init.get("command") or ["", "", "exit 1"])[2]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"PATH": "/nonexistent"},
        check=False,
        timeout=60,
    )
    expect(
        "and exits 0 without calling the app",
        done.returncode == 0 and "not in content.entries" in done.stdout,
        f"rc={done.returncode} {done.stdout}{done.stderr}",
    )


def test_an_entry_with_no_container_is_refused() -> None:
    """Nothing would fill its directory, so the render names the container it lacks."""
    added = [
        *entries(),
        {
            "name": "dfe-example-contract",
            "kind": "emit",
            "role": "contract",
            "app": "dfe-example",
            "ref": MOVED_REF,
        },
    ]
    try:
        render_extras(ENGINE, "slim", "local", overlay={"content": {"entries": added}})
        refused = ""
    except weave().WeaveError as exc:
        refused = str(exc)
    expect(
        "an entry no init container materialises is refused by name",
        "content-dfe-example-contract" in refused,
        f"refusal={refused[-300:]!r}",
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
