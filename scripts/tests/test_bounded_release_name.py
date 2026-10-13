#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_bounded_release_name.py
#  Purpose:      Prove a per-config Application named past Helm's 53-character
#                release cap renders under a bounded release name, and that an
#                Application name that fits stays its own release name.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The release name layer2-apps hands Helm, rendered through scripts/dfe-weave.

    python3 -m pytest scripts/tests/test_bounded_release_name.py -q

layer2-apps names each Application {service}-{instance}-{cluster}, which reaches 82
characters for dfe-transform-elastic with a 40-character instance. Argo CD names the
Helm release for the Application unless a helm source sets releaseName, and Helm
refuses a release name past 53. Both helm sources set releaseName for a name past 53
and leave it empty otherwise, which Argo drops, so an Application that rendered
before keeps its release name and every app.kubernetes.io/instance label with it.

- a name that fits sets no releaseName, and renders the same objects as the appset
  with no releaseName at all
- a name past the cap is its first 44 characters and 8 of its sha256, at most 53
- two names that differ only past the cut get different release names
- every per-config app with a 40-character instance renders, labelled with the
  release its Application names, with object names and label values in limits

The renders need helm and the scalo-service library (_weave.library).
"""

import re
from pathlib import Path

import pytest

from _gate import PROFILES
from _weave import REPO_ROOT, appset, helm, render_app, weave

HELM_RELEASE_MAX = 53
# Helm's release-name pattern, a DNS-1123 label.
RELEASE_RE = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
LABEL_VALUE_MAX = 63
# A Service name is an RFC 1035 label, and a CronJob's leaves room for its Jobs'
# suffix; every other kind rendered here takes a DNS-1123 subdomain.
NAME_MAX = {"Service": 63, "CronJob": 52}
SUBDOMAIN_MAX = 253
INSTANCE_LABEL = "app.kubernetes.io/instance"

# bootstrap names the cluster secret dfe-<cloud>-<env>; this is the local one.
CLUSTER = "dfe-local-dfe-local"
ELASTIC = "dfe-transform-elastic"
# The apps the engine deploys once per source, each under the instance's own name.
PER_CONFIG = ("dfe-fetcher", "dfe-transform-vrl", "dfe-transform-vector", ELASTIC)

# The longest instance name the engine accepts: a 40-character DNS label.
LONG = "customer-alpha-primary-ingest-0123456789"
# LONG's Application name with its first 44 characters and 8 of its sha256.
LONG_RELEASE = "dfe-transform-elastic-customer-alpha-primary-6502dba7"
# The same as LONG up to the 44-character cut, and different after it.
LONG_TWIN = "customer-alpha-primary-ingest-9876543210"
# Puts a hyphen at the 44th character of the elastic Application name.
HYPHEN_AT_CUT = "customer-alpha-primar-ingest-0123456789x"


def application_name(service: str, instance: str, cluster: str = CLUSTER) -> str:
    """The Application name layer2-apps generates."""
    return f"{service}-{instance}-{cluster}"


def generated(
    service: str, instance: str, cluster: str = CLUSTER, source: Path | None = None
) -> dict:
    """The Application the appset generates, every field rendered by helm's template engine."""
    w = weave()
    target = w.Target(service, "slim", "local", instance=instance, cluster=cluster)
    return w.application(REPO_ROOT, source or appset(), target, None, helm())


def release_names(app: dict) -> list[str]:
    """Each helm source's releaseName, empty where it sets none."""
    return [s["helm"].get("releaseName", "") for s in app["spec"]["sources"] if "helm" in s]


def identity(service: str, instance: str) -> dict:
    """The object names the engine writes into a per-config instance's values file."""
    if service not in PER_CONFIG:
        return {}
    name = f"{service}-{instance}"
    return {"fullnameOverride": name, "component": name.removeprefix("dfe-")}


def rendered(
    service: str,
    profile: str,
    instance: str,
    cluster: str = CLUSTER,
    source: Path | None = None,
) -> list[dict]:
    """One app's objects as its Application deploys them, with the engine's identity overlay."""
    options: dict = {"instance": instance, "cluster": cluster}
    if source is not None:
        options["appset"] = source
    return render_app(service, profile, "local", overlay=identity(service, instance), **options)


def instance_labels(docs: list[dict]) -> set[str]:
    """Every app.kubernetes.io/instance value the objects carry."""
    found = set()
    for doc in docs:
        labels = (doc.get("metadata") or {}).get("labels") or {}
        if INSTANCE_LABEL in labels:
            found.add(labels[INSTANCE_LABEL])
    return found


def _label_maps(doc: dict) -> list[dict]:
    spec = doc.get("spec") or {}
    template = (spec.get("template") or {}).get("metadata") or {}
    selector = (spec.get("selector") or {}).get("matchLabels") or {}
    return [(doc.get("metadata") or {}).get("labels") or {}, template.get("labels") or {}, selector]


def over_limit(docs: list[dict]) -> list[str]:
    """Each object name or label value past its Kubernetes limit; empty is a pass."""
    found = []
    for doc in docs:
        kind = doc.get("kind")
        name = str((doc.get("metadata") or {}).get("name"))
        cap = NAME_MAX.get(kind, SUBDOMAIN_MAX)
        if len(name) > cap:
            found.append(f"{kind}/{name}: {len(name)} characters, over {cap}")
        for labels in _label_maps(doc):
            for key, value in labels.items():
                if len(str(value)) > LABEL_VALUE_MAX:
                    found.append(f"{kind}/{name} label {key}: {len(str(value))} characters")
    return found


@pytest.fixture(scope="module")
def unbounded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """layer2-apps with every releaseName line dropped: the appset before the cap was handled."""
    lines = appset().read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.lstrip().startswith("releaseName:")]
    assert len(lines) - len(kept) == 2, "expected one releaseName per helm source"
    path = tmp_path_factory.mktemp("unbounded") / "layer2-apps.yaml"
    path.write_text("".join(kept), encoding="utf-8", newline="\n")
    return path


# ------------------------------------------------------------ a name that fits


@pytest.mark.parametrize(
    ("service", "instance", "cluster"),
    [
        ("dfe-receiver", "default", "in-cluster"),
        (ELASTIC, "default", CLUSTER),
        (ELASTIC, "e" * 11, CLUSTER),
    ],
    ids=["receiver", "elastic-default", "elastic-at-53"],
)
def test_a_name_that_fits_keeps_it_as_its_release(
    service: str, instance: str, cluster: str, unbounded: Path
) -> None:
    """Argo drops an empty releaseName, so the objects are the ones rendered before."""
    app = generated(service, instance, cluster)
    name = app["metadata"]["name"]
    assert len(name) <= HELM_RELEASE_MAX
    assert release_names(app) == ["", ""]
    docs = rendered(service, "slim", instance, cluster)
    assert docs == rendered(service, "slim", instance, cluster, source=unbounded)
    assert instance_labels(docs) == {name}


def test_one_character_past_53_is_bounded() -> None:
    app = generated(ELASTIC, "e" * 12)
    assert len(app["metadata"]["name"]) == HELM_RELEASE_MAX + 1
    assert all(name and len(name) <= HELM_RELEASE_MAX for name in release_names(app))


# ------------------------------------------------------------ a name past the cap


def test_a_long_name_takes_its_first_44_characters_and_8_of_its_hash() -> None:
    """The Application keeps its 82-character name, and both sources install one release."""
    app = generated(ELASTIC, LONG)
    assert app["metadata"]["name"] == application_name(ELASTIC, LONG)
    assert len(app["metadata"]["name"]) == 82
    assert release_names(app) == [LONG_RELEASE, LONG_RELEASE]
    assert len(LONG_RELEASE) == HELM_RELEASE_MAX
    assert RELEASE_RE.fullmatch(LONG_RELEASE)


def test_the_release_name_is_the_same_on_every_render() -> None:
    assert release_names(generated(ELASTIC, LONG)) == release_names(generated(ELASTIC, LONG))


def test_a_cut_landing_on_a_hyphen_drops_it() -> None:
    name = application_name(ELASTIC, HYPHEN_AT_CUT)
    assert name[43] == "-"
    release = release_names(generated(ELASTIC, HYPHEN_AT_CUT))[0]
    assert release.startswith(name[:43] + "-")
    assert "--" not in release
    assert len(release) == HELM_RELEASE_MAX - 1
    assert RELEASE_RE.fullmatch(release)


def test_two_names_that_differ_past_the_cut_get_different_releases() -> None:
    first = release_names(generated(ELASTIC, LONG))[0]
    second = release_names(generated(ELASTIC, LONG_TWIN))[0]
    assert application_name(ELASTIC, LONG)[:44] == application_name(ELASTIC, LONG_TWIN)[:44]
    assert first[:44] == second[:44]
    assert first != second


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("service", PER_CONFIG)
def test_a_40_character_instance_renders_within_kubernetes_limits(
    service: str, profile: str
) -> None:
    """The objects carry the release the Application names, and fit every name limit."""
    release = release_names(generated(service, LONG))[0]
    docs = rendered(service, profile, LONG)
    assert docs != []
    assert instance_labels(docs) == {release}
    assert over_limit(docs) == []


# ---------------------------------------------------------------- expected fails


def test_helm_refuses_the_same_name_with_no_release_name(unbounded: Path) -> None:
    """The appset with no releaseName hands Helm the 82-character Application name."""
    with pytest.raises(weave().WeaveError, match="53"):
        rendered(ELASTIC, "slim", LONG, source=unbounded)


def test_an_over_long_service_name_is_seen() -> None:
    docs = [{"kind": "Service", "metadata": {"name": "s" * 64}}]
    assert over_limit(docs) == [f"Service/{'s' * 64}: 64 characters, over 63"]


def test_an_over_long_label_value_is_seen() -> None:
    docs = [{"kind": "ConfigMap", "metadata": {"name": "c", "labels": {INSTANCE_LABEL: "i" * 64}}}]
    assert over_limit(docs) == [f"ConfigMap/c label {INSTANCE_LABEL}: 64 characters"]
