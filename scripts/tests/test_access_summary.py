#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_access_summary.py
#  Purpose:      Prove the access summary reads its endpoints off the live
#                HTTPRoutes, reports the Reach the gateway actually granted
#                each one, and picks THIS deploy's Gateway.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Behaviour tests for bootstrap/access-summary.sh.

The summary is what an operator is handed after a deploy, so a wrong row sends
them to a URL that does not answer. Three ways it was wrong: hostnames were
composed here from hardcoded labels while envoy-gateway-config takes every
hostname from the overlay, so a renamed label reported an exposed route as
internal; the Reach column tested each route's hostname against the list of
hostnames it was itself taken from, so every row read "exposed" and a second
hostname was dropped; and the Gateway address came from `items[0]`, which is
whichever Gateway the API happened to list first (dfe-vpn ships a second).

A fake `kubectl` first on PATH answers every query from a JSON fixture.

    python3 scripts/tests/test_access_summary.py

No third-party deps and no test runner, matching the script it tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SUMMARY = REPO_ROOT / "bootstrap" / "access-summary.sh"

# Answers the summary's four read shapes off FAKE_KUBECTL_FIXTURE, emitting each
# listing with the separator its jsonpath uses.
FAKE_KUBECTL = """#!/usr/bin/env python3
import base64, json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8") as fh:
    fixture = json.load(fh)

if "httproute" in args:
    for row in fixture.get("httproutes") or []:
        print("|".join(row))
elif "gateway" in args:
    for row in fixture.get("gateways") or []:
        print("\\t".join(row))
elif "dfe-cluster" in args:
    path = [a for a in args if ".metadata.annotations" in a][0]
    key = path.rsplit("/", 1)[1].rstrip("}")
    sys.stdout.write((fixture.get("annotations") or {}).get(key, ""))
elif "dfe-engine-seed-accounts" in args:
    accts = fixture.get("seed_accounts")
    if accts is None:
        sys.exit(1)
    sys.stdout.write(base64.b64encode(json.dumps(accts).encode()).decode())
elif "deployment" in args:
    # `dfe-ops creds` reads the two engine Secret names off the live Deployment.
    env = fixture.get("engine_env")
    if env is None:
        sys.exit(1)
    json.dump(
        {"spec": {"template": {"spec": {"containers": [{"env": env}]}}}}, sys.stdout
    )
elif "secret" in args and "-o" in args and "name" in args:
    # `dfe-ops creds` presence probe: every secret it asks about is absent
    # unless the fixture lists it.
    name = args[args.index("secret") + 1]
    sys.exit(0 if name in (fixture.get("secrets") or []) else 1)
"""

DOMAIN = "example.com"


def route(
    name: str,
    *hosts: str,
    path: str = "",
    accepted: str = "True=Accepted",
    resolved: str = "True=ResolvedRefs",
) -> list[str]:
    """One HTTPRoute in the five fields the summary's jsonpath emits."""
    return [name, path, f"{accepted} ", f"{resolved} ", " ".join(hosts) + " "]


def engine_env(
    admin: tuple[str, str] = ("dfe-engine-admin", "admin-password"),
    breakglass: tuple[str, str] = ("dfe-engine-breakglass", "breakglass-password"),
) -> list[dict]:
    """The engine Deployment's auth env, as the chart writes it."""
    return [
        {
            "name": "DFE_AUTH_LOCAL_ADMIN_PASSWORD",
            "valueFrom": {"secretKeyRef": {"name": admin[0], "key": admin[1]}},
        },
        {"name": "DFE_AUTH_LOCAL_ADMIN_SECRET_NAME", "value": admin[0]},
        {"name": "DFE_AUTH_LOCAL_ADMIN_SECRET_KEY", "value": admin[1]},
        {
            "name": "DFE_AUTH_BREAKGLASS_PASSWORD",
            "valueFrom": {"secretKeyRef": {"name": breakglass[0], "key": breakglass[1]}},
        },
    ]


BASE_FIXTURE = {
    "annotations": {"domain": DOMAIN, "dfe_namespace": "dfe-local", "profile": "scale"},
    "gateways": [["dfe-gateway", "dfe-envoy", "192.0.2.10"]],
    "httproutes": [],
    "seed_accounts": None,
    "secrets": ["dfe-engine-admin", "dfe-engine-breakglass"],
    "engine_env": engine_env(),
}

_failures = 0


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


def run_summary(**overrides) -> str:
    """The rendered summary for one cluster reading."""
    fixture = dict(BASE_FIXTURE)
    fixture.update(overrides)
    with tempfile.TemporaryDirectory() as tmp:
        bindir = Path(tmp)
        kubectl = bindir / "kubectl"
        kubectl.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        kubectl.chmod(0o755)
        fixture_file = bindir / "fixture.json"
        fixture_file.write_text(json.dumps(fixture), encoding="utf-8", newline="\n")

        env = dict(os.environ)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["FAKE_KUBECTL_FIXTURE"] = str(fixture_file)
        out = subprocess.run(
            ["bash", str(SUMMARY), "", str(bindir / "dfe-access.md")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
        if out.returncode != 0:
            raise SystemExit(f"access-summary.sh failed:\n{out.stderr}")
        return out.stdout


def endpoints(text: str) -> list[str]:
    """The endpoints table's rows, which is the part composed from the cluster."""
    rows, seen = [], False
    for line in text.splitlines():
        if line.startswith("| What "):
            seen = True
            continue
        if seen and not line.startswith("|"):
            break
        if seen and not line.startswith("|---"):
            rows.append(line)
    return rows


def test_the_endpoints_come_from_the_live_routes() -> None:
    """A hostname is the overlay's to choose, so it is read, never composed.

    envoy-gateway-config takes every route hostname from `hostnames`; a summary
    that rebuilds `dfe.<domain>` reports a renamed label as internal and prints
    a URL that answers nothing.
    """
    text = run_summary(
        httproutes=[
            route("dfe-ui", f"console.{DOMAIN}", path="/"),
            route("dfe-engine", f"console.{DOMAIN}", path="/api/v1"),
            route("hyperdx", f"hyperdx.{DOMAIN}"),
        ]
    )
    rows = endpoints(text)
    expect(
        "the renamed host is listed, and exposed",
        f"| DFE UI (+ embedded HyperDX explore) | https://console.{DOMAIN} | exposed |" in rows,
        f"{rows}",
    )
    expect(
        "the route's own path reaches the URL",
        f"| DFE API (engine) | https://console.{DOMAIN}/api/v1 | exposed |" in rows,
        f"{rows}",
    )
    expect(
        "and the composed hostname appears nowhere",
        not any(f"dfe.{DOMAIN}" in row for row in rows),
        f"{rows}",
    )


def test_the_ingest_routes_are_listed_like_the_others() -> None:
    """Both carry their own HTTPRoute now, so neither is a hand-written note."""
    rows = endpoints(
        run_summary(
            httproutes=[
                route("receiver", f"receiver.{DOMAIN}"),
                route("otel", f"otel.{DOMAIN}"),
            ]
        )
    )
    expect(
        "the receiver route is a row",
        f"| Receiver ingest | https://receiver.{DOMAIN} | exposed |" in rows,
        f"{rows}",
    )
    expect(
        "the otel route is a row",
        f"| OTLP ingest | https://otel.{DOMAIN} | exposed |" in rows,
        f"{rows}",
    )


def test_a_route_added_later_still_gets_a_row() -> None:
    """The label map is a courtesy, so an unknown route falls back to its name."""
    rows = endpoints(run_summary(httproutes=[route("grafana", f"grafana.{DOMAIN}", path="/")]))
    expect(
        "an unmapped route is named after itself",
        f"| grafana | https://grafana.{DOMAIN} | exposed |" in rows,
        f"{rows}",
    )


def test_a_route_the_gateway_refused_is_not_exposed() -> None:
    """Reach was tested against the hostnames it was taken from, so it was a constant.

    A route the gateway did not accept answers nothing, and the operator needs
    the reason on the row rather than a URL that times out.
    """
    rows = endpoints(
        run_summary(
            httproutes=[
                route("dfe-ui", f"dfe.{DOMAIN}", accepted="False=NoMatchingListenerHostname"),
                route("hyperdx", f"hyperdx.{DOMAIN}"),
            ]
        )
    )
    expect(
        "the refused route reads not accepted, with the gateway's reason",
        f"| DFE UI (+ embedded HyperDX explore) | https://dfe.{DOMAIN} | "
        "not accepted (NoMatchingListenerHostname) |" in rows,
        f"{rows}",
    )
    expect(
        "and the accepted route beside it still reads exposed",
        f"| HyperDX | https://hyperdx.{DOMAIN} | exposed |" in rows,
        f"{rows}",
    )


def test_a_route_with_unresolvable_backends_is_not_exposed() -> None:
    """Accepted says the listener took the hostname, not that a backend exists."""
    rows = endpoints(
        run_summary(httproutes=[route("dfe-ui", f"dfe.{DOMAIN}", resolved="False=BackendNotFound")])
    )
    expect(
        "an accepted route with no backend reads refs unresolved",
        f"| DFE UI (+ embedded HyperDX explore) | https://dfe.{DOMAIN} | "
        "refs unresolved (BackendNotFound) |" in rows,
        f"{rows}",
    )


def test_a_route_with_no_status_yet_is_not_exposed() -> None:
    """A route the gateway has not reached is not a reachable URL."""
    rows = endpoints(
        run_summary(httproutes=[route("dfe-ui", f"dfe.{DOMAIN}", accepted="", resolved="")])
    )
    expect(
        "an unprogrammed route says so",
        f"| DFE UI (+ embedded HyperDX explore) | https://dfe.{DOMAIN} | "
        "not accepted (no gateway status) |" in rows,
        f"{rows}",
    )


def test_every_hostname_of_a_route_gets_a_row() -> None:
    """hostnames is a list; taking [0] hides every alias the overlay declares."""
    rows = endpoints(
        run_summary(httproutes=[route("dfe-ui", f"dfe.{DOMAIN}", f"console.{DOMAIN}", path="/")])
    )
    expect(
        "the two-hostname route yields two rows",
        len(rows) == 2,
        f"{rows}",
    )
    expect(
        "and the second hostname is one of them",
        any(f"https://console.{DOMAIN} " in row for row in rows),
        f"{rows}",
    )


def test_no_routes_falls_back_to_the_composed_rows() -> None:
    """With no HTTPRoutes nothing is exposed, and the reader still gets the list."""
    rows = endpoints(run_summary(httproutes=[]))
    expect(
        "every fallback row reads internal",
        rows and all("internal (port-forward)" in row for row in rows),
        f"{rows}",
    )
    expect(
        "the fallback still names the UI",
        any(f"https://dfe.{DOMAIN} " in row for row in rows),
        f"{rows}",
    )


def test_the_gateway_is_chosen_by_name() -> None:
    """items[0] is whichever Gateway the API listed first; dfe-vpn ships a second."""
    text = run_summary(
        gateways=[
            ["dfe-vpn-gateway", "dfe-vpn", "192.0.2.99"],
            ["dfe-gateway", "dfe-envoy", "192.0.2.10"],
        ]
    )
    expect(
        "the deploy's own Gateway address is printed",
        "192.0.2.10" in text and "192.0.2.99" not in text,
        text.splitlines()[4] if len(text.splitlines()) > 4 else text,
    )


def test_the_gateway_falls_back_to_its_class() -> None:
    """A deployment may rename the Gateway; the GatewayClass is the other handle."""
    text = run_summary(
        gateways=[
            ["dfe-vpn-gateway", "dfe-vpn", "192.0.2.99"],
            ["edge", "dfe-envoy", "192.0.2.10"],
        ]
    )
    expect(
        "the Gateway on the deploy's class is printed",
        "192.0.2.10" in text and "192.0.2.99" not in text,
        text,
    )


def test_the_seed_logins_are_still_listed() -> None:
    """The rest of the summary has to survive the endpoints rework."""
    text = run_summary(seed_accounts=[{"username": "kaz", "groups": ["dfe_admin"]}])
    expect(
        "a configured seed account is printed with its groups",
        "- `kaz`  groups=[dfe_admin]" in text,
        text,
    )
    expect(
        "and none configured says so",
        "(none configured)" in run_summary(),
        "",
    )


def test_the_credential_block_names_the_minted_secrets() -> None:
    """The summary carries the fetch lines `dfe-ops creds` owns (#233).

    A hand-written copy here named `dfe-engine-admin` key `password` and the
    seed-accounts `admin-password` at the same time, and both were wrong once the
    deploy started minting.
    """
    text = run_summary()
    expect(
        "the minted admin secret is the one fetched",
        "get secret dfe-engine-admin -o jsonpath='{.data.admin-password}'" in text,
        text,
    )
    expect(
        "the break-glass secret is fetched too",
        "get secret dfe-engine-breakglass -o jsonpath='{.data.breakglass-password}'" in text,
        text,
    )
    expect(
        "and the admin password is no longer read out of the seed-accounts Secret",
        "dfe-engine-seed-accounts -o jsonpath='{.data.admin-password}'" not in text,
        text,
    )


def test_a_renamed_secret_is_fetched_by_the_name_the_engine_reads() -> None:
    """Both names are helm values, so hardcoding them prints a wrong command.

    A renamed Secret was printed under the chart default and marked "not on this
    cluster", which reads as break-glass turned off rather than as a rename.
    """
    admin = ("acme-dfe-admin", "password")
    breakglass = ("acme-dfe-breakglass", "password")
    text = run_summary(
        engine_env=engine_env(admin, breakglass),
        secrets=[admin[0], breakglass[0]],
    )
    expect(
        "the renamed admin Secret and key are the ones fetched",
        f"get secret {admin[0]} -o jsonpath='{{.data.{admin[1]}}}'" in text,
        text,
    )
    expect(
        "the renamed break-glass Secret is fetched too",
        f"get secret {breakglass[0]} -o jsonpath='{{.data.{breakglass[1]}}}'" in text,
        text,
    )
    expect(
        "and neither engine row is marked absent, since both are on the cluster",
        "# DFE admin (user `admin`)\n" in text
        and "# DFE break-glass (user `breakglass`)\n" in text,
        text,
    )
    expect(
        "and the chart defaults appear nowhere",
        "dfe-engine-admin" not in text and "dfe-engine-breakglass" not in text,
        text,
    )


def test_an_unreadable_engine_falls_back_to_the_chart_defaults() -> None:
    """A deploy still converging has no Deployment to read, and still needs a block."""
    text = run_summary(engine_env=None)
    expect(
        "the chart-default admin Secret is fetched",
        "get secret dfe-engine-admin -o jsonpath='{.data.admin-password}'" in text,
        text,
    )


def test_a_credential_the_cluster_lacks_is_marked_not_dropped() -> None:
    """A deploy that turned break-glass off has to read as off, not as missing."""
    text = run_summary(secrets=["dfe-engine-admin"])
    expect(
        "the absent secret is still listed, and marked",
        "# DFE break-glass (user `breakglass`)   [not on this cluster]" in text,
        text,
    )
    expect(
        "and the present one is not marked",
        "# DFE admin (user `admin`)\n" in text,
        text,
    )


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'FAILED' if _failures else 'ALL PASSED'} -- {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
