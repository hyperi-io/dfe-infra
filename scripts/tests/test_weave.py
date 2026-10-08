#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_weave.py
#  Purpose:      Prove scripts/dfe-weave assembles a thin chart reproducibly,
#                layers values exactly as layer2-apps.yaml does, and fails the
#                gate on a changed selector, a lost object or a renamed claim.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe-weave, the render-and-diff tool the chart switch is gated on.

    python3 -m pytest scripts/tests/test_weave.py -q
    DFE_WEAVE_LIBRARY=<scalo-service dir> python3 -m pytest scripts/tests/test_weave.py -q

Three tiers. Assembly and the diff facets need nothing but python, against a
library and renders built here. The value layering needs helm, and renders the
real appset and the real 2.2.0 charts, with a stand-in thin chart that dumps the
values it was handed. The dfe-ui and dfe-hyperdx renders need the scalo-service
library (_weave.library), with their integration values from argocd/values/apps.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from _weave import CONTRACTS, REPO_ROOT, diff_app, helm, library, weave

w = weave()
DIGEST = "sha256:" + "ab" * 32


# ---------------------------------------------------------------- a small library


def _library(root: Path) -> Path:
    """A scalo-service-shaped library: skeleton, contract schema, lint skips."""
    lib = root / "scalo-service"
    (lib / "schema").mkdir(parents=True)
    (lib / "skeleton" / "templates").mkdir(parents=True)
    (lib / "Chart.yaml").write_text(
        "apiVersion: v2\nname: scalo-service\ntype: library\nversion: 9.9.9\n", encoding="utf-8"
    )
    (lib / "schema" / "deployment-contract.v4.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["schema_version", "app_name"],
                "properties": {"schema_version": {"const": 4}, "app_name": {"type": "string"}},
            }
        ),
        encoding="utf-8",
    )
    (lib / "skeleton" / "Chart.yaml").write_text(
        "apiVersion: v2\nname: app-name\ntype: application\nversion: 0.0.0\nappVersion: '0.0.0'\n"
        "dependencies:\n  - name: scalo-service\n    version: 0.0.0\n"
        "    repository: oci://registry.example.com/charts\n",
        encoding="utf-8",
    )
    (lib / "skeleton" / "values.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"image": {"type": "object"}},
            }
        ),
        encoding="utf-8",
    )
    (lib / "skeleton" / "templates" / "deployment.yaml").write_text(
        '{{ include "scalo-service.deployment" . }}\n', encoding="utf-8"
    )
    (lib / "lint-skip.yaml").write_text(
        'checkov:\n  CKV_K8S_40: "The uid comes from the contract."\n', encoding="utf-8"
    )
    return lib


CONFIG_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "$defs": {
        "Batch": {
            "type": "object",
            "properties": {
                "size": {"type": "integer", "minimum": 1, "default": 500, "x-scalo-dial": "big"}
            },
        },
        "Level": {"type": "string", "enum": ["info", "debug"]},
    },
    "properties": {
        "batch": {"$ref": "#/$defs/Batch"},
        "log": {
            "properties": {
                "level": {"$ref": "#/$defs/Level", "default": "info", "x-scalo-dial": "small"}
            }
        },
        "plain": {"type": "string"},
    },
    "allOf": [
        {"properties": {"workers": {"type": "integer", "default": 4, "x-scalo-dial": "big"}}}
    ],
}


def _contract(**overrides: object) -> bytes:
    body = {
        "schema_version": 4,
        "app_name": "demo-app",
        "description": "A demo service",
        "config_schema": CONFIG_SCHEMA,
    }
    body.update(overrides)
    # Odd spacing on purpose: files/contract.json must keep these exact bytes.
    return json.dumps(body, indent=3).encode() + b"\n\n"


def _assemble(tmp_path: Path, out: str, raw: bytes | None = None, **pin: str) -> Path:
    lib = tmp_path / "scalo-service"
    if not lib.exists():
        _library(tmp_path)
    return w.assemble(
        raw if raw is not None else _contract(),
        lib,
        tmp_path / out,
        version=pin.get("version", "1.2.3"),
        tag=pin.get("tag", "v1.2.3"),
        digest=pin.get("digest", DIGEST),
    )


def _files(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ------------------------------------------------------------------------ assemble


def test_assemble_writes_the_same_bytes_twice(tmp_path: Path) -> None:
    first = _files(_assemble(tmp_path, "a"))
    second = _files(_assemble(tmp_path, "b"))
    assert first == second
    assert sorted(first) == [
        ".helmignore",
        ".hyperi-ci.yaml",
        "Chart.yaml",
        "files/contract.json",
        "templates/deployment.yaml",
        "values.schema.json",
        "values.yaml",
    ]


def test_assemble_keeps_the_contract_bytes(tmp_path: Path) -> None:
    raw = _contract()
    chart = _assemble(tmp_path, "out", raw)
    assert (chart / "files" / "contract.json").read_bytes() == raw


def test_assemble_fills_in_chart_yaml(tmp_path: Path) -> None:
    chart = _assemble(tmp_path, "out", tag="v4.5.6", version="4.5.6")
    meta = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8"))
    assert chart.name == "demo-app"
    assert (meta["name"], meta["version"], meta["appVersion"]) == ("demo-app", "4.5.6", "v4.5.6")
    assert meta["description"] == "A demo service"
    library_dir = (tmp_path / "scalo-service").resolve()
    assert meta["dependencies"] == [
        {"name": "scalo-service", "repository": f"file://{library_dir}", "version": "9.9.9"}
    ]


def test_assemble_nests_each_dial_under_config_with_its_refs_inlined(tmp_path: Path) -> None:
    schema = json.loads(
        (_assemble(tmp_path, "out") / "values.schema.json").read_text(encoding="utf-8")
    )
    config = schema["properties"]["config"]
    assert schema["properties"]["image"] == {"type": "object"}
    assert config["properties"]["batch"]["properties"]["size"] == {
        "type": "integer",
        "minimum": 1,
        "default": 500,
        "x-scalo-dial": "big",
    }
    assert config["properties"]["log"]["properties"]["level"] == {
        "type": "string",
        "enum": ["info", "debug"],
        "default": "info",
        "x-scalo-dial": "small",
    }
    assert config["properties"]["workers"]["x-scalo-dial"] == "big"
    assert "plain" not in config.get("properties", {})


def test_assemble_lists_each_dial_commented_in_values_yaml(tmp_path: Path) -> None:
    text = (_assemble(tmp_path, "out") / "values.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(text) == {"config": {}, "image": {"digest": DIGEST}}
    assert "# config.batch.size: 500  # big" in text
    assert '# config.log.level: "info"  # small' in text
    assert "# config.workers: 4  # big" in text


def test_assemble_carries_the_lint_skips_outside_the_package(tmp_path: Path) -> None:
    chart = _assemble(tmp_path, "out")
    config = yaml.safe_load((chart / ".hyperi-ci.yaml").read_text(encoding="utf-8"))
    assert config == {"quality": {"checkov": {"skip": ["CKV_K8S_40"]}}}
    assert ".hyperi-ci.yaml" in (chart / ".helmignore").read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("overrides", "pin", "message"),
    [
        ({"app_name": "Bad_Name"}, {}, "not a Kubernetes Service name"),
        ({"schema_version": 5}, {}, "cannot render schema_version 5"),
        ({"config_schema": {"properties": {"x": {"x-scalo-dial": "huge"}}}}, {}, "is 'huge'"),
        (
            {"config_schema": {"properties": {"x": {"$ref": "#/$defs/none"}}}},
            {},
            "points at nothing",
        ),
        ({"config_schema": {"properties": {"x": {"$ref": "http://x/y"}}}}, {}, "not local"),
        ({}, {"digest": "sha256:short"}, "is not sha256"),
        ({}, {"tag": "bad tag"}, "is not a tag"),
    ],
)
def test_assemble_refuses_what_the_library_cannot_render(
    tmp_path: Path, overrides: dict, pin: dict, message: str
) -> None:
    with pytest.raises(w.WeaveError, match=message):
        _assemble(tmp_path, "out", _contract(**overrides), **pin)


def test_assemble_refuses_to_overwrite_a_chart(tmp_path: Path) -> None:
    _assemble(tmp_path, "out")
    with pytest.raises(w.WeaveError, match="already exists"):
        _assemble(tmp_path, "out")


def test_the_committed_contracts_are_the_apps_own() -> None:
    for service, app in (("dfe-ui", "dfe-ui"), ("hyperdx", "dfe-hyperdx")):
        contract = json.loads((CONTRACTS / f"{service}.json").read_text(encoding="utf-8"))
        assert (contract["schema_version"], contract["app_name"]) == (4, app)


# -------------------------------------------------------------- appset and layers


def _values_chart(root: Path, name: str) -> Path:
    """A stand-in thin chart that renders the values it was handed into one ConfigMap."""
    chart = root / name
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        f"apiVersion: v2\nname: {name}\nversion: 0.0.0\ntype: application\n", encoding="utf-8"
    )
    (chart / "templates" / "values.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: values\n"
        "data:\n  values: {{ toJson .Values | quote }}\n",
        encoding="utf-8",
    )
    return chart


def _merged(docs: list[dict]) -> dict:
    return json.loads(next(d for d in docs if d["metadata"]["name"] == "values")["data"]["values"])


def _target(
    service: str = "dfe-ui", profile: str = "scale", cloud: str = "aws", **facts: object
) -> object:
    return w.Target(service, profile, cloud, **facts)


def _inputs(**fields: object) -> object:
    return w.Inputs(helm=helm(), **fields)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_old_render_reads_its_layers_from_layer2_apps() -> None:
    rendered = w.render_app(_target(), "old", _inputs())
    assert [(c["file"], c["present"]) for c in rendered.chain[0]] == [
        ("argocd/values/common.yaml", True),
        ("argocd/values/aws.yaml", True),
        ("argocd/values/profile-scale.yaml", True),
        ("$values/infra/common.yaml", False),
        ("$values/values/dfe-ui-default-values.yaml", False),
    ]
    appset = yaml.safe_load((REPO_ROOT / w.APPSET).read_text(encoding="utf-8"))
    written = appset["spec"]["template"]["spec"]["sources"][0]["helm"]["valueFiles"]
    assert len(rendered.chain[0]) == len(written)
    assert (rendered.release, rendered.namespace) == ("dfe-ui-default-in-cluster", "dfe")
    assert {(d["kind"], d["metadata"]["name"]) for d in rendered.docs} >= {
        ("Deployment", "dfe-ui"),
        ("Service", "dfe-ui"),
    }


def test_new_render_puts_the_app_layers_after_the_profile_and_before_the_deploy_repo(
    tmp_path: Path,
) -> None:
    apps = tmp_path / "apps"
    for rel in (
        "_common.yaml",
        "dfe-ui/values.yaml",
        "dfe-ui/aws.yaml",
        "dfe-ui/profile-scale.yaml",
    ):
        _write(apps / rel, {"layer": rel, rel.replace("/", "_").removesuffix(".yaml"): True})
    deploy = tmp_path / "deploy"
    _write(deploy / "infra" / "common.yaml", {"layer": "infra", "infraOnly": True})
    _write(
        deploy / "values" / "dfe-ui-default-values.yaml",
        {"deploy": {"service": "dfe-ui", "instance": "default"}, "layer": "overlay", "env": "prod"},
    )
    rendered = w.render_app(
        _target(),
        "new",
        _inputs(chart=_values_chart(tmp_path, "dfe-ui"), apps_dir=apps, deploy_repo=deploy),
    )
    assert [c["file"] for c in rendered.chain[0]] == [
        "argocd/values/common.yaml",
        "argocd/values/aws.yaml",
        "argocd/values/profile-scale.yaml",
        "<apps-dir>/_common.yaml",
        "<apps-dir>/dfe-ui/values.yaml",
        "<apps-dir>/dfe-ui/aws.yaml",
        "<apps-dir>/dfe-ui/profile-scale.yaml",
        "<deploy-repo>/infra/common.yaml",
        "<deploy-repo>/values/dfe-ui-default-values.yaml",
    ]
    values = _merged(rendered.docs)
    assert values["layer"] == "overlay"
    assert all(
        values[k] for k in ("_common", "dfe-ui_values", "dfe-ui_aws", "dfe-ui_profile-scale")
    )
    assert values["infraOnly"] is True
    # Parameters beat every values file, as Argo passes them with --set.
    assert values["env"] == "dev"
    assert values["imagePullSecrets"] == ["ghcr-pull-secret"]
    assert values["global"]["registry"] == "registry.example.com/dfe"


def test_a_missing_app_layer_is_skipped_as_the_appset_skips_it(tmp_path: Path) -> None:
    rendered = w.render_app(
        _target(profile="slim", cloud="local"),
        "new",
        _inputs(chart=_values_chart(tmp_path, "dfe-ui"), apps_dir=tmp_path / "empty"),
    )
    present = [c["file"] for c in rendered.chain[0] if c["present"]]
    assert present == [
        "argocd/values/common.yaml",
        "argocd/values/local.yaml",
        "argocd/values/profile-slim.yaml",
    ]


def test_the_new_render_carries_dfe_extras_as_its_second_source(tmp_path: Path) -> None:
    chart = _values_chart(tmp_path, "dfe-ui")
    rendered = w.render_app(_target(), "new", _inputs(chart=chart))
    assert len(rendered.chain) == 2
    assert [c["file"] for c in rendered.chain[1]] == [c["file"] for c in rendered.chain[0]]
    ids = {(d["kind"], d["metadata"]["name"]) for d in rendered.docs}
    assert {("ExternalSecret", "dfe-ui-nextauth"), ("Password", "dfe-ui-nextauth-gen")} <= ids
    alone = w.render_app(_target(), "new", _inputs(chart=chart, extras=False))
    assert len(alone.chain) == 1
    assert ("ExternalSecret", "dfe-ui-nextauth") not in {
        (d["kind"], d["metadata"]["name"]) for d in alone.docs
    }


def test_the_dfe_extras_source_is_the_chart_source_with_chart_name_and_profile() -> None:
    app = w.application(REPO_ROOT, w.APPSET, _target(service="hyperdx"), None, helm())
    sources = app["spec"]["sources"]
    built = w.extras_source(sources, "dfe-hyperdx", "scale", w.INFRA_REPO_URL)
    params = {p["name"]: p["value"] for p in built["helm"]["parameters"]}
    assert (built["repoURL"], built["path"]) == (w.INFRA_REPO_URL, "helm/charts/dfe-extras")
    assert (params["chartName"], params["profile"]) == ("dfe-hyperdx", "scale")
    assert built["helm"]["valueFiles"] == sources[0]["helm"]["valueFiles"]
    # No release name: its objects carry the Application's app.kubernetes.io/instance.
    assert "releaseName" not in built["helm"]
    assert sources[0]["path"] == "helm/charts/hyperdx"
    listed = [*sources, {"repoURL": w.INFRA_REPO_URL, "path": "helm/charts/dfe-extras", "helm": {}}]
    assert w.extras_source(listed, "dfe-hyperdx", "scale", w.INFRA_REPO_URL) is listed[-1]


def _reworked_tree(root: Path, ignore_missing: bool = True, extra_file: str | None = None) -> Path:
    """A tree whose appset already lists the app layers and pulls the chart over OCI."""
    tree = root / "tree"
    files = [
        "$infra/argocd/values/common.yaml",
        "$infra/argocd/values/apps/{{ .deploy.service }}/values.yaml",
        "$values/infra/common.yaml",
    ]
    if extra_file:
        files.append(extra_file)
    template = {
        "metadata": {"name": "{{ .deploy.service }}-{{ .deploy.instance }}-{{ .name }}"},
        "spec": {
            "sources": [
                {
                    "repoURL": 'oci://{{ index .metadata.annotations "dfe.hyperi.io/registry" }}'
                    "/charts/{{ .deploy.service }}",
                    "path": ".",
                    "targetRevision": DIGEST,
                    "helm": {
                        "ignoreMissingValueFiles": ignore_missing,
                        "valueFiles": files,
                        "parameters": [
                            {"name": "fromParameter", "value": "{{ .deploy.instance }}"}
                        ],
                    },
                },
                {
                    "repoURL": '{{ index .metadata.annotations "dfe.hyperi.io/repo_url" }}',
                    "ref": "infra",
                },
                {
                    "repoURL": '{{ index .metadata.annotations "dfe.hyperi.io/config_repo_url" }}',
                    "ref": "values",
                },
            ],
            "destination": {
                "namespace": '{{ index .metadata.annotations "dfe.hyperi.io/dfe_namespace" }}'
            },
        },
    }
    _write(
        tree / "argocd/appsets/reworked.yaml",
        {"kind": "ApplicationSet", "spec": {"goTemplate": True, "template": template}},
    )
    _write(tree / "argocd/values/common.yaml", {"layer": "common"})
    _write(tree / "argocd/values/apps/dfe-ui/values.yaml", {"layer": "app", "appOnly": True})
    return tree


def test_a_reworked_appset_is_followed_as_written(tmp_path: Path) -> None:
    tree = _reworked_tree(tmp_path)
    rendered = w.render_app(
        _target(),
        "new",
        _inputs(
            repo=tree,
            appset=Path("argocd/appsets/reworked.yaml"),
            chart=_values_chart(tmp_path, "dfe-ui"),
            extras=False,
        ),
    )
    assert [(c["file"], c["present"]) for c in rendered.chain[0]] == [
        ("argocd/values/common.yaml", True),
        ("argocd/values/apps/dfe-ui/values.yaml", True),
        ("$values/infra/common.yaml", False),
    ]
    values = _merged(rendered.docs)
    assert (values["layer"], values["appOnly"], values["fromParameter"]) == ("app", True, "default")


def test_an_oci_source_must_name_the_thin_chart(tmp_path: Path) -> None:
    tree = _reworked_tree(tmp_path)
    inputs = _inputs(
        repo=tree,
        appset=Path("argocd/appsets/reworked.yaml"),
        chart=_values_chart(tmp_path, "other"),
        extras=False,
    )
    with pytest.raises(w.WeaveError, match="the thin chart is other"):
        w.render_app(_target(), "new", inputs)


def test_a_tree_without_dfe_extras_is_refused_unless_told(tmp_path: Path) -> None:
    tree = _reworked_tree(tmp_path)
    inputs = _inputs(
        repo=tree,
        appset=Path("argocd/appsets/reworked.yaml"),
        chart=_values_chart(tmp_path, "dfe-ui"),
    )
    with pytest.raises(w.WeaveError, match="pass --no-extras"):
        w.render_app(_target(), "new", inputs)


def test_an_oci_source_cannot_render_old_without_a_ref(tmp_path: Path) -> None:
    tree = _reworked_tree(tmp_path)
    with pytest.raises(w.WeaveError, match="--old-ref"):
        w.render_app(
            _target(), "old", _inputs(repo=tree, appset=Path("argocd/appsets/reworked.yaml"))
        )


def test_a_missing_value_file_fails_when_the_source_requires_it(tmp_path: Path) -> None:
    tree = _reworked_tree(tmp_path, ignore_missing=False)
    inputs = _inputs(
        repo=tree,
        appset=Path("argocd/appsets/reworked.yaml"),
        chart=_values_chart(tmp_path, "dfe-ui"),
        extras=False,
    )
    with pytest.raises(w.WeaveError, match="missing and the source requires it"):
        w.render_app(_target(), "new", inputs)


@pytest.mark.parametrize("escape", ["$infra/../outside.yaml", "../../../outside.yaml"])
def test_a_value_file_outside_its_repo_is_refused(tmp_path: Path, escape: str) -> None:
    tree = _reworked_tree(tmp_path, extra_file=escape)
    inputs = _inputs(
        repo=tree,
        appset=Path("argocd/appsets/reworked.yaml"),
        chart=_values_chart(tmp_path, "dfe-ui"),
        extras=False,
    )
    with pytest.raises(w.WeaveError, match="outside its repo"):
        w.render_app(_target(), "new", inputs)


def test_the_application_takes_name_namespace_and_parameters_from_the_appset() -> None:
    target = _target(namespace="dfe-x", registry="mirror.example.com/dfe")
    app = w.application(REPO_ROOT, w.APPSET, target, None, helm())
    spec = app["spec"]
    params = {p["name"]: p["value"] for p in spec["sources"][0]["helm"]["parameters"]}
    assert app["metadata"]["name"] == "dfe-ui-default-in-cluster"
    assert app["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "7"
    assert spec["destination"]["namespace"] == "dfe-x"
    assert params["global.registry"] == "mirror.example.com/dfe"
    assert params["profile"] == "scale"
    # The receiver address annotation is unset here, and reads empty as Argo's string map gives it.
    assert "<no value>" not in json.dumps(app)
    engine = w.application(REPO_ROOT, w.APPSET, _target(service="dfe-engine"), None, helm())
    assert engine["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "5"


def test_the_appset_refuses_a_cluster_with_no_registry() -> None:
    target = _target(annotations=(("dfe.hyperi.io/registry", ""),))
    with pytest.raises(w.WeaveError, match=re.escape("carries no dfe.hyperi.io/registry")):
        w.application(REPO_ROOT, w.APPSET, target, None, helm())


def test_an_instance_file_for_another_service_is_refused(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    _write(
        deploy / "values" / "dfe-ui-default-values.yaml",
        {"deploy": {"service": "dfe-engine", "instance": "default"}},
    )
    with pytest.raises(w.WeaveError, match="declares deploy"):
        w.generator_params(_target(), deploy)


def test_old_ref_reads_the_render_from_git() -> None:
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode != 0:
        pytest.skip("not a git checkout")
    from_git = w.render_app(_target(), "old", _inputs(old_ref="HEAD"))
    from_tree = w.render_app(_target(), "old", _inputs())
    assert from_git.docs == from_tree.docs


def test_application_runs_from_the_command_line(capsys: pytest.CaptureFixture[str]) -> None:
    code = w.main(
        [
            "application",
            "--service",
            "hyperdx",
            "--profile",
            "slim",
            "--cloud",
            "local",
            "--helm",
            helm(),
        ]
    )
    app = yaml.safe_load(capsys.readouterr().out)
    assert code == 0
    assert app["metadata"]["name"] == "hyperdx-default-in-cluster"
    assert app["spec"]["sources"][0]["path"] == "helm/charts/hyperdx"


# ------------------------------------------------------------------------- facets


def _deployment(
    name: str = "app", selector: str = "app", claim: str | None = None, **container: object
) -> dict:
    main = {"name": "main", "image": "registry.example.com/app:v1", **container}
    pod: dict = {"containers": [main]}
    if claim:
        pod["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]
    return {
        "kind": "Deployment",
        "metadata": {"name": name},
        "spec": {
            "selector": {"matchLabels": {"app.kubernetes.io/name": selector}},
            "template": {"metadata": {"labels": {"app.kubernetes.io/name": selector}}, "spec": pod},
        },
    }


def _service(name: str = "app", selector: str = "app") -> dict:
    return {
        "kind": "Service",
        "metadata": {"name": name},
        "spec": {
            "selector": {"app.kubernetes.io/name": selector},
            "ports": [{"name": "http", "port": 80, "targetPort": "http"}],
        },
    }


def _pvc(name: str) -> dict:
    return {"kind": "PersistentVolumeClaim", "metadata": {"name": name}, "spec": {}}


def _report(old: list[dict], new: list[dict]) -> dict:
    target = _target()
    return w.make_report(target, w.Rendered(old, "r", "ns"), w.Rendered(new, "r", "ns"))


def test_identical_renders_pass_every_gate_and_change_nothing() -> None:
    docs = [_deployment(claim="app-data"), _service(), _pvc("app-data")]
    report = _report(docs, docs)
    assert report["failed"] == []
    assert {name: f["status"] for name, f in report["facets"].items()} == {
        name: "PASS" if name in w.GATE_FACETS else "SAME" for name in report["facets"]
    }
    assert w.format_table(report).rstrip().endswith("gate: PASS")


def test_a_changed_deployment_selector_fails() -> None:
    report = _report([_deployment()], [_deployment(selector="app-renamed")])
    assert report["facets"]["selector"]["status"] == "FAIL"
    assert report["facets"]["selector"]["differ"] == ["Deployment/app"]
    assert report["failed"] == ["selector"]
    assert "NOT byte-equal: Deployment/app" in w.format_table(report)


def test_a_selector_with_one_extra_label_fails() -> None:
    wider = _deployment()
    wider["spec"]["selector"]["matchLabels"]["app.kubernetes.io/instance"] = "r"
    report = _report([_deployment()], [wider])
    assert report["failed"] == ["selector"]


def test_a_changed_service_selector_fails() -> None:
    report = _report([_deployment(), _service()], [_deployment(), _service(selector="other")])
    assert report["facets"]["selector"]["differ"] == ["Service/app"]
    assert report["failed"] == ["selector"]


def test_a_renamed_pvc_fails() -> None:
    old = [_deployment(claim="app-cursor"), _pvc("app-cursor")]
    new = [_deployment(claim="app-cursor-0"), _pvc("app-cursor-0")]
    report = _report(old, new)
    assert report["facets"]["pvcs"]["status"] == "FAIL"
    assert report["facets"]["pvcs"]["only_old"] == [
        "PersistentVolumeClaim/app-cursor",
        "claimName/app-cursor",
    ]
    assert report["failed"] == ["objects", "pvcs"]
    assert "LOST PersistentVolumeClaim/app-cursor" in w.format_table(report)


def test_a_claim_mounted_by_name_is_held_too() -> None:
    report = _report([_deployment(claim="shared")], [_deployment(claim="shared-new")])
    assert report["facets"]["pvcs"]["only_old"] == ["claimName/shared"]
    assert "pvcs" in report["failed"]


def test_a_lost_object_fails_and_an_added_one_does_not() -> None:
    lost = _report([_deployment(), _service()], [_deployment()])
    assert lost["facets"]["objects"]["only_old"] == ["Service/app"]
    assert "objects" in lost["failed"]
    added = _report([_deployment()], [_deployment(), _service()])
    assert added["facets"]["objects"]["only_new"] == ["Service/app"]
    assert added["failed"] == []


def test_probes_compare_by_value_not_by_spelling() -> None:
    old = _deployment(
        ports=[{"name": "api", "containerPort": 8000}],
        livenessProbe={"httpGet": {"path": "/livez", "port": "api"}},
    )
    new = _deployment(
        ports=[{"name": "metrics", "containerPort": 8000, "protocol": "TCP"}],
        livenessProbe={
            "httpGet": {"path": "/livez", "port": 8000},
            "failureThreshold": 3,
            "periodSeconds": 10,
            "timeoutSeconds": 1,
        },
    )
    facets = _report([old], [new])["facets"]
    assert facets["probes"]["status"] == "SAME"
    assert facets["ports"]["status"] == "DIFF"
    slower = _deployment(
        livenessProbe={"httpGet": {"path": "/livez", "port": 8000}, "timeoutSeconds": 3}
    )
    assert _report([old], [slower])["facets"]["probes"]["status"] == "DIFF"


def test_env_names_count_configmap_keys_brought_in_by_envfrom() -> None:
    old = _deployment(env=[{"name": "A", "value": "1"}, {"name": "B", "value": "2"}])
    new = _deployment(
        env=[{"name": "A", "value": "1"}], envFrom=[{"configMapRef": {"name": "app-env"}}]
    )
    configmap = {"kind": "ConfigMap", "metadata": {"name": "app-env"}, "data": {"B": "3"}}
    env = _report([old], [new, configmap])["facets"]["env"]
    assert env["status"] == "SAME"
    assert env["changed"] == {"Deployment/app": {"B": {"old": "2", "new": "3"}}}
    dropped = _report([old], [_deployment(env=[{"name": "A", "value": "1"}])])["facets"]["env"]
    assert dropped["only_old"] == {"Deployment/app": ["B"]}


def test_secret_refs_cover_env_envfrom_and_volumes() -> None:
    old = _deployment(
        env=[{"name": "PW", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]
    )
    old["spec"]["template"]["spec"]["volumes"] = [{"name": "ca", "secret": {"secretName": "ca"}}]
    new = _deployment(
        env=[
            {
                "name": "PW",
                "valueFrom": {"secretKeyRef": {"name": "s", "key": "k", "optional": True}},
            }
        ],
        envFrom=[{"prefix": "X_", "secretRef": {"name": "t"}}],
    )
    refs = _report([old], [new])["facets"]["secretKeyRefs"]
    assert refs["only_old"] == {
        "Deployment/app": [
            "env main/PW <- secret s/k optional=false",
            "volume ca <- secret ca optional=false",
        ]
    }
    assert refs["only_new"] == {
        "Deployment/app": [
            "env main/PW <- secret s/k optional=true",
            "envFrom main prefix=X_ <- secret t optional=false",
        ]
    }


def test_the_report_is_json_and_names_every_facet_in_the_table() -> None:
    report = _report([_deployment()], [_deployment(selector="moved")])
    assert json.loads(json.dumps(report)) == report
    table = w.format_table(report)
    for name in report["facets"]:
        assert name in table
    assert table.rstrip().endswith("gate: FAIL selector")


# --------------------------------------------- the dfe-ui and dfe-hyperdx renders


@pytest.mark.parametrize("profile", ["slim", "single", "scale"])
@pytest.mark.parametrize("service", ["dfe-ui", "hyperdx"])
def test_the_thin_chart_keeps_every_object_selector_and_claim(service: str, profile: str) -> None:
    report = diff_app(service, profile, "local")
    facets = report["facets"]
    assert report["failed"] == [], {name: facets[name] for name in report["failed"]}
    assert facets["objects"]["only_old"] == []
    assert all(
        facets["selector"]["old"][k] == facets["selector"]["new"][k]
        for k in facets["selector"]["old"]
    )
    assert facets["image"]["new"] != {}


def test_a_thin_chart_renamed_by_its_overlay_fails_the_gate(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    _write(
        deploy / "values" / "dfe-ui-default-values.yaml",
        {
            "deploy": {"service": "dfe-ui", "instance": "default"},
            "fullnameOverride": "dfe-ui-renamed",
        },
    )
    report = diff_app("dfe-ui", "slim", "local", deploy_repo=deploy)
    assert report["failed"] == ["objects", "selector"]
    assert report["facets"]["selector"]["new"]["Deployment/dfe-ui"] is None


def test_diff_exits_one_on_a_failed_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = [
        "diff",
        "--service",
        "dfe-ui",
        "--profile",
        "slim",
        "--cloud",
        "local",
        "--helm",
        helm(),
        "--contract",
        str(CONTRACTS / "dfe-ui.json"),
        "--library",
        str(library()),
        "--format",
        "json",
    ]
    assert w.main(base) == 0
    assert json.loads(capsys.readouterr().out)["failed"] == []
    deploy = tmp_path / "deploy"
    # dfe-extras refuses a name outside the project, so the rename stays inside it.
    _write(
        deploy / "values" / "dfe-ui-default-values.yaml",
        {"deploy": {"service": "dfe-ui", "instance": "default"}, "fullnameOverride": "dfe-other"},
    )
    assert w.main([*base, "--deploy-repo", str(deploy)]) == 1


def test_assembling_the_committed_contracts_is_deterministic(tmp_path: Path) -> None:
    lib = library()
    for service in ("dfe-ui", "hyperdx"):
        raw = (CONTRACTS / f"{service}.json").read_bytes()
        charts = [
            w.assemble(
                raw, lib, tmp_path / f"{service}-{n}", version="1.0.0", tag="v1.0.0", digest=DIGEST
            )
            for n in (1, 2)
        ]
        assert _files(charts[0]) == _files(charts[1])
        shutil.rmtree(tmp_path / f"{service}-1")
