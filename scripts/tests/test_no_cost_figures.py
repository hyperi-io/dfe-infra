#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_no_cost_figures.py
#  Purpose:      Guard the costing rule: everything a deployer reads describes
#                cost as a T-shirt bucket and a pricing model, never a rate.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Refuse a currency figure anywhere a deployer reads it.

Buckets are defined once, in `docs/deployment/aws.md#how-costs-are-described`,
and a rate written down beside them is stale the day it lands and wrong for
every account but the one it was read on. This sweeps the docs, the dial, the
overlays, the chart values, the OpenTofu comments and the resolver's own
rendered output, and names the file and line of anything carrying a figure.

    python3 -m pytest scripts/tests/test_no_cost_figures.py -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sizing"

sys.path.insert(0, str(SCRIPTS))
import resolve_sizing  # noqa: E402

# Three shapes of currency figure: a dollar sign on a number, a currency code on
# one, and a number attached to a unit of billing.
CURRENCY = re.compile(
    r"(?<!\$)\$\s?\d"
    r"|\b(?:USD|AUD|EUR|GBP)\s*\$?\s*[\d.]"
    r"|\d[\d.,]*\s*(?:/|per )(?:month|mo\b|hour|hr\b|GB|LCU-hour|request)"
)

# Only a comment can carry prose in OpenTofu, so the rest of a .tf file is not
# swept and a resource named after a rate is not what this guards against.
TF_COMMENT = re.compile(r"(?://|#)(.*)$")


def _hits(text: str, label: str) -> list[str]:
    return [
        f"{label}:{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if CURRENCY.search(line)
    ]


def _tf_comment_hits(path: Path) -> list[str]:
    found: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        comment = TF_COMMENT.search(line)
        if comment and CURRENCY.search(comment.group(1)):
            found.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    return found


def _committed_files() -> list[Path]:
    paths = sorted(REPO_ROOT.glob("docs/**/*.md"))
    paths += [REPO_ROOT / "README.md", REPO_ROOT / "deployment.example.yaml"]
    paths += sorted((REPO_ROOT / "argocd" / "values").glob("*.yaml"))
    paths += sorted(REPO_ROOT.glob("helm/charts/*/values.yaml"))
    paths += sorted((REPO_ROOT / "sizing").glob("**/*.yaml"))
    paths += sorted((REPO_ROOT / "sizing").glob("**/*.md"))
    return paths


def test_no_committed_file_a_deployer_reads_carries_a_cost_figure() -> None:
    found: list[str] = []
    for path in _committed_files():
        found += _hits(path.read_text(encoding="utf-8"), str(path.relative_to(REPO_ROOT)))
    assert not found, "cost figures found -- use a bucket and the pricing model:\n" + "\n".join(found)


def test_no_opentofu_comment_carries_a_cost_figure() -> None:
    found: list[str] = []
    for path in sorted((REPO_ROOT / "terraform").glob("**/*.tf")):
        found += _tf_comment_hits(path)
    assert not found, "cost figures found -- use a bucket and the pricing model:\n" + "\n".join(found)


@pytest.fixture(scope="module")
def resolved(tmp_path_factory) -> Path:
    """One fixture dial resolved, with the spend warning deliberately tripped.

    A5 is the one finding that used to print an amount, so the dial sets a
    threshold low enough that the report has to render it.
    """
    out = tmp_path_factory.mktemp("no-cost-figures")
    dial = out / "deployment.yaml"
    dial.write_text(
        "apiVersion: dfe.hyperi.io/v1\n"
        "kind: DeployContext\n"
        "substrate: k8s\n"
        "metadata:\n"
        "  name: dfe-test\n"
        "target:\n"
        "  provision:\n"
        "    cloud: aws\n"
        "    region: us-west-2\n"
        "profile: scale\n"
        "kafka:\n"
        "  provider: strimzi\n"
        "sizing:\n"
        "  focus: economy\n"
        "  ingest_gb_per_day: 10000\n"
        "  spend_warn_usd_month: 1\n"
        "retention:\n"
        "  default_ttl_days: 90\n"
        "k8s:\n"
        "  env: test\n"
        "  cloud: aws\n",
        encoding="utf-8",
    )
    resolve_sizing.main(
        ["--dial", str(dial), "--fixtures", str(FIXTURES), "--cloud", "aws", "--out", str(out)]
    )
    return out


def test_the_rendered_report_carries_no_cost_figure(resolved: Path) -> None:
    report = resolved / "sizing" / "scale.report.md"
    text = report.read_text(encoding="utf-8")
    assert "A5" in text, "the fixture dial did not trip the spend warning it exists to render"
    assert not _hits(text, "sizing/scale.report.md"), "\n".join(_hits(text, str(report)))


def test_the_written_resolved_yaml_carries_a_bucket_and_no_figure(resolved: Path) -> None:
    text = (resolved / "sizing" / "resolved.yaml").read_text(encoding="utf-8")
    assert "compute_usd_per_hour" not in text
    assert re.search(r"^compute_bucket: \"(XS|S|M|L|XL)\"$", text, re.M), text
    assert not _hits(text, "sizing/resolved.yaml")
