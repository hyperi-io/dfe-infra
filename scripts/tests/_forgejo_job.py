#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         _forgejo_job.py
#  Purpose:      Run the Forgejo setup Job's own rendered script against a fake
#                Forgejo API standing on a directory, for the tests that prove
#                what it writes into the deploy repo.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The setup Job's script, run against a deploy repo on disk.

The script is taken from `helm template`, so a test runs what the chart ships.
It runs under busybox sh where present (the Job image's shell), else /bin/sh,
with `curl` on PATH answering the calls the script makes from files on disk.

A fresh repo answers the create call with 201 and reads as an empty repo until
something is written, the way Forgejo serves one created without auto_init. A
repo that is not fresh answers 409, as one that already exists.

The leading underscore keeps pytest from collecting this module as a test file.

    from _forgejo_job import run_job

    job = run_job(tmp_path, fresh=True, sizing={"scale.data-charts.values.yaml": "kafka: {}\\n"})
    assert job.writes[0]["path"] == "sizing/scale.data-charts.values.yaml"
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from _charts import chart_dir

# Answers the Forgejo contents, raw, repos and hooks calls the Job makes, from a
# directory standing in for the deploy repo, and logs every write it accepts.
FAKE_CURL = r'''#!/usr/bin/env python3
import base64
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["FAKE_FORGEJO_STATE"])
repo = state / "repo"
refuse = os.environ.get("FAKE_FORGEJO_REFUSE", "")
args = sys.argv[1:]
method, data, write_out, discard, url = None, None, None, False, ""
i = 0
while i < len(args):
    arg = args[i]
    if arg == "-X":
        method = args[i + 1]
    elif arg == "-d":
        data = args[i + 1]
    elif arg == "-w":
        write_out = args[i + 1]
    elif arg == "-o":
        discard = args[i + 1] == "/dev/null"
    elif arg in ("-u", "-H"):
        pass
    elif arg.startswith("-"):
        i += 1
        continue
    else:
        url = arg
        i += 1
        continue
    i += 2
method = method or ("POST" if data is not None else "GET")
path = url.split("://", 1)[1].split("/", 1)[1].split("?", 1)[0]
code, body = 404, {"message": "not found"}


def compact(value):
    return json.dumps(value, separators=(",", ":"))


def log(entry):
    with (state / "writes.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(compact(entry) + "\n")


def empty():
    return not any(p.is_file() for p in repo.rglob("*"))


if path == "api/healthz":
    code, body = 200, {"status": "pass"}
elif path == "api/v1/user/repos":
    if (state / "fresh").exists():
        (state / "fresh").unlink()
        log({"method": "POST", "path": "user/repos", "content": json.loads(data)})
        code, body = 201, {"empty": True}
    else:
        code, body = 409, {"message": "repository already exists"}
elif "/raw/" in path:
    target = repo / path.split("/raw/", 1)[1]
    if target.is_file():
        sys.stdout.write(target.read_text(encoding="utf-8"))
    sys.exit(0)
elif path.endswith("/hooks"):
    code, body = (201, {"id": 1}) if method == "POST" else (200, [])
elif "/contents/" in path:
    rel = path.split("/contents/", 1)[1]
    target = repo / rel
    payload = json.loads(data) if data else {}
    if method == "GET" and empty():
        # An empty Forgejo repo answers every contents read with an empty list.
        code, body = 200, []
    elif method == "GET" and target.is_dir():
        entries = sorted(p.name for p in target.iterdir())
        code, body = 200, [{"name": n, "path": f"{rel}/{n}", "type": "file"} for n in entries]
    elif method == "GET" and target.is_file():
        code, body = 200, {"name": target.name, "path": rel, "sha": "sha-" + target.name}
    elif method in ("POST", "PUT") and refuse and rel.startswith(refuse):
        code, body = 500, {"message": "refused by the test"}
    elif method == "POST" and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(payload["content"]))
        log({"method": "POST", "path": rel, "content": target.read_text(encoding="utf-8")})
        code, body = 201, {"content": {"sha": "sha-" + target.name}}
    elif method == "PUT" and target.is_file() and payload.get("sha") == "sha-" + target.name:
        target.write_bytes(base64.b64decode(payload["content"]))
        log({"method": "PUT", "path": rel, "content": target.read_text(encoding="utf-8")})
        code, body = 200, {"content": {"sha": "sha-" + target.name}}
    elif method in ("POST", "PUT"):
        code, body = 422, {"message": "refused"}

if not discard:
    sys.stdout.write(compact(body))
if write_out == "%{http_code}":
    sys.stdout.write(str(code))
sys.exit(22 if "-sf" in args and code >= 400 else 0)
'''


@dataclass(frozen=True, slots=True)
class JobRun:
    """One run of the Job script: its exit status, its output, what it wrote, and the repo."""

    returncode: int
    output: str
    writes: list[dict]
    repo: Path


def rendered_job(seed_apps: tuple[str, ...] = ()) -> dict:
    """The setup Job as the forgejo chart renders it, with these seedApps."""
    if shutil.which("helm") is None:
        pytest.skip("helm is not on PATH, so the Job script cannot be rendered")
    out = subprocess.run(
        [
            "helm", "template", "t", str(chart_dir("forgejo")),
            "--show-only", "templates/setup-job.yaml",
            "--set-json", f"deployRepo.seedApps={json.dumps(list(seed_apps))}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return yaml.safe_load(out.stdout)


def run_job(
    tmp_path: Path,
    *,
    seed_apps: tuple[str, ...] = (),
    values: tuple[str, ...] = (),
    marker: str | None = None,
    repo_files: dict[str, str] | None = None,
    sizing: dict[str, str] | None = None,
    fresh: bool = False,
    refuse: str = "",
) -> JobRun:
    """Run the Job script against a deploy repo holding these files.

    Args:
        tmp_path: Where the repo, the sizing seed and the fake curl live.
        seed_apps: deployRepo.seedApps for the render.
        values: Services whose values/<svc>-default-values.yaml the repo already holds.
        marker: The repo's .seeded-apps, or None for none.
        repo_files: Further files the repo already holds, by path.
        sizing: The sizing ConfigMap's files, by name, mounted where the Job reads them.
        fresh: The repo does not exist yet, so the create call makes it empty.
        refuse: A repo path prefix every write under is refused.
    """
    job = rendered_job(seed_apps)
    container = job["spec"]["template"]["spec"]["containers"][0]
    script = container["command"][2]
    env = {item["name"]: item["value"] for item in container["env"] if "value" in item}

    state = tmp_path / "forgejo"
    repo = state / "repo"
    repo.mkdir(parents=True)
    if fresh:
        (state / "fresh").touch()
    for svc in values:
        target = repo / "values" / f"{svc}-default-values.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"deploy:\n  service: {svc}\n", encoding="utf-8")
    if marker is not None:
        (repo / env["MARKER"]).write_text(marker, encoding="utf-8")
    for rel, text in (repo_files or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    seed_dir = tmp_path / "sizing-seed"
    seed_dir.mkdir()
    for name, text in (sizing or {}).items():
        (seed_dir / name).write_text(text, encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(FAKE_CURL, encoding="utf-8")
    curl.chmod(0o755)

    env.update({
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_FORGEJO_STATE": str(state),
        "FAKE_FORGEJO_REFUSE": refuse,
        "GITEA_USER": "admin",
        "GITEA_PASS": "not-a-real-password",
        "WEBHOOK_SECRET": "",
        "SIZING_SEED_DIR": str(seed_dir),
    })
    shell = ["busybox", "sh"] if shutil.which("busybox") else ["/bin/sh"]
    done = subprocess.run(
        [*shell, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=60,
    )
    log = state / "writes.jsonl"
    writes = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return JobRun(done.returncode, done.stdout + done.stderr, writes, repo)
