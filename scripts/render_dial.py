#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/render_dial.py
#  Purpose:      Render the deployment dial (k8s slice) into the DFE_* env file
#                dfe-ops consumes, and print the derived `dfe-ops cycle` line.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Render the deployment dial into the DFE_* env file, for the k8s substrate.

The deployment dial (``deployment.yaml``) is the single SSoT a deployment turns:
one file the whole automation reads, authored by hand today and populated by the
QA GUI wizard later. This is the k8s SIBLING of dfe-docker's ``render_dial.py``.
It renders the ``k8s:`` slice of the CANONICAL SUPERSET dial (whose schema +
example live in THIS repo, ``deployment.example.yaml``) into the flat DFE_* env
file ``dfe-ops`` already consumes (``bootstrap/.env``), then prints the derived
``dfe-ops cycle`` invocation with --mode / --stack / --registry taken from the
dial. So a redeploy is a dial edit plus one printed command -- dfe-ops itself is
the orchestrator, unchanged; the dial just feeds it.

    dial  ->  DFE_* env  ->  dfe-ops cycle  ->  running stack + E2E + teardown

The env file is SEEDED from ``bootstrap/local.env.example`` on first render (so
every DFE_* key + its comments are present), then the dial's non-empty values
are merged in place over it. Estate ENDPOINTS and SECRETS stay blank in the
committed template -- hyperi-infra's thin caller injects them from OpenBao at
deploy time, or an operator fills the copied ``.env`` by hand. This renderer
never reads or writes a secret.

Dependency-free (no PyYAML) and stdlib only, matching the dfe-ops rule -- the
dial's k8s slice is scalar / nested-map only.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAL = REPO_ROOT / "deployment.yaml"
DIAL_TEMPLATE = REPO_ROOT / "deployment.example.yaml"
ENV_FILE = REPO_ROOT / "bootstrap" / ".env"
ENV_TEMPLATE = REPO_ROOT / "bootstrap" / "local.env.example"

# (dial path) -> DFE_* env key. These are the flat env-file keys dfe-ops reads
# (bootstrap/local.env.example is the SSoT for the vocabulary). The three deploy
# levers the dial also carries -- profile, version.pin, registry -- are NOT here:
# dfe-ops takes them as --mode / --stack / --registry flags (cycle forces
# DFE_PROFILE from --mode), so they are printed as the derived command instead.
_ENV_MAP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("target", "existing", "namespace"), "DFE_NAMESPACE"),
    (("target", "existing", "clusterRef"), "DFE_KUBE_CONTEXT"),
    (("k8s", "env"), "DFE_ENV"),
    (("k8s", "cloud"), "DFE_CLOUD"),
    (("k8s", "region"), "DFE_REGION"),
    (("k8s", "domain"), "DFE_DOMAIN"),
    (("k8s", "storage_class"), "DFE_STORAGE_CLASS"),
    (("k8s", "repo_url"), "DFE_REPO_URL"),
    (("k8s", "target_revision"), "DFE_TARGET_REVISION"),
    (("k8s", "workload_identity_annotations"), "DFE_WORKLOAD_IDENTITY_ANNOTATIONS"),
    (("endpoints", "clickhouse_host"), "DFE_CLICKHOUSE_HOST"),
    (("endpoints", "kafka_bootstrap"), "DFE_KAFKA_BOOTSTRAP"),
    (("endpoints", "otel_endpoint"), "DFE_OTEL_ENDPOINT"),
    (("endpoints", "vault_addr"), "DFE_VAULT_ADDR"),
    (("retention", "default_ttl_days"), "DFE_CLICKHOUSE_DEFAULT_TTL_DAYS"),
)

# An env assignment, live (`KEY=`) or hash-commented (`# KEY=`). The env file's
# keys are DFE_*, KUBECONFIG, READINESS_TIMEOUT -- all [A-Z][A-Z0-9_]*.
_SETTING_RE = re.compile(r"^[ \t]*(?:#[ \t]?)?(?P<key>[A-Z][A-Z0-9_]*)[ \t]*=")


def _parse_yaml_subset(text: str) -> dict[str, object]:
    """Parse a minimal YAML subset -- nested maps, scalar values -- into dicts.

    Dependency-free (no PyYAML). Handles ``key: value`` scalars and ``key:``
    nesting by indentation, skipping ``#`` comments and blank lines. It does NOT
    handle lists or inline collections -- a line it cannot place raises
    ValueError -- which is why the dial keeps every list (nodePools) in prose.
    """
    root: dict[str, object] = {}
    stack: list[tuple[dict[str, object], int]] = [(root, -1)]
    for line_num, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        while len(stack) > 1 and stack[-1][1] >= indent:
            stack.pop()
        parent = stack[-1][0]
        if ": " in line:
            key, value = line.split(": ", 1)
            value = value.strip()
            if value and value[0] in ("'", '"'):
                # Quoted: take only what is INSIDE the quotes and drop any
                # trailing inline comment, so `key: "" # note` is empty (the
                # dial annotates blank estate fields exactly this way).
                quote = value[0]
                end = value.find(quote, 1)
                value = value[1:end] if end != -1 else value[1:]
            else:
                # Unquoted: strip a trailing ` # ...` inline comment.
                value = value.split(" #", 1)[0].strip()
            parent[key.strip()] = value
        elif line.endswith(":"):
            child: dict[str, object] = {}
            parent[line[:-1].strip()] = child
            stack.append((child, indent))
        else:
            raise ValueError(f"line {line_num}: cannot parse {line!r}")
    return root


def _scalar(dial: dict[str, object], path: tuple[str, ...]) -> str | None:
    """Return the non-empty scalar at ``path`` in the parsed dial, else None."""
    node: object = dial
    for step in path:
        if not isinstance(node, dict):
            return None
        node = node.get(step)
    return node.strip() if isinstance(node, str) and node.strip() else None


def _env_updates(dial: dict[str, object]) -> dict[str, str]:
    """Map the dial's k8s fields to the DFE_* keys they set (empty skipped)."""
    updates: dict[str, str] = {}
    for path, env_key in _ENV_MAP:
        value = _scalar(dial, path)
        if value is not None:
            updates[env_key] = value
    # DFE_REGISTRY_HOST is the pull-secret host -- the host part of the registry
    # (the full registry+path is the --registry flag on the derived command).
    registry = _scalar(dial, ("registry",))
    if registry:
        updates["DFE_REGISTRY_HOST"] = registry.split("/", 1)[0]
    return updates


def _merge_env(env_path: Path, updates: dict[str, str]) -> None:
    """Overwrite each mapped key in the env file with the dial value.

    Every OTHER line -- the example's blanks, comments, untouched settings --
    survives verbatim, so the estate secrets/endpoints stay present-but-blank for
    the operator (or the thin caller) to fill. A mapped key is replaced in place;
    a genuinely new key is appended under a labelled header.
    """
    remaining = dict(updates)
    out: list[str] = []
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _SETTING_RE.match(line)
        key = match.group("key") if match else None
        if key in updates:
            if key in remaining:
                out.append(f'{key}="{remaining.pop(key)}"')
            continue
        out.append(line)

    if remaining:
        out.append("")
        out.append("## Set by render_dial.py from deployment.yaml -- do not edit by hand.")
        out.extend(f'{key}="{value}"' for key, value in remaining.items())

    with env_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(out) + "\n")


def _derived_command(dial: dict[str, object], env_path: Path) -> str:
    """Build the `dfe-ops cycle` invocation the dial implies (mode/stack/registry)."""
    mode = _scalar(dial, ("profile",)) or "single"
    stack = _scalar(dial, ("version", "pin")) or ""
    registry = _scalar(dial, ("registry",)) or ""
    rel_env = env_path.relative_to(REPO_ROOT) if env_path.is_relative_to(REPO_ROOT) else env_path
    parts = ["python3 scripts/dfe-ops cycle", f"--mode {mode}"]
    if stack:
        parts.append(f"--stack {stack}")
    if registry:
        parts.append(f"--registry {registry}")
    parts.append(f"--env-file {rel_env}")
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="render_dial.py",
        description="Render the deployment dial's k8s slice into the DFE_* env file.",
    )
    ap.add_argument("--dial", type=Path, default=DIAL, help="dial path (default: deployment.yaml)")
    ap.add_argument(
        "--out",
        type=Path,
        default=ENV_FILE,
        help="env file to write (default: bootstrap/.env)",
    )
    args = ap.parse_args()

    if not args.dial.is_file():
        print(
            f"render_dial: no deployment dial at {args.dial} -- copy "
            f"{DIAL_TEMPLATE.name} to deployment.yaml and populate it",
            file=sys.stderr,
        )
        return 1

    dial = _parse_yaml_subset(args.dial.read_text(encoding="utf-8", errors="replace"))
    substrate = _scalar(dial, ("substrate",))
    if substrate != "k8s":
        print(
            f"render_dial: this renderer handles substrate 'k8s', the dial says "
            f"{substrate!r} -- the docker-vm substrate renders via dfe-docker",
            file=sys.stderr,
        )
        return 1

    # Seed the env file from the committed example on first render, so every
    # DFE_* key + its guidance is present before the dial merges over it.
    if not args.out.is_file():
        if not ENV_TEMPLATE.is_file():
            print(
                f"render_dial: no {ENV_TEMPLATE} to seed the env file from",
                file=sys.stderr,
            )
            return 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ENV_TEMPLATE, args.out)
        print(f"render_dial: seeded {args.out} from {ENV_TEMPLATE.name}", file=sys.stderr)

    updates = _env_updates(dial)
    if updates:
        _merge_env(args.out, updates)
        print(
            f"render_dial: merged {len(updates)} dial key(s) into {args.out}: "
            + ", ".join(sorted(updates)),
            file=sys.stderr,
        )
    else:
        print("render_dial: dial set no k8s keys -- env file unchanged", file=sys.stderr)

    print(file=sys.stderr)
    print("Deploy this dial with:", file=sys.stderr)
    print(f"  {_derived_command(dial, args.out)}", file=sys.stderr)
    print(
        "  # add --kubeconfig <path> if the target is not your current context",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
