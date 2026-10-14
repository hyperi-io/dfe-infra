#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_configmap_files.py
#  Purpose:      Prove every reader of a thin chart's config file and file sets
#                reads them from binaryData as it does from data.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""_gate.configmap_files, and the readers built on it, against binaryData.

    python3 -m pytest scripts/tests/test_configmap_files.py -q

The scalo-service library base64-encodes a thin chart's config file and file sets
under binaryData, because Helm deletes the text ``<no value>`` from anything it
renders as data. fixtures/binarydata/dfe-transform-vrl.yaml holds the ConfigMaps of
a render through that library with OVERLAY as the instance file. Every expectation
here is OVERLAY's own text, or a golden's, encoded by Python's base64 and never by
Helm.

The goldens hold data until _weave pins a library release that writes binaryData,
so each reader of a config file also runs on its golden with every thin-chart
ConfigMap re-encoded the way that release writes it.
"""

import base64
import copy
import importlib
import json

import pytest
import yaml

from _gate import (
    COMPONENTS,
    configmap_files,
    effective_env,
    golden,
    goldens,
    object_id,
    render_differences,
    runtime_view,
)
from _weave import TESTS

FIXTURE = TESTS / "fixtures" / "binarydata" / "dfe-transform-vrl.yaml"

# Text Helm strips from data, a template it must not expand, and non-ASCII text
# (e-acute, the euro sign, u-umlaut), each of which must reach the pod unchanged.
TEXT = f"caf{chr(0xE9)} {chr(0x20AC)} {chr(0xFC)}"
PROGRAM = f'.tag = "<no value>"\n.tpl = "{{{{ .Values.secret }}}}"\n  .text = "{TEXT}"\n'
TABLE = "abbreviation,offset\nAEST,+10:00\n"
CONFIG = {"absent": "<no value>", "template": "{{ .Values.secret }}", "text": TEXT}

# The instance file the fixture was rendered with.
OVERLAY = {
    "config": {"fixture": CONFIG},
    "fileSets": {
        "transforms": {"files": [{"name": "filebeat.vrl", "content": PROGRAM}]},
        "enrichment": {"files": [{"name": "timezones.csv", "content": TABLE}]},
    },
}

# The ConfigMaps the library writes after <fullname>: the config file, then each file set.
THIN_CONFIGMAPS = ("-config", "-transforms", "-enrichment")

# Each module-level reader of a thin chart's config file: (module, function, the
# golden it reads, the arguments after the render).
READERS = [
    ("test_transport", "transform_config", ("dfe-transform-vrl", "slim", "local"),
     ("dfe-transform-vrl",)),
    ("test_receiver_loader", "receiver_config", ("dfe-receiver", "slim", "local"), ()),
    ("test_vrl_offset_reset", "app_source", ("dfe-transform-vrl", "single", "local"), ()),
    ("test_vector_offset_reset", "app_source", ("dfe-transform-vector", "single", "local"), ()),
    ("test_enrichment_tables", "config", ("dfe-transform-vrl", "single", "local"),
     ("dfe-transform-vrl",)),
    ("test_render_properties", "config_file", ("dfe-loader", "scale", "aws"), ("dfe-loader",)),
    ("test_config_authority", "mounted_config", ("dfe-archiver", "slim", "local"), ()),
    ("test_app_env_contract", "config_file", ("dfe-fetcher", "single", "aws"), ("dfe-fetcher",)),
    ("test_transform_loader", "app_config", ("dfe-transform-vector", "slim", "local"),
     ("dfe-transform-vector",)),
    ("test_elastic_transform_config", "_config", ("dfe-transform-elastic", "single", "local"), ()),
    ("test_message_size_chain", "config_file", ("dfe-receiver", "scale", "aws"), ("dfe-receiver",)),
]


def _fixture() -> dict[str, dict]:
    docs = yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8"))
    return {d["metadata"]["name"]: d for d in docs if d}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _configmap(**maps: dict) -> dict:
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "probe"}, **maps}


def _as_the_library_writes(docs: list[dict]) -> list[dict]:
    """``docs`` with each thin-chart ConfigMap's data moved to binaryData, base64-encoded."""
    out = copy.deepcopy(docs)
    for doc in out:
        name = doc.get("metadata", {}).get("name", "")
        if doc.get("kind") == "ConfigMap" and name.endswith(THIN_CONFIGMAPS) and "data" in doc:
            doc["binaryData"] = {key: _b64(value) for key, value in doc.pop("data").items()}
    return out


# ------------------------------------------------------------- the library's render


@pytest.mark.parametrize("file_set", sorted(OVERLAY["fileSets"]))
def test_a_file_set_is_the_bytes_the_overlay_set(file_set: str) -> None:
    doc = _fixture()[f"dfe-transform-vrl-{file_set}"]
    held = {f["name"]: f["content"] for f in OVERLAY["fileSets"][file_set]["files"]}
    assert "data" not in doc
    assert doc["binaryData"] == {name: _b64(text) for name, text in held.items()}
    assert configmap_files(doc) == held


def test_the_config_file_keeps_the_text_helm_strips_from_data() -> None:
    files = configmap_files(_fixture()["dfe-transform-vrl-config"])
    assert list(files) == ["config.yaml"]
    assert "  absent: <no value>\n" in files["config.yaml"]
    assert yaml.safe_load(files["config.yaml"])["fixture"] == OVERLAY["config"]["fixture"]


def test_the_fixture_is_every_configmap_the_overlay_renders() -> None:
    wanted = ["config", *OVERLAY["fileSets"]]
    assert sorted(_fixture()) == sorted(f"dfe-transform-vrl-{name}" for name in wanted)


# ------------------------------------------------------------------- the helper


def test_data_reads_as_written() -> None:
    assert configmap_files(_configmap(data={"a.yaml": "a: 1\n"})) == {"a.yaml": "a: 1\n"}


def test_both_maps_are_read_together() -> None:
    doc = _configmap(data={"a.yaml": "a: 1\n"}, binaryData={"b.vrl": _b64(PROGRAM)})
    assert configmap_files(doc) == {"a.yaml": "a: 1\n", "b.vrl": PROGRAM}


def test_a_configmap_with_neither_map_mounts_no_files() -> None:
    """A file set with no files renders its ConfigMap with no data at all."""
    assert configmap_files(_configmap()) == {}
    assert configmap_files(_configmap(data=None, binaryData=None)) == {}


def test_a_key_in_both_maps_is_refused() -> None:
    doc = _configmap(data={"a.yaml": "a: 1\n"}, binaryData={"a.yaml": _b64("a: 2\n")})
    with pytest.raises(ValueError, match=r"ConfigMap/probe holds \['a.yaml'\] in both"):
        configmap_files(doc)


@pytest.mark.parametrize(
    "value",
    ["not base64!", "YWJj=", base64.b64encode(b"\xff\xfe").decode("ascii")],
    ids=["alphabet", "padding", "not-utf8"],
)
def test_a_value_that_is_not_base64_utf8_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match=r"ConfigMap/probe binaryData\[a\] is not base64"):
        configmap_files(_configmap(binaryData={"a": value}))


# ------------------------------------------------------------------ the gate's view


def test_the_gate_compares_binarydata_as_its_text() -> None:
    doc = _fixture()["dfe-transform-vrl-transforms"]
    assert runtime_view([doc])[object_id(doc)]["binaryData[filebeat.vrl]"] == PROGRAM


def test_env_takes_nothing_from_binarydata() -> None:
    """The kubelet reads only a ConfigMap's data into env, so the gate's env does too."""
    env = _configmap(binaryData={"LOG_LEVEL": _b64("debug")})
    env["metadata"]["name"] = "app-env"
    container = {"name": "app", "envFrom": [{"configMapRef": {"name": "app-env"}}]}
    workload = {
        "kind": "Deployment",
        "metadata": {"name": "app"},
        "spec": {"template": {"spec": {"containers": [container]}}},
    }
    assert effective_env([env, workload], "Deployment/app") == {"*<configMap app-env>": "<absent>"}


def test_the_gate_still_names_a_move_between_the_maps() -> None:
    """The same bytes moved from data to binaryData is a change to what Argo applies."""
    before = _configmap(data={"a.vrl": PROGRAM})
    after = _configmap(binaryData={"a.vrl": _b64(PROGRAM)})
    text = json.dumps(PROGRAM)
    assert render_differences([before], [after]) == [
        f'ConfigMap/probe binaryData[a.vrl]: "<absent>" -> {text}',
        f'ConfigMap/probe data[a.vrl]: {text} -> "<absent>"',
    ]


# --------------------------------------------------------------- every reader


@pytest.mark.parametrize("service", COMPONENTS)
def test_every_thin_configmap_reads_the_same_from_binarydata(service: str) -> None:
    for cell, docs in goldens(service):
        moved = {object_id(d): d for d in _as_the_library_writes(docs)}
        for doc in docs:
            if doc.get("kind") == "ConfigMap":
                assert configmap_files(moved[object_id(doc)]) == configmap_files(doc), cell


@pytest.mark.parametrize(
    ("module", "function", "cell", "args"),
    READERS,
    ids=[f"{module}.{function}" for module, function, _, _ in READERS],
)
def test_each_config_reader_reads_the_same_from_binarydata(
    module: str, function: str, cell: tuple[str, str, str], args: tuple[str, ...]
) -> None:
    reader = getattr(importlib.import_module(module), function)
    docs = golden(*cell)
    moved = _as_the_library_writes(docs)
    assert any("binaryData" in d for d in moved), f"{cell} holds no thin-chart ConfigMap"
    expected = reader(docs, *args)
    assert expected, f"{module}.{function} read nothing from {cell}"
    assert reader(moved, *args) == expected
