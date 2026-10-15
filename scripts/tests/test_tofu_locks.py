#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_tofu_locks.py
#  Purpose:      Hold every OpenTofu root to a committed provider lock, so a run
#                installs the providers it was tested with and not the newest
#                the constraint allows that day.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The committed .terraform.lock.hcl of each root under terraform/environments.

    python3 -m pytest scripts/tests/test_tofu_locks.py -q

A `~>` constraint names a range, so `tofu init` with no lock takes the newest
release in it, whatever was published that day. The lock turns the range into one
version and its hashes. These tests hold the lock to being committed (not ignored),
complete, never refreshed by a run, and enforced where CI initialises each root.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENVIRONMENTS = REPO_ROOT / "terraform" / "environments"
LOCK = ".terraform.lock.hcl"
REGISTRY = "registry.opentofu.org"
TF_VALIDATE = REPO_ROOT / ".github" / "workflows" / "tf-validate.yml"

PROVIDER_BLOCK = re.compile(r'(?ms)^provider "(?P<source>[^"]+)" \{\n(?P<body>.*?)^\}')


def _roots() -> list[Path]:
    """Every directory under terraform/environments that holds .tf files."""
    return sorted({tf.parent for tf in ENVIRONMENTS.rglob("*.tf")})


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _providers(root: Path) -> dict[str, str]:
    """Provider source to its block body, read from the root's lock file."""
    text = (root / LOCK).read_text(encoding="utf-8")
    return {m["source"]: m["body"] for m in PROVIDER_BLOCK.finditer(text)}


def test_the_roots_are_found() -> None:
    assert [r.name for r in _roots()] == ["aws", "aws-state", "local"]


@pytest.mark.parametrize("root", _roots(), ids=lambda root: root.name)
def test_every_root_commits_its_lock_file(root: Path) -> None:
    assert (root / LOCK).is_file(), f"{root.relative_to(REPO_ROOT)} has no {LOCK}: run tofu init, then commit it"
    ignored = _git("check-ignore", "-q", str((root / LOCK).relative_to(REPO_ROOT)))
    assert ignored.returncode == 1, f"{LOCK} of {root.name} is ignored by .gitignore, so it can never be committed"


def test_a_modules_lock_file_stays_ignored() -> None:
    """Validating a module alone writes one; only a root's is the run's."""
    ignored = _git("check-ignore", "-q", f"terraform/modules/managed-kafka/msk/{LOCK}")
    assert ignored.returncode == 0


@pytest.mark.parametrize("root", _roots(), ids=lambda root: root.name)
def test_a_lock_pins_every_provider_to_one_version_with_hashes_for_each_platform(root: Path) -> None:
    providers = _providers(root)
    assert providers, f"{root.name}'s lock names no provider"
    for source, body in providers.items():
        assert source.split("/")[0] == REGISTRY, source
        version = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', body)
        assert version, f"{source} names no version"
        assert re.fullmatch(r"\d+\.\d+\.\d+", version[1]), f"{source} is not pinned to one version"
        # linux_amd64, linux_arm64 and darwin_arm64 each carry an h1 hash, and zh is the registry's own checksum.
        assert len(re.findall(r'"h1:', body)) >= 3, f"{source} has fewer than three platform hashes"
        assert '"zh:' in body, f"{source} has no registry checksum"


def test_no_run_refreshes_the_providers() -> None:
    """`init -upgrade` ignores the lock, so nothing a run or CI executes may pass it."""
    scanned = [
        *(REPO_ROOT / "bootstrap").rglob("*.sh"),
        *(REPO_ROOT / ".github").rglob("*.yml"),
        *(p for p in (REPO_ROOT / "scripts").iterdir() if p.is_file()),
    ]
    init_upgrade = re.compile(r"(?:tofu|terraform)\b[^\n]*\binit\b[^\n]*-upgrade")
    found = [
        str(path.relative_to(REPO_ROOT))
        for path in scanned
        if init_upgrade.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert found == []


def test_ci_initialises_each_root_against_the_committed_lock() -> None:
    text = TF_VALIDATE.read_text(encoding="utf-8")
    roots_job = text.split("validate-roots:", 1)[1]
    inits = [line for line in roots_job.splitlines() if " init " in line and "tofu" in line]
    assert inits, "validate-roots runs no tofu init"
    for line in inits:
        assert "-lockfile=readonly" in line, f"CI would write a fresh lock instead of failing on a stale one: {line.strip()}"
