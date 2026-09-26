#  Project:      dfe-infra
#  File:         scripts/tests/test_forgejo_seed_marker.py
#  Purpose:      Run the Forgejo setup Job's own rendered script against a fake
#                Forgejo API and prove when it writes the deploy repo's
#                .seeded-apps marker.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the deploy repo's seed marker records, run through the real Job script.

The marker names every seedApps entry this deployment has been offered, and the
engine treats a repo with no marker as one where nothing was seeded. A repo stood
up before the marker existed already holds every seeded overlay, so the Job's
old rule -- write only when the set changed -- never gave it one.

The script is taken from `helm template`, so the test runs what the chart ships.
It runs under busybox sh where present (the Job image's shell), else /bin/sh,
with `curl` on PATH answering the calls the script makes from files on disk.

    python3 -m pytest scripts/tests/test_forgejo_seed_marker.py -q
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from _charts import chart_dir

SEED_APPS = ("dfe-receiver", "dfe-loader")
MARKER = ".seeded-apps"

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


if path == "api/healthz":
    code, body = 200, {"status": "pass"}
elif path == "api/v1/user/repos":
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
    if method == "GET" and target.is_dir():
        entries = sorted(p.name for p in target.iterdir())
        code, body = 200, [{"name": n, "path": f"{rel}/{n}", "type": "file"} for n in entries]
    elif method == "GET" and target.is_file():
        code, body = 200, {"name": target.name, "path": rel, "sha": "sha-" + target.name}
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


def _rendered_job() -> dict:
    if shutil.which("helm") is None:
        pytest.skip("helm is not on PATH, so the Job script cannot be rendered")
    out = subprocess.run(
        [
            "helm", "template", "t", str(chart_dir("forgejo")),
            "--show-only", "templates/setup-job.yaml",
            "--set-json", f"deployRepo.seedApps={json.dumps(list(SEED_APPS))}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return yaml.safe_load(out.stdout)


def _run(tmp_path: Path, *, values: tuple[str, ...], marker: str | None) -> tuple[int, str, list[dict]]:
    """Run the Job script against a deploy repo holding these overlays and this marker."""
    container = _rendered_job()["spec"]["template"]["spec"]["containers"][0]
    script = container["command"][2]
    env = {item["name"]: item["value"] for item in container["env"] if "value" in item}

    state = tmp_path / "forgejo"
    (state / "repo" / "values").mkdir(parents=True)
    for svc in values:
        (state / "repo" / "values" / f"{svc}-default-values.yaml").write_text(
            f"deploy:\n  service: {svc}\n", encoding="utf-8"
        )
    if marker is not None:
        (state / "repo" / MARKER).write_text(marker, encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(FAKE_CURL, encoding="utf-8")
    curl.chmod(0o755)

    env.update({
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_FORGEJO_STATE": str(state),
        "GITEA_USER": "admin",
        "GITEA_PASS": "not-a-real-password",
        "WEBHOOK_SECRET": "",
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
    return done.returncode, done.stdout + done.stderr, writes


def _marker_writes(writes: list[dict]) -> list[dict]:
    return [w for w in writes if w["path"] == MARKER]


def test_a_repo_that_predates_the_marker_gains_one(tmp_path: Path) -> None:
    rc, output, writes = _run(tmp_path, values=SEED_APPS, marker=None)

    assert rc == 0, output
    assert [w["path"] for w in writes] == [MARKER]
    assert writes[0]["method"] == "POST"
    assert writes[0]["content"].split() == sorted(SEED_APPS)


def test_a_marker_already_in_step_is_left_alone(tmp_path: Path) -> None:
    rc, output, writes = _run(tmp_path, values=SEED_APPS, marker="dfe-loader\ndfe-receiver\n")

    assert rc == 0, output
    assert writes == []


def test_a_fresh_repo_is_seeded_then_marked(tmp_path: Path) -> None:
    rc, output, writes = _run(tmp_path, values=(), marker=None)

    assert rc == 0, output
    seeded = sorted(w["path"] for w in writes if w["path"].startswith("values/"))
    assert seeded == sorted(f"values/{svc}-default-values.yaml" for svc in SEED_APPS)
    marker = _marker_writes(writes)
    assert [w["method"] for w in marker] == ["POST"]
    assert marker[0]["content"].split() == sorted(SEED_APPS)


def test_a_new_seed_app_reaches_a_marked_repo(tmp_path: Path) -> None:
    rc, output, writes = _run(tmp_path, values=("dfe-receiver",), marker="dfe-receiver\n")

    assert rc == 0, output
    assert [w["path"] for w in writes if w["path"].startswith("values/")] == [
        "values/dfe-loader-default-values.yaml"
    ]
    marker = _marker_writes(writes)
    assert [w["method"] for w in marker] == ["PUT"]
    assert marker[0]["content"].split() == sorted(SEED_APPS)


def test_an_app_the_operator_removed_stays_removed(tmp_path: Path) -> None:
    # Deleting a values file is how an operator turns an app off.
    rc, output, writes = _run(tmp_path, values=("dfe-receiver",), marker="dfe-loader\ndfe-receiver\n")

    assert rc == 0, output
    assert writes == []
