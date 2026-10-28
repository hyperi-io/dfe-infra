#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         check_signing_keys.py
#  Purpose:      Fail while there is still time to act on a signing key or an
#                upstream hold that will otherwise break a build on its date.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Watch the signing keys the toolbox pins and the upstream fix the gen-8 hold waits on.

    python3 scripts/check_signing_keys.py            # vendored keys, offline
    python3 scripts/check_signing_keys.py --online   # plus pkgs.k8s.io, the AWS docs, and the vpc-cni fix

The toolbox verifies kubectl, the AWS CLI and the Session Manager plugin against
keys pinned by fingerprint. A key that expires, or rotates to a new fingerprint,
fails the image build closed on the day. This fails WARN_DAYS before that day.

Offline: every vendored key under docker/dfe-toolbox must be valid for more than
WARN_DAYS. Online: the key pkgs.k8s.io serves must still carry the Dockerfile's
pinned fingerprint and be valid for more than WARN_DAYS, the AWS CLI key on the
install docs page must be the one vendored, and aws-network-policy-agent #686 (the
gen-8 hold in shapes/compute-shapes.yaml) must still be open.

Needs gpg. --online also needs network and an authenticated gh.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLBOX = REPO_ROOT / "docker" / "dfe-toolbox"
WARN_DAYS = 60
AWS_DOCS = "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
NPA_ISSUE = "aws/aws-network-policy-agent/issues/686"
PGP_BLOCK = re.compile(r"-----BEGIN PGP PUBLIC KEY BLOCK-----.*?-----END PGP PUBLIC KEY BLOCK-----", re.S)
TAG = re.compile(r"<[^>]+>")


def show_keys(armoured: str) -> list[dict]:
    """Each primary key in an armoured block: fingerprint and expiry (None when it never expires)."""
    out = subprocess.run(
        ["gpg", "--show-keys", "--with-colons", "--batch"],
        input=armoured,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    keys: list[dict] = []
    want_fpr = False
    for line in out.splitlines():
        cols = line.split(":")
        if cols[0] == "pub":
            expires = int(cols[6]) if cols[6] else None
            keys.append({"expires": expires, "fingerprint": ""})
            want_fpr = True
        elif cols[0] == "fpr" and want_fpr:
            keys[-1]["fingerprint"] = cols[9]
            want_fpr = False
    return keys


def days_left(expires: int | None, now: datetime.datetime) -> float:
    if expires is None:
        return float("inf")
    return (datetime.datetime.fromtimestamp(expires, datetime.UTC) - now).total_seconds() / 86400


def expiry_findings(label: str, keys: list[dict], now: datetime.datetime) -> list[str]:
    return [
        f"{label}: key {k['fingerprint']} {'has expired' if days_left(k['expires'], now) < 0 else 'expires'} "
        f"{datetime.datetime.fromtimestamp(k['expires'], datetime.UTC):%Y-%m-%d}, inside {WARN_DAYS} days"
        for k in keys
        if days_left(k["expires"], now) < WARN_DAYS
    ]


def vendored_findings(now: datetime.datetime) -> list[str]:
    findings: list[str] = []
    for asc in sorted(TOOLBOX.rglob("*.asc")):
        findings += expiry_findings(str(asc.relative_to(REPO_ROOT)), show_keys(asc.read_text(encoding="utf-8")), now)
    return findings


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def pinned_kubernetes() -> tuple[str, str]:
    """The Dockerfile's kubectl minor (v1.35) and the fingerprint it pins pkgs.k8s.io's key to."""
    text = (TOOLBOX / "base" / "Dockerfile").read_text(encoding="utf-8")
    version = re.search(r'^ARG KUBECTL_VERSION="(v\d+\.\d+)\.\d+"', text, re.M)
    pin = re.search(r"kubernetes\.gpg\s*\\\s*\n\s*([0-9A-F]{40})", text)
    if not version or not pin:
        raise SystemExit("base/Dockerfile no longer names KUBECTL_VERSION and the kubernetes.gpg fingerprint")
    return version.group(1), pin.group(1)


def kubernetes_findings(now: datetime.datetime) -> list[str]:
    minor, pinned = pinned_kubernetes()
    keys = show_keys(fetch(f"https://pkgs.k8s.io/core:/stable:/{minor}/deb/Release.key"))
    ours = [k for k in keys if k["fingerprint"] == pinned]
    if not ours:
        served = ", ".join(k["fingerprint"] for k in keys) or "none"
        return [f"pkgs.k8s.io {minor}: serves {served}, not the pinned {pinned}. Move the pin in base/Dockerfile"]
    return expiry_findings(f"pkgs.k8s.io {minor}", ours, now)


def aws_findings(now: datetime.datetime) -> list[str]:
    vendored = {k["fingerprint"]: k for k in show_keys((TOOLBOX / "aws" / "aws-cli.asc").read_text(encoding="utf-8"))}
    published = [k for block in PGP_BLOCK.findall(fetch(AWS_DOCS)) for k in show_keys(_unescape(block))]
    findings = []
    for key in published:
        known = vendored.get(key["fingerprint"])
        if known is None:
            findings.append(f"AWS docs publish key {key['fingerprint']}, which aws-cli.asc does not hold")
        elif (key["expires"] or 0) > (known["expires"] or 0):
            findings.append(f"AWS docs publish a later expiry for {key['fingerprint']}: refresh aws-cli.asc")
    return findings


def _unescape(block: str) -> str:
    import html

    return html.unescape(TAG.sub("", block)).strip() + "\n"


def npa_findings() -> list[str]:
    out = subprocess.run(
        ["gh", "api", f"repos/{NPA_ISSUE}", "--jq", ".state"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if out.returncode != 0:
        return [f"could not read {NPA_ISSUE}: {out.stderr.strip()}"]
    if out.stdout.strip() == "closed":
        return [f"{NPA_ISSUE} is closed: check the pinned vpc-cni carries the fix, then lift the gen-8 hold in shapes/compute-shapes.yaml"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--online", action="store_true", help="also check pkgs.k8s.io, the AWS docs and the vpc-cni fix")
    args = parser.parse_args(argv)
    now = datetime.datetime.now(datetime.UTC)
    findings = vendored_findings(now)
    if args.online:
        findings += kubernetes_findings(now) + aws_findings(now) + npa_findings()
    for finding in findings:
        print(f"FAIL {finding}")
    if not findings:
        print("ok: signing keys valid past the warning window" + (", upstream holds unchanged" if args.online else ""))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
