#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_image_digest_pin.py
#  Purpose:      Prove the versions.yaml digests reach a rendered manifest, so a
#                re-pushed tag cannot land bytes the stack never certified.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for dfe-common.image and the digest half of every app pin.

versions.yaml pins tag@sha256, `dfe-stack render` emits tag@sha256 and
`dfe-stack verify` re-fetches every digest -- and none of that reaches the
cluster, because the charts are what deploy. Registry tags are mutable, so a
chart that renders a bare tag can pull bytes other than the ones verified.

Each DFE component is read off its deployed render: the thin chart beside
dfe-extras, rendered live so a stale copy of a pin in a values file shows here.

Six things are checked:

1. Every component whose digest versions.yaml records renders `:tag@sha256:<that>`.
2. The image helpers are OPTIONAL -- a deployment that clears the digest renders
   the plain tag.
3. The digest a values file carries IS the SSoT's, not a stale copy of it: the
   engine image dfe-extras runs the hunt runner and keda shim on, and git-sync's.
4. A reference built in the integration values rather than by a helper -- the
   hyperdx dashboards init container -- carries the digest too.
5. The engine's content init containers run OTHER apps' images, so each one is
   checked against the pin for the app it speaks for.
6. Every upstream image a chart runs carries the digest versions.yaml certified.

    python3 scripts/tests/test_image_digest_pin.py

Needs `helm` on PATH; the thin-chart cases need the scalo-service library
(_weave.library). No test runner, matching the other checks here.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary
from _gate import COMPONENTS, cell
from _weave import contract, render_app, render_extras, weave

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
# The registry a deployed render pulls every DFE image from: the cluster secret's.
REGISTRY = weave().Target("", "", "").registry
# The registry the engine's content entries name, whatever the deployment's is.
CONTENT_REGISTRY = "ghcr.io/hyperi-io"

# Containers in a DFE chart running an upstream image: their pin is a
# services-digests key, not the chart's own app digest, so the app-wide
# assertions skip them and each is checked against its own key instead.
THIRD_PARTY_SIDECARS = frozenset({"git-sync", "git-sync-init"})

# Containers running ANOTHER app's pinned image -- the engine's content init
# containers and the hyperdx dashboards init container -- which the app-wide sweep
# would read as the wrong reference. Each is checked against its own app's pin.
CONTENT_PREFIX = "content-"
OTHER_APP_CONTAINERS = frozenset({"dashboards"})


def _parse_nested(text: str) -> dict:
    """The versions.yaml reader the drift check uses, kept local to stay dep-free."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        m = re.match(r'^([A-Za-z0-9_.-]+):\s*(?:"([^"]*)"|([^#]*?))?\s*(?:#.*)?$', raw.strip())
        if not m:
            continue
        key, quoted = m.group(1), m.group(2)
        value = quoted if quoted is not None else (m.group(3) or "").strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "" and quoted is None:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = value
    return root


def current_stack() -> dict:
    root = _parse_nested((REPO_ROOT / "versions.yaml").read_text(encoding="utf-8"))
    return root["stacks"][root["current"]]


def render(chart: str, *sets: str) -> list[dict]:
    cmd = ["helm", "template", chart, str(CHARTS / chart)]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def images(docs: list[dict], want_container: str | None = None) -> list[str]:
    """Container images a render emits, init containers included.

    Named container, or every container whose name is not a third-party sidecar.
    """
    found: list[str] = []
    for doc in docs:
        spec = doc.get("spec", {})
        pod = spec.get("template", {}).get("spec") or spec.get("spec")
        if not isinstance(pod, dict):
            continue
        for key in ("initContainers", "containers"):
            for container in pod.get(key) or []:
                name = container.get("name")
                if want_container:
                    wanted = name == want_container
                else:
                    wanted = (
                        name not in THIRD_PARTY_SIDECARS | OTHER_APP_CONTAINERS
                        and not str(name).startswith(CONTENT_PREFIX)
                    )
                if wanted and container.get("image"):
                    found.append(container["image"])
    return found


def app_pin(app: str) -> str:
    """tag@sha256 for one app: its apps: tag (content: for an image another ships)."""
    stack = current_stack()
    tag = stack["apps"].get(app) or stack["content"].get(app)
    return f"{tag}@{stack['digests'][app]}"


def engine_pin() -> str:
    return f"{REGISTRY}/dfe-engine:{app_pin('dfe-engine')}"


def test_every_published_app_renders_its_digest() -> None:
    """The pin has to survive the chart, which is the only thing that deploys."""
    digests = current_stack()["digests"]
    for service in COMPONENTS:
        app = json.loads(contract(service).read_text(encoding="utf-8"))["app_name"]
        expect(f"versions.yaml pins a digest for {app}", app in digests, f"{sorted(digests)}")
        if app not in digests:
            continue
        rendered = images(cell(service, "slim", "local"))
        want = f"{REGISTRY}/{app}:{app_pin(app)}"
        expect(
            f"{service} renders tag@sha256 from the SSoT",
            rendered and all(i == want for i in rendered),
            f"wanted {want}, got {rendered}",
        )


def test_the_engine_sidecar_workloads_carry_the_digest_too() -> None:
    """hunt-runner and keda-shim run the ENGINE image out of dfe-extras' own copy of the pin."""
    want = engine_pin()
    docs = render_extras("dfe-engine", "slim", "local")
    for workload in ("dfe-hunt-runner", "dfe-keda-shim"):
        rendered = [
            i
            for doc in docs
            if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == workload
            for i in images([doc])
        ]
        expect(
            f"{workload} renders the pinned engine digest",
            rendered and all(i == want for i in rendered),
            f"wanted {want}, got {rendered}",
        )


def test_the_hyperdx_dashboards_init_container_carries_the_digest() -> None:
    """A second copy of the engine pin, in the values of a chart named after another app.

    The integration values build the reference, so no image helper's digest
    branch covers it -- this is the only thing that reads the rendered result.
    """
    want = engine_pin()
    rendered = images(cell("hyperdx", "slim", "local"), want_container="dashboards")
    expect(
        "the hyperdx dashboards init container renders the pinned engine digest",
        rendered and all(i == want for i in rendered),
        f"wanted {want}, got {rendered}",
    )


def test_each_content_entry_runs_the_pin_of_the_app_it_speaks_for() -> None:
    """The contract is a property of a digest, so the entry has to name that digest.

    A tag moved without its digest here would mount one release's contract
    while the deployment runs another's.
    """
    docs = cell("dfe-engine", "slim", "local")
    entries = {
        c["name"]: c["image"]
        for doc in docs
        for c in (doc.get("spec", {}).get("template", {}).get("spec", {}) or {}).get(
            "initContainers"
        )
        or []
        # The emit entries -- each app's contract and the catalogue its image
        # prints -- are the ones that run an app's own pin.
        if c["name"].startswith(CONTENT_PREFIX) and c["name"].endswith(("-contract", "-catalogue"))
    }
    contracts = [name for name in entries if name.endswith("-contract")]
    expect(
        "the engine mounts a contract for every app that carries a settings surface",
        len(contracts) == 7,
        f"{sorted(contracts)}",
    )
    expect(
        "and the source catalogue the elastic image prints",
        f"{CONTENT_PREFIX}dfe-transform-elastic-catalogue" in entries,
        f"{sorted(entries)}",
    )
    for name, image in sorted(entries.items()):
        app = name[len(CONTENT_PREFIX) :].removesuffix("-contract").removesuffix("-catalogue")
        want = f"{CONTENT_REGISTRY}/{app}:{app_pin(app)}"
        expect(
            f"{name} runs {app}'s own tag@sha256 from the SSoT",
            image == want,
            f"wanted {want}, got {image}",
        )


def test_a_chart_with_no_digest_renders_what_it_always_did() -> None:
    """The helpers are optional: a deployment carrying no digest still renders its tag.

    The digest is emptied explicitly rather than borrowed from an app that has
    not shipped one, which stops holding the day that app is published. Both
    helpers are covered: the thin chart's, and the one dfe-extras builds the
    engine's sidecar workloads with.
    """
    app = "dfe-transform-elastic"
    tag = current_stack()["apps"][app]
    cleared = {"image": {"digest": ""}}
    rendered = images(render_app(app, "slim", "local", overlay=cleared))
    expect(
        "a thin chart with no digest renders a plain registry/repo:tag",
        rendered and all(i == f"{REGISTRY}/{app}:{tag}" for i in rendered),
        f"{rendered}",
    )
    engine_tag = current_stack()["apps"]["dfe-engine"]
    sidecars = images(render_extras("dfe-engine", "slim", "local", overlay=cleared))
    expect(
        "and so does every dfe-extras workload running the engine image",
        sidecars and all(i == f"{REGISTRY}/dfe-engine:{engine_tag}" for i in sidecars),
        f"{sidecars}",
    )


def test_the_hunt_runner_git_sync_images_carry_their_own_pin() -> None:
    """The one upstream image inside a DFE workload; it has its own SSoT key.

    Both passes run it -- the init container that bounds the first sync and the
    sidecar that keeps the worktree current -- so both are checked.
    """
    stack = current_stack()
    want = (
        f"registry.k8s.io/git-sync/git-sync:{stack['services']['git-sync']}"
        f"@{stack['services-digests']['git-sync']}"
    )
    docs = render_extras("dfe-engine", "slim", "local")
    rendered = images(docs, want_container="git-sync-init") + images(
        docs, want_container="git-sync"
    )
    expect(
        "both git-sync passes render tag@sha256 from the SSoT",
        rendered == [want, want],
        f"wanted {want} twice, got {rendered}",
    )


def test_the_links_page_keeps_its_third_party_digest() -> None:
    """Upstream rebuilds even a patch tag in place, so the digest is the only
    immutable half the links page has."""
    stack = current_stack()
    want = (
        f"nginxinc/nginx-unprivileged:{stack['services']['nginx-unprivileged']}"
        f"@{stack['services-digests']['nginx-unprivileged']}"
    )
    rendered = images(render("links"))
    expect(
        "links renders its tag pinned by digest",
        rendered == [want],
        f"wanted {want}, got {rendered}",
    )


def _pin(repository: str, key: str) -> str:
    """repository:tag@sha256 for a services: key and its services-digests: twin."""
    stack = current_stack()
    return f"{repository}:{stack['services'][key]}@{stack['services-digests'][key]}"


def _operator_images(docs: list[dict]) -> list[str]:
    """repository:tag from each ClickHouse operator CR's container template."""
    found: list[str] = []
    for doc in docs:
        if doc.get("kind") in ("ClickHouseCluster", "KeeperCluster"):
            image = doc["spec"]["containerTemplate"]["image"]
            found.append(f"{image['repository']}:{image['tag']}")
    return found


# Chart, --set overrides selecting the workload, the image repository it runs,
# and the services: key pinning that image. The kafka chart is rendered once per
# workload, because each mode deploys a different broker.
THIRD_PARTY_WORKLOADS = [
    ("ferretdb", (), "ghcr.io/ferretdb/ferretdb", "ferretdb"),
    ("ferretdb", (), "ghcr.io/ferretdb/postgres-documentdb", "documentdb-pg"),
    ("forgejo", (), "code.forgejo.org/forgejo/forgejo", "forgejo"),
    ("otel-collector", (), "otel/opentelemetry-collector-contrib", "otel-collector"),
    (
        "clickhouse-cluster",
        ("clickhouse.mode=single",),
        "clickhouse/clickhouse-server",
        "clickhouse-version",
    ),
    ("kafka", ("kafka.mode=single",), "apache/kafka", "kafka-version"),
    (
        "kafka",
        ("kafka.mode=single", "kafka.provider=redpanda", "kafka.redpanda.acceptLicense=true"),
        "docker.redpanda.com/redpandadata/redpanda",
        "redpanda-version",
    ),
    (
        "kafka",
        (
            "appNamespace=dfe-aws",
            "kafka.mode=external",
            "kafka.provider=msk",
            "kafka.external.bootstrap=b-1.dfe.example:9096",
            "kafka.external.msk.bootstrapIam=b-1.dfe.example:9098",
        ),
        "apache/kafka",
        "kafka-version",
    ),
]


def test_every_upstream_image_a_chart_runs_carries_the_ssot_digest() -> None:
    """A tag is mutable, so each container running an upstream image pulls the
    digest versions.yaml certified, not whatever the tag points at today."""
    for chart, sets, repository, key in THIRD_PARTY_WORKLOADS:
        want = _pin(repository, key)
        rendered = [i for i in images(render(chart, *sets)) if i.split(":", 1)[0] == repository]
        expect(
            f"{chart} {' '.join(sets) or '(defaults)'} runs {repository} by tag@sha256",
            rendered and all(i == want for i in rendered),
            f"wanted {want}, got {rendered}",
        )


def test_the_clickhouse_operator_crs_carry_the_digest_in_the_tag() -> None:
    """The operator renders repository:tag and drops `hash` once its defaulting
    sets a tag, so the digest has to ride in the tag to reach the pod."""
    rendered = sorted(_operator_images(render("clickhouse-cluster")))
    want = sorted(
        [
            _pin("docker.io/clickhouse/clickhouse-server", "clickhouse-version"),
            _pin("docker.io/clickhouse/clickhouse-keeper", "clickhouse-keeper"),
        ]
    )
    expect(
        "the ClickHouseCluster and KeeperCluster each name tag@sha256",
        rendered == want,
        f"wanted {want}, got {rendered}",
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
