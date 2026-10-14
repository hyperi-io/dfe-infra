#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_stack_deploy_env.py
#  Purpose:      Prove stack-deploy's env assembly unwraps the terraform bridge's
#                (value, sensitive) pairs, demands only the secrets vars the
#                declared backend actually uses, and runs its preflight with no
#                --registry given.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What `dfe-ops stack-deploy --from-terraform` builds its environment from.

bridge.get_tf_outputs answers name -> (value, sensitive) by design, documented
in its own docstring. Handing those pairs straight to subprocess.run refuses the
whole call with a TypeError, so every --from-terraform deploy failed before
bootstrap.sh ran at all.

The required-variable gate is the other half: DFE_VAULT_ADDR and
DFE_VAULT_ROLE_ID are openbao-only, and demanding them on an aws-sm deployment
refuses a deploy that has everything it needs.

    python3 -m pytest scripts/tests/test_dfe_ops_stack_deploy_env.py -q
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_stack_deploy_env", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_stack_deploy_env", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_stack_deploy_env"] = dfeops
_loader.exec_module(dfeops)

# The shape bridge.get_tf_outputs really answers with.
TF_OUTPUTS = {
    "DFE_DOMAIN": ("dfe-test.internal", False),
    "DFE_NAMESPACE": ("dfe-test", False),
    "DFE_VAULT_SECRET_ID": ("s3cr3t", True),
    "cluster_name": ("dfe-aws-test", False),
}


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        from_terraform=str(tmp_path),
        env_file=[],
        mode="scale",
        target_revision="",
        kubeconfig="",
        readiness_timeout=900,
        access_out=str(tmp_path / "access.txt"),
        stack="current",
        registry="registry.example.com/dfe",
        base_domain=None,
        domain=None,
        skip_e2e=False,
        dry_run=False,
        e2e=False,
    )


def test_terraform_outputs_land_as_strings(tmp_path: Path, monkeypatch) -> None:
    """A raw 2-tuple in the env dict is what crashed subprocess.run outright."""
    bridge = type(sys)("bridge")
    bridge.get_tf_outputs = lambda _dir: dict(TF_OUTPUTS)
    monkeypatch.setitem(sys.modules, "bridge", bridge)
    env = dfeops._assemble_env(_args(tmp_path))
    assert env["DFE_DOMAIN"] == "dfe-test.internal"
    assert env["DFE_NAMESPACE"] == "dfe-test"
    assert env["DFE_VAULT_SECRET_ID"] == "s3cr3t"
    assert all(isinstance(value, str) for value in env.values())
    # A non-DFE_ output is still filtered out.
    assert "cluster_name" not in env


def test_the_registry_flag_reaches_bootstrap(tmp_path: Path, monkeypatch) -> None:
    """--registry otherwise stops at the version-set render, and every dfe-*
    image is left resolving against Docker Hub."""
    bridge = type(sys)("bridge")
    bridge.get_tf_outputs = lambda _dir: dict(TF_OUTPUTS)
    monkeypatch.setitem(sys.modules, "bridge", bridge)
    env = dfeops._assemble_env(_args(tmp_path))
    assert env["DFE_REGISTRY"] == "registry.example.com/dfe"


def _no_terraform(tmp_path: Path, **over: object) -> argparse.Namespace:
    args = _args(tmp_path)
    args.from_terraform = None
    for key, value in over.items():
        setattr(args, key, value)
    return args


def test_the_access_summary_defaults_into_this_repos_run_directory(
    tmp_path: Path, monkeypatch
) -> None:
    """A cycle launched from another repo wrote dfe-access.md into THAT repo's tree."""
    elsewhere = tmp_path / "another-repo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    env = dfeops._assemble_env(_no_terraform(tmp_path, access_out="", stack="2.2.0-rc.99"))

    out = Path(env["DFE_ACCESS_OUT"])
    assert out == REPO_ROOT / ".tmp" / "2.2.0-rc.99-scale" / dfeops.ACCESS_OUT_FILENAME
    assert out.is_absolute()
    assert elsewhere not in out.parents


def test_the_access_summary_sits_beside_the_launchers_login_file(tmp_path: Path) -> None:
    env = dfeops._assemble_env(_no_terraform(tmp_path, access_out=""))

    assert Path(env["DFE_ACCESS_OUT"]).parent == Path(env["DFE_ACCESS_SUMMARY_OUT"]).parent


def test_an_explicit_access_out_is_used_as_given(tmp_path: Path) -> None:
    chosen = tmp_path / "mine.md"

    env = dfeops._assemble_env(_no_terraform(tmp_path, access_out=str(chosen)))

    assert env["DFE_ACCESS_OUT"] == str(chosen)


def test_the_summary_directory_exists_before_bootstrap_writes_into_it(
    tmp_path: Path, monkeypatch
) -> None:
    """access-summary.sh writes through tee, which fails on a missing directory."""
    out = tmp_path / "run" / "nested" / "summary.md"
    seen: dict[str, bool] = {}

    def fake_bootstrap(cmd: list[str], *, env: dict | None = None) -> int:
        seen["parent_existed"] = out.parent.is_dir()
        seen["pointed_at_it"] = env is not None and env["DFE_ACCESS_OUT"] == str(out)
        return 0

    monkeypatch.setattr(dfeops, "_resolve_stack", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_offline_preflight", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_bootstrap_required", lambda _env: ())
    monkeypatch.setattr(dfeops, "_require_script", lambda _name: tmp_path / "bootstrap.sh")
    monkeypatch.setattr(dfeops, "_strimzi_deploy_gate", lambda _kubeconfig: True)
    monkeypatch.setattr(dfeops, "_run_streaming", fake_bootstrap)
    monkeypatch.delenv("DFE_VAULT_ADDR", raising=False)
    args = _no_terraform(tmp_path, access_out=str(out), check_only=False, mode="single")

    assert dfeops.cmd_stack_deploy(args) == 0
    assert seen == {"parent_existed": True, "pointed_at_it": True}


def test_the_flag_defaults_empty_so_the_run_directory_decides() -> None:
    args = dfeops.build_parser().parse_args(["stack-deploy"])

    assert args.access_out == ""


def test_a_hand_run_bootstrap_also_writes_under_the_repos_tmp() -> None:
    """bootstrap.sh run without dfe-ops falls back to its own default, not the cwd."""
    body = (REPO_ROOT / "bootstrap" / "bootstrap.sh").read_text(encoding="utf-8")

    assert '"${DFE_ACCESS_OUT:-${REPO_ROOT}/.tmp/dfe-access.md}"' in body
    assert '"${DFE_ACCESS_SUMMARY_OUT:-${REPO_ROOT}/.tmp/access-summary.md}"' in body


def _preflight_render_argv(monkeypatch, registry: str | None) -> list[str]:
    """The argv the offline preflight's first step runs, stopping it right there."""
    seen: list[list[str]] = []

    def fake_run_text(cmd: list[str], env: dict | None = None) -> tuple[int, str, str]:
        seen.append(cmd)
        return 1, "", "stopped by the test"

    monkeypatch.setattr(dfeops, "_run_text", fake_run_text)
    args = argparse.Namespace(registry=registry, stack="2.2.0-rc.99", strict_compat=False, mode="single")
    assert dfeops._offline_preflight(args) == 1
    return seen[0]


def test_preflight_runs_with_no_registry_flag(monkeypatch) -> None:
    """--registry defaults to None, and a None in argv refused the whole run with
    a TypeError before any check had run."""
    argv = _preflight_render_argv(monkeypatch, None)
    assert None not in argv
    assert "--registry" not in argv
    assert argv[-3:] == ["render", "--stack", "2.2.0-rc.99"]


def test_preflight_passes_a_given_registry_on(monkeypatch) -> None:
    argv = _preflight_render_argv(monkeypatch, "registry.example.com/dfe")
    assert argv[argv.index("--registry") + 1] == "registry.example.com/dfe"


def test_openbao_is_required_only_when_the_backend_is_openbao() -> None:
    aws_sm = dfeops._bootstrap_required({"DFE_SECRETS_BACKEND": "aws-sm"})
    assert "DFE_VAULT_ADDR" not in aws_sm
    assert "DFE_VAULT_ROLE_ID" not in aws_sm
    assert "DFE_SECRETS_REGION" in aws_sm

    openbao = dfeops._bootstrap_required({"DFE_SECRETS_BACKEND": "openbao"})
    assert "DFE_VAULT_ADDR" in openbao
    assert "DFE_VAULT_ROLE_ID" in openbao
    assert "DFE_SECRETS_REGION" not in openbao


def test_an_unset_or_empty_backend_reads_as_openbao() -> None:
    """bootstrap.sh's own ${DFE_SECRETS_BACKEND:-openbao} -- a present-but-empty
    value must not be read as aws-sm and drop both checks."""
    for env in ({}, {"DFE_SECRETS_BACKEND": ""}):
        required = dfeops._bootstrap_required(env)
        assert "DFE_VAULT_ADDR" in required, env
        assert "DFE_SECRETS_REGION" not in required, env


def test_only_the_cluster_broker_reads_its_credential_from_the_store() -> None:
    """The single broker's credential comes from an in-cluster generator, so the
    store holds nothing it needs."""
    assert dfeops._broker_credential_from_store("scale") is True
    assert dfeops._broker_credential_from_store("single") is False
    assert dfeops._broker_credential_from_store("slim") is False
    assert dfeops._broker_credential_from_store("mesh") is False


def _deploy_without_a_secret_id(tmp_path: Path, monkeypatch, mode: str) -> tuple[int, bool]:
    """(stack-deploy's exit code, whether bootstrap ran) with a store address and no SecretID."""
    ran: dict[str, bool] = {"bootstrap": False}

    def fake_bootstrap(cmd: list[str], *, env: dict | None = None) -> int:
        ran["bootstrap"] = True
        return 0

    monkeypatch.setattr(dfeops, "_resolve_stack", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_offline_preflight", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_bootstrap_required", lambda _env: ())
    monkeypatch.setattr(dfeops, "_require_script", lambda _name: tmp_path / "bootstrap.sh")
    monkeypatch.setattr(dfeops, "_strimzi_deploy_gate", lambda _kubeconfig: True)
    monkeypatch.setattr(dfeops, "_run_streaming", fake_bootstrap)
    monkeypatch.setenv("DFE_VAULT_ADDR", "https://store.example.invalid:8200")
    monkeypatch.delenv("DFE_VAULT_SECRET_ID", raising=False)
    args = _no_terraform(
        tmp_path, access_out=str(tmp_path / "access.md"), check_only=False, mode=mode
    )
    return dfeops.cmd_stack_deploy(args), ran["bootstrap"]


def test_a_single_broker_deploy_needs_no_secret_id(tmp_path: Path, monkeypatch) -> None:
    """Refusing it sent a store-less kind or on-prem deploy of the single tier away
    for a credential nothing in that tier reads."""
    assert _deploy_without_a_secret_id(tmp_path, monkeypatch, "single") == (0, True)


def test_a_cluster_broker_deploy_still_needs_the_secret_id(tmp_path: Path, monkeypatch) -> None:
    assert _deploy_without_a_secret_id(tmp_path, monkeypatch, "scale") == (1, False)


def test_the_backend_split_matches_the_bridge_it_drives() -> None:
    """dfe-ops and bridge.py gate the same deploy, so a disagreement is either a
    false refusal or a miss that surfaces later and worse."""
    sys.path.insert(0, str(REPO_ROOT / "bootstrap"))
    import bridge

    for backend in dfeops.STORE_BACKENDS:
        ours = set(dfeops._bootstrap_required({"DFE_SECRETS_BACKEND": backend}))
        theirs = bridge._required_vars({"DFE_SECRETS_BACKEND": backend})
        assert ours & {"DFE_VAULT_ADDR", "DFE_VAULT_ROLE_ID", "DFE_SECRETS_REGION"} == theirs & {
            "DFE_VAULT_ADDR",
            "DFE_VAULT_ROLE_ID",
            "DFE_SECRETS_REGION",
        }, backend


def test_the_none_backend_demands_no_store_input() -> None:
    required = set(dfeops._bootstrap_required({"DFE_SECRETS_BACKEND": "none"}))
    assert not required & {"DFE_VAULT_ADDR", "DFE_VAULT_ROLE_ID", "DFE_SECRETS_REGION"}


# Everything bootstrap.sh requires beyond the secrets store, as an outsider's
# kind deploy supplies it: placeholder values only (RFC 2606), this repo ships publicly.
_OUTSIDER_ENV = {
    "DFE_ENV": "local",
    "DFE_CLOUD": "local-dfe",
    "DFE_REGION": "local",
    "DFE_DOMAIN": "dfe.example.test",
    "DFE_REPO_URL": "https://git.example.invalid/dfe-infra.git",
    "DFE_TARGET_REVISION": "main",
    "DFE_STORAGE_CLASS": "local-path",
    "DFE_NAMESPACE": "dfe-local",
    "DFE_CLICKHOUSE_HOST": "clickhouse.clickhouse.svc.cluster.local",
    "DFE_OTEL_ENDPOINT": "otel-collector-gateway.otel.svc.cluster.local:4317",
    "DFE_WORKLOAD_IDENTITY_ANNOTATIONS": "{}",
}


def _outsider_deploy(
    tmp_path: Path, monkeypatch, mode: str, **extra: str
) -> tuple[int, dict[str, str] | None]:
    """(stack-deploy's exit code, the env bootstrap.sh got, or None when it never ran)."""
    seen: dict[str, dict[str, str]] = {}

    def fake_bootstrap(cmd: list[str], *, env: dict | None = None) -> int:
        seen["env"] = dict(env or {})
        return 0

    monkeypatch.setattr(dfeops, "_resolve_stack", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_offline_preflight", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_require_script", lambda _name: tmp_path / "bootstrap.sh")
    monkeypatch.setattr(dfeops, "_strimzi_deploy_gate", lambda _kubeconfig: True)
    monkeypatch.setattr(dfeops, "_run_streaming", fake_bootstrap)
    for key in [k for k in dfeops.os.environ if k.startswith("DFE_")]:
        monkeypatch.delenv(key)
    for key, value in {**_OUTSIDER_ENV, **extra}.items():
        monkeypatch.setenv(key, value)
    args = _no_terraform(
        tmp_path, access_out=str(tmp_path / "access.md"), check_only=False, mode=mode
    )
    return dfeops.cmd_stack_deploy(args), seen.get("env")


def test_a_store_less_tier_deploys_with_no_secrets_inputs(tmp_path: Path, monkeypatch) -> None:
    """A kind run of single passed only with made-up OpenBao values, for a store
    whose ClusterSecretStore sat InvalidProviderConfig and that nothing read."""
    for mode in ("single", "slim"):
        rc, env = _outsider_deploy(tmp_path, monkeypatch, mode)
        assert rc == 0, mode
        assert env is not None, mode
        assert env["DFE_SECRETS_BACKEND"] == "none", mode


def test_a_store_needing_tier_still_refuses_without_a_store(tmp_path: Path, monkeypatch) -> None:
    """The cluster broker's credential comes from the store, so scale keeps the
    openbao default and its demand for an address and an AppRole."""
    assert _outsider_deploy(tmp_path, monkeypatch, "scale") == (1, None)


def test_an_unknown_backend_is_refused(tmp_path: Path, monkeypatch) -> None:
    rc, env = _outsider_deploy(tmp_path, monkeypatch, "single", DFE_SECRETS_BACKEND="vault")
    assert (rc, env) == (1, None)


def test_an_explicit_none_is_refused_where_the_store_is_read(tmp_path: Path, monkeypatch) -> None:
    rc, env = _outsider_deploy(tmp_path, monkeypatch, "scale", DFE_SECRETS_BACKEND="none")
    assert (rc, env) == (1, None)


def test_an_openbao_input_keeps_the_openbao_default(tmp_path: Path, monkeypatch) -> None:
    """Every deployment that ran before named an OpenBao address, and still gets openbao."""
    half = _outsider_deploy(
        tmp_path, monkeypatch, "single", DFE_VAULT_ADDR="https://store.example.invalid:8200"
    )
    assert half == (1, None)
    rc, env = _outsider_deploy(
        tmp_path,
        monkeypatch,
        "single",
        DFE_VAULT_ADDR="https://store.example.invalid:8200",
        DFE_VAULT_ROLE_ID="role-placeholder",
    )
    assert rc == 0
    assert env is not None
    assert env["DFE_SECRETS_BACKEND"] == "openbao"


BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"

# (env, the backend bootstrap.sh resolves when none is named). Placeholder values only.
_STORE_DEFAULTS = (
    ({"DFE_CLOUD": "aws"}, "aws-sm"),
    ({"DFE_CLOUD": "aws", "DFE_VAULT_ADDR": "https://store.example.invalid:8200"}, "aws-sm"),
    ({"DFE_CLOUD": "gcp"}, "openbao"),
    ({"DFE_CLOUD": "azure"}, "openbao"),
    ({"DFE_CLOUD": "local-dfe"}, "openbao"),
)


def _bootstrap_backend(env: dict[str, str]) -> subprocess.CompletedProcess:
    """bootstrap.sh's own backend default and aws refusals, run from its text rather than restated."""
    script = BOOTSTRAP.read_text(encoding="utf-8")
    start = script.index('if [[ -z "${DFE_SECRETS_BACKEND:-}" ]]; then')
    end = script.index('case "${DFE_SECRETS_BACKEND}" in', start)
    return subprocess.run(
        ["bash", "-c", script[start:end] + 'printf "%s" "${DFE_SECRETS_BACKEND}"\n'],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def test_an_unnamed_backend_is_secrets_manager_on_aws_and_openbao_elsewhere() -> None:
    for env, expected in _STORE_DEFAULTS:
        done = _bootstrap_backend(env)
        assert (done.returncode, done.stdout) == (0, expected), env
        assert dfeops._default_store_backend(env) == expected, env


def test_bootstrap_refuses_openbao_on_aws() -> None:
    """An aws deployment keeps every secret in Secrets Manager and runs no OpenBao."""
    for env in (
        {"DFE_CLOUD": "aws", "DFE_SECRETS_BACKEND": "openbao"},
        {"DFE_CLOUD": "aws", "DFE_CERTMANAGER_SECRET_ID": "approle-placeholder"},
    ):
        done = _bootstrap_backend(env)
        assert done.returncode == 1, env
        assert "runs no OpenBao" in done.stderr, env
    done = _bootstrap_backend({"DFE_CLOUD": "gcp", "DFE_SECRETS_BACKEND": "openbao"})
    assert (done.returncode, done.stdout) == (0, "openbao")


def test_stack_deploy_refuses_openbao_on_aws(tmp_path: Path, monkeypatch) -> None:
    rc, env = _outsider_deploy(
        tmp_path, monkeypatch, "single", DFE_CLOUD="aws", DFE_SECRETS_BACKEND="openbao"
    )
    assert (rc, env) == (1, None)


def test_an_openbao_address_selects_nothing_on_aws(tmp_path: Path, monkeypatch) -> None:
    rc, env = _outsider_deploy(
        tmp_path,
        monkeypatch,
        "single",
        DFE_CLOUD="aws",
        DFE_SECRETS_REGION="us-west-2",
        DFE_VAULT_ADDR="https://store.example.invalid:8200",
    )
    assert rc == 0
    assert env is not None
    assert env["DFE_SECRETS_BACKEND"] == "aws-sm"


def test_the_aws_default_is_what_dfe_ops_and_the_bridge_check_for() -> None:
    sys.path.insert(0, str(REPO_ROOT / "bootstrap"))
    import bridge

    store_inputs = {"DFE_VAULT_ADDR", "DFE_VAULT_ROLE_ID", "DFE_SECRETS_REGION"}
    for env, expected in _STORE_DEFAULTS:
        ours = set(dfeops._bootstrap_required(env)) & store_inputs
        theirs = bridge._required_vars(env) & store_inputs
        assert ours == theirs, env
        assert ("DFE_SECRETS_REGION" in ours) == (expected == "aws-sm"), env


def test_an_aws_deploy_that_reads_the_store_gets_aws_sm(tmp_path: Path, monkeypatch) -> None:
    """aws.yaml turns the gateway's OIDC on, whose client secret lives in the store."""
    rc, env = _outsider_deploy(
        tmp_path, monkeypatch, "single", DFE_CLOUD="aws", DFE_SECRETS_REGION="us-west-2"
    )
    assert rc == 0
    assert env is not None
    assert env["DFE_SECRETS_BACKEND"] == "aws-sm"


def test_ca_persistence_is_a_store_consumer(tmp_path: Path, monkeypatch) -> None:
    """The persisted root is an ExternalSecret against the store, which none never creates."""
    rc, env = _outsider_deploy(
        tmp_path, monkeypatch, "single", DFE_SECRETS_BACKEND="none", DFE_CA_PERSIST="true"
    )
    assert (rc, env) == (1, None)


def test_the_store_consumers_follow_the_charts() -> None:
    """Read from each store-referencing template's own render condition, per tier."""
    on_prem = {"DFE_CLOUD": "local-dfe"}
    for mode in ("slim", "single", "mesh"):
        assert dfeops._store_consumers(mode, on_prem) == [], mode
    scale = dfeops._store_consumers("scale", on_prem)
    assert any("cluster broker" in use for use in scale), scale
    # A managed broker takes its credential from the store unless it signs with IAM.
    managed = dfeops._store_consumers("single", {**on_prem, "DFE_KAFKA_PROVIDER": "confluent-cloud"})
    assert any("external broker" in use for use in managed), managed
    # aws.yaml turns the gateway's OIDC on, and its client secret lives in the store.
    aws = dfeops._store_consumers("single", {"DFE_CLOUD": "aws"})
    assert any("OIDC" in use for use in aws), aws


def test_the_console_nextauth_secret_is_a_store_consumer_when_a_layer_names_the_store(
    monkeypatch,
) -> None:
    """dfe-extras renders the NextAuth ExternalSecret off the layers over its dfe-ui defaults."""
    tracked = dfeops._component_values
    assert tracked("dfe-ui", "single").get("config", {}).get("nextauthSecretName"), (
        "dfe-extras' dfe-ui defaults name the NextAuth secret"
    )
    assert not [u for u in dfeops._store_consumers("single", {}) if "NextAuth" in u]

    def store_named(service: str, mode: str, cloud: str = "") -> dict:
        values = tracked(service, mode, cloud)
        if service == "dfe-ui":
            values.setdefault("config", {})["secretStoreName"] = dfeops.STORE_NAME
        return values

    monkeypatch.setattr(dfeops, "_component_values", store_named)
    uses = dfeops._store_consumers("single", {})
    assert any("NextAuth" in use for use in uses), uses


def test_the_component_values_layer_the_appset_files_over_the_extras_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    """A layer that sets a key wins; a key no layer sets keeps the dfe-extras default."""
    extras = tmp_path / "dfe-extras"
    extras.mkdir()
    (extras / "values.yaml").write_text(
        "extras:\n  defaults:\n    dfe-ui:\n      config:\n"
        "        nextauthSecretName: from-defaults\n        secretStoreName: ''\n",
        encoding="utf-8",
        newline="\n",
    )
    apps = tmp_path / "apps"
    (apps / "dfe-ui").mkdir(parents=True)
    (apps / "dfe-ui" / "values.yaml").write_text(
        "config:\n  secretStoreName: from-the-layer\n", encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr(dfeops, "EXTRAS_CHART", extras)
    monkeypatch.setattr(dfeops, "APPS_VALUES", apps)
    config = dfeops._component_values("dfe-ui", "single").get("config", {})
    assert config.get("secretStoreName") == "from-the-layer", config
    assert config.get("nextauthSecretName") == "from-defaults", config


@pytest.mark.parametrize("mode", dfeops.MODES)
def test_the_preflight_renders_each_apps_extras_and_finds_the_catalogue(mode: str) -> None:
    """The offline render stack-deploy gates on: dfe-extras per app, the engine's catalogue in it."""
    if shutil.which("helm") is None:
        pytest.skip("helm is not on PATH")
    for app in dfeops._PREFLIGHT_APPS:
        done = subprocess.run(
            [*dfeops._extras_render(app, mode), "--set", "appNamespace=preflight"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        assert done.returncode == 0, f"{app} ({mode}): {done.stderr}"
        if app == "dfe-engine":
            banner = dfeops.composition.CATALOGUE_BANNER.splitlines()[0].lstrip("# ")
            assert banner in done.stdout, "the app catalogue ConfigMap carries the manifest"
            assert "  DFE_APP_CATALOGUE_FILE: /etc/dfe-engine/catalogue/apps.yaml\n" in done.stdout
