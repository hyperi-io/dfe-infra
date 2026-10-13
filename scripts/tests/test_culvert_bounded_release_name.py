#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_culvert_bounded_release_name.py
#  Purpose:      Prove culvert's Application from the edge module renders under a
#                bounded release name when its name passes Helm's 53-character
#                cap, and that an Application name that fits stays its own release.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The release name layer2-edge hands Helm for culvert, rendered through scripts/dfe-weave.

    python3 -m pytest scripts/tests/test_culvert_bounded_release_name.py -q

The tunnel's ApplicationSet names each Application {service}-{instance}-{cluster}, the
name layer2-apps gives it, and the engine accepts a 40-character instance for it.
Argo CD names the Helm release for the Application unless a helm source sets
releaseName, and Helm refuses a release name past 53. Both helm sources set
releaseName for a name past 53 with the expression layer2-apps uses, and leave it
empty otherwise, which Argo drops.

- a name that fits sets no releaseName, and renders the same objects as the appset
  with no releaseName at all
- a name past the cap is its first 44 characters and 8 of its sha256, at most 53
- two names that differ only past the cut get different release names
- layer2-apps, which still generates culvert for a cluster the edge module does not
  reach, hands Helm the same release name for the same Application
- a 40-character instance renders, labelled with the release its Application names,
  with object names and label values in limits

The renders need helm and the scalo-service library (_weave.library).
"""

from pathlib import Path

import pytest
from test_bounded_release_name import (
    CLUSTER,
    HELM_RELEASE_MAX,
    RELEASE_RE,
    application_name,
    generated,
    instance_labels,
    over_limit,
    release_names,
)

from _gate import APPSETS, CLOUDS, INSTANCE, PROFILES
from _weave import appset, render_app, weave

CULVERT = "culvert"

# The longest instance name the engine accepts: a 40-character DNS label.
LONG = "customer-alpha-primary-ingest-0123456789"
# LONG's Application name is 68 characters: its first 44 and 8 of its sha256.
LONG_RELEASE = "culvert-customer-alpha-primary-ingest-012345-4cfe9ba0"
# The same as LONG up to the 44-character cut, and different after it.
LONG_TWIN = "customer-alpha-primary-ingest-0123459876"
# Puts a hyphen at the 44th character of the culvert Application name.
HYPHEN_AT_CUT = "customer-alpha-primary-ingest-01234-6789"


def edge_app(instance: str, cluster: str = CLUSTER) -> dict:
    """The culvert Application layer2-edge generates, every field rendered by helm's engine."""
    return generated(CULVERT, instance, cluster, source=appset(APPSETS[CULVERT]))


def rendered(
    profile: str, cloud: str, instance: str, cluster: str = CLUSTER, source: Path | None = None
) -> list[dict]:
    """culvert's objects as the edge Application deploys them, with the instance values it needs."""
    options: dict = {
        "instance": instance,
        "cluster": cluster,
        "appset": source or appset(APPSETS[CULVERT]),
    }
    return render_app(CULVERT, profile, cloud, overlay=INSTANCE[CULVERT][cloud], **options)


@pytest.fixture(scope="module")
def unbounded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """layer2-edge with every releaseName line dropped: the appset before the cap was handled."""
    source = appset(APPSETS[CULVERT])
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.lstrip().startswith("releaseName:")]
    assert len(lines) - len(kept) == 2, "expected one releaseName per culvert helm source"
    path = tmp_path_factory.mktemp("unbounded") / source.name
    path.write_text("".join(kept), encoding="utf-8", newline="\n")
    return path


# ------------------------------------------------------------ a name that fits


@pytest.mark.parametrize(
    ("instance", "cluster"),
    [("default", "in-cluster"), ("default", CLUSTER), ("e" * 25, CLUSTER)],
    ids=["default", "default-local", "at-53"],
)
def test_a_name_that_fits_keeps_it_as_its_release(
    instance: str, cluster: str, unbounded: Path
) -> None:
    """Argo drops an empty releaseName, so the objects are the ones rendered before."""
    app = edge_app(instance, cluster)
    name = app["metadata"]["name"]
    assert name == application_name(CULVERT, instance, cluster)
    assert len(name) <= HELM_RELEASE_MAX
    assert release_names(app) == ["", ""]
    docs = rendered("slim", "local", instance, cluster)
    assert docs == rendered("slim", "local", instance, cluster, source=unbounded)
    assert instance_labels(docs) == {name}


def test_the_cap_falls_between_53_and_54_characters() -> None:
    at_cap, past_cap = edge_app("e" * 25), edge_app("e" * 26)
    assert len(at_cap["metadata"]["name"]) == HELM_RELEASE_MAX
    assert release_names(at_cap) == ["", ""]
    assert len(past_cap["metadata"]["name"]) == HELM_RELEASE_MAX + 1
    assert all(name and len(name) <= HELM_RELEASE_MAX for name in release_names(past_cap))


# ------------------------------------------------------------ a name past the cap


def test_a_long_name_takes_its_first_44_characters_and_8_of_its_hash() -> None:
    """The Application keeps its 68-character name, and both sources install one release."""
    app = edge_app(LONG)
    assert app["metadata"]["name"] == application_name(CULVERT, LONG)
    assert len(app["metadata"]["name"]) == 68
    assert release_names(app) == [LONG_RELEASE, LONG_RELEASE]
    assert len(LONG_RELEASE) == HELM_RELEASE_MAX
    assert RELEASE_RE.fullmatch(LONG_RELEASE)


def test_the_release_name_is_the_same_on_every_render() -> None:
    assert release_names(edge_app(LONG)) == release_names(edge_app(LONG))


def test_a_cut_landing_on_a_hyphen_drops_it() -> None:
    name = application_name(CULVERT, HYPHEN_AT_CUT)
    assert name[43] == "-"
    release = release_names(edge_app(HYPHEN_AT_CUT))[0]
    assert release.startswith(name[:43] + "-")
    assert "--" not in release
    assert len(release) == HELM_RELEASE_MAX - 1
    assert RELEASE_RE.fullmatch(release)


def test_two_names_that_differ_past_the_cut_get_different_releases() -> None:
    first = release_names(edge_app(LONG))[0]
    second = release_names(edge_app(LONG_TWIN))[0]
    assert application_name(CULVERT, LONG)[:44] == application_name(CULVERT, LONG_TWIN)[:44]
    assert first[:44] == second[:44]
    assert first != second


@pytest.mark.parametrize("instance", [LONG, HYPHEN_AT_CUT, "e" * 26, "default"])
def test_the_bridge_and_the_edge_module_hand_helm_the_same_release(instance: str) -> None:
    """A cluster moving between them keeps the release, and its objects their instance label."""
    bridge = generated(CULVERT, instance)
    edge = edge_app(instance)
    assert bridge["metadata"]["name"] == edge["metadata"]["name"]
    assert release_names(bridge) == release_names(edge)


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("cloud", CLOUDS)
def test_a_40_character_instance_renders_within_kubernetes_limits(cloud: str, profile: str) -> None:
    """The objects carry the release the Application names, and fit every name limit."""
    release = release_names(edge_app(LONG))[0]
    docs = rendered(profile, cloud, LONG)
    assert docs != []
    assert instance_labels(docs) == {release}
    assert over_limit(docs) == []


# ---------------------------------------------------------------- expected fails


def test_helm_refuses_the_same_name_with_no_release_name(unbounded: Path) -> None:
    """The appset with no releaseName hands Helm the 68-character Application name."""
    with pytest.raises(weave().WeaveError, match="53"):
        rendered("slim", "local", LONG, source=unbounded)
