#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         argocd_login.py
#  Purpose:      Point the login of the Argo CD bootstrap.sh installs at the OIDC
#                provider the gateway already fronts Argo with, map DFE's groups
#                to Argo roles, and report which login Argo offers.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Argo CD's own login, from the deployment's identity.

    python3 bootstrap/argocd_login.py values --domain slim.dfe.example.com
    python3 bootstrap/argocd_login.py summary

`values` prints the helm values bootstrap.sh layers onto its own Argo install,
as JSON (which helm reads as YAML), and one line on stderr naming the login they
give. `summary` reads the live argocd-cm and prints the Markdown the access
summary carries.

The provider comes off the cluster, from the gateway's infra SecurityPolicy on
the argocd route. oidc.providers live in the deploy repo's overlay, which
bootstrap cannot read, and that policy is what the gateway renders from them. So
Argo and the edge in front of it name one issuer and one client, and Argo's
client secret is a reference to the Secret the policy already names. Argo
resolves `$<secret>:<key>` only from a Secret in its own namespace labelled
app.kubernetes.io/part-of=argocd, which the gateway chart puts on the
argocd-namespace copy.

With no such policy -- no provider, the infra routes off, or the gateway not
synced yet -- Argo keeps its local admin and Dex stays as the chart ships it.
"""

import argparse
import base64
import json
import subprocess
import sys
from urllib.parse import urlsplit

NAMESPACE = "argocd"
# templates/security-policy-infra.yaml names each route's policy
# dfe-oidc-<routeName>-admin, and routes.argocd.routeName is argocd.
POLICY = "dfe-oidc-argocd-admin"
POLICY_RESOURCE = "securitypolicies.gateway.envoyproxy.io"
# The key Envoy Gateway reads an OIDC client credential from, and no other.
CLIENT_KEY = "client-secret"
# The wildcard listener's certificate (templates/gateway.yaml), in gateway.namespace.
GATEWAY_NAMESPACE = "envoy-gateway-system"
EDGE_TLS = "dfe-wildcard-tls"
# Scopes the gateway chart requests when a provider names none.
DEFAULT_SCOPES = ["openid", "email", "profile", "groups"]
# dfe-engine's vocabulary (docs/control-plane/rbac-vocabulary.md): the admin group
# it seeds, and the canonical infrastructure read-only group.
ADMIN_GROUP = "dfe-admins"
VIEWER_GROUP = "dfe-infra-viewers"
GROUP_ROLES = ((ADMIN_GROUP, "role:admin"), (VIEWER_GROUP, "role:readonly"))


def policy_csv() -> str:
    """argocd-rbac-cm's policy.csv: each DFE group bound to its built-in Argo role."""
    return "".join(f"g, {group}, {role}\n" for group, role in GROUP_ROLES)


def served_by_gateway(issuer: str, domain: str) -> bool:
    """Whether the issuer's host is one the wildcard listener `*.<domain>` answers."""
    host = (urlsplit(issuer).hostname or "").lower()
    label, _, parent = host.partition(".")
    return bool(domain) and bool(label) and parent == domain.lower()


def oidc_config(policy: dict | None, domain: str, edge_ca: str) -> tuple[dict | None, str]:
    """(Argo's oidc.config, why) from the gateway's policy on the argocd route.

    Args:
        policy: The SecurityPolicy, or None when the cluster carries none.
        domain: The deployment's domain, which the gateway's wildcard serves.
        edge_ca: The CA that signed the gateway's certificate, PEM; empty when unknown.

    Returns:
        The config, or None when Argo keeps its local admin, and the one-line reason.
    """
    local = "so Argo keeps its local admin"
    if policy is None:
        return None, f"no {POLICY} policy in {NAMESPACE}: no OIDC provider fronts Argo, {local}"
    spec = policy.get("spec") or {}
    oidc = spec.get("oidc") or {}
    issuer = (oidc.get("provider") or {}).get("issuer", "")
    client_id = oidc.get("clientID", "")
    # A reference to the Secret holding the client credential, never the value.
    ref = oidc.get("clientSecret") or {}
    ref_name = ref.get("name", "")
    ref_namespace = ref.get("namespace") or (policy.get("metadata") or {}).get("namespace", "")
    if not (issuer and client_id and ref_name):
        return None, f"{POLICY} names no issuer, client id and credential Secret, {local}"
    if ref_namespace != NAMESPACE:
        return None, (
            f"{POLICY} reads its client credential from namespace {ref_namespace}, and Argo "
            f"resolves a Secret reference only in {NAMESPACE}, {local}"
        )
    # The login provider is also one of the policy's JWT issuers, under its own name.
    issuers = (spec.get("jwt") or {}).get("providers") or []
    names = [p["name"] for p in issuers if p.get("issuer") == issuer and p.get("name")]
    config = {
        "name": names[0] if names else "SSO",
        "issuer": issuer,
        "clientID": client_id,
        "clientSecret": f"${ref_name}:{CLIENT_KEY}",
        "requestedScopes": oidc.get("scopes") or DEFAULT_SCOPES,
    }
    # rootCA REPLACES Argo's system trust, so it is set only for an issuer the
    # gateway itself serves; a public IdP keeps the system roots.
    if edge_ca and served_by_gateway(issuer, domain):
        config["rootCA"] = edge_ca
    return config, f"Argo signs in through {issuer} as client {client_id}, the same as its edge"


def helm_values(config: dict | None) -> dict:
    """The values bootstrap.sh layers onto its Argo install for this login.

    The group mapping is set either way: without a provider no groups claim
    arrives and it grants nothing, and with one it is already in place.
    """
    values: dict = {"configs": {"rbac": {"policy.csv": policy_csv()}}}
    if config is not None:
        values["configs"]["cm"] = {"oidc.config": json.dumps(config, indent=2) + "\n"}
        values["dex"] = {"enabled": False}
    return values


def configured_issuer(raw: str) -> str:
    """The issuer an oidc.config names: the JSON this module writes, or plain YAML."""
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return str(parsed.get("issuer", ""))
    issuers = [line.split(":", 1)[1] for line in raw.splitlines() if line.startswith("issuer:")]
    return issuers[0].strip() if issuers else ""


def summary(cm: dict | None, error: str = "") -> str:
    """The access summary's Argo CD login block, from the live argocd-cm."""
    if cm is None:
        reason = error or "no reason given"
        return f"- Argo CD login: unknown, argocd-cm could not be read ({reason}).\n"
    issuer = configured_issuer((cm.get("data") or {}).get("oidc.config", ""))
    if not issuer:
        return (
            "- Argo CD has no OIDC provider, so it keeps its local `admin` login. Its password\n"
            "  is the `Argo CD initial admin` entry in the credentials block above.\n"
        )
    return (
        f"- Argo CD signs in through {issuer}, the provider the gateway fronts it with.\n"
        f"  `{ADMIN_GROUP}` gets `role:admin` and `{VIEWER_GROUP}` gets `role:readonly`;\n"
        "  any other group signs in and sees nothing. The IdP client must allow the\n"
        "  redirect URI `<Argo CD URL>/auth/callback`. The local `admin` stays as recovery.\n"
    )


def _kubectl(argv: list[str], kube: list[str]) -> tuple[int, str, str]:
    """(rc, stdout, last stderr line) for one read-only kubectl call."""
    try:
        done = subprocess.run(
            ["kubectl", *kube, *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "kubectl is not on PATH"
    lines = done.stderr.strip().splitlines()
    return done.returncode, done.stdout, lines[-1] if lines else ""


def read_policy(kube: list[str]) -> tuple[dict | None, str]:
    """(the argocd route's SecurityPolicy or None, why it is None)."""
    rc, out, err = _kubectl(
        ["-n", NAMESPACE, "get", POLICY_RESOURCE, POLICY, "-o", "json", "--ignore-not-found"], kube
    )
    if rc != 0:
        reason = err or f"kubectl exited {rc}"
        return None, f"cannot read {POLICY_RESOURCE} in {NAMESPACE}: {reason}"
    if not out.strip():
        return None, ""
    try:
        return json.loads(out), ""
    except json.JSONDecodeError as exc:
        return None, f"kubectl printed no JSON for {POLICY}: {exc}"


def read_edge_ca(kube: list[str]) -> str:
    """The CA of the gateway's wildcard certificate, PEM; empty when absent."""
    ca_field = "jsonpath={.data.ca\\.crt}"
    rc, out, _ = _kubectl(
        ["-n", GATEWAY_NAMESPACE, "get", "secret", EDGE_TLS, "-o", ca_field], kube
    )
    if rc != 0 or not out.strip():
        return ""
    try:
        return base64.b64decode(out.strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def _kube_args(args: argparse.Namespace) -> list[str]:
    kube: list[str] = []
    if args.kubeconfig:
        kube += ["--kubeconfig", args.kubeconfig]
    if args.context:
        kube += ["--context", args.context]
    return kube


def cmd_values(args: argparse.Namespace) -> int:
    kube = _kube_args(args)
    policy, error = read_policy(kube)
    edge_ca = read_edge_ca(kube) if policy is not None else ""
    config, why = oidc_config(policy, args.domain, edge_ca)
    if error:
        why = f"{error}; {why}"
    # codeql[py/clear-text-logging-sensitive-data] names a Secret, never prints its value
    print(f"  [argocd] {why}", file=sys.stderr)
    # codeql[py/clear-text-logging-sensitive-data] a $<secret>:<key> reference, not the value
    print(json.dumps(helm_values(config), indent=2, sort_keys=True))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    argv = ["-n", NAMESPACE, "get", "configmap", "argocd-cm", "-o", "json"]
    rc, out, err = _kubectl(argv, _kube_args(args))
    if rc != 0:
        print(summary(None, err or f"kubectl exited {rc}"), end="")
        return 0
    try:
        cm = json.loads(out)
    except json.JSONDecodeError as exc:
        print(summary(None, str(exc)), end="")
        return 0
    print(summary(cm), end="")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kubeconfig", default="", help="kubeconfig kubectl reads")
    parser.add_argument("--context", default="", help="kubeconfig context kubectl reads")
    sub = parser.add_subparsers(dest="action", required=True, metavar="<action>")
    values = sub.add_parser("values", help="print the helm values for bootstrap.sh's Argo install")
    values.add_argument("--domain", required=True, help="the deployment's domain (DFE_DOMAIN)")
    values.set_defaults(func=cmd_values)
    report = sub.add_parser("summary", help="print the access summary's Argo CD login block")
    report.set_defaults(func=cmd_summary)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
