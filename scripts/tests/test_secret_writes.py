#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_secret_writes.py
#  Purpose:      Prove every Secret the bootstrap and ops scripts write goes
#                through server-side apply under a named field manager.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every Secret a script writes is applied server-side, never client-side.

A client-side `kubectl apply` keeps the whole submitted object, `.data` included,
in the kubectl.kubernetes.io/last-applied-configuration annotation, so any read of
the Secret's metadata prints the credential. Server-side apply records field
ownership in managedFields and keeps no copy.

The field manager matters as much as the flag. Under kubectl's default manager an
existing annotation is kept up to date, so a Secret an earlier run wrote
client-side keeps its copy. A named manager takes the annotation over from the
client-side manager, and the apply then drops it.

    python3 -m pytest scripts/tests/test_secret_writes.py -q
    python3 scripts/tests/test_secret_writes.py
"""

import ast
import re
import sys
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
TEMPLATES = REPO_ROOT / "bootstrap" / "templates"
PYTHON_ROOTS = (REPO_ROOT / "scripts", REPO_ROOT / "bootstrap")

HELPER = "dfe_apply_secret"
HELPER_COMMAND = "kubectl apply --server-side --field-manager=dfe-bootstrap --force-conflicts -f -"
# What bootstrap.sh renders a template through, so a command naming it writes what the template holds.
RENDERER = "render_env.py"

# Every Secret bootstrap.sh writes, by the name its command gives it, so a check
# that silently matched nothing fails here rather than passing.
BOOTSTRAP_SECRETS = {
    "dfe-cluster",
    "cert-manager-approle",
    "dfe-vault-approle-secret",
    '"${DFE_IMAGE_PULL_SECRET}"',
    "dfe-forgejo-admin",
    "dfe-deploy-repo-auth",
    "dfe-argo-webhook",
    "repo-deploy",
    "repo-charts",
    '"repo-chart-${chart}"',
    "repo-envoyproxy-oci",
}

# The Python programs that render a Secret manifest and hand it to kubectl.
PYTHON_SECRET_WRITERS = {"scripts/dfe-ops", "scripts/tester_idp.py"}

_PIPE = re.compile(r"(?<!\|)\|(?!\|)")
_CREATED = re.compile(r"\bcreate secret [\w-]+\s+(\S+)")
_TEMPLATE_REF = re.compile(r"\$\{TEMPLATES_DIR\}/([\w.-]+)")
_TEMPLATE_KIND = re.compile(r"^kind:\s*Secret\s*$", re.M)
_TEMPLATE_NAME = re.compile(r"^metadata:\n\s+name:\s*(\S+)", re.M)
_SECRET_MANIFEST = re.compile(r"""["']kind["']\s*:\s*["']Secret["']""")


def logical_commands(script: str) -> list[tuple[int, str]]:
    """Each command with its backslash continuations joined, by its first line."""
    commands = []
    pending, start = "", 0
    for number, line in enumerate(script.splitlines(), start=1):
        if not pending:
            start = number
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        commands.append((start, pending + line))
        pending = ""
    return commands


def secret_templates(directory: Path) -> dict[str, str]:
    """Template file name -> the name of the Secret it renders."""
    found = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if _TEMPLATE_KIND.search(text):
            name = _TEMPLATE_NAME.search(text)
            found[path.name] = name.group(1) if name else path.name
    return found


def written_secret(command: str, templates: dict[str, str]) -> str | None:
    """The name of the Secret a command writes, or None for any other command."""
    stripped = command.strip()
    if stripped.startswith(("#", "echo ")):
        return None
    created = _CREATED.search(stripped)
    if created:
        return created.group(1)
    if RENDERER in stripped:
        for ref in _TEMPLATE_REF.findall(stripped):
            if ref in templates:
                return templates[ref]
    return None


def secret_writes(script: str, templates: dict[str, str]) -> list[tuple[int, str, str]]:
    """(line, Secret name, the pipeline's last stage) for every Secret write."""
    writes = []
    for line, command in logical_commands(script):
        name = written_secret(command, templates)
        if name is not None:
            writes.append((line, name, _PIPE.split(command)[-1].strip()))
    return writes


def helper_body(script: str) -> str:
    match = re.search(rf"^{HELPER}\(\) \{{\n(.*?)^\}}", script, re.M | re.S)
    return match.group(1) if match else ""


def client_side_applies(source: str) -> list[int]:
    """Line of each kubectl apply argv that lacks --server-side or a named manager."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.List, ast.Tuple)):
            items = node.elts
        elif isinstance(node, ast.Call):
            items = node.args
        else:
            continue
        words = {i.value for i in items if isinstance(i, ast.Constant) and isinstance(i.value, str)}
        if "apply" not in words or "-f" not in words:
            continue
        managers = {w for w in words if w.startswith("--field-manager=")}
        named = bool(managers) and "--field-manager=kubectl" not in managers
        if "--server-side" not in words or not named:
            lines.append(node.lineno)
    return lines


def python_programs() -> list[Path]:
    """Every Python program under scripts/ and bootstrap/, tests aside."""
    found = []
    for root in PYTHON_ROOTS:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "tests" in path.relative_to(REPO_ROOT).parts:
                continue
            if path.suffix == ".py":
                found.append(path)
            elif not path.suffix:
                with path.open("rb") as handle:
                    first = handle.readline()
                if first.startswith(b"#!") and b"python" in first:
                    found.append(path)
    return found


def test_the_bootstrap_helper_applies_server_side_under_a_named_manager() -> None:
    body = helper_body(BOOTSTRAP.read_text(encoding="utf-8"))
    expect(f"bootstrap.sh defines {HELPER}", body != "", "no helper function found")
    expect(
        f"{HELPER} runs {HELPER_COMMAND!r}",
        HELPER_COMMAND in body,
        f"the helper body is {body.strip()!r}",
    )


def test_every_bootstrap_secret_write_goes_through_the_helper() -> None:
    writes = secret_writes(BOOTSTRAP.read_text(encoding="utf-8"), secret_templates(TEMPLATES))
    found = {name for _, name, _ in writes}
    expect(
        "the check sees every Secret bootstrap.sh writes",
        BOOTSTRAP_SECRETS <= found,
        f"missing {sorted(BOOTSTRAP_SECRETS - found)}",
    )
    for line, name, stage in writes:
        expect(
            f"bootstrap.sh:{line} writes {name} through {HELPER}",
            re.match(rf"{HELPER}\b", stage) is not None,
            f"its last stage is {stage!r}, which keeps the data in an annotation",
        )


def test_the_bootstrap_check_catches_a_client_side_write() -> None:
    script = (
        "kubectl -n ns create secret generic leak \\\n"
        "  --from-literal=k=v \\\n"
        "  --dry-run=client -o yaml | kubectl apply -f -\n"
        'python3 "${SCRIPT_DIR}/render_env.py" < "${TEMPLATES_DIR}/cluster-secret.yaml.tpl" | kubectl apply -f -\n'
        'echo "kubectl create secret generic shown -o yaml | kubectl apply -f -"\n'
    )
    writes = secret_writes(script, {"cluster-secret.yaml.tpl": "dfe-cluster"})
    expect(
        "both client-side writes are found, the echo is not",
        [(n, s) for _, n, s in writes]
        == [("leak", "kubectl apply -f -"), ("dfe-cluster", "kubectl apply -f -")],
        f"found {writes}",
    )


def test_every_python_secret_writer_applies_server_side() -> None:
    writers = []
    for path in python_programs():
        source = path.read_text(encoding="utf-8")
        if not _SECRET_MANIFEST.search(source):
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        writers.append(relative)
        for line in client_side_applies(source):
            expect(
                f"{relative}:{line} applies server-side under a named manager",
                False,
                "a client-side apply keeps a Secret's data in an annotation",
            )
    expect(
        "the check sees every Python Secret writer",
        PYTHON_SECRET_WRITERS <= set(writers),
        f"missing {sorted(PYTHON_SECRET_WRITERS - set(writers))}",
    )


def test_the_python_check_catches_a_client_side_apply() -> None:
    cases = (
        ('run(["kubectl", "apply", "-f", "-"])', [1]),
        ('run([*kube, "apply", "--server-side", "-f", "-"])', [1]),
        ('run([*kube, "apply", "--server-side", "--field-manager=kubectl", "-f", "-"])', [1]),
        ('run([*kube, "apply", "--server-side", "--field-manager=dfe-ops", "-f", "-"])', []),
        ('run(["tofu", "apply", "-auto-approve"])', []),
    )
    for source, want in cases:
        got = client_side_applies(source)
        expect(f"{source} -> {want}", got == want, f"got {got}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
