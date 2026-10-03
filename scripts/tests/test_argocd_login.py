#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_argocd_login.py
#  Purpose:      Prove the Argo CD bootstrap.sh installs signs in through the
#                provider the gateway fronts it with, maps DFE's groups to Argo
#                roles, keeps its local admin with no provider, and says which.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Argo CD's own login, wired from the gateway's policy on the argocd route.

Argo came up with Dex running, an empty policy.csv and no oidc.config, so DFE's
groups mapped to no Argo role and only the local admin could do anything.

The policy the reader parses is the one the gateway chart renders, and the
bootstrap step runs as it ships, under bash with a fake `helm` and `kubectl`
first on PATH answering from a fixture.

    python3 -m pytest scripts/tests/test_argocd_login.py -q

Needs `helm` on PATH for the chart renders.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from _charts import chart_dir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP_DIR = REPO_ROOT / "bootstrap"
BOOTSTRAP = BOOTSTRAP_DIR / "bootstrap.sh"
ACCESS_SUMMARY = BOOTSTRAP_DIR / "access-summary.sh"
GATEWAY = chart_dir("envoy-gateway-config")
VALUES = REPO_ROOT / "argocd" / "values"
USERS_FIXTURE = BOOTSTRAP_DIR / "fixtures" / "tester-idp-users.toml"

DOMAIN = "slim.dfe.test"
EDGE_CA = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"

# The cascade layer2-edge.yaml runs for an on-prem kind cluster.
ON_PREM = (
    "--namespace", "envoy-gateway-system",
    "-f", str(VALUES / "common.yaml"),
    "-f", str(VALUES / "local-dfe.yaml"),
    "-f", str(VALUES / "edge-onprem.yaml"),
    "-f", str(VALUES / "profile-slim.yaml"),
    "--set", "appNamespace=dfe-local",
    "--set", f"domain={DOMAIN}",
)


def _load_login_module():
    spec = importlib.util.spec_from_file_location("argocd_login", BOOTSTRAP_DIR / "argocd_login.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["argocd_login"] = module
    spec.loader.exec_module(module)
    return module


argocd_login = _load_login_module()


def provider(issuer: str = f"https://dex.{DOMAIN}", **extra: str) -> list[dict]:
    return [{"name": "dex", "issuerUrl": issuer, "clientId": "dfe-engine", **extra}]


def render(*args: str) -> list[dict]:
    out = subprocess.run(
        ["helm", "template", "envoy-gateway-config", str(GATEWAY), *ON_PREM, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def with_provider(providers: list[dict], *args: str) -> list[dict]:
    return render(
        "--set", "oidc.enabled=true", "--set-json", f"oidc.providers={json.dumps(providers)}", *args
    )


def argo_policy(docs: list[dict]) -> dict | None:
    return next(
        (
            d for d in docs
            if d.get("kind") == "SecurityPolicy"
            and d["metadata"]["name"] == argocd_login.POLICY
            and d["metadata"]["namespace"] == argocd_login.NAMESPACE
        ),
        None,
    )


def external_secrets(docs: list[dict]) -> dict[str, dict]:
    """namespace -> the OIDC ExternalSecret rendered there."""
    return {
        d["metadata"]["namespace"]: d
        for d in docs
        if d.get("kind") == "ExternalSecret" and d["metadata"]["name"].startswith("dfe-oidc-")
    }


# --- the reader against the policy the gateway chart renders -----------------
def test_the_gateway_renders_the_policy_the_reader_looks_for() -> None:
    assert argo_policy(with_provider(provider())) is not None


def test_argo_logs_in_through_the_gateways_provider_and_client() -> None:
    config, why = argocd_login.oidc_config(argo_policy(with_provider(provider())), DOMAIN, "")
    assert config is not None, why
    assert config["issuer"] == f"https://dex.{DOMAIN}"
    assert config["clientID"] == "dfe-engine"
    assert config["name"] == "dex"
    assert config["requestedScopes"] == ["openid", "email", "profile", "groups"]


def test_the_client_secret_is_a_reference_to_the_gateways_own_secret() -> None:
    """A reference, never the value, and to the Secret the edge already reads."""
    docs = with_provider(provider())
    config, _ = argocd_login.oidc_config(argo_policy(docs), DOMAIN, "")
    policy_secret = argo_policy(docs)["spec"]["oidc"]["clientSecret"]["name"]
    assert config["clientSecret"] == f"${policy_secret}:client-secret"
    target = external_secrets(docs)[argocd_login.NAMESPACE]["spec"]["target"]["name"]
    assert target == policy_secret


def test_a_renamed_client_secret_is_followed() -> None:
    docs = with_provider(provider(clientSecretName="idp-client"))
    config, _ = argocd_login.oidc_config(argo_policy(docs), DOMAIN, "")
    assert config["clientSecret"] == "$idp-client:client-secret"


def test_the_login_provider_is_the_one_the_edge_logs_in_with() -> None:
    providers = [
        *provider(),
        {"name": "corp", "issuerUrl": "https://login.example.com", "clientId": "argo"},
    ]
    docs = with_provider(providers, "--set", "oidc.loginProvider=corp")
    config, _ = argocd_login.oidc_config(argo_policy(docs), DOMAIN, "")
    assert (config["name"], config["issuer"], config["clientID"]) == (
        "corp", "https://login.example.com", "argo",
    )


def test_no_provider_renders_no_policy_and_argo_keeps_its_local_admin() -> None:
    docs = render()
    assert argo_policy(docs) is None
    config, why = argocd_login.oidc_config(argo_policy(docs), DOMAIN, EDGE_CA)
    assert config is None
    assert "local admin" in why


def test_the_infra_kill_switch_leaves_argo_on_its_local_admin() -> None:
    docs = with_provider(provider(), "--set", "exposure.infraUisExternal=false")
    assert argo_policy(docs) is None


def test_a_client_secret_outside_argos_namespace_is_refused() -> None:
    policy = argo_policy(with_provider(provider()))
    policy["spec"]["oidc"]["clientSecret"]["namespace"] = "dfe-local"
    config, why = argocd_login.oidc_config(policy, DOMAIN, "")
    assert config is None
    assert "dfe-local" in why


# --- trust for an IdP the gateway serves --------------------------------------
def test_an_issuer_the_gateway_serves_is_verified_against_the_edge_ca() -> None:
    config, _ = argocd_login.oidc_config(argo_policy(with_provider(provider())), DOMAIN, EDGE_CA)
    assert config["rootCA"] == EDGE_CA


def test_a_public_issuer_keeps_the_system_roots() -> None:
    """rootCA replaces Argo's trust store, so a public IdP must never get one."""
    docs = with_provider(provider("https://login.microsoftonline.com/tenant/v2.0"))
    config, _ = argocd_login.oidc_config(argo_policy(docs), DOMAIN, EDGE_CA)
    assert "rootCA" not in config


@pytest.mark.parametrize(
    ("issuer", "served"),
    [
        (f"https://dex.{DOMAIN}", True),
        (f"https://DEX.{DOMAIN}:443/realms/dfe", True),
        (f"https://a.dex.{DOMAIN}", False),
        (f"https://{DOMAIN}", False),
        (f"https://dex.{DOMAIN}.example.com", False),
    ],
)
def test_only_one_label_under_the_domain_is_the_wildcards(issuer: str, served: bool) -> None:
    assert argocd_login.served_by_gateway(issuer, DOMAIN) is served


# --- the ExternalSecret Argo reads the reference from --------------------------
def test_the_argocd_copy_of_the_client_secret_is_labelled_for_argo() -> None:
    target = external_secrets(with_provider(provider()))[argocd_login.NAMESPACE]["spec"]["target"]
    labels = target["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/part-of"] == "argocd"
    assert labels["app.kubernetes.io/instance"] == "envoy-gateway-config"


def test_no_other_copy_claims_to_be_part_of_argo() -> None:
    copies = external_secrets(with_provider(provider()))
    others = {ns: es for ns, es in copies.items() if ns != argocd_login.NAMESPACE}
    assert others
    assert all("template" not in es["spec"]["target"] for es in others.values())


# --- the values bootstrap layers on ------------------------------------------
def test_both_argo_carrying_groups_are_argo_admin_and_the_viewer_group_read_only() -> None:
    """dfe-infra's engine role, infra_admin, carries argo:*, so it is Argo's admin too."""
    assert argocd_login.policy_csv().splitlines() == [
        "g, dfe-admins, role:admin",
        "g, dfe-infra, role:admin",
        "g, dfe-infra-viewers, role:readonly",
    ]


def test_argos_admins_are_the_deployments_admin_groups() -> None:
    """Argo keeps its own policy lines, so they are held to adminGroups in common.yaml."""
    admin_groups = yaml.safe_load((VALUES / "common.yaml").read_text(encoding="utf-8"))["adminGroups"]
    argo_admins = [
        line.split(", ")[1]
        for line in argocd_login.policy_csv().splitlines()
        if line.endswith(", role:admin")
    ]
    assert sorted(argo_admins) == sorted(admin_groups), (
        f"Argo CD's admins {argo_admins} are not adminGroups {admin_groups}: change "
        "bootstrap/argocd_login.py and argocd/values/common.yaml together"
    )


def test_the_mapped_groups_are_ones_the_fixture_idp_serves() -> None:
    groups = {g["name"] for g in tomllib.loads(USERS_FIXTURE.read_text(encoding="utf-8"))["groups"]}
    mapped = {argocd_login.ADMIN_GROUP, argocd_login.INFRA_GROUP, argocd_login.VIEWER_GROUP}
    assert mapped <= groups


@pytest.mark.skipif(shutil.which("argocd") is None, reason="needs the argocd CLI on PATH")
@pytest.mark.parametrize(
    ("group", "action", "resource", "target", "allowed"),
    [
        ("dfe-admins", "update", "clusters", "*", True),
        ("dfe-infra", "update", "clusters", "*", True),
        ("dfe-infra", "sync", "applications", "default/app", True),
        ("dfe-infra-viewers", "get", "applications", "default/app", True),
        ("dfe-infra-viewers", "sync", "applications", "default/app", False),
        ("dfe-infra-viewers", "update", "clusters", "*", False),
        ("dfe-viewers", "get", "applications", "default/app", False),
    ],
)
def test_argos_own_rbac_engine_grants_what_the_mapping_says(
    tmp_path: Path, group: str, action: str, resource: str, target: str, allowed: bool
) -> None:
    """Evaluated by `argocd admin settings rbac can`, against Argo's built-in roles."""
    policy = tmp_path / "policy.csv"
    policy.write_text(argocd_login.policy_csv(), encoding="utf-8")
    out = subprocess.run(
        ["argocd", "admin", "settings", "rbac", "can", group, action, resource, target,
         "--policy-file", str(policy)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert out.stdout.strip() == ("Yes" if allowed else "No"), out.stderr


def test_a_provider_turns_dex_off_and_sets_oidc_config() -> None:
    config, _ = argocd_login.oidc_config(argo_policy(with_provider(provider())), DOMAIN, "")
    values = argocd_login.helm_values(config)
    assert values["dex"] == {"enabled": False}
    assert yaml.safe_load(values["configs"]["cm"]["oidc.config"]) == config


def test_no_provider_leaves_dex_and_argo_cm_alone() -> None:
    values = argocd_login.helm_values(None)
    assert "dex" not in values
    assert "cm" not in values["configs"]
    assert values["configs"]["rbac"]["policy.csv"] == argocd_login.policy_csv()


# --- the access summary says which login Argo offers ---------------------------
def test_the_summary_names_the_provider_and_the_roles() -> None:
    config, _ = argocd_login.oidc_config(argo_policy(with_provider(provider())), DOMAIN, "")
    cm = {"data": argocd_login.helm_values(config)["configs"]["cm"]}
    text = argocd_login.summary(cm)
    assert f"https://dex.{DOMAIN}" in text
    assert "`dfe-admins` and `dfe-infra` get `role:admin`" in text
    assert "`dfe-infra-viewers` gets `role:readonly`" in text


def test_the_summary_says_argo_keeps_its_local_admin_with_no_provider() -> None:
    assert "keeps its local `admin` login" in argocd_login.summary({"data": {}})


def test_the_summary_reads_a_yaml_oidc_config_too() -> None:
    raw = "name: Okta\nissuer: https://corp.okta.com\n"
    assert argocd_login.configured_issuer(raw) == "https://corp.okta.com"
    text = argocd_login.summary({"data": {"oidc.config": raw}})
    assert text.startswith("- Argo CD signs in through https://corp.okta.com, ")


# --- bootstrap.sh, run against a fake cluster -----------------------------------
FAKE_HELM = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write("helm " + " ".join(args) + "\\n")
fixture = json.load(open(os.environ["FAKE_FIXTURE"], encoding="utf-8"))
if args[:1] == ["list"]:
    print(json.dumps(fixture.get("releases", [])))
elif args[:2] == ["get", "values"]:
    print(json.dumps(fixture.get("values")))
elif args[:1] == ["upgrade"] and "-" in args:
    with open(os.environ["FAKE_STDIN"], "a", encoding="utf-8") as out:
        out.write(sys.stdin.read() + "\\n---\\n")
"""

FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
fixture = json.load(open(os.environ["FAKE_FIXTURE"], encoding="utf-8"))
if "crd" in args:
    sys.exit(0 if fixture.get("crd") else 1)
if "deploy" in args:
    sys.exit(0 if fixture.get("server") else 1)
if "securitypolicies.gateway.envoyproxy.io" in args:
    if fixture.get("policy"):
        print(json.dumps(fixture["policy"]))
"""

CACHE_HOST = "valkey.argocd.svc.cluster.local"
OUR_RELEASE = {
    "releases": [{"name": "argocd", "namespace": "argocd", "chart": "argo-cd-10.9.0"}],
    "values": {"redis": {"enabled": False}, "externalRedis": {"host": CACHE_HOST}},
    "crd": True,
    "server": True,
}


def _function(lines: list[str], name: str) -> list[str]:
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    if lines[start].rstrip().endswith("}"):
        return [lines[start]]
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return lines[start:end + 1]


def _between(lines: list[str], start: str, end: str) -> list[str]:
    first = next(i for i, ln in enumerate(lines) if ln.startswith(start))
    last = next(i for i in range(first, len(lines)) if lines[i].startswith(end))
    return lines[first:last]


HELPERS = ("run", "dfe_have_crd", "dfe_should_install")
STEP = ('echo "==> [6/7] ArgoCD', "# argocd-secret exists only once Argo")
REREAD = ("# The gateway's policy on the argocd route exists", 'if [ "${POST_INTEGRATION}"')

type BootstrapRun = tuple[subprocess.CompletedProcess, list[str], list[dict]]


def step_script() -> str:
    """bootstrap.sh's helpers, step [6/7] and the login re-read after readiness."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    helpers = [line for name in HELPERS for line in _function(lines, name)]
    step, reread = _between(lines, *STEP), _between(lines, *REREAD)
    return "\n".join(["set -euo pipefail", *helpers, *step, "switch_cluster", *reread]) + "\n"


def run_bootstrap(tmp_path: Path, before: dict, after: dict) -> BootstrapRun:
    """[6/7] against `before`, then the post-readiness re-read against `after`.

    Returns the output, every helm upgrade, and the values each one read on stdin.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    for name, body in (("helm", FAKE_HELM), ("kubectl", FAKE_KUBECTL)):
        path = bindir / name
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(before), encoding="utf-8")
    after_file = tmp_path / "after.json"
    after_file.write_text(json.dumps(after), encoding="utf-8")
    log = tmp_path / "calls.log"
    stdin_log = tmp_path / "stdin.log"
    log.touch()
    stdin_log.touch()
    switch = f'switch_cluster() {{ cp "{after_file}" "{fixture}"; }}\n'
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_FIXTURE": str(fixture),
        "FAKE_LOG": str(log),
        "FAKE_STDIN": str(stdin_log),
        "SCRIPT_DIR": str(BOOTSTRAP_DIR),
        "VALKEY_SVC": "valkey",
        "ARGOCD_VERSION": "10.9.0",
        "DFE_DOMAIN": DOMAIN,
        "POST_READINESS": "true",
    }
    out = subprocess.run(
        ["bash", "-c", switch + step_script()], env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines()
    upgrades = [call for call in calls if call.startswith("helm upgrade")]
    stdin = stdin_log.read_text(encoding="utf-8").split("\n---\n")
    return out, upgrades, [json.loads(doc) for doc in stdin if doc.strip()]


def rendered_policy() -> dict:
    return argo_policy(with_provider(provider()))


def test_our_argo_signs_in_through_the_provider_the_gateway_already_carries(tmp_path: Path) -> None:
    fixture = {**OUR_RELEASE, "policy": rendered_policy()}
    out, calls, values = run_bootstrap(tmp_path, fixture, fixture)
    assert out.returncode == 0, out.stderr
    assert len(calls) == 1, calls
    assert "--values -" in calls[0]
    assert values[0]["dex"] == {"enabled": False}
    oidc = yaml.safe_load(values[0]["configs"]["cm"]["oidc.config"])
    assert oidc["clientSecret"] == "$dfe-oidc-dex:client-secret"
    assert "login unchanged since [6/7]" in out.stdout


def test_a_first_bootstrap_wires_the_login_once_the_gateway_has_synced(tmp_path: Path) -> None:
    """No policy at [6/7]; the gateway's policy exists by the time readiness passes."""
    synced = {**OUR_RELEASE, "policy": rendered_policy()}
    out, calls, values = run_bootstrap(tmp_path, OUR_RELEASE, synced)
    assert out.returncode == 0, out.stderr
    assert len(calls) == 2, calls
    assert "cm" not in values[0]["configs"]
    assert "oidc.config" in values[1]["configs"]["cm"]


def test_no_provider_leaves_argo_on_its_local_admin(tmp_path: Path) -> None:
    out, calls, values = run_bootstrap(tmp_path, OUR_RELEASE, OUR_RELEASE)
    assert out.returncode == 0, out.stderr
    assert len(calls) == 1, calls
    assert "dex" not in values[0]
    assert "cm" not in values[0]["configs"]
    assert "Argo keeps its local admin" in out.stderr


def test_a_foreign_argo_is_never_reconfigured(tmp_path: Path) -> None:
    foreign = {**OUR_RELEASE, "values": None, "policy": rendered_policy()}
    out, calls, _ = run_bootstrap(tmp_path, foreign, foreign)
    assert out.returncode == 0, out.stderr
    assert calls == []
    assert "Argo CD login against the converged gateway" not in out.stdout


# --- the access summary, as it ships ---------------------------------------------
SUMMARY_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
if "argocd-cm" in args:
    print(os.environ["FAKE_ARGOCD_CM"])
    sys.exit(0)
sys.exit(1)
"""


def test_the_access_summary_carries_argos_login(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    kubectl = bindir / "kubectl"
    kubectl.write_text(SUMMARY_KUBECTL, encoding="utf-8", newline="\n")
    kubectl.chmod(0o755)
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_ARGOCD_CM": json.dumps({"data": {}}),
    }
    out = subprocess.run(
        ["bash", str(ACCESS_SUMMARY), "", str(tmp_path / "access.md")], env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "Argo CD has no OIDC provider, so it keeps its local `admin` login" in out.stdout
