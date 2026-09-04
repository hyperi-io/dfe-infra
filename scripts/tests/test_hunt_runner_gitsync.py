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

Five things are checked:

1. The sidecar reaches the SAME repo, branch and credential as the engine, off
   `gitops.*`. A second copy of any of the three could point somewhere else.
2. The runner's DFE_HUNTS_DIR and DFE_HUNTS_RULES_DIR resolve THROUGH git-sync's
   link into the synced subtree, on a volume both containers mount.
3. The sidecar is hardened like the runner: non-root pod context, read-only
   rootfs, all capabilities dropped.
4. Disabled, the pod is a single container again and nothing references the
   deploy repo -- the pre-#212 shape, so the switch is a real off.
5. No deploy repo (gitops.enabled=false) means no sidecar: there is nothing to
   sync and the credential Secret does not exist.

    python3 scripts/tests/test_hunt_runner_gitsync.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "dfe-engine"
REGISTRY = "ghcr.io/hyperi-io"

_failures = 0


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


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
    for c in pod(doc)["containers"]:
        if c["name"] == name:
            return c
    return {}


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
    expect(
        "and gets the writable /tmp its read-only rootfs needs for git config",
        mount_of(sidecar, "tmp").get("mountPath") == "/tmp",
        f"{sidecar.get('volumeMounts')}",
    )


def test_disabled_restores_the_single_container_pod() -> None:
    """The off switch has to be a real off, not a rename of the same pod."""
    doc = render("huntRunner.gitSync.enabled=false")
    names = [c["name"] for c in pod(doc)["containers"]]
    expect("only the runner remains", names == ["hunt-runner"], f"{names}")
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
    names = [c["name"] for c in pod(render("gitops.enabled=false"))["containers"]]
    expect("the sidecar is not rendered", names == ["hunt-runner"], f"{names}")


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'FAILED' if _failures else 'ALL PASSED'} -- {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
