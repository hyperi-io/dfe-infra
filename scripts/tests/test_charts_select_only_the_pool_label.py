#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_charts_select_only_the_pool_label.py
#  Purpose:      Hold every chart but the pool chart to placing pods by the pool
#                label and the cloud-neutral node labels, never a cloud's own.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""No chart outside the pool chart places a pod by a cloud's or a node autoscaler's label.

A workload reaches its pool through `dfe.hyperi.io/workload`, which every pool
puts on its nodes and taints its dedicated nodes with, and through the
cloud-neutral well-known labels: kubernetes.io/arch, kubernetes.io/os,
kubernetes.io/hostname, topology.kubernetes.io/zone and topology.kubernetes.io/region.
A nodeSelector, node affinity, toleration or spread constraint on a key under
`karpenter.sh/`, `karpenter.k8s.aws/`, `eks.amazonaws.com/`, `cloud.google.com/`,
`kubernetes.azure.com/` or `node.kubernetes.io/instance-type` matches no node on
any other cloud or node autoscaler, and the render that ships it is green.

Three reads, because each sees what the others miss:

- the templates as text, every branch, including the cloud-gated ones a local
  render skips;
- the values files parsed, where the selectors a template copies in with toYaml live;
- the charts rendered over the standard cascade (test_pod_spec_invariants.py), for
  a selector a template builds in a variable before the field it lands in.

A `topologyKey` names a node label wherever it sits, so the values file's
`podSpread.topologyKey` and a pod affinity's are read as well.

    python3 scripts/tests/test_charts_select_only_the_pool_label.py

Runs under pytest too, which is how CI reaches it. The rendered read needs `helm`.
"""

import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

import test_pod_spec_invariants as pod_specs
import yaml

from _charts import HELM, REPO_ROOT, chart_dir
from _expect import expect, standalone, summary

# The one chart whose job is to name the node autoscaler's own labels.
POOL_CHART = chart_dir("karpenter-pools")

# The fields that place a pod on a node, by the key each one names.
FIELDS = ("nodeSelector", "nodeAffinity", "tolerations", "topologySpreadConstraints")

# Labels and taints only one cloud or one node autoscaler puts on a node.
DENIED = (
    "karpenter.sh/",
    "karpenter.k8s.aws/",
    "eks.amazonaws.com/",
    "node.kubernetes.io/instance-type",
    "cloud.google.com/",
    "kubernetes.azure.com/",
)
_DENIED = re.compile("|".join(re.escape(prefix) for prefix in DENIED))

# What a chart places pods by instead, every one of them set on every node pool.
ALLOWED = (
    "dfe.hyperi.io/workload",
    "kubernetes.io/arch",
    "kubernetes.io/os",
    "kubernetes.io/hostname",
    "topology.kubernetes.io/zone",
    "topology.kubernetes.io/region",
)

# A template line that opens one of FIELDS, or names a topologyKey.
_FIELD_LINE = re.compile(rf"^(?P<indent> *)(?:- )?(?P<field>{'|'.join(FIELDS)}|topologyKey)\s*:")
_COMMENT = re.compile(r"^\s*(#|\{\{-?\s*/\*)")


def charts() -> list[Path]:
    """Every chart under helm/ the rule binds: all of them but the pool chart."""
    return sorted(path.parent for path in HELM.rglob("Chart.yaml") if path.parent != POOL_CHART)


def templates(chart: Path) -> list[Path]:
    """A chart's own templates, its vendored dependencies aside."""
    return sorted(
        path
        for path in chart.glob("templates/**/*")
        if path.suffix in (".yaml", ".yml", ".tpl") and path.is_file()
    )


def values_files(chart: Path) -> list[Path]:
    return sorted(chart.glob("values*.yaml"))


def selector_lines(text: str) -> Iterator[tuple[int, str, str]]:
    """(line number, field, line) for every line inside a node-placement field of a template.

    A field's block runs on while lines are indented past it. A template action
    line belongs to the block whatever its indent, because Helm strips it.
    """
    lines = text.splitlines()
    for start, line in enumerate(lines):
        opened = _FIELD_LINE.match(line)
        if not opened:
            continue
        depth = len(opened["indent"])
        yield start + 1, opened["field"], line
        for number, following in enumerate(lines[start + 1 :], start + 2):
            stripped = following.strip()
            indent = len(following) - len(following.lstrip())
            if stripped and not stripped.startswith("{{") and indent <= depth:
                break
            yield number, opened["field"], following


def template_offences(text: str) -> list[str]:
    """`<line>: <field>: <line text>` for each denied key inside a node-placement field.

    A line inside two fields, such as a spread constraint's own topologyKey, is
    reported once, under the field that opened first.
    """
    found: dict[int, str] = {}
    for number, field, line in selector_lines(text):
        if number in found or _COMMENT.match(line) or not _DENIED.search(line):
            continue
        found[number] = f"{number}: {field}: {line.strip()}"
    return [found[number] for number in sorted(found)]


def node_keys(node: object, field: str | None = None) -> Iterator[tuple[str, str]]:
    """(field, key) for each node label or taint key a parsed manifest or values tree places by.

    That is a nodeSelector's keys, a node affinity's or a toleration's `key`, and
    every `topologyKey`. A spread constraint's labelSelector names pod labels, so
    it is not read.
    """
    if isinstance(node, list):
        for item in node:
            yield from node_keys(item, field)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key == "topologyKey" and isinstance(value, str):
            yield field or key, value
        elif field is None and key in FIELDS:
            yield from node_keys(value, key)
        elif field == "nodeSelector" and not isinstance(value, dict | list):
            yield field, str(key)
        elif field in ("nodeAffinity", "tolerations") and key == "key" and isinstance(value, str):
            yield field, value
        else:
            yield from node_keys(value, field)


def denied(pairs: Iterable[tuple[str, str]]) -> list[str]:
    """`<field>: <key>` for each pair whose key only one cloud or autoscaler sets."""
    return sorted({f"{field}: {key}" for field, key in pairs if _DENIED.match(key)})


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _rendered() -> list[tuple[str, tuple[dict, ...]]]:
    covered, _skipped = pod_specs.rendered()
    return [(name, docs) for name, docs in covered if name != POOL_CHART.name]


# ------------------------------------------------------------- the check itself


def test_the_check_fires_on_each_denied_key_in_each_field() -> None:
    """A check that cannot fail reads the same as a clean tree."""
    every_field = ["nodeAffinity", "nodeSelector", "tolerations", "topologySpreadConstraints"]
    for prefix in DENIED:
        key = f"{prefix}x" if prefix.endswith("/") else prefix
        values_text = (
            "nodeSelector:\n"
            f"  {key}: a\n"
            "tolerations:\n"
            f"  - key: {key}\n"
            "    operator: Exists\n"
            "affinity:\n"
            "  nodeAffinity:\n"
            "    requiredDuringSchedulingIgnoredDuringExecution:\n"
            "      nodeSelectorTerms:\n"
            "        - matchExpressions:\n"
            f"            - key: {key}\n"
            "topologySpreadConstraints:\n"
            f"  - topologyKey: {key}\n"
        )
        # The same fields as a pod spec in a template, with an action line inside one.
        lines = [f"      {line}" for line in values_text.splitlines()]
        lines.insert(-1, "        {{- range .Values.spread }}")
        template = "\n".join(lines) + "\n"
        fields = sorted(entry.split(": ")[1] for entry in template_offences(template))
        expect(f"{prefix} is caught in every field of a template", fields == every_field, f"{fields}")
        found = denied(node_keys(yaml.safe_load(values_text)))
        expect(
            f"{prefix} is caught in every field of a values tree",
            found == [f"{field}: {key}" for field in every_field],
            f"{found}",
        )


def test_the_check_passes_the_pool_label_and_a_cloud_key_outside_placement() -> None:
    """The pool label and the neutral labels place pods, and an annotation places nothing."""
    template = (
        "  annotations:\n"
        '    cloud.google.com/load-balancer-type: "Internal"\n'
        "      nodeSelector:\n"
        "        # karpenter.sh/capacity-type is the pool chart's to name, not this one's\n"
        + "".join(f"        {key}: a\n" for key in ALLOWED)
        + "      containers:\n"
        "        - name: karpenter.sh/not-a-selector\n"
    )
    found = template_offences(template)
    expect("nothing in the template is reported", found == [], f"{found}")
    values = {
        "nodeSelector": dict.fromkeys(ALLOWED, "a"),
        "podSpread": {"topologyKey": "kubernetes.io/hostname"},
        "serviceAccount": {"annotations": {"eks.amazonaws.com/role-arn": "x"}},
    }
    found = denied(node_keys(values))
    expect("nor in the values tree", found == [], f"{found}")


def test_the_sweep_reads_every_chart_and_the_selectors_they_carry() -> None:
    """A sweep whose globs stopped matching reports the same clean as a clean tree."""
    found = charts()
    expect("the sweep reaches the charts under helm/", len(found) >= 10, f"{[_rel(c) for c in found]}")
    expect(
        "and leaves out only the pool chart",
        POOL_CHART.is_dir() and POOL_CHART not in found,
        _rel(POOL_CHART),
    )
    lines = sum(
        1
        for chart in found
        for template in templates(chart)
        for _ in selector_lines(template.read_text(encoding="utf-8"))
    )
    expect("the template read finds the placement fields there are", lines > 50, f"{lines} line(s)")
    keys = {
        pair
        for chart in found
        for values in values_files(chart)
        for pair in node_keys(yaml.safe_load(values.read_text(encoding="utf-8")))
    }
    expect(
        "the values read finds the dedicated pools' tolerations and the spread key",
        {("tolerations", "dfe.hyperi.io/workload"), ("topologyKey", "kubernetes.io/hostname")} <= keys,
        f"{sorted(keys)}",
    )
    rendered = {pair for _name, docs in _rendered() for doc in docs for pair in node_keys(doc)}
    expect("the rendered read finds the placements the cascade sets", bool(rendered), "none rendered")


# ------------------------------------------------------------- the charts


def test_no_template_places_a_pod_by_a_cloud_label() -> None:
    offenders = [
        f"{_rel(template)}:{entry}"
        for chart in charts()
        for template in templates(chart)
        for entry in template_offences(template.read_text(encoding="utf-8"))
    ]
    expect(
        "no chart template places a pod by a cloud or node-autoscaler label",
        not offenders,
        "place by dfe.hyperi.io/workload or a well-known label: " + "; ".join(offenders),
    )


def test_no_values_file_places_a_pod_by_a_cloud_label() -> None:
    offenders = [
        f"{_rel(values)}: {entry}"
        for chart in charts()
        for values in values_files(chart)
        for entry in denied(node_keys(yaml.safe_load(values.read_text(encoding="utf-8"))))
    ]
    expect(
        "no chart's values place a pod by a cloud or node-autoscaler label",
        not offenders,
        "place by dfe.hyperi.io/workload or a well-known label: " + "; ".join(offenders),
    )


def test_no_rendered_chart_places_a_pod_by_a_cloud_label() -> None:
    offenders = [
        f"{name} {doc.get('kind')}/{(doc.get('metadata') or {}).get('name')}: {entry}"
        for name, docs in _rendered()
        for doc in docs
        for entry in denied(node_keys(doc))
    ]
    expect(
        "no rendered chart places a pod by a cloud or node-autoscaler label",
        not offenders,
        "place by dfe.hyperi.io/workload or a well-known label: " + "; ".join(offenders),
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
