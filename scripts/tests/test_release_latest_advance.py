#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_release_latest_advance.py
#  Purpose:      Guard the Stack Release step that moves `latest`: it lands on main
#                only through a pull request whose checks have passed on the exact
#                head it merges, and it never pushes to main.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the `latest` advance in .github/workflows/release.yml.

    python3 -m pytest scripts/tests/test_release_latest_advance.py -q

main requires its checks on every update, so the advance opens a pull request, waits for its
checks and squash-merges it pinned to the head it pushed. The step's own script runs here
under bash against a bare repository standing in for origin, with the real `dfe-stack
set-latest` editing a copy of the real versions.yaml. `gh` is a stub that answers from a
scenario and logs every call; `sleep` and `timeout` are stubs so no case waits.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
TAG = "9.9.9"
BRANCH = f"stack-latest/{TAG}"
PR_URL = "https://github.com/example/dfe-infra/pull/7"
APP_TOKEN = "${{ steps.latest-token.outputs.token }}"
RELEASE_ONLY = "steps.ver.outputs.cut == 'true' && steps.ver.outputs.maturity == 'release'"

# Loads the real dfe-stack by path and points it at this checkout, so set-latest edits the copy.
_DFE_STACK_SHIM = f"""\
#!/usr/bin/env python3
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

loader = importlib.machinery.SourceFileLoader("dfe_stack", {str(REPO_ROOT / "scripts" / "dfe-stack")!r})
spec = importlib.util.spec_from_loader("dfe_stack", loader)
stack = importlib.util.module_from_spec(spec)
loader.exec_module(stack)
stack.REPO_ROOT = Path(__file__).resolve().parent.parent
sys.argv[0] = "dfe-stack"
sys.exit(stack.main())
"""

# Answers each gh call the step makes from the scenario in GH_STUB_STATE and logs its argv.
_GH_STUB = """\
#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

path = Path(os.environ["GH_STUB_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
with open(state["log"], "a") as log:
    log.write("gh " + " ".join(args) + "\\n")


def git(repo, *argv):
    return subprocess.run(["git", *argv], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def save():
    path.write_text(json.dumps(state))


verb = args[:2]
if verb == ["pr", "list"]:
    print(state["existing_pr"])
elif verb == ["pr", "create"]:
    print(state["pr_url"])
elif verb == ["pr", "view"]:
    field = args[args.index("--json") + 1]
    if field == "headRefOid":
        print(git(state["origin"], "rev-parse", "refs/heads/" + state["branch"]))
    else:
        print(state["merge_states"].pop(0) if state["merge_states"] else "BLOCKED")
        save()
elif verb == ["pr", "checks"]:
    if "--watch" in args:
        sys.exit(state["watch_exit"])
    print(11)
elif verb == ["pr", "merge"]:
    head = args[args.index("--match-head-commit") + 1]
    if state["merge_results"].pop(0) if state["merge_results"] else True:
        state["merged"] = head
        save()
        sys.exit(0)
    # "main" lands an unrelated commit on main; "branch" lands a stranger's commit on the PR branch.
    target = state.pop("on_refusal", "")
    if target:
        mover = state["mover"]
        ref = "main" if target == "main" else state["branch"]
        git(mover, "fetch", "-q", "origin", ref)
        git(mover, "checkout", "-q", "-B", "mover", "FETCH_HEAD")
        Path(mover, "MOVED.md").write_text(target + " moved\\n")
        git(mover, "add", "MOVED.md")
        git(mover, "commit", "-q", "-m", "fix: another change lands first")
        git(mover, "push", "-q", "origin", "HEAD:refs/heads/" + ref)
    save()
    print("merge refused", file=sys.stderr)
    sys.exit(1)
else:
    sys.exit("unexpected gh call: " + " ".join(args))
"""


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps() -> list[dict]:
    return _workflow()["jobs"]["release"]["steps"]


def _step(name: str) -> dict:
    (step,) = [s for s in _steps() if s.get("name") == name]
    return step


def _advance() -> dict:
    return _step("Advance `latest` through a pull request")


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, env=_base_env(repo.parent), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=True)
    return done.stdout.strip()


def _base_env(home: Path) -> dict[str, str]:
    return {
        "PATH": f"{home / 'bin'}:{Path(sys.executable).parent}:/usr/bin:/bin",
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }


class Origin:
    """A bare origin seeded with the real versions.yaml, a clone the step runs in, and the gh stub."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bare = root / "origin.git"
        self.work = root / "work"
        self.log = root / "gh.log"
        self.state = root / "gh-state.json"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, body in {
            "gh": _GH_STUB,
            "sleep": "#!/bin/sh\nexit 0\n",
            "timeout": '#!/bin/sh\nshift\nexec "$@"\n',
        }.items():
            (bin_dir / name).write_text(body, encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        (root / ".gitconfig").write_text("[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n",
                                         encoding="utf-8")

        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], env=_base_env(root), check=True)
        seed = root / "seed"
        seed.mkdir()
        _git(seed, "init", "-q")
        shutil.copy(REPO_ROOT / "versions.yaml", seed / "versions.yaml")
        (seed / "scripts").mkdir()
        (seed / "scripts" / "dfe-stack").write_text(_DFE_STACK_SHIM, encoding="utf-8")
        _git(seed, "add", ".")
        _git(seed, "commit", "-q", "-m", "seed")
        _git(seed, "push", "-q", str(self.bare), "HEAD:refs/heads/main")
        subprocess.run(["git", "clone", "-q", "--branch", "main", str(self.bare), str(self.work)],
                       env=_base_env(root), check=True)
        subprocess.run(["git", "clone", "-q", str(self.bare), str(root / "mover")], env=_base_env(root),
                       check=True)

    def scenario(self, **overrides: object) -> None:
        state = {"log": str(self.log), "origin": str(self.bare), "mover": str(self.root / "mover"),
                 "branch": BRANCH, "pr_url": PR_URL, "existing_pr": "", "watch_exit": 0,
                 "merge_results": [], "merge_states": []}
        state.update(overrides)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def read_state(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def run(self, git_trace: Path | None = None) -> subprocess.CompletedProcess:
        step = _advance()
        env = {**_base_env(self.root), "GH_STUB_STATE": str(self.state), "TAG": TAG,
               "GH_TOKEN": "stub-token"}
        if git_trace:
            env["GIT_TRACE"] = str(git_trace)
        # The runner's own invocation for `run:` with the default bash shell.
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                              cwd=self.work, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False)

    def gh_calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def rev(self, ref: str) -> str:
        return _git(self.root / "seed", "--git-dir", str(self.bare), "rev-parse", ref)

    def show(self, *args: str) -> str:
        return _git(self.root / "seed", "--git-dir", str(self.bare), "show", *args)


@pytest.fixture
def origin(tmp_path: Path) -> Origin:
    return Origin(tmp_path)


def _verbs(calls: list[str]) -> list[str]:
    return [" ".join(call.split()[:3]) for call in calls]


# --- the workflow wiring --------------------------------------------------------------------------


def test_no_step_pushes_to_main() -> None:
    """main takes no push without its required checks, and a direct push carries none."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "HEAD:main" not in text
    assert "[skip ci]" not in text


def test_the_token_checkout_and_advance_run_only_for_a_release_maturity_cut() -> None:
    names = ("Mint a dfe-infra token for the `latest` pull request", "Check out main with the App token",
             "Advance `latest` through a pull request")
    for name in names:
        assert _step(name)["if"] == RELEASE_ONLY, name
    order = [s.get("name") for s in _steps()]
    assert [order.index(n) for n in names] == sorted(order.index(n) for n in names)
    assert order.index(names[0]) > order.index("Cosign sign the artifact (keyless, GitHub OIDC)")


def test_the_app_token_is_scoped_to_this_repository_and_what_the_advance_does() -> None:
    mint = _step("Mint a dfe-infra token for the `latest` pull request")
    assert mint["uses"] == "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1"
    assert mint["with"] == {
        "client-id": "${{ vars.GH_APP_CLIENT_ID }}",
        "private-key": "${{ secrets.GH_APP_PRIVATE_KEY }}",
        "owner": "${{ github.repository_owner }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-contents": "write",
        "permission-pull-requests": "write",
        "permission-checks": "read",
        "permission-statuses": "read",
    }


def test_the_push_and_every_gh_call_use_the_app_token() -> None:
    """A push or pull request made with GITHUB_TOKEN starts no check, or one waiting for approval."""
    checkout = _step("Check out main with the App token")
    assert checkout["with"] == {"ref": "main", "path": ".latest", "token": APP_TOKEN}
    advance = _advance()
    assert advance["working-directory"] == ".latest"
    assert advance["env"]["GH_TOKEN"] == APP_TOKEN
    assert "${{" not in advance["run"]


def test_maturity_is_read_once_and_both_consumers_take_it_from_there() -> None:
    assert _step("Attach to the GitHub release")["env"]["MATURITY"] == "${{ steps.ver.outputs.maturity }}"
    assert sum("dfe-stack maturity" in s.get("run", "") for s in _steps()) == 1


# --- the step itself ------------------------------------------------------------------------------


def test_the_advance_merges_a_pull_request_pinned_to_the_head_it_pushed(origin: Origin, tmp_path: Path) -> None:
    main_before = origin.rev("refs/heads/main")
    origin.scenario()
    trace = tmp_path / "git-trace"
    done = origin.run(git_trace=trace)
    assert done.returncode == 0, done.stdout + done.stderr

    head = origin.rev(f"refs/heads/{BRANCH}")
    assert origin.rev(f"{head}^") == main_before
    assert f'latest: "{TAG}"' in origin.show(f"{head}:versions.yaml")
    assert origin.show("--no-patch", "--format=%B", head).strip() == f"chore: advance latest -> {TAG}"
    assert origin.rev("refs/heads/main") == main_before, "the step pushed to main"
    assert origin.read_state()["merged"] == head

    calls = origin.gh_calls()
    assert _verbs(calls) == ["gh pr list", "gh pr create", "gh pr view", "gh pr checks", "gh pr checks",
                             "gh pr merge"]
    assert f"--base main --head {BRANCH} --title chore: advance latest -> {TAG}" in calls[1]
    assert "--watch --fail-fast --interval 30" in calls[4]
    assert calls[5] == f"gh pr merge {PR_URL} --squash --match-head-commit {head}"

    pushes = [line.split("built-in: ", 1)[1] for line in trace.read_text(encoding="utf-8").splitlines()
              if "built-in: git" in line and (" push " in line or " commit " in line or " fetch " in line)]
    print("\n".join(["git fetch/commit/push, then gh:", *pushes, *calls]))


def test_a_failing_check_leaves_the_pull_request_open_and_merges_nothing(origin: Origin) -> None:
    origin.scenario(watch_exit=1)
    done = origin.run()
    assert done.returncode == 1
    assert "::error::a check on" in done.stdout
    assert "gh pr merge" not in _verbs(origin.gh_calls())
    assert "merged" not in origin.read_state()


def test_a_conflict_with_a_moved_main_rebuilds_the_branch_on_it(origin: Origin) -> None:
    origin.scenario(merge_results=[False], merge_states=["DIRTY"], on_refusal="main")
    first_main = origin.rev("refs/heads/main")
    done = origin.run()
    assert done.returncode == 0, done.stdout + done.stderr

    moved_main = origin.rev("refs/heads/main")
    head = origin.rev(f"refs/heads/{BRANCH}")
    assert moved_main != first_main
    assert origin.rev(f"{head}^") == moved_main, "the second attempt was not cut from the moved main"
    assert origin.read_state()["merged"] == head
    verbs = _verbs(origin.gh_calls())
    assert verbs.count("gh pr create") == 1, "the rebuilt branch opened a second pull request"
    assert verbs.count("gh pr merge") == 2


def test_a_required_check_reporting_late_is_waited_for_before_the_merge_is_judged(origin: Origin) -> None:
    origin.scenario(merge_results=[False, False], merge_states=["BLOCKED", "BLOCKED"])
    done = origin.run()
    assert done.returncode == 0, done.stdout + done.stderr
    verbs = _verbs(origin.gh_calls())
    assert verbs.count("gh pr merge") == 3
    assert verbs.count("gh pr create") == 1


def test_a_merge_refused_for_good_fails_the_step_and_names_the_state(origin: Origin) -> None:
    origin.scenario(merge_results=[False] * 10, merge_states=["BLOCKED"] * 10)
    done = origin.run()
    assert done.returncode == 1
    assert f"::error::{PR_URL} stayed unmergeable (BLOCKED)" in done.stdout
    assert _verbs(origin.gh_calls()).count("gh pr merge") == 10


def test_a_branch_and_pull_request_left_by_an_earlier_run_are_reused(origin: Origin) -> None:
    mover = origin.root / "mover"
    _git(mover, "checkout", "-q", "-b", "stale")
    (mover / "STALE.md").write_text("an earlier attempt\n", encoding="utf-8")
    _git(mover, "add", "STALE.md")
    _git(mover, "commit", "-q", "-m", "an earlier attempt")
    _git(mover, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
    _git(mover, "checkout", "-q", "main")
    origin.scenario(existing_pr=PR_URL)
    done = origin.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "gh pr create" not in _verbs(origin.gh_calls())
    head = origin.rev(f"refs/heads/{BRANCH}")
    assert origin.rev(f"{head}^") == origin.rev("refs/heads/main")


def test_a_push_to_the_branch_by_anyone_else_is_never_overwritten(origin: Origin) -> None:
    """The lease names the head this step last pushed, so a stranger's commit refuses the rebuild."""
    origin.scenario(merge_results=[False], merge_states=["DIRTY"], on_refusal="branch")
    done = origin.run()
    assert done.returncode != 0
    assert "stale info" in done.stderr
    assert "MOVED.md" in origin.show("--name-only", "--format=", f"refs/heads/{BRANCH}")
    assert "merged" not in origin.read_state()


def test_a_main_already_pointing_at_the_tag_opens_nothing(origin: Origin) -> None:
    seed = origin.root / "seed"
    _git(seed, "pull", "-q", str(origin.bare), "main")
    subprocess.run([sys.executable, str(seed / "scripts" / "dfe-stack"), "set-latest", TAG], cwd=seed,
                   env=_base_env(origin.root), check=True, capture_output=True)
    _git(seed, "commit", "-q", "-am", "latest already moved")
    _git(seed, "push", "-q", str(origin.bare), "HEAD:refs/heads/main")
    origin.scenario()
    done = origin.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"main already points latest at {TAG}" in done.stdout
    assert _verbs(origin.gh_calls()) == ["gh pr list"]
