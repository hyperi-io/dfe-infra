#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_external_idp.py
#  Purpose:      Prove `dfe-ops idp wire-external` renders provider and group files
#                the engine accepts, keeps the client secret off every argv and
#                out of every printout, and that --teardown removes exactly what
#                the wire added. `wire-engine --teardown` shares the volume cleanup,
#                so its cases sit here beside the same fake tools.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/external_idp.py, the `dfe-ops idp wire-external` action.

A fake kubectl first on PATH keeps ConfigMaps and Secrets in a JSON file and
answers get, create, patch, apply and delete against it, so a wire followed by a
teardown can be checked for what is left. A fake docker runs the in-container
edit script against a temp directory standing in for the engine's config volume.

    python3 scripts/tests/test_external_idp.py

No third-party deps and no test runner, matching the tool it tests.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
DFE_OPS = SCRIPTS / "dfe-ops"
GROUPS_DIR = Path(__file__).resolve().parent / "fixtures" / "external-idp" / "engine-groups"

sys.path.insert(0, str(SCRIPTS))
import external_idp  # noqa: E402

# A value no rendered file, argv or printout may ever carry.
SECRET = "Synthetic-Secret.Value~42"
SECRET_ENV = "TEST_IDP_CLIENT_SECRET"

FAKE_KUBECTL = r"""#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
body = sys.stdin.read() if "-" in args or "exec" in args else ""
with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": "kubectl", "args": args, "stdin": body}) + "\n")
path = os.environ["FAKE_STATE"]
state = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
rest = list(args)
for flag in ("--kubeconfig", "--context", "-n"):
    while flag in rest:
        i = rest.index(flag)
        del rest[i:i + 2]
verb = rest[0]


def save():
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def merge(target, patch):
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict):
            merge(target.setdefault(key, {}), value)
        else:
            target[key] = value


if verb == "get":
    obj = state.get(f"{rest[1]}/{rest[2]}")
    if obj is None:
        print(f'Error from server (NotFound): {rest[1]}s "{rest[2]}" not found', file=sys.stderr)
        sys.exit(1)
    print(json.dumps(obj))
elif verb in ("create", "apply"):
    doc = json.loads(body)
    for obj in doc["items"] if doc.get("kind") == "List" else [doc]:
        key = f"{obj['kind'].lower()}/{obj['metadata']['name']}"
        if verb == "create" and key in state:
            print("AlreadyExists", file=sys.stderr)
            sys.exit(1)
        state[key] = obj
    save()
elif verb == "patch":
    obj = state.get(f"{rest[1]}/{rest[2]}")
    if obj is None:
        print("Error from server (NotFound)", file=sys.stderr)
        sys.exit(1)
    merge(obj, json.loads(rest[rest.index("-p") + 1]))
    if obj.get("data") == {}:
        del obj["data"]
    if obj["metadata"].get("annotations") == {}:
        del obj["metadata"]["annotations"]
    save()
elif verb == "delete":
    state.pop(f"{rest[1]}/{rest[2]}", None)
    save()
elif verb == "exec":
    sys.exit(int(os.environ.get("FAKE_EXEC_RC", "0")))
"""

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, subprocess, sys

args = sys.argv[1:]
body = sys.stdin.read() if args[:1] == ["exec"] else ""
with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": "docker", "args": args, "stdin": body, "cwd": os.getcwd()}) + "\n")
if args[:1] == ["exec"]:
    plan = json.loads(body)
    plan["root"] = os.environ["FAKE_ROOT"] + plan["root"]
    script = args[args.index("-c") + 1]
    done = subprocess.run([sys.executable, "-c", script], input=json.dumps(plan), text=True,
                          capture_output=True, check=False)
    sys.exit(done.returncode)
"""


class FakeTools:
    """A temp dir holding fake kubectl and docker, their call log and their state."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.calls = self.dir / "calls.jsonl"
        self.state = self.dir / "state.json"
        self.root = self.dir / "volume"
        self.compose = self.dir / "compose"
        (self.compose / "env").mkdir(parents=True)
        for tool, body in (("kubectl", FAKE_KUBECTL), ("docker", FAKE_DOCKER)):
            path = self.dir / tool
            path.write_text(body, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        self.env = {
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_CALLS": str(self.calls),
            "FAKE_STATE": str(self.state),
            "FAKE_ROOT": str(self.root),
        }

    def called(self) -> list[dict]:
        if not self.calls.exists():
            return []
        lines = self.calls.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line]

    def objects(self) -> dict:
        if not self.state.exists():
            return {}
        return json.loads(self.state.read_text(encoding="utf-8"))

    def seed(self, objects: dict) -> None:
        self.state.write_text(json.dumps(objects), encoding="utf-8")

    def __enter__(self) -> FakeTools:
        return self

    def __exit__(self, *_exc) -> None:
        self._tmp.cleanup()


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


def run(
    tools: FakeTools, argv: list[str], *, secret: str | None = SECRET, action: str = "wire-external"
) -> tuple[int, str]:
    """Parse with dfe-ops' own parser and run it. Returns (rc, everything printed)."""
    args = _ops().build_parser().parse_args(["idp", action, *argv])
    saved_env = dict(os.environ)
    out = io.StringIO()
    os.environ.update(tools.env)
    if secret is None:
        os.environ.pop(SECRET_ENV, None)
    else:
        os.environ[SECRET_ENV] = secret
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                rc = args.func(args)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
    return rc, out.getvalue()


def wire_args(idp_type: str = "okta", *extra: str) -> list[str]:
    return [
        "--type",
        idp_type,
        "--issuer",
        "https://idp.example.com",
        "--client-id",
        "synthetic-client-id",
        "--client-secret-env",
        SECRET_ENV,
        "--groups-dir",
        str(GROUPS_DIR),
        *extra,
    ]


def k8s(*extra: str) -> list[str]:
    return ["--target", "k8s", "--namespace", "dfe-test", *extra]


def docker(tools: FakeTools, *extra: str) -> list[str]:
    return ["--target", "docker", "--compose-dir", str(tools.compose), *extra]


def configmap(tools: FakeTools, name: str) -> dict:
    return tools.objects().get(f"configmap/{name}", {})


def provider_file(tools: FakeTools, provider: str) -> dict:
    data = configmap(tools, "dfe-oidc-providers").get("data", {})
    return json.loads(data[f"{provider}.yaml"])


# --- provider rendering, per type ---------------------------------------------
def test_okta_reads_the_groups_claim_and_keeps_the_engine_scopes() -> None:
    with FakeTools() as tools:
        rc, _ = run(tools, [*wire_args("okta"), *k8s()])
        body = provider_file(tools, "okta")
    expect("okta wire succeeds", rc == 0, str(rc))
    expect("okta is type okta", body["type"] == "okta", str(body))
    expect(
        "okta reads the groups claim",
        body["groups"] == {"mode": "token_claim", "claim_name": "groups"},
        str(body),
    )
    expect("okta leaves scopes to the engine default", "scopes" not in body, str(body))
    expect("the client id is held in the clear", body["client_id"] == "synthetic-client-id")
    expect(
        "the engine reads the secret from DFE_OIDC_OKTA_CLIENT_SECRET",
        body["client_secret_env"] == "DFE_OIDC_OKTA_CLIENT_SECRET",
        str(body),
    )
    expect("the provider name is never a field", "name" not in body, str(body))


def test_entra_registers_as_entra_id_under_the_name_entra() -> None:
    with FakeTools() as tools:
        rc, _ = run(tools, [*wire_args("entra_id"), *k8s()])
        body = provider_file(tools, "entra")
        groups = configmap(tools, "dfe-auth-groups").get("data", {})
    expect("entra wire succeeds", rc == 0, str(rc))
    expect("entra is type entra_id", body["type"] == "entra_id", str(body))
    expect("entra reads the groups claim", body["groups"]["mode"] == "token_claim", str(body))
    expect(
        "entra takes only the entra- group files",
        sorted(groups) == ["entra-dfe-admins.yaml"],
        str(sorted(groups)),
    )


def test_entra_records_its_tenant_for_the_overage_lookup() -> None:
    tenant = "00000000-0000-0000-0000-0000000000aa"
    argv = [*wire_args("entra_id"), *k8s()]
    argv[argv.index("https://idp.example.com")] = f"https://login.example.com/{tenant}/v2.0"
    with FakeTools() as tools:
        rc, _ = run(tools, argv)
        derived = provider_file(tools, "entra")["groups"]
        rc_flag, _ = run(tools, [*wire_args("entra_id", "--tenant-id", tenant.upper()), *k8s()])
        explicit = provider_file(tools, "entra")["groups"]
        rc_bad, _ = run(tools, [*wire_args("okta", "--tenant-id", tenant), *k8s()])
    expect("entra wire with a tenant issuer succeeds", rc == 0, str(rc))
    expect(
        "the tenant comes from the issuer path", derived.get("tenant_id") == tenant, str(derived)
    )
    expect(
        "--tenant-id wins",
        rc_flag == 0 and explicit.get("tenant_id") == tenant.upper(),
        str(explicit),
    )
    expect("--tenant-id is refused for okta", rc_bad == 2, str(rc_bad))


def test_google_runs_in_api_mode_with_enrich_on_login() -> None:
    with FakeTools() as tools:
        rc, _ = run(tools, [*wire_args("google"), *k8s()])
        body = provider_file(tools, "google-workspace")
    expect("google wire succeeds", rc == 0, str(rc))
    expect(
        "google is the one api-mode shape the engine accepts",
        body["groups"] == {"mode": "api", "enrich_on_login": True},
        str(body),
    )
    expect("google never carries a scopes override", "scopes" not in body, str(body))


def test_google_refuses_a_scopes_override() -> None:
    with FakeTools() as tools:
        rc, out = run(tools, [*wire_args("google", "--scopes", "openid email"), *k8s()])
        calls = tools.called()
    expect("google --scopes is an input error", rc == 2, str(rc))
    expect("the refusal names the reason", "refused for google" in out, out)
    expect("a refused run touches nothing", calls == [], str(calls))


def test_a_scopes_override_must_carry_openid() -> None:
    with FakeTools() as tools:
        bad, _ = run(tools, [*wire_args("okta", "--scopes", "email groups"), *k8s()])
        good, _ = run(tools, [*wire_args("okta", "--scopes", "openid email groups"), *k8s()])
        body = provider_file(tools, "okta")
    expect("scopes without openid are refused", bad == 2, str(bad))
    expect(
        "scopes with openid are written",
        good == 0 and body["scopes"] == "openid email groups",
        str(body),
    )


def test_an_http_issuer_is_refused() -> None:
    argv = [*wire_args("okta"), *k8s()]
    argv[argv.index("https://idp.example.com")] = "http://idp.example.com"
    with FakeTools() as tools:
        rc, _ = run(tools, argv)
    expect("a plain-http issuer is an input error", rc == 2, str(rc))


def test_a_provider_name_outside_the_dns_label_rule_is_refused() -> None:
    with FakeTools() as tools:
        rc, _ = run(tools, [*wire_args("okta", "--provider", "Okta_Prod"), *k8s()])
    expect("an uppercase provider name is refused", rc == 2, str(rc))


# --- group files --------------------------------------------------------------
def test_only_the_providers_own_group_files_are_taken() -> None:
    files = external_idp.render_group_files(GROUPS_DIR, "okta")
    expect(
        "okta takes its three files",
        sorted(files)
        == [
            "okta-dfe-admins.yaml",
            "okta-dfe-nogroup.yaml",
            "okta-dfe-test-org-viewers.yaml",
        ],
        str(sorted(files)),
    )


def test_each_group_file_links_to_its_provider_and_claim_value() -> None:
    files = external_idp.render_group_files(GROUPS_DIR, "okta")
    admins = json.loads(files["okta-dfe-admins.yaml"])
    org = json.loads(files["okta-dfe-test-org-viewers.yaml"])
    nogroup = json.loads(files["okta-dfe-nogroup.yaml"])
    expect("a block list reads as roles", admins["roles"] == ["admin"], str(admins))
    expect("source_provider is the provider", admins["source_provider"] == "okta", str(admins))
    expect("an explicit source_id is kept", admins["source_id"] == "dfe-admins", str(admins))
    expect(
        "an indented block list and an org scope read",
        org["roles"] == ["org_viewer"]
        and org["org_ids"] == ["test_org"]
        and org["scope"] == "org:test_org",
        str(org),
    )
    expect(
        "a quoted description loses its quotes",
        org["description"] == "Synthetic fixture group, org viewer",
        str(org),
    )
    expect("an empty flow list is no roles", nogroup["roles"] == [], str(nogroup))
    expect(
        "a missing source_id defaults to the stem after the prefix",
        nogroup["source_id"] == "dfe-nogroup",
        str(nogroup),
    )


def test_a_recorded_id_and_a_flow_list_read() -> None:
    entra = json.loads(
        external_idp.render_group_files(GROUPS_DIR, "entra")["entra-dfe-admins.yaml"]
    )
    expect("a flow list reads", entra["roles"] == ["admin", "data_viewer"], str(entra))
    expect(
        "a recorded object id is the source_id",
        entra["source_id"] == "00000000-0000-0000-0000-000000000001",
        str(entra),
    )


def test_a_json_group_file_reads() -> None:
    dex = json.loads(external_idp.render_group_files(GROUPS_DIR, "dex")["dex-dfe-admins.yaml"])
    expect(
        "a JSON group file reads",
        dex["roles"] == ["admin"] and dex["source_id"] == "dfe-admins",
        str(dex),
    )


def _refused(body: str, provider: str = "okta") -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / f"{provider}-bad.yaml").write_text(body, encoding="utf-8")
        try:
            external_idp.render_group_files(Path(tmp), provider)
        except ValueError:
            return True
    return False


def test_unusable_group_files_are_refused() -> None:
    expect(
        "another provider's link is refused", _refused("roles: [admin]\nsource_provider: entra\n")
    )
    expect("a field the group model lacks is refused", _refused("roles: [admin]\nmembers: [x]\n"))
    expect("a nested mapping is refused", _refused("roles: [admin]\nattributes:\n  a: b\n"))
    expect("a bad scope is refused", _refused("roles: [admin]\nscope: tenant\n"))
    expect("a non-list roles value is refused", _refused("roles: admin\n"))
    expect("a repeated key is refused", _refused("roles: [admin]\nroles: [viewer]\n"))
    expect("a JSON list is refused", _refused("[1, 2]\n"))
    with tempfile.TemporaryDirectory() as tmp:
        try:
            external_idp.render_group_files(Path(tmp), "okta")
            empty_refused = False
        except ValueError:
            empty_refused = True
    expect("a directory with no file for the provider is refused", empty_refused)


# --- the client secret --------------------------------------------------------
def test_a_secret_value_on_argv_is_refused_without_echo() -> None:
    with FakeTools() as tools:
        rc, out = run(tools, ["--type", "okta", "--client-secret", SECRET, *k8s()])
        calls = tools.called()
    expect("--client-secret is an input error", rc == 2, str(rc))
    expect("the refusal does not echo the value", SECRET not in out, out)
    expect("the refusal points at --client-secret-env", "--client-secret-env" in out, out)
    expect("a refused run touches nothing", calls == [], str(calls))


def test_a_value_passed_where_a_name_belongs_is_refused_without_echo() -> None:
    argv = [*wire_args("okta"), *k8s()]
    argv[argv.index(SECRET_ENV)] = SECRET
    with FakeTools() as tools:
        rc, out = run(tools, argv)
    expect("a value as --client-secret-env is an input error", rc == 2, str(rc))
    expect("that refusal does not echo it either", SECRET not in out, out)


def test_an_unset_secret_variable_is_refused() -> None:
    with FakeTools() as tools:
        rc, out = run(tools, [*wire_args("okta"), *k8s()], secret=None)
        calls = tools.called()
    expect("an unset variable is an input error", rc == 2, str(rc))
    expect("an unset variable touches nothing", calls == [], str(calls))
    expect("the refusal says why", "unset or empty" in out, out)


def test_the_secret_reaches_k8s_on_stdin_only() -> None:
    with FakeTools() as tools:
        rc, out = run(tools, [*wire_args("okta"), *k8s()])
        calls = tools.called()
        secret = tools.objects()["secret/dfe-oidc-okta"]
        cms = [configmap(tools, "dfe-oidc-providers"), configmap(tools, "dfe-auth-groups")]
    expect("wire succeeds", rc == 0, out)
    expect("no argv carries the secret", not any(SECRET in " ".join(c["args"]) for c in calls))
    expect("nothing printed carries the secret", SECRET not in out)
    expect("the Secret holds it", secret["stringData"] == {"client-secret": SECRET}, str(secret))
    expect("no ConfigMap holds it", SECRET not in json.dumps(cms))


def test_the_secret_reaches_docker_through_the_env_file_only() -> None:
    with FakeTools() as tools:
        rc, out = run(tools, [*wire_args("okta"), *docker(tools)])
        calls = tools.called()
        env_file = tools.compose / "env" / "engine.env"
        mode = stat.S_IMODE(env_file.stat().st_mode)
        text = env_file.read_text(encoding="utf-8")
        written = [p.read_text(encoding="utf-8") for p in tools.root.rglob("*.yaml")]
    expect("docker wire succeeds", rc == 0, out)
    expect(
        "no docker argv carries the secret",
        not any(SECRET in " ".join(c["args"]) or SECRET in c["stdin"] for c in calls),
    )
    expect("nothing printed carries the secret", SECRET not in out)
    expect("the env file carries it", f"DFE_OIDC_OKTA_CLIENT_SECRET={SECRET}" in text, text)
    expect("the env file is private", mode == 0o600, oct(mode))
    expect("no written file carries it", not any(SECRET in body for body in written))


def test_a_dry_run_touches_nothing_and_redacts_the_secret() -> None:
    for target in ("k8s", "docker"):
        with FakeTools() as tools:
            argv = [*wire_args("okta", "--dry-run"), *(k8s() if target == "k8s" else docker(tools))]
            rc, out = run(tools, argv)
            calls = tools.called()
        expect(f"{target} dry run succeeds", rc == 0, out)
        expect(f"{target} dry run touches nothing", calls == [], str(calls))
        expect(f"{target} dry run never prints the secret", SECRET not in out)
        expect(f"{target} dry run shows the provider file", "okta.yaml" in out, out)


# --- k8s wire and teardown ----------------------------------------------------
def test_k8s_wire_records_a_ledger_before_teardown_reads_it() -> None:
    with FakeTools() as tools:
        run(tools, [*wire_args("okta"), *k8s()])
        annotations = configmap(tools, "dfe-oidc-providers")["metadata"]["annotations"]
    ledger = json.loads(annotations["dfe.hyperi.io/wire-external-okta"])
    expect("the ledger names the Secret", ledger["holder"] == "dfe-oidc-okta", str(ledger))
    expect(
        "the ledger names every group key",
        ledger["groups"]
        == [
            "okta-dfe-admins.yaml",
            "okta-dfe-nogroup.yaml",
            "okta-dfe-test-org-viewers.yaml",
        ],
        str(ledger),
    )


def test_k8s_teardown_removes_exactly_what_the_wire_added() -> None:
    existing = {
        "configmap/dfe-oidc-providers": {
            "kind": "ConfigMap",
            "metadata": {"name": "dfe-oidc-providers"},
            "data": {"dex.yaml": "{}"},
        },
    }
    with FakeTools() as tools:
        tools.seed(existing)
        run(tools, [*wire_args("okta"), *k8s()])
        run(tools, [*wire_args("entra_id"), *k8s()])
        rc, out = run(tools, ["--type", "okta", "--teardown", *k8s()], secret=None)
        objects = tools.objects()
        execs = [c for c in tools.called() if "exec" in c["args"]]
    providers = objects["configmap/dfe-oidc-providers"]
    groups = objects["configmap/dfe-auth-groups"]
    expect("teardown succeeds", rc == 0, out)
    expect(
        "the okta provider key is gone, dex and entra stay",
        sorted(providers["data"]) == ["dex.yaml", "entra.yaml"],
        str(providers),
    )
    expect(
        "the okta ledger is gone, entra's stays",
        list(providers["metadata"]["annotations"]) == ["dfe.hyperi.io/wire-external-entra"],
        str(providers["metadata"]),
    )
    expect(
        "only the okta group keys are gone",
        sorted(groups["data"]) == ["entra-dfe-admins.yaml"],
        str(groups),
    )
    expect("the okta Secret is gone", "secret/dfe-oidc-okta" not in objects, str(objects))
    expect("the entra Secret stays", "secret/dfe-oidc-entra" in objects, str(objects))
    removed = json.loads(execs[-1]["stdin"])["remove"] if execs else []
    expect(
        "the seeded copies are removed from the engine's config volume",
        removed
        == [
            "auth/oidc-providers/okta.yaml",
            "auth/groups/okta-dfe-admins.yaml",
            "auth/groups/okta-dfe-nogroup.yaml",
            "auth/groups/okta-dfe-test-org-viewers.yaml",
        ],
        str(removed),
    )
    expect(
        "the copies are removed under the chart's config mount",
        execs and json.loads(execs[-1]["stdin"])["root"] == "/config",
        str(execs),
    )


def test_k8s_teardown_deletes_only_configmaps_it_created_once_empty() -> None:
    with FakeTools() as tools:
        run(tools, [*wire_args("okta"), *k8s()])
        rc, _ = run(tools, ["--type", "okta", "--teardown", *k8s()], secret=None)
        objects = tools.objects()
    expect("teardown succeeds", rc == 0, str(rc))
    expect("both ConfigMaps the wire created are gone once empty", objects == {}, str(objects))


def test_k8s_rewire_removes_a_group_the_new_set_drops() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("okta-dfe-admins.yaml", "okta-dfe-nogroup.yaml"):
            (Path(tmp) / name).write_text(
                (GROUPS_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
        with FakeTools() as tools:
            run(tools, [*wire_args("okta"), *k8s()])
            argv = [*wire_args("okta"), *k8s()]
            argv[argv.index(str(GROUPS_DIR))] = tmp
            rc, _ = run(tools, argv)
            groups = configmap(tools, "dfe-auth-groups")["data"]
    expect("rewire succeeds", rc == 0, str(rc))
    expect(
        "the dropped group key is removed",
        sorted(groups) == ["okta-dfe-admins.yaml", "okta-dfe-nogroup.yaml"],
        str(groups),
    )


def test_k8s_teardown_with_nothing_recorded_changes_nothing() -> None:
    with FakeTools() as tools:
        rc, out = run(tools, ["--type", "okta", "--teardown", *k8s()], secret=None)
        writes = [c for c in tools.called() if "get" not in c["args"]]
    expect("an unrecorded teardown succeeds", rc == 0, out)
    expect("it only reads", writes == [], str(writes))


def test_k8s_teardown_reports_a_failed_volume_cleanup() -> None:
    with FakeTools() as tools:
        run(tools, [*wire_args("okta"), *k8s()])
        tools.env["FAKE_EXEC_RC"] = "1"
        rc, out = run(tools, ["--type", "okta", "--teardown", *k8s()], secret=None)
    expect("a failed cleanup exits non-zero", rc == 1, str(rc))
    expect("it names the files left behind", "auth/oidc-providers/okta.yaml" in out, out)


# --- wire-engine teardown, through the same volume cleanup ---------------------
GROUP_MAP = '[[groups]]\n  name = "dfe-admins"\n\n[[groups]]\n  name = "dfe-viewers"\n'


def engine_teardown_args(tmp: Path, *extra: str) -> list[str]:
    """wire-engine's flags for a two-group fixture map written under `tmp`."""
    groups = tmp / "groups.toml"
    groups.write_text(GROUP_MAP, encoding="utf-8")
    return ["--namespace", "dfe-test", "--groups-file", str(groups), *extra]


def engine_objects() -> dict:
    """What a wire-engine run leaves, plus a neighbour of each kind it must not touch."""
    return {
        "configmap/dfe-oidc-providers": {
            "kind": "ConfigMap",
            "metadata": {"name": "dfe-oidc-providers"},
            "data": {"dex.yaml": "{}", "okta.yaml": "{}"},
        },
        "configmap/dfe-auth-groups": {
            "kind": "ConfigMap",
            "metadata": {"name": "dfe-auth-groups"},
            "data": {"dfe-admins.yaml": "{}", "dfe-viewers.yaml": "{}", "okta-ops.yaml": "{}"},
        },
        "secret/dfe-oidc-dex": {"kind": "Secret", "metadata": {"name": "dfe-oidc-dex"}},
        "secret/dfe-oidc-okta": {"kind": "Secret", "metadata": {"name": "dfe-oidc-okta"}},
    }


def test_wire_engine_teardown_removes_exactly_the_files_it_seeded() -> None:
    with tempfile.TemporaryDirectory() as tmp, FakeTools() as tools:
        tools.seed(engine_objects())
        rc, out = run(
            tools,
            ["--teardown", *engine_teardown_args(Path(tmp))],
            secret=None,
            action="wire-engine",
        )
        objects = tools.objects()
        execs = [c for c in tools.called() if "exec" in c["args"]]
    plan = json.loads(execs[0]["stdin"]) if execs else {}
    expect("teardown succeeds", rc == 0, out)
    expect("the volume is cleaned once", len(execs) == 1, str(execs))
    expect(
        "each seeded file is removed and nothing else",
        plan.get("remove")
        == [
            "auth/oidc-providers/dex.yaml",
            "auth/groups/dfe-admins.yaml",
            "auth/groups/dfe-viewers.yaml",
        ],
        str(plan),
    )
    expect(
        "under the chart's config mount, writing nothing",
        (plan.get("root"), plan.get("write")) == ("/config", {}),
        str(plan),
    )
    expect(
        "the engine pod is the one reached",
        "deploy/dfe-engine" in (execs[0]["args"] if execs else []),
        str(execs),
    )
    expect(
        "the provider key is gone from the ConfigMap, a neighbour stays",
        sorted(objects["configmap/dfe-oidc-providers"]["data"]) == ["okta.yaml"],
        str(objects),
    )
    expect(
        "the group keys are gone, a neighbour stays",
        sorted(objects["configmap/dfe-auth-groups"]["data"]) == ["okta-ops.yaml"],
        str(objects),
    )
    expect("the Secret it applied is gone", "secret/dfe-oidc-dex" not in objects, str(objects))
    expect("another provider's Secret stays", "secret/dfe-oidc-okta" in objects, str(objects))


def test_wire_engine_teardown_undoes_a_wire() -> None:
    with tempfile.TemporaryDirectory() as tmp, FakeTools() as tools:
        env = Path(tmp) / "idp.env"
        env.write_text(
            "TESTER_IDP_ISSUER=https://dex.example.com\nTESTER_IDP_CLIENT_ID=dfe-engine\n"
            "TESTER_IDP_CLIENT_SECRET=synthetic-client-value\n",
            encoding="utf-8",
        )
        common = engine_teardown_args(Path(tmp))
        wired, _ = run(
            tools, ["--secrets-file", str(env), *common], secret=None, action="wire-engine"
        )
        seeded = sorted(configmap(tools, "dfe-oidc-providers").get("data", {}))
        rc, out = run(tools, ["--teardown", *common], secret=None, action="wire-engine")
        objects = tools.objects()
        execs = [c for c in tools.called() if "exec" in c["args"]]
    expect("the wire succeeds", wired == 0, str(wired))
    expect("it wrote the provider key", seeded == ["dex.yaml"], str(seeded))
    expect("the teardown succeeds", rc == 0, out)
    expect(
        "no key, no Secret and no copy is left",
        not objects["configmap/dfe-oidc-providers"].get("data")
        and not objects["configmap/dfe-auth-groups"].get("data")
        and "secret/dfe-oidc-dex" not in objects
        and len(execs) == 1,
        str(objects),
    )


def test_wire_engine_teardown_reports_a_failed_volume_cleanup() -> None:
    with tempfile.TemporaryDirectory() as tmp, FakeTools() as tools:
        tools.seed(engine_objects())
        tools.env["FAKE_EXEC_RC"] = "1"
        rc, out = run(
            tools,
            ["--teardown", *engine_teardown_args(Path(tmp))],
            secret=None,
            action="wire-engine",
        )
    expect("a failed cleanup exits non-zero", rc == 1, str(rc))
    expect("it names the files left behind", "auth/oidc-providers/dex.yaml" in out, out)


def test_wire_engine_teardown_dry_run_touches_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp, FakeTools() as tools:
        rc, out = run(
            tools,
            ["--teardown", "--dry-run", *engine_teardown_args(Path(tmp))],
            secret=None,
            action="wire-engine",
        )
        calls = tools.called()
    expect("the dry run succeeds", rc == 0, out)
    expect("it names the copies it would remove", "auth/groups/dfe-viewers.yaml" in out, out)
    expect("and calls nothing", calls == [], str(calls))


# --- docker wire and teardown -------------------------------------------------
def test_docker_wire_writes_the_files_and_recreates_the_engine() -> None:
    with FakeTools() as tools:
        rc, out = run(tools, [*wire_args("okta"), *docker(tools)])
        config = tools.root / "app" / "config" / "auth"
        provider = json.loads((config / "oidc-providers" / "okta.yaml").read_text("utf-8"))
        groups = sorted(p.name for p in (config / "groups").iterdir())
        compose = [c for c in tools.called() if c["args"][:1] == ["compose"]]
    expect("docker wire succeeds", rc == 0, out)
    expect("the provider file lands in the engine's config dir", provider["type"] == "okta")
    expect(
        "the group files land beside it",
        groups
        == [
            "okta-dfe-admins.yaml",
            "okta-dfe-nogroup.yaml",
            "okta-dfe-test-org-viewers.yaml",
        ],
        str(groups),
    )
    expect(
        "the engine is recreated, which rereads env_file",
        compose
        and compose[0]["args"]
        == ["compose", "up", "-d", "--no-deps", "--force-recreate", "dfe-engine"],
        str(compose),
    )
    expect(
        "compose runs in the project directory",
        compose and compose[0]["cwd"] == str(tools.compose),
        str(compose),
    )


def test_docker_teardown_removes_exactly_what_the_wire_added() -> None:
    with FakeTools() as tools:
        env_file = tools.compose / "env" / "engine.env"
        env_file.write_text("DFE_ENV=dev\n# DFE_OIDC_OKTA_CLIENT_SECRET=\n", encoding="utf-8")
        keeper = tools.root / "app" / "config" / "auth" / "groups" / "dfe-admins.yaml"
        keeper.parent.mkdir(parents=True)
        keeper.write_text("{}", encoding="utf-8")
        run(tools, [*wire_args("okta"), *docker(tools)])
        rc, out = run(tools, ["--type", "okta", "--teardown", *docker(tools)], secret=None)
        left = sorted(str(p.relative_to(tools.root)) for p in tools.root.rglob("*.yaml"))
        text = env_file.read_text(encoding="utf-8")
    expect("docker teardown succeeds", rc == 0, out)
    expect(
        "only the files the wire added are gone",
        left == ["app/config/auth/groups/dfe-admins.yaml"],
        str(left),
    )
    expect(
        "the env file is back to its own lines",
        text == "DFE_ENV=dev\n# DFE_OIDC_OKTA_CLIENT_SECRET=\n",
        repr(text),
    )


def test_docker_refuses_a_secret_variable_the_env_file_already_sets() -> None:
    with FakeTools() as tools:
        env_file = tools.compose / "env" / "engine.env"
        env_file.write_text("DFE_OIDC_OKTA_CLIENT_SECRET=other\n", encoding="utf-8")
        rc, out = run(tools, [*wire_args("okta"), *docker(tools)])
        calls = tools.called()
    expect("a clashing variable is an input error", rc == 2, str(rc))
    expect("the refusal names the variable", "DFE_OIDC_OKTA_CLIENT_SECRET" in out, out)
    expect("nothing is written", calls == [], str(calls))


def test_docker_rewire_replaces_its_block_and_drops_stale_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "okta-dfe-admins.yaml").write_text(
            (GROUPS_DIR / "okta-dfe-admins.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        with FakeTools() as tools:
            run(tools, [*wire_args("okta"), *docker(tools)])
            argv = [*wire_args("okta"), *docker(tools)]
            argv[argv.index(str(GROUPS_DIR))] = tmp
            rc, _ = run(tools, argv)
            groups = sorted(p.name for p in (tools.root / "app/config/auth/groups").iterdir())
            text = (tools.compose / "env" / "engine.env").read_text(encoding="utf-8")
    expect("docker rewire succeeds", rc == 0, str(rc))
    expect("the dropped group file is removed", groups == ["okta-dfe-admins.yaml"], str(groups))
    expect("one block, not two", text.count("# BEGIN dfe-ops idp wire-external okta") == 1, text)


def test_the_cli_parses_through_dfe_ops() -> None:
    args = (
        _ops()
        .build_parser()
        .parse_args(
            ["idp", "wire-external", "--type", "google", "--target", "docker", "--compose-dir", "x"]
        )
    )
    expect(
        "wire-external is registered on dfe-ops idp",
        args.func is external_idp.cmd_idp_wire_external,
        str(args.func),
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
