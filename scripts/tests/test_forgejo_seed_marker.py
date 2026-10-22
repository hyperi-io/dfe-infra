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

_forgejo_job.py renders the script and runs it against a deploy repo on disk.

    python3 -m pytest scripts/tests/test_forgejo_seed_marker.py -q
"""

from pathlib import Path

from _forgejo_job import run_job

SEED_APPS = ("dfe-receiver", "dfe-loader")
MARKER = ".seeded-apps"


def _run(tmp_path: Path, *, values: tuple[str, ...], marker: str | None) -> tuple[int, str, list[dict]]:
    """Run the Job script against a deploy repo holding these overlays and this marker."""
    job = run_job(tmp_path, seed_apps=SEED_APPS, values=values, marker=marker)
    return job.returncode, job.output, job.writes


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
