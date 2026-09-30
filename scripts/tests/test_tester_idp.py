#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_tester_idp.py
#  Purpose:      Prove `dfe-ops idp deploy` renders a dex + glauth fixture that
#                would actually work, that no generated password reaches the
#                values file, the argv, or the helm release history, and that
#                the shipped fixture grants what the OIDC role spec asserts.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Render tests for scripts/tester_idp.py, the `dfe-ops idp` subcommand.

Every mistake this guards against fails the same silent way. A dex whose
connector is subtly wrong does not crash -- it authenticates and returns an
empty groups claim, and the RBAC chain under test then proves nothing. A
redirect URI that did not make it into staticClients surfaces as an IdP error
page halfway through a browser flow. A password that leaks into the values file
sits in helm's release history in the cluster for as long as the fixture lives.

A fake helm and kubectl first on PATH record their argv and stdin, so the whole
deploy runs with no cluster.

    python3 scripts/tests/test_tester_idp.py

No third-party deps and no test runner, matching the tool it tests.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
DFE_OPS = SCRIPTS / "dfe-ops"
RBA_SPEC = REPO_ROOT / "tests" / "e2e-ui" / "specs" / "engine" / "oidc-rba.spec.ts"

sys.path.insert(0, str(SCRIPTS))
import tester_idp  # noqa: E402

# Records argv and any stdin, then answers the few reads the deploy makes.
FAKE_TOOL = """#!/usr/bin/env python3
import json, os, sys

log = os.environ["FAKE_CALLS"]
args = sys.argv[1:]
body = ""
if "-" in args and "-f" in args:
    body = sys.stdin.read()
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": os.path.basename(sys.argv[0]),
                         "args": args, "stdin": body}) + "\\n")
sys.exit(0)
"""


class FakeCluster:
    """A temp dir holding a fake helm + kubectl and their call log."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.calls = self.dir / "calls.jsonl"
        for tool in ("helm", "kubectl"):
            path = self.dir / tool
            path.write_text(FAKE_TOOL, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        self.env = {"PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_CALLS": str(self.calls)}

    def called(self) -> list[dict]:
        if not self.calls.exists():
            return []
        return [json.loads(ln) for ln in self.calls.read_text(encoding="utf-8").splitlines() if ln]

    def __enter__(self) -> FakeCluster:
        return self

    def __exit__(self, *_exc) -> None:
        self._tmp.cleanup()


def applied(calls: list[dict]) -> list[dict]:
    """Every object a run sent to `kubectl apply -f -`."""
    out = []
    for call in calls:
        if call["tool"] == "kubectl" and "apply" in call["args"] and call["stdin"]:
            out.extend(json.loads(call["stdin"])["items"])
    return out


USERS = """
[[users]]
  name = "alice"
  uidnumber = 6001
  primarygroup = 5000
  mail = "alice@dfe.test"
  passsha256 = "{{DFE_FIXTURE_PASS_SHA256}}"

[[groups]]
  name = "dfe-admins"
  gidnumber = 5000
"""

# Known plaintexts, so a test can assert they are absent from what gets deployed.
USER_PW = "hunter2xyz"
BIND_PW = "bindpw123"


def deploy_args(tmp: Path, **overrides) -> argparse.Namespace:
    """Built from dfe-ops' own parser, so a flag rename breaks this too."""
    ns = _ops().build_parser().parse_args(
        [
            "idp", "deploy",
            "--domain", "dfe.example.com",
            "--redirect-uri", "https://dfe.dfe.example.com/api/v1/auth/oidc/dex/callback",
            "--secrets-out", str(tmp / "idp.env"),
            "--users-file", str(tmp / "users.toml"),
        ]
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


_ops_module = None


def _ops():
    """dfe-ops carries no extension, so it is loaded by path rather than imported."""
    global _ops_module
    if _ops_module is None:
        loader = importlib.machinery.SourceFileLoader("dfeops", str(DFE_OPS))
        spec = importlib.util.spec_from_loader("dfeops", loader)
        _ops_module = importlib.util.module_from_spec(spec)
        sys.modules["dfeops"] = _ops_module
        loader.exec_module(_ops_module)
    return _ops_module


def run_failing_deploy(failing: str, **overrides) -> tuple[SystemExit | None, Path]:
    """A deploy whose `failing` tool exits non-zero. Returns (the exit, secrets file)."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "users.toml").write_text(USERS, encoding="utf-8", newline="\n")
    args = deploy_args(tmp, **overrides)
    with FakeCluster() as cluster:
        (cluster.dir / failing).write_text(
            FAKE_TOOL.replace("sys.exit(0)", "sys.exit(1)"), encoding="utf-8", newline="\n"
        )
        (cluster.dir / failing).chmod(0o755)
        saved_env, saved_out, saved_err = dict(os.environ), sys.stdout, sys.stderr
        os.environ.update(cluster.env)
        sys.stdout = sys.stderr = io.StringIO()
        raised: SystemExit | None = None
        try:
            tester_idp.cmd_idp_deploy(args)
        except SystemExit as exc:
            raised = exc
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err
            os.environ.clear()
            os.environ.update(saved_env)
        return raised, tmp / "idp.env"


def run_deploy(**overrides) -> tuple[int, list[dict], Path]:
    """A full `idp deploy` against fake tools. Returns (rc, calls, secrets file).

    The calls are read back BEFORE the fake tools' temp dir is removed, and the
    tool's own progress output is swallowed so a passing run reads as test
    output rather than as a deploy transcript.
    """
    tmp = Path(tempfile.mkdtemp())
    (tmp / "users.toml").write_text(USERS, encoding="utf-8", newline="\n")
    args = deploy_args(tmp, **overrides)
    with FakeCluster() as cluster:
        saved_env, saved_out, saved_err = dict(os.environ), sys.stdout, sys.stderr
        os.environ.update(cluster.env)
        sys.stdout = sys.stderr = io.StringIO()
        try:
            rc = tester_idp.cmd_idp_deploy(args)
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err
            os.environ.clear()
            os.environ.update(saved_env)
        return rc, cluster.called(), tmp / "idp.env"


# --- dex values --------------------------------------------------------------
def test_the_dex_client_allows_every_redirect_uri_it_was_given() -> None:
    """A URI missing here is an IdP error page part-way through a browser flow."""
    uris = [
        "https://dfe.example.com/api/v1/auth/oidc/dex/callback",
        "http://localhost:8000/api/v1/auth/oidc/dex/callback",
    ]
    values = tester_idp.render_dex_values(
        issuer="https://dex.example.com", namespace="dex", redirect_uris=uris
    )
    client = values["config"]["staticClients"][0]
    expect("every redirect URI is registered", client["redirectURIs"] == uris, f"{client}")
    expect("under the requested client id", client["id"] == "dfe-engine", f"{client['id']}")


def test_a_client_with_no_redirect_uri_is_refused() -> None:
    """Dex accepts an empty redirectURIs and then rejects every login."""
    try:
        tester_idp.render_dex_values(
            issuer="https://dex.example.com", namespace="dex", redirect_uris=[]
        )
        expect("an empty redirect list is refused", False, "no error raised")
    except ValueError:
        expect("an empty redirect list is refused", True)


def test_the_directory_is_ldap_and_the_static_password_db_is_off() -> None:
    """The static password DB cannot emit groups, which is the claim under test."""
    values = tester_idp.render_dex_values(
        issuer="https://dex.example.com", namespace="idp", redirect_uris=["https://x/cb"]
    )
    config = values["config"]
    expect("the static password DB is off", config["enablePasswordDB"] is False)
    connector = config["connectors"][0]
    expect("the connector is ldap", connector["type"] == "ldap", f"{connector['type']}")
    expect(
        "pointed at glauth in the same namespace",
        connector["config"]["host"] == "glauth.idp.svc.cluster.local:3893",
        connector["config"]["host"],
    )


def test_the_group_search_matches_glauth_s_own_schema() -> None:
    """glauth emits groupOfUniqueNames with full user DNs and the name in ou.

    The posixGroup shape (memberUid against uidNumber, name in cn) authenticates
    fine and returns NO groups, so this is the setting that decides whether the
    fixture proves anything.
    """
    values = tester_idp.render_dex_values(
        issuer="https://dex.example.com",
        namespace="dex",
        redirect_uris=["https://x/cb"],
        base_dn="dc=acme,dc=test",
    )
    gs = values["config"]["connectors"][0]["config"]["groupSearch"]
    expect("group search is under the groups OU", gs["baseDN"] == "ou=groups,dc=acme,dc=test", gs["baseDN"])
    expect("matching the user DN against uniqueMember",
           gs["userMatchers"] == [{"userAttr": "DN", "groupAttr": "uniqueMember"}], f"{gs}")
    expect("and reading the name from ou", gs["nameAttr"] == "ou", gs["nameAttr"])


def test_the_approval_screen_is_skipped() -> None:
    """Every consumer is headless; a consent page nobody clicks is a hang."""
    values = tester_idp.render_dex_values(
        issuer="https://dex.example.com", namespace="dex", redirect_uris=["https://x/cb"]
    )
    expect("skipApprovalScreen is on", values["config"]["oauth2"]["skipApprovalScreen"] is True)


def test_the_dex_image_is_pulled_by_digest() -> None:
    """A floating tag would silently move the IdP under a passing test suite."""
    values = tester_idp.render_dex_values(
        issuer="https://dex.example.com", namespace="dex", redirect_uris=["https://x/cb"]
    )
    digest = values["image"]["digest"]
    expect("the dex image carries a digest", digest.startswith("sha256:"), digest)
    expect("of the full 64 hex characters", len(digest) == 71, str(len(digest)))


def test_the_glauth_image_is_digest_pinned_too() -> None:
    expect(
        "the default glauth image is pinned by digest",
        "@sha256:" in tester_idp.GLAUTH_IMAGE,
        tester_idp.GLAUTH_IMAGE,
    )


def test_secrets_reach_dex_as_env_vars_not_values() -> None:
    """helm keeps every values file in the release history, in the cluster."""
    values = tester_idp.render_dex_values(
        issuer="https://dex.example.com", namespace="dex", redirect_uris=["https://x/cb"]
    )
    client = values["config"]["staticClients"][0]
    expect("the client secret is named, not inlined", "secret" not in client, f"{client}")
    expect("it is read from an env var", client["secretEnv"] == "DEX_CLIENT_SECRET")
    sources = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in values["envVars"]}
    expect(
        "both env vars come from the dex-secrets Secret",
        all(ref["name"] == "dex-secrets" for ref in sources.values()) and len(sources) == 2,
        f"{sources}",
    )
    expect(
        "and the bind password is an env reference in the connector",
        values["config"]["connectors"][0]["config"]["bindPW"] == "$GLAUTH_SEARCH_PW",
    )


# --- HTTPRoute ---------------------------------------------------------------
def test_the_route_attaches_to_the_named_gateway() -> None:
    route = tester_idp.render_httproute(
        namespace="dex", hostname="dex.dfe.example.com", gateway="envoy-gateway-system/dfe-gateway"
    )
    parent = route["spec"]["parentRefs"][0]
    expect("the gateway namespace is split off", parent["namespace"] == "envoy-gateway-system", f"{parent}")
    expect("and its name", parent["name"] == "dfe-gateway", f"{parent}")
    expect("the hostname is the issuer host", route["spec"]["hostnames"] == ["dex.dfe.example.com"])
    backend = route["spec"]["rules"][0]["backendRefs"][0]
    expect("routed to dex on its http port", backend == {"name": "dex", "port": 5556}, f"{backend}")


def test_the_route_pins_no_listener_section() -> None:
    """sectionName would bind the fixture to one deployment's listener names."""
    route = tester_idp.render_httproute(namespace="dex", hostname="dex.example.com")
    expect(
        "no sectionName is set",
        "sectionName" not in route["spec"]["parentRefs"][0],
        f"{route['spec']['parentRefs'][0]}",
    )


def test_a_gateway_without_a_namespace_is_refused() -> None:
    """A bare name would attach to the IdP's own namespace and route nothing."""
    try:
        tester_idp.render_httproute(namespace="dex", hostname="d.example.com", gateway="dfe-gateway")
        expect("a namespaceless --gateway is refused", False, "no error raised")
    except ValueError:
        expect("a namespaceless --gateway is refused", True)


# --- the glauth directory ----------------------------------------------------
def test_the_password_is_stored_only_as_its_hash() -> None:
    """The config lands in a cluster Secret; a plaintext password there is the leak."""
    config = tester_idp.render_glauth_config(
        USERS, fixture_password=USER_PW, search_password=BIND_PW
    )
    expect("the plaintext never appears", USER_PW not in config)
    expect("the bind plaintext never appears", BIND_PW not in config)
    expect(
        "the user's hash does",
        hashlib.sha256(USER_PW.encode()).hexdigest() in config,
    )
    expect("and the placeholder is gone", "{{DFE_FIXTURE_PASS_SHA256}}" not in config)


def test_the_bind_account_can_search() -> None:
    """glauth grants no capability by default, and a dex that cannot search
    returns an empty directory rather than an error."""
    config = tester_idp.render_glauth_config(
        USERS, fixture_password=USER_PW, search_password=BIND_PW
    )
    expect("the search account exists", 'name = "search"' in config)
    expect("with an explicit search capability", 'action = "search"' in config)
    expect("over everything", 'object = "*"' in config)


def test_the_base_dn_flows_into_the_config() -> None:
    config = tester_idp.render_glauth_config(
        USERS, fixture_password=USER_PW, search_password=BIND_PW, base_dn="dc=acme,dc=test"
    )
    expect("the backend uses the requested base DN", 'baseDN = "dc=acme,dc=test"' in config, config)


def test_a_user_in_an_undeclared_group_is_caught_before_deploy() -> None:
    """glauth crash-loops on this, which reads as a broken image rather than a typo."""
    bad = USERS.replace("primarygroup = 5000", "primarygroup = 5999")
    try:
        tester_idp.validate_users_toml(bad)
        expect("an undeclared gid is refused", False, "no error raised")
    except ValueError as exc:
        expect("an undeclared gid is refused", "5999" in str(exc), str(exc))


def test_an_empty_directory_is_refused() -> None:
    for name, text in (("no users", '[[groups]]\n  name = "g"\n  gidnumber = 1\n'),
                       ("no groups", '[[users]]\n  name = "u"\n  uidnumber = 1\n')):
        try:
            tester_idp.validate_users_toml(text)
            expect(f"a directory with {name} is refused", False, "no error raised")
        except ValueError:
            expect(f"a directory with {name} is refused", True)


def test_the_shipped_fixture_is_a_valid_directory() -> None:
    """The default users file is the one nobody passes a flag for."""
    text = tester_idp.DEFAULT_USERS_FILE.read_text(encoding="utf-8")
    users, groups = tester_idp.validate_users_toml(text)
    expect("the shipped fixture parses", len(users) >= 4, f"{users}")
    expect("and declares the group names the engine maps",
           {"dfe-admins", "dfe-viewers", "dfe-analysts"} <= set(groups), f"{groups}")


# --- the fixture against the OIDC role spec ----------------------------------
# One row of the spec's TRUTH_TABLE, single-line or wrapped.
_SPEC_ROW = re.compile(
    r"\{\s*user:\s*'([^']+)',\s*roles:\s*\[([^\]]*)\],\s*orgIds:\s*\[([^\]]*)\],?\s*\}"
)


def spec_truth_table() -> dict[str, tuple[set[str], set[str]]]:
    """user -> (roles, org ids) as oidc-rba.spec.ts asserts them."""
    text = RBA_SPEC.read_text(encoding="utf-8")
    rows = {
        user: (set(re.findall(r"'([^']+)'", roles)), set(re.findall(r"'([^']+)'", orgs)))
        for user, roles, orgs in _SPEC_ROW.findall(text)
    }
    expect("every spec row is parsed", len(rows) == text.count("user: '"), f"{sorted(rows)}")
    return rows


def shipped_resolution() -> dict[str, tuple[set[str], set[str]]]:
    """user -> (roles, org ids) the shipped fixture pair grants.

    Mirrors the engine's claim resolution: a claim value takes the group whose
    source_id it is, for a login through that group's source_provider, and
    grants its roles, its org_ids and the org its scope names. Direct groups
    only, because the tester IdP emits no transitive membership.
    """
    directory = tomllib.loads(
        tester_idp.DEFAULT_USERS_FILE.read_text(encoding="utf-8").replace(
            "{{DFE_FIXTURE_PASS_SHA256}}", "x" * 64
        )
    )
    group_name = {g["gidnumber"]: g["name"] for g in directory["groups"]}
    engine = {
        body["source_id"]: body
        for body in map(
            json.loads,
            tester_idp.render_engine_groups(
                tester_idp.DEFAULT_GROUPS_FILE.read_text(encoding="utf-8")
            ).values(),
        )
        if body.get("source_provider") == tester_idp.DEFAULT_PROVIDER
    }
    out = {}
    for user in directory["users"]:
        roles: set[str] = set()
        orgs: set[str] = set()
        for gid in (user["primarygroup"], *user.get("othergroups", [])):
            group = engine.get(group_name[gid])
            if group is None:
                continue
            roles.update(group["roles"])
            orgs.update(group["org_ids"])
            if group["scope"].startswith("org:"):
                orgs.add(group["scope"].removeprefix("org:"))
        out[user["name"]] = (roles, orgs)
    return out


def test_every_spec_identity_is_in_the_shipped_directory() -> None:
    """A row the directory lacks fails at the IdP login form, not at the assertion."""
    missing = set(spec_truth_table()) - set(shipped_resolution())
    expect("the directory carries every user the spec logs in as", not missing, f"{sorted(missing)}")


def test_the_shipped_fixture_grants_exactly_what_the_spec_asserts() -> None:
    """The spec is the oracle; the in-repo fixture pair must satisfy every row of it."""
    granted = shipped_resolution()
    for user, (roles, orgs) in sorted(spec_truth_table().items()):
        got_roles, got_orgs = granted.get(user, (set(), set()))
        expect(f"{user} resolves the spec's roles", got_roles == roles, f"{got_roles} != {roles}")
        expect(f"{user} resolves the spec's org ids", got_orgs == orgs, f"{got_orgs} != {orgs}")


def test_the_group_map_names_only_directory_groups() -> None:
    """A mapped name the IdP never emits is a typo that grants nothing."""
    _, groups = tester_idp.validate_users_toml(
        tester_idp.DEFAULT_USERS_FILE.read_text(encoding="utf-8")
    )
    mapped = {
        name.removesuffix(".yaml")
        for name in tester_idp.render_engine_groups(
            tester_idp.DEFAULT_GROUPS_FILE.read_text(encoding="utf-8")
        )
    }
    expect("every mapped group is in the directory", mapped <= set(groups), f"{mapped - set(groups)}")
    expect("and only the no-role group is unmapped",
           set(groups) - mapped == {"dfe-nogroup"}, f"{set(groups) - mapped}")


def test_the_group_map_repeats_the_engine_defaults() -> None:
    """A seeded group store never gets the engine's own four, so the map carries them."""
    files = tester_idp.render_engine_groups(tester_idp.DEFAULT_GROUPS_FILE.read_text(encoding="utf-8"))
    defaults = {"dfe-admins": ["admin"], "dfe-analysts": ["data_analyst"],
                "dfe-viewers": ["data_viewer"], "dfe-infra": ["infra_admin"]}
    for name, roles in defaults.items():
        body = json.loads(files.get(f"{name}.yaml", "{}"))
        expect(f"{name} keeps the engine's roles", body.get("roles") == roles, f"{body}")


def test_an_unusable_group_map_is_refused() -> None:
    """Refused before the cluster sees it, by the rules the engine's group model applies."""
    cases = {
        "an org scope with no org": '[[groups]]\n  name = "g"\n  scope = "org:"\n',
        "an unknown scope": '[[groups]]\n  name = "g"\n  scope = "tenant"\n',
        "a path in the name": '[[groups]]\n  name = "../g"\n',
        "roles that are not a list": '[[groups]]\n  name = "g"\n  roles = "admin"\n',
        "a duplicate": '[[groups]]\n  name = "g"\n[[groups]]\n  name = "g"\n',
        "an empty source_id": '[[groups]]\n  name = "g"\n  source_id = " "\n',
        "no groups at all": "",
    }
    for label, text in cases.items():
        try:
            tester_idp.render_engine_groups(text)
            expect(f"{label} is refused", False, "no error raised")
        except ValueError:
            expect(f"{label} is refused", True)


def test_an_org_scoped_group_renders_the_engine_shape() -> None:
    files = tester_idp.render_engine_groups(
        '[[groups]]\n  name = "g"\n  roles = ["org_viewer"]\n'
        '  scope = "org:acme"\n  org_ids = ["acme"]\n'
    )
    body = json.loads(files["g.yaml"])
    expect("keyed by the file name the engine reads", list(files) == ["g.yaml"], f"{list(files)}")
    expect("carrying roles, scope, org_ids and the link",
           body == {"description": "", "scope": "org:acme", "roles": ["org_viewer"],
                    "org_ids": ["acme"], "source_provider": "dex", "source_id": "g"}, f"{body}")


def test_every_shipped_group_is_linked_for_the_provider() -> None:
    """The engine links a claim value by source_id alone, so an unlinked file grants nothing."""
    files = tester_idp.render_engine_groups(tester_idp.DEFAULT_GROUPS_FILE.read_text(encoding="utf-8"))
    for key, text in sorted(files.items()):
        body = json.loads(text)
        expect(f"{key} names the provider", body.get("source_provider") == "dex", f"{body}")
        expect(f"{key} links the name the IdP emits",
               body.get("source_id") == key.removesuffix(".yaml"), f"{body}")


def test_a_provider_and_a_source_id_override_reach_the_file() -> None:
    """Another IdP sends ids, not names: the map says which, and wire-engine says whose."""
    files = tester_idp.render_engine_groups(
        '[[groups]]\n  name = "g"\n  source_id = "00000000-aaaa"\n', "entra"
    )
    body = json.loads(files["g.yaml"])
    expect("the provider is the one given", body["source_provider"] == "entra", f"{body}")
    expect("the id is the entry's own", body["source_id"] == "00000000-aaaa", f"{body}")
    try:
        tester_idp.render_engine_groups('[[groups]]\n  name = "g"\n', "")
        expect("an empty provider is refused", False, "no error raised")
    except ValueError:
        expect("an empty provider is refused", True)


def test_wire_engine_hands_the_group_map_over() -> None:
    """Without it every fixture group past the engine's four defaults grants nothing."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "idp.env").write_text(
        "TESTER_IDP_ISSUER=https://dex.example.com\n"
        "TESTER_IDP_CLIENT_ID=dfe-engine\n"
        "TESTER_IDP_CLIENT_SECRET=clientsecretvalue\n",
        encoding="utf-8", newline="\n",
    )
    args = _ops().build_parser().parse_args(
        ["idp", "wire-engine", "--secrets-file", str(tmp / "idp.env"), "--dry-run",
         "--provider", "tester"]
    )
    saved_out = sys.stdout
    sys.stdout = captured = io.StringIO()
    try:
        rc = tester_idp.cmd_idp_wire_engine(args)
    finally:
        sys.stdout = saved_out
    expect("the dry run succeeds", rc == 0, f"rc={rc}")
    objects = json.loads(captured.getvalue()) if rc == 0 else []
    maps = [o for o in objects if o["metadata"]["name"] == tester_idp.DEFAULT_GROUPS_CONFIGMAP]
    expect("a groups ConfigMap is rendered", len(maps) == 1, f"{[o['metadata']['name'] for o in objects]}")
    data = maps[0]["data"] if maps else {}
    expect("carrying the org-scoped fixture group",
           json.loads(data.get("dfe-test-org-viewers.yaml", "{}")).get("scope") == "org:test_org",
           f"{sorted(data)}")
    admins = json.loads(data.get("dfe-admins.yaml", "{}"))
    expect("linked to the provider the engine registers",
           (admins.get("source_provider"), admins.get("source_id")) == ("tester", "dfe-admins"),
           f"{admins}")
    providers = [o for o in objects if o["metadata"]["name"] == "dfe-oidc-providers"]
    expect("under the same name as the provider file",
           bool(providers) and list(providers[0]["data"]) == ["tester.yaml"],
           f"{[list(o['data']) for o in providers]}")
    expect("and the client secret stays redacted", "clientsecretvalue" not in captured.getvalue())


# --- hostname ----------------------------------------------------------------
def test_the_hostname_is_composed_from_the_domain() -> None:
    ns = argparse.Namespace(hostname=None, domain="dfe.example.com", host_label="dex")
    expect("label + domain", tester_idp.resolve_hostname(ns) == "dex.dfe.example.com")
    ns = argparse.Namespace(hostname="idp.example.com", domain="dfe.example.com", host_label="dex")
    expect("an explicit --hostname wins", tester_idp.resolve_hostname(ns) == "idp.example.com")


def test_neither_domain_nor_hostname_is_refused() -> None:
    ns = argparse.Namespace(hostname=None, domain=None, host_label="dex")
    try:
        tester_idp.resolve_hostname(ns)
        expect("a nameless IdP is refused", False, "no error raised")
    except ValueError:
        expect("a nameless IdP is refused", True)


# --- the deploy, end to end against fake tools -------------------------------
def test_the_deploy_installs_the_pinned_chart_and_routes_it() -> None:
    rc, calls, _ = run_deploy()
    expect("the deploy succeeds", rc == 0, f"rc={rc}")
    helm = [c for c in calls if c["tool"] == "helm" and "upgrade" in c["args"]]
    expect("helm installs dex once", len(helm) == 1, f"{helm}")
    args = helm[0]["args"]
    expect("at the pinned chart version",
           "--version" in args and args[args.index("--version") + 1] == tester_idp.DEX_CHART_VERSION,
           f"{args}")
    kinds = [o["kind"] for o in applied(calls)]
    expect("the two Secrets, glauth and the route are applied",
           kinds == ["Secret", "Secret", "Deployment", "Service", "HTTPRoute"], f"{kinds}")


def test_the_deploy_waits_for_glauth_before_installing_dex() -> None:
    """dex resolves its connector at startup; a glauth that is not listening yet
    leaves dex up and unable to authenticate anyone."""
    _, calls, _ = run_deploy()
    order = [
        i for i, c in enumerate(calls)
        if (c["tool"] == "kubectl" and "rollout" in c["args"])
        or (c["tool"] == "helm" and "upgrade" in c["args"])
    ]
    expect("the rollout wait comes first", len(order) == 2 and order[0] < order[1], f"{order}")


def test_no_generated_password_is_ever_an_argument() -> None:
    """argv is world-readable in /proc for the life of the process."""
    _, calls, env_file = run_deploy()
    secrets = {
        ln.split("=", 1)[1]
        for ln in env_file.read_text(encoding="utf-8").splitlines()
        if ln.startswith(("TESTER_IDP_CLIENT_SECRET=", "TESTER_IDP_USER_PASSWORD=",
                          "TESTER_IDP_SEARCH_PASSWORD="))
    }
    expect("three secrets were generated", len(secrets) == 3, f"{len(secrets)}")
    argv = " ".join(a for c in calls for a in c["args"])
    leaked = [s for s in secrets if s in argv]
    expect("none of them appears in any argv", leaked == [], "a generated secret was on argv")


def test_the_secrets_file_is_private() -> None:
    _, _, env_file = run_deploy()
    mode = stat.S_IMODE(env_file.stat().st_mode)
    expect("the secrets file is 0600", mode == 0o600, oct(mode))


def test_a_helm_timeout_still_leaves_the_credentials_on_disk() -> None:
    """The apply puts dex-secrets in the cluster, so helm is not the first writer.

    A `helm upgrade --wait` that times out raises SystemExit, and with the file
    written afterwards the run left live credentials nobody had a copy of.
    """
    raised, env_file = run_failing_deploy("helm")
    expect("the helm failure still stops the deploy", raised is not None, f"{raised}")
    expect("but the secrets file exists", env_file.is_file(), f"{env_file}")
    if env_file.is_file():
        env = dict(
            ln.split("=", 1)
            for ln in env_file.read_text(encoding="utf-8").splitlines()
            if "=" in ln and not ln.startswith("#")
        )
        expect(
            "carrying the client secret the cluster now holds",
            len(env.get("TESTER_IDP_CLIENT_SECRET", "")) >= 32,
            f"{sorted(env)}",
        )
        expect(
            "and it is still 0600",
            stat.S_IMODE(env_file.stat().st_mode) == 0o600,
            oct(stat.S_IMODE(env_file.stat().st_mode)),
        )


def test_reuse_secrets_on_an_older_file_is_an_input_error() -> None:
    """A file written before a key existed used to raise a bare KeyError."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "users.toml").write_text(USERS, encoding="utf-8", newline="\n")
    (tmp / "idp.env").write_text(
        "TESTER_IDP_ISSUER=https://dex.example.com\n", encoding="utf-8", newline="\n"
    )
    args = deploy_args(tmp, reuse_secrets=True)
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = captured = io.StringIO()
    try:
        rc = tester_idp.cmd_idp_deploy(args)
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
    expect("it returns the input-error code", rc == 2, f"rc={rc}")
    expect(
        "naming the key that is missing",
        "TESTER_IDP_CLIENT_SECRET" in captured.getvalue(),
        captured.getvalue(),
    )


def test_the_generated_password_matches_the_hash_in_the_cluster_secret() -> None:
    """The file the caller logs in with has to be the one glauth will accept."""
    _, calls, env_file = run_deploy()
    env = dict(
        ln.split("=", 1) for ln in env_file.read_text(encoding="utf-8").splitlines() if "=" in ln
    )
    cfg = next(
        o for o in applied(calls)
        if o["kind"] == "Secret" and o["metadata"]["name"] == "glauth-config"
    )["stringData"]["glauth.cfg"]
    expect(
        "the user password hashes to what glauth stores",
        hashlib.sha256(env["TESTER_IDP_USER_PASSWORD"].encode()).hexdigest() in cfg,
    )
    expect("and the plaintext is not in the Secret", env["TESTER_IDP_USER_PASSWORD"] not in cfg)


def test_the_dry_run_touches_nothing() -> None:
    rc, calls, _ = run_deploy(dry_run=True)
    expect("the dry run succeeds", rc == 0, f"rc={rc}")
    expect("and ran no helm or kubectl at all", calls == [], f"{calls}")


def test_a_dry_run_redacts_a_hash_outside_the_passsha256_lines() -> None:
    """The placeholder is substituted everywhere, so a comment naming it carries the hash."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "users.toml").write_text(
        "# the deploy fills {{DFE_FIXTURE_PASS_SHA256}} in\n" + USERS,
        encoding="utf-8", newline="\n",
    )
    args = deploy_args(tmp, dry_run=True)
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), (captured := io.StringIO())
    try:
        rc = tester_idp.cmd_idp_deploy(args)
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
    expect("the dry run succeeds", rc == 0, f"rc={rc}")
    leaked = re.findall(r"\b[0-9a-f]{64}\b", captured.getvalue())
    expect("no password hash reaches the printed glauth.cfg", leaked == [], f"{len(leaked)} hash(es)")


def test_a_dry_run_never_prints_a_secret_value() -> None:
    """A dry run gets pasted into terminals and captured in CI logs."""
    objects = [
        tester_idp.render_secret(
            "creds", "dfe", {"client-secret": "s3cr3tvalue", "client-id": "idvalue"}
        ),
        {"kind": "ConfigMap", "data": {"note": "configmapvalue"}},
    ]
    out = json.dumps(tester_idp._redacted(objects))
    expect("the secret value is gone", "s3cr3tvalue" not in out, out)
    expect("and so is every other value in it", "idvalue" not in out, out)
    expect("but the keys survive", "client-secret" in out and "client-id" in out, out)
    expect("and non-Secret objects are untouched", "configmapvalue" in out, out)


def test_the_cli_still_parses() -> None:
    out = subprocess.run(
        [sys.executable, str(DFE_OPS), "idp", "deploy", "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    expect("--help works", out.returncode == 0, out.stderr)
    expect(
        "and names the flags the tool is parameterised on",
        all(f in out.stdout for f in ("--gateway", "--redirect-uri", "--users-file", "--domain")),
        out.stdout,
    )
    wire = subprocess.run(
        [sys.executable, str(DFE_OPS), "idp", "wire-engine", "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    expect("wire-engine --help works", wire.returncode == 0, wire.stderr)
    expect(
        "and names the group map flags",
        all(f in wire.stdout for f in ("--groups-file", "--groups-configmap")),
        wire.stdout,
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
