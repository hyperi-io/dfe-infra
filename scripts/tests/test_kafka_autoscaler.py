#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_kafka_autoscaler.py
#  Purpose:      Prove the KEDA broker-count autoscaler never renders with an
#                empty trigger list, and that a landing source turns into a
#                real trigger.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""gate-3-correctness.md P1-2: at the shipped scale-profile defaults,
kafka.autoscaling.enabled is true but kafka.landingTopics.sources is empty and
the Prometheus trigger is off, so the ScaledObject that used to render carried
`triggers: null` -- KEDA's CRD marks `triggers` required, so the apply was
rejected and the kafka Application went SyncFailed on the very defaults this
repo ships.

The fix gates the WHOLE autoscaler (ConfigMap, TriggerAuthentication and
ScaledObject) on there being at least one trigger, and prints why on NOTES.txt
so "armed but nothing rendered" reads as the intended state, not a bug.

    python3 scripts/tests/test_kafka_autoscaler.py

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
CHART = REPO_ROOT / "helm" / "charts" / "kafka"
VALUES = REPO_ROOT / "argocd" / "values"

# The cascade a real scale-tier deploy layers, minus the deploy-repo overlay.
BASE_CASCADE = [VALUES / "common.yaml", VALUES / "profile-scale.yaml"]


def render(*extra_values: Path) -> list[dict]:
    """Every manifest the chart renders under the scale cascade."""
    cmd = ["helm", "template", "kafka", str(CHART), "--set", "appNamespace=dfe"]
    for v in [*BASE_CASCADE, *extra_values]:
        cmd += ["-f", str(v)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def render_notes(*extra_values: Path) -> str:
    """NOTES.txt's rendered text. `helm template` never prints it -- NOTES
    lives in the release's `.Info.Notes`, a separate field from the manifest
    list `--show-only` filters over, not one of the documents `template`
    emits at all. `helm install --dry-run=client` does print it, appended
    after a `NOTES:` marker, so that is what this reads."""
    cmd = [
        "helm", "install", "kafka", str(CHART),
        "--dry-run=client", "--namespace", "default",
        "--set", "appNamespace=dfe",
    ]
    for v in [*BASE_CASCADE, *extra_values]:
        cmd += ["-f", str(v)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm install --dry-run failed:\n{out.stderr}")
    return out.stdout.rsplit("NOTES:", 1)[-1]


def scaled_objects(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d.get("kind") == "ScaledObject"]


def trigger_auths(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d.get("kind") == "TriggerAuthentication"]


def test_scale_defaults_render_no_autoscaler_and_say_why() -> None:
    """The shipped scale profile declares no landing source and no Prometheus
    endpoint -- exactly the shape that used to ship a broken ScaledObject."""
    docs = render()
    expect("no ScaledObject at the shipped defaults", not scaled_objects(docs), scaled_objects(docs))
    expect("no TriggerAuthentication either -- nothing left to authenticate for",
           not trigger_auths(docs), trigger_auths(docs))
    expect("no broker-scaler username ConfigMap either",
           not [d for d in docs if d.get("kind") == "ConfigMap"
                and d["metadata"]["name"].endswith("-broker-scaler-username")])
    notes = render_notes()
    expect(
        "NOTES.txt explains autoscaling is armed but idle",
        "autoscaling.enabled is true, but no KEDA ScaledObject was rendered" in notes,
        notes,
    )


def test_one_landing_source_renders_one_trigger() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write("kafka:\n  landingTopics:\n    sources:\n      - name: okta\n")
        overlay = Path(fh.name)
    docs = render(overlay)
    overlay.unlink()
    scaled = scaled_objects(docs)
    expect("exactly one ScaledObject renders", len(scaled) == 1, scaled)
    triggers = scaled[0]["spec"]["triggers"]
    expect("exactly one trigger, for the one landing source", len(triggers) == 1, triggers)
    expect("the trigger watches that source's landing topic",
           triggers[0]["metadata"]["topic"] == "okta_land", triggers[0])
    expect("a TriggerAuthentication renders to back it",
           len(trigger_auths(docs)) == 1, trigger_auths(docs))


def test_two_landing_sources_render_two_triggers() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(
            "kafka:\n  landingTopics:\n    sources:\n"
            "      - name: okta\n"
            "      - name: m365\n"
        )
        overlay = Path(fh.name)
    docs = render(overlay)
    scaled = scaled_objects(docs)
    expect("exactly one ScaledObject renders", len(scaled) == 1, scaled)
    triggers = scaled[0]["spec"]["triggers"]
    expect("exactly two triggers, one per landing source", len(triggers) == 2, triggers)
    topics = {t["metadata"]["topic"] for t in triggers}
    expect("one trigger per source's landing topic",
           topics == {"okta_land", "m365_land"}, topics)
    notes = render_notes(overlay)
    overlay.unlink()
    expect("no idle-autoscaler NOTES line once triggers exist",
           "no KEDA ScaledObject was rendered" not in notes, notes)


def test_the_prometheus_trigger_alone_is_enough_to_render() -> None:
    """No landing source, but a Prometheus endpoint wired up -- the OTHER way
    to have at least one trigger, so the autoscaler still renders."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(
            "kafka:\n  autoscaling:\n    metrics:\n"
            "      usePrometheus: true\n"
            "      prometheusUrl: http://prometheus.monitoring:9090\n"
        )
        overlay = Path(fh.name)
    docs = render(overlay)
    scaled = scaled_objects(docs)
    expect("the ScaledObject renders on the Prometheus trigger alone", len(scaled) == 1, scaled)
    triggers = scaled[0]["spec"]["triggers"]
    expect("exactly one trigger, the prometheus one", len(triggers) == 1 and triggers[0]["type"] == "prometheus",
           triggers)
    notes = render_notes(overlay)
    overlay.unlink()
    expect("NOTES.txt stays quiet -- the autoscaler is not idle",
           "no KEDA ScaledObject was rendered" not in notes, notes)


def main() -> int:
    with standalone():
        test_scale_defaults_render_no_autoscaler_and_say_why()
        test_one_landing_source_renders_one_trigger()
        test_two_landing_sources_render_two_triggers()
        test_the_prometheus_trigger_alone_is_enough_to_render()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
