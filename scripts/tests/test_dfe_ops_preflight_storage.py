#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_preflight_storage.py
#  Purpose:      Prove preflight's storage verdict agrees with what bootstrap
#                does: a fresh EKS cluster with only gp2 passes, because
#                bootstrap creates the named class there, while the same
#                missing class off AWS is still refused.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for dfe-ops `_storage_class_verdict`, the preflight storage check.

Offline: the StorageClass list arrives as arguments, so nothing calls kubectl.

    python3 -m pytest scripts/tests/test_dfe_ops_preflight_storage.py -q
    python3 scripts/tests/test_dfe_ops_preflight_storage.py
"""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_loader = importlib.machinery.SourceFileLoader(
    "dfeops_storage", str(REPO_ROOT / "scripts" / "dfe-ops")
)
_spec = importlib.util.spec_from_loader("dfeops_storage", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_storage"] = dfeops
_loader.exec_module(dfeops)
verdict = dfeops._storage_class_verdict


def test_fresh_eks_with_only_gp2_passes() -> None:
    level, msg = verdict("gp3", "aws", ["gp2"], [])
    expect("a missing gp3 on AWS is ok", level == "ok", f"got {level}: {msg}")
    expect("the message says bootstrap creates it", "bootstrap creates it" in msg, msg)


def test_a_missing_named_class_off_aws_is_refused() -> None:
    level, msg = verdict("gp3", "gcp", ["standard-rwo"], ["standard-rwo"])
    expect("a missing class on GCP fails", level == "fail", f"got {level}: {msg}")
    expect("the message lists what exists", "standard-rwo" in msg, msg)


def test_a_missing_named_class_with_no_cloud_is_refused() -> None:
    level, _ = verdict("fast-ssd", "", ["local-path"], [])
    expect("on-prem with the wrong class fails", level == "fail", f"got {level}")


def test_an_existing_named_class_passes() -> None:
    level, _ = verdict("gp3", "aws", ["gp2", "gp3"], [])
    expect("an existing class is ok", level == "ok", f"got {level}")


def test_local_path_on_an_empty_cluster_passes() -> None:
    level, _ = verdict("local-path", "", [], [])
    expect("bootstrap installs local-path", level == "ok", f"got {level}")


def test_no_named_class_uses_the_default() -> None:
    level, msg = verdict("", "aws", ["gp2"], ["gp2"])
    expect("a default class is ok", level == "ok" and "gp2" in msg, f"got {level}: {msg}")


def test_no_named_class_and_no_default_warns() -> None:
    level, _ = verdict("", "", [], [])
    expect("no class at all only warns", level == "warn", f"got {level}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
