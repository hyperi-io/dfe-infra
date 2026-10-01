#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/toolbox_inputs.py
#  Purpose:      Build a dfe-toolbox image only when its published index does
#                not already carry the hash of its build inputs, so a push that
#                changes none of them leaves the tag and its digest pin alone.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Build-or-skip for the dfe-toolbox image family.

dfe-toolbox-base is pinned by digest, in versions.yaml digests.dfe-toolbox-base
and in the dfe-toolbox chart's image.digest. Rebuilding it on every push that
touches versions.yaml moves its tag, so the pin goes stale the moment it lands,
and the commit that updates the pin moves the tag again.

So every image records the hash of its build inputs as an OCI index annotation,
INPUTS_ANNOTATION, and is built only when the published tag does not carry that
hash. The inputs are every build arg, the platform list, and the path and content
of every file in the build context. The cloud images build FROM the base image by
tag, so their inputs also include the base image's own inputs hash.

The decision reads what is PUBLISHED, never a git diff: a build that failed
leaves the old annotation in place, so the next run builds again.

    plan    hash the inputs, read the published index, emit build=true|false
            plus the docker flags that carry the hash
    verify  read the pushed index back and fail unless it carries the hash

Usage:
    python3 scripts/toolbox_inputs.py plan --ref REF --context DIR --platform LIST
                                           [--build-arg NAME=VALUE ...] [--base-inputs HEX]
                                           [--github-output PATH]
    python3 scripts/toolbox_inputs.py verify --ref REF --inputs HEX [--pinned-by WHERE ...]

Requires `docker` with the buildx plugin on PATH. Stdlib only.
"""

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry_pins

INPUTS_ANNOTATION = "io.hyperi.dfe-toolbox.inputs"

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_BUILD_ARG = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")


class Reason(StrEnum):
    """Why an image is, or is not, built."""

    ABSENT = "absent"
    UNANNOTATED = "unannotated"
    CHANGED = "changed"
    MATCH = "match"


@dataclass(frozen=True, slots=True)
class Published:
    """What a published tag resolves to.

    Attributes:
        digest: The index digest the tag points at.
        inputs: The INPUTS_ANNOTATION value on that index, or None when it has none.
    """

    digest: str
    inputs: str | None


def build_arg(text: str) -> str:
    """Validate one NAME=VALUE build arg.

    The value may be empty but may not contain whitespace, because the flags
    travel to the build step as one space-separated workflow output.

    Raises:
        argparse.ArgumentTypeError: When the text is not NAME=VALUE.
    """
    if not _BUILD_ARG.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not NAME=VALUE with no whitespace")
    return text


def sha256_hex(text: str) -> str:
    """Validate a lowercase sha256 hex digest, the form an inputs hash takes.

    Raises:
        argparse.ArgumentTypeError: When the text is not 64 lowercase hex characters.
    """
    if not _SHA256_HEX.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not a sha256 hex digest")
    return text


def platforms(text: str) -> list[str]:
    """Split a docker --platform list, sorted and de-duplicated.

    Raises:
        argparse.ArgumentTypeError: When the list names no platform or holds whitespace.
    """
    found = sorted({item.strip() for item in text.split(",") if item.strip()})
    if not found or any(re.search(r"\s", item) for item in found):
        raise argparse.ArgumentTypeError(f"{text!r} is not a comma-separated platform list")
    return found


def duplicate_names(build_args: list[str]) -> list[str]:
    """Build-arg names given more than once, which docker would silently collapse."""
    names = [arg.split("=", 1)[0] for arg in build_args]
    return sorted({name for name in names if names.count(name) > 1})


def context_files(context: Path) -> list[tuple[str, str]]:
    """(path relative to the context, sha256 of its content) for every file under it.

    Paths are POSIX and relative to the context, so the hash follows what docker
    is sent rather than where the checkout sits.
    """
    files = [
        (path.relative_to(context).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in context.rglob("*")
        if path.is_file()
    ]
    return sorted(files)


def inputs_hash(
    build_args: list[str],
    platform_list: list[str],
    files: list[tuple[str, str]],
    base_inputs: str = "",
) -> str:
    """The sha256 of an image's build inputs, independent of the order they arrive in."""
    document = {
        "build_args": sorted(build_args),
        "platforms": sorted(platform_list),
        "files": [list(entry) for entry in sorted(files)],
        "base_inputs": base_inputs,
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def index_inputs(raw: str) -> str | None:
    """The INPUTS_ANNOTATION an image index carries at its top level, or None.

    Only the index's own annotations count: buildx writes per-manifest
    annotations for attestations, and none of those is the inputs hash.

    Raises:
        json.JSONDecodeError: When the raw manifest is not JSON.
    """
    document = json.loads(raw)
    annotations = document.get("annotations") if isinstance(document, dict) else None
    if not isinstance(annotations, dict):
        return None
    value = annotations.get(INPUTS_ANNOTATION)
    return value if isinstance(value, str) else None


def decide(published: Published | None, inputs: str) -> Reason:
    """Whether to build, from what the tag carries now. Only MATCH skips the build."""
    if published is None:
        return Reason.ABSENT
    if published.inputs is None:
        return Reason.UNANNOTATED
    if published.inputs != inputs:
        return Reason.CHANGED
    return Reason.MATCH


def build_flags(build_args: list[str], platform_list: list[str], inputs: str) -> list[str]:
    """The docker buildx flags that build exactly the inputs that were hashed."""
    flags = [f"--platform={','.join(platform_list)}"]
    flags += [f"--build-arg={arg}" for arg in build_args]
    flags.append(f"--annotation=index:{INPUTS_ANNOTATION}={inputs}")
    return flags


def plan_line(ref: str, reason: Reason, published: Published | None) -> str:
    """The log line for a decision. A skip is a ::notice:: so the run summary shows it."""
    if reason is Reason.MATCH and published is not None:
        return (
            f"::notice::{ref} already matches these inputs at {published.digest} -- "
            f"not rebuilt, so the tag and its digest stay put"
        )
    if reason is Reason.ABSENT or published is None:
        return f"{ref} is not published -- building it"
    if reason is Reason.UNANNOTATED:
        return f"{ref} ({published.digest}) records no inputs hash -- rebuilding moves the tag"
    return (
        f"{ref} ({published.digest}) was built from inputs {published.inputs} -- "
        f"rebuilding moves the tag"
    )


def pushed_error(ref: str, published: Published | None, inputs: str) -> str | None:
    """Why a just-pushed tag does not carry the expected inputs hash, or None."""
    if published is None:
        return f"{ref} is not published after the push"
    if published.inputs != inputs:
        return (
            f"{ref} ({published.digest}) carries inputs {published.inputs or '(none)'}, "
            f"expected {inputs} -- the index annotation did not reach the registry"
        )
    return None


def read_published(ref: str) -> Published | None:
    """The published index's digest and inputs hash, or None when the tag is absent.

    Raises:
        registry_pins.RegistryError: When the registry could not be read, so an
            outage never reads as an absent tag and triggers a build.
    """
    raw, err = registry_pins._imagetools(ref, "--raw")
    if raw is None:
        if registry_pins._ABSENT.search(err):
            return None
        raise registry_pins.RegistryError(f"{ref}: {err}")
    try:
        inputs = index_inputs(raw)
    except json.JSONDecodeError as exc:
        raise registry_pins.RegistryError(f"{ref}: unreadable index: {exc}") from exc
    digest, err = registry_pins.ref_digest(ref)
    if digest is None:
        raise registry_pins.RegistryError(f"{ref}: {err}")
    return Published(digest=digest, inputs=inputs)


def write_outputs(outputs: dict[str, str], github_output: str | None) -> None:
    """Append key=value lines to the GITHUB_OUTPUT file, or print them without one."""
    text = "".join(f"{key}={value}\n" for key, value in outputs.items())
    if github_output:
        with Path(github_output).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    else:
        print(text, end="")


def cmd_plan(args: argparse.Namespace) -> int:
    """Hash the inputs, read the published tag and emit the decision."""
    context = Path(args.context)
    if not context.is_dir():
        print(f"::error::build context {context} is not a directory")
        return 1
    files = context_files(context)
    if not files:
        print(f"::error::build context {context} holds no files")
        return 1
    inputs = inputs_hash(args.build_arg, args.platform, files, args.base_inputs or "")
    try:
        published = read_published(args.ref)
    except registry_pins.RegistryError as exc:
        print(f"::error::{exc}")
        return 1
    reason = decide(published, inputs)
    print(f"{args.ref}: inputs {inputs} over {len(files)} file(s), decision {reason}")
    print(plan_line(args.ref, reason, published))
    flags = build_flags(args.build_arg, args.platform, inputs)
    build = "false" if reason is Reason.MATCH else "true"
    write_outputs({"inputs": inputs, "build": build, "flags": " ".join(flags)}, args.github_output)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Read the pushed index back and fail unless it carries the inputs hash."""
    try:
        published = read_published(args.ref)
    except registry_pins.RegistryError as exc:
        print(f"::error::{exc}")
        return 1
    error = pushed_error(args.ref, published, args.inputs)
    if error or published is None:
        print(f"::error::{error}")
        return 1
    print(f"{args.ref}: index {published.digest} carries {INPUTS_ANNOTATION}={published.inputs}")
    if args.pinned_by:
        print(
            f"::warning::{args.ref} moved to {published.digest} -- set "
            f"{' and '.join(args.pinned_by)} to that digest"
        )
    else:
        print(f"::notice::{args.ref} moved to {published.digest}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="decide whether to build, and emit the build flags")
    plan.add_argument("--ref", required=True, help="the published tag, registry/name:tag")
    plan.add_argument("--context", required=True, help="the docker build context directory")
    plan.add_argument(
        "--platform", required=True, type=platforms, help="comma-separated os/arch list"
    )
    plan.add_argument(
        "--build-arg",
        action="append",
        default=[],
        type=build_arg,
        help="NAME=VALUE, exactly as the build receives it (repeatable)",
    )
    plan.add_argument(
        "--base-inputs",
        type=sha256_hex,
        help="the inputs hash of the image this one builds FROM",
    )
    plan.add_argument(
        "--github-output", help="append the outputs to this file (the step's $GITHUB_OUTPUT)"
    )
    plan.set_defaults(func=cmd_plan)

    verify = sub.add_parser("verify", help="check the pushed index carries the inputs hash")
    verify.add_argument("--ref", required=True, help="the tag just pushed, registry/name:tag")
    verify.add_argument("--inputs", required=True, type=sha256_hex, help="the hash plan emitted")
    verify.add_argument(
        "--pinned-by",
        action="append",
        default=[],
        help="where this image's digest is pinned; a move is then a ::warning:: (repeatable)",
    )
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "plan":
        repeated = duplicate_names(args.build_arg)
        if repeated:
            parser.error(f"build arg(s) given more than once: {', '.join(repeated)}")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
