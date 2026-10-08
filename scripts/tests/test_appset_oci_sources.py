#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_appset_oci_sources.py
#  Purpose:      Prove every DFE component's Application pulls its thin chart
#                over OCI by digest beside dfe-extras, with the value layers,
#                refs and parameters the chart switch rests on.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The layer 2 appsets' four-source Application, rendered for every component.

    python3 -m pytest scripts/tests/test_appset_oci_sources.py -q

Each Application is expanded by scripts/dfe-weave, which renders the appset's Go
templates through helm against a cluster secret's facts. The appsets refuse a
chart pin that is not a published digest, so the shape is read from the copy
_weave.appset() pins, and the refusals from the committed text with one entry
broken.

The order is the one Argo needs: (1) the thin chart, oci://<registry>/charts/<chart>
at path "." and a sha256 digest; (2) helm/charts/dfe-extras at target_revision;
(3) this repo as ref `infra` at the same revision; (4) the deploy repo as ref
`values`. Both helm sources read nine value files in order -- the stack's
common, cloud and profile files, the component's apps/_common.yaml, values.yaml,
cloud and profile files, then the deploy repo's infra/common.yaml and instance
file -- and culvert's edge Application reads its flavour file after the cloud one.

Needs helm on PATH (DFE_WEAVE_HELM).
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from _weave import (
    CONTRACTS,
    DIGEST_RE,
    RENDER_DIGEST,
    REPO_ROOT,
    appset,
    drift,
    helm,
    old_appset,
    weave,
)

w = weave()

# Every component a layer 2 appset deploys, by deploy.service.
COMPONENTS = (
    "culvert",
    "dfe-archiver",
    "dfe-engine",
    "dfe-fetcher",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-elastic",
    "dfe-transform-vector",
    "dfe-transform-vrl",
    "dfe-ui",
    "hyperdx",
)
APPS = "layer2-apps.yaml"
EDGE = "layer2-edge.yaml"
# culvert from the edge module, and from layer2-apps' bridge for a cluster it does not reach.
CASES = [(s, APPS) for s in COMPONENTS if s != "culvert"] + [("culvert", EDGE), ("culvert", APPS)]
IDS = [f"{service}@{name}" for service, name in CASES]

EXTRAS_PATH = "helm/charts/dfe-extras"
# Revisions that differ, so a source reading the wrong annotation shows.
INFRA_REVISION = "2.2.1"
DEPLOY_REVISION = "deploy-main"
# RFC 5737, no deployment's own.
RECEIVER_ADDRESS = "203.0.113.10"
FACTS = {
    "dfe.hyperi.io/target_revision": INFRA_REVISION,
    "dfe.hyperi.io/config_repo_revision": DEPLOY_REVISION,
    "dfe.hyperi.io/receiver_address": RECEIVER_ADDRESS,
}
# The facts a managed broker adds, which steer the inline values block.
MANAGED = {
    "dfe.hyperi.io/kafka_mode": "external",
    "dfe.hyperi.io/kafka_bootstrap": "broker.example.com:9096",
    "dfe.hyperi.io/kafka_security_protocol": "SASL_SSL",
    "dfe.hyperi.io/kafka_message_max_bytes": "8388608",
}
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"


def chart_name(service: str) -> str:
    """The thin chart's name, which its contract's app_name sets."""
    return "dfe-hyperdx" if service == "hyperdx" else service


def application(
    service: str,
    appset_name: str,
    *,
    profile: str = "scale",
    cloud: str = "aws",
    facts: dict[str, str] | None = None,
    path: Path | None = None,
) -> dict:
    """The Application an appset generates for one component's default instance."""
    annotations = tuple(sorted({**FACTS, **(facts or {})}.items()))
    target = w.Target(service, profile, cloud, annotations=annotations)
    return w.application(REPO_ROOT, path or appset(appset_name), target, None, helm())


def flavour(cloud: str) -> str:
    """The edge flavour file a cloud reads, as layer2-edge.yaml maps it."""
    return "onprem" if cloud in ("local", "local-dfe", "rancher") else cloud


def layers(service: str, appset_name: str, profile: str, cloud: str) -> list[str]:
    """The value files both helm sources read, in order, once rendered."""
    stack = ["$infra/argocd/values/common.yaml", f"$infra/argocd/values/{cloud}.yaml"]
    if appset_name == EDGE:
        stack.append(f"$infra/argocd/values/edge-{flavour(cloud)}.yaml")
    stack.append(f"$infra/argocd/values/profile-{profile}.yaml")
    apps = "$infra/argocd/values/apps"
    return [
        *stack,
        f"{apps}/_common.yaml",
        f"{apps}/{service}/values.yaml",
        f"{apps}/{service}/{cloud}.yaml",
        f"{apps}/{service}/profile-{profile}.yaml",
        "$values/infra/common.yaml",
        f"$values/values/{service}-default-values.yaml",
    ]


def helm_sources(app: dict) -> tuple[dict, dict]:
    """The chart source and the dfe-extras source."""
    chart, extras = app["spec"]["sources"][:2]
    return chart, extras


def params(source: dict) -> dict[str, str]:
    return {p["name"]: p["value"] for p in source["helm"].get("parameters") or []}


def _repo(url: str) -> str:
    """A git repo URL as Argo compares them: case, a trailing slash and .git ignored."""
    return url.lower().rstrip("/").removesuffix(".git")


def ref_conflicts(app: dict) -> list[str]:
    """Each git repo the Application reads at more than one revision; empty is a pass.

    Argo reads one revision per repo URL, so a second source on the same repo at
    another revision is read from the first one's tree without a word.
    """
    revisions: dict[str, set[str]] = {}
    for source in app["spec"]["sources"]:
        url = source.get("repoURL", "")
        if url.startswith("oci://"):
            continue
        revisions.setdefault(_repo(url), set()).add(source.get("targetRevision", ""))
    return [f"{url} at {sorted(revs)}" for url, revs in sorted(revisions.items()) if len(revs) > 1]


def broken(appset_name: str, service: str, pin: str | None) -> str:
    """The pinned appset's text with one service's pin replaced, or dropped for None."""
    text = appset(appset_name).read_text(encoding="utf-8")
    entry = re.compile(r'\n\s*"' + re.escape(service) + r'"\s+"[^"]*"')
    found = entry.search(text)
    assert found is not None, f"{appset_name} pins no chart for {service}"
    replacement = "" if pin is None else f'\n                "{service}" "{pin}"'
    return text[: found.start()] + replacement + text[found.end() :]


def _written(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# ------------------------------------------------------------------- the shape


@pytest.mark.parametrize(("service", "appset_name"), CASES, ids=IDS)
def test_every_component_gets_four_sources_in_order(service: str, appset_name: str) -> None:
    app = application(service, appset_name)
    sources = app["spec"]["sources"]
    assert len(sources) == 4, sources
    chart, extras, infra, values = sources
    assert chart["repoURL"] == f"oci://registry.example.com/dfe/charts/{chart_name(service)}"
    assert chart["path"] == "."
    assert DIGEST_RE.fullmatch(chart["targetRevision"]), chart["targetRevision"]
    assert "ref" not in chart
    assert (extras["repoURL"], extras["path"]) == (w.INFRA_REPO_URL, EXTRAS_PATH)
    assert extras["targetRevision"] == INFRA_REVISION
    assert {k: infra[k] for k in ("repoURL", "targetRevision", "ref")} == {
        "repoURL": w.INFRA_REPO_URL,
        "targetRevision": INFRA_REVISION,
        "ref": "infra",
    }
    assert "path" not in infra
    assert values == {
        "repoURL": w.DEPLOY_REPO_URL,
        "targetRevision": DEPLOY_REVISION,
        "ref": "values",
    }


@pytest.mark.parametrize(("service", "appset_name"), CASES, ids=IDS)
@pytest.mark.parametrize(("profile", "cloud"), [("slim", "local"), ("mesh", "aws")])
def test_both_helm_sources_read_the_value_layers_in_order(
    service: str, appset_name: str, profile: str, cloud: str
) -> None:
    app = application(service, appset_name, profile=profile, cloud=cloud)
    want = layers(service, appset_name, profile, cloud)
    assert len(want) == (10 if appset_name == EDGE else 9)
    for source in helm_sources(app):
        assert source["helm"]["valueFiles"] == want
        assert source["helm"]["ignoreMissingValueFiles"] is True


@pytest.mark.parametrize(("service", "appset_name"), CASES, ids=IDS)
def test_no_helm_source_names_a_release(service: str, appset_name: str) -> None:
    """The objects keep the app.kubernetes.io/instance the 2.2.0 charts gave them."""
    app = application(service, appset_name)
    assert app["metadata"]["name"] == f"{service}-default-in-cluster"
    for source in helm_sources(app):
        assert "releaseName" not in source["helm"]


@pytest.mark.parametrize(("service", "appset_name"), CASES, ids=IDS)
@pytest.mark.parametrize("facts", [{}, MANAGED], ids=["in-cluster", "managed-broker"])
def test_dfe_extras_reads_what_the_chart_reads_plus_its_chart_name(
    service: str, appset_name: str, facts: dict[str, str]
) -> None:
    chart, extras = helm_sources(application(service, appset_name, facts=facts))
    assert params(extras) == {**params(chart), "chartName": chart_name(service)}
    assert params(extras)["profile"] == "scale"
    assert yaml.safe_load(extras["helm"]["values"]) == yaml.safe_load(chart["helm"]["values"])


@pytest.mark.parametrize(("service", "appset_name"), CASES, ids=IDS)
def test_the_cluster_secret_facts_reach_both_sources(service: str, appset_name: str) -> None:
    for source in helm_sources(application(service, appset_name, cloud="gcp")):
        found = params(source)
        assert found["cloud"] == "gcp"
        assert found["global.registry"] == "registry.example.com/dfe"
        assert "exposure.public.loadBalancerIP" not in found
        if appset_name == APPS:
            assert found["publicService.loadBalancerIP"] == RECEIVER_ADDRESS


@pytest.mark.parametrize(("service", "appset_name"), CASES, ids=IDS)
def test_no_repo_is_read_at_two_revisions(service: str, appset_name: str) -> None:
    assert ref_conflicts(application(service, appset_name)) == []


@pytest.mark.parametrize("service", COMPONENTS)
def test_each_chart_is_named_by_its_contract(service: str) -> None:
    contract = yaml.safe_load((CONTRACTS / f"{service}.json").read_text(encoding="utf-8"))
    name = APPS if service != "culvert" else EDGE
    chart, extras = helm_sources(application(service, name))
    assert chart["repoURL"].rsplit("/", 1)[-1] == contract["app_name"]
    assert params(extras)["chartName"] == contract["app_name"]


# ------------------------------------------------------------------ the pin maps


def test_the_pin_maps_carry_exactly_the_versions_yaml_digests() -> None:
    d = drift()
    versions = d.load_versions()
    pinned = {
        key.split(".", 1)[1]: value
        for key, value in versions.items()
        if key.startswith("chart-digests.")
    }
    assert set(pinned) == set(COMPONENTS)
    texts = {path: (REPO_ROOT / path).read_text(encoding="utf-8") for path in d.CHART_PIN_APPSETS}
    assert d.chart_pin_map(texts[Path("argocd/appsets") / APPS]) == pinned
    assert d.chart_pin_map(texts[Path("argocd/appsets") / EDGE]) == {"culvert": pinned["culvert"]}
    assert d.chart_pin_problems(versions, texts) == []


def test_the_committed_appsets_refuse_each_chart_not_yet_published() -> None:
    """A placeholder renders nothing, so no Application points at a chart that is not there."""
    for name in (APPS, EDGE):
        committed = REPO_ROOT / "argocd" / "appsets" / name
        pins = drift().chart_pin_map(committed.read_text(encoding="utf-8"))
        for service, pin in sorted(pins.items()):
            if DIGEST_RE.fullmatch(pin):
                continue
            with pytest.raises(w.WeaveError, match=f"the chart digest pinned for {service} is"):
                application(service, name, path=committed)


# ---------------------------------------------------------------- expected fails


@pytest.mark.parametrize(("service", "appset_name"), [("dfe-ui", APPS), ("culvert", EDGE)])
def test_a_service_with_no_digest_fails_the_render(
    tmp_path: Path, service: str, appset_name: str
) -> None:
    path = _written(tmp_path, appset_name, broken(appset_name, service, None))
    with pytest.raises(w.WeaveError, match=f"no chart digest is pinned for {service}"):
        application(service, appset_name, path=path)


@pytest.mark.parametrize("pin", ["unpublished", "sha256:abc", "1.2.3", "sha256:" + "AB" * 32])
@pytest.mark.parametrize(("service", "appset_name"), [("hyperdx", APPS), ("culvert", EDGE)])
def test_a_placeholder_digest_fails_the_render(
    tmp_path: Path, service: str, appset_name: str, pin: str
) -> None:
    path = _written(tmp_path, appset_name, broken(appset_name, service, pin))
    message = f'the chart digest pinned for {service} is "{pin}", not a published sha256 digest'
    with pytest.raises(w.WeaveError, match=re.escape(message)):
        application(service, appset_name, path=path)


def test_a_broken_entry_leaves_every_other_service_rendering(tmp_path: Path) -> None:
    path = _written(tmp_path, APPS, broken(APPS, "dfe-ui", None))
    chart, _ = helm_sources(application("dfe-loader", APPS, path=path))
    assert chart["targetRevision"] == RENDER_DIGEST


@pytest.mark.parametrize("appset_name", [APPS, EDGE])
@pytest.mark.parametrize(
    "deploy_url",
    [w.INFRA_REPO_URL, "HTTPS://git.example.com/dfe-infra", "https://git.example.com/dfe-infra/"],
    ids=["same-url", "case-and-no-suffix", "trailing-slash"],
)
def test_the_deploy_repo_on_the_chart_repo_at_another_revision_is_refused(
    appset_name: str, deploy_url: str
) -> None:
    service = "culvert" if appset_name == EDGE else "dfe-receiver"
    facts = {"dfe.hyperi.io/config_repo_url": deploy_url}
    with pytest.raises(w.WeaveError, match="Argo CD reads one revision per repo"):
        application(service, appset_name, facts=facts)


def test_the_deploy_repo_on_the_chart_repo_at_the_same_revision_renders() -> None:
    facts = {
        "dfe.hyperi.io/config_repo_url": w.INFRA_REPO_URL,
        "dfe.hyperi.io/config_repo_revision": INFRA_REVISION,
    }
    assert ref_conflicts(application("dfe-receiver", APPS, facts=facts)) == []


def test_one_repo_at_two_revisions_is_caught() -> None:
    app = {
        "spec": {
            "sources": [
                {"repoURL": "oci://registry.example.com/dfe/charts/dfe-ui", "targetRevision": "x"},
                {"repoURL": w.INFRA_REPO_URL, "targetRevision": "main", "path": EXTRAS_PATH},
                {"repoURL": w.INFRA_REPO_URL.upper(), "targetRevision": "other", "ref": "infra"},
            ]
        }
    }
    assert ref_conflicts(app) == ["https://git.example.com/dfe-infra at ['main', 'other']"]


@pytest.mark.parametrize(("service", "appset_name"), [("dfe-ui", APPS), ("culvert", EDGE)])
def test_a_cluster_with_no_registry_gets_no_chart_source(service: str, appset_name: str) -> None:
    with pytest.raises(w.WeaveError, match=re.escape("carries no dfe.hyperi.io/registry")):
        application(service, appset_name, facts={"dfe.hyperi.io/registry": ""})


# ---------------------------------------------------------------------- bootstrap


def test_bootstrap_registers_a_pull_credential_for_every_chart() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    listed = re.search(r'^DFE_THIN_CHARTS="([^"]*)"$', text, re.MULTILINE)
    assert listed is not None, "bootstrap.sh names no DFE_THIN_CHARTS"
    assert sorted(listed.group(1).split()) == sorted(chart_name(s) for s in COMPONENTS)
    loop = text[listed.end() : text.index("done", listed.end())]
    assert "--from-literal=type=oci" in loop
    assert '--from-literal=url="oci://${DFE_REGISTRY}/charts/${chart}"' in loop
    assert '--from-literal=password="${DFE_PULL_SECRET_TOKEN}"' in loop
    assert "argocd.argoproj.io/secret-type=repository" in loop
    assert text.index("DFE_THIN_CHARTS=") < text.index('==> [7/7]'), (
        "the credentials must exist before the ApplicationSets are applied"
    )


# ------------------------------------------------------- the 2.2.0 appsets kept


@pytest.mark.parametrize("name", [APPS, EDGE])
def test_the_kept_2_2_0_appsets_are_the_tagged_ones(name: str) -> None:
    """Render (a) reads these copies, so they must be what 2.2.0 deployed."""
    shown = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"2.2.0:argocd/appsets/{name}"],
        capture_output=True,
        check=False,
    )
    if shown.returncode != 0:
        pytest.skip("the 2.2.0 tag is not in this checkout")
    assert old_appset(name).read_bytes() == shown.stdout
