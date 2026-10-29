#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_hunt_runner_gitsync.py
#  Purpose:      Prove the hunt runner's hunts and rules come off a volume the
#                git-sync sidecar fills, not off an emptyDir nothing writes.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for huntRunner.gitSync on the deployed dfe-engine Application.

The runner reads hunt and rule YAML off disk (dfe_engine.hunt_runner.spec_loader,
on every reload tick), so a directory nothing fills means no hunt has ever run
(dfe-infra#212). A template that renders is not evidence of that: the values are.
dfe-extras renders the runner beside the engine's thin chart, so the default
shape is read off the golden render and each switch is rendered through dfe-extras.

Eight things are checked:

1. The sidecar reaches the SAME repo, branch and credential as the engine, off
   `gitops.*`, which the appset fills from the cluster secret. A second copy of
   any of the three could point somewhere else.
2. The FIRST sync is bounded. The sidecar retries forever, which on its own
   reproduces #212: a bad credential leaves the link absent and the runner
   Running with zero hunts, so an init container has to fail the pod instead.
3. The runner's DFE_HUNTS_DIR and DFE_HUNTS_RULES_DIR resolve THROUGH git-sync's
   link into the synced subtree, on a volume both containers mount.
4. The runner leaves DFE_SCHEMAS_DIR to the image, since nothing seeds one here.
5. The sidecar is hardened like the runner: non-root pod context, read-only
   rootfs, all capabilities dropped, and its own HOME rather than the runner's.
6. Disabled, the pod is a single container again and nothing references the
   deploy repo -- the pre-#212 shape, so the switch is a real off.
7. No deploy repo (gitops.enabled=false) means no sidecar: there is nothing to
   sync and the credential Secret does not exist.
8. The pod waits for the engine to report ready before the runner starts. The
   runner exits without the coordination tables the engine's schema phase
   creates, and it shares the engine's sync wave, so the wait is the only thing
   holding it back. git-sync-init joins the same initContainers list.

    python3 scripts/tests/test_hunt_runner_gitsync.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

import sys

import yaml

from _expect import expect, standalone, summary
from _gate import NAMESPACE, effective_env, golden
from _weave import REPO_ROOT, render_extras

ENGINE = "dfe-engine"
EXTRAS_VALUES = REPO_ROOT / "helm" / "charts" / "dfe-extras" / "values.yaml"
ENGINE_VALUES = REPO_ROOT / "argocd" / "values" / "apps" / ENGINE / "values.yaml"
# A deployment's own deploy repo, as its cluster secret names it.
DEPLOY_REPO = {
    "dfe.hyperi.io/config_repo_url": "https://git.example.org/acme/dfe-deploy.git",
    "dfe.hyperi.io/config_repo_revision": "release",
}


def runner_defaults() -> dict:
    """The engine defaults dfe-extras renders the runner from."""
    values = yaml.safe_load(EXTRAS_VALUES.read_text(encoding="utf-8"))
    return values["extras"]["defaults"][ENGINE]


def gitsync_defaults() -> dict:
    return runner_defaults()["huntRunner"]["gitSync"]


def config_mount() -> str:
    """The engine's config mount, as its integration values set it."""
    return yaml.safe_load(ENGINE_VALUES.read_text(encoding="utf-8"))["config"]["mountPath"]


def runner(docs: list[dict]) -> dict:
    """The hunt-runner Deployment, or {} when the render holds none."""
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "dfe-hunt-runner":
            return doc
    return {}


def deployed() -> list[dict]:
    """What a default slim deployment renders for the engine."""
    return golden(ENGINE, "slim", "local")


def render(overlay: dict | None = None, **options: object) -> dict:
    """The hunt-runner Deployment dfe-extras renders under these instance values."""
    return runner(render_extras(ENGINE, "slim", "local", overlay=overlay, **options))


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


def secret_ref(entry: object) -> tuple[str, str] | None:
    """(secret, key) of one effective env entry, or None when it is a literal."""
    ref = ((entry if isinstance(entry, dict) else {}).get("valueFrom") or {}).get("secretKeyRef")
    return (ref["name"], ref["key"]) if ref else None


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


def engine_env_data(docs: list[dict]) -> dict:
    """The literal env dfe-extras hands the engine, from its dfe-engine-env ConfigMap."""
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == f"{ENGINE}-env":
            return doc["data"]
    return {}


def test_the_sidecar_syncs_the_engines_own_deploy_repo() -> None:
    """Repo, branch and credential are the engine's gitops.* values, not a second copy.

    The appset injects gitops.repoUrl and gitops.branch from the cluster-secret
    annotations so the engine's write target equals the repo Argo reads. A pin
    of its own here would let the runner watch a repo nobody writes.
    """
    docs = deployed()
    engine = effective_env(docs, f"Deployment/{ENGINE}")
    sidecar = container(runner(docs), "git-sync")
    expect("the sidecar renders by default", bool(sidecar), "no git-sync container")
    if not sidecar:
        return

    expect(
        "--repo is the repo the engine writes",
        flag(sidecar, "repo") == engine.get("DFE_GITOPS_REPO_URL"),
        f"{flag(sidecar, 'repo')} vs {engine.get('DFE_GITOPS_REPO_URL')}",
    )
    expect(
        "--ref is the branch the engine writes",
        flag(sidecar, "ref") == engine.get("DFE_GITOPS_BRANCH"),
        f"{flag(sidecar, 'ref')} vs {engine.get('DFE_GITOPS_BRANCH')}",
    )
    refs = secret_refs(sidecar)
    expect(
        "it reuses the engine's deploy-repo credential, both keys",
        refs.get("GITSYNC_USERNAME") == secret_ref(engine.get("DFE_GITOPS_USERNAME"))
        and refs.get("GITSYNC_PASSWORD") == secret_ref(engine.get("DFE_GITOPS_TOKEN"))
        and refs.get("GITSYNC_USERNAME") is not None,
        f"{refs} vs {engine.get('DFE_GITOPS_USERNAME')} {engine.get('DFE_GITOPS_TOKEN')}",
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

    moved = render_extras(ENGINE, "slim", "local", annotations=DEPLOY_REPO)
    moved_sidecar = container(runner(moved), "git-sync")
    literal = engine_env_data(moved)
    expect(
        "a deployment's own deploy repo moves the sidecar and the engine together",
        flag(moved_sidecar, "repo") == literal.get("DFE_GITOPS_REPO_URL")
        == DEPLOY_REPO["dfe.hyperi.io/config_repo_url"]
        and flag(moved_sidecar, "ref") == literal.get("DFE_GITOPS_BRANCH")
        == DEPLOY_REPO["dfe.hyperi.io/config_repo_revision"],
        f"{moved_sidecar.get('args')} vs {literal.get('DFE_GITOPS_REPO_URL')}",
    )


def test_the_first_sync_is_bounded() -> None:
    """#212's shape again: retry-forever on the FIRST sync hides a bad credential.

    With only the sidecar the link never appears, the runner loads zero hunts and
    the pod stays Running, so the init container is what turns that into a pod
    that never starts.
    """
    gs = gitsync_defaults()
    doc = runner(deployed())
    init = container(doc, "git-sync-init")
    sidecar = container(doc, "git-sync")
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
        flag(sidecar, "max-failures") == "-1",
        f"{flag(sidecar, 'max-failures')}",
    )
    expect(
        "it seeds the worktree the sidecar and the runner then use",
        flag(init, "root") == f"{gs['mountPath']}/repo" and flag(init, "link") == gs["link"],
        f"{flag(init, 'root')} {flag(init, 'link')}",
    )
    expect(
        "off the same repo, ref and credential as the sidecar",
        flag(init, "repo") == flag(sidecar, "repo")
        and flag(init, "ref") == flag(sidecar, "ref")
        and secret_refs(init) == secret_refs(sidecar),
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
    gs = gitsync_defaults()
    doc = runner(deployed())
    hunt_runner = container(doc, "hunt-runner")
    sidecar = container(doc, "git-sync")
    env = env_of(hunt_runner)

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
        flag(sidecar, "root") == root,
        f"{flag(sidecar, 'root')}",
    )
    expect(
        "and the link it publishes is the one those paths cross",
        flag(sidecar, "link") == gs["link"],
        f"{flag(sidecar, 'link')}",
    )

    expect(
        "the runner mounts the synced volume read-only",
        mount_of(hunt_runner, "deploy-repo").get("readOnly") is True,
        f"{mount_of(hunt_runner, 'deploy-repo')}",
    )
    expect(
        "the sidecar mounts the same volume writable",
        mount_of(sidecar, "deploy-repo").get("readOnly") is None,
        f"{mount_of(sidecar, 'deploy-repo')}",
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
    env = env_of(container(runner(deployed()), "hunt-runner"))
    expect(
        "DFE_SCHEMAS_DIR is not set on the runner",
        "DFE_SCHEMAS_DIR" not in env,
        f"{env.get('DFE_SCHEMAS_DIR')}",
    )


def test_the_sidecar_is_hardened_like_the_runner() -> None:
    """A sidecar with a writable rootfs would be the softest thing in the pod."""
    doc = runner(deployed())
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
    home = gitsync_defaults()["homePath"]
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
    doc = render({"huntRunner": {"gitSync": {"enabled": False}}})
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
    mount = config_mount()
    expect(
        "the hunt directories fall back to the config mount",
        env.get("DFE_HUNTS_DIR") == f"{mount}/hunts"
        and env.get("DFE_HUNTS_RULES_DIR") == f"{mount}/rules",
        f"{env.get('DFE_HUNTS_DIR')} {env.get('DFE_HUNTS_RULES_DIR')}",
    )


def test_no_deploy_repo_means_no_sidecar() -> None:
    """gitops.enabled=false leaves no repo to sync and no credential Secret.

    Rendering the sidecar anyway would wedge the pod in
    CreateContainerConfigError on a Secret nothing creates.
    """
    doc = render({"gitops": {"enabled": False}})
    names = [c["name"] for c in pod(doc)["containers"]]
    expect("the sidecar is not rendered", names == ["hunt-runner"], f"{names}")
    expect(
        "nor the init container that would wedge the pod on it",
        init_names(doc) == ["wait-for-engine"],
        f"{init_names(doc)}",
    )


def test_the_runner_waits_for_the_engine() -> None:
    """A fresh deploy restarted the runner on SchemaNotAppliedError until the schema landed."""
    docs = deployed()
    doc = runner(docs)
    expect(
        "the engine wait runs first, then the first sync",
        init_names(doc) == ["wait-for-engine", "git-sync-init"],
        f"{init_names(doc)}",
    )
    service = next(
        d for d in docs if d.get("kind") == "Service" and d["metadata"]["name"] == ENGINE
    )
    port = next(p["port"] for p in service["spec"]["ports"] if p.get("name") == "http")
    script = (container(doc, "wait-for-engine").get("command") or [""])[-1]
    want = f"http://{ENGINE}.{NAMESPACE}.svc.cluster.local:{port}/readyz"
    expect("it polls the engine Service's /readyz", f'URL="{want}"' in script, script)
    expect(
        "on the engine Service's own port",
        runner_defaults()["waitForEngine"]["port"] == port,
        f"{runner_defaults()['waitForEngine']['port']} vs {port}",
    )
    elsewhere = render(namespace="dfe-local")
    local_script = (container(elsewhere, "wait-for-engine").get("command") or [""])[-1]
    expect(
        "in the namespace the release lands in",
        f'URL="http://{ENGINE}.dfe-local.svc.cluster.local:{port}/readyz"' in local_script,
        local_script,
    )
    off = render({"waitForEngine": {"enabled": False}})
    expect(
        "switched off, only the first sync is left",
        init_names(off) == ["git-sync-init"],
        f"{init_names(off)}",
    )
    neither = {"waitForEngine": {"enabled": False}, "huntRunner": {"gitSync": {"enabled": False}}}
    bare = render(neither)
    expect(
        "and with neither, the pod has no initContainers key",
        "initContainers" not in pod(bare),
        f"{pod(bare).get('initContainers')}",
    )


def test_the_runner_reads_the_destinations_the_api_writes() -> None:
    """An API-created alert destination must reach the runner that delivers hunt matches.

    The engine writes them as YAML under <config>/alerts on its ReadWriteOnce claim,
    not into git, so the runner mounts that claim read-only on the engine's node.
    """
    alerts = f"{config_mount()}/alerts"
    doc = render()
    main = container(doc, "hunt-runner")
    engine = engine_env_data(deployed())
    expect(
        "the runner reads the directory the engine writes",
        env_of(main).get("DFE_HUNTS_ALERT_DESTINATIONS_DIR") == alerts
        and engine.get("DFE_HUNTS_ALERT_DESTINATIONS_DIR") == alerts,
        f"{env_of(main).get('DFE_HUNTS_ALERT_DESTINATIONS_DIR')} vs {engine.get('DFE_HUNTS_ALERT_DESTINATIONS_DIR')}",
    )
    mount = mount_of(main, "alert-destinations")
    expect(
        "it is the alerts subdirectory of the config claim, read-only",
        mount.get("mountPath") == alerts
        and mount.get("subPath") == "alerts"
        and mount.get("readOnly") is True,
        f"{mount}",
    )
    claims = [
        v["persistentVolumeClaim"] for v in pod(doc)["volumes"] if v["name"] == "alert-destinations"
    ]
    expect(
        "off the engine's own claim, read-only",
        claims == [{"claimName": "dfe-engine-config", "readOnly": True}],
        f"{claims}",
    )
    terms = pod(doc)["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    expect(
        "on the engine's node, since the claim is ReadWriteOnce",
        terms
        == [
            {
                "topologyKey": "kubernetes.io/hostname",
                "labelSelector": {"matchLabels": {"app.kubernetes.io/name": ENGINE}},
            }
        ],
        f"{terms}",
    )
    for label, overlay in (
        ("switched off", {"huntRunner": {"alertDestinations": {"enabled": False}}}),
        (
            "with no config claim",
            {"writablePaths": {"config": {"persistence": {"enabled": False}}}},
        ),
    ):
        bare = render(overlay)
        expect(
            f"{label}, no mount, volume, env or affinity",
            not mount_of(container(bare, "hunt-runner"), "alert-destinations")
            and "DFE_HUNTS_ALERT_DESTINATIONS_DIR" not in env_of(container(bare, "hunt-runner"))
            and not [v for v in pod(bare)["volumes"] if v["name"] == "alert-destinations"]
            and "affinity" not in pod(bare),
            f"{label}",
        )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
