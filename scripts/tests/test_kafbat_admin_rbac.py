#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_kafbat_admin_rbac.py
#  Purpose:      Prove Kafbat's own OIDC grants admin to the admin groups and to
#                no other group, and that the break-glass login form renders no
#                role map while that OIDC is not live.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for Kafbat's default role map.

Kafbat runs its own OIDC, so the edge gate never sees it and its RBAC is the
only group check. A role with no subject grants nobody anything, so an empty
admin role locks every admin out of a deployment whose Kafbat OIDC is live.

The admin role defaults to the groups the edge gate admits to the other admin
UIs, the gateway chart's oidc.adminGroups. ro and rw stay empty, because the
admin UIs are for admins only. Without a usable OIDC block the chart falls back
to the break-glass LOGIN_FORM and renders no rbac block at all.

    python3 scripts/tests/test_kafbat_admin_rbac.py

Runs standalone or under pytest. Needs `helm` on PATH.
"""

import functools
import json
import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
KAFBAT = chart_dir("kafbat")
GATEWAY = chart_dir("envoy-gateway-config")
VALUES = REPO_ROOT / "argocd" / "values"
CONFIG = "application-local.yml"

# The canonical admin groups (dfe-engine docs/control-plane/rbac-vocabulary.md).
ADMIN_GROUPS = ["dfe-admins", "dfe-infra"]

# Every key kafbat.oidcIsUsable needs, so the chart renders an effective OAUTH2.
OIDC = (
    "--set", "oidc.enabled=true",
    "--set", "oidc.issuerUri=https://id.example.com",
    "--set", "oidc.clientId=kafbat",
    "--set", "oidc.clientSecretName=dfe-kafbat-oidc",
)
CLUSTERS = [{"name": "dfe", "bootstrapServers": "kafka.example.com:9092"}]
CLUSTER = ("--set-json", f"kafka.clusters={json.dumps(CLUSTERS)}")

# The shapes that leave Kafbat's OIDC unusable: the chart default, the switch
# alone, and the AWS cascade layer2-data renders, whose aws.yaml sets the switch.
BREAK_GLASS = {
    "chart defaults": (),
    "oidc switch alone": ("--set", "oidc.enabled=true"),
    "aws scale cascade": (
        "-f", str(VALUES / "common.yaml"),
        "-f", str(VALUES / "aws.yaml"),
        "-f", str(VALUES / "profile-scale.yaml"),
    ),
}


def chart_values(chart: Path) -> dict:
    return yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8"))


@functools.cache
def app_config(*args: str) -> dict:
    """The kafbat config file as the chart writes it into its ConfigMap."""
    out = subprocess.run(
        ["helm", "template", "kafbat", str(KAFBAT), "--show-only", "templates/configmap.yaml",
         *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for the kafbat chart:\n{out.stderr}")
    maps = [
        d for d in yaml.safe_load_all(out.stdout)
        if d and d.get("kind") == "ConfigMap" and CONFIG in (d.get("data") or {})
    ]
    if len(maps) != 1:
        raise SystemExit(f"the kafbat chart wrote {len(maps)} ConfigMaps carrying {CONFIG}")
    return yaml.safe_load(maps[0]["data"][CONFIG]) or {}


def roles(config: dict) -> dict[str, list[dict]]:
    """Each rendered role's subjects by role name; a role with none maps to []."""
    return {
        role["name"]: role.get("subjects") or []
        for role in (config.get("rbac") or {}).get("roles") or []
    }


def test_the_admin_default_is_the_edge_gates_admin_groups() -> None:
    """One set of admin groups across the admin UIs, so a group is admin everywhere or nowhere."""
    kafbat = chart_values(KAFBAT)["rbac"]["roles"]
    gateway = chart_values(GATEWAY)["oidc"]["adminGroups"]
    expect("kafbat's admin role defaults to the admin groups",
           kafbat["admin"]["subjects"] == ADMIN_GROUPS, f"got {kafbat['admin']['subjects']!r}")
    expect("the gateway admits the same groups to the other admin UIs",
           gateway == ADMIN_GROUPS, f"got {gateway!r}")
    for name in ("ro", "rw"):
        expect(f"kafbat's {name} role defaults to no group", not kafbat[name]["subjects"],
               f"got {kafbat[name]['subjects']!r}")


def test_live_oidc_grants_admin_to_the_admin_groups_and_no_one_else() -> None:
    config = app_config(*OIDC, *CLUSTER)
    auth = (config.get("auth") or {}).get("type")
    expect("a filled OIDC block renders OAUTH2", auth == "OAUTH2", f"got {auth!r}")
    by_role = roles(config)
    expect("the role map is admin, rw and ro", sorted(by_role) == ["admin", "ro", "rw"],
           f"got {sorted(by_role)}")
    admin = by_role.get("admin", [])
    groups = [s.get("value") for s in admin if s.get("type") == "role"]
    expect("admin is granted to exactly the admin groups", groups == ADMIN_GROUPS,
           f"got {groups!r}")
    expect("every admin group binds to the OIDC provider",
           all(s.get("provider") == "oauth" for s in admin if s.get("type") == "role"),
           f"got {admin!r}")
    others = [s for s in admin if s.get("type") != "role"]
    account = chart_values(KAFBAT)["breakGlass"]["username"]
    expect(
        "the only other admin subject is the break-glass account",
        others == [{"provider": "oauth", "type": "user", "value": account}],
        f"got {others!r}",
    )
    for name in ("ro", "rw"):
        subjects = by_role.get(name, [])
        expect(f"{name} is granted to nobody", not subjects, f"got {subjects!r}")


def test_an_overlay_can_still_name_its_own_admin_groups() -> None:
    config = app_config(*OIDC, *CLUSTER, "--set-json", 'rbac.roles.admin.subjects=["kafka-ops"]')
    groups = [s.get("value") for s in roles(config).get("admin", []) if s.get("type") == "role"]
    expect("an overlay's admin groups replace the default", groups == ["kafka-ops"],
           f"got {groups!r}")


def test_without_usable_oidc_the_break_glass_form_renders_no_rbac() -> None:
    """A role map binds only OIDC subjects, so under LOGIN_FORM it would lock the account out."""
    for shape, args in BREAK_GLASS.items():
        config = app_config(*args)
        auth = (config.get("auth") or {}).get("type")
        expect(f"the login form renders [{shape}]", auth == "LOGIN_FORM", f"got {auth!r}")
        expect(f"no rbac block renders [{shape}]", "rbac" not in config,
               f"got roles {sorted(roles(config))}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
