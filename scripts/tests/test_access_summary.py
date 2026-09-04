#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_access_summary.py
#  Purpose:      Prove the access summary reads its endpoints off the live
#                HTTPRoutes and picks THIS deploy's Gateway, rather than
#                composing hostnames from labels and taking items[0].
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Behaviour tests for bootstrap/access-summary.sh.

The summary is what an operator is handed after a deploy, so a wrong row sends
them to a URL that does not answer. Two ways it was wrong: hostnames were
composed here from hardcoded labels while envoy-gateway-config takes every
hostname from the overlay, so a renamed label reported an exposed route as
internal; and the Gateway address came from `items[0]`, which is whichever
Gateway the API happened to list first (dfe-vpn ships a second).

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

# Answers the summary's four read shapes off FAKE_KUBECTL_FIXTURE. The two
# tab-separated listings are emitted the way the jsonpath ranges do.
FAKE_KUBECTL = """#!/usr/bin/env python3
import base64, json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8") as fh:
    fixture = json.load(fh)

if "httproute" in args:
    for row in fixture.get("httproutes") or []:
        print("\\t".join(row))
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
"""

DOMAIN = "example.com"
BASE_FIXTURE = {
    "annotations": {"domain": DOMAIN, "dfe_namespace": "dfe-local", "profile": "scale"},
    "gateways": [["dfe-gateway", "dfe-envoy", "192.0.2.10"]],
    "httproutes": [],
    "seed_accounts": None,
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
            ["dfe-ui", f"console.{DOMAIN}", "/"],
            ["dfe-engine", f"console.{DOMAIN}", "/api/v1"],
            ["hyperdx", f"hyperdx.{DOMAIN}", ""],
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
                ["receiver", f"receiver.{DOMAIN}", ""],
                ["otel", f"otel.{DOMAIN}", ""],
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
    rows = endpoints(run_summary(httproutes=[["grafana", f"grafana.{DOMAIN}", "/"]]))
    expect(
        "an unmapped route is named after itself",
        f"| grafana | https://grafana.{DOMAIN} | exposed |" in rows,
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


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'FAILED' if _failures else 'ALL PASSED'} -- {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
