#  Project:      dfe-infra
#  File:         scripts/tests/test_edge_gateway_fence.py
#  Purpose:      Prove the public gateway's allow-list travels from bootstrap's
#                environment to the gateway chart -- both halves or neither -- and
#                that a deployment naming no fence renders exactly what it did.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The gateway fence, from the cluster secret to the load balancer.

The aws root emits DFE_EDGE_ALLOWED_CIDRS and DFE_EDGE_TRUSTED_PROXY_CIDRS
(terraform/modules/edge/aws asserts how), bootstrap.sh writes them onto the
cluster secret, and layer2-edge.yaml hands them to the gateway chart as
ui.allowed_cidrs and ui.trusted_proxy_cidrs. A miss anywhere renders empty and
leaves the load balancer open to every address, with no error at all.

The appset's values block is rendered here by helm, which runs the same Go
template engine and Sprig functions Argo CD's goTemplate does.

    python3 -m pytest scripts/tests/test_edge_gateway_fence.py -q
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from _charts import chart_dir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-edge.yaml"
VALUES = REPO_ROOT / "argocd" / "values"

ALLOWED = "20.1.2.3/32,203.0.113.7/32"
TRUSTED = "10.90.128.0/20,10.90.144.0/20,10.90.160.0/20"
ALLOWED_KEY = "dfe.hyperi.io/edge_allowed_cidrs"
TRUSTED_KEY = "dfe.hyperi.io/edge_trusted_proxy_cidrs"


# --- bootstrap.sh: both annotations or neither ------------------------------------


def _bootstrap_block() -> str:
    """bootstrap.sh's own fence block, run on its own rather than copied here."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = lines.index('export DFE_EDGE_ALLOWED_CIDRS="${DFE_EDGE_ALLOWED_CIDRS:-}"')
    end = next(i for i, line in enumerate(lines) if line.startswith("export DFE_EDGE_TRUSTED_PROXY_CIDRS_ANNOTATION="))
    return "\n".join(lines[start : end + 1])


def _run_block(**env: str) -> subprocess.CompletedProcess:
    script = (
        _bootstrap_block()
        + '\nprintf "%s\\n%s\\n" "$DFE_EDGE_ALLOWED_CIDRS_ANNOTATION" "$DFE_EDGE_TRUSTED_PROXY_CIDRS_ANNOTATION"'
    )
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def test_a_fence_writes_both_annotations() -> None:
    done = _run_block(DFE_EDGE_ALLOWED_CIDRS=ALLOWED, DFE_EDGE_TRUSTED_PROXY_CIDRS=TRUSTED)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [f'{ALLOWED_KEY}: "{ALLOWED}"', f'{TRUSTED_KEY}: "{TRUSTED}"']


def test_no_fence_writes_neither() -> None:
    done = _run_block()
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["", ""]


@pytest.mark.parametrize("half", [
    {"DFE_EDGE_ALLOWED_CIDRS": ALLOWED},
    {"DFE_EDGE_TRUSTED_PROXY_CIDRS": TRUSTED},
])
def test_expected_fail_half_a_fence_is_refused_by_name(half: dict[str, str]) -> None:
    """The chart refuses the pair split, and a sync failure surfaces long after bootstrap."""
    done = _run_block(**half)
    assert done.returncode == 1
    assert "DFE_EDGE_ALLOWED_CIDRS and DFE_EDGE_TRUSTED_PROXY_CIDRS" in done.stderr


def _fence_notice(**env: str) -> subprocess.CompletedProcess:
    """bootstrap.sh's fence notice and the cloud test it calls, run on their own."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    providers = next(line for line in lines if line.startswith("DFE_CLOUD_LB_PROVIDERS="))
    helper = lines.index("dfe_cloud_programs_loadbalancers() {")
    start = lines.index("dfe_edge_fence_notice() {")
    call = lines.index("dfe_edge_fence_notice", start)
    script = "\n".join([providers, *lines[helper : helper + 3], *lines[start : call + 1]])
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={"PATH": "/usr/bin:/bin", "DFE_EDGE_ENABLED": "true", "DFE_EDGE_ALLOWED_CIDRS": "",
             "DFE_CLOUD": "aws", **env},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


@pytest.mark.parametrize("cloud", ["aws", "gcp", "azure"])
def test_no_fence_on_a_cloud_says_the_public_web_is_closed(cloud: str) -> None:
    done = _fence_notice(DFE_CLOUD=cloud)
    assert done.returncode == 0, done.stderr
    assert "Public web: CLOSED" in done.stdout
    assert done.stderr == ""


@pytest.mark.parametrize(("cloud", "edge"), [("local", "true"), ("aws", "false")])
def test_no_notice_where_nothing_is_internet_facing(cloud: str, edge: str) -> None:
    done = _fence_notice(DFE_CLOUD=cloud, DFE_EDGE_ENABLED=edge)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


@pytest.mark.parametrize("fence", ["0.0.0.0/0", "203.0.113.7/32, ::/0", "10.0.0.0/0"])
def test_an_allow_all_fence_is_a_warning(fence: str) -> None:
    done = _fence_notice(DFE_EDGE_ALLOWED_CIDRS=fence)
    assert done.returncode == 0, done.stderr
    assert "WARNING: DFE_EDGE_ALLOWED_CIDRS carries" in done.stderr
    assert "admits every address" in done.stderr
    assert done.stdout == ""


def test_a_listed_fence_says_nothing() -> None:
    assert _fence_notice(DFE_EDGE_ALLOWED_CIDRS=ALLOWED).stderr == ""


def test_the_cluster_secret_carries_both_annotations() -> None:
    rendered = subprocess.run(
        ["envsubst"],
        input=CLUSTER_SECRET.read_text(encoding="utf-8"),
        env={
            "PATH": "/usr/bin:/bin",
            "DFE_EDGE_ALLOWED_CIDRS_ANNOTATION": f'{ALLOWED_KEY}: "{ALLOWED}"',
            "DFE_EDGE_TRUSTED_PROXY_CIDRS_ANNOTATION": f'{TRUSTED_KEY}: "{TRUSTED}"',
        },
        capture_output=True, text=True, check=True,
    ).stdout
    annotations = yaml.safe_load(rendered)["metadata"]["annotations"]
    assert annotations[ALLOWED_KEY] == ALLOWED
    assert annotations[TRUSTED_KEY] == TRUSTED


# --- layer2-edge.yaml: the appset hands the chart both keys ------------------------


def _gateway_values_block() -> str:
    gateway = next(yaml.safe_load_all(APPSET.read_text(encoding="utf-8")))
    return gateway["spec"]["template"]["spec"]["sources"][0]["helm"]["values"]


def _render_values_block(tmp_path: Path, annotations: dict[str, str]) -> dict:
    """The appset's values block for a cluster carrying these annotations."""
    chart = tmp_path / "appset-values"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: appset-values\nversion: 0.1.0\n", encoding="utf-8")
    (chart / "templates" / "values.yaml").write_text(
        "{{- with .Values.cluster }}\n" + _gateway_values_block() + "\n{{- end }}\n", encoding="utf-8"
    )
    cluster = tmp_path / "cluster.yaml"
    cluster.write_text(yaml.safe_dump({"cluster": {"metadata": {"annotations": annotations}}}), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "appset-values", str(chart), "-f", str(cluster)],
        capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, out.stderr
    return yaml.safe_load(out.stdout) or {}


def test_the_appset_hands_the_gateway_both_halves_of_a_fence(tmp_path: Path) -> None:
    values = _render_values_block(tmp_path, {ALLOWED_KEY: ALLOWED, TRUSTED_KEY: TRUSTED})
    assert values["ui"] == {"allowed_cidrs": ALLOWED, "trusted_proxy_cidrs": TRUSTED}


def test_a_cluster_with_no_fence_gets_no_ui_key_at_all(tmp_path: Path) -> None:
    """An empty ui.allowed_cidrs would override a deployer's own overlay with nothing."""
    assert "ui" not in _render_values_block(tmp_path, {})


# --- the gateway chart: what the fence actually fences ------------------------------


def _gateway(*extra: str) -> list[dict]:
    out = subprocess.run(
        ["helm", "template", "envoy-gateway-config", str(chart_dir("envoy-gateway-config")),
         "--namespace", "envoy-gateway-system",
         "-f", str(VALUES / "common.yaml"),
         "-f", str(VALUES / "aws.yaml"),
         "-f", str(VALUES / "edge-aws.yaml"),
         "--set", "appNamespace=dfe",
         "--set", "domain=dfe.example.com",
         *extra],
        capture_output=True, text=True, check=False, cwd=REPO_ROOT,
    )
    assert out.returncode == 0, out.stderr
    return [doc for doc in yaml.safe_load_all(out.stdout) if doc]


def _source_ranges(docs: list[dict]) -> list[str]:
    proxy = next(d for d in docs if d["kind"] == "EnvoyProxy")
    return proxy["spec"]["provider"]["kubernetes"]["envoyService"].get("loadBalancerSourceRanges", [])


def test_the_fence_closes_the_aws_load_balancer_to_everyone_else(tmp_path: Path) -> None:
    fence = tmp_path / "fence.yaml"
    fence.write_text(yaml.safe_dump(_render_values_block(tmp_path, {ALLOWED_KEY: ALLOWED, TRUSTED_KEY: TRUSTED})),
                     encoding="utf-8")
    assert _source_ranges(_gateway("-f", str(fence))) == ALLOWED.split(",")


def test_no_fence_leaves_the_aws_load_balancer_as_it_was() -> None:
    assert _source_ranges(_gateway()) == []
