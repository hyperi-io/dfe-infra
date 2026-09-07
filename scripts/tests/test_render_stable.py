#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_stable.py
#  Purpose:      Prove a chart render is reproducible, so Argo re-rendering an
#                app cannot change a generated secret out from under live pods.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Render determinism for charts that carry a generated secret.

Argo renders with `helm template`, never `helm install`, so `lookup` always
returns nil and a lookup-guarded `randAlphaNum` mints a NEW value on every
render. That is what re-minted the dfe-engine JWT signing key on every sync,
rolled the engine through Reloader, and killed every issued token (#224). The
render is the thing that has to be stable, so the assertion is byte equality
across two independent renders of the same inputs.

    python3 scripts/tests/test_render_stable.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
ENGINE = CHARTS / "dfe-engine"

# A mint function guarded by a cluster read: the pairing that only works under
# `helm install`, and silently re-mints under every `helm template`.
MINTERS = re.compile(r"\brand(AlphaNum|Alpha|Numeric|Ascii|Bytes)\b")

_failures = 0


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


def render(chart: Path, *sets: str) -> str:
    cmd = ["helm", "template", chart.name, str(chart)]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart.name} {sets}:\n{out.stderr}")
    return out.stdout


def docs(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d]


def named(text: str, kind: str, name: str) -> list[dict]:
    return [d for d in docs(text) if d.get("kind") == kind and d["metadata"]["name"] == name]


def test_engine_renders_identically_twice() -> None:
    """The whole chart, not just the Secret -- any drift here rolls a pod."""
    first = render(ENGINE)
    second = render(ENGINE)
    expect("dfe-engine renders byte-identically twice", first == second,
           "two renders of the same inputs differ")


def test_jwt_key_is_not_minted_by_the_template() -> None:
    """A template-minted key is a new key per render; ESO writes one once."""
    out = render(ENGINE)
    gens = named(out, "Password", "dfe-engine-jwt-gen")
    es = named(out, "ExternalSecret", "dfe-engine-jwt")
    plain = named(out, "Secret", "dfe-engine-jwt")
    expect("the default renders the ESO generator", len(gens) == 1, f"got {len(gens)}")
    expect("the default renders the ExternalSecret", len(es) == 1, f"got {len(es)}")
    expect("the default renders no template-minted Secret", plain == [], "a Secret rendered")
    if es:
        spec = es[0]["spec"]
        expect("the key is written once, not refreshed",
               spec.get("refreshPolicy") == "CreatedOnce", f"got {spec.get('refreshPolicy')}")
        expect("no refresh interval reopens the generator",
               str(spec.get("refreshInterval")) == "0", f"got {spec.get('refreshInterval')}")


def test_a_pinned_key_renders_that_key_and_nothing_else() -> None:
    out = render(ENGINE, "auth.jwtSecret=pinned-key-value")
    plain = named(out, "Secret", "dfe-engine-jwt")
    gens = named(out, "Password", "dfe-engine-jwt-gen")
    expect("a pinned key renders one plain Secret", len(plain) == 1, f"got {len(plain)}")
    expect("a pinned key renders no generator", gens == [], "a generator rendered")
    if plain:
        expect("the pinned key is the one rendered",
               plain[0]["stringData"]["jwt-secret"] == "pinned-key-value",
               f"got {plain[0]['stringData']}")


def test_creation_can_be_handed_to_the_deployment() -> None:
    """A deployment supplying the Secret itself must get nothing from the chart."""
    out = render(ENGINE, "auth.jwtSecretCreate=false")
    expect("jwtSecretCreate=false renders no generator",
           named(out, "Password", "dfe-engine-jwt-gen") == [], "a generator rendered")
    expect("jwtSecretCreate=false renders no ExternalSecret",
           named(out, "ExternalSecret", "dfe-engine-jwt") == [], "an ExternalSecret rendered")
    expect("jwtSecretCreate=false renders no Secret",
           named(out, "Secret", "dfe-engine-jwt") == [], "a Secret rendered")


def render_fails(chart: Path, *sets: str) -> str:
    """The stderr of a render expected to be refused, or "" when it succeeded."""
    cmd = ["helm", "template", chart.name, str(chart)]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return out.stderr if out.returncode != 0 else ""


def test_the_admin_password_is_minted_once_like_the_signing_key() -> None:
    """The deploy mints the admin login now (#233), by the same mechanism.

    A template-minted password is a new password on every Argo sync, which locks
    the operator out of the account they were handed.
    """
    out = render(ENGINE)
    for secret, key in (
        ("dfe-engine-admin", "admin-password"),
        ("dfe-engine-breakglass", "breakglass-password"),
    ):
        gens = named(out, "Password", f"{secret}-gen")
        es = named(out, "ExternalSecret", secret)
        expect(f"{secret} renders the ESO generator", len(gens) == 1, f"got {len(gens)}")
        expect(f"{secret} renders the ExternalSecret", len(es) == 1, f"got {len(es)}")
        expect(f"{secret} renders no template-minted Secret",
               named(out, "Secret", secret) == [], "a Secret rendered")
        if es:
            spec = es[0]["spec"]
            expect(f"{secret} is written once, not refreshed",
                   spec.get("refreshPolicy") == "CreatedOnce", f"got {spec.get('refreshPolicy')}")
            expect(f"{secret} carries the key the engine reads",
                   key in (spec["target"]["template"]["data"]), f"got {spec['target']}")


def test_the_seed_accounts_secret_no_longer_claims_the_admin_password() -> None:
    """Two Secrets each claiming to be the admin password is what #233 collapsed."""
    out = render(ENGINE, "seedAuth.seedAccounts[0].username=alice",
                 "seedAuth.seedAccounts[0].password=s3cret")
    seed = named(out, "Secret", "dfe-engine-seed-accounts")
    expect("the seed-accounts Secret still renders", len(seed) == 1, f"got {len(seed)}")
    if seed:
        expect("and carries only the named accounts",
               list(seed[0]["stringData"]) == ["seed-accounts"], f"got {seed[0]['stringData']}")


def test_the_engine_reads_the_minted_secret() -> None:
    """The chart wires what dfe-engine #300 reads, or the engine refuses to start."""
    deployment = named(render(ENGINE), "Deployment", "dfe-engine")[0]
    env = {e["name"]: e for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    ref = env["DFE_AUTH_LOCAL_ADMIN_PASSWORD"]["valueFrom"]["secretKeyRef"]
    expect("the admin password comes from the minted Secret",
           (ref["name"], ref["key"]) == ("dfe-engine-admin", "admin-password"), f"got {ref}")
    expect("the Secret is named for the login page's fetch command",
           env["DFE_AUTH_LOCAL_ADMIN_SECRET_NAME"]["value"] == "dfe-engine-admin",
           f"got {env.get('DFE_AUTH_LOCAL_ADMIN_SECRET_NAME')}")
    expect("the namespace comes off the pod, so the command is complete",
           env["DFE_DEPLOYMENT_NAMESPACE"]["valueFrom"]["fieldRef"]["fieldPath"]
           == "metadata.namespace", f"got {env.get('DFE_DEPLOYMENT_NAMESPACE')}")
    bg = env["DFE_AUTH_BREAKGLASS_PASSWORD"]["valueFrom"]["secretKeyRef"]
    expect("the break-glass password comes from its own Secret",
           (bg["name"], bg["key"]) == ("dfe-engine-breakglass", "breakglass-password"),
           f"got {bg}")


def test_a_non_dev_posture_cannot_render_without_minting() -> None:
    """The production half of the model: refuse at render, not at first login."""
    expect(
        "a production posture minting nothing is refused",
        "adminSecretName is empty" in render_fails(ENGINE, "env=production",
                                                   "auth.adminSecretName="),
        "the render succeeded",
    )
    expect(
        "and the shipped default pinned outside dev is refused",
        "shipped default" in render_fails(ENGINE, "env=production",
                                          "auth.adminPassword=changeme"),
        "the render succeeded",
    )
    expect(
        "and a padded default too, since the engine trims before comparing",
        "shipped default" in render_fails(ENGINE, "env=production",
                                          "auth.adminPassword=  changeme  "),
        "the render succeeded",
    )
    expect(
        "a dev posture may still tyre-kick on a known password",
        render_fails(ENGINE, "env=local", "auth.adminPassword=changeme") == "",
        "a dev render was refused",
    )


def test_no_chart_mints_a_secret_behind_a_lookup() -> None:
    """The #224 shape, repo-wide: a cluster read cannot guard a render-time mint."""
    offenders = []
    for template in CHARTS.glob("*/templates/**/*.yaml"):
        body = template.read_text(encoding="utf-8", errors="replace")
        code = "\n".join(
            line for line in body.splitlines() if "{{" in line or "{{-" in line
        )
        if MINTERS.search(code):
            offenders.append(str(template.relative_to(REPO_ROOT)))
    expect("no chart template mints a value at render time", offenders == [],
           f"got {offenders}")


def main() -> int:
    test_engine_renders_identically_twice()
    test_jwt_key_is_not_minted_by_the_template()
    test_a_pinned_key_renders_that_key_and_nothing_else()
    test_creation_can_be_handed_to_the_deployment()
    test_the_admin_password_is_minted_once_like_the_signing_key()
    test_the_seed_accounts_secret_no_longer_claims_the_admin_password()
    test_the_engine_reads_the_minted_secret()
    test_a_non_dev_posture_cannot_render_without_minting()
    test_no_chart_mints_a_secret_behind_a_lookup()
    print(f"\n{_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
