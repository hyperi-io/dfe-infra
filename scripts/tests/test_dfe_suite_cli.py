"""Tests for tools/dfe_suite.py -- the graph-driven suite CLI.

The graph lives in dfe-infra, so the CLI shells out to that repo's own
``scripts/dfe-stack``. These tests write a real stand-in script at that path in
a temp directory and let the shell-out run for real, which exercises the
invocation rather than replacing it. No mocks, no patched module attributes,
and nothing here reaches the network or a dfe-infra checkout.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# `dfe_suite` is the PACKAGE; the CLI is `scripts/dfe-suite`. Loading it by
# path keeps the two apart -- an `import dfe_suite` here would pull the package.
_CLI = REPO_ROOT / "scripts" / "dfe-suite"
_SPEC = importlib.util.spec_from_file_location(
    "dfe_suite_cli", _CLI, loader=SourceFileLoader("dfe_suite_cli", str(_CLI))
)
ds = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ds)

# One cargo-dep edge, which is the shape `dfe-stack suite --producer` answers
# with: the slice's metadata, the kinds, the nodes and the out-edges. The
# producer node carries the `package` the rebuilds bump.
SLICE = {
    "schema": "1",
    "verified": "2026-09-03",
    "edge_kinds": {"cargo-dep": {"means": "range", "check": "admits?"}},
    "nodes": {
        "suite-producer-xyzzy": {
            "repo": "hyperi-io/suite-producer-xyzzy",
            "package": "scalo",
            "default_in_pass": True,
        },
        "suite-consumer-xyzzy": {
            "repo": "hyperi-io/suite-consumer-xyzzy",
            "maturity": "ga",
            "default_in_pass": True,
        },
    },
    "edges": [
        {
            "from": "suite-producer-xyzzy",
            "to": "suite-consumer-xyzzy",
            "kind": "cargo-dep",
            "type": "potential",
            "evidence": "suite-consumer-xyzzy/Cargo.toml:6",
        }
    ],
}

# The three edges of one consumer that probe_walk.py showed getting swallowed:
# a cargo range that admits (so a rebuild dispatches) beside two edges no
# rebuild answers.
HUMAN_EDGE = {
    "from": "suite-producer-xyzzy",
    "to": "suite-consumer-xyzzy",
    "kind": "mirrored-logic",
    "type": "potential",
    "evidence": "suite-consumer-xyzzy/Cargo.toml:6",
}
CONTRACT_EDGE = {
    "from": "suite-producer-xyzzy",
    "to": "suite-consumer-xyzzy",
    "kind": "contract-guard",
    "type": "lockstep",
    "evidence": "suite-consumer-xyzzy/Cargo.toml:6",
}
GENERATED_EDGE = {
    "from": "suite-producer-xyzzy",
    "to": "suite-consumer-xyzzy",
    "kind": "generated-file",
    "type": "lockstep",
    "evidence": "suite-consumer-xyzzy/Cargo.toml:6",
}

GRAPH = {
    "schema": "1",
    "verified": "2026-09-03",
    "nodes": {
        "suite-infra-xyzzy": {
            "repo": "hyperi-io/suite-infra-xyzzy",
            "role": "infra",
            "language": "python",
            "audience": "suite",
            "maturity": "ga",
            "default_in_pass": True,
        },
        "suite-rustlib-xyzzy": {
            "repo": "hyperi-io/suite-rustlib-xyzzy",
            "package": "scalo",
            "role": "library",
            "language": "rust",
            "audience": "general",
            "maturity": "ga",
            "default_in_pass": True,
        },
        "suite-rustapp-xyzzy": {
            "repo": "hyperi-io/suite-rustapp-xyzzy",
            "role": "service",
            "language": "rust",
            "audience": "suite",
            "maturity": "ga",
            "default_in_pass": True,
        },
        "suite-rustalpha-xyzzy": {
            "repo": "hyperi-io/suite-rustalpha-xyzzy",
            "role": "service",
            "language": "rust",
            "audience": "suite",
            "maturity": "alpha",
            "default_in_pass": False,
        },
        "suite-pyapp-xyzzy": {
            "repo": "hyperi-io/suite-pyapp-xyzzy",
            "role": "service",
            "language": "python",
            "audience": "suite",
            "maturity": "ga",
            "default_in_pass": True,
        },
        "suite-tsapp-xyzzy": {
            "repo": "hyperi-io/suite-tsapp-xyzzy",
            "role": "ui",
            "language": "typescript",
            "audience": "suite",
            "maturity": "ga",
            "default_in_pass": True,
        },
    },
    "edges": [
        {
            "from": "suite-rustlib-xyzzy",
            "to": "suite-rustapp-xyzzy",
            "kind": "cargo-dep",
            "type": "potential",
            "evidence": "suite-rustapp-xyzzy/Cargo.toml:6",
        },
        {
            "from": "suite-rustapp-xyzzy",
            "to": "suite-infra-xyzzy",
            "kind": "image-pin",
            "type": "lockstep",
            "evidence": "suite-infra-xyzzy/Chart.yaml:6",
        },
    ],
    "lanes": [
        {"name": "toolchain", "members": ["suite-infra-xyzzy"]},
        {"name": "libraries", "members": ["suite-rustlib-xyzzy"]},
        {
            "name": "consumers",
            "members": [
                "suite-rustapp-xyzzy",
                "suite-rustalpha-xyzzy",
                "suite-pyapp-xyzzy",
                "suite-tsapp-xyzzy",
            ],
        },
        {"name": "deployment", "members": ["suite-infra-xyzzy"]},
    ],
}

STUB = '''\
"""Stand-in for dfe-infra's scripts/dfe-stack.

A filtered `suite` answers the fixed slice; a bare `suite` answers the whole
graph when one was written beside it.
"""

import sys
from pathlib import Path

here = Path(__file__)
here.with_suffix(".argv").write_text(
    "\\n".join(sys.argv[1:]) + "\\n", encoding="utf-8"
)
if {status}:
    sys.stderr.write("suite.yaml is not readable\\n")
    raise SystemExit({status})
whole = here.with_suffix(".graph")
if sys.argv[1:] == ["suite"] and whole.is_file():
    print(whole.read_text(encoding="utf-8"))
    raise SystemExit(0)
print(here.with_suffix(".slice").read_text(encoding="utf-8"))
'''


def _stub_infra(
    root: Path, *, status: int = 0, slice_: object = None, graph: object = None
) -> Path:
    """A directory shaped enough like dfe-infra to be shelled out to."""
    infra = root / "dfe-infra-stub"
    scripts = infra / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "dfe-stack").write_text(
        STUB.format(status=status), encoding="utf-8", newline="\n"
    )
    (scripts / "dfe-stack.slice").write_text(
        json.dumps(SLICE if slice_ is None else slice_),
        encoding="utf-8",
        newline="\n",
    )
    if graph is not None:
        (scripts / "dfe-stack.graph").write_text(
            json.dumps(graph), encoding="utf-8", newline="\n"
        )
    return infra


def _consumer(root: Path, dependency: str) -> Path:
    """A consumer checkout whose Cargo.toml line 6 is the cited dependency."""
    repo = root / "suite-consumer-xyzzy"
    repo.mkdir(exist_ok=True)
    (repo / "Cargo.toml").write_text(
        f'[package]\nname = "suite-consumer-xyzzy"\n\n[dependencies]\n'
        f'tokio = "1"\n{dependency}\n',
        encoding="utf-8",
        newline="\n",
    )
    return repo


class KindsTests(unittest.TestCase):
    """`kinds` is the tool saying what it can and cannot answer mechanically."""

    def _run(self) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            assert ds.main(["kinds"]) == 0
        return buffer.getvalue()

    def test_every_kind_the_tool_knows_is_listed(self) -> None:
        output = self._run()
        for kind in ds.CHECKS:
            assert f"{kind}:" in output

    def test_the_kinds_a_script_cannot_answer_say_so(self) -> None:
        output = self._run()
        assert "never mechanical" in output
        assert "goes past it" in output


class CheckTests(unittest.TestCase):
    """`check` walks the producer's out-edges and reports, changing nothing."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="dfe-suite-cli-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _check(self, dependency: str, version: str, *, status: int = 0) -> str:
        infra = _stub_infra(self.root, status=status)
        _consumer(self.root, dependency)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.code = ds.main(
                [
                    "check",
                    "suite-producer-xyzzy",
                    version,
                    "--dfe-infra",
                    str(infra),
                    "--repos",
                    str(self.root),
                ]
            )
        return buffer.getvalue()

    def test_the_producer_reaches_dfe_stack(self) -> None:
        output = self._check('scalo = { version = ">=2.10.14, <3" }', "2.11.0")
        argv = (self.root / "dfe-infra-stub" / "scripts" / "dfe-stack.argv").read_text(
            encoding="utf-8"
        )
        assert argv.split() == ["suite", "--producer", "suite-producer-xyzzy"]
        assert "1 out-edge(s)" in output

    def test_an_admitting_range_reports_a_rebuild(self) -> None:
        output = self._check('scalo = { version = ">=2.10.14, <3" }', "2.11.0")
        assert self.code == 0
        line = next(ln for ln in output.splitlines() if ln.startswith("suite-consumer"))
        fields = [field.strip() for field in line.split("|")]
        assert fields[:5] == ["suite-consumer-xyzzy", "cargo-dep", "potential", "needs-change", "rebuild_rust"]

    def test_a_range_that_does_not_admit_also_reads_needs_change(self) -> None:
        # `moved` read as "the producer moved", which is true of every edge on
        # the walk; the column answers whether the CONSUMER has to change.
        output = self._check('scalo = { version = ">=2.10.14, <3" }', "3.0.0")
        line = next(ln for ln in output.splitlines() if ln.startswith("suite-consumer"))
        assert "needs-change" in line
        assert "| moved |" not in line

    def test_the_header_names_the_package_the_producer_publishes(self) -> None:
        output = self._check('scalo = { version = ">=2.10.14, <3" }', "2.11.0")
        assert "package scalo" in output

    def test_a_range_that_does_not_admit_reports_widening_it(self) -> None:
        output = self._check('scalo = { version = ">=2.10.14, <3" }', "3.0.0")
        line = next(ln for ln in output.splitlines() if ln.startswith("suite-consumer"))
        assert "widen-range" in line
        assert "suite-consumer-xyzzy/Cargo.toml:6" in line

    def test_a_missing_consumer_checkout_is_one_bad_line_not_a_crash(self) -> None:
        infra = _stub_infra(self.root)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ds.main(
                [
                    "check",
                    "suite-producer-xyzzy",
                    "2.11.0",
                    "--dfe-infra",
                    str(infra),
                    "--repos",
                    str(self.root / "nowhere"),
                ]
            )
        assert code == 0
        assert "does not exist" in buffer.getvalue()

    def test_a_failing_dfe_stack_stops_the_walk(self) -> None:
        infra = _stub_infra(self.root, status=3)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ds.main(
                ["check", "suite-producer-xyzzy", "2.11.0", "--dfe-infra", str(infra)]
            )
        assert code == 1
        assert buffer.getvalue() == ""

    def test_a_directory_that_is_not_dfe_infra_is_named(self) -> None:
        with pytest.raises(ds.FleetError) as caught:
            ds.load_producer("suite-producer-xyzzy", dfe_infra=self.root)
        assert "dfe-stack" in str(caught.value)


class ReleaseTests(unittest.TestCase):
    """Only the nodes whose release path is proven can be released from here."""

    def test_an_unsupported_node_names_what_is_supported(self) -> None:
        with pytest.raises(ds.FleetError) as caught:
            ds.cmd_release(ds.build_parser().parse_args(["release", "dfe-loader"]))
        message = str(caught.value)
        assert "scalo-rs" in message
        assert "scalo-py" in message

    def test_the_supported_nodes_resolve_to_their_ship_specs(self) -> None:
        assert ds.RELEASABLE["scalo-rs"].repo_name == "scalo-rs"
        assert ds.RELEASABLE["scalo-py"].artefact.kind == "pypi"


class WalkTests(unittest.TestCase):
    """`walk` acts on what the checks say, and stops short where a person is needed."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="dfe-suite-walk-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _walk(
        self,
        dependency: str,
        version: str,
        *,
        slice_: object = None,
        extra: list[str] | None = None,
    ) -> tuple[int, str]:
        infra = _stub_infra(self.root, slice_=slice_)
        _consumer(self.root, dependency)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ds.main(
                [
                    "--dry-run",
                    "walk",
                    "suite-producer-xyzzy",
                    version,
                    "--dfe-infra",
                    str(infra),
                    "--repos",
                    str(self.root),
                    *(extra or []),
                ]
            )
        return code, buffer.getvalue()

    def _slice(self, *edges: dict, **node_overrides: object) -> dict:
        """The stock slice with a different edge list, and optional node edits."""
        slice_ = dict(SLICE)
        slice_["edges"] = list(edges)
        if node_overrides:
            nodes = {name: dict(node) for name, node in SLICE["nodes"].items()}
            nodes["suite-consumer-xyzzy"].update(node_overrides)
            slice_["nodes"] = nodes
        return slice_

    def test_a_range_that_does_not_admit_is_left_for_a_person_and_the_walk_says_so(
        self,
    ) -> None:
        code, output = self._walk('scalo = { version = ">=2.10.14, <3" }', "3.0.0")
        assert code == 2
        assert "widen-range" in output
        assert "need a person" in output
        assert "rebuilding" not in output

    def test_a_derived_edge_needs_nothing_and_the_walk_exits_clean(self) -> None:
        slice_ = self._slice(
            dict(SLICE["edges"][0], kind="derived-pins", type="derived")
        )
        code, output = self._walk(
            'scalo = { version = ">=2.10.14, <3" }', "2.11.0", slice_=slice_
        )
        assert code == 0
        assert "derived-pins" in output
        assert "rebuilding" not in output

    def test_every_edge_of_a_consumer_is_reported_before_it_is_rebuilt(self) -> None:
        # A second edge of a kind that is never mechanical sits beside the
        # cargo range; the walk reports both and the person-list holds only
        # the one a rebuild does not cover.
        slice_ = self._slice(SLICE["edges"][0], HUMAN_EDGE)
        code, output = self._walk(
            'scalo = { version = ">=2.10.14, <3" }', "3.0.0", slice_=slice_
        )
        assert code == 2
        assert output.count("suite-consumer-xyzzy |") == 2
        assert "human-read" in output

    def test_a_producer_the_rebuilds_do_not_know_goes_to_the_person_list(self) -> None:
        slice_ = dict(SLICE)
        nodes = {name: dict(node) for name, node in SLICE["nodes"].items()}
        nodes["suite-producer-xyzzy"]["package"] = "dfe-schemas"
        slice_["nodes"] = nodes
        code, output = self._walk(
            'dfe-schemas = { version = ">=0.2, <1" }', "0.3.0", slice_=slice_
        )
        assert code == 2, output
        assert "rebuild for dfe-schemas is not automated yet" in output
        assert "rebuilding onto" not in output

    def test_a_consumer_outside_the_default_pass_is_skipped_and_named(self) -> None:
        slice_ = self._slice(SLICE["edges"][0], default_in_pass=False, maturity="alpha")
        code, output = self._walk(
            'scalo = { version = ">=2.10.14, <3" }', "2.11.0", slice_=slice_
        )
        assert code == 0, output
        assert "skipped: alpha" in output
        assert "--include suite-consumer-xyzzy" in output

    def test_include_walks_the_skipped_consumer_anyway(self) -> None:
        slice_ = self._slice(SLICE["edges"][0], default_in_pass=False, maturity="alpha")
        code, output = self._walk(
            'scalo = { version = ">=2.10.14, <3" }',
            "3.0.0",
            slice_=slice_,
            extra=["--include", "suite-consumer-xyzzy"],
        )
        assert code == 2, output
        assert "skipped: alpha" not in output
        assert "widen-range" in output

    def test_a_consumer_with_no_checkout_is_one_line_not_a_lost_walk(self) -> None:
        infra = _stub_infra(self.root)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ds.main(
                [
                    "--dry-run",
                    "walk",
                    "suite-producer-xyzzy",
                    "2.11.0",
                    "--dfe-infra",
                    str(infra),
                    "--repos",
                    str(self.root / "nowhere"),
                ]
            )
        output = buffer.getvalue()
        assert code == 2, output
        assert "need a person" in output


class WalkActsTests(unittest.TestCase):
    """The acting path: a dry-run rebuild really is dispatched and reached."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="dfe-suite-acts-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # rebuild_rust short-circuits at dry_run only AFTER require_tools and
        # sync_main, so gh and cargo have to be on PATH for real.
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, body in (
            ("gh", "#!/bin/sh\necho 'hyperi-io/suite-consumer-xyzzy'\n"),
            ("cargo", "#!/bin/sh\nexit 0\n"),
        ):
            path = bin_dir / name
            path.write_text(body, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        saved = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved}"
        self.addCleanup(os.environ.__setitem__, "PATH", saved)

    def _walk(self, *edges: dict) -> tuple[int, str]:
        slice_ = dict(SLICE)
        if edges:
            slice_["edges"] = list(edges)
        infra = _stub_infra(self.root, slice_=slice_)
        repo = _consumer(self.root, 'scalo = { version = ">=2.10.14, <3" }')
        if not (repo / ".git").exists():
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ds.main(
                [
                    "--dry-run",
                    "walk",
                    "suite-producer-xyzzy",
                    "2.11.0",
                    "--dfe-infra",
                    str(infra),
                    "--repos",
                    str(self.root),
                ]
            )
        return code, buffer.getvalue()

    def test_an_admitting_range_reaches_the_rebuild_and_exits_clean(self) -> None:
        code, output = self._walk()
        assert code == 0, output
        assert "rebuilding onto suite-producer-xyzzy 2.11.0" in output
        assert "DRY RUN -- nothing will be changed or pushed" in output
        assert "would pin scalo to 2.11.0" in output
        assert "dry run complete" in output

    def test_a_rebuild_does_not_swallow_a_siblings_edge_in_either_order(self) -> None:
        # The regression: a rebuild dispatched by one edge used to discard
        # every later non-rebuild edge of the same consumer, so the answer
        # depended on the order the graph happened to list them in.
        for label, edges in (
            ("rebuild first", (SLICE["edges"][0], HUMAN_EDGE, CONTRACT_EDGE)),
            ("rebuild last", (HUMAN_EDGE, CONTRACT_EDGE, SLICE["edges"][0])),
        ):
            with self.subTest(label):
                code, output = self._walk(*edges)
                assert code == 2, output
                assert "after the rebuild, confirm:" in output
                assert "mirrored-logic: human-read" in output
                assert "contract-guard: run-contract-test" in output
                assert "2 edge(s) need a person" in output
                assert "rebuilding onto" in output

    def test_a_skip_optimize_consumer_is_released_through_the_consent(self) -> None:
        # walk has no --release-unoptimized to pass, so the consumer's own CI
        # config is what has to route it past hyperi-ci's Build refusal.
        repo = self.root / "suite-consumer-xyzzy"
        workflows = repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (repo / ".hyperi-ci.yaml").write_text(
            "build:\n  skip_optimize: true\n", encoding="utf-8", newline="\n"
        )
        (workflows / "ci.yml").write_text(
            "on:\n"
            "  workflow_dispatch:\n"
            "    inputs:\n"
            "      skip-optimize: {type: string}\n"
            "      release-unoptimized: {type: string}\n"
            "jobs:\n"
            "  ci:\n"
            "    uses: example-org/ci/.github/workflows/rust-ci.yml@main\n"
            "    with:\n"
            "      skip-optimize: ${{ inputs.skip-optimize }}\n"
            "      release-unoptimized: ${{ inputs.release-unoptimized }}\n",
            encoding="utf-8",
            newline="\n",
        )
        code, output = self._walk()
        assert code == 0, output
        assert "release path: consented workflow_dispatch" in output
        assert "without the release trailer" in output
        assert "-f release-unoptimized=true" in output

    def test_an_edge_the_rebuild_provably_covers_is_not_counted(self) -> None:
        # rebuild_rust regenerates the Dockerfile, which IS the generated-file
        # edge, so that one is stated as covered rather than left for a person.
        code, output = self._walk(SLICE["edges"][0], GENERATED_EDGE)
        assert code == 0, output
        assert "generated-file: regenerate-and-diff -- covered by the rebuild" in output
        assert "need a person" not in output


GH_NO_RUNS = """#!/bin/sh
case "$1 $2" in
  "auth status") exit 0 ;;
  "repo view") echo '{"isFork": false, "parent": null,
                      "defaultBranchRef": {"name": "main"}}' ;;
  "run list") echo '[]' ;;
  *) echo '[]' ;;
esac
"""


def _hyperi_ai_installed() -> bool:
    """True when hyperi-ai's fixes_scan.py is reachable.

    `signals` and the review pack's signal line read it. Without hyperi-ai
    there is nothing to gather, so those cases SKIP -- mocking the scanner
    would assert against our own stub rather than the real gather.
    """
    from pathlib import Path as _P

    root = os.environ.get("HYPERI_AI_ROOT")
    candidates = [_P(root) / "tools" / "fixes_scan.py"] if root else []
    candidates += [
        _P.home() / ".local" / "share" / "hyperi-ai" / "tools" / "fixes_scan.py",
        _P("/usr/local/share/hyperi-ai/tools/fixes_scan.py"),
    ]
    return any(path.is_file() for path in candidates)


NEEDS_HYPERI_AI = pytest.mark.skipif(
    not _hyperi_ai_installed(),
    reason="signal gather needs hyperi-ai's fixes_scan.py",
)


@NEEDS_HYPERI_AI
class SignalsTests(unittest.TestCase):
    """`signals` reports a member's open signals through the same gather /fixes uses.

    Named `signals`, not `hygiene`: `hyperi-ai helper hygiene` is repo
    tidiness, which is a different job on a different surface.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="dfe-suite-signals-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._gh("#!/bin/sh\necho 'no gh here' >&2\nexit 1\n")
        self._path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{self._path}"
        self.addCleanup(os.environ.__setitem__, "PATH", self._path)
        self.repo = self.root / "suite-member-xyzzy"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)

    def _gh(self, body: str) -> None:
        """A real gh on a scratch PATH, so every gather runs for real."""
        gh = self.bin_dir / "gh"
        gh.write_text(body, encoding="utf-8", newline="\n")
        gh.chmod(0o755)

    def _run(self) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ds.main(["signals", "suite-member-xyzzy", "--repos", str(self.root)])
        return code, buffer.getvalue()

    def test_a_bare_member_has_no_open_signals_and_says_ci_was_not_checked(
        self,
    ) -> None:
        code, output = self._run()
        assert "0 open signal(s)" in output
        assert "NOT CHECKED" in output
        # The headline must say CI warnings were never read, not claim a
        # clean 0 -- the two look identical downstream of a real miss.
        assert "ci warnings 0" not in output, output
        assert "ci warnings not checked" in output, output
        # Unchecked is not clean: returning 0 here would pass the gate on a
        # check that never ran.
        assert code == 1, output

    def test_a_member_whose_branch_has_no_completed_run_says_so(self) -> None:
        subprocess.run(
            [
                "git",
                "remote",
                "add",
                "origin",
                "https://github.com/hyperi-io/suite-member-xyzzy.git",
            ],
            cwd=self.repo,
            check=True,
        )
        self._gh(GH_NO_RUNS)
        code, output = self._run()
        assert code == 1, output
        assert "no completed run on main" in output
        assert "warnings NOT checked" in output


class ReviewPlanTests(unittest.TestCase):
    """`review-plan` expands a language group into one context pack per member.

    The tool decides no model: the pack says what to review and what is left
    off that subagent, and the agent routes it.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="dfe-suite-plan-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # A gh that fails, so the signal gather degrades to notes rather than
        # reaching the network from a test.
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("#!/bin/sh\necho 'no gh here' >&2\nexit 1\n", encoding="utf-8")
        gh.chmod(0o755)
        saved = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved}"
        self.addCleanup(os.environ.__setitem__, "PATH", saved)
        # One member on disk and the rest absent, so both halves of the path
        # line are exercised.
        self.on_disk = self.root / "suite-rustlib-xyzzy"
        self.on_disk.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=self.on_disk, check=True
        )

    def _plan(self, *extra: str) -> tuple[int, str]:
        infra = _stub_infra(self.root, graph=GRAPH)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ds.main(
                [
                    "review-plan",
                    *extra,
                    "--dfe-infra",
                    str(infra),
                    "--repos",
                    str(self.root),
                ]
            )
        return code, buffer.getvalue()

    def _members(self, output: str) -> list[str]:
        """The member names, in the order the plan printed their packs."""
        return [
            line[3:] for line in output.splitlines() if line.startswith("## suite-")
        ]

    def test_the_whole_graph_is_asked_for_once_with_no_filter(self) -> None:
        code, _ = self._plan("rust")
        assert code == 0
        argv = (self.root / "dfe-infra-stub" / "scripts" / "dfe-stack.argv").read_text(
            encoding="utf-8"
        )
        assert argv.split() == ["suite"]

    def test_a_group_is_every_node_of_that_language(self) -> None:
        code, output = self._plan("rust")
        assert code == 0, output
        assert self._members(output) == ["suite-rustlib-xyzzy", "suite-rustapp-xyzzy", "suite-rustalpha-xyzzy"]
        assert "suite-pyapp-xyzzy" not in output
        assert "suite-tsapp-xyzzy" not in output

    def test_the_order_is_the_graphs_lane_order(self) -> None:
        code, output = self._plan("rust")
        assert code == 0, output
        members = self._members(output)
        # The libraries lane comes before the consumers lane, whatever order
        # the nodes happen to be declared in.
        assert members.index("suite-rustlib-xyzzy") < members.index("suite-rustapp-xyzzy")
        assert "lane order: libraries, consumers, deployment" in output

    def test_ts_is_an_alias_for_typescript(self) -> None:
        code, output = self._plan("ts")
        assert code == 0, output
        assert self._members(output) == ["suite-tsapp-xyzzy"]

    def test_role_keeps_only_the_roles_named(self) -> None:
        code, output = self._plan("rust", "--role", "library")
        assert code == 0, output
        assert self._members(output) == ["suite-rustlib-xyzzy"]

    def test_infra_is_out_of_a_group_by_default(self) -> None:
        code, output = self._plan("python")
        assert code == 0, output
        assert self._members(output) == ["suite-pyapp-xyzzy"]

    def test_a_member_named_outright_is_in_whatever_its_role(self) -> None:
        code, output = self._plan("python", "suite-infra-xyzzy")
        assert code == 0, output
        assert self._members(output) == ["suite-infra-xyzzy", "suite-pyapp-xyzzy"]
        # Named in the toolchain lane AND the deployment lane; planned once.
        assert output.count("## suite-infra-xyzzy") == 1

    def test_a_member_outside_the_default_pass_is_listed_and_marked(self) -> None:
        code, output = self._plan("rust")
        assert code == 0, output
        assert "suite-rustalpha-xyzzy" in output
        assert "default_in_pass: false -- name it to include" in output

    def test_every_pack_says_what_is_not_on_this_model(self) -> None:
        code, output = self._plan("rust")
        assert code == 0, output
        assert output.count("NOT ON THIS MODEL: the security pass -- it runs separately on " "the standard tier") == len(self._members(output))

    def test_the_header_leaves_the_model_to_the_caller(self) -> None:
        code, output = self._plan("rust")
        assert code == 0, output
        assert "fable is gated and needs the user's opt-in" in output
        assert "rule: one subagent per member, serial, the security pass to the " "standard tier" in output

    def test_a_pack_carries_the_edges_both_ways_and_the_rules_file(self) -> None:
        code, output = self._plan("rust")
        assert code == 0, output
        pack = output.split("## suite-rustapp-xyzzy")[1]
        assert "suite-rustlib-xyzzy | cargo-dep | potential" in pack
        assert "suite-infra-xyzzy | image-pin | lockstep" in pack
        assert "language rules: ~/.local/share/hyperi-ai/standards/languages/rust.md" in pack

    @NEEDS_HYPERI_AI
    def test_a_member_that_is_not_checked_out_is_said_so_not_guessed(self) -> None:
        code, output = self._plan("rust")
        assert code == 0, output
        on_disk = output.split("## suite-rustlib-xyzzy")[1].split("## ")[0]
        assert f"path: {self.on_disk}" in on_disk
        assert "0 open --" in on_disk
        assert "ci.checked no" in on_disk
        absent = output.split("## suite-rustapp-xyzzy")[1].split("## ")[0]
        assert "path: not on disk" in absent
        assert "open signals: not on disk" in absent

    def test_an_unknown_group_names_the_valid_ones(self) -> None:
        infra = _stub_infra(self.root, graph=GRAPH)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = ds.main(["review-plan", "cobol", "--dfe-infra", str(infra)])
        assert code == 1
        message = buffer.getvalue()
        for group in ("rust", "python", "typescript", "ts"):
            assert group in message

    def test_json_is_one_dict_per_member(self) -> None:
        code, output = self._plan("rust", "--json")
        assert code == 0, output
        packs = json.loads(output)
        assert [pack["member"] for pack in packs] == ["suite-rustlib-xyzzy", "suite-rustapp-xyzzy", "suite-rustalpha-xyzzy"]
        first = packs[0]
        assert first["repo"] == "hyperi-io/suite-rustlib-xyzzy"
        assert first["language"] == "rust"
        assert first["role"] == "library"
        assert first["out_edges"] == ["suite-rustapp-xyzzy | cargo-dep | potential"]
        assert len(first["passes"]) == 7
        assert "NOT ON THIS MODEL" in first["not_on_this_model"]
        assert not packs[2]["default_in_pass"]


class CliTests(unittest.TestCase):
    """The parser shape, so a typo fails at parse time rather than mid-walk."""

    def test_walk_and_signals_take_their_flags(self) -> None:
        args = ds.build_parser().parse_args(
            [
                "walk",
                "scalo-rs",
                "2.11.0",
                "--subject",
                "fix: x",
                "--no-tests",
                "--repos",
                "/y",
            ]
        )
        assert (args.subject, args.no_tests, args.repos) == ("fix: x", "", "/y")
        args = ds.build_parser().parse_args(["signals", "dfe-loader"])
        assert args.member == "dfe-loader"

    def test_the_skip_flags_take_a_consumer_list_or_stand_bare(self) -> None:
        parse = ds.build_parser().parse_args
        bare = parse(["walk", "scalo-rs", "2.11.0", "--no-tests", "--no-chart"])
        assert (bare.no_tests, bare.no_chart) == ("", "")
        assert ds._flag_applies(bare.no_tests, "dfe-loader")
        listed = parse(
            ["walk", "scalo-rs", "2.11.0", "--no-tests", "dfe-loader,dfe-receiver"]
        )
        assert ds._flag_applies(listed.no_tests, "dfe-loader")
        assert not ds._flag_applies(listed.no_tests, "dfe-fetcher")
        absent = parse(["walk", "scalo-rs", "2.11.0"])
        assert absent.no_chart is None
        assert not ds._flag_applies(absent.no_chart, "dfe-loader")

    def test_include_repeats_and_comma_separates(self) -> None:
        args = ds.build_parser().parse_args(
            ["walk", "scalo-rs", "2.11.0", "--include", "a,b", "--include", "c"]
        )
        assert ds._included(args) == {"a", "b", "c"}

    def test_the_global_flags_work_either_side_of_the_subcommand(self) -> None:
        parse = ds.build_parser().parse_args
        for argv in (
            ["--dry-run", "walk", "scalo-rs", "2.11.0"],
            ["walk", "scalo-rs", "2.11.0", "--dry-run"],
        ):
            assert parse(argv).dry_run, argv
        for argv in (
            ["--org", "elsewhere", "check", "scalo-rs", "2.11.0"],
            ["check", "scalo-rs", "2.11.0", "--org", "elsewhere"],
        ):
            assert parse(argv).org == "elsewhere", argv

    def test_an_unused_subcommand_flag_does_not_overwrite_the_global_one(self) -> None:
        args = ds.build_parser().parse_args(
            ["--dry-run", "check", "scalo-rs", "2.11.0"]
        )
        assert args.dry_run

    def test_a_subcommand_is_required(self) -> None:
        with pytest.raises(SystemExit):
            ds.build_parser().parse_args([])

    def test_check_requires_a_producer_and_a_version(self) -> None:
        with pytest.raises(SystemExit):
            ds.build_parser().parse_args(["check", "scalo-rs"])

    def test_check_takes_the_two_path_flags(self) -> None:
        args = ds.build_parser().parse_args(
            ["check", "scalo-rs", "2.11.0", "--dfe-infra", "/x", "--repos", "/y"]
        )
        assert (args.dfe_infra, args.repos) == ("/x", "/y")

    def test_review_plan_takes_several_targets_and_repeated_roles(self) -> None:
        args = ds.build_parser().parse_args(
            [
                "review-plan",
                "rust",
                "dfe-infra",
                "--role",
                "service",
                "--role",
                "library",
                "--json",
            ]
        )
        assert args.target == ["rust", "dfe-infra"]
        assert args.role == ["service", "library"]
        assert args.json

    def test_review_plan_requires_a_target(self) -> None:
        with pytest.raises(SystemExit):
            ds.build_parser().parse_args(["review-plan"])

    def test_release_takes_an_optional_subject(self) -> None:
        args = ds.build_parser().parse_args(["release", "scalo-rs"])
        assert args.subject == ""
        args = ds.build_parser().parse_args(["release", "scalo-rs", "fix: a thing"])
        assert args.subject == "fix: a thing"


if __name__ == "__main__":
    unittest.main()
