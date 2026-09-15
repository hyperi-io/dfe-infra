#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_cluster_secret_facts.py
#  Purpose:      Hold the three independent spellings of every conditional
#                cluster-secret annotation together.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The conditional cluster-secret annotations, spelled three times.

A tofu output reaches a chart by a chain nothing type-checks: bootstrap.sh
composes `<annotation>: "<value>"` into a `DFE_*_ANNOTATION` variable,
cluster-secret.yaml.tpl substitutes that variable, and an ApplicationSet reads
the annotation back by name. Each step spells the annotation independently, and
a `{{ index .metadata.annotations "..." }}` miss is not an error -- it renders
empty, the chart takes its own default, and the deployment is quietly wrong.

The unconditional annotations are spelled once in the template and need no such
guard; these are the ones whose key lives in bootstrap.sh instead.

    python3 scripts/tests/test_cluster_secret_facts.py

No test runner, matching the other checks here.
"""

from __future__ import annotations

import sys
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
APPSETS = REPO_ROOT / "argocd" / "appsets"

# (env var, annotation, the appset that reads it back, the chart key it lands on
# as that appset spells it -- a dotted `- name:` parameter for some, a nested
# key inside a `values:` block for others)
CONDITIONAL_FACTS = (
    ("DFE_KUBE_CLUSTER_NAME", "dfe.hyperi.io/cluster_name", "layer1-addons.yaml", "clusterName"),
    ("DFE_VPC_ID", "dfe.hyperi.io/vpc_id", "layer1-addons.yaml", "vpcId"),
    (
        "DFE_CLICKHOUSE_OBJECT_STORE_ENDPOINT",
        "dfe.hyperi.io/clickhouse_object_store_endpoint",
        "layer2-data.yaml",
        "clickhouse.objectStore.endpoint",
    ),
    (
        "DFE_KARPENTER_DISCOVERY_TAG",
        "dfe.hyperi.io/karpenter_discovery_tag",
        "layer2-platform.yaml",
        "discoveryTag",
    ),
    (
        "DFE_KARPENTER_INSTANCE_PROFILE",
        "dfe.hyperi.io/karpenter_instance_profile",
        "layer2-platform.yaml",
        "instanceProfile",
    ),
    (
        "DFE_KARPENTER_KMS_KEY_ID",
        "dfe.hyperi.io/karpenter_kms_key_id",
        "layer2-platform.yaml",
        "kmsKeyId",
    ),
    (
        "DFE_KARPENTER_POOLS",
        "dfe.hyperi.io/karpenter_pools",
        "layer2-platform.yaml",
        "pools",
    ),
)

# The JSON pool map carries double quotes of its own, so its annotation is
# single-quoted where every other one here is double-quoted, and it is the
# escaped copy that goes in the scalar rather than the raw value.
SINGLE_QUOTED = {"DFE_KARPENTER_POOLS": "DFE_KARPENTER_POOLS_YAML"}


def test_bootstrap_composes_each_annotation_under_its_own_key() -> None:
    """The `${VAR:+<annotation>: "..."}` form is what makes the line render
    blank rather than as an empty-valued annotation."""
    script = BOOTSTRAP.read_text()
    for env_key, annotation, _appset, _param in CONDITIONAL_FACTS:
        quote = "'" if env_key in SINGLE_QUOTED else '\\"'
        value_key = SINGLE_QUOTED.get(env_key, env_key)
        composed = f"{env_key}:+{annotation}: {quote}${{{value_key}}}{quote}"
        expect(f"bootstrap.sh composes {annotation} from {env_key}",
               composed in script, f"looked for: {composed}")


def test_a_single_quoted_annotation_escapes_a_quote_in_its_value() -> None:
    """A single quote inside the value would close the YAML scalar early, so the
    escaped copy doubles it before the annotation is composed."""
    script = BOOTSTRAP.read_text()
    for env_key, value_key in SINGLE_QUOTED.items():
        expect(f"{env_key} is escaped into {value_key} first",
               f"{value_key}=\"${{{env_key}//\\'/\\'\\'}}\"" in script,
               f"no doubling of ' for {env_key}")


def test_the_template_substitutes_every_composed_annotation() -> None:
    """A composed variable the template never substitutes reaches no cluster."""
    template = CLUSTER_SECRET.read_text()
    for env_key, annotation, _appset, _param in CONDITIONAL_FACTS:
        expect(f"cluster-secret.yaml.tpl substitutes {env_key}_ANNOTATION",
               f"${{{env_key}_ANNOTATION}}" in template, f"missing for {annotation}")


def test_each_appset_reads_the_annotation_bootstrap_writes() -> None:
    """The spelling that matters: an appset reading a key nothing writes gets an
    empty string and the chart's own default, with no error anywhere."""
    for _env_key, annotation, appset, chart_key in CONDITIONAL_FACTS:
        body = (APPSETS / appset).read_text()
        read = f'.metadata.annotations "{annotation}"'
        expect(f"{appset} reads {annotation}", read in body, f"not found in {appset}")
        # The key sits on the line that reads the annotation, or the one above:
        # a nested `key: {{ index ... }}` is one line, a parameter block's
        # `- name: <key>` / `value: {{ index ... }}` is two. Asserting only that
        # both strings appear somewhere would prove nothing about the pairing.
        lines = body.splitlines()
        paired = [
            i for i, ln in enumerate(lines)
            if read in ln and (chart_key in ln or (i and chart_key in lines[i - 1]))
        ]
        expect(f"{appset} lands {annotation} on {chart_key}",
               paired != [], f"no line in {appset} pairs them")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
