#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_weave_value_map.py
#  Purpose:      Prove scripts/weave/value-map.yaml maps or drops, with its
#                reason, every .Values key each switched component's 2.2.0
#                chart reads, so no deployer setting is lost unread.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The value-map completeness gate of the chart switch.

    python3 -m pytest scripts/tests/test_weave_value_map.py -q

A deploy repo's overlay sets keys the 2.2.0 chart reads. After the switch the
thin chart, its integration values and dfe-extras read them, at the same path or
another, and an overlay key nothing reads is silently lost: placement, a cursor
claim, a secret name. scripts/weave/value-map.yaml holds, per component, where
each 2.2.0 key goes, or why it is dropped.

The keys a 2.2.0 chart reads are found statically (reads_values): every
``.Values`` path in its templates and in the dfe-common helpers they include,
paths reached through a variable bound to one or a ``with`` block over one, the
keys of a ``dig`` over one, and every leaf of the chart's values.yaml. A path
built at render time (``index .Values $name``) is out of its reach.

Needs nothing but python: no render.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from _gate import COMPONENTS
from _weave import REPO_ROOT

VALUE_MAP = REPO_ROOT / "scripts" / "weave" / "value-map.yaml"
LIBRARY = REPO_ROOT / "helm" / "library" / "dfe-common" / "templates"
APPS = REPO_ROOT / "argocd" / "values" / "apps"
EXTRAS = REPO_ROOT / "helm" / "charts" / "dfe-extras" / "values.yaml"
COMMON = REPO_ROOT / "argocd" / "values" / "common.yaml"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-apps.yaml"
SKELETON_KEYS = (
    # scalo-service skeleton/values.schema.json, the keys the library reads.
    "affinity",
    "args",
    "autoscaling",
    "commonAnnotations",
    "commonLabels",
    "configMount",
    "configOverrides",
    "containerSecurityContext",
    "extraEnv",
    "extraEnvFrom",
    "extraObjects",
    "extraPorts",
    "extraVolumeMounts",
    "extraVolumes",
    "fileSets",
    "fullnameOverride",
    "global",
    "image",
    "imagePullSecrets",
    "initContainers",
    "keda",
    "livenessProbe",
    "networkPolicy",
    "nodeSelector",
    "otel",
    "partOf",
    "pdb",
    "podAnnotations",
    "podLabels",
    "podSecurityContext",
    "priorityClassName",
    "publicService",
    "readinessProbe",
    "reload",
    "replicaCount",
    "resources",
    "secrets",
    "service",
    "serviceAccount",
    "sidecars",
    "startupProbe",
    "strategy",
    "telemetry",
    "terminationGracePeriodSeconds",
    "tolerations",
    "topologySpreadConstraints",
    "versionCheck",
    "workingDir",
    "writablePaths",
    "config",
)

ACTION = re.compile(r"\{\{-?(.*?)-?\}\}", re.S)
COMMENT = re.compile(r"/\*.*?\*/", re.S)
DEFINE = re.compile(r'^\s*define\s+"([^"]+)"\s*$')
INCLUDE = re.compile(r'\b(?:include|template)\s+"([^"]+)"')
CHAIN = r"(?:\.Values(?:\.[A-Za-z_]\w*)+|\$[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
VALUES = re.compile(r"\.Values((?:\.[A-Za-z_]\w*)+)")
BIND = re.compile(r"(\$[A-Za-z_]\w*)\s*:?=\s*\(?\s*(" + CHAIN + r")")
VAR = re.compile(r"(\$[A-Za-z_]\w*)((?:\.[A-Za-z_]\w*)+)")
RELATIVE = re.compile(r"(?<![\w$)\]])\.([A-Za-z_]\w*)((?:\.[A-Za-z_]\w*)*)")
DIG = re.compile(r'\bdig((?:\s+"[^"]*")+)\s+(\S+)')
STRING = re.compile(r'"(?:[^"\\]|\\.)*"|`[^`]*`')
FIRST_CHAIN = re.compile(CHAIN)
BUILTINS = {"Values", "Release", "Chart", "Capabilities", "Template", "Files"}
BLOCKS = ("if", "range", "define", "block", "with")


# ---------------------------------------------------------------- the extractor


def _blocks(source: str) -> dict[str, str]:
    """A template file's define bodies by name, and its top level under ``""``."""
    text = COMMENT.sub("", source)
    found: dict[str, str] = {}
    current, depth, start = "", 0, 0
    top: list[str] = []
    for match in ACTION.finditer(text):
        action = match.group(1).strip()
        word = action.split(None, 1)[0] if action else ""
        named = DEFINE.match(action)
        if named and depth == 0:
            top.append(text[start : match.start()])
            current, depth, start = named.group(1), 1, match.end()
            continue
        if depth:
            if word in BLOCKS:
                depth += 1
            elif word == "end":
                depth -= 1
                if depth == 0:
                    found[current] = text[start : match.start()]
                    start = match.end()
    top.append(text[start:])
    found[""] = "".join(top)
    return found


@dataclass(slots=True)
class _Scope:
    """What a template body has bound: variables to paths, and each open block with
    the values path its dot stands for, or None where the dot is not one."""

    variables: dict[str, str] = field(default_factory=dict)
    frames: list[tuple[str, str | None]] = field(default_factory=list)

    def dot(self) -> str | None:
        return self.frames[-1][1] if self.frames else None


def _resolve(chain: str, scope: _Scope) -> str | None:
    if chain.startswith(".Values."):
        return chain.removeprefix(".Values.")
    head, _, rest = chain.partition(".")
    base = scope.variables.get(head)
    if base is None:
        return None
    return f"{base}.{rest}" if rest else base


def _paths_in(action: str, scope: _Scope) -> set[str]:
    bare = STRING.sub('""', action)
    found = {m.group(1)[1:] for m in VALUES.finditer(bare)}
    for var, rest in VAR.findall(bare):
        if var in scope.variables:
            found.add(scope.variables[var] + rest)
    dot = scope.dot()
    if dot:
        for name, rest in RELATIVE.findall(bare):
            if name not in BUILTINS:
                found.add(f"{dot}.{name}{rest}")
    for match in DIG.finditer(action):
        keys = re.findall(r'"([^"]*)"', match.group(1))
        follows = match.group(2)
        if follows[:1] in ("(", "$", "."):
            keys = keys[:-1]
        tail = action[match.end(1) :]
        target = FIRST_CHAIN.search(tail)
        base = _resolve(target.group(0), scope) if target else None
        if base and keys:
            found.add(".".join([base, *keys]))
    return found


def _body_paths(body: str) -> set[str]:
    scope = _Scope()
    found: set[str] = set()
    for match in ACTION.finditer(COMMENT.sub("", body)):
        action = match.group(1).strip()
        word = action.split(None, 1)[0] if action else ""
        found |= _paths_in(action, scope)
        for var, chain in BIND.findall(action):
            path = _resolve(chain, scope)
            if path:
                scope.variables[var] = path
        if word == "with":
            target = FIRST_CHAIN.search(action)
            scope.frames.append(("with", _resolve(target.group(0), scope) if target else None))
        elif word == "range":
            scope.frames.append(("range", None))
        elif word in ("if", "define", "block"):
            scope.frames.append((word, scope.dot()))
        elif word == "else" and scope.frames and scope.frames[-1][0] == "with":
            # A with's else branch keeps the outer dot; `else with X` moves it to X.
            target = FIRST_CHAIN.search(action) if action.startswith("else with") else None
            outer = scope.frames[-2][1] if len(scope.frames) > 1 else None
            scope.frames[-1] = ("with", _resolve(target.group(0), scope) if target else outer)
        elif word == "end" and scope.frames:
            scope.frames.pop()
    return found


def _leaves(node: object, path: str = "") -> set[str]:
    if isinstance(node, dict) and node:
        found: set[str] = set()
        for key, value in node.items():
            found |= _leaves(value, f"{path}.{key}" if path else str(key))
        return found
    return {path} if path else set()


def reads_values(chart: Path, library: Path = LIBRARY) -> set[str]:
    """Every ``.Values`` path a chart reads: templates, helpers they include, values.yaml."""
    defines: dict[str, str] = {}
    for tpl in sorted(library.glob("*.tpl")):
        for name, body in _blocks(tpl.read_text(encoding="utf-8")).items():
            if name:
                defines[name] = body
    bodies = []
    for tpl in sorted((chart / "templates").glob("*")):
        for name, body in _blocks(tpl.read_text(encoding="utf-8")).items():
            if name:
                defines[name] = body
            else:
                bodies.append(body)
    queue = [n for body in bodies for n in INCLUDE.findall(body)]
    reached: set[str] = set()
    while queue:
        name = queue.pop()
        if name in reached or name not in defines:
            continue
        reached.add(name)
        queue += INCLUDE.findall(defines[name])
    found: set[str] = set()
    for body in bodies + [defines[n] for n in sorted(reached)]:
        found |= _body_paths(body)
    values = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8")) or {}
    return found | _leaves(values)


# ------------------------------------------------------------------- the map


class MapError(Exception):
    """scripts/weave/value-map.yaml does not have the shape the gate reads."""


def load_map(path: Path = VALUE_MAP) -> dict[str, dict]:
    """The value map's apps, each ``{"chart": Path, "keys": {path: entry}}``, checked.

    Raises:
        MapError: An app or an entry the gate cannot read.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    apps = data.get("apps")
    if not isinstance(apps, dict):
        raise MapError(f"{path}: no `apps` mapping")
    found = {}
    for service, body in apps.items():
        if not isinstance(body, dict) or set(body) != {"chart", "keys"}:
            raise MapError(f"{service}: takes `chart` and `keys`")
        for key, entry in (body["keys"] or {}).items():
            if not isinstance(entry, dict) or set(entry) - {"to", "dropped", "note"}:
                raise MapError(f"{service} {key}: an entry takes to, dropped and note")
            if ("to" in entry) == ("dropped" in entry):
                raise MapError(f"{service} {key}: needs exactly one of `to` and `dropped`")
            values = (
                entry["to"]
                if isinstance(entry.get("to"), list)
                else [entry.get("to", entry.get("dropped"))]
            )
            if not values or not all(isinstance(v, str) and v.strip() for v in values):
                raise MapError(f"{service} {key}: `to` is a path or paths and `dropped` a reason")
        found[service] = {"chart": REPO_ROOT / body["chart"], "keys": dict(body["keys"] or {})}
    return found


def targets(entry: dict) -> list[str]:
    """The paths an entry's value moves to; none for a dropped key."""
    to = entry.get("to", [])
    return to if isinstance(to, list) else [to]


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + ".")


def unmapped(read: set[str], keys: dict[str, dict]) -> list[str]:
    """The read paths no entry covers: an entry covers itself and the keys beneath it.

    A path whose own subtree the map holds key by key is covered by those entries.
    """
    return sorted(
        p
        for p in read
        if not any(_under(p, k) for k in keys) and not any(_under(k, p) for k in keys)
    )


def stale(read: set[str], keys: dict[str, dict]) -> list[str]:
    """The entries for a key the chart does not read, nor any key around it."""
    return sorted(k for k in keys if not any(_under(k, p) or _under(p, k) for p in read))


def known_targets(service: str) -> set[str]:
    """The top-level keys a ``to`` may name: what the library, the integration files,
    the cascade, the appset's parameters and dfe-extras read for this component."""
    found = set(SKELETON_KEYS)
    for path in [*sorted((APPS / service).glob("*.yaml")), APPS / "_common.yaml", COMMON]:
        if path.is_file():
            found |= set(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    appset = yaml.safe_load(APPSET.read_text(encoding="utf-8"))
    for source in appset["spec"]["template"]["spec"]["sources"]:
        found |= {
            p["name"].split(".")[0] for p in (source.get("helm") or {}).get("parameters") or []
        }
    extras = yaml.safe_load(EXTRAS.read_text(encoding="utf-8"))
    chart_name = "dfe-hyperdx" if service == "hyperdx" else service
    found |= set(extras["extras"]["defaults"].get(chart_name) or {})
    for tpl in sorted((EXTRAS.parent / "templates").glob("*")):
        found |= {p.split(".")[0] for p in _body_paths(tpl.read_text(encoding="utf-8"))}
    return found


# ------------------------------------------------------------------- the gate


@pytest.mark.parametrize("service", COMPONENTS)
def test_every_key_the_2_2_0_chart_reads_is_mapped_or_dropped(service: str) -> None:
    app = load_map()[service]
    assert unmapped(reads_values(app["chart"]), app["keys"]) == []


@pytest.mark.parametrize("service", COMPONENTS)
def test_no_entry_names_a_key_the_chart_never_read(service: str) -> None:
    app = load_map()[service]
    assert stale(reads_values(app["chart"]), app["keys"]) == []


@pytest.mark.parametrize("service", COMPONENTS)
def test_every_target_is_a_key_something_reads(service: str) -> None:
    known = known_targets(service)
    keys = load_map()[service]["keys"]
    wrong = sorted(
        f"{k} -> {t}" for k, e in keys.items() for t in targets(e) if t.split(".")[0] not in known
    )
    assert wrong == []


def test_every_gated_component_has_a_map() -> None:
    assert set(COMPONENTS) <= set(load_map())


# ---------------------------------------------------------------- expected fails


def test_an_unmapped_key_fails() -> None:
    app = load_map()["dfe-ui"]
    keys = {k: e for k, e in app["keys"].items() if k != "config.nodeOptions"}
    assert unmapped(reads_values(app["chart"]), keys) == ["config.nodeOptions"]


def test_a_key_a_new_template_reads_fails(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    (chart / "templates").mkdir(parents=True)
    (chart / "values.yaml").write_text("a: 1\n", encoding="utf-8")
    (chart / "templates" / "x.yaml").write_text(
        "{{ .Values.a }}{{ $s := .Values.sec | default dict }}{{ $s.key }}"
        "{{ with .Values.w }}{{ .inner.leaf }}{{ end }}"
        '{{ dig "k1" "k2" "" (.Values.d | default dict) }}'
        "{{ range .Values.items }}{{ .notAValue }}{{ end }}",
        encoding="utf-8",
    )
    read = reads_values(chart, library=tmp_path)
    assert read == {"a", "sec", "sec.key", "w", "w.inner.leaf", "d", "d.k1.k2", "items"}
    assert unmapped(read, {"a": {"to": "a"}}) == [
        "d",
        "d.k1.k2",
        "items",
        "sec",
        "sec.key",
        "w",
        "w.inner.leaf",
    ]


def test_a_helper_the_chart_includes_is_read(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    (chart / "templates").mkdir(parents=True)
    (chart / "values.yaml").write_text("{}\n", encoding="utf-8")
    (chart / "templates" / "x.yaml").write_text('{{ include "lib.used" . }}', encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "_lib.tpl").write_text(
        '{{- define "lib.used" -}}{{ .Values.used }}{{ include "lib.nested" . }}{{- end -}}'
        '{{- define "lib.nested" -}}{{ .Values.nested }}{{- end -}}'
        '{{- define "lib.unused" -}}{{ .Values.unused }}{{- end -}}',
        encoding="utf-8",
    )
    assert reads_values(chart, library=lib) == {"used", "nested"}


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"to": "a", "dropped": "b"}, "exactly one"),
        ({}, "exactly one"),
        ({"dropped": ""}, "a reason"),
        ({"to": "a", "why": "b"}, "takes to, dropped and note"),
    ],
)
def test_a_malformed_entry_is_refused(tmp_path: Path, entry: dict, message: str) -> None:
    path = tmp_path / "map.yaml"
    body = {"apps": {"x": {"chart": "helm/charts/x", "keys": {"k": entry}}}}
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    with pytest.raises(MapError, match=message):
        load_map(path)
