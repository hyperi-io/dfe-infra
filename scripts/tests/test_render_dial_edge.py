#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_dial_edge.py
#  Purpose:      Guard the dial's `edge:` block -- every boolean and every enum
#                is refused by name, the deprecated blocks feed it for one
#                release, a dial setting both spellings of one key is refused,
#                and the summary names each tier-2 opt-in with its bucket.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for render_dial.py's `edge:` block.

    python3 -m pytest scripts/tests/test_render_dial_edge.py -q

One block names every door a deployment opens, and this renderer applies none
of it except `edge.enabled`. So the value of the block is what it REFUSES: a
boolean that is not a boolean, an enum outside the vocabulary its chart accepts,
a combination no chart renders, and a dial that says one thing twice.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
COMMON = REPO_ROOT / "argocd" / "values" / "common.yaml"

sys.path.insert(0, str(SCRIPTS))
import render_dial  # noqa: E402
from yaml_subset import parse as parse_dial  # noqa: E402

EXAMPLE = REPO_ROOT / "deployment.example.yaml"


def dial(body: str) -> dict[str, object]:
    return parse_dial(body, source="test-dial")


def example() -> dict[str, object]:
    return parse_dial(EXAMPLE.read_text(encoding="utf-8"), source=EXAMPLE.name)


# ---------------------------------------------------------------------------
# Booleans
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(render_dial._EDGE_BOOL_DEFAULTS))
def test_every_boolean_is_refused_by_name(path: tuple[str, ...]) -> None:
    """Each one reads through the same _flag() path as endpoint.public."""
    body = "\n".join(
        f"{'  ' * depth}{key}:" + (' "yes"' if depth == len(path) - 1 else "")
        for depth, key in enumerate(path)
    )
    with pytest.raises(render_dial.DialError, match=re.escape(".".join(path))):
        render_dial._edge_flags(dial(body + "\n"))


def test_each_boolean_reads_through_quoted_or_not() -> None:
    parsed = dial(
        "edge:\n"
        "  enabled: false\n"
        "  ingest:\n"
        "    tunnel:\n"
        '      enabled: "true"\n'
    )
    flags = render_dial._edge_flags(parsed)
    assert flags["edge.enabled"] is False
    assert flags["edge.ingest.tunnel.enabled"] is True


def test_the_tunnel_is_off_until_a_deployment_asks() -> None:
    """The module OFFERS the tunnel; the engine turns it on by writing a file."""
    assert render_dial._edge_flags(dial("substrate: k8s\n"))["edge.ingest.tunnel.enabled"] is False
    assert render_dial._edge_flags(example())["edge.ingest.tunnel.enabled"] is False


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(render_dial._EDGE_ENUMS))
def test_every_enum_refuses_a_value_outside_its_vocabulary(path: tuple[str, ...]) -> None:
    body = "\n".join(
        f"{'  ' * depth}{key}:" + (" nosuchvalue" if depth == len(path) - 1 else "")
        for depth, key in enumerate(path)
    )
    with pytest.raises(render_dial.DialError, match=re.escape(".".join(path))):
        render_dial._edge_enums(dial(body + "\n"))


@pytest.mark.parametrize("path", sorted(render_dial._EDGE_ENUMS))
def test_every_named_value_of_every_enum_reads_through(path: tuple[str, ...]) -> None:
    for value in render_dial._EDGE_ENUMS[path]:
        body = "\n".join(
            f"{'  ' * depth}{key}:" + (f" {value}" if depth == len(path) - 1 else "")
            for depth, key in enumerate(path)
        )
        assert render_dial._edge_enums(dial(body + "\n"))[".".join(path)] == value


@pytest.mark.parametrize(("cloud", "flavour"), [("aws", "aws"), ("gcp", "gcp"),
                                                ("azure", "azure"), ("local", "onprem"),
                                                ("local-dfe", "onprem"), ("rancher", "onprem")])
def test_the_flavour_defaults_to_the_one_the_cloud_fact_selects(cloud: str, flavour: str) -> None:
    """The appset derives it the same way, so a dial that states none still reports it."""
    assert render_dial._edge_flavour(dial(f"k8s:\n  cloud: {cloud}\n")) == flavour


def test_the_receivers_door_still_falls_back_to_the_cloud_overlay() -> None:
    """A dial naming no mode reports whatever argocd/values/<cloud>.yaml applies."""
    for cloud, mode in (("aws", "vpn"), ("gcp", "vpn"), ("azure", "vpn"), ("local", "internal")):
        enums = render_dial._edge_enums(dial(f"k8s:\n  cloud: {cloud}\n"))
        assert enums["edge.ingest.receiver.mode"] == mode, cloud


# ---------------------------------------------------------------------------
# The one-release aliases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("new", "old"), sorted(render_dial._EDGE_ALIASES.items()))
def test_every_alias_feeds_its_new_path(new: tuple[str, ...], old: tuple[str, ...]) -> None:
    body = "\n".join(
        f"{'  ' * depth}{key}:" + (" fromtheoldpath" if depth == len(old) - 1 else "")
        for depth, key in enumerate(old)
    )
    value, label = render_dial._edge_scalar(dial(body + "\n"), new)
    assert value == "fromtheoldpath"
    assert label == ".".join(old)


@pytest.mark.parametrize(("new", "old"), sorted(render_dial._EDGE_ALIASES.items()))
def test_setting_both_spellings_of_one_key_is_refused(
    new: tuple[str, ...], old: tuple[str, ...]
) -> None:
    """They are one key, so a dial that sets both has two answers and no winner.

    The tree is built directly because two spellings can share a parent block,
    which two concatenated YAML fragments would repeat.
    """
    tree: dict[str, object] = {}
    for path, value in ((old, "old"), (new, "new")):
        node = tree
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    conflicts = render_dial._edge_alias_conflicts(tree)
    assert any(".".join(new) in c and ".".join(old) in c for c in conflicts), conflicts


def test_a_dial_on_the_new_block_alone_reports_no_deprecation() -> None:
    assert render_dial._edge_deprecations(example()) == []
    assert render_dial._edge_alias_conflicts(example()) == []


# ---------------------------------------------------------------------------
# What the module does not offer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["[hunt]", "[]"])
def test_the_retired_private_path_family_key_is_refused_by_name(value: str) -> None:
    """Empty too: the key is gone, and a dial still carrying it is a dial whose
    author thinks something reads it."""
    parsed = dial(f"edge:\n  engine_api:\n    private_path_families: {value}\n")
    with pytest.raises(render_dial.DialError, match=r"private_path_families"):
        render_dial._edge_refusals(parsed, render_dial._edge_flags(parsed))


def test_the_refusal_names_the_two_switches_that_replaced_it() -> None:
    parsed = dial("edge:\n  engine_api:\n    private_path_families: []\n")
    with pytest.raises(render_dial.DialError) as raised:
        render_dial._edge_refusals(parsed, render_dial._edge_flags(parsed))
    assert "edge.engine_api.cli_families_public" in str(raised.value)
    assert "edge.engine_api.scim_public" in str(raised.value)


def _refusals(body: str) -> None:
    parsed = dial(body)
    render_dial._edge_refusals(parsed, render_dial._edge_flags(parsed))


OTEL_ON = "edge:\n  ingest:\n    otel:\n      enabled: true\n"
TOKEN_PATH = "      auth:\n        remoteKey: dfe/local/otel/ingress\n"


def test_otel_ingress_on_with_no_token_path_is_refused() -> None:
    """Both charts refuse the same pair, so the dial agrees before a deploy does."""
    with pytest.raises(render_dial.DialError, match=r"edge\.ingest\.otel\.auth\.remoteKey"):
        _refusals(OTEL_ON)


def test_otel_ingress_on_with_a_token_path_is_accepted() -> None:
    _refusals(OTEL_ON + TOKEN_PATH)


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_auth_none_is_refused_whatever_the_switch(enabled: str) -> None:
    """There is no unauthenticated mode, so no dial may say there is one."""
    body = f"edge:\n  ingest:\n    otel:\n      enabled: {enabled}\n      auth: none\n"
    with pytest.raises(render_dial.DialError, match=r"edge\.ingest\.otel\.auth") as raised:
        _refusals(body)
    assert "no unauthenticated mode" in str(raised.value)


def test_the_retired_auth_required_reads_through_with_a_deprecation() -> None:
    """The one value it ever meant is now the only behaviour, so it is named, not refused."""
    parsed = dial("edge:\n  ingest:\n    otel:\n      enabled: false\n      auth: required\n")
    render_dial._edge_refusals(parsed, render_dial._edge_flags(parsed))
    lines = render_dial._edge_deprecations(parsed)
    assert any("edge.ingest.otel.auth" in line and "remoteKey" in line for line in lines), lines


def test_the_old_public_flag_feeds_the_switch_for_one_release() -> None:
    parsed = dial("edge:\n  ingest:\n    otel:\n      public: true\n" + TOKEN_PATH)
    assert render_dial._edge_flags(parsed)["edge.ingest.otel.enabled"] is True
    assert "edge.ingest.otel.public moved to edge.ingest.otel.enabled" in " ".join(
        render_dial._edge_deprecations(parsed)
    )


@pytest.mark.parametrize("port", ["99999", "0", "otlp"])
def test_an_otel_port_that_is_not_a_port_is_refused(port: str) -> None:
    with pytest.raises(render_dial.DialError, match=r"edge\.ingest\.otel\.port"):
        _refusals(f"edge:\n  ingest:\n    otel:\n      port: {port}\n")


def test_the_dial_block_is_the_common_yaml_block_verbatim() -> None:
    """The deployer pastes it under otel.ingress in infra/common.yaml, so it has
    to be that block, key for key and default for default."""
    example_block = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["edge"]["ingest"]["otel"]
    common = yaml.safe_load(COMMON.read_text(encoding="utf-8"))
    assert example_block == common["otel"]["ingress"]


def test_turning_otel_ingress_on_is_a_tier_two_exposure_with_no_spend() -> None:
    parsed = dial("k8s:\n  cloud: aws\n" + OTEL_ON + TOKEN_PATH)
    lines = render_dial._edge_tier2_on(
        render_dial._edge_enums(parsed), render_dial._edge_flags(parsed)
    )
    assert len(lines) == 1, lines
    assert lines[0].startswith("edge.ingest.otel.enabled: true -- no spend"), lines


# ---------------------------------------------------------------------------
# The admin peer's two inert fields
# ---------------------------------------------------------------------------


def _admin_peer(body: str) -> dict[str, object]:
    return render_dial._edge_tunnel(dial(f"edge:\n  ingest:\n    tunnel:\n      admin_peer:\n{body}"))


def test_an_admin_peer_ttl_other_than_the_default_is_refused_by_name() -> None:
    """A field nothing reads is worse than one that stops: the operator sets a
    deadline, sees it accepted, and gets a session nothing bounds."""
    with pytest.raises(render_dial.DialError, match=r"admin_peer\.ttl_minutes"):
        _admin_peer("        ttl_minutes: 90\n")


def test_the_refusal_names_the_upstream_change_and_what_to_do_instead() -> None:
    with pytest.raises(render_dial.DialError) as raised:
        _admin_peer("        ttl_minutes: 90\n")
    assert render_dial.ADMIN_PEER_ISSUE in str(raised.value)
    assert "bastion down" in str(raised.value)


def test_an_admin_peer_cidr_is_refused_by_name() -> None:
    with pytest.raises(render_dial.DialError, match=r"admin_peer\.peer_cidr"):
        _admin_peer('        peer_cidr: "100.64.1.0/24"\n')
    with pytest.raises(render_dial.DialError) as raised:
        _admin_peer('        peer_cidr: "100.64.1.0/24"\n')
    assert render_dial.ADMIN_PEER_ISSUE in str(raised.value)


def test_the_defaults_and_an_omitted_block_both_read_through() -> None:
    """The example dial writes both fields, so the value it ships must pass."""
    _admin_peer(f"        ttl_minutes: {render_dial.ADMIN_PEER_TTL_MINUTES}\n        peer_cidr: \"\"\n")
    render_dial._edge_tunnel(dial("substrate: k8s\n"))
    render_dial._edge(example())


# ---------------------------------------------------------------------------
# The printed summary
# ---------------------------------------------------------------------------


def test_the_shipped_example_turns_on_no_tier_two_key() -> None:
    """Tier 1 is the default posture; every costed door is the deployer's act."""
    parsed = example()
    tier2 = render_dial._edge_tier2_on(
        render_dial._edge_enums(parsed), render_dial._edge_flags(parsed)
    )
    assert tier2 == []


def test_a_tier_two_key_is_named_with_its_bucket_and_never_a_rate() -> None:
    parsed = dial("edge:\n  ingest:\n    receiver:\n      mode: public\n")
    lines = render_dial._edge_tier2_on(
        render_dial._edge_enums(parsed), render_dial._edge_flags(parsed)
    )
    assert len(lines) == 1, lines
    assert "edge.ingest.receiver.mode: public" in lines[0]
    assert "bucket L" in lines[0]
    assert "per GB processed" in lines[0]
    assert not re.search(r"\$|USD|\d+\s*/\s*(month|hour)", lines[0]), lines[0]


def test_a_tier_two_key_that_costs_nothing_says_no_spend_instead_of_a_bucket() -> None:
    """Exposure and spend are different reasons to be tier 2, and a bucket on a
    key that creates nothing would read as a charge the deployer cannot find."""
    # The cloud is named so the receiver's own door takes the overlay's vpn
    # default and this reads the two engine rows alone.
    parsed = dial(
        "k8s:\n  cloud: aws\n"
        "edge:\n  engine_api:\n    cli_families_public: true\n    scim_public: true\n"
    )
    lines = render_dial._edge_tier2_on(
        render_dial._edge_enums(parsed), render_dial._edge_flags(parsed)
    )
    assert len(lines) == 2, lines
    assert all("no spend" in line for line in lines), lines
    assert not any("bucket" in line for line in lines), lines


def test_every_tier_two_row_names_a_key_the_dial_validates() -> None:
    """A row keyed on a path no enum and no boolean validates would never fire."""
    known = {".".join(p) for p in render_dial._EDGE_ENUMS}
    known |= {".".join(p) for p in render_dial._EDGE_BOOL_DEFAULTS}
    assert {row[0] for row in render_dial._EDGE_TIER2} <= known


def test_the_render_prints_the_edge_summary(tmp_path: Path) -> None:
    (tmp_path / "deployment.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8").replace(
            "      mode: vpn              #", "      mode: public           #"
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_dial.py"),
         "--dial", str(tmp_path / "deployment.yaml"),
         "--out", str(tmp_path / "bootstrap.env")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "EDGE MODULE (aws) -- on" in result.stderr
    assert "public hostnames (edge.product.public, edge.admin_uis.public.*): dfe-ui" in result.stderr
    assert "fleet tunnel (edge.ingest.tunnel.enabled): off" in result.stderr
    assert "OTLP ingest from outside the cluster (edge.ingest.otel.enabled): off" in result.stderr
    assert "tier 2 opt-ins that are ON: edge.ingest.receiver.mode: public" in result.stderr
    assert "bucket L" in result.stderr


@pytest.mark.parametrize(("cidrs", "flavour", "said"), [
    ([], "aws", "every web route answers 403 to every address"),
    ([], "gcp", "every web route answers 403 to every address"),
    ([], "azure", "every web route answers 403 to every address"),
    ([], "onprem", "not internet-facing, so nothing is fenced"),
    (["203.0.113.7/32", "198.51.100.0/24"], "aws", "203.0.113.7/32, 198.51.100.0/24"),
])
def test_the_web_fence_line_says_what_the_gateway_admits(cidrs: list[str], flavour: str, said: str) -> None:
    line, warnings = render_dial.web_fence(cidrs, flavour)
    assert said in line, line
    assert warnings == []


@pytest.mark.parametrize("everything", ["0.0.0.0/0", "::/0", "10.0.0.0/0"])
def test_a_slash_zero_is_every_address_and_warns(everything: str) -> None:
    """A /0 is the whole family whatever the address part says."""
    line, warnings = render_dial.web_fence(["203.0.113.7/32", everything], "aws")
    assert line.endswith("-- every address"), line
    assert len(warnings) == 1, warnings
    assert everything in warnings[0], warnings


@pytest.mark.parametrize(("value", "said"), [
    ('""', "web allow-list (edge.product.allowed_cidrs): empty -- the gateway is internet-facing"),
    ('"0.0.0.0/0"', "render_dial: WARNING -- edge.product.allowed_cidrs carries 0.0.0.0/0"),
])
def test_the_render_prints_the_web_fence(value: str, said: str, tmp_path: Path) -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    line = '    allowed_cidrs: ""\n'
    assert text.count(line) == 1, "the example's edge.product.allowed_cidrs moved; this test edits it in place"
    (tmp_path / "deployment.yaml").write_text(
        text.replace(line, f"    allowed_cidrs: {value}\n"), encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_dial.py"),
         "--dial", str(tmp_path / "deployment.yaml"),
         "--out", str(tmp_path / "bootstrap.env")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert said in result.stderr, result.stderr


def test_the_render_says_where_otel_ingress_takes_its_token(tmp_path: Path) -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    block = "    otel:\n      enabled: false\n      port: 4319\n      auth:\n        remoteKey: \"\"\n"
    assert block in text, "the example's otel block moved; this test edits it in place"
    (tmp_path / "deployment.yaml").write_text(
        text.replace(block, block.replace("enabled: false", "enabled: true")
                     .replace('remoteKey: ""', "remoteKey: dfe/prod/otel/ingress")),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_dial.py"),
         "--dial", str(tmp_path / "deployment.yaml"),
         "--out", str(tmp_path / "bootstrap.env")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (
        "OTLP ingest from outside the cluster (edge.ingest.otel.enabled): on -- bearer token "
        "from dfe/prod/otel/ingress"
    ) in result.stderr
    assert "infra/common.yaml" in result.stderr


def test_a_dial_setting_both_spellings_fails_the_render(tmp_path: Path) -> None:
    (tmp_path / "deployment.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8") + "\nui:\n  public:\n    dfe_ui: false\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_dial.py"),
         "--dial", str(tmp_path / "deployment.yaml"),
         "--out", str(tmp_path / "bootstrap.env")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "edge.product.public and ui.public.dfe_ui" in result.stderr


# One dial per refusal main() makes after parsing, each built from the shipped
# example so everything else in it would render.
REFUSED_DIALS = {
    "both spellings of one key": lambda text: text + "\nui:\n  public:\n    dfe_ui: false\n",
    "otel ingress with no token": lambda text: text.replace(
        "      enabled: false\n      port: 4319", "      enabled: true\n      port: 4319"
    ),
    "an unknown controller pool": lambda text: text.replace(
        "  controller_pool: combined", "  controller_pool: nosuchpool"
    ),
}

# An env file an operator already holds, which a refused dial must not touch.
OPERATOR_ENV = '# the operator\'s own\nDFE_ENV="operator-set"\nDFE_NAMESPACE="theirs"\n'


def _render(dial_text: str, tmp_path: Path) -> subprocess.CompletedProcess:
    (tmp_path / "deployment.yaml").write_text(dial_text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "render_dial.py"),
         "--dial", str(tmp_path / "deployment.yaml"),
         "--out", str(tmp_path / "bootstrap.env")],
        capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("refusal", sorted(REFUSED_DIALS))
def test_a_refused_dial_writes_no_env_file(refusal: str, tmp_path: Path) -> None:
    """A later step reads whatever env file is there, so a refusal must leave none behind."""
    shipped = EXAMPLE.read_text(encoding="utf-8")
    dial_text = REFUSED_DIALS[refusal](shipped)
    assert dial_text != shipped, "the edit to the example matched nothing"
    result = _render(dial_text, tmp_path)
    assert result.returncode == 1, result.stderr
    assert not (tmp_path / "bootstrap.env").exists(), result.stderr


@pytest.mark.parametrize("refusal", sorted(REFUSED_DIALS))
def test_a_refused_dial_leaves_an_existing_env_file_untouched(refusal: str, tmp_path: Path) -> None:
    env = tmp_path / "bootstrap.env"
    env.write_text(OPERATOR_ENV, encoding="utf-8")
    result = _render(REFUSED_DIALS[refusal](EXAMPLE.read_text(encoding="utf-8")), tmp_path)
    assert result.returncode == 1, result.stderr
    assert env.read_text(encoding="utf-8") == OPERATOR_ENV, result.stderr


def test_an_accepted_dial_still_writes_the_env_file(tmp_path: Path) -> None:
    """The counterweight: validating first must not stop the write it guards."""
    result = _render(EXAMPLE.read_text(encoding="utf-8"), tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'DFE_EDGE_ENABLED="true"' in (tmp_path / "bootstrap.env").read_text(encoding="utf-8")


def test_a_dial_on_the_old_spelling_alone_renders_and_prints_the_deprecation(
    tmp_path: Path
) -> None:
    """_edge_deprecations is tested as a function; this is the line that has to
    reach the operator, and deleting the loop in main() leaves that green."""
    dial = EXAMPLE.read_text(encoding="utf-8").replace(
        "  product:\n    public: true", '  product:\n    public: ""'
    ) + "\nui:\n  public:\n    dfe_ui: false\n"
    (tmp_path / "deployment.yaml").write_text(dial, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_dial.py"),
         "--dial", str(tmp_path / "deployment.yaml"),
         "--out", str(tmp_path / "bootstrap.env")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "render_dial: deprecated -- ui.public.dfe_ui moved to edge.product.public" in result.stderr


def test_the_shipped_example_parses_and_validates() -> None:
    parsed = example()
    assert render_dial._edge_alias_conflicts(parsed) == []
    flags = render_dial._edge_flags(parsed)
    enums = render_dial._edge_enums(parsed)
    render_dial._edge_refusals(parsed, flags)
    assert flags["edge.enabled"] is True
    assert flags["edge.ingest.otel.enabled"] is False
    assert enums["edge.flavour"] == "aws"
    assert enums["edge.ingest.tunnel.pki_mode"] == "external"
    assert enums["edge.ingest.receiver.mode"] == "vpn"
