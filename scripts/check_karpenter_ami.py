#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/check_karpenter_ami.py
#  Purpose:      Warn when versions.yaml's Karpenter AL2023 alias pin lags the
#                newest EKS AL2023 release for the stack's Kubernetes minor.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Report how far ``operators.karpenter-al2023-ami`` sits behind the newest release.

The pin is the node-pools chart's own default alias, which a deployment runs
only until ``scripts/resolve_sizing.py images`` records the alias its release
gates pick. check_versions_drift.py holds the chart to it. This only says when a
newer release exists, so it WARNS and never fails. A lagging pin, a source it
cannot read and a pin it cannot parse all exit 0.

The lag is measured between the two releases' GitHub published_at dates, never
their tags: a tag names the build date, and v20260311 was published 47 days
after it.

The minor is the stack's ``platform.kubernetes`` ceiling, the newest minor the
stack is proven on, read the way bootstrap/check_platform.py reads it. The
newest release comes from one of two sources:

    github  awslabs/amazon-eks-ami's releases, through ``gh api`` -- the
            default, because CI has a token and no AWS credential
    ssm     the EKS ``recommended`` SSM parameter for the minor in a region,
            through scripts/node_images.py -- the per-region authority, which
            needs AWS credentials

    python3 scripts/check_karpenter_ami.py
    python3 scripts/check_karpenter_ami.py --source ssm --region us-west-2
    python3 scripts/check_karpenter_ami.py --releases-json recorded.json

Stdlib only. Under GitHub Actions a lag is also a ``::warning::`` annotation.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import node_images
from aws_cli import run_cli
from yaml_subset import YamlSubsetError, at
from yaml_subset import parse as parse_yaml_subset

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_FILE = REPO_ROOT / "versions.yaml"
PIN_PATH = ("operators", "karpenter-al2023-ami")

sys.path.insert(1, str(REPO_ROOT / "bootstrap"))
from check_platform import parse_requirement  # noqa: E402

EKS_AMI_REPO = node_images.EKS_AMI_REPO

PIN = re.compile(r"^v(?P<date>\d{8})$")


@dataclass(frozen=True, slots=True)
class Newest:
    """The newest AL2023 release for one minor, and where it was read."""

    tag: str
    published: str
    source: str


def stack_facts(versions: Path, stack: str | None) -> tuple[str, str, str]:
    """(stack name, pinned alias version, Kubernetes minor) from versions.yaml.

    Raises:
        ValueError: The stack, its pin or its platform minor is missing.
    """
    try:
        tree = parse_yaml_subset(versions.read_text(encoding="utf-8"), source=str(versions))
    except (OSError, YamlSubsetError) as err:
        raise ValueError(f"cannot read {versions}: {err}") from err
    name = stack or str(at(tree, ("current",)) or "")
    body = at(tree, ("stacks", name))
    if not name or not isinstance(body, dict):
        raise ValueError(f"versions.yaml has no stack {name!r}")
    pin = at(body, PIN_PATH)
    platform = at(body, ("platform", "kubernetes"))
    if not isinstance(pin, str) or not pin.strip():
        raise ValueError(f"stack {name} carries no {'.'.join(PIN_PATH)} pin")
    if not isinstance(platform, str) or not platform.strip():
        raise ValueError(f"stack {name} carries no platform.kubernetes")
    floor, ceiling = parse_requirement(platform)
    return name, pin.strip(), (ceiling or floor).removeprefix("v")


def newest_from_releases(releases: object, minor: str, arch: str) -> Newest | None:
    """The newest published release that ships the AL2023 standard image for this minor."""
    if not isinstance(releases, list):
        return None
    wanted = f"amazon-eks-node-al2023-{arch}-standard-{minor}-v"
    published = [
        r for r in releases
        if isinstance(r, dict) and not r.get("draft") and not r.get("prerelease")
    ]
    for release in sorted(published, key=lambda r: str(r.get("published_at", "")), reverse=True):
        names = release.get("ami_names")
        if isinstance(names, list) and any(str(n).startswith(wanted) for n in names):
            return Newest(str(release.get("tag_name", "")), str(release.get("published_at", "")), "github")
    return None


def fetch_releases() -> object:
    """The newest amazon-eks-ami releases, the same read node_images.py gates.

    Raises:
        node_images.ImageLookupError: gh is missing or refused the read.
    """
    args = list(node_images.EKS_RELEASES_ARGS)
    try:
        done = run_cli("gh", args, timeout=60)
    except FileNotFoundError as err:
        raise node_images.ImageLookupError("the gh CLI is not on PATH") from err
    except subprocess.TimeoutExpired as err:
        raise node_images.ImageLookupError("gh api took over 60s") from err
    if done.returncode != 0:
        tail = done.stderr.strip().splitlines()[-1:] or ["no stderr"]
        raise node_images.ImageLookupError(f"gh api {EKS_AMI_REPO} releases failed: {tail[0]}")
    try:
        return json.loads(done.stdout or "[]")
    except json.JSONDecodeError as err:
        raise node_images.ImageLookupError(f"gh api answered no JSON: {err}") from err


def published_at(releases: object, tag: str) -> str:
    """A release's GitHub published_at, or "" when the read did not reach it."""
    if not isinstance(releases, list):
        return ""
    for release in releases:
        if isinstance(release, dict) and release.get("tag_name") == tag:
            return str(release.get("published_at") or "")
    return ""


def lag(pin_published: str, newest: Newest) -> int | None:
    """Days from the pin's release to the newest by published_at, or None for an unknown date."""
    try:
        pinned = datetime.fromisoformat(pin_published)
        latest = datetime.fromisoformat(newest.published)
    except ValueError:
        return None
    return (latest - pinned).days


def _say(level: str, message: str) -> None:
    """One line on stderr, and the matching annotation under GitHub Actions."""
    print(f"check_karpenter_ami: {level.upper()} {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{message}")


def main(argv: list[str] | None = None) -> int:
    """Report the pin's lag. Exits 0 whatever it finds: this is a warning, never a gate."""
    parser = argparse.ArgumentParser(prog="check_karpenter_ami.py", description=__doc__.splitlines()[0])
    parser.add_argument("--stack", default=None, help="the stack to read (default: the current pointer)")
    parser.add_argument("--versions", type=Path, default=VERSIONS_FILE, help="versions.yaml to read")
    parser.add_argument("--minor", default=None, help="override the stack's Kubernetes minor")
    parser.add_argument("--arch", default="arm64", help="the node architecture (default arm64)")
    parser.add_argument("--source", choices=("github", "ssm"), default="github")
    parser.add_argument("--region", default="us-west-2", help="the region the ssm source reads")
    parser.add_argument("--releases-json", type=Path, default=None, help="recorded github answer")
    parser.add_argument("--fixtures", type=Path, default=None, help="recorded ssm answers")
    args = parser.parse_args(argv)

    try:
        stack, pin, minor = stack_facts(args.versions, args.stack)
    except ValueError as err:
        _say("warning", f"Karpenter AMI pin not checked -- {err}")
        return 0
    minor = args.minor or minor

    releases: object = []
    try:
        if args.source == "ssm":
            read = (
                node_images.fixture_reader(args.fixtures)
                if args.fixtures
                else node_images.live_reader()
            )
            target = node_images.Target("aws", args.region, "", minor, args.arch)
            alias = node_images.recommended_ami_alias(target, read)
            newest: Newest | None = Newest(alias.split("@", 1)[1], "", f"ssm {args.region}")
        else:
            releases = (
                json.loads(args.releases_json.read_text(encoding="utf-8"))
                if args.releases_json
                else fetch_releases()
            )
            newest = newest_from_releases(releases, minor, node_images.AWS_ARCH.get(args.arch, args.arch))
    except (node_images.ImageLookupError, OSError, json.JSONDecodeError) as err:
        _say("notice", f"Karpenter AMI pin not checked -- {err}. This is a skip, not a pass.")
        return 0
    if newest is None:
        _say("notice", f"no {EKS_AMI_REPO} release ships an AL2023 {args.arch} image for {minor}")
        return 0

    if not PIN.match(pin):
        _say("warning", f"stack {stack} pins {'.'.join(PIN_PATH)} {pin!r}, not v<YYYYMMDD>")
        return 0
    published = f", published {newest.published[:10]}" if newest.published else ""
    behind = None if pin == newest.tag else lag(published_at(releases, pin), newest)
    # A date the read did not reach leaves only the tags to order by, and no day count.
    unmeasured = behind is None and pin < newest.tag
    if unmeasured or (behind is not None and behind > 0):
        distance = "behind" if behind is None else f"{behind} day(s) behind"
        undated = " One of the two release dates was not read, so the lag has no day count."
        undated = undated if unmeasured else ""
        _say(
            "warning",
            f"stack {stack} pins {'.'.join(PIN_PATH)} {pin}, {distance} the newest AL2023 "
            f"release for Kubernetes {minor}, {newest.tag} ({newest.source}{published}).{undated} "
            "A deployment runs the alias scripts/resolve_sizing.py images picks behind the "
            "release gates, and falls back to this pin only when none is recorded.",
        )
    else:
        print(
            f"check_karpenter_ami: OK stack {stack} pins {pin}, the newest AL2023 release for "
            f"Kubernetes {minor} ({newest.source}{published})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
