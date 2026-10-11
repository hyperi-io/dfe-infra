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

The generated keys are dfe-extras objects (chartName dfe-engine, dfe-hyperdx).
The pods that read them are checked on the deployed render: the golden of each
cell, and the thin chart beside dfe-extras where a deployment turns a dial.

    python3 scripts/tests/test_render_stable.py

Needs `helm` on PATH; the dial cases render the thin chart, which needs the
scalo-service library (_weave.library). No test runner, matching the other checks
here.
"""

import re
import subprocess
import sys
from pathlib import Path

import yaml

from _charts import CHART_TREES
from _expect import expect, standalone, summary
from _gate import CLOUDS, PROFILES, effective_env, golden
from _weave import render_app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
EXTRAS = CHARTS / "dfe-extras"
ENGINE_EXTRAS = "chartName=dfe-engine"
CELLS = [(p, k) for p in PROFILES for k in CLOUDS]

# A mint function guarded by a cluster read: the pairing that only works under
# `helm install`, and silently re-mints under every `helm template`.
MINTERS = re.compile(r"\brand(AlphaNum|Alpha|Numeric|Ascii|Bytes)\b")


def render(chart: Path, *sets: str) -> str:
    cmd = ["helm", "template", chart.name, str(chart)]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart.name} {sets}:\n{out.stderr}")
    return out.stdout


def extras(component: str, *sets: str) -> str:
    """dfe-extras rendering one component's objects."""
    return render(EXTRAS, f"chartName={component}", *sets)


def docs(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d]


def named(text: str, kind: str, name: str) -> list[dict]:
    return [d for d in docs(text) if d.get("kind") == kind and d["metadata"]["name"] == name]


def test_engine_renders_identically_twice() -> None:
    """Every engine extra, not just the Secrets -- any drift here rolls a pod."""
    first = extras("dfe-engine")
    second = extras("dfe-engine")
    expect("dfe-extras renders the engine's objects byte-identically twice", first == second,
           "two renders of the same inputs differ")


def test_jwt_key_is_not_minted_by_the_template() -> None:
    """A template-minted key is a new key per render; ESO writes one once."""
    out = extras("dfe-engine")
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
    out = extras("dfe-engine", "auth.jwtSecret=pinned-key-value")
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
    out = extras("dfe-engine", "auth.jwtSecretCreate=false")
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
    out = extras("dfe-engine")
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
    out = extras("dfe-engine", "seedAuth.seedAccounts[0].username=alice",
                 "seedAuth.seedAccounts[0].password=s3cret")
    seed = named(out, "Secret", "dfe-engine-seed-accounts")
    expect("the seed-accounts Secret still renders", len(seed) == 1, f"got {len(seed)}")
    if seed:
        expect("and carries only the named accounts",
               list(seed[0]["stringData"]) == ["seed-accounts"], f"got {seed[0]['stringData']}")


def secret_ref(entry: object) -> tuple[str | None, str | None]:
    """(secret, key) of one effective env entry; (None, None) for a literal or nothing."""
    ref = ((entry if isinstance(entry, dict) else {}).get("valueFrom") or {}).get("secretKeyRef")
    return (ref.get("name"), ref.get("key")) if ref else (None, None)


def test_the_engine_reads_the_minted_secret() -> None:
    """The deployment wires what dfe-engine #300 reads, or the engine refuses to start."""
    for profile, cloud in CELLS:
        where = f"{profile}/{cloud}"
        docs = golden("dfe-engine", profile, cloud)
        env = effective_env(docs, "Deployment/dfe-engine")
        minted = {
            d["spec"]["target"]["name"]: d["spec"]["target"]["template"]["data"]
            for d in docs
            if d.get("kind") == "ExternalSecret"
        }
        ref = secret_ref(env.get("DFE_AUTH_LOCAL_ADMIN_PASSWORD"))
        expect(f"{where}: the admin password comes from the minted Secret",
               ref == ("dfe-engine-admin", "admin-password") and ref[1] in minted.get(ref[0], {}),
               f"got {ref}, minted {sorted(minted)}")
        expect(f"{where}: the Secret is named for the login page's fetch command",
               env.get("DFE_AUTH_LOCAL_ADMIN_SECRET_NAME") == "dfe-engine-admin",
               f"got {env.get('DFE_AUTH_LOCAL_ADMIN_SECRET_NAME')}")
        namespace = (env.get("DFE_DEPLOYMENT_NAMESPACE") or {}).get("valueFrom") or {}
        expect(f"{where}: the namespace comes off the pod, so the command is complete",
               namespace.get("fieldRef", {}).get("fieldPath") == "metadata.namespace",
               f"got {env.get('DFE_DEPLOYMENT_NAMESPACE')}")
        bg = secret_ref(env.get("DFE_AUTH_BREAKGLASS_PASSWORD"))
        expect(f"{where}: the break-glass password comes from its own minted Secret",
               bg == ("dfe-engine-breakglass", "breakglass-password")
               and bg[1] in minted.get(bg[0], {}),
               f"got {bg}, minted {sorted(minted)}")


def test_a_non_dev_posture_cannot_render_without_minting() -> None:
    """The production half of the model: refuse at render, not at first login."""
    expect(
        "a production posture minting nothing is refused",
        "adminSecretName is empty" in render_fails(EXTRAS, ENGINE_EXTRAS, "env=production",
                                                   "auth.adminSecretName="),
        "the render succeeded",
    )
    expect(
        "and the shipped default pinned outside dev is refused",
        "shipped default" in render_fails(EXTRAS, ENGINE_EXTRAS, "env=production",
                                          "auth.adminPassword=changeme"),
        "the render succeeded",
    )
    expect(
        "and a padded default too, since the engine trims before comparing",
        "shipped default" in render_fails(EXTRAS, ENGINE_EXTRAS, "env=production",
                                          "auth.adminPassword=  changeme  "),
        "the render succeeded",
    )
    expect(
        "a dev posture may still tyre-kick on a known password",
        render_fails(EXTRAS, ENGINE_EXTRAS, "env=local", "auth.adminPassword=changeme") == "",
        "a dev render was refused",
    )


# (env var, Secret, data key, value template, values block). The token key is the
# SHA-256 hex of its password because the image takes 64 hex chars or base64 of
# exactly 32 bytes, and refuses to start on anything else.
HYPERDX_KEYS = (
    ("EXPRESS_SESSION_SECRET", "dfe-hyperdx-session", "session-secret",
     "{{ .password }}", "sessionSecret"),
    ("TOKEN_ENCRYPTION_KEY", "dfe-hyperdx-token-encryption", "token-encryption-key",
     "{{ .password | sha256sum }}", "tokenEncryption"),
)


def hyperdx_env(rendered: list[dict], name: str) -> object:
    """One effective env entry of the dfe-hyperdx container, or {} when absent."""
    return effective_env(rendered, "Deployment/dfe-hyperdx").get(name, {})


def test_hyperdx_keys_are_minted_once_and_read_by_the_pod() -> None:
    """Unset, sessions are signed with upstream's published key and tokens stored plain."""
    first = extras("dfe-hyperdx")
    expect("dfe-extras renders the hyperdx objects byte-identically twice",
           first == extras("dfe-hyperdx"), "two renders of the same inputs differ")
    for env, secret, key, value, _ in HYPERDX_KEYS:
        gens = named(first, "Password", f"{secret}-gen")
        es = named(first, "ExternalSecret", secret)
        expect(f"{env} renders the ESO generator", len(gens) == 1, f"got {len(gens)}")
        expect(f"{env} renders the ExternalSecret", len(es) == 1, f"got {len(es)}")
        expect(f"{env} renders no template-minted Secret",
               named(first, "Secret", secret) == [], "a Secret rendered")
        if gens:
            expect(f"{env}'s generated password is at least 32 characters",
                   gens[0]["spec"]["length"] >= 32, f"got {gens[0]['spec']['length']}")
        if es:
            spec = es[0]["spec"]
            expect(f"{env} is written once, not refreshed",
                   spec.get("refreshPolicy") == "CreatedOnce",
                   f"got {spec.get('refreshPolicy')}")
            expect(f"{env}: no refresh interval reopens the generator",
                   str(spec.get("refreshInterval")) == "0",
                   f"got {spec.get('refreshInterval')}")
            data = spec["target"]["template"]["data"]
            expect(f"{env}'s Secret carries the key the pod reads, in the format it parses",
                   data.get(key) == value, f"got {data}")
            expect(f"an uninstall keeps {env}",
                   es[0]["metadata"].get("annotations", {}).get("helm.sh/resource-policy")
                   == "keep", f"got {es[0]['metadata'].get('annotations')}")
        for profile, cloud in CELLS:
            ref = secret_ref(hyperdx_env(golden("hyperdx", profile, cloud), env))
            expect(f"{profile}/{cloud}: hyperdx reads {env} from the minted Secret",
                   ref == (secret, key), f"got {ref}")


def test_hyperdx_keys_can_come_from_the_deployment() -> None:
    for env, secret, _, _, block in HYPERDX_KEYS:
        rendered = render_app("hyperdx", "slim", "local", overlay={block: {"create": False}})
        objects = [f"{d['kind']}/{d['metadata']['name']}" for d in rendered]
        for kind in ("Password", "ExternalSecret", "Secret"):
            name = f"{secret}-gen" if kind == "Password" else secret
            expect(f"{block}.create=false renders no {kind}",
                   f"{kind}/{name}" not in objects, f"got {objects}")
        ref = secret_ref(hyperdx_env(rendered, env))
        expect(f"the pod still reads the deployment's {env} Secret",
               ref[0] == secret, f"got {ref}")


def test_no_chart_mints_a_secret_behind_a_lookup() -> None:
    """The #224 shape, repo-wide: a cluster read cannot guard a render-time mint."""
    offenders = []
    for template in (t for tree in CHART_TREES for t in tree.glob("*/templates/**/*.yaml")):
        body = template.read_text(encoding="utf-8", errors="replace")
        code = "\n".join(
            line for line in body.splitlines() if "{{" in line or "{{-" in line
        )
        if MINTERS.search(code):
            offenders.append(str(template.relative_to(REPO_ROOT)))
    expect("no chart template mints a value at render time", offenders == [],
           f"got {offenders}")


def main() -> int:
    with standalone():
        test_engine_renders_identically_twice()
        test_jwt_key_is_not_minted_by_the_template()
        test_a_pinned_key_renders_that_key_and_nothing_else()
        test_creation_can_be_handed_to_the_deployment()
        test_the_admin_password_is_minted_once_like_the_signing_key()
        test_the_seed_accounts_secret_no_longer_claims_the_admin_password()
        test_the_engine_reads_the_minted_secret()
        test_a_non_dev_posture_cannot_render_without_minting()
        test_hyperdx_keys_are_minted_once_and_read_by_the_pod()
        test_hyperdx_keys_can_come_from_the_deployment()
        test_no_chart_mints_a_secret_behind_a_lookup()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
