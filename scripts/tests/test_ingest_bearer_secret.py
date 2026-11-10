#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_ingest_bearer_secret.py
#  Purpose:      Prove dfe-engine's file store hands dfe-receiver its bearer
#                tokens through one Secret: rendered empty, writable by the
#                engine alone, mounted read-only by the receiver, and never
#                reset by Argo.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-receiver's ingest bearer tokens off a Secrets Manager store, from the chart to the pod.

The engine compiles each bearer listener's source as
``file:/var/run/secrets/dfe/ingest-bearer-tokens/<listener>`` while it keeps what it
mints on its own volume, and mirrors the tokens into the Secret common.yaml names.
dfe-extras renders that Secret empty beside the engine, with a Role granting the
engine's account get and update on it alone, and the receiver mounts it.

    python3 -m pytest scripts/tests/test_ingest_bearer_secret.py -q

The dfe-extras renders need helm alone. The thin-chart renders pull the
scalo-service library (_weave.library).
"""

import re
from pathlib import Path

import pytest
import yaml

from _weave import REPO_ROOT, render_app, render_extras, weave

COMMON = REPO_ROOT / "argocd" / "values" / "common.yaml"
APPS = Path("argocd/appsets/layer2-apps.yaml")
_COMMON = yaml.safe_load(COMMON.read_text(encoding="utf-8"))
SECRET = _COMMON["ingestBearer"]["secretName"]
# The Secret the engine mirrors hunt alert destinations into, for dfe-hunt-runner.
ALERTS = _COMMON["alertDestinations"]["secretName"]
# common.yaml key -> the Secret it names. Both are rendered by dfe-engine.mirrorSecret.
MIRRORS = {"ingestBearer": SECRET, "alertDestinations": ALERTS}
# dfe-engine's RECEIVER_BEARER_MOUNT, the directory its file: sources name.
MOUNT = "/var/run/secrets/dfe/ingest-bearer-tokens"

# The fact that moves the engine's store to Secrets Manager, in example shapes only.
AWS_STORE = {
    "dfe.hyperi.io/region": "us-west-2",
    "dfe.hyperi.io/engine_secrets_prefix": "dfe/test/engine",
    "dfe.hyperi.io/kms_key_arn": "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000",
}


def keyed(docs: list[dict]) -> dict[tuple[str, str], dict]:
    return {(d.get("kind"), d["metadata"]["name"]): d for d in docs}


def pod_spec(docs: list[dict], deployment: str) -> dict:
    return keyed(docs)[("Deployment", deployment)]["spec"]["template"]["spec"]


# ---------------------------------------------------------------- the engine


@pytest.mark.parametrize("cloud", ["local", "rancher", "gcp", "azure", "aws"])
@pytest.mark.parametrize("profile", ["slim", "single", "scale", "mesh"])
def test_off_secrets_manager_the_secrets_render_empty(profile: str, cloud: str) -> None:
    docs = keyed(render_extras("dfe-engine", profile, cloud))
    for name in MIRRORS.values():
        secret = docs[("Secret", name)]
        assert secret["type"] == "Opaque"
        assert "data" not in secret
        assert "stringData" not in secret


@pytest.mark.parametrize("name", sorted(MIRRORS.values()))
def test_the_engine_may_get_and_update_each_secret_and_nothing_else(name: str) -> None:
    docs = keyed(render_extras("dfe-engine", "scale", "local"))
    role = docs[("Role", f"{name}-writer")]
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": [name], "verbs": ["get", "update"]}
    ]
    binding = docs[("RoleBinding", f"{name}-writer")]
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": f"{name}-writer"}
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "dfe-engine", "namespace": "dfe"}]
    # Every grant on Secrets the engine's render carries names its Secrets.
    grants = [d for d in docs.values() if d.get("kind") in ("Role", "ClusterRole")]
    secrets_rules = [r for g in grants for r in g.get("rules") or [] if "secrets" in r.get("resources", [])]
    assert all(r.get("resourceNames") for r in secrets_rules)


def test_the_binding_names_the_account_the_engine_pod_runs_as_with_its_token() -> None:
    docs = render_app("dfe-engine", "scale", "local")
    pod = pod_spec(docs, "dfe-engine")
    binding = keyed(docs)[("RoleBinding", f"{SECRET}-writer")]
    assert binding["subjects"][0]["name"] == pod["serviceAccountName"]
    # The provider authenticates with the pod's token, so the engine mounts it.
    assert pod.get("automountServiceAccountToken") is not False


def test_the_engine_is_told_the_secret_it_writes() -> None:
    env = keyed(render_extras("dfe-engine", "scale", "local"))[("ConfigMap", "dfe-engine-env")]["data"]
    assert env["DFE_ENGINE_INGEST_BEARER_SECRET"] == SECRET
    assert env["DFE_ENGINE_HUNTS_ALERT_DESTINATIONS_SECRET"] == ALERTS
    assert env["DFE_ENGINE_SECRETS_PATH"] == "/config/.secrets"


def test_on_a_secrets_manager_store_none_of_it_renders() -> None:
    docs = render_extras("dfe-engine", "scale", "aws", annotations=AWS_STORE)
    assert not [d for d in docs if any(n in d["metadata"]["name"] for n in MIRRORS.values())]
    env = keyed(docs)[("ConfigMap", "dfe-engine-env")]["data"]
    assert "DFE_ENGINE_INGEST_BEARER_SECRET" not in env
    assert "DFE_ENGINE_HUNTS_ALERT_DESTINATIONS_SECRET" not in env
    assert env["DFE_ENGINE_SECRETS_PROVIDER"] == "aws"


@pytest.mark.parametrize("key", sorted(MIRRORS))
def test_expected_fail_an_empty_name_fails_the_render(key: str) -> None:
    with pytest.raises(weave().WeaveError, match=re.escape(f"{key}.secretName is empty")):
        render_extras("dfe-engine", "single", "local", overlay={key: {"secretName": ""}})


@pytest.mark.parametrize("facts", [{}, AWS_STORE], ids=["file-store", "secrets-manager-store"])
@pytest.mark.parametrize("cloud", ["local", "gcp", "aws"])
def test_the_sidecars_run_as_accounts_of_their_own_with_no_token(cloud: str, facts: dict[str, str]) -> None:
    docs = render_extras("dfe-engine", "scale", cloud, annotations=facts)
    accounts = keyed(docs)
    for name in ("dfe-hunt-runner", "dfe-keda-shim"):
        assert pod_spec(docs, name)["serviceAccountName"] == name
        assert accounts[("ServiceAccount", name)]["automountServiceAccountToken"] is False


@pytest.mark.parametrize("name", sorted(MIRRORS.values()))
def test_only_the_engines_pod_runs_as_the_account_the_secret_is_bound_to(name: str) -> None:
    docs = render_app("dfe-engine", "scale", "local")
    bound = keyed(docs)[("RoleBinding", f"{name}-writer")]["subjects"][0]["name"]
    runners = sorted(
        d["metadata"]["name"]
        for d in docs
        if d.get("kind") == "Deployment" and d["spec"]["template"]["spec"].get("serviceAccountName") == bound
    )
    assert runners == ["dfe-engine"]


@pytest.mark.parametrize("sidecar", ["dfe-hunt-runner", "dfe-keda-shim"])
def test_a_sidecar_with_no_token_still_logs_its_namespace(sidecar: str) -> None:
    container = pod_spec(render_extras("dfe-engine", "scale", "local"), sidecar)["containers"][0]
    namespace = next(e for e in container["env"] if e["name"] == "POD_NAMESPACE")
    assert namespace["valueFrom"] == {"fieldRef": {"fieldPath": "metadata.namespace"}}


@pytest.mark.parametrize("service", ["dfe-loader", "dfe-receiver", "dfe-hyperdx", "dfe-ui"])
def test_no_other_app_renders_the_secrets_or_their_grants(service: str) -> None:
    docs = render_extras(service, "scale", "local")
    assert not [d for d in docs if any(n in d["metadata"]["name"] for n in MIRRORS.values())]


# ---------------------------------------------------------------- the receiver


@pytest.mark.parametrize("facts", [{}, AWS_STORE], ids=["file-store", "secrets-manager-store"])
def test_the_receiver_mounts_the_secret_read_only_as_a_directory(facts: dict[str, str]) -> None:
    pod = pod_spec(render_app("dfe-receiver", "scale", "aws", annotations=facts), "dfe-receiver")
    volume = next(v for v in pod["volumes"] if v["name"] == "ingest-bearer-tokens")
    assert volume == {"name": "ingest-bearer-tokens", "secret": {"secretName": SECRET, "optional": True}}
    mounts = [m for c in pod["containers"] for m in c.get("volumeMounts") or [] if m["name"] == "ingest-bearer-tokens"]
    # subPath would freeze the file at pod start, and the receiver re-reads it.
    assert mounts == [{"name": "ingest-bearer-tokens", "mountPath": MOUNT, "readOnly": True}]


# ---------------------------------------------------------------- Argo


def test_argo_never_takes_the_engines_keys_back() -> None:
    doc = weave().load_appset(REPO_ROOT, APPS)
    spec = doc["spec"]["template"]["spec"]
    ignored = [d for d in spec["ignoreDifferences"] if d.get("kind") == "Secret"]
    assert ignored == [{"kind": "Secret", "name": n, "jsonPointers": ["/data"]} for n in (SECRET, ALERTS)]
    options = spec["syncPolicy"]["syncOptions"]
    # Applied during the sync, not only in the diff view, and server-side, so data
    # the chart never names stays the engine's.
    assert "RespectIgnoreDifferences=true" in options
    assert "ServerSideApply=true" in options
