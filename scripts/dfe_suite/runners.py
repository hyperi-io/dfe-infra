#  Project:      dfe-infra
#  File:         scripts/dfe_suite/runners.py
#  Purpose:      Which runners a consumer's CI lands on, which sets how many rebuilds overlap.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A consumer's runner mode, resolved in the order hyperi-ci's Plan resolves it.

The caller workflow's ``runner-mode`` input wins, then the ``GH_RUNNER_MODE``
variable (the repo's, else the org's). With neither, a public repo runs free
on GitHub-hosted runners and any other repo self-hosted. A repo whose
visibility cannot be read counts as self-hosted, the mode with the cap.
"""

from pathlib import Path

from dfe_suite.landing import CI_WORKFLOW_FILE
from dfe_suite.proc import FleetError, run
from dfe_suite.rebuild import _load_yaml

FREE = "free"
SELF_HOSTED = "self-hosted"
MODES = (FREE, SELF_HOSTED)

# A Rust rebuild peaks at about 6 CI jobs on ARC, which fits three 16x32
# runners a node, so self-hosted consumers rebuild this many at a time.
SELF_HOSTED_JOBS = 6

_VARIABLE = "GH_RUNNER_MODE"


def _from_workflow(repo: Path) -> str:
    """The literal ``runner-mode`` a job in the caller workflow passes, else empty."""
    workflow = repo / ".github" / "workflows" / CI_WORKFLOW_FILE
    if not workflow.is_file():
        return ""
    try:
        doc = _load_yaml(workflow)
    except FleetError:
        return ""
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    for job in jobs.values() if isinstance(jobs, dict) else ():
        passed = job.get("with") if isinstance(job, dict) else None
        value = passed.get("runner-mode") if isinstance(passed, dict) else None
        if value in MODES:
            return value
    return ""


def _gh(*args: str) -> str:
    """One gh read's stripped stdout, or empty when it fails."""
    proc = run(["gh", *args], check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def runner_mode(repo: Path, slug: str) -> tuple[str, str]:
    """The runner mode a consumer's CI resolves to, and where it came from.

    Args:
        repo: The consumer's checkout, for its caller workflow.
        slug: ``owner/name`` on GitHub.

    Returns:
        ``(mode, reason)``, the mode one of :data:`MODES`.
    """
    explicit = _from_workflow(repo)
    if explicit:
        return explicit, f"{CI_WORKFLOW_FILE} passes runner-mode {explicit}"
    owner = slug.split("/", 1)[0]
    for scope, path in (
        ("repo", f"repos/{slug}/actions/variables/{_VARIABLE}"),
        ("org", f"orgs/{owner}/actions/variables/{_VARIABLE}"),
    ):
        value = _gh("api", path, "--jq", ".value")
        if value in MODES:
            return value, f"the {scope} variable {_VARIABLE} is {value}"
    visibility = _gh(
        "repo", "view", slug, "--json", "visibility", "--jq", ".visibility"
    )
    if visibility.lower() == "public":
        return FREE, "a public repo with no runner-mode set"
    if visibility:
        return SELF_HOSTED, f"a {visibility.lower()} repo with no runner-mode set"
    return SELF_HOSTED, "visibility unreadable, so self-hosted"
