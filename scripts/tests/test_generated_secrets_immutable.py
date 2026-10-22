#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_generated_secrets_immutable.py
#  Purpose:      Prove every in-cluster generated password outside dfe-extras is
#                written once and to an immutable Secret, so ESO cannot mint a
#                second value under a server that already read the first.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The Kafka broker user, the kafbat break-glass admin, the DocumentDB gateway login and Keeper's identity.

ESO's `refreshPolicy: CreatedOnce` re-runs the generator on any reconcile that finds no
recorded sync or a target Secret whose data hash fails validation, and every run mints a
new value. The consumer that read the first value at start (the broker, the gateway) then
disagrees with one that reads a later copy. `target.immutable: true` makes ESO discard the
new value and leave the Secret as it was, and flips an existing mutable Secret without
writing a new value (ESO v2.5.0 to v2.11.0 `externalsecret_controller.go`, mutationFunc).

The ClickHouse admin password is pinned in test_clickhouse_admin_password.py. The
generators in the dfe-extras chart are not swept here yet.

    python3 scripts/tests/test_generated_secrets_immutable.py

Needs `helm` on PATH. Runs under pytest too, which is how CI reaches it.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from _charts import CHART_TREES
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Templates that render a generator-backed ExternalSecret and are pinned by this file or by
# test_clickhouse_admin_password.py. A new one fails the sweep until it is added to a test.
COVERED_TEMPLATES = {
    REPO_ROOT / "helm/charts/clickhouse-cluster/templates/admin-secret.yaml",
    REPO_ROOT / "helm/charts/clickhouse-cluster/templates/keeper-identity-secret.yaml",
    REPO_ROOT / "helm/charts/documentdb/templates/auth.yaml",
    REPO_ROOT / "helm/charts/kafbat/templates/externalsecret.yaml",
    REPO_ROOT / "helm/charts/kafka/templates/kafka-single-user.yaml",
}
# SHORTCUT: the dfe-extras generators are not asserted here, upgrade trigger: the change
# that makes them immutable lands, then this exclusion goes and its templates join the set.
NOT_SWEPT_YET = REPO_ROOT / "helm/charts/dfe-extras"


@dataclass(frozen=True, slots=True)
class Case:
    """One chart render and the generator-backed Secret it has to write."""

    label: str
    chart: str
    sets: tuple[str, ...]
    secret: str


KAFKA = Case("kafka single-tier broker user", "kafka", ("kafka.mode=single",), "dfe-kafka-user")
KAFBAT = Case("kafbat break-glass admin", "kafbat", (), "dfe-kafbat-breakglass")
DOCUMENTDB = Case(
    "documentdb gateway login",
    "documentdb",
    ("appNamespace=dfe-ui",),
    "dfe-documentdb-credentials",
)
KEEPER_IDENTITY = Case(
    "single-mode Keeper identity",
    "clickhouse-cluster",
    ("clickhouse.mode=single", "appNamespace=dfe"),
    "dfe-clickhouse-keeper-identity",
)


def render(case: Case) -> tuple[dict, ...]:
    """The chart as helm renders it with the case's values."""
    cmd = ["helm", "template", case.chart, str(REPO_ROOT / "helm" / "charts" / case.chart)]
    for value in case.sets:
        cmd += ["--set", value]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {case.label}:\n{out.stderr}")
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


def is_generated(external_secret: dict) -> bool:
    """True when a dataFrom entry takes its values from a generator."""
    entries = external_secret["spec"].get("dataFrom") or []
    return any((e.get("sourceRef") or {}).get("generatorRef") for e in entries)


def check_written_once_and_immutable(case: Case) -> None:
    docs = render(case)
    writers = [
        d
        for d in docs
        if d.get("kind") == "ExternalSecret"
        and is_generated(d)
        and d["spec"]["target"]["name"] == case.secret
    ]
    expect(
        f"{case.label}: one generator-backed ExternalSecret writes {case.secret}",
        len(writers) == 1,
        f"got {len(writers)}",
    )
    if len(writers) != 1:
        return
    spec = writers[0]["spec"]
    expect(
        f"{case.label}: {case.secret} is written once",
        spec.get("refreshPolicy") == "CreatedOnce",
        f"got {spec.get('refreshPolicy')}",
    )
    expect(
        f"{case.label}: {case.secret} is immutable, so a re-run generator cannot replace it",
        spec["target"].get("immutable") is True,
        f"got {spec['target']}",
    )


def test_the_kafka_broker_user_cannot_be_reminted() -> None:
    check_written_once_and_immutable(KAFKA)


def test_the_kafbat_break_glass_password_cannot_be_reminted() -> None:
    check_written_once_and_immutable(KAFBAT)


def test_the_documentdb_gateway_login_cannot_be_reminted() -> None:
    """The gateway creates the user once, so a second value locks hyperdx out."""
    check_written_once_and_immutable(DOCUMENTDB)


def test_the_single_mode_keeper_identity_cannot_be_reminted() -> None:
    """A second value locks the server out of every znode the first one created."""
    check_written_once_and_immutable(KEEPER_IDENTITY)


def test_every_generator_backed_template_is_covered_by_a_test() -> None:
    """Closed set: a new template with a generatorRef has to be asserted somewhere."""
    found = {
        path
        for tree in CHART_TREES
        for path in tree.rglob("templates/*.yaml")
        if NOT_SWEPT_YET not in path.parents
        and any(
            line.strip() == "generatorRef:"
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    }
    expect(
        "the covered set is exactly every generator-backed template outside dfe-extras",
        found == COVERED_TEMPLATES,
        f"not covered: {sorted(found - COVERED_TEMPLATES)}, "
        f"no longer generator-backed: {sorted(COVERED_TEMPLATES - found)}",
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    raise SystemExit(main())
