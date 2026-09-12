#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_suite.py
#  Purpose:      Prove the scripts/dfe-suite CLI: the graph-driven check and
#                walk, that the walk stops on the first failed rebuild, that
#                --dry-run reaches no rebuild that acts, and that the four
#                scalo-shaped aliases still resolve.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe-suite -- the graph-driven suite CLI.

The graph is read straight from suite_graph, so these tests hand the CLI a
fixture slice rather than a stand-in script: the reader has its own tests in
test_suite_graph.py, and what is under test here is what the CLI does with the
edges it gets back. The rebuilds are recorded rather than run -- nothing here
reaches the network, cargo, uv or gh.

Runs offline. Under pytest, and standalone via the shared runner:

    python3 -m pytest scripts/tests/test_dfe_suite.py
    python3 scripts/tests/test_dfe_suite.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import run_module  # noqa: E402
from suite.proc import FleetError  # noqa: E402


def _load(name: str, filename: str):
    """Load a script that has no .py suffix, so the loader is told it is source."""
    path = str(SCRIPTS / filename)
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cli = _load("dfe_suite_cli", "dfe-suite")

PRODUCER = "suite-producer-xyzzy"
CONSUMER = "suite-consumer-xyzzy"
SECOND = "suite-second-xyzzy"

CARGO_EDGE = {
    "from": PRODUCER,
    "to": CONSUMER,
    "kind": "cargo-dep",
    "type": "potential",
    "evidence": f"{CONSUMER}/Cargo.toml:6",
}
HUMAN_EDGE = {
    "from": PRODUCER,
    "to": CONSUMER,
    "kind": "mirrored-logic",
    "type": "potential",
    "evidence": f"{CONSUMER}/Cargo.toml:6",
}
DERIVED_EDGE = {
    "from": PRODUCER,
    "to": CONSUMER,
    "kind": "derived-pins",
    "type": "derived",
    "evidence": f"{CONSUMER}/Cargo.toml:6",
}
SECOND_EDGE = {
    "from": PRODUCER,
    "to": SECOND,
    "kind": "cargo-dep",
    "type": "potential",
    "evidence": f"{SECOND}/Cargo.toml:6",
}


def _slice(*edges: dict, **consumer_overrides: object) -> dict:
    """A producer slice shaped like the one suite_graph hands back."""
    nodes = {
        PRODUCER: {"repo": f"hyperi-io/{PRODUCER}", "package": "scalo"},
        CONSUMER: {"repo": f"hyperi-io/{CONSUMER}", "maturity": "ga", "default_in_pass": True},
        SECOND: {"repo": f"hyperi-io/{SECOND}", "maturity": "ga", "default_in_pass": True},
    }
    nodes[CONSUMER].update(consumer_overrides)
    return {
        "schema": "1",
        "verified": "2026-09-03",
        "edge_kinds": {"cargo-dep": {"check": "admits?"}},
        "nodes": nodes,
        "edges": list(edges) or [CARGO_EDGE],
    }


def _consumer(root: Path, name: str, dependency: str) -> Path:
    """A checkout whose Cargo.toml line 6 is the cited dependency."""
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "Cargo.toml").write_text(
        f'[package]\nname = "{name}"\n\n[dependencies]\ntokio = "1"\n{dependency}\n',
        encoding="utf-8",
        newline="\n",
    )
    return repo


class FakeRebuild:
    """Stands in for a rebuild: records the call and answers as told."""

    def __init__(self, *, rc: int = 0, raises: str = "") -> None:
        self.calls: list[tuple[Path, str, dict]] = []
        self.rc = rc
        self.raises = raises

    def __call__(self, repo, version, **kwargs):
        self.calls.append((repo, version, kwargs))
        if self.raises:
            raise FleetError(self.raises)
        return self.rc


def _install(monkeypatch, slice_: dict, rebuild: FakeRebuild | None = None) -> FakeRebuild:
    """Hand the CLI a fixture slice and a recorded rebuild."""
    monkeypatch.setattr(cli, "load_producer", lambda producer, path=None: slice_)
    recorder = rebuild or FakeRebuild()
    monkeypatch.setattr(cli, "rebuild_rust", recorder)
    monkeypatch.setattr(cli, "rebuild_python", recorder)
    return recorder


# --- kinds --------------------------------------------------------------------
def test_kinds_lists_every_kind_the_tool_knows(capsys):
    rc = cli.main(["kinds"])
    captured = capsys.readouterr()

    assert rc == 0
    for kind in cli.CHECKS:
        assert f"{kind}:" in captured.out


def test_the_kinds_a_script_cannot_answer_say_so(capsys):
    """An honest "a person reads this" beats a guess dressed up as an answer."""
    cli.main(["kinds"])
    captured = capsys.readouterr()

    assert "never mechanical" in captured.out
    assert "nothing can drift" in captured.out


# --- check --------------------------------------------------------------------
def test_an_admitting_range_reports_a_rebuild(monkeypatch, capsys, tmp_path):
    _install(monkeypatch, _slice())
    _consumer(tmp_path, CONSUMER, 'scalo = { version = ">=2.10.14, <3" }')

    rc = cli.main(["check", PRODUCER, "2.11.0", "--repos", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 0
    assert "needs-change | rebuild_rust" in captured.out
    assert "which admits 2.11.0" in captured.out


def test_a_range_that_does_not_admit_reports_widening_it_first(monkeypatch, capsys, tmp_path):
    _install(monkeypatch, _slice())
    _consumer(tmp_path, CONSUMER, 'scalo = { version = ">=2.10.14, <3" }')

    cli.main(["check", PRODUCER, "3.0.0", "--repos", str(tmp_path)])
    captured = capsys.readouterr()

    assert "widen-range" in captured.out
    assert "does NOT admit 3.0.0" in captured.out


def test_the_header_names_the_package_the_producer_publishes(monkeypatch, capsys, tmp_path):
    """The two range kinds need it, or they read a neighbouring dependency's range."""
    _install(monkeypatch, _slice())
    _consumer(tmp_path, CONSUMER, 'scalo = { version = ">=2.10.14, <3" }')

    cli.main(["check", PRODUCER, "2.11.0", "--repos", str(tmp_path)])
    captured = capsys.readouterr()

    assert "package scalo" in captured.out


def test_a_missing_consumer_checkout_is_one_bad_line_not_a_crash(monkeypatch, capsys, tmp_path):
    _install(monkeypatch, _slice())

    rc = cli.main(["check", PRODUCER, "2.11.0", "--repos", str(tmp_path / "nowhere")])
    captured = capsys.readouterr()

    assert rc == 0
    assert "human-read" in captured.out


# --- walk ---------------------------------------------------------------------
def test_walk_rebuilds_an_admitting_consumer_and_exits_clean(monkeypatch, capsys, tmp_path):
    rebuild = _install(monkeypatch, _slice())
    _consumer(tmp_path, CONSUMER, 'scalo = { version = ">=2.10.14, <3" }')

    rc = cli.main(["walk", PRODUCER, "2.11.0", "--repos", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 0, captured.out
    assert len(rebuild.calls) == 1
    assert rebuild.calls[0][1] == "2.11.0"
    assert rebuild.calls[0][2]["package"] == "scalo"


def test_a_range_that_does_not_admit_is_left_for_a_person(monkeypatch, capsys, tmp_path):
    rebuild = _install(monkeypatch, _slice())
    _consumer(tmp_path, CONSUMER, 'scalo = { version = ">=2.10.14, <3" }')

    rc = cli.main(["walk", PRODUCER, "3.0.0", "--repos", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 2
    assert "widen-range" in captured.out
    assert "need a person" in captured.out
    assert rebuild.calls == []


def test_a_derived_edge_needs_nothing_and_the_walk_goes_past_it(monkeypatch, capsys, tmp_path):
    rebuild = _install(monkeypatch, _slice(DERIVED_EDGE))
    _consumer(tmp_path, CONSUMER, 'scalo = { version = ">=2.10.14, <3" }')

    rc = cli.main(["walk", PRODUCER, "2.11.0", "--repos", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 0, captured.out
    assert "derived-pins" in captured.out
    assert rebuild.calls == []


def test_a_sibling_edge_the_rebuild_does_not_answer_is_still_counted(
    monkeypatch, capsys, tmp_path
):
    """A lockstep edge must not be swallowed by a sibling that dispatched a rebuild."""
    _install(monkeypatch, _slice(CARGO_EDGE, HUMAN_EDGE))
    _consumer(tmp_path, CONSUMER, 'scalo = { version = ">=2.10.14, <3" }')

    rc = cli.main(["walk", PRODUCER, "2.11.0", "--repos", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 2, captured.out
    assert "after the rebuild, confirm:" in captured.out
    assert "human-read" in captured.out


def test_the_walk_stops_on_the_first_failed_rebuild(monkeypatch, capsys, tmp_path):
    """A second consumer must not be moved onto a producer the first one rejected."""
    rebuild = _install(
        monkeypatch, _slice(CARGO_EDGE, SECOND_EDGE), FakeRebuild(raises="clippy went red")
    )
    _consumer(tmp_path, CONSUMER, 'scalo = { version = ">=2.10.14, <3" }')
    _consumer(tmp_path, SECOND, 'scalo = { version = ">=2.10.14, <3" }')

    rc = cli.main(["walk", PRODUCER, "2.11.0", "--repos", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert len(rebuild.calls) == 1, rebuild.calls
    assert "clippy went red" in captured.out
    assert f"{SECOND} |" not in captured.out


def test_a_rebuild_returning_non_zero_also_stops_the_walk(monkeypatch, capsys, tmp_path):
    rebuild = _install(monkeypatch, _slice(CARGO_EDGE, SECOND_EDGE), FakeRebuild(rc=3))
    _consumer(tmp_path, CONSUMER, 'scalo = { version = ">=2.10.14, <3" }')
    _consumer(tmp_path, SECOND, 'scalo = { version = ">=2.10.14, <3" }')

    rc = cli.main(["walk", PRODUCER, "2.11.0", "--repos", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 3
    assert len(rebuild.calls) == 1
    assert "the walk stops here" in captured.out


def test_a_producer_the_rebuilds_do_not_know_goes_to_the_person_list(
    monkeypatch, capsys, tmp_path
):
    slice_ = _slice()
    slice_["nodes"][PRODUCER]["package"] = "dfe-schemas"
    rebuild = _install(monkeypatch, slice_)
    _consumer(tmp_path, CONSUMER, 'dfe-schemas = { version = ">=0.2, <1" }')

    rc = cli.main(["walk", PRODUCER, "0.3.0", "--repos", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 2, captured.out
    assert "rebuild for dfe-schemas is not automated yet" in captured.out
    assert rebuild.calls == []


def test_a_consumer_outside_the_default_pass_is_skipped_until_it_is_named(
    monkeypatch, capsys, tmp_path
):
    _install(monkeypatch, _slice(default_in_pass=False, maturity="alpha"))
    _consumer(tmp_path, CONSUMER, 'scalo = { version = ">=2.10.14, <3" }')

    rc = cli.main(["walk", PRODUCER, "2.11.0", "--repos", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 0, captured.out
    assert "skipped: alpha" in captured.out
    assert f"--include {CONSUMER}" in captured.out


def test_include_walks_the_skipped_consumer_anyway(monkeypatch, capsys, tmp_path):
    rebuild = _install(monkeypatch, _slice(default_in_pass=False, maturity="alpha"))
    _consumer(tmp_path, CONSUMER, 'scalo = { version = ">=2.10.14, <3" }')

    rc = cli.main(
        ["walk", PRODUCER, "2.11.0", "--repos", str(tmp_path), "--include", CONSUMER]
    )
    captured = capsys.readouterr()

    assert rc == 0, captured.out
    assert "skipped: alpha" not in captured.out
    assert len(rebuild.calls) == 1


def test_walk_dry_run_passes_the_flag_down_and_reaches_no_acting_rebuild(
    monkeypatch, capsys, tmp_path
):
    rebuild = _install(monkeypatch, _slice())
    _consumer(tmp_path, CONSUMER, 'scalo = { version = ">=2.10.14, <3" }')

    rc = cli.main(["-n", "walk", PRODUCER, "2.11.0", "--repos", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 0, captured.out
    assert "[dry-run]" in captured.out
    assert rebuild.calls[0][2]["dry_run"] is True


def test_the_skip_flags_reach_only_the_consumers_they_name(monkeypatch, capsys, tmp_path):
    rebuild = _install(monkeypatch, _slice(CARGO_EDGE, SECOND_EDGE))
    _consumer(tmp_path, CONSUMER, 'scalo = { version = ">=2.10.14, <3" }')
    _consumer(tmp_path, SECOND, 'scalo = { version = ">=2.10.14, <3" }')

    cli.main(
        ["walk", PRODUCER, "2.11.0", "--repos", str(tmp_path), "--no-tests", CONSUMER]
    )
    capsys.readouterr()

    by_repo = {repo.name: kwargs for repo, _version, kwargs in rebuild.calls}
    assert by_repo[CONSUMER]["run_tests"] is False
    assert by_repo[SECOND]["run_tests"] is True


# --- the CLI surface ----------------------------------------------------------
def test_the_four_scalo_shaped_aliases_are_still_there():
    parser = cli.build_parser()
    for alias in ("ship-rs", "ship-py", "rebuild-rs", "rebuild-py"):
        assert parser.parse_args([alias, *(["/x", "1.0.0"] if "rebuild" in alias else [])])


def test_ship_rs_and_ship_py_resolve_to_their_own_release_specs():
    assert cli.RELEASABLE["scalo-rs"] is cli.SHIP_RS
    assert cli.RELEASABLE["scalo-py"] is cli.SHIP_PY
    assert cli.SHIP_RS.artefact.kind == "crates"
    assert cli.SHIP_PY.artefact.kind == "pypi"


def test_an_unsupported_release_node_names_what_is_supported(capsys):
    rc = cli.main(["release", "not-a-node-xyzzy"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "scalo-rs" in captured.err
    assert "scalo-py" in captured.err


def test_the_dry_run_flag_works_either_side_of_the_subcommand():
    assert cli.build_parser().parse_args(["-n", "ship-rs"]).dry_run
    assert cli.build_parser().parse_args(["ship-rs", "-n"]).dry_run
    assert not cli.build_parser().parse_args(["ship-rs"]).dry_run


def test_an_unused_subcommand_flag_does_not_overwrite_the_global_one():
    assert cli.build_parser().parse_args(["-n", "check", "x", "1.0.0"]).dry_run


def test_rebuild_rs_takes_the_skip_flags_and_rebuild_py_does_not():
    parsed = cli.build_parser().parse_args(["rebuild-rs", "/x", "1.0.0", "--no-tests"])
    assert parsed.no_tests
    for flag in ("--no-tests", "--no-chart"):
        try:
            cli.build_parser().parse_args(["rebuild-py", "/x", "1.0.0", flag])
        except SystemExit:
            continue
        raise AssertionError(f"rebuild-py accepted {flag}")


def test_a_subcommand_is_required():
    try:
        cli.build_parser().parse_args([])
    except SystemExit:
        return
    raise AssertionError("a bare invocation was accepted")


def test_signals_says_where_the_gather_is_when_it_is_not_installed(monkeypatch):
    """The gather is a hyperi-ai tool; every other subcommand runs without it."""
    monkeypatch.setattr(cli, "FIXES_SCAN_ROOTS", ("/nowhere-xyzzy",))
    raised = ""
    try:
        cli.fixes_scan()
    except FleetError as exc:
        raised = str(exc)

    assert "HYPERI_AI_HOME" in raised
    assert "/nowhere-xyzzy" in raised


if __name__ == "__main__":
    sys.exit(run_module(globals()))
