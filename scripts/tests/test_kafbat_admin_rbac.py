#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_kafbat_admin_rbac.py
#  Purpose:      Prove Kafbat's own OIDC grants admin to the admin groups and to
#                no other group, refuses to render with none, and that the
#                break-glass login form renders no role map while that OIDC is
#                not live.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for Kafbat's default role map.

Kafbat runs its own OIDC, so the edge gate never sees it and its RBAC is the
only group check. A role with no subject grants nobody anything, so an empty
admin role locks every admin out of a deployment whose Kafbat OIDC is live.

The admin role defaults to adminGroups in argocd/values/common.yaml, the same
list the edge gate admits to the other admin UIs, and an admin role left with no
group refuses to render. ro and rw stay empty, because the admin UIs are for
admins only. Without a usable OIDC block the chart falls back to the break-glass
LOGIN_FORM and renders no rbac block at all.

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
COMMON = ("-f", str(VALUES / "common.yaml"))
STAGED_EXAMPLE = VALUES / "staging" / "kafbat-oidc-rbac.example.yaml"
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


def values_file(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@functools.cache
def helm_configmap(*args: str) -> subprocess.CompletedProcess:
    """helm template of the kafbat ConfigMap alone, whether or not it rendered."""
    return subprocess.run(
        ["helm", "template", "kafbat", str(KAFBAT), "--show-only", "templates/configmap.yaml",
         *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def refusal(*args: str) -> str:
    """helm's error when the chart refuses these values; empty when it renders."""
    out = helm_configmap(*args)
    return out.stderr.strip() if out.returncode != 0 else ""


def app_config(*args: str) -> dict:
    """The kafbat config file as the chart writes it into its ConfigMap."""
    out = helm_configmap(*args)
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


def admin_groups(config: dict) -> list[str]:
    """The OIDC groups the rendered admin role grants."""
    return [s.get("value") for s in roles(config).get("admin", []) if s.get("type") == "role"]


def test_the_admin_groups_are_one_key_in_common_yaml() -> None:
    """One list across the admin UIs, so a group is admin everywhere or nowhere."""
    common = values_file(VALUES / "common.yaml").get("adminGroups")
    expect("common.yaml's adminGroups is the canonical pair", common == ADMIN_GROUPS,
           f"got {common!r}")
    kafbat = chart_values(KAFBAT)
    gateway = chart_values(GATEWAY)
    for name, values in (("kafbat", kafbat), ("gateway", gateway)):
        expect(f"the {name} chart names no admin group of its own", not values.get("adminGroups"),
               f"got {values.get('adminGroups')!r}")
    expect("the gateway chart carries no oidc.adminGroups", "adminGroups" not in gateway["oidc"],
           f"got {gateway['oidc'].get('adminGroups')!r}")
    for name, role in kafbat["rbac"]["roles"].items():
        expect(f"kafbat's {name} role names no group in the chart", not role["subjects"],
               f"got {role['subjects']!r}")


def test_live_oidc_grants_admin_to_the_admin_groups_and_no_one_else() -> None:
    config = app_config(*COMMON, *OIDC, *CLUSTER)
    auth = (config.get("auth") or {}).get("type")
    expect("a filled OIDC block renders OAUTH2", auth == "OAUTH2", f"got {auth!r}")
    by_role = roles(config)
    expect("the role map is admin, rw and ro", sorted(by_role) == ["admin", "ro", "rw"],
           f"got {sorted(by_role)}")
    admin = by_role.get("admin", [])
    groups = admin_groups(config)
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
    overlay = ("--set-json", 'rbac.roles.admin.subjects=["kafka-ops"]')
    for shape, base in (("with adminGroups", COMMON), ("without adminGroups", ())):
        groups = admin_groups(app_config(*base, *OIDC, *CLUSTER, *overlay))
        expect(f"an overlay's admin groups replace adminGroups [{shape}]",
               groups == ["kafka-ops"], f"got {groups!r}")


def test_no_admin_group_under_oidc_rbac_refuses_to_render() -> None:
    """An admin role with no group grants no OIDC user admin, so it is refused, not rendered."""
    shapes = {
        "no adminGroups": (*OIDC, *CLUSTER),
        "adminGroups empty": (*COMMON, *OIDC, *CLUSTER, "--set-json", "adminGroups=[]"),
        "adminGroups removed": (*COMMON, *OIDC, *CLUSTER, "--set", "adminGroups=null"),
        "adminGroups not a list": (*OIDC, *CLUSTER, "--set", "adminGroups=dfe-admins"),
    }
    for shape, args in shapes.items():
        error = refusal(*args)
        expect(f"the chart refuses [{shape}]", "the admin role names no group" in error,
               f"got {error or 'a render'}")
    kept = {
        "rbac off": (*OIDC, *CLUSTER, "--set", "rbac.enabled=false"),
        "login form": (),
    }
    for shape, args in kept.items():
        error = refusal(*args)
        expect(f"no admin group renders where no role map does [{shape}]", not error,
               f"got {error}")


def test_the_staged_example_takes_effect_as_a_values_file() -> None:
    """The appsets hand values files to the chart at the top level, so a nested block did nothing."""
    staged = values_file(STAGED_EXAMPLE)
    expect("the example carries no kafbat: block, which the chart never reads",
           "kafbat" not in staged, f"got {sorted(staged)}")
    config = app_config(*COMMON, "-f", str(STAGED_EXAMPLE))
    auth = (config.get("auth") or {}).get("type")
    expect("the example renders OAUTH2", auth == "OAUTH2", f"got {auth!r}")
    oauth = ((config.get("spring") or {}).get("security") or {}).get("oauth2") or {}
    client = ((oauth.get("client") or {}).get("registration") or {}).get("dfe") or {}
    issuer = ((oauth.get("client") or {}).get("provider") or {}).get("dfe") or {}
    expect("its client id reaches kafbat", client.get("client-id") == staged["oidc"]["clientId"],
           f"got {client.get('client-id')!r}")
    expect("its issuer reaches kafbat", issuer.get("issuer-uri") == staged["oidc"]["issuerUri"],
           f"got {issuer.get('issuer-uri')!r}")
    admins = admin_groups(config)
    expect("admin is the deployment's adminGroups", admins == ADMIN_GROUPS, f"got {admins!r}")
    by_role = roles(config)
    viewers = [s.get("value") for s in by_role.get("ro", []) if s.get("type") == "role"]
    expect("read-only is the example's viewer group",
           viewers == staged["rbac"]["roles"]["ro"]["subjects"], f"got {viewers!r}")
    expect("read-write is granted to nobody", not by_role.get("rw"), f"got {by_role.get('rw')!r}")
    clusters = {
        role["name"]: role.get("clusters")
        for role in (config.get("rbac") or {}).get("roles") or []
    }
    expect("every role covers the example's cluster",
           all(names == ["dfe"] for names in clusters.values()), f"got {clusters}")


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
