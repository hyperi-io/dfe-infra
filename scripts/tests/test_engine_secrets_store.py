#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_engine_secrets_store.py
#  Purpose:      Prove the aws root's engine store facts reach dfe-engine as a
#                Secrets Manager store, its sidecars leave its service account,
#                its egress opens for the store and keeps the Pod Identity agent,
#                and nothing moves off AWS.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-engine's own secrets on AWS, from the tofu output to the pod.

The aws root fences a role to ``<path>/engine`` and binds it to the dfe-engine
service account. bootstrap.sh writes the prefix and the deployment key onto the
cluster secret, layer2-apps.yaml hands them to the engine's values, and
dfe-extras turns them into the engine's DFE_SECRETS_* env. The hunt runner and
the keda shim then need accounts of their own, or EKS Pod Identity hands them
the engine's write grant. layer2-platform.yaml opens the engine's egress for the
store, and the Pod Identity agent has to stay reachable through every except.

    python3 -m pytest scripts/tests/test_engine_secrets_store.py -q

The dfe-extras renders need helm and nothing from a registry; the thin-chart
render pulls the scalo-service library (_weave.library).
"""

import ipaddress
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from _weave import REPO_ROOT, helm, render_app, render_extras, weave

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import cloud_run

BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
RENDER_ENV = REPO_ROOT / "bootstrap" / "render_env.py"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
PLATFORM = Path("argocd/appsets/layer2-platform.yaml")

PREFIX = "dfe/test/engine"
KEY = "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"
TAGS = '{"dfe-e2e":"r20261016t000000z-abcdef","service-name":"dfe"}'
# The facts bootstrap writes for an aws root, in RFC 5737 and example shapes only.
AWS_FACTS = {
    "dfe.hyperi.io/region": "us-west-2",
    "dfe.hyperi.io/engine_secrets_prefix": PREFIX,
    "dfe.hyperi.io/kms_key_arn": KEY,
    "dfe.hyperi.io/resource_tags": TAGS,
    "dfe.hyperi.io/pod_cidr": "10.90.0.0/16",
    "dfe.hyperi.io/service_cidr": "172.20.0.0/16",
}
# The same cluster before this fact existed: every cloud but AWS, and an older AWS secret.
NO_STORE = {k: v for k, v in AWS_FACTS.items() if k != "dfe.hyperi.io/engine_secrets_prefix"}
NO_KEY = {k: v for k, v in AWS_FACTS.items() if k != "dfe.hyperi.io/kms_key_arn"}

POD_IDENTITY = ipaddress.ip_address("169.254.170.23")
ENGINE_LABELS = {"app.kubernetes.io/name": "dfe-engine"}

RenderError = weave().WeaveError


def keyed(docs: list[dict]) -> dict[tuple[str, str], dict]:
    return {(d.get("kind"), d["metadata"]["name"]): d for d in docs}


def engine_env(docs: list[dict]) -> dict[str, str]:
    return keyed(docs)[("ConfigMap", "dfe-engine-env")]["data"]


def service_account(docs: list[dict], deployment: str) -> str:
    return keyed(docs)[("Deployment", deployment)]["spec"]["template"]["spec"]["serviceAccountName"]


# ---------------------------------------------------------------- tofu + bootstrap


def test_the_aws_root_hands_bootstrap_the_prefix_and_the_key() -> None:
    outputs = (REPO_ROOT / "terraform" / "environments" / "aws" / "outputs.tf").read_text(encoding="utf-8")
    prefix = outputs.split('output "DFE_ENGINE_SECRETS_PREFIX"')[1].split("\noutput ")[0]
    key = outputs.split('output "DFE_KMS_KEY_ARN"')[1].split("\noutput ")[0]
    assert "module.secrets.engine_name_prefix" in prefix
    assert "module.cluster.kms_key_arn" in key


def _annotations(**env: str) -> list[str]:
    """bootstrap.sh's own two lines, run on their own rather than copied here."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = lines.index('export DFE_ENGINE_SECRETS_PREFIX="${DFE_ENGINE_SECRETS_PREFIX:-}"')
    end = next(i for i, line in enumerate(lines) if line.startswith("export DFE_KMS_KEY_ARN_ANNOTATION="))
    script = "\n".join(lines[start : end + 1])
    script += '\nprintf "%s\\n%s\\n" "$DFE_ENGINE_SECRETS_PREFIX_ANNOTATION" "$DFE_KMS_KEY_ARN_ANNOTATION"'
    done = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script], env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return done.stdout.splitlines()


def test_bootstrap_writes_both_annotations_from_the_roots_outputs() -> None:
    lines = _annotations(DFE_ENGINE_SECRETS_PREFIX=PREFIX, DFE_KMS_KEY_ARN=KEY)
    assert lines == [f'dfe.hyperi.io/engine_secrets_prefix: "{PREFIX}"', f'dfe.hyperi.io/kms_key_arn: "{KEY}"']


def test_a_root_that_names_neither_writes_neither() -> None:
    assert _annotations() == ["", ""]


def test_the_cluster_secret_carries_both() -> None:
    rendered = subprocess.run(
        [sys.executable, str(RENDER_ENV)],
        input=CLUSTER_SECRET.read_text(encoding="utf-8"),
        env={"PATH": "/usr/bin:/bin", **dict(zip(
            ("DFE_ENGINE_SECRETS_PREFIX_ANNOTATION", "DFE_KMS_KEY_ARN_ANNOTATION"),
            _annotations(DFE_ENGINE_SECRETS_PREFIX=PREFIX, DFE_KMS_KEY_ARN=KEY), strict=True,
        ))},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    annotations = yaml.safe_load(rendered)["metadata"]["annotations"]
    assert annotations["dfe.hyperi.io/engine_secrets_prefix"] == PREFIX
    assert annotations["dfe.hyperi.io/kms_key_arn"] == KEY


# ---------------------------------------------------------------- the engine


def test_the_engine_writes_secrets_manager_under_its_prefix_region_key_and_tags() -> None:
    env = engine_env(render_extras("dfe-engine", "scale", "aws", annotations=AWS_FACTS))
    assert {k: v for k, v in env.items() if k.startswith("DFE_SECRETS_")} == {
        "DFE_SECRETS_PROVIDER": "aws",
        "DFE_SECRETS_REGION": "us-west-2",
        "DFE_SECRETS_NAME_PREFIX": PREFIX,
        "DFE_SECRETS_KMS_KEY_ID": KEY,
        "DFE_SECRETS_RECOVERY_WINDOW_DAYS": "0",
        "DFE_SECRETS_TAGS": TAGS,
    }


def _resource_tags_annotation(tags: str) -> str:
    """bootstrap.sh's own annotation lines, then the cluster secret, as Argo reads it back."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = lines.index('export DFE_RESOURCE_TAGS="${DFE_RESOURCE_TAGS:-}"')
    end = next(i for i, line in enumerate(lines) if line.startswith("export DFE_RESOURCE_TAGS_ANNOTATION="))
    script = "\n".join(lines[start : end + 1]) + '\nprintf "%s\\n" "$DFE_RESOURCE_TAGS_ANNOTATION"'
    annotation = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script], env={"PATH": "/usr/bin:/bin", "DFE_RESOURCE_TAGS": tags},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout.strip()
    rendered = subprocess.run(
        [sys.executable, str(RENDER_ENV)],
        input=CLUSTER_SECRET.read_text(encoding="utf-8"),
        env={"PATH": "/usr/bin:/bin", "DFE_RESOURCE_TAGS_ANNOTATION": annotation},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    return yaml.safe_load(rendered)["metadata"]["annotations"]["dfe.hyperi.io/resource_tags"]


def test_a_cycles_run_tags_reach_every_secret_the_engine_creates() -> None:
    """A test account's boundary refuses CreateSecret without both run tags, and the engine sends these as Tags.

    cloud_run.run_tags is the pair the cycle's tofu run renders (test_cloud_run
    holds the two halves equal), and the aws root merges it into DFE_RESOURCE_TAGS.
    """
    run = cloud_run.run_tags("r20261008t000000z-0a1b2c", 1791460800)
    resource_tags = json.dumps({"service-name": "dfe", **run}, separators=(",", ":"))
    facts = {**AWS_FACTS, "dfe.hyperi.io/resource_tags": _resource_tags_annotation(resource_tags)}
    tags = json.loads(engine_env(render_extras("dfe-engine", "scale", "aws", annotations=facts))["DFE_SECRETS_TAGS"])
    assert tags["dfe-e2e"] == "r20261008t000000z-0a1b2c"
    assert tags["expires-at"] == "2026-10-08T12:00:00Z"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", tags["expires-at"])


def test_expected_fail_a_store_without_the_deployment_key_fails_the_render() -> None:
    """An empty key id would encrypt under the account's aws/secretsmanager key instead."""
    with pytest.raises(RenderError, match=re.escape("dfe.hyperi.io/kms_key_arn annotation")):
        render_extras("dfe-engine", "scale", "aws", annotations=NO_KEY)


def test_without_the_prefix_the_engine_keeps_its_files() -> None:
    env = engine_env(render_extras("dfe-engine", "scale", "aws", annotations=NO_STORE))
    assert env["DFE_SECRETS_PATH"] == "/config/.secrets"
    assert "DFE_SECRETS_PROVIDER" not in env


@pytest.mark.parametrize("service", ["dfe-loader", "dfe-receiver", "hyperdx", "dfe-ui"])
def test_no_other_app_moves_with_the_engines_store(service: str) -> None:
    """layer2-apps hands the store to dfe-engine alone."""
    assert render_extras(service, "scale", "aws", annotations=AWS_FACTS) == render_extras(
        service, "scale", "aws", annotations=NO_STORE
    )


@pytest.mark.parametrize(
    ("store", "said"),
    [
        ({"provider": "aws", "region": "", "namePrefix": PREFIX}, "secrets.region is empty"),
        ({"provider": "aws", "region": "us-west-2", "namePrefix": ""}, "secrets.namePrefix is empty"),
        ({"provider": "aws", "region": "us-west-2", "namePrefix": PREFIX}, "secrets.kmsKeyId is empty"),
        ({"provider": "aws", "region": "us-west-2", "namePrefix": PREFIX, "kmsKeyId": KEY, "recoveryWindowDays": 3},
         "takes 0 (no window) or 7-30"),
        ({"provider": "openbao"}, 'secrets.provider is "openbao"'),
    ],
    ids=["no-region", "no-prefix", "no-key", "window-out-of-range", "unwired-provider"],
)
def test_expected_fail_a_store_the_engine_cannot_start_on_fails_the_render(store: dict, said: str) -> None:
    with pytest.raises(RenderError, match=re.escape(said)):
        render_extras("dfe-engine", "single", "aws", overlay={"secrets": store})


def test_the_thin_chart_never_sees_the_store() -> None:
    """Its schema holds every secrets.<key> to a secret group, so a scalar there refuses the render."""
    with_store = render_app("dfe-engine", "scale", "aws", annotations=AWS_FACTS)
    without = render_app("dfe-engine", "scale", "aws", annotations=NO_STORE)
    deployment = ("Deployment", "dfe-engine")
    assert keyed(with_store)[deployment]["spec"] == keyed(without)[deployment]["spec"]
    assert engine_env(with_store)["DFE_SECRETS_PROVIDER"] == "aws"


# ---------------------------------------------------------------- the sidecars


def test_on_aws_the_runner_and_the_shim_run_as_accounts_of_their_own() -> None:
    docs = render_extras("dfe-engine", "scale", "aws", annotations=AWS_FACTS)
    assert service_account(docs, "dfe-hunt-runner") == "dfe-hunt-runner"
    assert service_account(docs, "dfe-keda-shim") == "dfe-keda-shim"
    accounts = keyed(docs)
    for name in ("dfe-hunt-runner", "dfe-keda-shim"):
        assert accounts[("ServiceAccount", name)]["automountServiceAccountToken"] is False
        assert "automountServiceAccountToken" not in accounts[("Deployment", name)]["spec"]["template"]["spec"]
    # The engine's own account is the thin chart's; dfe-extras renders none by that name.
    assert ("ServiceAccount", "dfe-engine") not in accounts


def test_the_runner_still_reads_the_shared_signing_key() -> None:
    docs = render_extras("dfe-engine", "scale", "aws", annotations=AWS_FACTS)
    container = keyed(docs)[("Deployment", "dfe-hunt-runner")]["spec"]["template"]["spec"]["containers"][0]
    jwt = next(e for e in container["env"] if e["name"] == "DFE_API_JWT_SECRET")
    assert jwt["valueFrom"]["secretKeyRef"]["name"] == "dfe-engine-jwt"


def test_without_the_store_the_sidecars_keep_the_engines_account() -> None:
    docs = render_extras("dfe-engine", "scale", "aws", annotations=NO_STORE)
    assert service_account(docs, "dfe-hunt-runner") == "dfe-engine"
    assert service_account(docs, "dfe-keda-shim") == "dfe-engine"
    # The only account left is the one ESO reads the collector's password through.
    accounts = [k[1] for k in keyed(docs) if k[0] == "ServiceAccount"]
    assert accounts == ["otel-writer-clickhouse-reader"]


# ---------------------------------------------------------------- the egress


def network_policies(facts: dict[str, str]) -> list[dict]:
    """network-policies as layer2-platform's Application renders it on aws."""
    w = weave()
    doc = w.load_appset(REPO_ROOT, PLATFORM)
    element = next(
        e
        for generator in doc["spec"]["generators"]
        for nested in generator["matrix"]["generators"]
        for e in (nested.get("list") or {}).get("elements") or []
        if e["app"] == "network-policies"
    )
    target = w.Target(element["app"], "scale", "aws", annotations=tuple(sorted(facts.items())))
    app = w.expand_template(doc["spec"]["template"], {**w.generator_params(target, None), **element}, helm())
    source = app["spec"]["sources"][0]
    chart = REPO_ROOT / source["path"]
    with tempfile.TemporaryDirectory() as tmp:
        deploy = Path(tmp)
        chain = []
        for raw in source["helm"].get("valueFiles") or []:
            path = deploy / raw.removeprefix("$values/") if raw.startswith("$values/") else (chart / raw).resolve()
            chain.append({"written": raw, "path": path, "present": path.is_file()})
        docs = w.render_source(
            source, chart=chart, release=element["app"], namespace=element["namespace"], chain=chain, helm=helm()
        )
    return [d for d in docs if d.get("kind") == "NetworkPolicy"]


def _selects(selector: dict, labels: dict[str, str]) -> bool:
    """Whether a podSelector picks a pod carrying ``labels``."""
    if any(labels.get(k) != v for k, v in (selector.get("matchLabels") or {}).items()):
        return False
    for expression in selector.get("matchExpressions") or []:
        key, operator, values = expression["key"], expression["operator"], expression.get("values") or []
        held = key in labels
        if (operator == "DoesNotExist" and held) or (operator == "Exists" and not held):
            return False
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and held and labels[key] in values:
            return False
    return True


def engine_egress(facts: dict[str, str]) -> list[dict]:
    """Every egress rule of every policy that selects the engine's pod, in its namespace."""
    return [
        rule
        for p in network_policies(facts)
        if p["metadata"]["namespace"] == "dfe"
        and "Egress" in p["spec"].get("policyTypes", [])
        and _selects(p["spec"]["podSelector"], ENGINE_LABELS)
        for rule in p["spec"].get("egress") or []
    ]


def test_the_store_opens_the_engines_secrets_egress_on_443_alone() -> None:
    policies = [p for p in network_policies(AWS_FACTS) if p["metadata"]["name"].startswith("allow-external-secrets-")]
    assert [p["spec"]["podSelector"] for p in policies] == [
        {"matchLabels": ENGINE_LABELS},
        {"matchLabels": {"dfe.hyperi.io/app": "dfe-engine"}},
    ]
    ports = {(port["port"], port["protocol"]) for p in policies for r in p["spec"]["egress"] for port in r["ports"]}
    assert ports == {(443, "TCP")}


def test_without_the_store_no_secrets_egress_renders() -> None:
    assert not [p for p in network_policies(NO_STORE) if p["metadata"]["name"].startswith("allow-external-secrets-")]


@pytest.mark.parametrize("facts", [AWS_FACTS, NO_STORE], ids=["store", "no-store"])
def test_the_engine_reaches_the_pod_identity_agent_through_every_except(facts: dict[str, str]) -> None:
    """The VPC CNI agent enforces an except as a deny and resolves by longest prefix.

    Kubernetes ORs the rules, so the /32 allow wins there regardless. The agent
    instead turns every except on the pod into a deny entry and lets the longest
    prefix decide (aws/aws-network-policy-agent#326), so the agent's address must
    be named by an allow at /32 and by no except as specific as that.
    """
    rules = engine_egress(facts)
    allows = [
        (ipaddress.ip_network(peer["ipBlock"]["cidr"]), rule)
        for rule in rules
        for peer in rule.get("to") or []
        if "ipBlock" in peer and POD_IDENTITY in ipaddress.ip_network(peer["ipBlock"]["cidr"])
    ]
    agent = [
        (net, rule) for net, rule in allows
        if net.prefixlen == 32 and {(p["port"], p.get("protocol", "TCP")) for p in rule["ports"]} == {(80, "TCP")}
    ]
    assert agent, f"no rule selecting the engine allows {POD_IDENTITY}/32 on TCP 80"
    assert all("except" not in peer["ipBlock"] for _net, rule in agent for peer in rule["to"])
    excepts = [
        ipaddress.ip_network(cidr)
        for rule in rules
        for peer in rule.get("to") or []
        for cidr in (peer.get("ipBlock") or {}).get("except") or []
    ]
    covering = [net for net in excepts if POD_IDENTITY in net]
    assert all(net.prefixlen < 32 for net in covering), covering
    # The carve-out reaches the engine on aws, so the longest-prefix rule is what keeps the agent.
    assert covering, "no except covers the agent, so this test no longer exercises the carve-out"
