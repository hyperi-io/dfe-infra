#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_helm_lint_triggers.py
#  Purpose:      Guard the paths that run the Helm Lint checks: every file the upgrade
#                tooling and the other tests read is in the filter, and every workflow
#                holding a check starts on every pull request so the check reports.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the paths filter of .github/workflows/helm-lint.yml and the triggers around it.

    python3 -m pytest scripts/tests/test_helm_lint_triggers.py -q

A pull request touching only a file outside the paths skips every guard and merges unchecked.
The upgrade inputs come from the constants the upgrade module itself reads, so a new input it
grows is held to the same filter without this file being edited. The rest come from the globs
the other tests walk, and from the top-level names their sources name.

The filter lives in each workflow's `changes` job rather than in `on.<event>.paths`: a
workflow a path filter never starts reports no check at all, and a required check that never
reports blocks the merge, where a job skipped by its `if:` reports and passes. Every `changes`
job runs the one script in .github/actions/changes, handing it that workflow's paths, and the
script itself runs here against a scratch repository for each verdict it can reach.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(1, str(REPO_ROOT / "bootstrap"))
import dfe_ops_upgrade as u  # noqa: E402

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "helm-lint.yml"
ACTION = REPO_ROOT / ".github" / "actions" / "changes" / "action.yml"
ACTION_REF = "./.github/actions/changes"
# The workflows whose jobs are required checks on main, each filtered by its `changes` job.
GATED = ("helm-lint.yml", "e2e-ui.yml", "tf-validate.yml")
RUN_UNLESS_NOTHING_RELEVANT = "!cancelled() && needs.changes.outputs.relevant != 'false'"

# What `dfe-ops upgrade` and its tests read from the checkout; constraints/ is what
# `dfe-stack compat-check --strict` reads for the target stack.
UPGRADE_INPUTS = (
    u.UPGRADE_ORDER,
    u.VERSIONS_FILE,
    u.DFE_STACK,
    u.ARGOCD_LOGIN,
    Path(u.__file__),
    Path(u.helm_releases.__file__),
    Path(u.argocd_release.__file__),
    REPO_ROOT / "bootstrap" / "bootstrap.sh",
    next((REPO_ROOT / "constraints").glob("*.yaml")),
)


# The trees and files the other scripts/tests modules read from outside the filtered
# directories, written as the globs those modules walk. No other workflow runs them.
TEST_READ_GLOBS = (
    ".github/workflows/*.yml",  # test_workflow_log_masks, test_toolbox_inputs
    ".github/actions/**/action.yml",  # this file
    "docs/**/*.md",  # test_no_cost_figures
    "terraform/**/*.tf",  # test_no_cost_figures, the drift check's sweep
    "sizing/**/*.yaml",  # test_validate_sizing, test_no_cost_figures
    "shapes/**/*.yaml",  # test_validate_sizing, test_no_cost_figures
    "shapes/**/*.json",  # test_no_cost_figures
    "docker/dfe-toolbox/base/*",  # test_toolbox_inputs
    "README.md",  # test_no_cost_figures
    "renovate.json",  # test_dfe_stack
    ".gitignore",  # test_check_versions_drift
    "tests/e2e-ui/specs/engine/oidc-rba.spec.ts",  # test_tester_idp
)

# `REPO_ROOT / "name"` and `REPO_ROOT.glob("name/...")` in a test source.
_NAMED_AT_ROOT = re.compile(
    r"""REPO_ROOT\s*/\s*["']([^"'/]+)["']|REPO_ROOT\.glob\(["']([^"'/*]+)[/"']"""
)


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    return doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` as true


_WIRED = re.compile(r"\$\{\{ steps\.([\w-]+)\.outputs\.relevant \}\}")


def _filter_steps(name: str) -> dict[str, dict]:
    """The steps of a workflow's `changes` job that run the shared filter, by step id."""
    steps = [step for step in _load(name)["jobs"]["changes"]["steps"] if step.get("uses") == ACTION_REF]
    assert steps, f"{name}'s changes job never runs {ACTION_REF}"
    ids = [step.get("id") for step in steps]
    assert all(ids), f"{name}: every filter step needs an id: {ids}"
    assert len(set(ids)) == len(ids), f"{name}: two filter steps share an id: {ids}"
    return {step["id"]: step for step in steps}


def _filter_step(name: str) -> dict:
    """The workflow-wide filter: the step the `changes` job's `relevant` output carries."""
    wired = _load(name)["jobs"]["changes"]["outputs"]["relevant"]
    match = _WIRED.fullmatch(wired)
    assert match, f"{name}'s changes job exports `relevant` from no filter step: {wired}"
    return _filter_steps(name)[match.group(1)]


def _patterns(text: str) -> list[str]:
    """The patterns in a paths block, the way the filter's script reads them: `#` lines are notes."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def _filters() -> list[str]:
    return _patterns(_filter_step(WORKFLOW.name)["with"]["paths"])


def _covers(patterns: list[str], path: str) -> bool:
    """Whether a filter names `path` as a file or under a `dir/**`, the two forms the script reads."""
    return any(path.startswith(p.removesuffix("**")) if p.endswith("/**") else path == p for p in patterns)


def test_the_filters_use_only_the_two_forms_the_script_reads() -> None:
    assert _filters(), "helm-lint.yml's paths are empty, so every test below would pass"
    assert [p for p in _filters() if "*" in p.removesuffix("/**")] == []


@pytest.mark.parametrize("name", GATED)
def test_a_gated_workflow_starts_on_every_pull_request_and_every_push_to_main(name: str) -> None:
    triggers = _triggers(_load(name))
    assert triggers["pull_request"] is None, f"{name} filters pull_request: {triggers['pull_request']}"
    assert triggers["push"] == {"branches": ["main"]}, f"{name} filters push: {triggers['push']}"


@pytest.mark.parametrize("name", GATED)
def test_every_check_in_a_gated_workflow_runs_unless_its_filter_found_nothing(name: str) -> None:
    jobs = _load(name)["jobs"]
    step = _filter_step(name)
    assert _patterns(step["with"]["paths"]), f"{name} hands the filter no pattern"
    wired = f"${{{{ steps.{step['id']}.outputs.relevant }}}}"
    assert jobs["changes"]["outputs"]["relevant"] == wired, f"{name}'s changes job does not export the verdict"
    for job_id, job in jobs.items():
        if job_id == "changes":
            continue
        assert job.get("needs") == "changes", f"{name}:{job_id} does not need the changes job"
        condition = str(job.get("if", ""))
        assert RUN_UNLESS_NOTHING_RELEVANT in condition, f"{name}:{job_id} if: {condition}"


@pytest.mark.parametrize("name", GATED)
def test_the_filter_script_lives_only_in_the_shared_action(name: str) -> None:
    """A copy left in one workflow drifts from the script the others run."""
    scripts = [step.get("name") for step in _load(name)["jobs"]["changes"]["steps"] if "run" in step]
    assert scripts == [], f"{name}'s changes job runs its own script: {scripts}"


@pytest.mark.parametrize("path", UPGRADE_INPUTS, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_every_file_the_upgrade_tooling_reads_starts_the_guards(path: Path) -> None:
    assert path.is_file(), path
    relative = path.relative_to(REPO_ROOT).as_posix()
    assert _covers(_filters(), relative), f"{relative} is not in helm-lint.yml's paths"


def _test_reads() -> list[str]:
    found = {p for pattern in TEST_READ_GLOBS for p in REPO_ROOT.glob(pattern) if p.is_file()}
    return sorted(path.relative_to(REPO_ROOT).as_posix() for path in found)


def test_every_file_the_other_tests_read_starts_the_guards() -> None:
    reads, filters = _test_reads(), _filters()
    assert reads, "the globs match nothing, so this test would pass for any filter"
    outside = [path for path in reads if not _covers(filters, path)]
    shown = f"{outside[:8]} and {len(outside) - 8} more" if len(outside) > 8 else f"{outside}"
    assert outside == [], f"read by a test, outside helm-lint.yml's paths: {shown}"


def test_every_top_level_name_a_test_source_reads_is_in_the_filters() -> None:
    """A test reading a new top-level path fails here until the filter names it."""
    named = {
        name
        for source in (REPO_ROOT / "scripts" / "tests").glob("*.py")
        for groups in _NAMED_AT_ROOT.findall(source.read_text(encoding="utf-8"))
        for name in groups
        if name and (REPO_ROOT / name).exists()
    }
    assert {"docs", "terraform", "README.md"} <= named, f"the scan found {sorted(named)}"
    filters = _filters()
    reached = {n for n in named if any(p == n or p.startswith(f"{n}/") for p in filters)}
    missing = sorted(named - reached)
    assert missing == [], f"named by a test, absent from helm-lint.yml's paths: {missing}"


# --- the narrower filter in front of the third-party arch check ---------------------------------

SCRIPTS = REPO_ROOT / "scripts"
IMAGE_ARCH = SCRIPTS / "check_image_arch.py"
IMAGE_ARCH_JOB = "third-party-arch"


def _image_filters() -> list[str]:
    return _patterns(_filter_steps(WORKFLOW.name)["images"]["with"]["paths"])


def _script_closure(entry: Path) -> list[str]:
    """Every scripts/ file `entry` reaches: modules it imports and files it loads by name.

    A name a source writes as a string constant is a file it loads by path, the
    way check_image_arch.py loads the extensionless dfe-stack.
    """
    seen: set[Path] = set()
    todo = [entry]
    while todo:
        source = todo.pop()
        if source in seen:
            continue
        seen.add(source)
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] + ".py" for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module.split(".")[0] + ".py"]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "/" not in node.value:
                names = [node.value]
            else:
                continue
            todo += [SCRIPTS / name for name in names if name and (SCRIPTS / name).is_file()]
    return sorted(path.relative_to(REPO_ROOT).as_posix() for path in seen)


def test_the_closure_walk_reaches_the_path_loaded_cli() -> None:
    closure = _script_closure(IMAGE_ARCH)
    assert {"scripts/check_image_arch.py", "scripts/registry_pins.py", "scripts/dfe-stack"} <= set(
        closure
    ), closure


def test_the_arch_check_runs_when_any_code_it_reaches_changes() -> None:
    outside = [path for path in _script_closure(IMAGE_ARCH) if not _covers(_image_filters(), path)]
    assert outside == [], f"reached by check_image_arch.py, outside its `images` filter: {outside}"


def test_the_arch_check_runs_when_a_pin_it_reads_changes() -> None:
    import check_image_arch

    pinned = ["versions.yaml", *(path.as_posix() for path, _ in check_image_arch.CHART_VALUES_IMAGES.values())]
    outside = [path for path in pinned if not _covers(_image_filters(), path)]
    assert outside == [], f"read by check_image_arch.py, outside its `images` filter: {outside}"


def test_the_arch_check_job_waits_on_its_own_filter() -> None:
    jobs = _load(WORKFLOW.name)["jobs"]
    assert jobs["changes"]["outputs"]["images"] == "${{ steps.images.outputs.relevant }}"
    assert "needs.changes.outputs.images != 'false'" in str(jobs[IMAGE_ARCH_JOB]["if"])


def test_a_change_the_images_filter_sees_also_starts_the_workflow() -> None:
    """A narrower filter outside the workflow's own would gate a job that never starts."""
    workflow = _filters()
    outside = [p for p in _image_filters() if not _covers(workflow, p.removesuffix("**"))]
    assert outside == [], f"in the `images` filter, outside helm-lint.yml's paths: {outside}"


# --- the shared filter's own verdicts ---------------------------------------------------------

_GIT_ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1"}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=repo, env={**_GIT_ENV, "HOME": str(repo.parent)}, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return done.stdout.strip()


def _filter_script() -> str:
    doc = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    (step,) = doc["runs"]["steps"]
    assert doc["runs"]["using"] == "composite"
    assert doc["outputs"]["relevant"]["value"] == f"${{{{ steps.{step['id']}.outputs.relevant }}}}"
    return step["run"]


@pytest.fixture
def merged(tmp_path: Path) -> tuple[Path, str]:
    """A repository at a pull request merge commit whose second parent changed terraform/x.tf only.

    Its origin is itself, so the push path's fetch of the event's `before` commit resolves.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "terraform").mkdir()
    (repo / "terraform" / "x.tf").write_text("# x\n", encoding="utf-8")
    _git(repo, "add", "terraform/x.tf")
    _git(repo, "commit", "-q", "-m", "change")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "feature")
    _git(repo, "remote", "add", "origin", str(repo))
    return repo, base


def _run_filter(repo: Path, paths: str, event: str, before: str = "") -> tuple[subprocess.CompletedProcess, str]:
    output = repo.parent / "github_output"
    output.write_text("", encoding="utf-8")
    env = {**_GIT_ENV, "HOME": str(repo.parent), "PATHS": paths, "EVENT": event, "BEFORE": before,
           "RUNNER_TEMP": str(repo.parent), "GITHUB_OUTPUT": str(output)}
    # The runner's own invocation for a composite step with `shell: bash`.
    done = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _filter_script()],
                          cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False)
    return done, output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("paths", "verdict"),
    [
        ("terraform/**\n", "true"),
        ("# a note\nterraform/x.tf\n", "true"),
        # README.md changed on main before the pull request, so its diff does not carry it.
        ("helm/**\nREADME.md\n", "false"),
    ],
)
def test_a_pull_request_is_relevant_only_when_its_merge_diff_touches_the_paths(
    merged: tuple[Path, str], paths: str, verdict: str
) -> None:
    repo, _base = merged
    done, output = _run_filter(repo, paths, "pull_request")
    assert done.returncode == 0, done.stdout + done.stderr
    assert output == f"relevant={verdict}\n"


@pytest.mark.parametrize(("paths", "verdict"), [("terraform/**\n", "true"), ("helm/**\n", "false")])
def test_a_push_is_relevant_only_when_its_range_touches_the_paths(
    merged: tuple[Path, str], paths: str, verdict: str
) -> None:
    repo, base = merged
    done, output = _run_filter(repo, paths, "push", before=base)
    assert done.returncode == 0, done.stdout + done.stderr
    assert output == f"relevant={verdict}\n"


@pytest.mark.parametrize(
    ("event", "before", "checkout"),
    [
        ("push", "0" * 40, None),  # a new branch has no range to diff
        ("push", "f" * 40, None),  # a `before` the remote no longer has
        ("pull_request", "", "HEAD^2"),  # no merge commit to diff against its base
        ("workflow_dispatch", "", None),
    ],
)
def test_an_event_with_no_diff_to_read_runs_every_check(
    merged: tuple[Path, str], event: str, before: str, checkout: str | None
) -> None:
    repo, _base = merged
    if checkout:
        _git(repo, "checkout", "-q", checkout)
    done, output = _run_filter(repo, "helm/**\n", event, before=before)
    assert done.returncode == 0, done.stdout + done.stderr
    assert output == "relevant=true\n"


@pytest.mark.parametrize("paths", ["helm/*.yaml\n", "**/x.tf\n", "# only a note\n\n"])
def test_a_paths_block_the_script_cannot_read_fails_rather_than_skipping(
    merged: tuple[Path, str], paths: str
) -> None:
    repo, _base = merged
    done, output = _run_filter(repo, paths, "pull_request")
    assert done.returncode == 1
    assert "::error::" in done.stdout
    assert output == ""


@pytest.mark.parametrize(
    ("changed", "verdict"),
    [
        # A deploy-tooling change of the shape that met a registry rate limit: no pin, no reader.
        (["bootstrap/bootstrap.sh", "scripts/dfe-ops", "scripts/tests/test_render_env.py"], "false"),
        (["bootstrap/bootstrap.sh", "versions.yaml"], "true"),
        (["helm/charts/forgejo/values.yaml"], "true"),
        (["scripts/registry_pins.py"], "true"),
    ],
)
def test_the_images_filter_runs_the_arch_check_only_for_what_it_reads(
    tmp_path: Path, changed: list[str], verdict: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    for name in changed:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("changed\n", encoding="utf-8")
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", "change")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "feature")
    paths = _filter_steps(WORKFLOW.name)["images"]["with"]["paths"]
    done, output = _run_filter(repo, paths, "pull_request")
    assert done.returncode == 0, done.stdout + done.stderr
    assert output == f"relevant={verdict}\n"


def test_the_workflows_name_the_action_these_tests_run() -> None:
    """Inputs reach the script through its env, never spliced into the shell text."""
    assert (REPO_ROOT / ACTION_REF.removeprefix("./") / "action.yml").resolve() == ACTION.resolve()
    assert "${{" not in _filter_script()
