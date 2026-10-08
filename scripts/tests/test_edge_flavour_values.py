#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_edge_flavour_values.py
#  Purpose:      Pin the edge module's per-flavour overlays -- every key in one
#                is read by an edge chart and nothing else, the appset layers
#                the right file, and each flavour's cascade still renders.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One overlay per cloud flavour, carrying the edge module's tier table.

    python3 -m pytest scripts/tests/test_edge_flavour_values.py -q

A key moved out of `argocd/values/<cloud>.yaml` and into
`argocd/values/edge-<flavour>.yaml` reaches only the two Applications
`argocd/appsets/layer2-edge.yaml` generates, so a key a NON-edge chart also
reads would silently change what that chart deploys. That is the rule these
pin, along with the two keys deliberately left behind for exactly that reason.

Needs `helm` on PATH for the render cases.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from _charts import chart_dir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-edge.yaml"
EDGE_TREE = REPO_ROOT / "helm" / "edge"
CHARTS_TREE = REPO_ROOT / "helm" / "charts"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Flavour -> the cloud overlay it layers on top of. `local` and `rancher` are
# one flavour, so the on-prem file has two cloud facts selecting it.
FLAVOURS = {
    "aws": "aws",
    "gcp": "gcp",
    "azure": "azure",
    "onprem": "local",
}

# Culvert's own listeners, restated because Helm REPLACES a list and the on-prem
# overlay sets the RECEIVER's list on the same key.
TUNNEL_LISTENERS = (
    '[{"name":"wireguard","port":51820,"protocol":"UDP","exposed":true},'
    '{"name":"openvpn-udp","port":1194,"protocol":"UDP","exposed":true}]'
)


def flavour_file(flavour: str) -> Path:
    return VALUES / f"edge-{flavour}.yaml"


def leaf_paths(node: object, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every value path in a parsed values file, deepest first."""
    if not isinstance(node, dict) or not node:
        return [prefix] if prefix else []
    out: list[tuple[str, ...]] = []
    for key, child in node.items():
        out += leaf_paths(child, (*prefix, str(key)))
    return out


# dfe-extras renders each component's DFE-only objects, and its culvert-* templates
# render only for culvert, whose Application is an edge one.
EXTRAS_EDGE_TEMPLATES = ("charts/dfe-extras", "culvert-", "edge/culvert")


def templates(tree: Path) -> list[Path]:
    return sorted(p for p in tree.glob("*/templates/**/*") if p.is_file())


def reader(tree: Path, template: Path) -> str:
    """The chart a template reads values for, as `<tree>/<chart>`."""
    found = f"{tree.name}/{template.relative_to(tree).parts[0]}"
    chart, prefix, edge = EXTRAS_EDGE_TEMPLATES
    return edge if found == chart and template.name.startswith(prefix) else found


def names_exactly(text: str, path: tuple[str, ...]) -> bool:
    """True when the text reads `.Values.<path>` and descends no further."""
    dotted = re.escape(".".join(path))
    return re.search(rf"\.Values\.{dotted}(?![\w.])", text) is not None


def readers(path: tuple[str, ...]) -> set[str]:
    """Which chart directories read `path`, by it or by any prefix of it.

    A chart that binds the whole branch (culvert's `$pub := .Values.exposure`)
    reads every key under it, so a prefix counts as a read.
    """
    prefixes = [path[:n] for n in range(1, len(path) + 1)]
    found: set[str] = set()
    for tree in (EDGE_TREE, CHARTS_TREE):
        for template in templates(tree):
            text = template.read_text(encoding="utf-8", errors="replace")
            if any(names_exactly(text, prefix) for prefix in prefixes):
                found.add(reader(tree, template))
    return found


def render(chart: str, *args: str) -> str:
    out = subprocess.run(
        ["helm", "template", chart, str(chart_dir(chart)), *args],
        capture_output=True, text=True, check=False, cwd=REPO_ROOT,
    )
    assert out.returncode == 0, f"helm template {chart} {args} failed:\n{out.stderr}"
    return out.stdout


def objects(text: str) -> set[tuple[str, str]]:
    return {
        (d["kind"], d["metadata"]["name"])
        for d in yaml.safe_load_all(text)
        if d and "kind" in d
    }


@pytest.mark.parametrize("flavour", sorted(FLAVOURS))
def test_every_flavour_has_its_own_overlay(flavour: str) -> None:
    assert flavour_file(flavour).is_file()


@pytest.mark.parametrize("flavour", sorted(FLAVOURS))
def test_the_overlay_names_the_flavour_and_its_tiers(flavour: str) -> None:
    """The file is the tier table, so it has to say which flavour and which tier."""
    body = flavour_file(flavour).read_text(encoding="utf-8")
    assert body.splitlines()[0].startswith("# THE EDGE MODULE,")
    for tier in ("TIER 1", "TIER 2", "TIER 3"):
        assert tier in body, f"edge-{flavour}.yaml states no {tier}"


@pytest.mark.parametrize("flavour", sorted(FLAVOURS))
def test_no_key_in_a_flavour_overlay_is_read_outside_the_edge(flavour: str) -> None:
    """The overlay reaches the two edge Applications alone, so a key another
    chart reads would change that chart's render by being moved here."""
    tree = yaml.safe_load(flavour_file(flavour).read_text(encoding="utf-8")) or {}
    strays: dict[str, set[str]] = {}
    for path in leaf_paths(tree):
        outside = {c for c in readers(path) if not c.startswith("edge/")}
        if outside:
            strays[".".join(path)] = outside
    assert not strays, f"edge-{flavour}.yaml carries keys a non-edge chart reads: {strays}"


@pytest.mark.parametrize("flavour", sorted(FLAVOURS))
def test_every_key_in_a_flavour_overlay_is_read_by_an_edge_chart(flavour: str) -> None:
    """A key no chart reads is a dial that does nothing."""
    tree = yaml.safe_load(flavour_file(flavour).read_text(encoding="utf-8")) or {}
    unread = [".".join(p) for p in leaf_paths(tree) if not readers(p)]
    assert not unread, f"edge-{flavour}.yaml carries keys no chart reads: {unread}"


# Tier 3 is absent by design: the two AWS mechanisms an order above this
# cluster's own compute, and the group (e) surfaces offered no door on any
# flavour. Each name is matched against a key path, never against the prose,
# because naming one in a comment is how the tier table documents the refusal.
TIER_3_NAMES = frozenset({
    "shield", "shield_advanced", "global_accelerator", "globalaccelerator",
    "kafka", "clickhouse", "keeper", "postgres", "postgresql", "openbao", "kubernetes",
})


def _own_keys(path: tuple[str, ...]) -> list[str]:
    """The path's own key names, dropping any annotation key a vendor owns."""
    return [part.lower() for part in path if "/" not in part and "." not in part]


@pytest.mark.parametrize("flavour", sorted(FLAVOURS))
def test_no_tier_3_mechanism_has_a_key_in_a_flavour_overlay(flavour: str) -> None:
    """A tier-3 door is one nobody may turn on, so the absence has to be of the
    KEY -- a default of false is a switch, and a switch gets flipped."""
    tree = yaml.safe_load(flavour_file(flavour).read_text(encoding="utf-8")) or {}
    named = [
        ".".join(path) for path in leaf_paths(tree)
        if TIER_3_NAMES & set(_own_keys(path))
    ]
    assert not named, f"edge-{flavour}.yaml carries a tier-3 key: {named}"


def test_no_tier_3_mechanism_has_a_key_in_the_dial() -> None:
    """The same rule on the deployer's own surface, where a key would be read
    as an offer rather than as the refusal the tier table states."""
    body = (REPO_ROOT / "deployment.example.yaml").read_text(encoding="utf-8")
    offered = [
        line for line in body.splitlines()
        if not line.lstrip().startswith("#")
        and any(f"{name}:" in line.lower() for name in ("shield_advanced", "global_accelerator"))
    ]
    assert not offered, f"deployment.example.yaml offers a tier-3 key: {offered}"


def test_the_receivers_own_exposure_mode_stayed_in_the_cloud_overlay() -> None:
    """dfe-receiver is not an edge chart, so its door is not the module's key."""
    assert readers(("exposure", "mode")) & {"charts/dfe-receiver"}
    for cloud, mode in (("aws", "vpn"), ("gcp", "vpn"), ("azure", "vpn"), ("local", "internal")):
        tree = yaml.safe_load((VALUES / f"{cloud}.yaml").read_text(encoding="utf-8"))
        assert tree["exposure"]["mode"] == mode, cloud


def test_the_edge_oidc_switch_stayed_where_kafbat_and_the_engine_read_it() -> None:
    """Moving it would degrade kafbat's auth type and drop the engine's provider env."""
    assert readers(("oidc", "enabled")) >= {"charts/kafbat", "charts/dfe-engine"}
    tree = yaml.safe_load((VALUES / "aws.yaml").read_text(encoding="utf-8"))
    assert tree["oidc"]["enabled"] is True


def test_the_tunnels_pki_mode_is_external_on_every_cloud_flavour() -> None:
    """Local mode mints the CA in the pod, so a rebuild invalidates every config."""
    for flavour in ("aws", "gcp", "azure"):
        tree = yaml.safe_load(flavour_file(flavour).read_text(encoding="utf-8"))
        assert tree["pki"]["mode"] == "external", flavour
    onprem = yaml.safe_load(flavour_file("onprem").read_text(encoding="utf-8"))
    assert "pki" not in onprem, "on-prem keeps the chart's own local default"


def test_both_edge_applications_layer_the_flavour_overlay() -> None:
    """One tier table serves both doors, or the tunnel reads a different one."""
    appsets = [
        d for d in yaml.safe_load_all(APPSET.read_text(encoding="utf-8"))
        if d and d.get("kind") == "ApplicationSet"
    ]
    assert len(appsets) == 2
    for appset in appsets:
        files = appset["spec"]["template"]["spec"]["sources"][0]["helm"]["valueFiles"]
        edge = [f for f in files if "/edge-" in f]
        assert len(edge) == 1, files
        cloud = next(i for i, f in enumerate(files) if f.endswith('/{{ index .metadata.annotations "dfe.hyperi.io/cloud" }}.yaml'))
        assert files.index(edge[0]) > cloud, "the flavour file must beat the cloud overlay"
        assert files.index(edge[0]) < files.index("$values/infra/common.yaml")


ASSIGN = '{{ $c := index .metadata.annotations "dfe.hyperi.io/cloud" }}'
BRANCH = re.compile(
    r'\{\{ if or ((?:\(eq \$c "[^"]+"\) ?)+)\}\}'
    r'([^{]*)\{\{ else \}\}\{\{ \$c \}\}\{\{ end \}\}'
)
MAPPED_CLOUD = re.compile(r'\(eq \$c "([^"]+)"\)')


def resolve(expression: str, cloud: str) -> str:
    """Evaluate the appset's flavour expression for one cloud fact.

    The mapped clouds and the flavour they map to are read out of the
    expression rather than restated, so changing the branch changes the answer.
    """
    body = expression.replace(ASSIGN, "")
    branch = BRANCH.search(body)
    assert branch, body
    clauses, mapped = branch.groups()
    chosen = mapped if cloud in MAPPED_CLOUD.findall(clauses) else cloud
    return body[: branch.start()] + chosen + body[branch.end():]


@pytest.mark.parametrize(("cloud", "flavour"),
                         [("aws", "aws"), ("gcp", "gcp"), ("azure", "azure"),
                          ("local", "onprem"), ("local-dfe", "onprem"), ("rancher", "onprem")])
def test_the_cloud_fact_selects_the_flavour_file(cloud: str, flavour: str) -> None:
    """local, local-dfe and rancher are one flavour; every other cloud names its own."""
    appsets = [
        d for d in yaml.safe_load_all(APPSET.read_text(encoding="utf-8"))
        if d and d.get("kind") == "ApplicationSet"
    ]
    assert len(appsets) == 2
    for appset in appsets:
        files = appset["spec"]["template"]["spec"]["sources"][0]["helm"]["valueFiles"]
        expression = next(f for f in files if "/edge-" in f)
        resolved = resolve(expression, cloud)
        assert resolved.endswith(f"/edge-{flavour}.yaml"), resolved
        assert (REPO_ROOT / "argocd" / "values" / f"edge-{flavour}.yaml").is_file()


@pytest.mark.parametrize("flavour", sorted(FLAVOURS))
def test_the_gateway_renders_under_every_flavour_cascade(flavour: str) -> None:
    text = render(
        "envoy-gateway-config",
        "--namespace", "envoy-gateway-system",
        "-f", str(VALUES / "common.yaml"),
        "-f", str(VALUES / f"{FLAVOURS[flavour]}.yaml"),
        "-f", str(flavour_file(flavour)),
        "--set", "appNamespace=dfe",
        "--set", "domain=dfe.example.com",
    )
    assert ("Gateway", "dfe-gateway") in objects(text), sorted(objects(text))


@pytest.mark.parametrize("flavour", sorted(FLAVOURS))
def test_the_tunnel_renders_under_every_flavour_cascade(flavour: str) -> None:
    """External PKI needs the Secret the deployment's instance file names, so a
    cloud flavour renders with one and the chart refuses without it."""
    args = [
        "--namespace", "dfe",
        "-f", str(VALUES / "common.yaml"),
        "-f", str(VALUES / f"{FLAVOURS[flavour]}.yaml"),
        "-f", str(flavour_file(flavour)),
        "--set", "domain=dfe.example.com",
        "--set-json", f"listeners={TUNNEL_LISTENERS}",
    ]
    if flavour != "onprem":
        args += ["--set", "pki.existingSecret=dfe-culvert-pki"]
    text = render("culvert", *args)
    assert ("Deployment", "dfe-culvert") in objects(text), sorted(objects(text))


def test_external_pki_with_no_secret_is_refused_by_name() -> None:
    """The cloud flavours set the mode; the deployment still has to name the Secret."""
    out = subprocess.run(
        ["helm", "template", "culvert", str(chart_dir("culvert")),
         "-f", str(VALUES / "common.yaml"),
         "-f", str(VALUES / "aws.yaml"),
         "-f", str(VALUES / "edge-aws.yaml"),
         "--set-json", f"listeners={TUNNEL_LISTENERS}"],
        capture_output=True, text=True, check=False, cwd=REPO_ROOT,
    )
    assert out.returncode != 0
    assert "pki.mode is external with no pki.existingSecret" in out.stderr


def test_the_tunnel_takes_no_load_balancer_on_aws() -> None:
    """A NodePort carries no per-byte charge, which is the whole AWS default."""
    text = render(
        "culvert",
        "--namespace", "dfe",
        "-f", str(VALUES / "common.yaml"),
        "-f", str(VALUES / "aws.yaml"),
        "-f", str(VALUES / "edge-aws.yaml"),
        "--set", "pki.existingSecret=dfe-culvert-pki",
        "--set-json", f"listeners={TUNNEL_LISTENERS}",
    )
    kinds = {
        d["metadata"]["name"]: d["spec"]["type"]
        for d in yaml.safe_load_all(text) if d and d.get("kind") == "Service"
    }
    assert kinds["dfe-culvert-public-udp"] == "NodePort", kinds


def test_the_receiver_still_renders_no_load_balancer_on_aws() -> None:
    """The receiver is not an edge chart and reads no flavour file at all."""
    text = render(
        "dfe-receiver",
        "--namespace", "dfe",
        "-f", str(VALUES / "common.yaml"),
        "-f", str(VALUES / "aws.yaml"),
        "--set", "domain=dfe.example.com",
    )
    types = [
        d["spec"]["type"]
        for d in yaml.safe_load_all(text) if d and d.get("kind") == "Service"
    ]
    assert types == ["ClusterIP"], types
