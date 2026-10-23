#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/node_images.py
#  Purpose:      Read each cloud's node image, and on aws pick the newest one
#                that clears the release gates, for the tfvars the shape
#                resolver writes.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The node-image cascade's first layer: the provider's image, behind release gates.

A node image reaches tofu through two layers, highest first:

    1. this lookup, written to ``node-images.auto.tfvars.json`` by
       ``scripts/resolve_sizing.py images``
    2. a value the operator set in tfvars (``terraform.tfvars``)

OpenTofu orders them itself: an ``*.auto.tfvars.json`` loads after
``terraform.tfvars``, so a key written here wins and a key left out lets the
layer below through. The aws root carries no image default, so a key neither
layer answers is refused at plan by name. ``node_image.pin: "true"`` in the
dial skips every lookup and empties the file, so the operator's tfvars value
holds -- a deliberate hold for a bad provider image or a reproducible rebuild.

Worker-node images are the one version class that moves without a DFE release,
and on aws every one passes release gates first. versions.yaml's ``node-images``
block holds the knobs, and the gates are:

    a  released at least ``cooldown-days`` ago, measured from the GitHub
       release's published_at or the EC2 image's CreationDate, never a tag date
    b  not a draft or prerelease
    c  not in the skip-list
    d  its own release notes carry no warning admonition
    e  no newer release's admonition names it as affected
    f  no open awslabs/amazon-eks-ami issue names it in its title
    g  at or above the floor

    EKS-optimised AL2023   the node groups' dated release and Karpenter's
                           EC2NodeClass alias, from awslabs/amazon-eks-ami's
                           releases: all seven gates, then the image must be
                           published for the minor in the deployment's region
    Amazon Linux 2023      the toolbox's and the tunnel forwarder's AMI ids,
                           from EC2 DescribeImages in the family of the AL2023
                           default SSM parameter: a, c and g only, because b, d,
                           e and f read GitHub notes only the EKS AMI publishes

The newest candidate that clears every gate wins. When none does, or the lookup
fails (GitHub or AWS unreachable, rate-limited or malformed), the value the
previous resolve recorded (``sizing/node-images.json``, which a deploy repo
commits) holds unless it is now skip-listed or below the floor, and is never
replaced by the newest release. With nothing recorded, the key is left out and
the plan refuses by name.

    gcp     GKE's server config ``defaultImageType`` for the location
    azure   AKS's version-tracking Linux OS SKU, once the location lists the
            minor as generally available

GitHub is read anonymously over HTTPS, one releases page and one issue search a
run, with ``GH_TOKEN`` or ``GITHUB_TOKEN`` sent only to raise the rate limit.

Stdlib only, like every other script here. Every provider read goes through one
``Reader``, so a test hands it recorded responses and the CLI hands it GitHub's
REST API and the live ``aws``/``gcloud``/``az`` binaries.
"""

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.client import HTTPMessage
from pathlib import Path
from typing import IO

from aws_cli import run_cli
from yaml_subset import YamlSubsetError, at
from yaml_subset import parse as parse_yaml_subset

TFVARS_NAME = "node-images.auto.tfvars.json"

# Under the resolver's sizing/, so the record travels into the deploy repo with resolved.yaml.
RECORD_NAME = "node-images.json"

# The node-pools chart overlay layer2-platform reads from the deploy repo's sizing/.
NODE_POOLS_VALUES_NAME = "node-pools.values.yaml"

# Answered for the node-pools chart rather than tofu, so no root declares it.
CHART_KEYS = frozenset({"karpenter_ami_alias"})

# (fixture name, CLI binary, argv) -> the CLI's stdout.
type Reader = Callable[[str, str, list[str]], str]

# compute-shapes.yaml spells x86 `amd64`, AWS's image paths spell it `x86_64`.
AWS_ARCH = {"arm64": "arm64", "amd64": "x86_64", "x86_64": "x86_64"}

# https://docs.aws.amazon.com/eks/latest/userguide/retrieve-ami-id.html
EKS_RECOMMENDED = (
    "/aws/service/eks/optimized-ami/{minor}/amazon-linux-2023/{arch}/standard/recommended"
)

# One release's image in a region, the parameter Karpenter's alias resolves through.
EKS_IMAGE_ID = (
    "/aws/service/eks/optimized-ami/{minor}/amazon-linux-2023/{arch}/standard/{name}/image_id"
)
EKS_IMAGE_NAME = "amazon-eks-node-al2023-{arch}-standard-{minor}-{tag}"

# The CreateNodegroup amiType for each AL2023 standard image the parameter above publishes.
# https://docs.aws.amazon.com/eks/latest/APIReference/API_CreateNodegroup.html
EKS_AMI_TYPES = {"arm64": "AL2023_ARM_64_STANDARD", "x86_64": "AL2023_x86_64_STANDARD"}

# https://docs.aws.amazon.com/linux/al2023/ug/ec2.html
AL2023_AMI = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{arch}"

# AL2023's own image names, e.g. al2023-ami-2023.12.20261012.0-kernel-6.12-arm64.
AL2023_NAME = re.compile(
    r"^al2023-ami-(?P<release>2023\.\d+\.(?P<date>\d{8})\.\d+)-(?P<kernel>kernel-[0-9.]+)-"
    r"(?P<arch>arm64|x86_64)$"
)

# AKS's own best-practice SKU for Linux, which tracks the newest validated Ubuntu per minor.
# https://learn.microsoft.com/en-us/azure/aks/upgrade-os-version
AKS_LINUX_OS_SKU = "Ubuntu"

EKS_AMI_REPO = "awslabs/amazon-eks-ami"
GITHUB_API = "https://api.github.com"

# Sent on every GitHub read, which refuses a request without one.
GITHUB_USER_AGENT = "dfe-infra-node-images"

# The reader binary name for a GitHub REST read: args are the URL and the most pages to follow.
HTTPS = "https"

# The newest hundred releases in one page, over a year of them, inside the anonymous rate limit.
EKS_RELEASES_ARGS = [f"{GITHUB_API}/repos/{EKS_AMI_REPO}/releases?per_page=100", "1"]
EKS_RELEASES_FIXTURE = "eks-ami-releases.json"

# One search for every open issue, pull requests left out, following its pages.
EKS_ISSUES_QUERY = urllib.parse.urlencode(
    {"q": f"repo:{EKS_AMI_REPO} is:issue is:open", "per_page": "100"}
)
EKS_ISSUES_ARGS = [f"{GITHUB_API}/search/issues?{EKS_ISSUES_QUERY}", "10"]
EKS_ISSUES_FIXTURE = "eks-ami-open-issues.json"

# What the gates read from a release body: its AL2023 standard image names, its
# per-minor release versions and its warning admonitions.
BODY_AMI_NAME = re.compile(r"amazon-eks-node-al2023-[a-z0-9_]+-standard-[0-9.]+-v[0-9]{8}")
BODY_RELEASE_VERSION = re.compile(r"\b[0-9]+\.[0-9]+\.[0-9]+-[0-9]{8}\b")
BODY_ADMONITION = re.compile(r"(?m)^> \[!(?:IMPORTANT|WARNING|CAUTION)\][^\n]*(?:\n>[^\n]*)*")
LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')

# A hedge that warns of a fault in the release itself.
WARNING_PHRASE = re.compile(r"\b(?:might cause|may cause|may experience|known issue)", re.I)
# A sentence that says it fixes something describes a fault elsewhere, never in this release.
FIX_WORD = re.compile(r"\b(?:fix|fixes|fixed|resolves|resolved|addresses|addressed)\b", re.I)
RELEASE_TAG = re.compile(r"\bv\d{8}\b")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

RELEASE_VERSION = re.compile(r"^(?P<patch>\d+\.\d+\.\d+)-(?P<date>\d{8})$")
EKS_IMAGE_NAME_DATE = re.compile(r"-v(?P<date>\d{8})$")
AMI_ID = re.compile(r"^ami-(?:[0-9a-f]{8}|[0-9a-f]{17})$")
KUBERNETES_MINOR = re.compile(r"^1\.\d+$")
EKS_TAG = re.compile(r"^v\d{8}$")
AL2023_DATE = re.compile(r"^\d{8}$")

POLICY_PATH = ("node-images",)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ImageLookupError(Exception):
    """A provider read that gave no usable answer."""


class NoEligibleImageError(ImageLookupError):
    """Every candidate failed a release gate.

    Attributes:
        refusals: One line per refused candidate, naming the gate.
    """

    def __init__(self, message: str, refusals: list[str]) -> None:
        super().__init__(message)
        self.refusals = refusals


class GatePolicyError(ValueError):
    """versions.yaml carries no usable ``node-images`` block."""


@dataclass(frozen=True, slots=True)
class Target:
    """What a lookup needs to know about the deployment.

    Attributes:
        cloud: The dial's target.provision.cloud token.
        region: The region (aws) or location (gcp, azure) the cluster lands in.
        account: The AWS account, GCP project or Azure subscription.
        kubernetes_version: The control-plane minor, e.g. 1.36.
        arch: The node architecture compute-shapes.yaml declares for the cloud.
    """

    cloud: str
    region: str
    account: str
    kubernetes_version: str
    arch: str


@dataclass(frozen=True, slots=True)
class GatePolicy:
    """The release-gate knobs from versions.yaml's ``node-images`` block.

    Attributes:
        cooldown_days: How old a release must be before it is eligible.
        eks_floor: The oldest EKS AMI release tag allowed, e.g. v20261012.
        eks_skip: Known-bad EKS AMI release tags, each with its reason.
        al2023_floor: The oldest AL2023 release date allowed, YYYYMMDD.
        al2023_skip: Known-bad AL2023 releases (2023.x.YYYYMMDD.n), each with its reason.
    """

    cooldown_days: int
    eks_floor: str
    eks_skip: Mapping[str, str]
    al2023_floor: str
    al2023_skip: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Release:
    """One awslabs/amazon-eks-ami release, as the gates read it."""

    tag: str
    published: datetime | None
    prerelease: bool
    ami_names: tuple[str, ...]
    release_versions: tuple[str, ...]
    admonitions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Pick:
    """One answered key.

    Attributes:
        value: What the tfvars file or the chart overlay carries.
        release: The release tag or AMI name the value came from, empty where
            the provider names none.
        skipped: One line per newer candidate a gate refused.
    """

    value: object
    release: str
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Resolution:
    """What one resolve answered.

    Attributes:
        values: Every key picked or held, tfvars and chart keys alike.
        record: The same keys as ``{"value": ..., "release": ...}``, for the record file.
        refused: Keys whose recorded value a gate now refuses, to drop from the record.
    """

    values: dict[str, object]
    record: dict[str, dict[str, object]]
    refused: frozenset[str]


def _json(raw: str, what: str) -> dict[str, object]:
    """Parse one provider answer, naming it when it is not a JSON object."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ImageLookupError(f"{what} is not JSON: {err}") from err
    if not isinstance(doc, dict):
        raise ImageLookupError(f"{what} is not a JSON object")
    return doc


def _json_list(raw: str, what: str) -> list[object]:
    """Parse one provider answer that must be a JSON array."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ImageLookupError(f"{what} is not JSON: {err}") from err
    if not isinstance(doc, list):
        raise ImageLookupError(f"{what} is not a JSON array")
    return doc


def _timestamp(raw: object) -> datetime | None:
    """An ISO 8601 UTC timestamp, or None when absent or unreadable."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _skip_map(raw: object, what: str) -> dict[str, str]:
    """A skip-list written as a map of release to reason, empty when written as "" or {}."""
    if raw is None or (isinstance(raw, str) and raw.strip() in ("", "{}")):
        return {}
    if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
        raise GatePolicyError(f"{what} must map each release to its reason")
    return {str(key): str(value) for key, value in raw.items()}


def _floor(raw: object, pattern: re.Pattern[str], what: str) -> str:
    """A floor spelt ``>=<release>``."""
    value = raw.strip().removeprefix(">=").strip() if isinstance(raw, str) else ""
    if not pattern.match(value):
        raise GatePolicyError(f"{what} must be >=<release> such as {pattern.pattern}, got {raw!r}")
    return value


def read_policy(versions: Path) -> GatePolicy:
    """The release-gate knobs, from versions.yaml's top-level ``node-images`` block.

    Raises:
        GatePolicyError: The file or its ``node-images`` block is missing or malformed.
    """
    try:
        tree = parse_yaml_subset(versions.read_text(encoding="utf-8"), source=str(versions))
    except (OSError, YamlSubsetError) as err:
        raise GatePolicyError(f"cannot read {versions}: {err}") from err
    block = at(tree, POLICY_PATH)
    if not isinstance(block, dict):
        raise GatePolicyError(f"{versions.name} carries no top-level node-images block")
    cooldown = str(block.get("cooldown-days", "")).strip()
    if not cooldown.isdigit():
        raise GatePolicyError(
            f"node-images.cooldown-days must be a whole number of days, got {cooldown!r}"
        )
    return GatePolicy(
        cooldown_days=int(cooldown),
        eks_floor=_floor(block.get("eks-ami-floor"), EKS_TAG, "node-images.eks-ami-floor"),
        eks_skip=_skip_map(block.get("eks-ami-skip"), "node-images.eks-ami-skip"),
        al2023_floor=_floor(
            block.get("al2023-ami-floor"), AL2023_DATE, "node-images.al2023-ami-floor"
        ),
        al2023_skip=_skip_map(block.get("al2023-ami-skip"), "node-images.al2023-ami-skip"),
    )


def _aws_arch(target: Target) -> str:
    """AWS's spelling of the target's architecture."""
    if target.arch not in AWS_ARCH:
        raise ImageLookupError(f"AWS publishes no image for arch {target.arch!r}")
    return AWS_ARCH[target.arch]


def _minor(target: Target) -> str:
    """The target's Kubernetes minor, refused before any read when it is not one."""
    minor = target.kubernetes_version
    if not KUBERNETES_MINOR.match(minor):
        raise ImageLookupError(f"{minor!r} is not a Kubernetes minor such as 1.36")
    return minor


def _ssm_value(target: Target, read: Reader, fixture: str, name: str) -> str:
    """The value of one public SSM parameter in the target's region."""
    args = [
        "ssm",
        "get-parameter",
        "--region",
        target.region,
        "--name",
        name,
        "--query",
        "Parameter.Value",
        "--output",
        "text",
    ]
    return read(fixture, "aws", args).strip()


def _age_refusal(published: datetime | None, now: datetime, cooldown_days: int) -> str | None:
    """Gate a: why a release is too young, or None once the cooldown has passed."""
    if published is None:
        return "carries no release date to measure the cooldown from"
    age = now - published
    if age < timedelta(0):
        return f"released {published.date().isoformat()}, after {now.date().isoformat()}"
    if age < timedelta(days=cooldown_days):
        return (
            f"released {published.date().isoformat()}, {age.days} day(s) ago, inside the "
            f"{cooldown_days}-day cooldown"
        )
    return None


# --- the EKS-optimised AL2023 image ---------------------------------------------


def releases_from(entries: list[object]) -> list[Release]:
    """GitHub release objects as the gates read them, newest published first.

    Raises:
        ImageLookupError: An entry is not a release object.
    """
    releases: list[Release] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("tag_name"), str):
            raise ImageLookupError(f"{EKS_AMI_REPO} releases carry an entry with no tag_name")
        body = str(entry.get("body") or "").replace("\r", "")
        releases.append(
            Release(
                tag=entry["tag_name"],
                published=_timestamp(entry.get("published_at")),
                prerelease=bool(entry.get("prerelease")) or bool(entry.get("draft")),
                ami_names=tuple(sorted(set(BODY_AMI_NAME.findall(body)))),
                release_versions=tuple(sorted(set(BODY_RELEASE_VERSION.findall(body)))),
                admonitions=tuple(BODY_ADMONITION.findall(body)),
            )
        )
    # An undated release sorts last, below every dated one.
    releases.sort(key=lambda r: (r.published is not None, r.published or _EPOCH), reverse=True)
    return releases


def parse_releases(raw: str) -> list[Release]:
    """The releases EKS_RELEASES_ARGS answered, newest published first.

    Raises:
        ImageLookupError: The answer is not a list of release objects.
    """
    return releases_from(_json_list(raw, f"{EKS_AMI_REPO} releases"))


def open_issue_titles(raw: str) -> list[str]:
    """The titles of the open issues EKS_ISSUES_ARGS answered, each as ``#<number> <title>``.

    Raises:
        ImageLookupError: The answer is not a list of issue objects.
    """
    titles: list[str] = []
    for issue in _json_list(raw, f"{EKS_AMI_REPO} open issues"):
        if not isinstance(issue, dict) or not isinstance(issue.get("title"), str):
            raise ImageLookupError(f"{EKS_AMI_REPO} open issues carry an entry with no title")
        titles.append(f"#{issue.get('number')} {issue['title']}")
    return titles


def warning_sentences(admonition: str) -> list[str]:
    """The sentences of one admonition that warn of a fault in its own release."""
    lines = [line.removeprefix(">").strip() for line in admonition.splitlines()]
    text = " ".join(line for line in lines if line and not line.startswith("[!"))
    return [
        sentence
        for sentence in SENTENCE_END.split(text)
        if WARNING_PHRASE.search(sentence) and not FIX_WORD.search(sentence)
    ]


def eks_refusals(
    release: Release,
    *,
    releases: list[Release],
    policy: GatePolicy,
    open_titles: list[str],
    now: datetime,
) -> list[str]:
    """Every gate, a to g, the release fails, and nothing when it is eligible."""
    refusals: list[str] = []
    if (young := _age_refusal(release.published, now, policy.cooldown_days)) is not None:
        refusals.append(young)
    if release.prerelease:
        refusals.append("a draft or prerelease")
    if release.tag in policy.eks_skip:
        refusals.append(f"skip-listed in versions.yaml: {policy.eks_skip[release.tag]}")
    for admonition in release.admonitions:
        refusals.extend(f"its own release notes warn: {s}" for s in warning_sentences(admonition))
    for newer in releases:
        if newer.tag == release.tag or newer.published is None or release.published is None:
            continue
        if newer.published <= release.published:
            continue
        if any(release.tag in RELEASE_TAG.findall(a) for a in newer.admonitions):
            refusals.append(f"{newer.tag}'s release notes name it as affected")
    refusals.extend(
        f"open issue {title}" for title in open_titles if release.tag.lower() in title.lower()
    )
    if release.tag < policy.eks_floor:
        refusals.append(f"below the floor {policy.eks_floor}")
    return refusals


def release_version_for(release: Release, minor: str) -> str | None:
    """The release's node-group release version for one minor, e.g. 1.36.4-20261012."""
    date = release.tag.removeprefix("v")
    for version in release.release_versions:
        match = RELEASE_VERSION.match(version)
        if match and match["date"] == date and match["patch"].startswith(f"{minor}."):
            return version
    return None


def _not_in_region(err: ImageLookupError) -> bool:
    """SSM's answer for a parameter the region does not publish."""
    return "ParameterNotFound" in str(err)


def eks_release(
    target: Target, read: Reader, policy: GatePolicy, now: datetime
) -> tuple[Release, str, tuple[str, ...]]:
    """The newest EKS AL2023 release that clears every gate and reaches the region.

    Returns:
        (the release, its release version for the target's minor, one line per
        newer candidate a gate refused).

    Raises:
        NoEligibleImageError: No candidate cleared every gate.
        ImageLookupError: GitHub or SSM gave no usable answer.
    """
    minor = _minor(target)
    arch = _aws_arch(target)
    releases = parse_releases(read(EKS_RELEASES_FIXTURE, HTTPS, list(EKS_RELEASES_ARGS)))
    titles = open_issue_titles(read(EKS_ISSUES_FIXTURE, HTTPS, list(EKS_ISSUES_ARGS)))
    refused: list[str] = []
    for release in releases:
        name = EKS_IMAGE_NAME.format(arch=arch, minor=minor, tag=release.tag)
        if name not in release.ami_names:
            continue
        reasons = eks_refusals(
            release, releases=releases, policy=policy, open_titles=titles, now=now
        )
        version = release_version_for(release, minor)
        if version is None:
            reasons.append(f"its release notes carry no {minor} release version")
        if reasons:
            if release.tag >= policy.eks_floor:
                refused.append(f"{release.tag}: {' | '.join(reasons)}")
            continue
        fixture = f"aws-{target.region}-eks-{name}.txt"
        parameter = EKS_IMAGE_ID.format(minor=minor, arch=arch, name=name)
        try:
            image_id = _ssm_value(target, read, fixture, parameter)
        except ImageLookupError as err:
            if not _not_in_region(err):
                raise
            refused.append(f"{release.tag}: not published in {target.region}")
            continue
        if not AMI_ID.match(image_id):
            raise ImageLookupError(f"{parameter} answered {image_id!r}, not an AMI id")
        return release, str(version), tuple(refused)
    raise NoEligibleImageError(
        f"no {EKS_AMI_REPO} release for {minor} {arch} clears the release gates", refused
    )


def recommended_ami_alias(target: Target, read: Reader) -> str:
    """Karpenter's alias for the newest RECOMMENDED AL2023 release, ungated.

    The alias version is the amazon-eks-ami release tag, which the recommended
    image's name carries (karpenter.sh/docs/concepts/nodeclasses). This is the
    newest release in the region, for scripts/check_karpenter_ami.py's lag
    report, never what a deployment runs.

    Returns:
        ``al2023@v<YYYYMMDD>``, never ``al2023@latest``.

    Raises:
        ImageLookupError: No parameter for this minor and arch, or one whose
            release version does not belong to the minor or disagrees with its
            image name.
    """
    minor = _minor(target)
    arch = _aws_arch(target)
    name = EKS_RECOMMENDED.format(minor=minor, arch=arch)
    fixture = f"aws-{target.region}-eks-recommended-{minor}-{arch}.json"
    doc = _json(_ssm_value(target, read, fixture, name), name)
    release = str(doc.get("release_version", ""))
    image_name = str(doc.get("image_name", ""))
    match = RELEASE_VERSION.match(release)
    if not match:
        raise ImageLookupError(
            f"{name} carries release_version {release!r}, not <patch>-<YYYYMMDD>"
        )
    if not match["patch"].startswith(f"{minor}."):
        raise ImageLookupError(f"{name} carries {release}, which is not a {minor} release")
    named = EKS_IMAGE_NAME_DATE.search(image_name)
    if not named or named["date"] != match["date"]:
        raise ImageLookupError(
            f"{name}'s image_name {image_name!r} does not carry release {match['date']}"
        )
    return f"al2023@v{match['date']}"


# --- the Amazon Linux 2023 image ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Al2023Image:
    """One AL2023 AMI DescribeImages listed."""

    image_id: str
    name: str
    release: str
    date: str
    family: str
    created: datetime | None


def _al2023_images(raw: str) -> list[Al2023Image]:
    """The AL2023 AMIs DescribeImages answered, skipping names outside AL2023's own scheme."""
    images: list[Al2023Image] = []
    for entry in _json_list(raw, "AL2023 DescribeImages"):
        if not isinstance(entry, dict):
            continue
        match = AL2023_NAME.match(str(entry.get("name", "")))
        if not match or not AMI_ID.match(str(entry.get("id", ""))):
            continue
        images.append(
            Al2023Image(
                image_id=str(entry["id"]),
                name=str(entry["name"]),
                release=match["release"],
                date=match["date"],
                family=f"{match['kernel']}-{match['arch']}",
                created=_timestamp(entry.get("created")),
            )
        )
    return images


def al2023_refusals(image: Al2023Image, policy: GatePolicy, now: datetime) -> list[str]:
    """Gates a, c and g for one AL2023 AMI, and nothing when it is eligible."""
    refusals: list[str] = []
    if (young := _age_refusal(image.created, now, policy.cooldown_days)) is not None:
        refusals.append(young)
    if image.release in policy.al2023_skip:
        refusals.append(f"skip-listed in versions.yaml: {policy.al2023_skip[image.release]}")
    if image.date < policy.al2023_floor:
        refusals.append(f"below the floor {policy.al2023_floor}")
    return refusals


def al2023_image(target: Target, read: Reader, policy: GatePolicy, now: datetime) -> Pick:
    """The newest AL2023 AMI in the default SSM image's family that clears gates a, c and g.

    Raises:
        NoEligibleImageError: No AMI in the family cleared every gate.
        ImageLookupError: SSM or EC2 gave no usable answer.
    """
    arch = _aws_arch(target)
    parameter = AL2023_AMI.format(arch=arch)
    default_id = _ssm_value(target, read, f"aws-{target.region}-al2023-ami-{arch}.txt", parameter)
    if not AMI_ID.match(default_id):
        raise ImageLookupError(f"{parameter} answered {default_id!r}, not an AMI id")
    args = [
        "ec2",
        "describe-images",
        "--region",
        target.region,
        "--owners",
        "amazon",
        "--filters",
        f"Name=name,Values=al2023-ami-2023.*-{arch}",
        "Name=state,Values=available",
        "--query",
        "Images[].{id:ImageId,name:Name,created:CreationDate}",
        "--output",
        "json",
    ]
    images = _al2023_images(read(f"aws-{target.region}-al2023-images-{arch}.json", "aws", args))
    default = next((image for image in images if image.image_id == default_id), None)
    if default is None:
        raise ImageLookupError(
            f"{parameter}'s {default_id} is not among the AL2023 images EC2 lists"
        )
    family = [image for image in images if image.family == default.family]
    family.sort(key=lambda image: (image.created is not None, image.created or now), reverse=True)
    refused: list[str] = []
    for image in family:
        reasons = al2023_refusals(image, policy, now)
        if not reasons:
            return Pick(image.image_id, image.name, tuple(refused))
        if image.date >= policy.al2023_floor:
            refused.append(f"{image.name}: {' | '.join(reasons)}")
    raise NoEligibleImageError(f"no AL2023 {default.family} AMI clears the release gates", refused)


# --- gcp and azure --------------------------------------------------------------


def gke_node_image(target: Target, read: Reader) -> dict[str, str]:
    """GKE's default node image type for the location.

    Reads ``ServerConfig.defaultImageType``
    (docs.cloud.google.com/kubernetes-engine/docs/reference/rest/v1/ServerConfig).

    Args:
        target: The deployment.
        read: The provider reader.

    Returns:
        ``{"family": <imageType>}``, e.g. ``{"family": "COS_CONTAINERD"}``.

    Raises:
        ImageLookupError: The provider gave no usable answer, or offers no node
            version on the minor.
    """
    args = [
        "container",
        "get-server-config",
        "--location",
        target.region,
        "--project",
        target.account,
        "--format",
        "json",
    ]
    fixture = f"gcp-{target.region}-gke-server-config.json"
    doc = _json(read(fixture, "gcloud", args), "GKE server config")
    default = str(doc.get("defaultImageType", ""))
    valid = doc.get("validImageTypes")
    if not default or not isinstance(valid, list) or default not in valid:
        raise ImageLookupError(
            f"GKE server config names defaultImageType {default!r} outside validImageTypes"
        )
    versions = doc.get("validNodeVersions")
    offered = isinstance(versions, list) and any(
        str(version).startswith(f"{target.kubernetes_version}.") for version in versions
    )
    if not offered:
        raise ImageLookupError(
            f"GKE offers no {target.kubernetes_version} node version in {target.region}"
        )
    return {"family": default}


def aks_node_image(target: Target, read: Reader) -> dict[str, str]:
    """AKS's version-tracking Linux OS SKU, once the location offers the minor.

    AKS publishes no API answer for a recommended OS SKU: its guidance is the
    unversioned ``Ubuntu`` SKU, which moves to the newest validated Ubuntu with
    each minor (learn.microsoft.com/en-us/azure/aks/upgrade-os-version). The
    lookup confirms the location lists the minor as generally available
    (``az aks get-versions``) before it writes that SKU.

    Args:
        target: The deployment.
        read: The provider reader.

    Returns:
        ``{"family": "Ubuntu"}``.

    Raises:
        ImageLookupError: The provider gave no usable answer, or lists the minor
            only in preview or not at all.
    """
    args = [
        "aks",
        "get-versions",
        "--location",
        target.region,
        "--subscription",
        target.account,
        "--output",
        "json",
    ]
    doc = _json(read(f"azure-{target.region}-aks-versions.json", "az", args), "AKS versions")
    values = doc.get("values")
    if not isinstance(values, list):
        raise ImageLookupError("AKS versions carry no values list")
    entry = next(
        (
            v
            for v in values
            if isinstance(v, dict) and v.get("version") == target.kubernetes_version
        ),
        None,
    )
    if entry is None:
        raise ImageLookupError(f"AKS lists no {target.kubernetes_version} in {target.region}")
    if entry.get("isPreview") is True:
        raise ImageLookupError(
            f"AKS lists {target.kubernetes_version} in {target.region} as preview only"
        )
    return {"family": AKS_LINUX_OS_SKU}


# --- the cascade ----------------------------------------------------------------


@dataclass(slots=True)
class Context:
    """One resolve's inputs, and the picks two keys share so each source is read once."""

    target: Target
    read: Reader
    policy: GatePolicy
    now: datetime
    cache: dict[str, Pick | ImageLookupError] = field(default_factory=dict)

    def once(self, name: str, pick: Callable[[], Pick]) -> Pick:
        """The shared pick for ``name``, raising the same error to every key that asks."""
        if name not in self.cache:
            try:
                self.cache[name] = pick()
            except ImageLookupError as err:
                self.cache[name] = err
        answer = self.cache[name]
        if isinstance(answer, ImageLookupError):
            raise answer
        return answer


def _eks_pick(ctx: Context) -> Pick:
    """The gated EKS release, as the node groups' image."""
    release, version, skipped = eks_release(ctx.target, ctx.read, ctx.policy, ctx.now)
    family = EKS_AMI_TYPES[_aws_arch(ctx.target)]
    return Pick({"family": family, "version": version}, release.tag, skipped)


def _aws_node_image(ctx: Context) -> Pick:
    return ctx.once("eks", lambda: _eks_pick(ctx))


def _aws_karpenter_alias(ctx: Context) -> Pick:
    eks = ctx.once("eks", lambda: _eks_pick(ctx))
    return Pick(f"al2023@{eks.release}", eks.release)


def _aws_al2023(ctx: Context) -> Pick:
    return ctx.once("al2023", lambda: al2023_image(ctx.target, ctx.read, ctx.policy, ctx.now))


def _gke(ctx: Context) -> Pick:
    return Pick(gke_node_image(ctx.target, ctx.read), "")


def _aks(ctx: Context) -> Pick:
    return Pick(aks_node_image(ctx.target, ctx.read), "")


type Lookup = Callable[[Context], Pick]

# Per cloud, each key a lookup answers. The forwarder's AMI is its own key, so a
# toolbox-only refresh never replaces the forwarder.
LOOKUPS: dict[str, tuple[tuple[str, Lookup], ...]] = {
    "aws": (
        ("node_image", _aws_node_image),
        ("karpenter_ami_alias", _aws_karpenter_alias),
        ("instance_ami_id", _aws_al2023),
        ("forwarder_ami_id", _aws_al2023),
    ),
    "gcp": (("node_image", _gke),),
    "azure": (("node_image", _aks),),
}

# Which gates a held value is checked against again: an EKS release tag or an AL2023 AMI name.
_EKS_KEYS = frozenset({"node_image", "karpenter_ami_alias"})
_AL2023_KEYS = frozenset({"instance_ami_id", "forwarder_ami_id"})


def tfvars_keys(cloud: str) -> frozenset[str]:
    """The keys a cloud's lookups write to tfvars, which its root declares."""
    return frozenset(key for key, _lookup in LOOKUPS.get(cloud, ()) if key not in CHART_KEYS)


def held_refusal(key: str, entry: Mapping[str, object], policy: GatePolicy) -> str | None:
    """Why a recorded value may no longer be held: skip-listed or below the floor."""
    release = str(entry.get("release", ""))
    if key in _EKS_KEYS:
        if release in policy.eks_skip:
            return f"{release} is skip-listed: {policy.eks_skip[release]}"
        if not EKS_TAG.match(release) or release < policy.eks_floor:
            return f"{release or 'an unnamed release'} is below the floor {policy.eks_floor}"
    if key in _AL2023_KEYS:
        match = AL2023_NAME.match(release)
        if match is None:
            return f"{release or 'an unnamed AMI'} is not an AL2023 image name"
        if match["release"] in policy.al2023_skip:
            return f"{release} is skip-listed: {policy.al2023_skip[match['release']]}"
        if match["date"] < policy.al2023_floor:
            return f"{release} is below the floor {policy.al2023_floor}"
    return None


def resolve(
    target: Target,
    *,
    pinned: bool,
    read: Reader,
    warn: Callable[[str], None],
    policy: GatePolicy,
    now: datetime,
    recorded: Mapping[str, Mapping[str, object]] | None = None,
    keys: frozenset[str] | None = None,
    note: Callable[[str], None] | None = None,
) -> Resolution:
    """Every node-image key this cloud's lookups answered or held.

    Args:
        target: The deployment.
        pinned: The dial's node_image.pin -- true skips every lookup.
        read: The provider reader.
        warn: Called once per key held or left unanswered.
        policy: The release gates.
        now: The moment the cooldown is measured from.
        recorded: The previous resolve's record, ``{key: {"value", "release"}}``.
        keys: Resolve only these keys, or every one the cloud has when None.
        note: Called once per newer candidate a gate refused, when given.

    Returns:
        The keys to write. A key whose lookup failed, or whose candidates all
        fail a gate, holds its recorded value. With nothing recorded, or a
        recorded value the gates now refuse, the key is left out, so tfvars
        applies or the plan refuses by name. A pin, or a cloud with no provider
        lookup, returns nothing.
    """
    empty = Resolution({}, {}, frozenset())
    if pinned or target.cloud not in LOOKUPS:
        return empty
    ctx = Context(target, read, policy, now)
    recorded = recorded or {}
    values: dict[str, object] = {}
    record: dict[str, dict[str, object]] = {}
    refused: set[str] = set()
    noted: set[str] = set()
    for key, lookup in LOOKUPS[target.cloud]:
        if keys is not None and key not in keys:
            continue
        try:
            pick = lookup(ctx)
        except ImageLookupError as err:
            refusals = err.refusals if isinstance(err, NoEligibleImageError) else []
            why = f"{err}: {' / '.join(refusals)}" if refusals else str(err)
            entry = recorded.get(key)
            if entry is None or "value" not in entry:
                warn(
                    f"{key} not resolved ({why}) and nothing was recorded to hold -- tfvars "
                    "applies, or the plan refuses by name"
                )
                continue
            refusal = held_refusal(key, entry, policy)
            if refusal is not None:
                refused.add(key)
                warn(f"{key} not resolved ({why}), and its recorded value is refused: {refusal}")
                continue
            values[key] = entry["value"]
            record[key] = {"value": entry["value"], "release": entry.get("release", "")}
            warn(f"{key} holds its recorded {entry.get('release', '')} ({why})")
            continue
        values[key] = pick.value
        record[key] = {"value": pick.value, "release": pick.release}
        for line in pick.skipped:
            if note is not None and line not in noted:
                noted.add(line)
                note(f"passed over {line}")
    return Resolution(values, record, frozenset(refused))


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follows a redirect, dropping the token when it leaves the host it was meant for."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        old_host = urllib.parse.urlsplit(req.full_url).netloc
        if new is not None and urllib.parse.urlsplit(newurl).netloc != old_host:
            new.remove_header("Authorization")
        return new


def _github_headers() -> dict[str, str]:
    """GitHub's REST headers, with a token from the environment only when one is set."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": GITHUB_USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_pages(url: str, max_pages: int, timeout: float) -> str:
    """Every item a GitHub REST list or search answers, following its next links.

    Returns:
        The items of every page read, as one JSON array.

    Raises:
        ImageLookupError: GitHub is unreachable, rate-limits the read (403 or
            429), answers another error, or answers something that is not a
            list or a complete search.
    """
    opener = urllib.request.build_opener(_SameHostRedirect())
    items: list[object] = []
    next_url: str | None = url
    for _page in range(max_pages):
        if next_url is None:
            break
        request = urllib.request.Request(next_url, headers=_github_headers())
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                link = response.headers.get("Link") or ""
        except urllib.error.HTTPError as err:
            if err.code in (403, 429):
                raise ImageLookupError(f"GitHub rate-limited {next_url} (HTTP {err.code})") from err
            raise ImageLookupError(f"GitHub answered HTTP {err.code} for {next_url}") from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise ImageLookupError(f"GitHub unreachable for {next_url}: {err}") from err
        page = _json_page(body, next_url)
        items.extend(page)
        match = LINK_NEXT.search(link)
        next_url = match[1] if match else None
    return json.dumps(items)


def _json_page(body: str, url: str) -> list[object]:
    """One page's items: a list as it stands, or a complete search's ``items``."""
    try:
        page = json.loads(body)
    except json.JSONDecodeError as err:
        raise ImageLookupError(f"GitHub answered no JSON for {url}: {err}") from err
    if isinstance(page, list):
        return page
    if isinstance(page, dict) and isinstance(page.get("items"), list):
        if page.get("incomplete_results") is True:
            raise ImageLookupError(f"GitHub's search timed out before it was complete for {url}")
        return page["items"]
    raise ImageLookupError(f"GitHub answered neither a list nor a search for {url}")


def live_reader(timeout: float = 60.0) -> Reader:
    """A reader that reads GitHub's REST API over HTTPS and runs the clouds' own CLIs."""

    def read(_fixture: str, tool: str, args: list[str]) -> str:
        if tool == HTTPS:
            return github_pages(args[0], int(args[1]), timeout)
        try:
            done = run_cli(tool, args, timeout=timeout)
        except FileNotFoundError as err:
            raise ImageLookupError(f"the {tool} CLI is not on PATH") from err
        except subprocess.TimeoutExpired as err:
            raise ImageLookupError(f"{tool} {' '.join(args[:2])} took over {timeout:g}s") from err
        if done.returncode != 0:
            tail = done.stderr.strip().splitlines()[-1:] or ["no stderr"]
            raise ImageLookupError(f"{tool} {' '.join(args[:2])} failed: {tail[0]}")
        return done.stdout

    return read


def fixture_reader(directory: Path) -> Reader:
    """A reader that answers from recorded responses, one file per read."""

    def read(fixture: str, _tool: str, _args: list[str]) -> str:
        path = directory / fixture
        if not path.is_file():
            raise ImageLookupError(f"no recorded response at {path}")
        return path.read_text(encoding="utf-8")

    return read


def _merged(
    path: Path, values: Mapping[str, object], keys: frozenset[str] | None
) -> dict[str, object]:
    """``values`` over the keys of ``path`` that ``keys`` does not name."""
    kept: dict[str, object] = {}
    if keys is not None and path.is_file():
        try:
            earlier = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            earlier = {}
        if isinstance(earlier, dict):
            kept = {key: value for key, value in earlier.items() if key not in keys}
    return {**kept, **values}


def write_tfvars(
    directory: Path, values: dict[str, object], *, keys: frozenset[str] | None = None
) -> Path:
    """Write the answered keys, replacing what an earlier resolve left for them.

    With ``keys`` None the whole file is replaced, so an empty answer writes
    ``{}`` and nothing a previous resolve wrote outlives a lookup that neither
    answered nor held a recorded value.
    ``resolve_sizing.py images`` writes a pin this way whatever ``--only``
    named, so nothing outlives a pin either. With ``keys`` named, only those
    are replaced -- dropped when unanswered -- and every other key in the file
    stays as it was.
    """
    path = directory / TFVARS_NAME
    body = json.dumps(_merged(path, values, keys), indent=2, sort_keys=True) + "\n"
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def read_record(path: Path) -> dict[str, dict[str, object]]:
    """A record file as ``{key: {"value", "release"}}``, empty when missing or unreadable."""
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(doc, dict):
        return {}
    return {str(k): v for k, v in doc.items() if isinstance(v, dict) and "value" in v}


def write_record(
    directory: Path,
    resolution: Resolution,
    previous: Mapping[str, Mapping[str, object]],
) -> Path:
    """Record what each key carries now: picked or held, refused dropped, failed kept.

    A key whose lookup failed keeps its previous entry, because the deployment
    still runs that image until a resolve answers it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / RECORD_NAME
    merged = {key: dict(entry) for key, entry in previous.items() if key not in resolution.refused}
    merged.update(resolution.record)
    body = json.dumps(merged, indent=2, sort_keys=True) + "\n"
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def write_node_pools_values(directory: Path, alias: object) -> Path | None:
    """Write Karpenter's alias as the node-pools overlay, or remove the overlay with no alias.

    Returns:
        The overlay written, or None when there is no alias and so no overlay.
    """
    path = directory / NODE_POOLS_VALUES_NAME
    if not isinstance(alias, str) or not alias:
        path.unlink(missing_ok=True)
        return None
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "## Written by scripts/resolve_sizing.py images -- do not edit by hand.",
        "## The EC2NodeClass image the release gates picked. infra/node-pools.yaml wins over it.",
        "pools:",
        "  aws:",
        "    nodeClass:",
        f"      amiAlias: {alias}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path
