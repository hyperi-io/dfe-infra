#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_member_pins.py
#  Purpose:      Guard the member pin scan: the stack image map and its alias
#                data, the three pin shapes, the tag and digest comparison,
#                waivers, and reading members at origin/main, never a working tree.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/check_member_pins.py and scripts/stack_images.py.

    python3 -m pytest scripts/tests/test_check_member_pins.py -q

Member trees are real git repositories in a temp directory, so every read goes
through the same `git grep` and `git show` at origin/main the real scan runs.
Nothing here reaches the network or a real member checkout.
"""

import importlib.util
import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import check_member_pins as cmp  # noqa: E402
import stack_images  # noqa: E402
import suite_graph  # noqa: E402

KAFKA_DIGEST = "sha256:" + "a" * 64
CH_DIGEST = "sha256:" + "b" * 64
RP_DIGEST = "sha256:" + "c" * 64
OLD_DIGEST = "sha256:" + "0" * 64

# Two stacks, so a test can prove only the current block is read.
VERSIONS = f"""current: "2.0.0"
stacks:
  1.0.0:
    services:
      # image: apache/kafka -- moves by hand
      kafka-version: "4.2.0"
    services-digests:
      kafka-version: "{OLD_DIGEST}"
  2.0.0:
    services:
      # image: apache/kafka rejects=apache/kafka-native -- moves by hand
      kafka-version: "4.3.1"
      # renovate: datasource=docker depName=clickhouse/clickhouse-server
      clickhouse-version: "26.3.32.14"
      # image: docker.redpanda.com/redpandadata/redpanda aliases=redpandadata/redpanda,example.test/rp
      redpanda-version: "v26.2.2"
      # renovate: datasource=docker depName=otel/opentelemetry-collector-contrib
      otel-collector: "0.158.0"
    services-digests:
      kafka-version: "{KAFKA_DIGEST}"
      clickhouse-version: "{CH_DIGEST}"
      redpanda-version: "{RP_DIGEST}"
"""

NODES = {
    "mem-a": {"classification": "product"},
    "mem-b": {"classification": "product"},
    "mem-fork": {"classification": "fork"},
    "dfe-infra": {"classification": "product"},
}


def _images() -> list[stack_images.StackImage]:
    return stack_images.stack_images(VERSIONS)[1]


@pytest.fixture(autouse=True)
def _no_real_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test whose read reaches the real `gh api`, which would read
    GitHub as whoever this host is logged in as."""

    def refuse(*args: str) -> subprocess.CompletedProcess:
        raise AssertionError(f"test reached the real gh api: {args}")

    monkeypatch.setattr(cmp, "_gh", refuse)


# ---------------------------------------------------------------------------
# stack_images -- the map is the annotations, the digests and the alias data
# ---------------------------------------------------------------------------


def test_stack_images_reads_the_current_block_and_its_digests() -> None:
    stack, images = stack_images.stack_images(VERSIONS)
    by_key = {image.key: image for image in images}
    assert stack == "2.0.0"
    assert by_key["kafka-version"].tag == "4.3.1"
    assert by_key["kafka-version"].digest == KAFKA_DIGEST
    assert by_key["clickhouse-version"].ref == "clickhouse/clickhouse-server"
    # Annotated but never digested: nothing immutable to compare against.
    assert "otel-collector" not in by_key


def test_stack_images_reads_aliases_and_rejects_as_data() -> None:
    by_key = {image.key: image for image in _images()}
    assert by_key["redpanda-version"].aliases == ("redpandadata/redpanda", "example.test/rp")
    assert by_key["redpanda-version"].refs[0] == "docker.redpanda.com/redpandadata/redpanda"
    assert by_key["kafka-version"].rejects == ("apache/kafka-native",)
    assert by_key["kafka-version"].aliases == ()


def test_annotated_images_contract_is_unchanged_by_the_extra_tokens() -> None:
    found = stack_images.annotated_images(VERSIONS.split("  2.0.0:")[1])
    assert found["redpanda-version"] == ("docker.redpanda.com/redpandadata/redpanda", "v26.2.2")
    assert found["kafka-version"] == ("apache/kafka", "4.3.1")


def test_normalise_ref_drops_only_the_docker_hub_spellings() -> None:
    assert stack_images.normalise_ref("docker.io/library/busybox") == "busybox"
    assert stack_images.normalise_ref("docker.io/apache/kafka") == "apache/kafka"
    assert stack_images.normalise_ref("ghcr.io/kafbat/kafka-ui") == "ghcr.io/kafbat/kafka-ui"


def test_the_real_stack_carries_the_redpanda_alias_and_the_kafka_native_reject() -> None:
    _stack, images = stack_images.stack_images((REPO_ROOT / "versions.yaml").read_text(encoding="utf-8"))
    by_key = {image.key: image for image in images}
    assert by_key["redpanda-version"].aliases == ("redpandadata/redpanda",)
    assert by_key["kafka-version"].ref == "apache/kafka"
    assert by_key["kafka-version"].rejects == ("apache/kafka-native",)


# ---------------------------------------------------------------------------
# extract_pins -- the three shapes, each read once
# ---------------------------------------------------------------------------

RUST = f"""/// Kafka to test against.
// renovate: datasource=docker depName=apache/kafka
const KAFKA_TAG: &str = "4.2.0";

/// Digest of `KAFKA_TAG`.
const KAFKA_DIGEST: &str =
    "{OLD_DIGEST}";
"""


def test_extracts_an_annotated_rust_tag_and_its_wrapped_digest_const() -> None:
    (pin,) = cmp.extract_pins("tests/common/mod.rs", RUST)
    assert (pin.ref, pin.tag, pin.line) == ("apache/kafka", "4.2.0", 3)
    assert (pin.digest, pin.digest_line) == (OLD_DIGEST, 7)


def test_extracts_an_annotated_python_tag_with_no_digest() -> None:
    text = '# renovate: datasource=docker depName=clickhouse/clickhouse-server\n_CH_TAG = "26.3"\n'
    (pin,) = cmp.extract_pins("tests/conftest.py", text)
    assert (pin.ref, pin.tag, pin.digest, pin.digest_line) == ("clickhouse/clickhouse-server", "26.3", "", 0)


def test_extracts_annotated_shell_tag_and_digest_lines() -> None:
    text = (
        "# renovate: datasource=docker depName=docker.redpanda.com/redpandadata/redpanda\n"
        'KAFKA_TAG="v26.2.2"\n'
        f'KAFKA_DIGEST="{RP_DIGEST}"\n'
        'KAFKA_IMAGE="${OVERRIDE:-docker.redpanda.com/redpandadata/redpanda:${KAFKA_TAG}@${KAFKA_DIGEST}}"\n'
    )
    (pin,) = cmp.extract_pins("scripts/pgo-workload.sh", text)
    assert (pin.tag, pin.digest, pin.line, pin.digest_line) == ("v26.2.2", RP_DIGEST, 2, 3)


def test_extracts_a_shell_default_with_a_literal_tag_and_digest() -> None:
    text = f'KAFKA_IMAGE="${{PGO_KAFKA_IMAGE:-docker.redpanda.com/redpandadata/redpanda:v26.2.1@{RP_DIGEST}}}"\n'
    (pin,) = cmp.extract_pins("scripts/pgo-workload.sh", text)
    assert (pin.ref, pin.tag, pin.digest, pin.shape) == (
        "docker.redpanda.com/redpandadata/redpanda", "v26.2.1", RP_DIGEST, "shell",
    )


def test_extracts_compose_images_digested_or_not_and_an_env_default() -> None:
    text = (
        "services:\n"
        "  ch:\n"
        f"    image: clickhouse/clickhouse-server:26.3.32.14@{CH_DIGEST}\n"
        "  rp:\n"
        "    # renovate: datasource=docker depName=redpandadata/redpanda\n"
        "    image: redpandadata/redpanda:v26.2.1\n"
        "  k:\n"
        f"    image: apache/kafka:${{KAFKA_VERSION:-4.3.1@{KAFKA_DIGEST}}}\n"
        "  derived:\n"
        "    image: clickhouse/clickhouse-server:${CLICKHOUSE_VERSION}\n"
    )
    pins = cmp.extract_pins("docker-compose.dev.yaml", text)
    assert [(p.ref, p.tag, p.digest, p.line) for p in pins] == [
        ("redpandadata/redpanda", "v26.2.1", "", 6),
        ("clickhouse/clickhouse-server", "26.3.32.14", CH_DIGEST, 3),
        ("apache/kafka", "4.3.1", KAFKA_DIGEST, 8),
    ]


def test_a_tag_at_digest_value_is_split() -> None:
    text = f'# renovate: datasource=docker depName=apache/kafka\nconst KAFKA: &str = "4.3.1@{KAFKA_DIGEST}";\n'
    (pin,) = cmp.extract_pins("x.rs", text)
    assert (pin.tag, pin.digest, pin.digest_line) == ("4.3.1", KAFKA_DIGEST, 2)


def test_an_annotation_over_something_that_is_not_a_tag_yields_nothing() -> None:
    text = '# renovate: datasource=docker depName=apache/kafka\nKAFKA_IMAGE="${OVERRIDE}"\n'
    assert cmp.extract_pins("x.sh", text) == []


def test_a_commented_out_image_line_is_not_an_annotation() -> None:
    text = f'#   image: timberio/vector:0.58.0-alpine@{KAFKA_DIGEST}\nname: "4.2.0"\n'
    assert cmp.extract_pins("config.example.yaml", text) == []


# ---------------------------------------------------------------------------
# compare -- tag and digest against the stack
# ---------------------------------------------------------------------------


def _pin(ref: str, tag: str, digest: str = "") -> cmp.Pin:
    return cmp.Pin("f.rs", 10, ref, tag, digest, 11 if digest else 0)


def test_a_matching_pin_needs_no_edit() -> None:
    assert cmp.compare("m", [_pin("apache/kafka", "4.3.1", KAFKA_DIGEST)], _images()) == []


def test_a_stale_tag_and_digest_are_two_edits() -> None:
    findings = cmp.compare("m", [_pin("apache/kafka", "4.2.0", OLD_DIGEST)], _images())
    assert [(f.field, f.old, f.new, f.line) for f in findings] == [
        ("tag", "4.2.0", "4.3.1", 10),
        ("digest", OLD_DIGEST, KAFKA_DIGEST, 11),
    ]


def test_a_pin_with_no_digest_is_drift() -> None:
    (finding,) = cmp.compare("m", [_pin("apache/kafka", "4.3.1")], _images())
    assert (finding.field, finding.old, finding.new, finding.line) == ("digest", "(none)", KAFKA_DIGEST, 10)


def test_an_alias_matches_and_its_drift_is_still_drift() -> None:
    assert cmp.compare("m", [_pin("redpandadata/redpanda", "v26.2.2", RP_DIGEST)], _images()) == []
    (finding,) = cmp.compare("m", [_pin("docker.io/redpandadata/redpanda", "v26.2.1", RP_DIGEST)], _images())
    assert (finding.field, finding.old, finding.new) == ("tag", "v26.2.1", "v26.2.2")


def test_a_rejected_image_fails_and_names_the_stack_image() -> None:
    (finding,) = cmp.compare("m", [_pin("apache/kafka-native", "4.3.1", KAFKA_DIGEST)], _images())
    assert finding.field == "image"
    assert finding.new == f"apache/kafka:4.3.1@{KAFKA_DIGEST}"


def test_an_image_the_stack_does_not_carry_is_skipped() -> None:
    assert cmp.compare("m", [_pin("openbao/openbao", "2.0.0")], _images()) == []


def test_a_managed_kafka_version_spelling_is_skipped() -> None:
    assert cmp.compare("m", [_pin("apache/kafka", "3.9.x.kraft")], _images()) == []


# ---------------------------------------------------------------------------
# reading members -- at origin/main, never the working tree
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def members(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A --repos directory with mem-a (drifted) and mem-b (clean), each with
    origin/main pointing at its committed tree."""
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "Test")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "test@example.invalid")
    root = tmp_path / "repos"
    trees = {
        "mem-a": {"tests/common/mod.rs": RUST, "README.md": "image: apache/kafka:1.0.0\n"},
        "mem-b": {"docker-compose.yml": f"services:\n  k:\n    image: apache/kafka:4.3.1@{KAFKA_DIGEST}\n"},
    }
    for member, files in trees.items():
        repo = root / member
        for rel, text in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text, encoding="utf-8")
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "init")
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def _scan(root: Path, members: list[str] | None = None, waivers: list[dict] | None = None):
    graph = {"nodes": NODES, "pin_waivers": waivers or []}
    return cmp.scan(members, repos=str(root), fetch=False, graph=graph, versions_text=VERSIONS)


def test_scan_reports_drift_grouped_by_member_with_file_and_line(members: Path) -> None:
    stack, images, reports, stale = _scan(members, ["mem-a", "mem-b"])
    lines = cmp.render(stack, images, reports, stale)
    by_member = {r.member: r for r in reports}
    assert [(f.path, f.line, f.field) for f in by_member["mem-a"].findings] == [
        ("tests/common/mod.rs", 3, "tag"), ("tests/common/mod.rs", 7, "digest"),
    ]
    assert by_member["mem-b"].findings == []
    assert "  tests/common/mod.rs:3  apache/kafka  tag: 4.2.0 -> 4.3.1" in lines
    assert any(line.startswith("mem-b @ main ") and line.endswith("via checkout: ok, 1 pin(s) checked") for line in lines)
    assert cmp.exit_code(reports, stale) == cmp.EXIT_DRIFT


def test_scan_reads_origin_main_not_the_working_tree(members: Path) -> None:
    fixed = RUST.replace('"4.2.0"', '"4.3.1"').replace(OLD_DIGEST, KAFKA_DIGEST)
    (members / "mem-a" / "tests/common/mod.rs").write_text(fixed, encoding="utf-8")
    _stack, _images, reports, _stale = _scan(members, ["mem-a"])
    assert len(reports[0].edits) == 2  # the fix is not on main yet


def test_scan_fetches_before_it_reads(members: Path, tmp_path: Path) -> None:
    remote = tmp_path / "mem-a.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(members / "mem-a"), str(remote)], check=True)
    _git(members / "mem-a", "remote", "add", "origin", str(remote))
    _git(members / "mem-a", "fetch", "-q", "origin")
    pusher = tmp_path / "pusher"
    subprocess.run(["git", "clone", "-q", str(remote), str(pusher)], check=True)
    fixed = RUST.replace('"4.2.0"', '"4.3.1"').replace(OLD_DIGEST, KAFKA_DIGEST)
    (pusher / "tests/common/mod.rs").write_text(fixed, encoding="utf-8")
    _git(pusher, "commit", "-q", "-am", "fix: pin the stack's kafka")
    _git(pusher, "push", "-q", "origin", "main")

    graph = {"nodes": NODES, "pin_waivers": []}
    stale_read = cmp.scan(["mem-a"], repos=str(members), fetch=False, graph=graph, versions_text=VERSIONS)
    fresh_read = cmp.scan(["mem-a"], repos=str(members), fetch=True, graph=graph, versions_text=VERSIONS)
    assert len(stale_read[2][0].edits) == 2
    assert fresh_read[2][0].edits == []


def test_a_member_that_is_not_on_disk_is_unread_and_not_clean(members: Path) -> None:
    graph = {"nodes": {**NODES, "mem-missing": {}}, "pin_waivers": []}
    _stack, _images, reports, stale = cmp.scan(
        ["mem-b", "mem-missing"], repos=str(members), fetch=False, graph=graph, versions_text=VERSIONS,
    )
    assert "is not a git checkout" in reports[1].unread
    assert cmp.exit_code(reports, stale) == cmp.EXIT_UNREAD


API_SLUG = "hyperi-io/mem-api"
API_SHA = "c" * 40


def _serve_gh(monkeypatch: pytest.MonkeyPatch, files: dict[str, str], *, truncated: bool = False) -> list[tuple]:
    """Answer `gh api` for API_SLUG from `files`, the way GitHub would, recording each call."""
    calls: list[tuple] = []
    contents = f"repos/{API_SLUG}/contents/"

    def gh(*args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        endpoint = args[0]
        if endpoint == f"repos/{API_SLUG}/commits/main":
            return subprocess.CompletedProcess(args, 0, stdout=f"{API_SHA}\n", stderr="")
        if endpoint == f"repos/{API_SLUG}/git/trees/{API_SHA}?recursive=1":
            tree = [{"path": p, "type": "blob"} for p in files] + [{"path": "tests", "type": "tree"}]
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"tree": tree, "truncated": truncated}), stderr="")
        if endpoint.startswith(contents):
            path = unquote(endpoint[len(contents) :].split("?", 1)[0])
            return subprocess.CompletedProcess(args, 0, stdout=files[path], stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="gh: Not Found (HTTP 404)")

    monkeypatch.setattr(cmp, "_gh", gh)
    return calls


API_NODES = {**NODES, "mem-api": {"classification": "product", "repo": API_SLUG},
             "mem-a": {"classification": "product", "repo": API_SLUG}}


def _api_scan(root: Path, members: list[str], *, api: bool = False):
    graph = {"nodes": API_NODES, "pin_waivers": []}
    return cmp.scan(members, repos=str(root), fetch=False, api=api, graph=graph, versions_text=VERSIONS)


def test_a_member_with_no_checkout_is_read_through_gh_api(members: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _serve_gh(monkeypatch, {
        "tests/common/mod.rs": RUST,
        "web/pnpm-lock.yaml": "image: apache/kafka:1.0.0\n",
        "docs/README.md": "image: apache/kafka:1.0.0\n",
    })
    stack, images, reports, stale = _api_scan(members, ["mem-api"])
    (report,) = reports
    assert (report.source, report.sha) == ("gh api", API_SHA[:9])
    assert [(f.path, f.line, f.field) for f in report.findings] == [
        ("tests/common/mod.rs", 3, "tag"), ("tests/common/mod.rs", 7, "digest"),
    ]
    fetched = [c[0] for c in calls if c[0].startswith(f"repos/{API_SLUG}/contents/")]
    assert fetched == [f"repos/{API_SLUG}/contents/tests/common/mod.rs?ref={API_SHA}"]  # no lockfile, no markdown
    assert f"mem-api @ main {API_SHA[:9]} via gh api: 2 edit(s), 1 pin(s) checked" in cmp.render(stack, images, reports, stale)


def test_api_forces_gh_api_over_a_local_checkout(members: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clean = RUST.replace('"4.2.0"', '"4.3.1"').replace(OLD_DIGEST, KAFKA_DIGEST)
    calls = _serve_gh(monkeypatch, {"tests/common/mod.rs": clean})
    _stack, _images, reports, _stale = _api_scan(members, ["mem-a"], api=True)
    assert reports[0].source == "gh api"
    assert reports[0].findings == []  # main through the API is clean; the local origin/main is not
    assert calls


def test_a_truncated_api_tree_is_unread_not_partly_read(members: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_gh(monkeypatch, {"tests/common/mod.rs": RUST}, truncated=True)
    _stack, _images, reports, stale = _api_scan(members, ["mem-api"])
    assert "is not a git checkout" in reports[0].unread
    assert "listed hyperi-io/mem-api truncated" in reports[0].unread
    assert cmp.exit_code(reports, stale) == cmp.EXIT_UNREAD


def test_a_member_neither_route_can_read_names_both_reasons(members: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_gh(monkeypatch, {})
    graph = {"nodes": {"mem-gone": {"repo": "hyperi-io/mem-gone"}}, "pin_waivers": []}
    _stack, _images, reports, _stale = cmp.scan(
        ["mem-gone"], repos=str(members), fetch=False, graph=graph, versions_text=VERSIONS,
    )
    assert "mem-gone is not a git checkout" in reports[0].unread
    assert "gh api cannot read hyperi-io/mem-gone main: gh: Not Found (HTTP 404)" in reports[0].unread


def test_this_repo_and_a_fork_are_exempt_and_never_read(members: Path) -> None:
    _stack, _images, reports, _stale = _scan(members, ["dfe-infra", "mem-fork"])
    assert [r.exempt != "" for r in reports] == [True, True]
    assert [r.sha for r in reports] == ["", ""]


def test_an_unknown_member_name_is_refused(members: Path) -> None:
    with pytest.raises(cmp.FleetError, match="not a suite member: nope"):
        _scan(members, ["nope"])


# ---------------------------------------------------------------------------
# waivers -- excuse a file and an image, and must excuse something
# ---------------------------------------------------------------------------

WAIVER = {"member": "mem-a", "file": "tests/common/mod.rs", "image": "apache/kafka", "reason": "tests the old line"}


def test_a_waiver_excuses_its_file_and_image(members: Path) -> None:
    _stack, _images, reports, stale = _scan(members, ["mem-a"], [WAIVER])
    assert reports[0].edits == []
    assert {f.waived_by for f in reports[0].findings} == {"tests the old line"}
    assert stale == []
    assert cmp.exit_code(reports, stale) == cmp.EXIT_CLEAN


def test_a_waiver_that_excuses_nothing_fails(members: Path) -> None:
    waiver = {**WAIVER, "member": "mem-b", "file": "docker-compose.yml"}
    _stack, _images, reports, stale = _scan(members, ["mem-b"], [waiver])
    assert [(f.member, f.field) for f in stale] == [("mem-b", "waiver")]
    assert cmp.exit_code(reports, stale) == cmp.EXIT_DRIFT


def test_a_waiver_for_a_member_this_run_did_not_read_is_not_judged(members: Path) -> None:
    _stack, _images, _reports, stale = _scan(members, ["mem-b"], [WAIVER])
    assert stale == []


def test_suite_graph_refuses_a_waiver_carrying_a_version() -> None:
    graph = {"nodes": NODES, "pin_waivers": [{**WAIVER, "reason": "holds 4.2.0 until the soak"}]}
    problems = suite_graph._waiver_problems(graph["pin_waivers"], NODES)
    assert problems == [
        "pin_waivers #1 (mem-a tests/common/mod.rs): `reason` carries a version literal "
        "('holds 4.2.0 until the soak'); name the image, not a version"
    ]


def test_suite_graph_refuses_an_incomplete_waiver_or_an_unknown_member() -> None:
    problems = suite_graph._waiver_problems([{"member": "ghost", "file": "f"}], NODES)
    assert "pin_waivers #1 (ghost f): missing `image`" in problems
    assert "pin_waivers #1 (ghost f): missing `reason`" in problems
    assert "pin_waivers #1 (ghost f): member ghost is not a node" in problems


def test_the_real_suite_yaml_carries_a_valid_waiver_list() -> None:
    graph = suite_graph.load()
    assert isinstance(graph.get("pin_waivers"), list)
    assert suite_graph._waiver_problems(graph["pin_waivers"], graph["nodes"]) == []


# ---------------------------------------------------------------------------
# dfe-suite pins -- the same scan behind the suite CLI
# ---------------------------------------------------------------------------

_CLI = SCRIPTS / "dfe-suite"
_SPEC = importlib.util.spec_from_file_location("dfe_suite_cli_pins", _CLI, loader=SourceFileLoader("dfe_suite_cli_pins", str(_CLI)))
ds = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ds)


def _fixture_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI's scan at the fixture graph and stack; the flags pass through."""
    real_scan = cmp.scan

    def scan(members_arg, **kwargs):
        return real_scan(members_arg, graph={"nodes": NODES, "pin_waivers": []}, versions_text=VERSIONS, **kwargs)

    monkeypatch.setattr(cmp, "scan", scan)


def test_dfe_suite_pins_runs_the_scan_with_its_flags(members: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _fixture_scan(monkeypatch)
    code = ds.main(["pins", "mem-a", "--repos", str(members), "--no-fetch"])
    out = capsys.readouterr().out
    assert code == cmp.EXIT_DRIFT
    assert "  tests/common/mod.rs:3  apache/kafka  tag: 4.2.0 -> 4.3.1" in out
    assert "member-pins: 2 edit(s), 0 member(s) not read -- exit 1" in out


def test_dfe_suite_signals_counts_pin_drift(members: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixture members' origin/main is a local ref with no remote to fetch from.
    real_scan = cmp.scan

    def scan(members_arg, **kwargs):
        return real_scan(
            members_arg, graph={"nodes": NODES, "pin_waivers": []}, versions_text=VERSIONS,
            repos=kwargs["repos"], fetch=False,
        )

    monkeypatch.setattr(cmp, "scan", scan)
    lines, count, read = ds._pin_signals("mem-a", str(members))
    assert (count, read) == (2, True)
    assert lines[0].strip() == "tests/common/mod.rs:3  apache/kafka  tag: 4.2.0 -> 4.3.1"
    assert ds._pin_signals("dfe-infra", str(members))[1:] == (0, True)
