#  Project:      dfe-infra
#  File:         scripts/stack_images.py
#  Purpose:      The third-party image map one versions.yaml stack declares:
#                each annotated pin's image, tag, digest and registry aliases.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The image map versions.yaml carries in its own annotations.

A `services:` (or `bootstrap:`) pin names its image in the comment above it,
either as Renovate reads it or as a plain `# image:` line::

    # renovate: datasource=docker depName=otel/opentelemetry-collector-contrib
    otel-collector: "0.158.0"
    # image: docker.redpanda.com/redpandadata/redpanda aliases=redpandadata/redpanda
    redpanda-version: "v26.2.2"

The annotation doubles as the image map, so there is no second table to drift.
Two optional tokens on the annotation line carry the rest as data:

    aliases=<ref>[,<ref>]   other references to the SAME image (another
                            registry's name for it), which a member may pin.
    rejects=<ref>[,<ref>]   references that look like the image and are not
                            it; a member pinning one is pinning the wrong image.

The digest half of each pin is the stack's `services-digests:` entry under the
same key. dfe-stack's refresh-digests and scripts/check_member_pins.py both read
the map from here.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import yaml_subset  # noqa: E402

# `# renovate: ... depName=<ref>` or `# image: <ref>` above a `key: "value"` line.
_ANNOTATION = re.compile(
    r"^[^\S\n]*#[^\S\n]*(?:renovate:[^\n]*?depName=(?P<a>[^\s]+)"
    r"|image:[^\S\n]*(?P<b>[^\s]+))[^\n]*\n"
    r"(?:^[^\S\n]*#[^\n]*\n)*"  # further comment lines between annotation and key
    r"^[^\S\n]*(?P<key>[A-Za-z0-9_.-]+):[^\S\n]*\"(?P<value>[^\"]+)\"",
    re.MULTILINE,
)


def _refs(line: str, token: str) -> tuple[str, ...]:
    match = re.search(rf"(?:^|\s){token}=([^\s]+)", line)
    return tuple(ref for ref in match[1].split(",") if ref) if match else ()


def annotated_images(text: str) -> dict[str, tuple[str, str]]:
    """services key -> (image ref, tag) for every annotated pin."""
    found: dict[str, tuple[str, str]] = {}
    for m in _ANNOTATION.finditer(text):
        found[m.group("key")] = (m.group("a") or m.group("b"), m.group("value"))
    return found


def annotation_refs(text: str) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """services key -> (aliases, rejects) read off each annotation line."""
    found: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for m in _ANNOTATION.finditer(text):
        line = m.group(0).splitlines()[0]
        found[m.group("key")] = (_refs(line, "aliases"), _refs(line, "rejects"))
    return found


def block_span(lines: list[str], stack: str) -> tuple[int, int]:
    """Line span [start, end) of a stack block inside `stacks:`.

    The block runs from its `  <version>:` key to the next key at that indent, or
    to end of file for the last one.
    """
    start = next(
        (i for i, ln in enumerate(lines) if re.match(rf"^  {re.escape(stack)}:\s*$", ln)),
        None,
    )
    if start is None:
        raise SystemExit(f"error: no `{stack}:` block in versions.yaml")
    for i in range(start + 1, len(lines)):
        if re.match(r"^  \S", lines[i]):
            return start, i
    return start, len(lines)


def normalise_ref(ref: str) -> str:
    """An image reference as Docker resolves it: Docker Hub's implicit
    `docker.io/` and `library/` prefixes dropped, so the two spellings compare equal."""
    ref = ref.strip().lower()
    for prefix in ("docker.io/", "index.docker.io/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix) :]
    return ref.removeprefix("library/")


@dataclass(frozen=True, slots=True)
class StackImage:
    """One digested, annotated pin of a stack.

    Attributes:
        key: The versions.yaml key the tag lives under (`services.<key>` or
            `bootstrap.<key>`) and the digest under (`services-digests.<key>`).
        ref: The image the annotation names.
        tag: The pinned tag.
        digest: The pinned `sha256:` digest.
        aliases: Other references to the same image.
        rejects: References that are not this image.
    """

    key: str
    ref: str
    tag: str
    digest: str
    aliases: tuple[str, ...] = ()
    rejects: tuple[str, ...] = ()

    @property
    def refs(self) -> tuple[str, ...]:
        """Every reference that IS this image, normalised."""
        return tuple(normalise_ref(r) for r in (self.ref, *self.aliases))


def stack_images(versions_text: str, stack: str | None = None) -> tuple[str, list[StackImage]]:
    """(stack name, every annotated pin of it that also carries a digest).

    `stack` defaults to the `current` pointer. A pin with no services-digests
    entry is left out: with no digest there is nothing immutable to compare.
    """
    root = yaml_subset.parse(versions_text, source="versions.yaml")
    name = stack or str(root.get("current") or "")
    stacks = root.get("stacks")
    if not name or not isinstance(stacks, dict) or name not in stacks:
        raise SystemExit(f"error: no stack {name!r} in versions.yaml")
    digests = stacks[name].get("services-digests") or {}
    lines = versions_text.splitlines(keepends=True)
    start, end = block_span(lines, name)
    block = "".join(lines[start:end])
    refs = annotation_refs(block)
    images = []
    for key, (ref, tag) in sorted(annotated_images(block).items()):
        digest = digests.get(key) if isinstance(digests, dict) else None
        if digest:
            aliases, rejects = refs.get(key, ((), ()))
            images.append(StackImage(key, ref, tag, str(digest), aliases, rejects))
    return name, images
