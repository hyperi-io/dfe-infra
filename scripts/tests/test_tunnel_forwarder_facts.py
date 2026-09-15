#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_tunnel_forwarder_facts.py
#  Purpose:      Prove the tunnel forwarder's two facts -- its Elastic IP and
#                the zone it is pinned to -- travel from the tofu module to the
#                cluster secret and on into culvert's own scheduling, and that
#                the ports the module DNATs to are the ones the chart pins.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Two facts and one port map, across four files that cannot check each other.

The forwarder holds the address every client config names and sits in one
availability zone. Neither fact is knowable from inside the cluster, and both
fail silently when they do not arrive: external-dns publishes nothing, and
culvert's pod lands in whichever zone the scheduler likes, which is a per-GB
boundary hop on every packet of every tunnel.

The port map fails harder. The tofu module DNATs to a nodePort number, the
chart's Service pins one, and a mismatch is a tunnel that connects to nothing
while every object involved reports healthy.

    python3 -m pytest scripts/tests/test_tunnel_forwarder_facts.py -q
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-edge.yaml"
CULVERT_VALUES = REPO_ROOT / "helm" / "edge" / "culvert" / "values.yaml"
FORWARDER_TF = REPO_ROOT / "terraform" / "modules" / "edge" / "aws" / "forwarder.tf"
EDGE_VARIABLES_TF = REPO_ROOT / "terraform" / "modules" / "edge" / "aws" / "variables.tf"
AWS_ROOT_OUTPUTS = REPO_ROOT / "terraform" / "environments" / "aws" / "outputs.tf"

FACTS = {
    "DFE_TUNNEL_ADDRESS": "dfe.hyperi.io/tunnel_address",
    "DFE_TUNNEL_ZONE": "dfe.hyperi.io/tunnel_zone",
}


def _render(**env: str) -> str:
    """The cluster secret template through envsubst, as bootstrap.sh renders it."""
    result = subprocess.run(
        ["envsubst"],
        input=CLUSTER_SECRET.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **env},
        check=True,
    )
    return result.stdout


def test_the_aws_root_emits_both_facts() -> None:
    outputs = AWS_ROOT_OUTPUTS.read_text(encoding="utf-8")
    for key in FACTS:
        assert f'output "{key}"' in outputs, f"{key} is not a root output, so bridge.py can never pass it on"


def test_bootstrap_omits_the_annotation_rather_than_rendering_it_empty() -> None:
    """An annotation rendered with an empty value is a fact the appset reads as
    present-and-blank; the `:+` form leaves the line out instead."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    for key, annotation in FACTS.items():
        assert f'export {key}="${{{key}:-}}"' in text, f"{key} is not defaulted in bootstrap.sh"
        assert f'${{{key}:+{annotation}:' in text, f"{key} does not render {annotation} conditionally"


def test_the_template_carries_both_annotation_substitutions() -> None:
    text = CLUSTER_SECRET.read_text(encoding="utf-8")
    for key in FACTS:
        assert f"${{{key}_ANNOTATION}}" in text, f"{key}_ANNOTATION is not substituted into the cluster secret"


def test_a_forwarder_deployment_writes_both_annotations() -> None:
    rendered = _render(
        DFE_TUNNEL_ADDRESS_ANNOTATION='dfe.hyperi.io/tunnel_address: "198.51.100.42"',
        DFE_TUNNEL_ZONE_ANNOTATION='dfe.hyperi.io/tunnel_zone: "us-west-2c"',
    )
    assert 'dfe.hyperi.io/tunnel_address: "198.51.100.42"' in rendered
    assert 'dfe.hyperi.io/tunnel_zone: "us-west-2c"' in rendered
    assert "${" not in rendered, "something in the cluster secret template was left unsubstituted"


def test_a_byo_deployment_writes_neither() -> None:
    rendered = _render()
    assert "tunnel_address" not in rendered
    assert "tunnel_zone" not in rendered


def test_the_appset_turns_the_zone_into_culverts_own_node_selector() -> None:
    """The annotation is inert unless something reads it, and a cluster secret
    that predates the fact must not select on an empty zone."""
    text = APPSET.read_text(encoding="utf-8")
    assert '$tunnelZone := index .metadata.annotations "dfe.hyperi.io/tunnel_zone"' in text
    assert "{{- if $tunnelZone }}" in text
    assert "topology.kubernetes.io/zone: {{ $tunnelZone | quote }}" in text
    # nodeScheduling.nodeSelector is the shared library's own key
    # (helm/library/dfe-common/templates/_scheduling.tpl), not one invented here.
    assert "nodeScheduling:" in text


def _culvert_listeners() -> dict[str, dict]:
    values = yaml.safe_load(CULVERT_VALUES.read_text(encoding="utf-8"))
    return {entry["name"]: entry for entry in values["listeners"]}


def _tf_default(name: str) -> int:
    """One node_ports default out of the edge module's tunnel variable."""
    match = re.search(rf"^\s*{name}\s*=\s*optional\(number,\s*(\d+)\)", EDGE_VARIABLES_TF.read_text(encoding="utf-8"), re.M)
    assert match is not None, f"the tunnel variable declares no node_ports.{name} default"
    return int(match.group(1))


def test_the_chart_pins_a_node_port_for_every_exposed_listener() -> None:
    """A NodePort Kubernetes allocates is not knowable before the Service
    exists, so the forwarder in front of it could never be told the number."""
    for name, entry in _culvert_listeners().items():
        if not entry.get("exposed"):
            continue
        assert "nodePort" in entry, f"exposed listener {name} pins no nodePort"
        assert 30000 <= entry["nodePort"] <= 32767, f"{name}'s nodePort is outside the Kubernetes range"


def test_the_module_dnats_to_the_ports_the_chart_pins() -> None:
    listeners = _culvert_listeners()
    assert _tf_default("wireguard") == listeners["wireguard"]["nodePort"]
    assert _tf_default("openvpn") == listeners["openvpn-udp"]["nodePort"]


def test_the_module_admits_the_listen_ports_the_chart_exposes() -> None:
    """The security group's ports are written in the module as literals, so
    they are checked against the chart that actually listens on them."""
    listeners = _culvert_listeners()
    forwarder = FORWARDER_TF.read_text(encoding="utf-8")
    for name in ("wireguard", "openvpn-udp"):
        port = listeners[name]["port"]
        assert f'name = "{name}", port = {port}' in forwarder, (
            f"the module's tunnel_ports does not carry {name} on {port}, which the chart exposes"
        )
