#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_hunt_runner_gitsync.py
#  Purpose:      Prove the hunt runner's hunts and rules come off a volume the
#                git-sync sidecar fills, not off an emptyDir nothing writes.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for huntRunner.gitSync in the dfe-engine chart.

The runner reads hunt and rule YAML off disk (dfe_engine.hunt_runner.spec_loader,
on every reload tick), so a directory nothing fills means no hunt has ever run
(dfe-infra#212). A template that renders is not evidence of that: the values are.

Seven things are checked:

1. The sidecar reaches the SAME repo, branch and credential as the engine, off
   `gitops.*`. A second copy of any of the three could point somewhere else.
2. The FIRST sync is bounded. The sidecar retries forever, which on its own
   reproduces #212: a bad credential leaves the link absent and the runner
   Running with zero hunts, so an init container has to fail the pod instead.
3. The runner's DFE_HUNTS_DIR and DFE_HUNTS_RULES_DIR resolve THROUGH git-sync's
   link into the synced subtree, on a volume both containers mount.
4. The sidecar is hardened like the runner: non-root pod context, read-only
   rootfs, all capabilities dropped, and its own HOME rather than the runner's.
5. Disabled, the pod is a single container again and nothing references the
   deploy repo -- the pre-#212 shape, so the switch is a real off.
6. No deploy repo (gitops.enabled=false) means no sidecar: there is nothing to
   sync and the credential Secret does not exist.
7. The pod waits for the engine to report ready before the runner starts. The
   runner exits without the coordination tables the engine's schema phase
   creates, and it shares the engine's sync wave, so the wait is the only thing
   holding it back. git-sync-init joins the same initContainers list.

    python3 scripts/tests/test_hunt_runner_gitsync.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "dfe-engine"
REGISTRY = "ghcr.io/hyperi-io"


def render(*sets: str) -> dict:
    """The hunt-runner Deployment, or {} when the template renders nothing."""
    cmd = [
        "helm",
        "template",
        "dfe-engine",
        str(CHART),
        "--set",
        f"global.registry={REGISTRY}",
        "--show-only",
        "templates/hunt-runner.yaml",
    ]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {sets}:\n{out.stderr}")
    for doc in yaml.safe_load_all(out.stdout):
        if doc and doc.get("kind") == "Deployment":
            return doc
    return {}


def chart_values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))


def pod(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]


def container(doc: dict, name: str) -> dict:
    for c in pod(doc)["containers"] + (pod(doc).get("initContainers") or []):
        if c["name"] == name:
            return c
    return {}


def init_names(doc: dict) -> list[str]:
    return [c["name"] for c in pod(doc).get("initContainers") or []]


def env_of(c: dict) -> dict:
    """name -> literal value, for the env entries that carry one."""
    return {e["name"]: e["value"] for e in c.get("env") or [] if "value" in e}


def secret_refs(c: dict) -> dict:
    """name -> (secret, key), for the env entries that come from a Secret."""
    found = {}
    for e in c.get("env") or []:
        ref = (e.get("valueFrom") or {}).get("secretKeyRef")
        if ref:
            found[e["name"]] = (ref["name"], ref["key"])
    return found


def flag(c: dict, name: str) -> str | None:
    """The value of a `--name=value` arg, or None."""
    for arg in c.get("args") or []:
        if arg.startswith(f"--{name}="):
            return arg.split("=", 1)[1]
    return None


def mount_of(c: dict, volume: str) -> dict:
    for m in c.get("volumeMounts") or []:
        if m["name"] == volume:
            return m
    return {}


def test_the_sidecar_syncs_the_engines_own_deploy_repo() -> None:
    """Repo, branch and credential are the gitops.* values, not a second copy.

    The appset injects gitops.repoUrl and gitops.branch from the cluster-secret
    annotations so the engine's write target equals the repo Argo reads. A pin
    of its own here would let the runner watch a repo nobody writes.
    """
    values = chart_values()
    sidecar = container(render(), "git-sync")
    expect("the sidecar renders by default", bool(sidecar), "no git-sync container")
    if not sidecar:
        return

    expect(
        "--repo is gitops.repoUrl",
        flag(sidecar, "repo") == values["gitops"]["repoUrl"],
        f"{flag(sidecar, 'repo')}",
    )
    expect(
        "--ref is gitops.branch",
        flag(sidecar, "ref") == values["gitops"]["branch"],
        f"{flag(sidecar, 'ref')}",
    )
    refs = secret_refs(sidecar)
    want_secret = values["gitops"]["credentialsSecret"]
    expect(
        "it reuses the engine's deploy-repo credential, both keys",
        refs.get("GITSYNC_USERNAME") == (want_secret, "username")
        and refs.get("GITSYNC_PASSWORD") == (want_secret, "password"),
        f"{refs}",
    )
    expect(
        "a fetch failure retries instead of killing the pod",
        flag(sidecar, "max-failures") == "-1",
        f"{flag(sidecar, 'max-failures')}",
    )
    expect(
        "the sync period is inside the runner's 5-minute reload",
        flag(sidecar, "period") == "30s",
        f"{flag(sidecar, 'period')}",
    )


def test_the_first_sync_is_bounded() -> None:
    """#212's shape again: retry-forever on the FIRST sync hides a bad credential.

    With only the sidecar the link never appears, the runner loads zero hunts and
    the pod stays Running, so the init container is what turns that into a pod
    that never starts.
    """
    values = chart_values()
    gs = values["huntRunner"]["gitSync"]
    doc = render()
    init = container(doc, "git-sync-init")
    expect("an init container renders by default", bool(init), "no git-sync-init")
    if not init:
        return

    expect(
        "it syncs once and exits",
        "--one-time" in (init.get("args") or []),
        f"{init.get('args')}",
    )
    expect(
        "its failures are counted, not retried forever",
        flag(init, "max-failures") == str(gs["initialMaxFailures"])
        and gs["initialMaxFailures"] > 0,
        f"{flag(init, 'max-failures')}",
    )
    expect(
        "the sidecar still retries forever, once the first sync landed",
        flag(container(doc, "git-sync"), "max-failures") == "-1",
        f"{flag(container(doc, 'git-sync'), 'max-failures')}",
    )
    expect(
        "it seeds the worktree the sidecar and the runner then use",
        flag(init, "root") == f"{gs['mountPath']}/repo" and flag(init, "link") == gs["link"],
        f"{flag(init, 'root')} {flag(init, 'link')}",
    )
    expect(
        "off the same repo, ref and credential as the sidecar",
        flag(init, "repo") == values["gitops"]["repoUrl"]
        and flag(init, "ref") == values["gitops"]["branch"]
        and secret_refs(init) == secret_refs(container(doc, "git-sync")),
        f"{init.get('args')} {secret_refs(init)}",
    )
    expect(
        "and it mounts the volume writable",
        mount_of(init, "deploy-repo").get("readOnly") is None
        and mount_of(init, "deploy-repo").get("mountPath") == gs["mountPath"],
        f"{init.get('volumeMounts')}",
    )


def test_the_runner_reads_hunts_through_the_synced_link() -> None:
    """The whole point of #212: the paths must land INSIDE the synced worktree.

    git-sync writes <root>/<link>, so a path that misses the link reads a
    directory that only ever exists mid-fetch, or none at all.
    """
    values = chart_values()
    gs = values["huntRunner"]["gitSync"]
    doc = render()
    runner = container(doc, "hunt-runner")
    env = env_of(runner)

    root = f"{gs['mountPath']}/repo"
    link = f"{root}/{gs['link']}"
    expect(
        "DFE_HUNTS_DIR resolves through the link into the deploy repo",
        env.get("DFE_HUNTS_DIR") == f"{link}/{gs['huntsPath']}",
        f"{env.get('DFE_HUNTS_DIR')}",
    )
    expect(
        "DFE_HUNTS_RULES_DIR does too",
        env.get("DFE_HUNTS_RULES_DIR") == f"{link}/{gs['rulesPath']}",
        f"{env.get('DFE_HUNTS_RULES_DIR')}",
    )
    expect(
        "git-sync's root is a subdirectory, not the volume root",
        flag(container(doc, "git-sync"), "root") == root,
        f"{flag(container(doc, 'git-sync'), 'root')}",
    )
    expect(
        "and the link it publishes is the one those paths cross",
        flag(container(doc, "git-sync"), "link") == gs["link"],
        f"{flag(container(doc, 'git-sync'), 'link')}",
    )

    expect(
        "the runner mounts the synced volume read-only",
        mount_of(runner, "deploy-repo").get("readOnly") is True,
        f"{mount_of(runner, 'deploy-repo')}",
    )
    expect(
        "the sidecar mounts the same volume writable",
        mount_of(container(doc, "git-sync"), "deploy-repo").get("readOnly") is None,
        f"{mount_of(container(doc, 'git-sync'), 'deploy-repo')}",
    )
    volumes = {v["name"]: v for v in pod(doc)["volumes"]}
    expect(
        "and that volume is a pod-local emptyDir",
        volumes.get("deploy-repo") == {"name": "deploy-repo", "emptyDir": {}},
        f"{volumes.get('deploy-repo')}",
    )


def test_the_runner_leaves_the_schemas_dir_to_the_image() -> None:
    """It pointed at the same empty volume, and no process in this pod seeds one.

    The engine daemon runs the bootstrap that fills a schemas directory; the
    runner does not, so an override here only shadows the tree the image ships.
    """
    env = env_of(container(render(), "hunt-runner"))
    expect(
        "DFE_SCHEMAS_DIR is not set on the runner",
        "DFE_SCHEMAS_DIR" not in env,
        f"{env.get('DFE_SCHEMAS_DIR')}",
    )


def test_the_sidecar_is_hardened_like_the_runner() -> None:
    """A sidecar with a writable rootfs would be the softest thing in the pod."""
    doc = render()
    sidecar = container(doc, "git-sync")
    sec = sidecar.get("securityContext", {})
    expect(
        "read-only rootfs, no privilege escalation, all capabilities dropped",
        sec.get("readOnlyRootFilesystem") is True
        and sec.get("allowPrivilegeEscalation") is False
        and sec.get("capabilities", {}).get("drop") == ["ALL"],
        f"{sec}",
    )
    expect(
        "it runs under the pod's non-root context",
        pod(doc)["securityContext"]["runAsNonRoot"] is True,
        f"{pod(doc)['securityContext']}",
    )
    home = chart_values()["huntRunner"]["gitSync"]["homePath"]
    expect(
        "it writes git config to its OWN HOME volume, not the runner's scratch",
        env_of(sidecar).get("HOME") == home
        and mount_of(sidecar, "git-sync-home").get("mountPath") == home
        and mount_of(sidecar, "tmp") == {},
        f"{env_of(sidecar).get('HOME')} {sidecar.get('volumeMounts')}",
    )
    expect(
        "and that HOME is a pod-local emptyDir",
        {"name": "git-sync-home", "emptyDir": {}} in pod(doc)["volumes"],
        f"{pod(doc)['volumes']}",
    )
    # git-sync creates its gitconfig with os.CreateTemp, which reads TMPDIR and
    # falls back to /tmp -- read-only here, so the first sync died on it.
    for name in ("git-sync", "git-sync-init"):
        expect(
            f"{name} points TMPDIR at the same writable volume",
            env_of(container(doc, name)).get("TMPDIR") == home,
            f"{env_of(container(doc, name)).get('TMPDIR')}",
        )


def test_disabled_restores_the_single_container_pod() -> None:
    """The off switch has to be a real off, not a rename of the same pod."""
    doc = render("huntRunner.gitSync.enabled=false")
    names = [c["name"] for c in pod(doc)["containers"]]
    expect("only the runner remains", names == ["hunt-runner"], f"{names}")
    expect(
        "and no git-sync init container either",
        init_names(doc) == ["wait-for-engine"],
        f"{init_names(doc)}",
    )
    expect(
        "no deploy-repo volume is declared",
        "deploy-repo" not in {v["name"] for v in pod(doc)["volumes"]},
        f"{pod(doc)['volumes']}",
    )
    env = env_of(container(doc, "hunt-runner"))
    expect(
        "the hunt directories fall back to the config mount",
        env.get("DFE_HUNTS_DIR") == "/config/hunts"
        and env.get("DFE_HUNTS_RULES_DIR") == "/config/rules",
        f"{env.get('DFE_HUNTS_DIR')} {env.get('DFE_HUNTS_RULES_DIR')}",
    )


def test_no_deploy_repo_means_no_sidecar() -> None:
    """gitops.enabled=false leaves no repo to sync and no credential Secret.

    Rendering the sidecar anyway would wedge the pod in
    CreateContainerConfigError on a Secret nothing creates.
    """
    doc = render("gitops.enabled=false")
    names = [c["name"] for c in pod(doc)["containers"]]
    expect("the sidecar is not rendered", names == ["hunt-runner"], f"{names}")
    expect(
        "nor the init container that would wedge the pod on it",
        init_names(doc) == ["wait-for-engine"],
        f"{init_names(doc)}",
    )


def test_the_runner_waits_for_the_engine() -> None:
    """A fresh deploy restarted the runner on SchemaNotAppliedError until the schema landed."""
    values = chart_values()
    doc = render()
    expect(
        "the engine wait runs first, then the first sync",
        init_names(doc) == ["wait-for-engine", "git-sync-init"],
        f"{init_names(doc)}",
    )
    script = (container(doc, "wait-for-engine").get("command") or [""])[-1]
    # render() names no namespace, so the release's is helm's `default`.
    want = f"http://dfe-engine.default.svc.cluster.local:{values['service']['port']}/readyz"
    expect("it polls the engine Service's /readyz", f'URL="{want}"' in script, script)
    expect(
        "on the engine chart's own Service port",
        values["waitForEngine"]["port"] == values["service"]["port"],
        f"{values['waitForEngine']['port']} vs {values['service']['port']}",
    )
    off = render("waitForEngine.enabled=false")
    expect(
        "switched off, only the first sync is left",
        init_names(off) == ["git-sync-init"],
        f"{init_names(off)}",
    )
    bare = render("waitForEngine.enabled=false", "huntRunner.gitSync.enabled=false")
    expect(
        "and with neither, the pod has no initContainers key",
        "initContainers" not in pod(bare),
        f"{pod(bare).get('initContainers')}",
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
