#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_kafka_controller_pool.py
#  Purpose:      Prove the kafka chart renders COMBINED KRaft at its shipped
#                defaults -- one node pool carrying both roles, and no separate
#                controller pool.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The rendered guard for kafka.controllerPool.enabled's default.

A separate KRaft controller pool re-forms the metadata quorum of a cluster that
already runs combined, so it is a decision for a NEW deployment and never a
default -- every rc.14 k8s deploy that upgrades from combined mode takes
whatever this chart renders. scripts/tests/test_resolve_sizing.py already reads
the `enabled:` line out of values.yaml as text; that passes even if a template
renders the pool regardless of the flag, or a values file in the cascade turns
it on. This renders the chart and reads the manifests instead.

    python3 -m pytest scripts/tests/test_kafka_controller_pool.py -q
    python3 scripts/tests/test_kafka_controller_pool.py

Needs `helm` on PATH.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = chart_dir("kafka")
VALUES = REPO_ROOT / "argocd" / "values"

# The cascade a real cluster-mode deploy layers, minus the deploy-repo overlay.
# The chart's own kafka.mode is `disabled`, so the bare defaults render nothing
# at all and could never catch a controller pool coming back.
SCALE_CASCADE = [VALUES / "common.yaml", VALUES / "profile-scale.yaml"]


def render(*extra_values: Path) -> list[dict]:
    """Every manifest the chart renders under the scale cascade."""
    cmd = ["helm", "template", "kafka", str(CHART), "--set", "appNamespace=dfe"]
    for v in [*SCALE_CASCADE, *extra_values]:
        cmd += ["-f", str(v)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def node_pools(docs: list[dict]) -> dict[str, dict]:
    """Every KafkaNodePool the render emitted, by name."""
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "KafkaNodePool"}


def overlay(body: str) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(body)
        return Path(fh.name)


def test_the_shipped_defaults_render_no_separate_controller_pool() -> None:
    """The cascade every rc.14 k8s deploy takes names no controllerPool, so it
    falls to the chart's own default and must come out combined."""
    pools = node_pools(render())
    expect("exactly one KafkaNodePool at the shipped defaults", len(pools) == 1, sorted(pools))
    name, pool = next(iter(pools.items()))
    expect("the one pool is the broker pool", name == "dfe-kafka-pool", name)
    expect("it carries BOTH roles -- combined KRaft",
           sorted(pool["spec"]["roles"]) == ["broker", "controller"], pool["spec"]["roles"])


def test_no_controller_pool_manifest_of_any_kind_at_the_defaults() -> None:
    """Named separately from the roles check: a pool that rendered under a
    different kind or name would still re-form the quorum."""
    docs = render()
    stray = [d["metadata"]["name"] for d in docs
             if "controller" in str(d.get("metadata", {}).get("name", ""))]
    expect("nothing named for a controller renders at the defaults", stray == [], stray)


def test_the_opt_in_really_does_split_the_pool() -> None:
    """The negative checks above would also pass against a chart that could
    never render a controller pool at all, so prove the flag still works."""
    values = overlay("kafka:\n  controllerPool:\n    enabled: true\n")
    pools = node_pools(render(values))
    values.unlink()
    expect("two pools once the opt-in is on", len(pools) == 2, sorted(pools))
    controller = pools.get("dfe-kafka-controller")
    expect("the controller pool renders under its own name", controller is not None, sorted(pools))
    if controller is not None:
        expect("it carries the controller role alone",
               controller["spec"]["roles"] == ["controller"], controller["spec"]["roles"])
    broker = pools.get("dfe-kafka-pool")
    expect("the broker pool drops the controller role",
           broker is not None and broker["spec"]["roles"] == ["broker"],
           broker["spec"]["roles"] if broker else None)


def main() -> int:
    with standalone():
        test_the_shipped_defaults_render_no_separate_controller_pool()
        test_no_controller_pool_manifest_of_any_kind_at_the_defaults()
        test_the_opt_in_really_does_split_the_pool()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
