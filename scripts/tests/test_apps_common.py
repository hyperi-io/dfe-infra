#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_apps_common.py
#  Purpose:      Prove argocd/values/apps/_common.yaml gives every app the OTLP
#                endpoint dfe-common.otelEndpoint resolves, its DFE labels and
#                reload annotations, and that each values file's nodeSelector and
#                tolerations equal its nodeScheduling.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the fleet-wide integration values the scalo-service thin charts take.

    python3 -m pytest scripts/tests/test_apps_common.py -q
    DFE_WEAVE_HELM=<helm> DFE_WEAVE_LIBRARY=<scalo-service dir> \\
        python3 -m pytest scripts/tests/test_apps_common.py -q

Three tiers. The placement check reads the values files and needs nothing else. The
endpoint checks need helm: a stand-in chart renders extraEnv through tpl as the
library does, over the value files exactly as scripts/dfe-weave layers them, and the
result is compared with what dfe-common.otelEndpoint renders from the same settings.
The thin-chart checks render the real chart and need the scalo-service chart in
DFE_WEAVE_LIBRARY, and skip without it.

Telemetry settings and `cloud` reach each render through a deploy repo's infra/common.yaml,
the layer after the app files. An appset parameter would beat that file, so the label test
passes the same cloud to the Target.
"""

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from _weave import CONTRACTS, REPO_ROOT, helm, library, weave

w = weave()

VALUES = REPO_ROOT / "argocd" / "values"
COMMON = VALUES / "common.yaml"
TELEMETRY_HELPER = REPO_ROOT / "helm" / "library" / "dfe-common" / "templates" / "_telemetry.tpl"
# The apps the manifest declares: deploy.service is the key the appset layers by.
SERVICES = tuple(yaml.safe_load((REPO_ROOT / "apps.yaml").read_text(encoding="utf-8"))["apps"])
CLOUDS = ("aws", "azure", "gcp", "local", "local-dfe")
VALUE_FILES = sorted(VALUES.glob("*.yaml"))
DIGEST = "sha256:" + "ab" * 32
ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
PROTOCOL = "OTEL_EXPORTER_OTLP_PROTOCOL"

# What each telemetry.mode needs beyond common.yaml, as a deployment's infra/common.yaml sets it.
MODES = {
    "hyperdx": {},
    "receiver": {},
    "external": {"externalEndpoint": "otlp.example.net:4317"},
    "prometheus": {},
}
TLS = pytest.mark.parametrize("tls", [False, True], ids=["tls-off", "tls-on"])
MODE = pytest.mark.parametrize("mode", MODES)
SERVICE = pytest.mark.parametrize("service", SERVICES)

# Endpoint shapes beyond the four modes: an override of the derived host, a collector in
# another namespace, a scheme the deployer wrote, and a mode left without its endpoint.
SHAPES = {
    "collector-namespace": ("hyperdx", {"collectorNamespace": "monitoring"}),
    "hyperdx-endpoint": ("hyperdx", {"hyperdxEndpoint": "collector.example.net:4317"}),
    "receiver-with-scheme": ("receiver", {"receiverEndpoint": "https://rx.example.net:8443"}),
    "external-with-scheme": ("external", {"externalEndpoint": "https://otlp.example.net:4318"}),
    "external-unset": ("external", {"externalEndpoint": ""}),
}


def _overlay(
    mode: str,
    tls: bool,
    telemetry: dict | None = None,
    endpoint: str | None = None,
    **top: object,
) -> dict:
    """A deploy repo's infra/common.yaml choosing a telemetry mode, otel.tls and otel.endpoint."""
    otel = {"tls": tls, **({"endpoint": endpoint} if endpoint else {})}
    return {
        "telemetry": {"mode": mode, **MODES.get(mode, {}), **(telemetry or {})},
        "otel": otel,
        **top,
    }


def _key(overlay: dict) -> str:
    return json.dumps(overlay, sort_keys=True)


# ------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def deploy_for(tmp_path_factory: pytest.TempPathFactory) -> Callable[[dict], Path]:
    """A deploy repo directory holding the overlay as infra/common.yaml, made once per overlay."""
    root = tmp_path_factory.mktemp("deploy")
    made: dict[str, Path] = {}

    def make(overlay: dict) -> Path:
        key = _key(overlay)
        if key not in made:
            repo = root / f"d{len(made)}"
            (repo / "infra").mkdir(parents=True)
            (repo / "infra" / "common.yaml").write_text(yaml.safe_dump(overlay), encoding="utf-8")
            made[key] = repo
        return made[key]

    return make


@pytest.fixture(scope="module")
def expected(tmp_path_factory: pytest.TempPathFactory) -> Callable[[dict], str]:
    """What dfe-common.otelEndpoint renders for an overlay, from the helper's own source file."""
    root = tmp_path_factory.mktemp("oracle")
    chart = root / "oracle"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: oracle\nversion: 0.0.0\ntype: application\n", encoding="utf-8"
    )
    shutil.copy(TELEMETRY_HELPER, chart / "templates" / "_telemetry.tpl")
    (chart / "templates" / "endpoint.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: endpoint\n"
        'data:\n  endpoint: {{ include "dfe-common.otelEndpoint" . | quote }}\n',
        encoding="utf-8",
    )
    common = yaml.safe_load(COMMON.read_text(encoding="utf-8"))
    seen: dict[str, str] = {}

    def resolve(overlay: dict) -> str:
        key = _key(overlay)
        if key not in seen:
            values = {
                "project": common["global"]["project"],
                "telemetry": {**common["telemetry"], **overlay.get("telemetry", {})},
                "otel": {**common["otel"], **overlay.get("otel", {})},
            }
            file = root / f"values{len(seen)}.yaml"
            file.write_text(yaml.safe_dump(values), encoding="utf-8")
            out = w.run(
                [w.find_helm(helm()), "template", "oracle", str(chart), "--values", str(file)],
                "dfe-common.otelEndpoint",
            )
            seen[key] = next(d for d in w.documents(out) if d["kind"] == "ConfigMap")["data"][
                "endpoint"
            ]
        return seen[key]

    return resolve


@pytest.fixture(scope="module")
def stand_in(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A chart that renders each extraEnv entry through tpl on the root context, as the library does."""
    chart = tmp_path_factory.mktemp("stand-in") / "stand-in"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: stand-in\nversion: 0.0.0\ntype: application\n", encoding="utf-8"
    )
    (chart / "templates" / "env.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: env\ndata:\n"
        "{{- range $name, $value := .Values.extraEnv }}\n"
        "  {{ $name }}: {{ tpl (toString $value) $ | quote }}\n"
        "{{- end }}\n",
        encoding="utf-8",
    )
    return chart


def _contract_for(service: str) -> bytes:
    """The app's committed contract, else dfe-ui's under the app's own name.

    The integration values do not depend on the contract, so an app without a
    committed one is rendered through a stand-in for it.
    """
    committed = CONTRACTS / f"{service}.json"
    if committed.is_file():
        return committed.read_bytes()
    body = json.loads((CONTRACTS / "dfe-ui.json").read_text(encoding="utf-8"))
    body["app_name"] = service
    return json.dumps(body, indent=2).encode()


@pytest.fixture(scope="module")
def thin_charts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The thin chart of every app, assembled once from the scalo-service chart."""
    lib = library()
    root = tmp_path_factory.mktemp("thin")
    charts = {}
    for service in SERVICES:
        charts[service] = w.assemble(
            _contract_for(service),
            lib,
            root / service,
            version="1.0.0",
            tag="v1.0.0",
            digest=DIGEST,
        )
        w.build_dependency(helm(), charts[service])
    return charts


def _render(chart: Path, deploy: Path, service: str, cloud: str = "local") -> list[dict]:
    target = w.Target(service, "slim", cloud)
    return w.render_app(target, "new", w.Inputs(chart=chart, deploy_repo=deploy, helm=helm())).docs


def _stand_in_env(chart: Path, deploy: Path, service: str) -> dict[str, str]:
    docs = _render(chart, deploy, service)
    return next(d for d in docs if d["kind"] == "ConfigMap")["data"]


def _deployment(docs: list[dict]) -> dict:
    return next(d for d in docs if d["kind"] == "Deployment")


def _container_env(docs: list[dict]) -> list[dict]:
    return _deployment(docs)["spec"]["template"]["spec"]["containers"][0]["env"]


def _env_values(docs: list[dict], name: str) -> list[str]:
    return [e["value"] for e in _container_env(docs) if e["name"] == name]


# ---------------------------------------------- the OTLP endpoint, through a stand-in


def test_dfe_common_resolves_each_mode_to_its_known_endpoint(
    expected: Callable[[dict], str],
) -> None:
    host = "dfe-otel-collector-gateway.otel.svc.cluster.local:4317"
    assert {
        (mode, tls): expected(_overlay(mode, tls)) for mode in MODES for tls in (False, True)
    } == {
        ("hyperdx", False): f"http://{host}",
        ("hyperdx", True): f"https://{host}",
        ("receiver", False): "http://dfe-receiver.dfe.svc.cluster.local:8443",
        ("receiver", True): "https://dfe-receiver.dfe.svc.cluster.local:8443",
        ("external", False): "http://otlp.example.net:4317",
        ("external", True): "https://otlp.example.net:4317",
        ("prometheus", False): "",
        ("prometheus", True): "",
    }


@SERVICE
@MODE
@TLS
def test_every_app_resolves_the_endpoint_dfe_common_does(
    service: str,
    mode: str,
    tls: bool,
    stand_in: Path,
    deploy_for: Callable[[dict], Path],
    expected: Callable[[dict], str],
) -> None:
    overlay = _overlay(mode, tls)
    env = _stand_in_env(stand_in, deploy_for(overlay), service)
    assert env[ENDPOINT] == expected(overlay)


@pytest.mark.parametrize(("mode", "wrong"), [(m, o) for m in MODES for o in MODES if m != o])
def test_a_wrong_mode_yields_a_different_endpoint(
    mode: str,
    wrong: str,
    stand_in: Path,
    deploy_for: Callable[[dict], Path],
    expected: Callable[[dict], str],
) -> None:
    env = _stand_in_env(stand_in, deploy_for(_overlay(mode, False)), "dfe-receiver")
    assert env[ENDPOINT] == expected(_overlay(mode, False))
    assert env[ENDPOINT] != expected(_overlay(wrong, False))


@pytest.mark.parametrize("shape", SHAPES)
@TLS
def test_an_endpoint_of_any_shape_resolves_as_dfe_common_does(
    shape: str,
    tls: bool,
    stand_in: Path,
    deploy_for: Callable[[dict], Path],
    expected: Callable[[dict], str],
) -> None:
    mode, telemetry = SHAPES[shape]
    overlay = _overlay(mode, tls, telemetry)
    env = _stand_in_env(stand_in, deploy_for(overlay), "dfe-receiver")
    assert env[ENDPOINT] == expected(overlay)


@TLS
def test_an_endpoint_that_carries_a_scheme_is_left_as_written(
    tls: bool, expected: Callable[[dict], str]
) -> None:
    mode, telemetry = SHAPES["external-with-scheme"]
    assert expected(_overlay(mode, tls, telemetry)) == "https://otlp.example.net:4318"


# ------------------------------------------------ the OTLP endpoint, in the thin chart


@SERVICE
@MODE
@TLS
def test_the_thin_chart_exports_the_endpoint_dfe_common_resolves(
    service: str,
    mode: str,
    tls: bool,
    thin_charts: dict[str, Path],
    deploy_for: Callable[[dict], Path],
    expected: Callable[[dict], str],
) -> None:
    overlay = _overlay(mode, tls)
    docs = _render(thin_charts[service], deploy_for(overlay), service)
    assert _env_values(docs, ENDPOINT) == [expected(overlay)]
    # The app's own default protocol applies: only an otel.endpoint override sets one.
    assert _env_values(docs, PROTOCOL) == []


@MODE
def test_an_otel_endpoint_override_wins_over_every_mode(
    mode: str,
    thin_charts: dict[str, Path],
    deploy_for: Callable[[dict], Path],
    expected: Callable[[dict], str],
) -> None:
    override = "https://collector.example.net:4317"
    overlay = _overlay(mode, False, endpoint=override)
    docs = _render(thin_charts["dfe-receiver"], deploy_for(overlay), "dfe-receiver")
    assert expected(overlay) == override
    assert _env_values(docs, ENDPOINT) == [override]
    assert _env_values(docs, PROTOCOL) == ["grpc"]


def test_an_override_without_a_scheme_is_exported_as_written(
    thin_charts: dict[str, Path],
    deploy_for: Callable[[dict], Path],
    expected: Callable[[dict], str],
) -> None:
    # The 2.2.0 helper adds http:// here, the library does not: common.yaml says so.
    overlay = _overlay("hyperdx", False, endpoint="collector.example.net:4317")
    docs = _render(thin_charts["dfe-receiver"], deploy_for(overlay), "dfe-receiver")
    assert _env_values(docs, ENDPOINT) == ["collector.example.net:4317"]
    assert expected(overlay) == "http://collector.example.net:4317"


# ------------------------------------------------------------- labels and reload


@SERVICE
def test_every_app_is_labelled_part_of_dfe_with_its_env_and_cloud(
    service: str, thin_charts: dict[str, Path], deploy_for: Callable[[dict], Path]
) -> None:
    # cloud is passed by the deploy repo and by the Target alike, so the label reads
    # the same whether the appset hands it over as a parameter or not.
    overlay = _overlay("hyperdx", False, cloud="aws")
    docs = _render(thin_charts[service], deploy_for(overlay), service, "aws")
    deployment = _deployment(docs)
    wanted = {
        "app.kubernetes.io/part-of": "dfe",
        "dfe.hyperi.io/env": "dev",
        "dfe.hyperi.io/cloud": "aws",
    }
    pod_labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert wanted.items() <= deployment["metadata"]["labels"].items()
    assert wanted.items() <= pod_labels.items()
    assert deployment["metadata"]["annotations"]["reloader.stakater.com/auto"] == "true"


# ----------------------------------------------------------------------- placement


def _placement(data: dict) -> tuple[dict, list]:
    scheduling = data.get("nodeScheduling") or {}
    return scheduling.get("nodeSelector") or {}, scheduling.get("tolerations") or []


def _copy_matches(data: dict) -> bool:
    """Whether a file's top-level nodeSelector and tolerations equal its nodeScheduling."""
    return (data.get("nodeSelector") or {}, data.get("tolerations") or []) == _placement(data)


@pytest.mark.parametrize("path", VALUE_FILES, ids=lambda p: p.name)
def test_each_values_file_places_pods_under_both_keys_alike(path: Path) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert _copy_matches(data), (path.name, _placement(data))


def test_a_copy_that_differs_from_node_scheduling_is_caught() -> None:
    scheduling = {"nodeSelector": {"dfe.hyperi.io/workload": "dfe"}, "tolerations": []}
    assert _copy_matches({"nodeScheduling": scheduling, **scheduling})
    assert not _copy_matches({"nodeScheduling": scheduling})
    assert not _copy_matches({"nodeScheduling": scheduling, "nodeSelector": {"other": "dfe"}})
    assert not _copy_matches({"nodeSelector": {"dfe.hyperi.io/workload": "dfe"}})


def test_the_local_cloud_pins_pods_to_the_dfe_nodes() -> None:
    data = yaml.safe_load((VALUES / "local.yaml").read_text(encoding="utf-8"))
    assert data["nodeSelector"] == {"dfe.hyperi.io/workload": "dfe"}
    assert [t["key"] for t in data["tolerations"]] == ["dfe.hyperi.io/workload"]


@pytest.mark.parametrize("cloud", CLOUDS)
def test_pods_take_the_placement_nodescheduling_gives_their_cloud(
    cloud: str, thin_charts: dict[str, Path], deploy_for: Callable[[dict], Path]
) -> None:
    data = yaml.safe_load((VALUES / f"{cloud}.yaml").read_text(encoding="utf-8")) or {}
    selector, tolerations = _placement(data)
    docs = _render(
        thin_charts["dfe-receiver"], deploy_for(_overlay("hyperdx", False)), "dfe-receiver", cloud
    )
    pod = _deployment(docs)["spec"]["template"]["spec"]
    assert (pod.get("nodeSelector") or {}, pod.get("tolerations") or []) == (selector, tolerations)
