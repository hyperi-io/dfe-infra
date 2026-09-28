#  Project:      dfe-infra
#  File:         scripts/tests/test_forgejo_argo_webhook.py
#  Purpose:      Run the Forgejo setup Job's own rendered script against a fake
#                Forgejo API and prove it moves the Argo push hook from the https
#                URL to the plain HTTP one, touching no other hook.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the setup Job does to the deploy repo's Argo CD push hook.

argocd-server now runs insecure behind the gateway, so a TLS ClientHello on its
https port is dropped. A deploy repo set up before that carries its hook at the
https URL; the Job removes that hook and registers the plain HTTP one, and a
second run changes nothing.

The script is taken from `helm template`, so the test runs what the chart ships.
It runs under busybox sh where present (the Job image's shell), else /bin/sh,
with `curl` on PATH answering from a hook list kept on disk in the shape Forgejo
returns (a top-level `url` beside `config.url`).

    python3 -m pytest scripts/tests/test_forgejo_argo_webhook.py -q
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from _charts import chart_dir

HOST = "argocd-server.argocd.svc.cluster.local"
HTTP_HOOK = f"http://{HOST}/api/webhook"
HTTPS_HOOK = f"https://{HOST}/api/webhook"
OTHER_HOOK = "https://ci.example.com/hooks/deploy"

# Answers the calls the Job makes, keeping the repo's hooks in hooks.json and
# logging every hook write it accepts. The deploy repo reads as already seeded.
FAKE_CURL = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["FAKE_FORGEJO_STATE"])
hooks_file = state / "hooks.json"
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
hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
code, body = 404, {"message": "not found"}


def compact(value):
    return json.dumps(value, separators=(",", ":"))


def log(entry):
    with (state / "writes.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(compact(entry) + "\n")


def forgejo_shape(hook):
    return {
        "id": hook["id"], "type": "gogs", "branch_filter": "", "url": hook["url"],
        "config": {"content_type": "json", "url": hook["url"]}, "events": ["push"],
        "authorization_header": "", "content_type": "json", "metadata": None,
        "active": True, "updated_at": "2026-01-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
    }


if path == "api/healthz":
    code, body = 200, {"status": "pass"}
elif path == "api/v1/user/repos":
    code, body = 409, {"message": "repository already exists"}
elif "/raw/" in path:
    sys.exit(0)
elif "/contents/" in path:
    code, body = 200, {"name": ".seeded-apps", "sha": "sha-marker"}
elif path.endswith("/hooks") and method == "GET":
    code, body = 200, [forgejo_shape(h) for h in hooks]
elif path.endswith("/hooks") and method == "POST":
    config = json.loads(data)["config"]
    counter = state / "last_id"
    new_id = int(counter.read_text(encoding="utf-8")) + 1
    counter.write_text(str(new_id), encoding="utf-8")
    new = {"id": new_id, "url": config["url"]}
    hooks.append(new)
    log({"method": "POST", "id": new["id"], "url": new["url"]})
    code, body = 201, forgejo_shape(new)
elif "/hooks/" in path and method == "DELETE":
    hook_id = int(path.rsplit("/", 1)[1])
    kept = [h for h in hooks if h["id"] != hook_id]
    code, body = (204, None) if len(kept) < len(hooks) else (404, {"message": "not found"})
    if code == 204:
        log({"method": "DELETE", "id": hook_id})
    hooks = kept
hooks_file.write_text(compact(hooks), encoding="utf-8")

if not discard and body is not None:
    sys.stdout.write(compact(body))
if write_out == "%{http_code}":
    sys.stdout.write(str(code))
sys.exit(22 if "-sf" in args and code >= 400 else 0)
'''


def _rendered_job() -> dict:
    if shutil.which("helm") is None:
        pytest.skip("helm is not on PATH, so the Job script cannot be rendered")
    out = subprocess.run(
        ["helm", "template", "t", str(chart_dir("forgejo")), "--show-only", "templates/setup-job.yaml"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return yaml.safe_load(out.stdout)


def _forgejo(tmp_path: Path, hooks: list[str]) -> Path:
    """A fake Forgejo whose deploy repo carries a hook at each of these URLs."""
    state = tmp_path / "forgejo"
    state.mkdir()
    listed = [{"id": n, "url": url} for n, url in enumerate(hooks, start=1)]
    (state / "hooks.json").write_text(json.dumps(listed), encoding="utf-8")
    # Forgejo never reuses a hook id, so the next one follows the highest issued.
    (state / "last_id").write_text(str(len(hooks)), encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(FAKE_CURL, encoding="utf-8")
    curl.chmod(0o755)
    return state


def _run_job(tmp_path: Path, state: Path) -> tuple[int, str, list[dict]]:
    """One run of the Job script; returns its exit, its output and the hook writes it made."""
    container = _rendered_job()["spec"]["template"]["spec"]["containers"][0]
    script = container["command"][2]
    env = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    log = state / "writes.jsonl"
    log.unlink(missing_ok=True)
    env.update({
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "FAKE_FORGEJO_STATE": str(state),
        "GITEA_USER": "admin",
        "GITEA_PASS": "not-a-real-password",
        "WEBHOOK_SECRET": "not-a-real-secret",
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
    writes = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return done.returncode, done.stdout + done.stderr, writes


def _hook_urls(state: Path) -> list[str]:
    return sorted(h["url"] for h in json.loads((state / "hooks.json").read_text(encoding="utf-8")))


def test_a_fresh_repo_gets_the_plain_http_hook(tmp_path: Path) -> None:
    state = _forgejo(tmp_path, [])
    rc, output, writes = _run_job(tmp_path, state)

    assert rc == 0, output
    assert writes == [{"method": "POST", "id": 1, "url": HTTP_HOOK}]
    assert _hook_urls(state) == [HTTP_HOOK]


def test_an_upgraded_repo_trades_the_https_hook_for_the_http_one(tmp_path: Path) -> None:
    state = _forgejo(tmp_path, [HTTPS_HOOK])
    rc, output, writes = _run_job(tmp_path, state)

    assert rc == 0, output
    assert writes == [
        {"method": "DELETE", "id": 1},
        {"method": "POST", "id": 2, "url": HTTP_HOOK},
    ]
    assert _hook_urls(state) == [HTTP_HOOK]
    assert f"removed the Argo webhook at {HTTPS_HOOK} (hook 1)" in output


def test_the_second_run_on_an_upgraded_repo_changes_nothing(tmp_path: Path) -> None:
    state = _forgejo(tmp_path, [HTTPS_HOOK])
    first_rc, first_output, _ = _run_job(tmp_path, state)
    assert first_rc == 0, first_output

    rc, output, writes = _run_job(tmp_path, state)

    assert rc == 0, output
    assert writes == []
    assert _hook_urls(state) == [HTTP_HOOK]
    assert "Argo webhook already registered" in output


def test_a_hook_at_any_other_url_is_left_alone(tmp_path: Path) -> None:
    near_misses = [OTHER_HOOK, f"{HTTPS_HOOK}/", f"https://{HOST}:8443/api/webhook"]
    state = _forgejo(tmp_path, [*near_misses, HTTP_HOOK])
    rc, output, writes = _run_job(tmp_path, state)

    assert rc == 0, output
    assert writes == []
    assert _hook_urls(state) == sorted([*near_misses, HTTP_HOOK])


def test_only_the_https_hook_goes_when_others_sit_beside_it(tmp_path: Path) -> None:
    state = _forgejo(tmp_path, [OTHER_HOOK, HTTPS_HOOK])
    rc, output, writes = _run_job(tmp_path, state)

    assert rc == 0, output
    assert writes == [
        {"method": "DELETE", "id": 2},
        {"method": "POST", "id": 3, "url": HTTP_HOOK},
    ]
    assert _hook_urls(state) == sorted([OTHER_HOOK, HTTP_HOOK])
