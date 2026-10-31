#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_docs_aggregate.py
#  Purpose:      Cover docs-site/scripts/aggregate.py: which stacks and refs it
#                builds, how it rewrites links, what it reports as strays, and real
#                anonymous sparse fetches from local git repositories.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the docs site's aggregate step.

The fetch and assemble tests build real git repositories under tmp_path and fetch
them over file://, so they need git and no network.

    python3 -m pytest scripts/tests/test_docs_aggregate.py -q
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "docs-site" / "scripts" / "aggregate.py"

spec = importlib.util.spec_from_file_location("docs_aggregate", SCRIPT)
aggregate = importlib.util.module_from_spec(spec)
sys.modules["docs_aggregate"] = aggregate
spec.loader.exec_module(aggregate)

Build = aggregate.Build
BuildResult = aggregate.BuildResult
Finding = aggregate.Finding
Member = aggregate.Member
Rule = aggregate.Rule

REPO = "hyperi-io/dfe-infra"
GIT_IDENTITY = ("-c", "user.name=Test", "-c", "user.email=test@example.com")


def git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        env=aggregate.anonymous_git_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def make_repo(root: Path, repo: str, files: dict[str, str], tag: str) -> str:
    """A repository at root/<repo>.git holding `files`, under an annotated tag. Returns the commit."""
    path = root / f"{repo}.git"
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", str(path))
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git("-C", str(path), "add", "-A")
    git("-C", str(path), *GIT_IDENTITY, "commit", "-q", "-m", "docs")
    git("-C", str(path), *GIT_IDENTITY, "tag", "-a", tag, "-m", tag)
    git("-C", str(path), "config", "uploadpack.allowFilter", "true")
    git("-C", str(path), "config", "uploadpack.allowAnySHA1InWant", "true")
    return git("-C", str(path), "rev-parse", "HEAD")


def write_tree(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def checkout_files(path: Path) -> list[str]:
    return sorted(
        found.relative_to(path).as_posix()
        for found in path.rglob("*")
        if found.is_file() and ".git" not in found.relative_to(path).parts
    )


@pytest.fixture
def remotes(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "remotes"
    commit = make_repo(
        root,
        "o/app",
        {
            "README.md": "# App\n",
            "docs/README.md": "# App docs\n",
            "docs/guide.md": "# Guide\n\n[readme](../README.md)\n",
            "openapi-spec/openapi.json": "{}\n",
            "src/main.py": "print('not docs')\n",
        },
        "v1.0.0",
    )
    return root, commit


# --- which stacks, members and refs -------------------------------------------


def test_only_release_maturity_x_y_z_stacks_count_newest_first() -> None:
    tree = {
        "stacks": {
            "2.2.0-rc.1": {"maturity": "rc", "support": "standard"},
            "2.2.0": {"maturity": "release", "support": "standard"},
            "2.10.0": {"maturity": "release", "support": "eol"},
            "2.9.1": {"maturity": "release", "support": "lts"},
            "2.2.1": {"maturity": "rc", "support": "withdrawn"},
            "2.3.0": {"maturity": "release", "support": "withdrawn"},
            "2.4.0-beta.1": {"maturity": "release", "support": "standard"},
        }
    }
    assert aggregate.released_stacks(tree) == ["2.10.0", "2.9.1", "2.2.0"]


def test_a_versions_file_without_stacks_releases_nothing() -> None:
    assert aggregate.released_stacks({}) == []
    assert aggregate.released_stacks({"stacks": "not a map"}) == []


def test_the_real_versions_file_releases_2_2_0_and_no_candidate() -> None:
    text = (REPO_ROOT / "versions.yaml").read_text(encoding="utf-8")
    released = aggregate.released_stacks(aggregate.yaml_subset.parse(text))
    assert "2.2.0" in released
    assert [name for name in released if "-" in name] == []


def test_members_follow_the_lanes_and_the_first_lane_wins() -> None:
    graph = {
        "nodes": {name: {"repo": f"o/{name}", "classification": "product"} for name in "abcd"},
        "lanes": [
            {"name": "one", "members": ["a", "b"]},
            {"name": "two", "members": ["c", "a"]},
            {"name": "three", "members": ["d"]},
        ],
    }
    members = aggregate.ordered_members(graph)
    assert [(m.name, m.repo, m.lane, m.position) for m in members] == [
        ("a", "o/a", "one", 10),
        ("b", "o/b", "one", 11),
        ("c", "o/c", "two", 12),
        ("d", "o/d", "three", 13),
    ]


def test_the_real_suite_lists_every_member_once_through_dfe_stack() -> None:
    graph = aggregate.load_suite()
    names = [member.name for member in aggregate.ordered_members(graph)]
    assert len(names) == len(set(names))
    assert set(names) == set(graph["nodes"])


def test_a_stack_pins_apps_and_content_and_its_own_tag_for_dfe_infra() -> None:
    members = [
        Member("dfe-infra", "o/dfe-infra", "toolchain", 10),
        Member("dfe-engine", "o/dfe-engine", "consumers", 11),
        Member("dfe-docker", "o/dfe-docker", "toolchain", 12),
        Member("dfe-ui", "o/dfe-ui", "consumers", 13),
        Member("scalo-rs", "o/scalo-rs", "libraries", 14),
    ]
    block = {"apps": {"dfe-engine": "v1.0.0", "dfe-ui": ""}, "content": {"dfe-docker": "abc123"}}
    assert aggregate.stack_refs("2.2.0", block, members) == {
        "dfe-infra": "2.2.0",
        "dfe-engine": "v1.0.0",
        "dfe-docker": "abc123",
    }


def test_the_real_2_2_0_stack_pins_no_library() -> None:
    text = (REPO_ROOT / "versions.yaml").read_text(encoding="utf-8")
    tree = aggregate.yaml_subset.parse(text)
    members = aggregate.ordered_members(aggregate.load_suite())
    refs = aggregate.stack_refs("2.2.0", tree["stacks"]["2.2.0"], members)
    assert refs["dfe-infra"] == "2.2.0"
    assert refs["dfe-engine"].startswith("v")
    libraries = {m.name for m in members if m.lane == "libraries"} - {"dfe-schemas"}
    assert libraries
    assert not libraries & set(refs)


def test_plan_builds_lists_released_stacks_then_the_preview() -> None:
    tree = {
        "stacks": {
            "2.2.0": {"maturity": "release", "support": "standard", "apps": {"x": "v1"}},
            "2.3.0-rc.1": {"maturity": "rc", "support": "standard"},
        }
    }
    members = [Member("x", "o/x", "consumers", 10)]
    builds = aggregate.plan_builds(tree, members, [], True)
    assert [(b.name, b.base_url, b.listed, dict(b.refs)) for b in builds] == [
        ("2.2.0", "/v/2.2.0/", True, {"x": "v1"}),
        ("next", "/next/", False, {"x": "HEAD"}),
    ]


def test_plan_builds_refuses_an_unreleased_stack_and_an_empty_plan() -> None:
    tree = {"stacks": {"2.3.0-rc.1": {"maturity": "rc", "support": "standard"}}}
    members = [Member("x", "o/x", "consumers", 10)]
    with pytest.raises(SystemExit, match=r"not a released stack: 2\.3\.0-rc\.1"):
        aggregate.plan_builds(tree, members, ["2.3.0-rc.1"], False)
    with pytest.raises(SystemExit, match="no released stack"):
        aggregate.plan_builds(tree, members, [], False)


@pytest.mark.parametrize(
    ("stacks", "expected"),
    [
        ([], ["2.2.1"]),
        (["2.2.1"], ["2.2.1"]),
        (["2.2.0"], r"publish\.yaml drops 2\.2\.0"),
        (["2.2.0", "2.2.1"], r"publish\.yaml drops 2\.2\.0"),
    ],
)
def test_a_dropped_stack_is_never_built(stacks: list[str], expected: list[str] | str) -> None:
    release = {"maturity": "release", "support": "standard", "apps": {"x": "v1"}}
    tree = {"stacks": {"2.2.0": release, "2.2.1": release}}
    members = [Member("x", "o/x", "consumers", 10)]
    dropped = frozenset({"2.2.0"})
    if isinstance(expected, str):
        with pytest.raises(SystemExit, match=expected):
            aggregate.plan_builds(tree, members, stacks, False, dropped)
        return
    builds = aggregate.plan_builds(tree, members, stacks, False, dropped)
    assert [build.name for build in builds] == expected


# --- publish.yaml ------------------------------------------------------------------


def test_the_real_publish_file_names_real_members_and_keeps_the_newest_release() -> None:
    members = aggregate.ordered_members(aggregate.load_suite())
    text = aggregate.PUBLISH_FILE.read_text(encoding="utf-8")
    policy = aggregate.load_policy(
        aggregate.yaml_subset.parse(text), [member.name for member in members]
    )
    versions = aggregate.yaml_subset.parse((REPO_ROOT / "versions.yaml").read_text())
    builds = aggregate.plan_builds(versions, members, [], False, policy.dropped)
    assert "2.2.0" in policy.dropped
    assert builds[0].name == versions["latest"]
    assert policy.mounts == {"dfe-hyperdx": "dfe-ui"}
    assert "docs/testing-cycle.md" in policy.internal_to("dfe-infra")


@pytest.mark.parametrize(
    ("tree", "expected"),
    [
        ({}, aggregate.Policy()),
        (
            {
                "dropped_versions": {"1.0.0": "old"},
                "mount_under": {"b": "a"},
                "internal": {"a": {"x.md": "why", "old/": "why"}},
            },
            aggregate.Policy(
                frozenset({"1.0.0"}), {"b": "a"}, {"a": frozenset({"docs/x.md", "docs/old/"})}
            ),
        ),
        (
            {"internal": {"c": {"all-pages": "a library"}}},
            aggregate.Policy(internal={"c": frozenset({"docs/"})}),
        ),
        ({"mount_under": "a"}, "`mount_under` is not a map"),
        ({"mount_under": {"b": "z"}}, "not both members"),
        ({"mount_under": {"a": "a"}}, "a parent must mount at the top level"),
        ({"mount_under": {"b": "a", "c": "b"}}, "a parent must mount at the top level"),
        ({"internal": {"z": {"x.md": "why"}}}, "internal z: not a member"),
        ({"internal": {"a": "x.md"}}, "internal a: not a member with a map"),
        ({"internal": {"a": {"../README.md": "why"}}}, r"\.\./README\.md is not a path"),
        ({"internal": {"a": {"/etc/x": "why"}}}, "/etc/x is not a path"),
    ],
)
def test_load_policy_reads_a_valid_file_and_names_every_problem(
    tree: dict, expected: object
) -> None:
    if isinstance(expected, str):
        with pytest.raises(SystemExit, match=expected):
            aggregate.load_policy(tree, ["a", "b", "c"])
        return
    assert aggregate.load_policy(tree, ["a", "b", "c"]) == expected


def test_the_real_journeys_file_places_every_member_that_publishes() -> None:
    members = [member.name for member in aggregate.ordered_members(aggregate.load_suite())]
    policy = aggregate.load_policy(
        aggregate.yaml_subset.parse(aggregate.PUBLISH_FILE.read_text(encoding="utf-8")), members
    )
    journeys = aggregate.load_journeys(
        aggregate.yaml_subset.parse(aggregate.JOURNEYS_FILE.read_text(encoding="utf-8")), members
    )
    assert [label for label, _ in journeys.order] == ["Evaluate", "Deploy", "Operate", "Reference"]
    whole = {name for name in members if policy.internal_to(name) == frozenset({"docs/"})}
    assert whole == {
        "scalo-rs",
        "scalo-py",
        "factbook-rs",
        "logreducer",
        "vector-vrl",
        "dfe-clickhouse-rs",
    }
    assert set(members) - whole <= set(journeys.placement)
    assert aggregate.ROOT_PAGES in journeys.placement


SIDEBAR = {"J": {"S": "", "T": ""}, "K": {"U": ""}}


@pytest.mark.parametrize(
    ("placement", "expected"),
    [
        (
            {"a": {"default": "J / S", "x/": "K / U", "x/y.md": "J / T", "top.md": ""}},
            {"a": {"default": "J / S", "x/": "K / U", "x/y.md": "J / T", "top.md": ""}},
        ),
        ({"z": {"default": "J / S"}}, "placement z: not a member"),
        ({"a": "J / S"}, "placement a: not a member with a map"),
        ({"a": {"x.md": "J / S"}}, "placement a: no `default`"),
        ({"a": {"default": "J / U"}}, "'J / U' is no journey and section"),
        ({"a": {"default": "J/S"}}, "'J/S' is no journey and section"),
    ],
)
def test_load_journeys_reads_a_valid_file_and_names_every_problem(
    placement: dict, expected: dict | str
) -> None:
    tree = {"sidebar": SIDEBAR, "placement": placement}
    if isinstance(expected, str):
        with pytest.raises(SystemExit, match=expected):
            aggregate.load_journeys(tree, ["a", "b"])
        return
    journeys = aggregate.load_journeys(tree, ["a", "b"])
    assert journeys.order == (("J", ("S", "T")), ("K", ("U",)))
    assert journeys.placement == expected
    assert journeys.manifest() == [
        {"label": "J", "sections": ["S", "T"]},
        {"label": "K", "sections": ["U"]},
    ]


@pytest.mark.parametrize(
    ("member", "prefix", "expected"),
    [
        (
            "a",
            "dfe-ui/a",
            {
                "dfe-ui/a/": "J / S",
                "dfe-ui/a/x/": "K / U",
                "dfe-ui/a/x/y": "J / T",
                "dfe-ui/a/top": "",
            },
        ),
        ("content-root", "", {"": "J / S", "README": ""}),
        ("b", "b", "b publishes and has no placement"),
    ],
)
def test_placements_are_keyed_by_doc_id_under_the_member_mount(
    member: str, prefix: str, expected: dict | str
) -> None:
    journeys = aggregate.Journeys(
        (("J", ("S", "T")), ("K", ("U",))),
        {
            "a": {"default": "J / S", "x/": "K / U", "x/y.md": "J / T", "top.md": ""},
            "content-root": {"default": "J / S", "README.md": ""},
        },
    )
    if isinstance(expected, str):
        with pytest.raises(SystemExit, match=expected):
            journeys.keys_for(member, prefix)
        return
    assert journeys.keys_for(member, prefix) == expected


# --- links ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("https://example.com/x", "https://example.com/x"),
        ("mailto:sales@example.com", "mailto:sales@example.com"),
        ("#anchor", "#anchor"),
        ("aws.md", "aws.md"),
        ("../architecture.md#seams", "../architecture.md#seams"),
        ("/docs/architecture.md", "../architecture.md"),
        ("/docs/kafka/", "../kafka/"),
        ("../../README.md", f"https://github.com/{REPO}/blob/2.2.0/README.md"),
        (
            "../../scripts/dfe-stack#L10",
            f"https://github.com/{REPO}/blob/2.2.0/scripts/dfe-stack#L10",
        ),
        ("../../helm/charts/", f"https://github.com/{REPO}/tree/2.2.0/helm/charts"),
        ("../../", f"https://github.com/{REPO}/tree/2.2.0"),
        ("/versions.yaml", f"https://github.com/{REPO}/blob/2.2.0/versions.yaml"),
        (
            "../../assets/flow.PNG",
            f"https://raw.githubusercontent.com/{REPO}/2.2.0/assets/flow.PNG",
        ),
        ("../plans/next.md", f"https://github.com/{REPO}/blob/2.2.0/docs/plans/next.md"),
        ("<../../README.md>", f"<https://github.com/{REPO}/blob/2.2.0/README.md>"),
        ("../../a.md?plain=1#x", f"https://github.com/{REPO}/blob/2.2.0/a.md?plain=1#x"),
    ],
)
def test_a_link_that_leaves_docs_points_at_github_at_the_ref(target: str, expected: str) -> None:
    assert aggregate.relink(target, doc_dir="docs/deployment", repo=REPO, ref="2.2.0") == (
        expected,
        False,
    )


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (
            "../testing-cycle.md#msk",
            f"https://github.com/{REPO}/blob/2.2.1/docs/testing-cycle.md#msk",
        ),
        ("../archive/old.md", f"https://github.com/{REPO}/blob/2.2.1/docs/archive/old.md"),
        ("../archive/", f"https://github.com/{REPO}/tree/2.2.1/docs/archive"),
        (
            "../archive/flow.png",
            f"https://raw.githubusercontent.com/{REPO}/2.2.1/docs/archive/flow.png",
        ),
        ("/docs/testing-cycle.md", f"https://github.com/{REPO}/blob/2.2.1/docs/testing-cycle.md"),
        ("../archived.md", "../archived.md"),
        ("aws.md", "aws.md"),
        ("../pipeline/", f"https://github.com/{REPO}/tree/2.2.1/docs/pipeline"),
        ("../pipeline/routing.md", "../pipeline/routing.md"),
        ("../pipeline/README.md", f"https://github.com/{REPO}/blob/2.2.1/docs/pipeline/README.md"),
    ],
)
def test_a_link_to_an_internal_page_points_at_github(target: str, expected: str) -> None:
    internal = frozenset({"docs/testing-cycle.md", "docs/archive/", "docs/pipeline/README.md"})
    relinked = aggregate.relink(
        target, doc_dir="docs/deployment", repo=REPO, ref="2.2.1", internal=internal
    )
    assert relinked == (expected, False)


def test_a_link_out_of_the_repository_is_left_and_flagged() -> None:
    assert aggregate.relink(
        "../../../other/x.md", doc_dir="docs/deployment", repo=REPO, ref="2.2.0"
    ) == ("../../../other/x.md", True)


def test_links_inside_code_stay_as_written() -> None:
    text = (
        "See [the chart](../../helm/charts/x/values.yaml) and [aws](aws.md).\n"
        "Code `[not](../../README.md)` stays.\n"
        "```bash\n"
        "echo [x](../../README.md)\n"
        "```\n"
        "~~~~\n"
        "[x](../../README.md)\n"
        "~~~\n"
        "[still code](../../README.md)\n"
        "~~~~\n"
        '<img src="../../logo.svg"> <a href="../../LICENSE">licence</a>\n'
        "[ref]: ../../CONTRIBUTING.md\n"
        "[out](../../../elsewhere.md)\n"
    )
    new, escaped = aggregate.rewrite_links(
        text, doc_path="docs/deployment/page.md", repo=REPO, ref="v1"
    )
    lines = new.splitlines()
    blob = f"https://github.com/{REPO}/blob/v1"
    assert lines[0] == f"See [the chart]({blob}/helm/charts/x/values.yaml) and [aws](aws.md)."
    assert lines[1] == "Code `[not](../../README.md)` stays."
    assert lines[3] == "echo [x](../../README.md)"
    assert lines[6] == "[x](../../README.md)"
    assert lines[8] == "[still code](../../README.md)"
    raw = f"https://raw.githubusercontent.com/{REPO}/v1"
    assert lines[10] == f'<img src="{raw}/logo.svg"> <a href="{blob}/LICENSE">licence</a>'
    assert lines[11] == f"[ref]: {blob}/CONTRIBUTING.md"
    assert lines[12] == "[out](../../../elsewhere.md)"
    assert escaped == ["../../../elsewhere.md"]


def test_mdx_specials_become_references_in_prose_and_stay_in_code() -> None:
    text = (
        "Poll GET /syncs/{task_id} for <name>, in <1ms, `{kept}` in code, \\{already.\n"
        "```json\n"
        '{"id": 1}\n'
        "```\n"
    )
    assert aggregate.mdx_safe(text) == (
        "Poll GET /syncs/&#123;task_id&#125; for &lt;name>, in <1ms, `{kept}` in code,"
        " \\{already.\n"
        "```json\n"
        '{"id": 1}\n'
        "```\n"
    )


def test_only_spec_descriptions_are_escaped() -> None:
    spec = {
        "info": {"title": "API {v}", "description": "See {x}."},
        "paths": {
            "/items/{id}": {
                "get": {
                    "summary": "Get {id}",
                    "description": "Returns {id}.",
                    "parameters": [{"name": "id", "description": "The {id}."}],
                }
            }
        },
        "components": {"schemas": {"Item": {"properties": {"description": {"type": "string"}}}}},
    }
    safe = aggregate.mdx_safe_spec(spec)
    operation = safe["paths"]["/items/{id}"]["get"]
    assert safe["info"] == {"title": "API {v}", "description": "See &#123;x&#125;."}
    assert operation["summary"] == "Get {id}"
    assert operation["description"] == "Returns &#123;id&#125;."
    assert operation["parameters"][0]["description"] == "The &#123;id&#125;."
    assert safe["components"] == spec["components"]


def test_leading_header_comments_go_so_the_h1_titles_the_page() -> None:
    header = (
        "<!--\nProject: x\nFile: docs/a.md\n-->\n\n<!-- second -->\n# Title\n\nBody <!-- kept -->\n"
    )
    dropped = aggregate.drop_leading_comments(header)
    assert dropped == "\n" * 6 + "# Title\n\nBody <!-- kept -->\n"
    assert dropped.splitlines().index("# Title") == header.splitlines().index("# Title")
    front = "---\nsidebar_position: 2\n---\n<!-- header -->\n# Title\n"
    assert aggregate.drop_leading_comments(front) == "---\nsidebar_position: 2\n---\n\n# Title\n"


def test_a_page_without_a_leading_comment_is_unchanged() -> None:
    for text in (
        "# Title\n<!-- later -->\n",
        "Intro\n\n<!-- later -->\n",
        "<!-- never closed\n# T\n",
    ):
        assert aggregate.drop_leading_comments(text) == text


# --- strays and copying ----------------------------------------------------------


def test_every_departure_from_the_standard_is_reported(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    write_tree(
        docs,
        {
            "UPPER-CASE.md": "#",
            "snake_name.md": "#",
            "DESIGN.md": "#",
            "notes.txt": "x",
            "good-page.md": "#",
            "image.png": "x",
            "sub/index.md": "#",
            "deep/page.md": "#",
            "ok/README.md": "#",
            "ok/fine.md": "#",
            "plans/wip.md": "#",
            ".hidden/x.md": "#",
        },
    )
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    (docs / "linked.md").symlink_to(tmp_path / "secret.txt")
    found = {(f.path, f.rule) for f in aggregate.find_strays(docs, "m")}
    assert found == {
        ("docs/", Rule.LANDING),
        ("docs/UPPER-CASE.md", Rule.CASE),
        ("docs/snake_name.md", Rule.UNDERSCORE),
        ("docs/DESIGN.md", Rule.BANNED),
        ("docs/notes.txt", Rule.NOT_MARKDOWN),
        ("docs/sub/index.md", Rule.INDEX_LANDING),
        ("docs/deep/", Rule.LANDING),
        ("docs/plans/", Rule.WORKING),
        ("docs/linked.md", Rule.SYMLINK),
    }


def test_a_tree_that_follows_the_standard_reports_nothing(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    write_tree(
        docs,
        {
            "README.md": "#",
            "getting-started.md": "#",
            "guide/README.md": "#",
            "guide/deploy.md": "#",
            "guide/flow.svg": "<svg/>",
        },
    )
    assert aggregate.find_strays(docs, "m") == []


def test_copying_skips_links_working_dirs_and_dotfiles(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    write_tree(
        docs,
        {
            "README.md": "# M\n\n[main](../src/main.py)\n",
            "img/flow.png": "png",
            "plans/wip.md": "# wip\n",
            ".private/x.md": "# x\n",
        },
    )
    outside = tmp_path / "outside"
    write_tree(outside, {"x.md": "# outside\n", "secret.txt": "secret"})
    (docs / "leak.md").symlink_to(outside / "secret.txt")
    (docs / "linked-dir").symlink_to(outside, target_is_directory=True)
    dest = tmp_path / "out"
    member = Member("m", "o/m", "consumers", 10)
    findings = aggregate.copy_docs(docs, dest, member=member, ref="v2")
    assert checkout_files(dest) == ["README.md", "img/flow.png"]
    assert "(https://github.com/o/m/blob/v2/src/main.py)" in (dest / "README.md").read_text()
    assert findings == []


def test_copying_leaves_internal_pages_and_directories_out(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    write_tree(
        docs,
        {
            "README.md": "# M\n\n[cycle](testing-cycle.md) [old](archive/x.md)\n",
            "testing-cycle.md": "# cycle\n",
            "archive/x.md": "# x\n",
            "archive/flow.png": "png",
            "archived.md": "# kept\n",
        },
    )
    dest = tmp_path / "out"
    internal = frozenset({"docs/testing-cycle.md", "docs/archive/"})
    member = Member("m", "o/m", "consumers", 10)
    aggregate.copy_docs(docs, dest, member=member, ref="v2", internal=internal)
    assert checkout_files(dest) == ["README.md", "archived.md"]
    text = (dest / "README.md").read_text()
    assert "(https://github.com/o/m/blob/v2/docs/testing-cycle.md)" in text
    assert "(https://github.com/o/m/blob/v2/docs/archive/x.md)" in text


@pytest.mark.parametrize(
    ("members", "expected"),
    [({"member"}, ["README.md", "quick/a.md"]), ({"quick"}, "collide with members: quick")],
)
def test_the_root_pages_leave_editor_files_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, members: set[str], expected: list[str] | str
) -> None:
    source = tmp_path / "content-root"
    write_tree(
        source,
        {"README.md": "# O\n", "VOICE.md": "# V\n", "quick/a.md": "# A\n", "quick/VOICE.md": "#"},
    )
    monkeypatch.setattr(aggregate, "CONTENT_ROOT", source)
    root = tmp_path / "build"
    root.mkdir()
    if isinstance(expected, str):
        with pytest.raises(SystemExit, match=expected):
            aggregate.copy_root(root, members)
        return
    aggregate.copy_root(root, members)
    assert checkout_files(root) == expected


def test_the_preview_copies_saved_pages_and_removes_deleted_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview_spec = importlib.util.spec_from_file_location(
        "docs_preview", SCRIPT.with_name("preview.py")
    )
    preview = importlib.util.module_from_spec(preview_spec)
    preview_spec.loader.exec_module(preview)
    source = tmp_path / "content-root"
    write_tree(source, {"README.md": "# O\n", "VOICE.md": "# V\n", "quick/a.md": "# A\n"})
    monkeypatch.setattr(preview.aggregate, "CONTENT_ROOT", source)
    build = tmp_path / "build"
    seen = preview.sync(build, {})
    assert checkout_files(build) == ["README.md", "quick/a.md"]
    (source / "quick" / "a.md").write_text("# A2\n", encoding="utf-8")
    os.utime(source / "quick" / "a.md", (seen["quick/a.md"] + 5, seen["quick/a.md"] + 5))
    (source / "README.md").unlink()
    seen = preview.sync(build, seen)
    assert checkout_files(build) == ["quick/a.md"]
    assert (build / "quick" / "a.md").read_text() == "# A2\n"
    assert set(seen) == {"quick/a.md"}


def test_prepare_out_refuses_a_directory_it_did_not_write(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match="refusing to clear"):
        aggregate.prepare_out(tmp_path)
    assert (tmp_path / "keep.txt").is_file()


def test_prepare_out_clears_its_own_output(tmp_path: Path) -> None:
    out = tmp_path / "content"
    write_tree(out, {"site.json": "{}", "old/page.md": "#"})
    aggregate.prepare_out(out)
    assert list(out.iterdir()) == []


# --- fetching ---------------------------------------------------------------------


def test_a_tag_fetch_checks_out_docs_and_the_spec_at_the_tagged_commit(
    remotes: tuple[Path, str], tmp_path: Path
) -> None:
    root, commit = remotes
    checkout = aggregate.Fetcher(tmp_path / "work", f"file://{root}").fetch("o/app", "v1.0.0")
    assert checkout.commit == commit
    assert checkout_files(checkout.path) == [
        "docs/README.md",
        "docs/guide.md",
        "openapi-spec/openapi.json",
    ]


def test_a_commit_pin_and_head_fetch_the_same_commit(
    remotes: tuple[Path, str], tmp_path: Path
) -> None:
    root, commit = remotes
    fetcher = aggregate.Fetcher(tmp_path / "work", f"file://{root}")
    assert fetcher.fetch("o/app", commit).commit == commit
    assert fetcher.fetch("o/app", "HEAD").commit == commit


def test_a_tag_checkout_is_reused_on_the_next_run(
    remotes: tuple[Path, str], tmp_path: Path
) -> None:
    root, commit = remotes
    work = tmp_path / "work"
    first = aggregate.Fetcher(work, f"file://{root}").fetch("o/app", "v1.0.0")
    (root / "o" / "app.git").rename(root / "o" / "gone.git")
    again = aggregate.Fetcher(work, f"file://{root}").fetch("o/app", "v1.0.0")
    assert again == first
    assert again.commit == commit


def test_a_missing_ref_is_a_fetch_error_and_not_a_refusal(
    remotes: tuple[Path, str], tmp_path: Path
) -> None:
    root, _ = remotes
    fetcher = aggregate.Fetcher(tmp_path / "work", f"file://{root}")
    with pytest.raises(aggregate.FetchError) as caught:
        fetcher.fetch("o/app", "v9.9.9")
    assert not isinstance(caught.value, aggregate.RefusedError)


def test_a_global_git_config_cannot_redirect_the_fetch(
    remotes: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, commit = remotes
    home = tmp_path / "home"
    home.mkdir()
    rewrite = f'[url "file:///nowhere/"]\n\tinsteadOf = file://{root}/\n'
    (home / ".gitconfig").write_text(rewrite, encoding="utf-8")
    plain = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    plain |= {"HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}
    plain.pop("XDG_CONFIG_HOME", None)
    control = subprocess.run(
        ["git", "ls-remote", f"file://{root}/o/app.git"],
        env=plain,
        capture_output=True,
        check=False,
    )
    assert control.returncode != 0, "the rewrite should defeat an ordinary git"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    fetcher = aggregate.Fetcher(tmp_path / "work", f"file://{root}")
    assert fetcher.fetch("o/app", "v1.0.0").commit == commit


# --- one build, end to end ----------------------------------------------------------


def test_assemble_lays_out_a_build(remotes: tuple[Path, str], tmp_path: Path) -> None:
    root, _ = remotes
    make_repo(
        root,
        "o/dfe-engine",
        {
            "docs/architecture.md": "# Arch\n\n[root](../README.md)\n",
            "openapi-spec/openapi.json": json.dumps(
                {"openapi": "3.1.0", "info": {"description": "Poll GET /tasks/{id}."}}
            ),
        },
        "v2.0.0",
    )
    make_repo(root, "o/fork", {"docs/README.md": "#", "docs/UPSTREAM_NAME.md": "#"}, "v3.0.0")
    members = [
        Member("app", "o/app", "toolchain", 10, "product"),
        Member("dfe-engine", "o/dfe-engine", "consumers", 11, "product"),
        Member("fork", "o/fork", "consumers", 12, "fork"),
        Member("lib", "o/lib", "libraries", 13, "oss"),
    ]
    refs = {"app": "v1.0.0", "dfe-engine": "v2.0.0", "fork": "v3.0.0"}
    build = Build("9.9.9", "/v/9.9.9/", True, refs)
    out = tmp_path / "content"
    out.mkdir()
    fetcher = aggregate.Fetcher(tmp_path / "work", f"file://{root}")
    result = aggregate.assemble(build, members, fetcher, out)
    tree = out / "9.9.9"

    assert json.loads((tree / "app" / "_category_.json").read_text()) == {
        "label": "app",
        "position": 10,
        "collapsed": True,
        "customProps": {"lane": "toolchain", "ref": "v1.0.0"},
    }
    engine = json.loads((tree / "dfe-engine" / "_category_.json").read_text())
    assert engine["link"] == {"type": "doc", "id": "dfe-engine/architecture"}
    fork = json.loads((tree / "fork" / "_category_.json").read_text())
    assert "link" not in fork
    api = json.loads((tree / "dfe-engine" / "api" / "_category_.json").read_text())
    assert api["customProps"] == {"openapiSlice": "dfe-engine/api/sidebar.ts"}
    placed = json.loads((out / "_specs" / "9.9.9" / "dfe-engine.json").read_text())
    assert placed == {
        "openapi": "3.1.0",
        "info": {"description": "Poll GET /tasks/&#123;id&#125;."},
    }
    assert result.openapi == {
        "spec": "_specs/9.9.9/dfe-engine.json",
        "output": "9.9.9/dfe-engine/api",
        "slice": "dfe-engine/api/sidebar.ts",
    }
    link = "(https://github.com/o/dfe-engine/blob/v2.0.0/README.md)"
    assert link in (tree / "dfe-engine" / "architecture.md").read_text()
    assert (tree / "README.md").is_file()
    assert (tree / "quickstart" / "docker.md").is_file()
    assert [entry["name"] for entry in result.members] == ["app", "dfe-engine", "fork"]
    assert {(f.member, f.path, f.rule) for f in result.findings} == {
        ("dfe-engine", "docs/", Rule.LANDING),
        ("lib", "", Rule.NO_PIN),
    }


@pytest.mark.parametrize(
    ("refs", "parent_docs", "expected"),
    [
        ({"ui": "v1.0.0", "hd": "v2.0.0"}, {}, ["ui/hd/README.md", "ui/hd/fork/README.md"]),
        ({"hd": "v2.0.0"}, {}, ["hd/README.md", "hd/fork/README.md"]),
        ({"ui": "v1.0.0", "hd": "v2.0.0"}, {"docs/hd/x.md": "# x\n"}, "ui/hd is already"),
    ],
)
def test_a_mounted_member_lands_inside_its_parent_when_the_parent_is_built(
    tmp_path: Path, refs: dict[str, str], parent_docs: dict[str, str], expected: list[str] | str
) -> None:
    root = tmp_path / "remotes"
    make_repo(root, "o/ui", {"docs/README.md": "# UI\n", **parent_docs}, "v1.0.0")
    make_repo(
        root,
        "o/hd",
        {"docs/README.md": "# HD\n", "docs/fork/README.md": "# Fork\n", "docs/guide.md": "#\n"},
        "v2.0.0",
    )
    members = [Member("hd", "o/hd", "ui", 10, "fork"), Member("ui", "o/ui", "ui", 11, "product")]
    policy = aggregate.Policy(mounts={"hd": "ui"}, internal={"hd": frozenset({"docs/guide.md"})})
    out = tmp_path / "content"
    out.mkdir()
    fetcher = aggregate.Fetcher(tmp_path / "work", f"file://{root}")
    build = Build("9.9.9", "/v/9.9.9/", True, refs)
    if isinstance(expected, str):
        with pytest.raises(SystemExit, match=expected):
            aggregate.assemble(build, members, fetcher, out, policy)
        return
    result = aggregate.assemble(build, members, fetcher, out, policy)
    pages = [path for path in checkout_files(out / "9.9.9") if path.endswith(".md")]
    assert [page for page in pages if "hd/" in page] == expected
    assert [entry["name"] for entry in result.members] == sorted(refs, key=["ui", "hd"].index)


@pytest.mark.parametrize(
    ("placement", "expected"),
    [
        (
            {"ui": {"default": "J / S"}, "hd": {"default": "J / T", "fork/": "K / U"}},
            {"ui/": "J / S", "ui/hd/": "J / T", "ui/hd/fork/": "K / U", "": "J / S", "README": ""},
        ),
        ({"ui": {"default": "J / S"}}, "hd publishes and has no placement"),
    ],
)
def test_assemble_places_every_published_member_and_skips_a_wholly_internal_one(
    tmp_path: Path, placement: dict, expected: dict | str
) -> None:
    root = tmp_path / "remotes"
    make_repo(root, "o/ui", {"docs/README.md": "# UI\n"}, "v1.0.0")
    make_repo(root, "o/hd", {"docs/README.md": "# HD\n", "docs/fork/README.md": "#\n"}, "v2.0.0")
    make_repo(root, "o/lib", {"docs/README.md": "# Lib\n"}, "v3.0.0")
    members = [
        Member("ui", "o/ui", "ui", 10, "product"),
        Member("hd", "o/hd", "ui", 11, "fork"),
        Member("lib", "o/lib", "libraries", 12, "oss"),
    ]
    policy = aggregate.Policy(mounts={"hd": "ui"}, internal={"lib": frozenset({"docs/"})})
    journeys = aggregate.Journeys(
        (("J", ("S", "T")), ("K", ("U",))),
        {**placement, "content-root": {"default": "J / S", "README.md": ""}},
    )
    out = tmp_path / "content"
    out.mkdir()
    fetcher = aggregate.Fetcher(tmp_path / "work", f"file://{root}")
    build = Build("9.9.9", "/v/9.9.9/", True, {"ui": "v1.0.0", "hd": "v2.0.0", "lib": "v3.0.0"})
    if isinstance(expected, str):
        with pytest.raises(SystemExit, match=expected):
            aggregate.assemble(build, members, fetcher, out, policy, journeys)
        return
    result = aggregate.assemble(build, members, fetcher, out, policy, journeys)
    assert result.placements == expected
    assert not (out / "9.9.9" / "lib").exists()
    assert ("lib", Rule.ALL_INTERNAL) in {(f.member, f.rule) for f in result.findings}
    assert [entry["name"] for entry in result.members] == ["ui", "hd"]
    manifest = aggregate.site_manifest([result], (), journeys)
    assert manifest["builds"][0]["placements"] == expected
    assert manifest["journeys"][0] == {"label": "J", "sections": ["S", "T"]}


def test_the_report_summarises_each_member_then_lists_every_finding() -> None:
    result = BuildResult(
        Build("1.0.0", "/v/1.0.0/", True, {}),
        findings=[
            Finding("a", "docs/X.md", Rule.CASE),
            Finding("a", "docs/Y.md", Rule.CASE),
            Finding("b", "", Rule.NO_PIN),
        ],
    )
    text = aggregate.stray_report(result)
    assert f"| a | 2 | {Rule.CASE} (2) |" in text
    assert f"| b | 1 | {Rule.NO_PIN} (1) |" in text
    assert f"- `docs/X.md`: {Rule.CASE}" in text
    assert f"- {Rule.NO_PIN}" in text


def test_the_manifest_names_the_newest_listed_build() -> None:
    results = [
        BuildResult(Build("2.3.0", "/v/2.3.0/", True, {})),
        BuildResult(Build("2.2.0", "/v/2.2.0/", True, {})),
        BuildResult(Build("next", "/next/", False, {})),
    ]
    manifest = aggregate.site_manifest(results)
    assert manifest["newest"] == "2.3.0"
    assert [build["name"] for build in manifest["builds"]] == ["2.3.0", "2.2.0", "next"]
    preview_only = aggregate.site_manifest([BuildResult(Build("next", "/next/", False, {}))])
    assert preview_only["newest"] is None
    assert manifest["dropped"] == []
    assert aggregate.site_manifest(results, {"2.1.0", "1.9.0"})["dropped"] == ["1.9.0", "2.1.0"]


@pytest.mark.parametrize(
    ("dropped", "expected"),
    [
        ([], []),
        (
            ["2.2.0"],
            [
                "/v/2.2.0 /v/2.2.1/ 301",
                "/v/2.2.0/ /v/2.2.1/ 301",
                "/v/2.2.0/* /v/2.2.1/:splat 301",
            ],
        ),
    ],
)
def test_a_dropped_stack_redirects_to_the_same_path_in_the_newest(
    dropped: list[str], expected: list[str]
) -> None:
    build_spec = importlib.util.spec_from_file_location(
        "docs_build_site", SCRIPT.with_name("build_site.py")
    )
    build_site = importlib.util.module_from_spec(build_spec)
    build_spec.loader.exec_module(build_site)
    lines = build_site.redirects("2.2.1", dropped).splitlines()
    assert lines[:3] == ["/ /v/2.2.1/ 302", "/v /v/2.2.1/ 302", "/v/ /v/2.2.1/ 302"]
    assert lines[3:] == expected


# --- the docs gate -------------------------------------------------------------------

gate_spec = importlib.util.spec_from_file_location("docs_gate", SCRIPT.with_name("gate.py"))
gate = importlib.util.module_from_spec(gate_spec)
gate_spec.loader.exec_module(gate)
GATE_PHRASES = gate.load_phrases(
    aggregate.yaml_subset.parse(gate.GATE_FILE.read_text(encoding="utf-8"))
)
EM_DASH = "\u2014"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Use it; then go.\n", [(1, "semicolon", ";")]),
        ('`a; b` and [x](http://a;b) &amp; <a href="x;y">ok</a>\n', []),
        ("Text\n```bash\nset -e; run\n```\n", []),
        ("---\ntitle: a; b\n---\n# T\n", []),
        ("Leverage the seam.\n", [(1, "register", "seam"), (1, "register", "leverage")]),
        ("Ships in 2.2.1-rc.7.\n", [(1, "process", "rc.N")]),
        ("It is expected to run.\n", [(1, "hedge", "is expected to")]),
        ("Elasticsearch output\n", [(1, "elasticsearch", "Elasticsearch")]),
        (f"A {EM_DASH} dash\n", [(1, "ascii", "U+2014 em dash")]),
        ("$$\n\\alpha; \u03b2\n$$\nInline $\\beta;$ fine\n", []),
        ("<!-- a; b\nstill; comment -->\nok\n", []),
        (":::note Stub\n", [(1, "process", "stub admonition")]),
        ("Ask the lead. The leader is fine.\n", [(1, "process", "the lead")]),
        ("<!-- docs-gate: allow semicolon -- quoted -->\n\nA; b\n", []),
        ("A; b <!-- docs-gate: allow semicolon -- quoted -->\n", []),
        ("{/* docs-gate: allow semicolon -- quoted */} A; b\n", []),
        ("<!-- docs-gate: allow semicolon -- x -->\nA; b\nC; d\n", [(3, "semicolon", ";")]),
        (
            f"<!-- docs-gate: off all -- maths -->\nA; b {EM_DASH} c\n<!-- docs-gate: on all -->\nE; f\n",
            [(4, "semicolon", ";")],
        ),
        (
            "<!-- docs-gate: allow semicolon -->\nA; b\n",
            [(1, "marker", "no reason after --"), (2, "semicolon", ";")],
        ),
        ("<!-- docs-gate: allow speling -- x -->\n", [(1, "marker", "unknown rule speling")]),
        (
            "<!-- docs-gate: skip semicolon -- x -->\n",
            [(1, "marker", "unknown verb 'skip' (allow, off or on)")],
        ),
    ],
)
def test_the_gate_reads_prose_only_and_honours_its_markers(
    text: str, expected: list[tuple[int, str, str]]
) -> None:
    findings = gate.check_page(text, GATE_PHRASES, build="b", page="p.md")
    assert [(f.line, f.rule, f.term) for f in findings] == expected


def test_the_gate_needs_every_phrase_section() -> None:
    with pytest.raises(SystemExit, match="`hedge` is not a map"):
        gate.load_phrases({"process": {}, "register": {}})


def write_content(root: Path, pages: dict[str, str]) -> Path:
    members = [
        {"name": "dfe-ui", "repo": "o/dfe-ui"},
        {"name": "dfe-hyperdx", "repo": "o/dfe-hyperdx"},
    ]
    site = {"builds": [{"name": "1.0.0", "members": members, "openapi": None}]}
    write_tree(root, {"site.json": json.dumps(site), **{f"1.0.0/{k}": v for k, v in pages.items()}})
    return root


@pytest.mark.parametrize(
    ("pages", "exit_code", "new"),
    [
        ({"dfe-ui/a.md": "# A\n\nIntro.\n\nOld; line.\n"}, 0, []),
        ({"dfe-ui/a.md": "# A\n\nOld; line.\n"}, 0, []),
        (
            {"dfe-ui/a.md": "# A\n\nOld; line, edited.\n"},
            1,
            ["o/dfe-ui docs/a.md:3: semicolon"],
        ),
        (
            {"dfe-ui/a.md": "# A\n\nOld; line.\n", "dfe-ui/dfe-hyperdx/b.md": "# B\nNew; one.\n"},
            1,
            ["o/dfe-hyperdx docs/b.md:2: semicolon"],
        ),
        (
            {"README.md": "# R\nRoot; page.\n"},
            1,
            ["hyperi-io/dfe-infra docs-site/content-root/README.md:2"],
        ),
    ],
)
def test_the_gate_fails_only_on_findings_the_baseline_lacks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    pages: dict[str, str],
    exit_code: int,
    new: list[str],
) -> None:
    baseline = write_content(tmp_path / "main", {"dfe-ui/a.md": "# A\n\nOld; line.\n"})
    content = write_content(tmp_path / "pr", pages)
    summary = tmp_path / "summary.md"
    status = gate.main(
        ["--content", str(content), "--baseline", str(baseline), "--summary", str(summary)]
    )
    out = capsys.readouterr().out
    assert status == exit_code
    for line in new:
        assert line in out
    assert f"{len(new)} finding(s) new against main" in summary.read_text()
    report_only = gate.main(
        ["--content", str(content), "--baseline", str(baseline), "--report-only"]
    )
    assert report_only == 0


def test_the_gate_reads_named_files_without_a_build(tmp_path: Path) -> None:
    page = tmp_path / "docs" / "aws.md"
    write_tree(tmp_path, {"docs/aws.md": "# AWS\n\nGo; now.\n", "docs/x.txt": "a; b"})
    assert gate.main([str(tmp_path / "docs"), "--report-only"]) == 0
    assert gate.main([str(page)]) == 1
