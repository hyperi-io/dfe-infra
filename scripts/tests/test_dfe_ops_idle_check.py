#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_idle_check.py
#  Purpose:      Prove the acceptance idle check expects a per_config app to be
#                absent until a source uses it, the way the engine deploys it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What `dfe-ops acceptance --mode <profile>` requires to be Ready before the data path.

apps.yaml seeds dfe-transform-vrl and dfe-transform-elastic on single and scale,
but both are per_config: the engine runs one Deployment per source that uses the
app and undeploys an instance no source uses. A fresh deploy therefore carries
neither, and requiring them failed the acceptance idle check on single.

The cluster side is a stand-in kubectl that answers from a fixed Deployment set,
and the metrics seams report an idle app, so no cluster or port-forward is used.

    python3 -m pytest scripts/tests/test_dfe_ops_idle_check.py -q
"""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_idle_check", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_idle_check", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_idle_check"] = dfeops
_loader.exec_module(dfeops)

composition = dfeops.composition

FAKE_KUBECTL = """\
import json
import sys

deployed = json.loads({deployed!r})
args = sys.argv[1:]
if "jsonpath={{.items[*].metadata.name}}" in args:
    print(" ".join(deployed))
    sys.exit(0)
name = args[args.index("deploy") + 1]
if name in deployed:
    print(deployed[name])
    sys.exit(0)
print(f'Error from server (NotFound): deployments.apps "{{name}}" not found', file=sys.stderr)
sys.exit(1)
"""


def _single_multiplicity(profile: str) -> dict[str, str]:
    """Deployment name -> ready replicas for every non-per_config default app."""
    return {
        composition.deployment_name(app): "1"
        for app in composition.default_apps(profile)
        if composition.multiplicity(app) != "per_config"
    }


def _idle_check(tmp_path: Path, monkeypatch, profile: str, deployed: dict[str, str]) -> int:
    kubectl = tmp_path / "kubectl.py"
    kubectl.write_text(FAKE_KUBECTL.format(deployed=json.dumps(deployed)), encoding="utf-8")
    monkeypatch.setattr(dfeops, "_forward", lambda *_args: None)
    monkeypatch.setattr(dfeops, "_forward_ready", lambda *_args: True)
    # Forwards are stubbed, so the host's own port state has no bearing on these runs.
    monkeypatch.setattr(dfeops, "_taken_ports", lambda _ports: [])
    monkeypatch.setattr(dfeops, "_metrics_body", lambda _url: "pipeline_idle 1\n")
    monkeypatch.setattr(dfeops, "_health_body", lambda _url: None)
    monkeypatch.setattr(dfeops, "_health_component", lambda _url, _name: None)
    return dfeops._idle_check([sys.executable, str(kubectl)], "dfe-local", profile, 19090)


def test_single_seeds_per_config_apps_the_engine_leaves_undeployed() -> None:
    per_config = [a for a in composition.default_apps("single") if composition.multiplicity(a) == "per_config"]
    assert "dfe-transform-vrl" in per_config
    assert "dfe-transform-elastic" in per_config


def test_an_undeployed_per_config_app_is_tolerated(tmp_path: Path, monkeypatch) -> None:
    """The failure the kind run hit: both transforms absent, everything else Ready."""
    assert _idle_check(tmp_path, monkeypatch, "single", _single_multiplicity("single")) == 0


def test_a_missing_single_app_still_fails(tmp_path: Path, monkeypatch) -> None:
    deployed = _single_multiplicity("single")
    deployed.pop(composition.deployment_name("dfe-loader"))
    assert _idle_check(tmp_path, monkeypatch, "single", deployed) == 1


def test_a_deployed_per_config_instance_must_be_ready(tmp_path: Path, monkeypatch) -> None:
    """Once a source uses the app its instance is checked like any other."""
    deployed = {**_single_multiplicity("single"), "dfe-transform-vrl-syslog": "0"}
    assert _idle_check(tmp_path, monkeypatch, "single", deployed) == 1
    deployed["dfe-transform-vrl-syslog"] = "1"
    assert _idle_check(tmp_path, monkeypatch, "single", deployed) == 0


def test_the_targets_name_each_instance_and_each_absent_app() -> None:
    apps = ("dfe-loader", "dfe-transform-vrl", "dfe-transform-elastic")
    targets, absent = dfeops._idle_targets(apps, {"dfe-loader", "dfe-transform-vrl-syslog"})
    assert targets == [("dfe-loader", "dfe-loader"), ("dfe-transform-vrl", "dfe-transform-vrl-syslog")]
    assert absent == ["dfe-transform-elastic"]
