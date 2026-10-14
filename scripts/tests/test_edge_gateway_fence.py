#  Project:      dfe-infra
#  File:         scripts/tests/test_edge_gateway_fence.py
#  Purpose:      Prove the deployment's own egress addresses, and any CDN ranges
#                in front of the gateway, travel from bootstrap's environment to
#                the gateway chart, that no allow list rides the cluster secret,
#                and that a deployment writing neither renders exactly what it did.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The gateway's self ranges, from the cluster secret to the load balancer.

The aws root emits DFE_EDGE_SELF_RANGES and DFE_EDGE_TRUSTED_PROXY_CIDRS
(terraform/modules/edge/aws asserts how), bootstrap.sh writes them onto the
cluster secret, and layer2-edge.yaml hands them to the gateway chart as
exposure.selfRanges and ui.trusted_proxy_cidrs. A miss anywhere renders empty
with no error at all. Who may reach each surface is the chart's exposure.groups,
which only the deploy repo sets: an allow list on the cluster secret would beat
that overlay at every sync, which is what made the old list deploy-time only.

The appset's values block is rendered here by helm, which runs the same Go
template engine and Sprig functions Argo CD's goTemplate does.

    python3 -m pytest scripts/tests/test_edge_gateway_fence.py -q
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from _charts import chart_dir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
RENDER_ENV = REPO_ROOT / "bootstrap" / "render_env.py"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-edge.yaml"
VALUES = REPO_ROOT / "argocd" / "values"

SELF = "203.0.113.7/32,203.0.113.8/32"
# A CDN's edge ranges: the only hops whose X-Forwarded-For the gateway believes.
TRUSTED = "192.0.2.0/25,2001:db8:cd::/48"
SELF_KEY = "dfe.hyperi.io/edge_self_ranges"
TRUSTED_KEY = "dfe.hyperi.io/edge_trusted_proxy_cidrs"
RETIRED_KEY = "dfe.hyperi.io/edge_allowed_cidrs"


# --- bootstrap.sh: each fact on its own annotation, a /0 refused ------------------


def _bootstrap_block() -> str:
    """bootstrap.sh's own block, run on its own rather than copied here."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = lines.index('DFE_EDGE_SELF_RANGES="${DFE_EDGE_SELF_RANGES:-}"')
    end = next(i for i, line in enumerate(lines) if line.startswith("export DFE_EDGE_TRUSTED_PROXY_CIDRS_ANNOTATION="))
    return "\n".join(lines[start : end + 1])


def _run_block(**env: str) -> subprocess.CompletedProcess:
    script = (
        _bootstrap_block()
        + '\nprintf "%s\\n%s\\n" "$DFE_EDGE_SELF_RANGES_ANNOTATION" "$DFE_EDGE_TRUSTED_PROXY_CIDRS_ANNOTATION"'
    )
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def test_both_facts_write_both_annotations() -> None:
    done = _run_block(DFE_EDGE_SELF_RANGES=SELF, DFE_EDGE_TRUSTED_PROXY_CIDRS=TRUSTED)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [f'{SELF_KEY}: "{SELF}"', f'{TRUSTED_KEY}: "{TRUSTED}"']


def test_neither_writes_neither() -> None:
    done = _run_block()
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["", ""]


def test_cdn_ranges_stand_alone_now_that_no_list_rides_beside_them() -> None:
    done = _run_block(DFE_EDGE_TRUSTED_PROXY_CIDRS=TRUSTED)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["", f'{TRUSTED_KEY}: "{TRUSTED}"']


def test_spaces_come_out_of_the_self_ranges() -> None:
    """layer2-edge.yaml splits on commas alone, so a space would become part of a range."""
    done = _run_block(DFE_EDGE_SELF_RANGES="203.0.113.7/32, 203.0.113.8/32")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == f'{SELF_KEY}: "{SELF}"'


@pytest.mark.parametrize("trusted", ["0.0.0.0/0", "::/0", "192.0.2.0/25, ::/0", "192.0.2.0/25,0.0.0.0/0"])
def test_expected_fail_a_trusted_range_of_every_address_is_refused(trusted: str) -> None:
    """Every peer is then trusted, so the fence reads a header the client writes."""
    done = _run_block(DFE_EDGE_SELF_RANGES=SELF, DFE_EDGE_TRUSTED_PROXY_CIDRS=trusted)
    assert done.returncode == 1, done.stdout
    assert "which trusts every address" in done.stderr, done.stderr
    assert done.stdout == ""


@pytest.mark.parametrize("ranges", ["0.0.0.0/0", "203.0.113.7/32,::/0"])
def test_expected_fail_a_self_range_of_every_address_is_refused(ranges: str) -> None:
    """Every web group admits the self ranges, so a /0 opens them all."""
    done = _run_block(DFE_EDGE_SELF_RANGES=ranges)
    assert done.returncode == 1, done.stdout
    assert "DFE_EDGE_SELF_RANGES carries" in done.stderr, done.stderr
    assert done.stdout == ""


def test_bootstrap_no_longer_reads_or_writes_an_allow_list() -> None:
    body = BOOTSTRAP.read_text(encoding="utf-8")
    assert "DFE_EDGE_ALLOWED_CIDRS" not in body
    assert RETIRED_KEY not in body


def test_the_cluster_secret_carries_both_annotations() -> None:
    rendered = subprocess.run(
        [sys.executable, str(RENDER_ENV)],
        input=CLUSTER_SECRET.read_text(encoding="utf-8"),
        env={
            "PATH": "/usr/bin:/bin",
            "DFE_EDGE_SELF_RANGES_ANNOTATION": f'{SELF_KEY}: "{SELF}"',
            "DFE_EDGE_TRUSTED_PROXY_CIDRS_ANNOTATION": f'{TRUSTED_KEY}: "{TRUSTED}"',
        },
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    annotations = yaml.safe_load(rendered)["metadata"]["annotations"]
    assert annotations[SELF_KEY] == SELF
    assert annotations[TRUSTED_KEY] == TRUSTED
    assert RETIRED_KEY not in annotations


# --- layer2-edge.yaml: the appset hands the chart both facts and no allow list -------


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
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert out.returncode == 0, out.stderr
    return yaml.safe_load(out.stdout) or {}


def test_the_appset_hands_the_gateway_both_facts(tmp_path: Path) -> None:
    values = _render_values_block(tmp_path, {SELF_KEY: SELF, TRUSTED_KEY: TRUSTED})
    assert values["exposure"] == {"selfRanges": SELF.split(",")}
    assert values["ui"] == {"trusted_proxy_cidrs": TRUSTED}


def test_expected_fail_no_annotation_lands_on_an_exposure_group(tmp_path: Path) -> None:
    """An inline value beats the deploy-repo overlay at every sync, so the groups stay the deploy repo's."""
    values = _render_values_block(tmp_path, {SELF_KEY: SELF, TRUSTED_KEY: TRUSTED, RETIRED_KEY: "198.51.100.0/24"})
    assert "groups" not in values.get("exposure", {}), values
    assert "allowed_cidrs" not in values.get("ui", {}), values


def test_a_cluster_with_neither_fact_gets_neither_key(tmp_path: Path) -> None:
    """An empty value would override a deployer's own overlay with nothing."""
    values = _render_values_block(tmp_path, {})
    assert "exposure" not in values
    assert "ui" not in values


# --- the gateway chart: what the self ranges reach ------------------------------------


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
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, cwd=REPO_ROOT,
    )
    assert out.returncode == 0, out.stderr
    return [doc for doc in yaml.safe_load_all(out.stdout) if doc]


def _source_ranges(docs: list[dict]) -> list[str]:
    proxy = next(d for d in docs if d["kind"] == "EnvoyProxy")
    return proxy["spec"]["provider"]["kubernetes"]["envoyService"].get("loadBalancerSourceRanges", [])


@pytest.mark.parametrize("cdn", [{TRUSTED_KEY: TRUSTED}, {}], ids=["cdn", "no cdn"])
def test_the_self_ranges_close_the_aws_load_balancer_to_everyone_else(tmp_path: Path, cdn: dict[str, str]) -> None:
    """With no exposure group list yet, the deployment's own egress is all the load balancer admits."""
    facts = tmp_path / "facts.yaml"
    facts.write_text(yaml.safe_dump(_render_values_block(tmp_path, {SELF_KEY: SELF, **cdn})), encoding="utf-8")
    assert _source_ranges(_gateway("-f", str(facts))) == SELF.split(",")


def test_a_deploy_repo_list_joins_the_self_ranges_on_the_load_balancer(tmp_path: Path) -> None:
    facts = tmp_path / "facts.yaml"
    facts.write_text(yaml.safe_dump(_render_values_block(tmp_path, {SELF_KEY: SELF})), encoding="utf-8")
    overlay = tmp_path / "infra-common.yaml"
    overlay.write_text(yaml.safe_dump({"exposure": {"groups": {"console": {"allow": ["198.51.100.0/24"]}}}}),
                       encoding="utf-8")
    # Argo layers the deploy repo's file before the inline values; Helm merges both.
    assert sorted(_source_ranges(_gateway("-f", str(overlay), "-f", str(facts)))) == sorted(
        ["198.51.100.0/24", *SELF.split(",")]
    )


def test_no_fact_leaves_the_aws_load_balancer_as_it_was() -> None:
    assert _source_ranges(_gateway()) == []
