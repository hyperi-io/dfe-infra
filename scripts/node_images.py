#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/node_images.py
#  Purpose:      Read each cloud's recommended node image, at its newest
#                version, for the tfvars the shape resolver writes.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The node-image cascade's first layer: what the provider recommends today.

A node image reaches tofu through three layers, highest first:

    1. this lookup, written to ``node-images.auto.tfvars.json`` by
       ``scripts/resolve_sizing.py images``
    2. a value the operator set in tfvars (``terraform.tfvars``)
    3. the variable default in the cloud root's ``variables.tf``

OpenTofu orders them itself: an ``*.auto.tfvars.json`` loads after
``terraform.tfvars``, so a key written here wins and a key left out lets the
layers below through. A lookup that fails writes no key and warns once. It never
fails the run, because a lower layer still holds an answer. ``node_image.pin:
"true"`` in the dial skips every lookup, so the operator's tfvars value holds --
a deliberate hold for a bad provider image or a reproducible rebuild.

Long-lived nodes take a DATED release, so their image moves only on the next
resolve and apply. An instance rebuilt on every use takes the newest AMI id.

    aws     the EKS managed node groups' AMI type and dated release, from the
            AL2023 ``recommended`` SSM parameter for the cluster's minor, and
            the toolbox and tunnel forwarder's AMI id, from the AL2023
            ``ami-amazon-linux-latest`` parameter
    gcp     GKE's server config ``defaultImageType`` for the location
    azure   AKS's version-tracking Linux OS SKU, once the location lists the
            minor as generally available

Karpenter's dated EC2NodeClass alias reads the same AWS parameter
(``karpenter_ami_alias``), and is not written to tfvars: its pin lives in
versions.yaml behind the release-age gate.

Stdlib only, like every other script here. Every provider read goes through one
``Reader``, so a test hands it recorded responses and the CLI hands it the live
``aws``/``gcloud``/``az`` binaries.
"""

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from aws_cli import run_cli

TFVARS_NAME = "node-images.auto.tfvars.json"

# (fixture name, CLI binary, argv) -> the CLI's stdout.
type Reader = Callable[[str, str, list[str]], str]

# compute-shapes.yaml spells x86 `amd64`, AWS's image paths spell it `x86_64`.
AWS_ARCH = {"arm64": "arm64", "amd64": "x86_64", "x86_64": "x86_64"}

# https://docs.aws.amazon.com/eks/latest/userguide/retrieve-ami-id.html
EKS_RECOMMENDED = "/aws/service/eks/optimized-ami/{minor}/amazon-linux-2023/{arch}/standard/recommended"

# The CreateNodegroup amiType for each AL2023 standard image the parameter above publishes.
# https://docs.aws.amazon.com/eks/latest/APIReference/API_CreateNodegroup.html
EKS_AMI_TYPES = {"arm64": "AL2023_ARM_64_STANDARD", "x86_64": "AL2023_x86_64_STANDARD"}

# https://docs.aws.amazon.com/linux/al2023/ug/ec2.html
AL2023_AMI = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{arch}"

# AKS's own best-practice SKU for Linux, which tracks the newest validated Ubuntu per minor.
# https://learn.microsoft.com/en-us/azure/aks/upgrade-os-version
AKS_LINUX_OS_SKU = "Ubuntu"

RELEASE_VERSION = re.compile(r"^(?P<patch>\d+\.\d+\.\d+)-(?P<date>\d{8})$")
EKS_IMAGE_NAME_DATE = re.compile(r"-v(?P<date>\d{8})$")
AMI_ID = re.compile(r"^ami-(?:[0-9a-f]{8}|[0-9a-f]{17})$")
KUBERNETES_MINOR = re.compile(r"^1\.\d+$")


class ImageLookupError(Exception):
    """A provider read that gave no usable answer."""


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


def _json(raw: str, what: str) -> dict[str, object]:
    """Parse one provider answer, naming it when it is not a JSON object."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ImageLookupError(f"{what} is not JSON: {err}") from err
    if not isinstance(doc, dict):
        raise ImageLookupError(f"{what} is not a JSON object")
    return doc


def _aws_arch(target: Target) -> str:
    """AWS's spelling of the target's architecture."""
    if target.arch not in AWS_ARCH:
        raise ImageLookupError(f"AWS publishes no image for arch {target.arch!r}")
    return AWS_ARCH[target.arch]


def _ssm_value(target: Target, read: Reader, fixture: str, name: str) -> str:
    """The value of one public SSM parameter in the target's region."""
    args = [
        "ssm", "get-parameter", "--region", target.region, "--name", name,
        "--query", "Parameter.Value", "--output", "text",
    ]
    return read(fixture, "aws", args).strip()


def _eks_recommended(target: Target, read: Reader) -> tuple[str, str, str]:
    """The AL2023 recommended image for the minor: (AMI type, release version, release date).

    Raises:
        ImageLookupError: No parameter for this minor and arch, or one whose release
            version does not belong to the minor or disagrees with its image name.
    """
    if not KUBERNETES_MINOR.match(target.kubernetes_version):
        raise ImageLookupError(f"{target.kubernetes_version!r} is not a Kubernetes minor such as 1.36")
    arch = _aws_arch(target)
    name = EKS_RECOMMENDED.format(minor=target.kubernetes_version, arch=arch)
    fixture = f"aws-{target.region}-eks-recommended-{target.kubernetes_version}-{arch}.json"
    doc = _json(_ssm_value(target, read, fixture, name), name)
    release = str(doc.get("release_version", ""))
    image_name = str(doc.get("image_name", ""))
    match = RELEASE_VERSION.match(release)
    if not match:
        raise ImageLookupError(f"{name} carries release_version {release!r}, not <patch>-<YYYYMMDD>")
    if not match["patch"].startswith(f"{target.kubernetes_version}."):
        raise ImageLookupError(
            f"{name} carries {release}, which is not a {target.kubernetes_version} release"
        )
    named = EKS_IMAGE_NAME_DATE.search(image_name)
    if not named or named["date"] != match["date"]:
        raise ImageLookupError(
            f"{name}'s image_name {image_name!r} does not carry release {match['date']}"
        )
    return EKS_AMI_TYPES[arch], release, match["date"]


def eks_node_image(target: Target, read: Reader) -> dict[str, str]:
    """The EKS managed node groups' image: AMI type and its newest dated release.

    Args:
        target: The deployment.
        read: The provider reader.

    Returns:
        ``{"family": <amiType>, "version": <releaseVersion>}``, e.g.
        ``{"family": "AL2023_ARM_64_STANDARD", "version": "1.36.4-20261012"}``.

    Raises:
        ImageLookupError: The provider gave no usable answer.
    """
    ami_type, release, _date = _eks_recommended(target, read)
    return {"family": ami_type, "version": release}


def karpenter_ami_alias(target: Target, read: Reader) -> str:
    """Karpenter's EC2NodeClass alias for the newest recommended AL2023 release.

    The alias version is the amazon-eks-ami release tag, which the recommended
    image's name carries (karpenter.sh/docs/concepts/nodeclasses).

    Args:
        target: The deployment.
        read: The provider reader.

    Returns:
        ``al2023@v<YYYYMMDD>``, never ``al2023@latest``.

    Raises:
        ImageLookupError: The provider gave no usable answer.
    """
    _ami_type, _release, date = _eks_recommended(target, read)
    return f"al2023@v{date}"


def instance_ami(target: Target, read: Reader) -> str:
    """The newest AL2023 AMI id in the region, for an instance rebuilt on every use.

    Args:
        target: The deployment.
        read: The provider reader.

    Returns:
        An AMI id, e.g. ``ami-0957e38de3db270cc``.

    Raises:
        ImageLookupError: The provider gave no usable answer.
    """
    arch = _aws_arch(target)
    name = AL2023_AMI.format(arch=arch)
    value = _ssm_value(target, read, f"aws-{target.region}-al2023-ami-{arch}.txt", name)
    if not AMI_ID.match(value):
        raise ImageLookupError(f"{name} answered {value!r}, not an AMI id")
    return value


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
        "container", "get-server-config", "--location", target.region,
        "--project", target.account, "--format", "json",
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
        "aks", "get-versions", "--location", target.region,
        "--subscription", target.account, "--output", "json",
    ]
    doc = _json(read(f"azure-{target.region}-aks-versions.json", "az", args), "AKS versions")
    values = doc.get("values")
    if not isinstance(values, list):
        raise ImageLookupError("AKS versions carry no values list")
    entry = next(
        (v for v in values if isinstance(v, dict) and v.get("version") == target.kubernetes_version),
        None,
    )
    if entry is None:
        raise ImageLookupError(f"AKS lists no {target.kubernetes_version} in {target.region}")
    if entry.get("isPreview") is True:
        raise ImageLookupError(
            f"AKS lists {target.kubernetes_version} in {target.region} as preview only"
        )
    return {"family": AKS_LINUX_OS_SKU}


type Lookup = Callable[[Target, Reader], object]

# Per cloud, the tfvars key each lookup writes -- the keys that cloud's root declares.
LOOKUPS: dict[str, tuple[tuple[str, Lookup], ...]] = {
    "aws": (("node_image", eks_node_image), ("instance_ami_id", instance_ami)),
    "gcp": (("node_image", gke_node_image),),
    "azure": (("node_image", aks_node_image),),
}


def resolve(
    target: Target, *, pinned: bool, read: Reader, warn: Callable[[str], None]
) -> dict[str, object]:
    """Every node-image key this cloud's lookups answered.

    Args:
        target: The deployment.
        pinned: The dial's node_image.pin -- true skips every lookup.
        read: The provider reader.
        warn: Called once per value no lookup answered.

    Returns:
        The tfvars keys to write. A failed lookup leaves its key out, so tfvars
        or the variable default applies. A pin, or a cloud with no provider
        lookup, returns nothing.
    """
    if pinned or target.cloud not in LOOKUPS:
        return {}
    values: dict[str, object] = {}
    for key, lookup in LOOKUPS[target.cloud]:
        try:
            values[key] = lookup(target, read)
        except ImageLookupError as err:
            warn(f"{key} not resolved ({err}) -- the tfvars value or the variable default applies")
    return values


def live_reader(timeout: float = 60.0) -> Reader:
    """A reader that runs the provider's own CLI."""

    def read(_fixture: str, tool: str, args: list[str]) -> str:
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


def write_tfvars(directory: Path, values: dict[str, object]) -> Path:
    """Write the answered keys, replacing whatever an earlier resolve left.

    An empty answer still writes ``{}``, so a key a previous resolve wrote
    cannot outlive a pin or a failed lookup.
    """
    path = directory / TFVARS_NAME
    body = json.dumps(values, indent=2, sort_keys=True) + "\n"
    path.write_text(body, encoding="utf-8", newline="\n")
    return path
