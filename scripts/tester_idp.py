#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         tester_idp.py
#  Purpose:      Stand a throwaway OIDC identity provider (Dex + a glauth LDAP
#                directory) behind a deployment's own gateway, so external-OIDC
#                login can be exercised without borrowing a shared IdP.
#                Registered on dfe-ops as the `idp` subcommand.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A tester IdP for a DFE deployment: `dfe-ops idp deploy|teardown|status`.

Proving external OIDC needs an IdP that will issue tokens to a throwaway
deployment. Pointing at a shared corporate one does not work: its redirect URIs
have to be edited for every new hostname, and restarting it to add a connector
takes down everyone else's logins. So a deployment brings its own.

Dex is the OIDC surface and glauth is the directory behind it. Dex's builtin
static password database emits no `groups` claim, and the group claim is the
whole point -- it is what the engine maps to roles -- so the users live in
glauth and dex reads them over LDAP.

Every environment-specific value is a flag: the tool has no idea which cluster,
domain, gateway or user set it is being pointed at.

    python3 scripts/dfe-ops idp deploy \\
        --kubeconfig .tmp/dfe.kubeconfig \\
        --domain slim.dfe.example.com \\
        --gateway envoy-gateway-system/dfe-gateway \\
        --base-domain dfe.example.com \\
        --secrets-out .tmp/tester-idp.env

--base-domain registers a redirect URI per deploy profile, so the client keeps
working when the cluster is rebuilt in another mode; --redirect-uri (repeatable)
replaces the derived set outright.

    python3 scripts/dfe-ops idp status --kubeconfig .tmp/dfe.kubeconfig \\
        --domain dfe.example.com
    python3 scripts/dfe-ops idp teardown --kubeconfig .tmp/dfe.kubeconfig

Passwords and the client secret are GENERATED per deploy and written to
--secrets-out with mode 0600. They are never printed, never passed on argv, and
never committed -- the tool prints the issuer, the client id and the file path,
which is everything a caller needs to look the rest up.

Rendered manifests are emitted as JSON. JSON is valid YAML, both kubectl and
helm accept it, and it removes a hand-written YAML templating step whose
quoting bugs would show up as a crash-looping IdP rather than a parse error.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import string
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import private_file
from profiles import MODES

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_USERS_FILE = REPO_ROOT / "bootstrap" / "fixtures" / "tester-idp-users.toml"
DEFAULT_GROUPS_FILE = REPO_ROOT / "bootstrap" / "fixtures" / "tester-idp-groups.toml"

# --- pins --------------------------------------------------------------------
# The tester IdP is a TEST FIXTURE, not a stack component, so its pins live here
# rather than in versions.yaml (which the drift checkers and the appsets read as
# the set of things a deploy installs). Digests verified against the registries
# on 2026-09-05; chart 0.24.1 ships appVersion 2.44.0, so the chart version and
# the image digest move together.
DEX_CHART_REPO = "https://charts.dexidp.io"
DEX_CHART = "dex/dex"
DEX_CHART_VERSION = "0.24.1"
DEX_IMAGE_REPOSITORY = "ghcr.io/dexidp/dex"
DEX_IMAGE_TAG = "v2.44.0"
DEX_IMAGE_DIGEST = "sha256:5d0656fce7d453c0e3b2706abf40c0d0ce5b371fb0b73b3cf714d05f35fa5f86"
GLAUTH_IMAGE = (
    "glauth/glauth:v2.5.2@sha256:"
    "93b1fffc865ce137ff7630a573ae413b85c45568cd63d2860971fe5ed121e0f8"
)

DEFAULT_NAMESPACE = "dex"
DEFAULT_GATEWAY = "envoy-gateway-system/dfe-gateway"
DEFAULT_CLIENT_ID = "dfe-engine"
DEFAULT_BASE_DN = "dc=dfe,dc=test"
DEFAULT_RELEASE = "dex"
# Host label prepended to --domain when --hostname is not given. `auth` is the
# canonical role name for the external OIDC issuer in the hostnames SSoT, but a
# tester IdP is deliberately named for what it is.
DEFAULT_HOST_LABEL = "dex"
# Provider name the engine registers this IdP under; it is a path segment of the
# OIDC callback, so the redirect URIs and `wire-engine` must agree on it.
DEFAULT_PROVIDER = "dex"
# Host label the engine and the UI are published on (the hostnames SSoT's `dfe`).
APP_HOST_LABEL = "dfe"
# Deploy profiles a cluster is rebuilt through, each with its own domain tag --
# every mode in the one table, so a new mode registers its callback too.
DEFAULT_PROFILES = MODES

GLAUTH_LDAP_PORT = 3893
DEX_HTTP_PORT = 5556

# glauth's config backend refuses an anonymous search (LDAP result 50), so dex
# binds as a service account. It sits outside the fixture's uid/gid range so a
# user file can never collide with it.
SEARCH_USER = "search"
SEARCH_UID = 6999
SEARCH_GID = 5998
SEARCH_GROUP = "svcaccts"

# The ConfigMap name the engine chart's authConfig.groupsConfigMap is pointed at.
DEFAULT_GROUPS_CONFIGMAP = "dfe-auth-groups"
# The engine stores a group as <name>.yaml, so its name must be a safe file stem.
_ENGINE_GROUP_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")
_ORG_SCOPE_PREFIX = "org:"

_SECRET_ALPHABET = string.ascii_letters + string.digits


# --- secret material ---------------------------------------------------------
def generate_password(length: int = 24) -> str:
    """A fresh alphanumeric password. Alphanumeric so it survives a URL, an LDAP
    bind string and a shell without quoting rules mattering."""
    return "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(length))


def sha256_hex(value: str) -> str:
    """glauth stores passwords as the hex sha256 of the plaintext."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# --- render: the glauth directory --------------------------------------------
def render_search_account(password_hash: str) -> str:
    """The dex bind account, appended to whatever user file the caller supplied.

    It carries an explicit search capability because glauth grants none by
    default, and a dex that cannot search returns an empty directory rather
    than an error.
    """
    return (
        "\n"
        "[[users]]\n"
        f'  name = "{SEARCH_USER}"\n'
        f"  uidnumber = {SEARCH_UID}\n"
        f"  primarygroup = {SEARCH_GID}\n"
        f'  passsha256 = "{password_hash}"\n'
        "    [[users.capabilities]]\n"
        '      action = "search"\n'
        '      object = "*"\n'
        "\n"
        "[[groups]]\n"
        f'  name = "{SEARCH_GROUP}"\n'
        f"  gidnumber = {SEARCH_GID}\n"
    )


def render_glauth_config(
    users_toml: str,
    *,
    fixture_password: str,
    search_password: str,
    base_dn: str = DEFAULT_BASE_DN,
) -> str:
    """Build the full glauth.cfg: preamble, the caller's users, the bind account.

    LDAPS is off because dex reaches glauth over the pod network inside one
    namespace and never leaves it; TLS is terminated at the gateway, on the
    OIDC surface that is actually exposed.
    """
    body = users_toml.replace("{{DFE_FIXTURE_PASS_SHA256}}", sha256_hex(fixture_password))
    config = (
        "# GENERATED by dfe-ops idp -- do not edit in the cluster.\n"
        "[ldap]\n"
        "  enabled = true\n"
        f'  listen = "0.0.0.0:{GLAUTH_LDAP_PORT}"\n'
        "\n"
        "[ldaps]\n"
        "  enabled = false\n"
        "\n"
        "[backend]\n"
        '  datastore = "config"\n'
        f'  baseDN = "{base_dn}"\n'
        '  nameformat = "cn"\n'
        '  groupformat = "ou"\n'
        "\n"
        "[behaviors]\n"
        "  IgnoreCapabilities = false\n"
        "\n"
    ) + body
    return config + render_search_account(sha256_hex(search_password))


def validate_users_toml(users_toml: str) -> tuple[list[str], list[str]]:
    """Parse the user file and return (user names, group names).

    A malformed directory is otherwise only discovered as a crash-looping
    glauth, so it is parsed here before anything reaches the cluster. Returns
    the names so the caller can report what it is about to deploy without
    touching a password.
    """
    parsed = tomllib.loads(users_toml.replace("{{DFE_FIXTURE_PASS_SHA256}}", "x" * 64))
    users = [u["name"] for u in parsed.get("users", [])]
    groups = [g["name"] for g in parsed.get("groups", [])]
    if not users:
        raise ValueError("user file declares no [[users]]")
    if not groups:
        raise ValueError("user file declares no [[groups]]")
    gids = {g["gidnumber"] for g in parsed.get("groups", [])}
    for user in parsed.get("users", []):
        wanted = {user.get("primarygroup"), *user.get("othergroups", [])} - {None}
        missing = wanted - gids
        if missing:
            raise ValueError(f"user {user['name']!r} references undeclared gid(s) {sorted(missing)}")
    return users, groups


# --- render: the engine's group -> role files --------------------------------
def render_engine_groups(groups_toml: str) -> dict[str, str]:
    """One engine group file per [[groups]] entry, keyed `<name>.yaml`.

    The engine resolves a groups claim by NAME against these files, so a group
    the IdP emits with no file here grants nothing. A malformed entry is refused
    before anything reaches the cluster, with the same name and scope rules the
    engine's group model applies. Each value is JSON, which the engine's YAML
    loader reads unchanged.
    """
    entries = tomllib.loads(groups_toml).get("groups", [])
    if not entries:
        raise ValueError("groups file declares no [[groups]]")
    files: dict[str, str] = {}
    for entry in entries:
        name = entry.get("name", "")
        if not isinstance(name, str) or not _ENGINE_GROUP_NAME.match(name):
            raise ValueError(f"{name!r} is not a valid engine group name")
        key = f"{name}.yaml"
        if key in files:
            raise ValueError(f"group {name!r} is declared twice")
        scope = entry.get("scope", "system")
        org_scoped = isinstance(scope, str) and scope.startswith(_ORG_SCOPE_PREFIX)
        if scope != "system" and not (org_scoped and scope[len(_ORG_SCOPE_PREFIX):].strip()):
            raise ValueError(f"group {name!r}: scope must be 'system' or 'org:<id>', got {scope!r}")
        body = {"description": entry.get("description", ""), "scope": scope}
        for field in ("roles", "org_ids"):
            values = entry.get(field, [])
            if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
                raise ValueError(f"group {name!r}: {field} must be a list of names")
            body[field] = values
        files[key] = json.dumps(body, indent=2) + "\n"
    return files


def render_glauth_manifests(namespace: str, *, image: str = GLAUTH_IMAGE) -> list[dict]:
    """Deployment + Service for glauth. The config arrives as a projected Secret.

    One replica on purpose: the directory is a file, so a second replica would
    only be a second copy of the same static answer.
    """
    labels = {"app.kubernetes.io/name": "glauth", "app.kubernetes.io/part-of": "dfe-tester-idp"}
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "glauth", "namespace": namespace, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000},
                        "containers": [
                            {
                                "name": "glauth",
                                "image": image,
                                "ports": [
                                    {"name": "ldap", "containerPort": GLAUTH_LDAP_PORT},
                                ],
                                "readinessProbe": {
                                    "tcpSocket": {"port": GLAUTH_LDAP_PORT},
                                    "initialDelaySeconds": 2,
                                    "periodSeconds": 5,
                                },
                                "volumeMounts": [
                                    {
                                        "name": "config",
                                        "mountPath": "/app/config",
                                        "readOnly": True,
                                    }
                                ],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": {"cpu": "20m", "memory": "32Mi"},
                                    "limits": {"memory": "128Mi"},
                                },
                            }
                        ],
                        "volumes": [
                            {
                                "name": "config",
                                "secret": {
                                    "secretName": "glauth-config",
                                    # The image's start script reads config.cfg
                                    # by that name, so the projection renames it
                                    # rather than the container taking a -c flag.
                                    "items": [{"key": "glauth.cfg", "path": "config.cfg"}],
                                },
                            }
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "glauth", "namespace": namespace, "labels": labels},
            "spec": {
                "selector": labels,
                "ports": [
                    {"name": "ldap", "port": GLAUTH_LDAP_PORT, "targetPort": GLAUTH_LDAP_PORT}
                ],
            },
        },
    ]


# --- render: dex -------------------------------------------------------------
def render_dex_values(
    *,
    issuer: str,
    namespace: str,
    client_id: str = DEFAULT_CLIENT_ID,
    redirect_uris: list[str],
    base_dn: str = DEFAULT_BASE_DN,
    image_repository: str = DEX_IMAGE_REPOSITORY,
    image_tag: str = DEX_IMAGE_TAG,
    image_digest: str = DEX_IMAGE_DIGEST,
) -> dict:
    """Helm values for the dex chart.

    skipApprovalScreen is on because every consumer of this IdP is a headless
    test: a consent page nobody clicks is an indefinite hang, not a prompt.
    The client secret and the LDAP bind password arrive as env vars from a
    Secret, so neither is in the values file or in helm's release history.
    """
    if not redirect_uris:
        raise ValueError("at least one --redirect-uri is required")
    return {
        "image": {
            "repository": image_repository,
            "tag": image_tag,
            "digest": image_digest,
        },
        "config": {
            "issuer": issuer,
            "storage": {"type": "kubernetes", "config": {"inCluster": True}},
            "web": {"http": f"0.0.0.0:{DEX_HTTP_PORT}"},
            "oauth2": {"skipApprovalScreen": True},
            # The directory is glauth. The static password DB cannot emit a
            # groups claim, which is the claim under test, so it stays off.
            "enablePasswordDB": False,
            "connectors": [
                {
                    "type": "ldap",
                    "id": "ldap",
                    "name": "LDAP",
                    "config": {
                        "host": f"glauth.{namespace}.svc.cluster.local:{GLAUTH_LDAP_PORT}",
                        "insecureNoSSL": True,
                        "bindDN": f"cn={SEARCH_USER},{base_dn}",
                        "bindPW": "$GLAUTH_SEARCH_PW",
                        "userSearch": {
                            "baseDN": base_dn,
                            "username": "cn",
                            "idAttr": "uidNumber",
                            "emailAttr": "mail",
                            "nameAttr": "cn",
                        },
                        # glauth emits groupOfUniqueNames carrying full user
                        # DNs, and puts the group name in ou rather than cn.
                        "groupSearch": {
                            "baseDN": f"ou=groups,{base_dn}",
                            "filter": "(objectClass=groupOfUniqueNames)",
                            "userMatchers": [{"userAttr": "DN", "groupAttr": "uniqueMember"}],
                            "nameAttr": "ou",
                        },
                    },
                }
            ],
            "staticClients": [
                {
                    "id": client_id,
                    "name": "DFE Engine",
                    "secretEnv": "DEX_CLIENT_SECRET",
                    "redirectURIs": list(redirect_uris),
                }
            ],
        },
        "envVars": [
            {
                "name": "DEX_CLIENT_SECRET",
                "valueFrom": {
                    "secretKeyRef": {"name": "dex-secrets", "key": "client-secret"}
                },
            },
            {
                "name": "GLAUTH_SEARCH_PW",
                "valueFrom": {
                    "secretKeyRef": {"name": "dex-secrets", "key": "search-password"}
                },
            },
        ],
        "resources": {
            "requests": {"cpu": "50m", "memory": "128Mi"},
            "limits": {"memory": "256Mi"},
        },
    }


def render_httproute(
    *,
    namespace: str,
    hostname: str,
    gateway: str = DEFAULT_GATEWAY,
    service: str = DEFAULT_RELEASE,
    port: int = DEX_HTTP_PORT,
) -> dict:
    """The route that puts dex on the deployment's existing gateway.

    The FIXTURE owns this route, not the gateway chart: the tester IdP has to
    be able to appear and disappear without a deploy-repo change, and a route
    the gateway chart owned would be reverted by the next Argo sync.

    No sectionName -- it attaches to every listener whose hostname matches, so
    the same route works on a gateway that terminates TLS on one listener and
    on one that has separate http/https listeners.
    """
    gw_namespace, _, gw_name = gateway.partition("/")
    if not gw_name:
        raise ValueError(f"--gateway must be namespace/name, got {gateway!r}")
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {
            "name": "dex",
            "namespace": namespace,
            "labels": {"app.kubernetes.io/part-of": "dfe-tester-idp"},
        },
        "spec": {
            "parentRefs": [{"name": gw_name, "namespace": gw_namespace}],
            "hostnames": [hostname],
            "rules": [{"backendRefs": [{"name": service, "port": port}]}],
        },
    }


def _redacted(objects: list[dict]) -> list[dict]:
    """Copy of `objects` with every Secret value replaced.

    A --dry-run is read by a human, pasted into a terminal someone is watching,
    and captured in CI logs, so a Secret printed in full is a credential
    published. Only the KEYS are shown -- which is what a dry run is for.
    """
    out = []
    for obj in objects:
        if obj.get("kind") != "Secret":
            out.append(obj)
            continue
        copy = {k: v for k, v in obj.items() if k != "stringData"}
        copy["stringData"] = dict.fromkeys(obj.get("stringData", {}), "<redacted>")
        out.append(copy)
    return out


def render_secret(name: str, namespace: str, data: dict[str, str]) -> dict:
    """An Opaque Secret from plaintext values (kubectl encodes stringData)."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/part-of": "dfe-tester-idp"},
        },
        "type": "Opaque",
        "stringData": data,
    }


def default_redirect_uris(
    *,
    hostname: str,
    host_label: str,
    base_domain: str = "",
    profiles: list[str] | tuple[str, ...] = DEFAULT_PROFILES,
    provider: str = DEFAULT_PROVIDER,
) -> list[str]:
    """The engine callbacks to register when the caller named none.

    A base domain registers one per profile, so the same client still works after
    the cluster is rebuilt in another mode on <profile>.<base> hostnames.
    """
    path = f"/api/v1/auth/oidc/{provider}/callback"
    if base_domain:
        return [
            f"https://{APP_HOST_LABEL}.{profile}.{base_domain}{path}"
            for profile in profiles or DEFAULT_PROFILES
        ]
    return [f"https://{hostname.replace(host_label, APP_HOST_LABEL, 1)}{path}"]


def resolve_hostname(args: argparse.Namespace) -> str:
    """--hostname wins; otherwise <label>.<domain>."""
    if args.hostname:
        return args.hostname
    if not args.domain:
        raise ValueError("one of --hostname or --domain is required")
    return f"{args.host_label}.{args.domain}"


# --- cluster helpers ---------------------------------------------------------
def _kube(args: argparse.Namespace) -> list[str]:
    cmd = ["kubectl"]
    if args.kubeconfig:
        cmd += ["--kubeconfig", args.kubeconfig]
    if args.context:
        cmd += ["--context", args.context]
    return cmd


def _helm(args: argparse.Namespace) -> list[str]:
    cmd = ["helm"]
    if args.kubeconfig:
        cmd += ["--kubeconfig", args.kubeconfig]
    if args.context:
        cmd += ["--kube-context", args.context]
    return cmd


def _run(cmd: list[str], *, check: bool = True, stdin: str | None = None) -> int:
    print(f"==> {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, input=stdin, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed (rc={proc.returncode}): {' '.join(cmd)}")
    return proc.returncode


def _run_text(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _apply(args: argparse.Namespace, objects: list[dict]) -> None:
    """Apply objects by feeding kubectl one JSON List on stdin."""
    doc = json.dumps({"apiVersion": "v1", "kind": "List", "items": objects})
    _run([*_kube(args), "apply", "-f", "-"], stdin=doc)


# --- deploy ------------------------------------------------------------------
def cmd_idp_deploy(args: argparse.Namespace) -> int:
    hostname = resolve_hostname(args)
    issuer = f"https://{hostname}"
    users_file = Path(args.users_file)
    if not users_file.is_file():
        print(f"ERROR: --users-file {users_file} not found", file=sys.stderr)
        return 2
    users_toml = users_file.read_text(encoding="utf-8", errors="replace")

    try:
        user_names, group_names = validate_users_toml(users_toml)
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"ERROR: {users_file} is not a usable directory: {exc}", file=sys.stderr)
        return 2

    redirect_uris = list(args.redirect_uri) or default_redirect_uris(
        hostname=hostname,
        host_label=args.host_label,
        base_domain=args.base_domain,
        profiles=args.profile,
        provider=args.provider,
    )

    secrets_file = Path(args.secrets_file)
    reused = args.reuse_secrets and secrets_file.is_file()
    if reused:
        # Adding a redirect URI or a user should not invalidate the credentials
        # every consumer of the fixture already holds.
        prior = {}
        for line in secrets_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                prior[key] = value
        missing = [
            k for k in ("TESTER_IDP_USER_PASSWORD", "TESTER_IDP_SEARCH_PASSWORD",
                        "TESTER_IDP_CLIENT_SECRET")
            if k not in prior
        ]
        if missing:
            print(
                f"ERROR: --reuse-secrets but {secrets_file} carries no "
                f"{', '.join(missing)}; delete it to generate a fresh set",
                file=sys.stderr,
            )
            return 2
        fixture_password = prior["TESTER_IDP_USER_PASSWORD"]
        search_password = prior["TESTER_IDP_SEARCH_PASSWORD"]
        client_secret = prior["TESTER_IDP_CLIENT_SECRET"]
    else:
        fixture_password = generate_password()
        search_password = generate_password()
        client_secret = generate_password(32)

    glauth_cfg = render_glauth_config(
        users_toml,
        fixture_password=fixture_password,
        search_password=search_password,
        base_dn=args.base_dn,
    )
    dex_values = render_dex_values(
        issuer=issuer,
        namespace=args.namespace,
        client_id=args.client_id,
        redirect_uris=redirect_uris,
        base_dn=args.base_dn,
        image_repository=args.dex_image_repository,
        image_tag=args.dex_image_tag,
        image_digest=args.dex_image_digest,
    )
    route = render_httproute(
        namespace=args.namespace,
        hostname=hostname,
        gateway=args.gateway,
        service=args.release,
    )

    if args.dry_run:
        print(json.dumps({"dexValues": dex_values, "httpRoute": route}, indent=2))
        print("\n--- glauth.cfg (password hashes redacted) ---", file=sys.stderr)
        # By value too: the placeholder is substituted wherever it appears, comments included.
        redacted = glauth_cfg
        for secret in (fixture_password, search_password):
            redacted = redacted.replace(sha256_hex(secret), "<redacted>")
        for line in redacted.splitlines():
            print(
                "  passsha256 = \"<redacted>\"" if "passsha256" in line else line, file=sys.stderr
            )
        return 0

    print(
        f"=== tester IdP: dex {args.dex_image_tag} + glauth in ns {args.namespace} "
        f"on {issuer} ===",
        file=sys.stderr,
    )
    print(
        f"  directory: {len(user_names)} user(s), {len(group_names)} group(s) -- "
        f"groups: {', '.join(group_names)}",
        file=sys.stderr,
    )

    # Written before the first apply: past this line the credentials are live in
    # the cluster, and a helm timeout below exits with no copy of them on disk.
    private_file.write_private(
        secrets_file,
        "# GENERATED by dfe-ops idp deploy -- mode 0600, never commit.\n"
        f"TESTER_IDP_ISSUER={issuer}\n"
        f"TESTER_IDP_CLIENT_ID={args.client_id}\n"
        f"TESTER_IDP_CLIENT_SECRET={client_secret}\n"
        f"TESTER_IDP_USER_PASSWORD={fixture_password}\n"
        f"TESTER_IDP_SEARCH_PASSWORD={search_password}\n"
        f"TESTER_IDP_USERS={','.join(user_names)}\n"
        f"TESTER_IDP_GROUPS={','.join(group_names)}\n",
    )

    _run([*_kube(args), "create", "namespace", args.namespace], check=False)

    _apply(
        args,
        [
            render_secret("glauth-config", args.namespace, {"glauth.cfg": glauth_cfg}),
            render_secret(
                "dex-secrets",
                args.namespace,
                {"client-secret": client_secret, "search-password": search_password},
            ),
            *render_glauth_manifests(args.namespace, image=args.glauth_image),
        ],
    )
    _run(
        [
            *_kube(args), "-n", args.namespace, "rollout", "status", "deploy/glauth",
            f"--timeout={args.timeout}s",
        ]
    )

    _run([*_helm(args), "repo", "add", "dex", args.dex_chart_repo], check=False)
    _run([*_helm(args), "repo", "update", "dex"], check=False)

    # mkstemp opens the file 0600, so the values never widen past this process.
    values_fd, values_path = tempfile.mkstemp(prefix="dex-values-", suffix=".json")
    try:
        os.write(values_fd, json.dumps(dex_values, indent=2).encode("utf-8"))
        os.close(values_fd)
        _run(
            [
                *_helm(args), "upgrade", "--install", args.release, args.dex_chart,
                "--namespace", args.namespace,
                "--version", args.dex_chart_version,
                "--values", values_path,
                "--wait", "--timeout", f"{args.timeout}s",
            ]
        )
    finally:
        os.unlink(values_path)

    _apply(args, [route])

    print("\n=== tester IdP UP ===", file=sys.stderr)
    print(f"  issuer:        {issuer}", file=sys.stderr)
    print(f"  discovery:     {issuer}/.well-known/openid-configuration", file=sys.stderr)
    print(f"  client id:     {args.client_id}", file=sys.stderr)
    print(f"  users:         {', '.join(user_names)}", file=sys.stderr)
    print("  redirect URIs:", file=sys.stderr)
    for uri in redirect_uris:
        print(f"    {uri}", file=sys.stderr)
    print(
        f"\n  client secret + user password: {secrets_file} (mode 0600"
        f"{', reused' if reused else ', freshly generated'})",
        file=sys.stderr,
    )
    print(
        "  The IdP serves the deployment's wildcard certificate, so a client that does not "
        "trust the cluster CA needs that CA (or --insecure for a test).",
        file=sys.stderr,
    )
    return 0


# --- wire-engine -------------------------------------------------------------
def render_provider_yaml(
    *,
    name: str,
    issuer: str,
    display_name: str,
    client_id_env: str,
    client_secret_env: str,
) -> str:
    """The engine's OIDC provider file for this IdP.

    Registered as `generic`, not as a dex-specific type, on purpose: the fixture
    exists to stand in for a real external provider, so a test that passes only
    because the engine special-cased dex would prove nothing about Okta or
    Entra.

    The groups mode is token_claim because the fixture has no directory API to
    enrich from -- the claim in the id_token IS the answer, which is what makes
    the whole groups -> roles chain testable with no second system.
    """
    return (
        f"# GENERATED by dfe-ops idp wire-engine for {issuer}\n"
        "type: generic\n"
        "enabled: true\n"
        f'display_name: "{display_name}"\n'
        f'issuer: "{issuer}"\n'
        f"client_id_env: {client_id_env}\n"
        f"client_secret_env: {client_secret_env}\n"
        "# dex drops the groups claim unless the client asks for the scope AND\n"
        "# the provider advertises it. Both are required and the failure is silent.\n"
        'scopes: "openid email profile groups"\n'
        "groups:\n"
        "  mode: token_claim\n"
        "  claim_name: groups\n"
    )


def cmd_idp_wire_engine(args: argparse.Namespace) -> int:
    """Hand the IdP's credentials, trust material and group map to a consumer namespace.

    Four objects the engine chart references by name but does not create: the
    client-credential Secret, the provider-definition ConfigMap, the group ->
    role ConfigMap, and the CA bundle its OIDC discovery fetch has to trust.
    Without the CA the engine's discovery call fails TLS verification against
    the deployment's own private issuer, which surfaces as a 401 on callback
    rather than as a trust error. Without the group map every fixture group
    beyond the engine's four defaults resolves to no role.

    The chart-values half (auth/oidc/authConfig) stays with the deployment, so
    this command creates only the objects those values name.
    """
    env_file = Path(args.secrets_file)
    if not env_file.is_file():
        print(f"ERROR: --secrets-file {env_file} not found (run `idp deploy` first)", file=sys.stderr)
        return 2
    groups_file = Path(args.groups_file)
    if not groups_file.is_file():
        print(f"ERROR: --groups-file {groups_file} not found", file=sys.stderr)
        return 2
    try:
        group_files = render_engine_groups(groups_file.read_text(encoding="utf-8", errors="replace"))
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"ERROR: {groups_file} is not a usable group map: {exc}", file=sys.stderr)
        return 2
    env = {}
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            env[key] = value

    ca = ""
    if args.ca_secret:
        ns, _, secret = args.ca_secret.partition("/")
        rc, out = _run_text(
            [*_kube(args), "-n", ns, "get", "secret", secret, "-o", "jsonpath={.data.ca\\.crt}"]
        )
        if rc != 0 or not out.strip():
            print(f"ERROR: no ca.crt in secret {args.ca_secret}", file=sys.stderr)
            return 1
        ca = base64.b64decode(out.strip()).decode("utf-8")

    provider = render_provider_yaml(
        name=args.provider,
        issuer=env["TESTER_IDP_ISSUER"],
        display_name=args.display_name,
        client_id_env=args.client_id_env,
        client_secret_env=args.client_secret_env,
    )

    objects = [
        render_secret(
            args.secret_name,
            args.namespace,
            {
                "client-id": env["TESTER_IDP_CLIENT_ID"],
                "client-secret": env["TESTER_IDP_CLIENT_SECRET"],
            },
        ),
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": args.providers_configmap,
                "namespace": args.namespace,
                "labels": {"app.kubernetes.io/part-of": "dfe-tester-idp"},
            },
            "data": {f"{args.provider}.yaml": provider},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": args.groups_configmap,
                "namespace": args.namespace,
                "labels": {"app.kubernetes.io/part-of": "dfe-tester-idp"},
            },
            "data": group_files,
        },
    ]
    if ca:
        objects.append(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": args.ca_configmap,
                    "namespace": args.namespace,
                    "labels": {"app.kubernetes.io/part-of": "dfe-tester-idp"},
                },
                "data": {"ca-bundle.pem": ca},
            }
        )

    if args.dry_run:
        print(json.dumps(_redacted(objects), indent=2))
        return 0

    _apply(args, objects)
    print(f"\n=== wired {env['TESTER_IDP_ISSUER']} into namespace {args.namespace} ===", file=sys.stderr)
    print("  Set these on the consumer's chart values to pick them up:", file=sys.stderr)
    print("    auth.oidcEnabled: true", file=sys.stderr)
    print("    oidc.enabled: true", file=sys.stderr)
    print(f"    oidc.providers[0].secretName: {args.secret_name}", file=sys.stderr)
    print(
        f"    oidc.providers[0].envMappings: "
        f"{{{args.client_id_env}: client-id, {args.client_secret_env}: client-secret}}",
        file=sys.stderr,
    )
    print(f"    authConfig.providersConfigMap: {args.providers_configmap}", file=sys.stderr)
    print(f"    authConfig.groupsConfigMap: {args.groups_configmap}", file=sys.stderr)
    if ca:
        print(f"    authConfig.caBundleConfigMap: {args.ca_configmap}", file=sys.stderr)
    print(f"\n  Login URL: /api/v1/auth/oidc/{args.provider}/login", file=sys.stderr)
    return 0


# --- status ------------------------------------------------------------------
def cmd_idp_status(args: argparse.Namespace) -> int:
    """Report whether the IdP is actually serving, not merely installed."""
    problems = 0
    for deploy in ("glauth", args.release):
        rc, out = _run_text(
            [
                *_kube(args), "-n", args.namespace, "get", "deploy", deploy,
                "-o", "jsonpath={.status.readyReplicas}/{.status.replicas}",
            ]
        )
        ready = out.strip()
        ok = rc == 0 and ready and not ready.startswith(("0/", "/"))
        print(f"  [{'ok' if ok else 'FAIL'}] deploy/{deploy}: {ready or 'absent'}", file=sys.stderr)
        problems += 0 if ok else 1

    rc, out = _run_text(
        [*_kube(args), "-n", args.namespace, "get", "httproute", "dex", "-o", "json"]
    )
    accepted = False
    hostnames: list[str] = []
    if rc == 0:
        route = json.loads(out)
        hostnames = route.get("spec", {}).get("hostnames", [])
        for parent in route.get("status", {}).get("parents", []):
            for cond in parent.get("conditions", []):
                if cond.get("type") == "Accepted" and cond.get("status") == "True":
                    accepted = True
    print(
        f"  [{'ok' if accepted else 'FAIL'}] httproute/dex: "
        f"{'Accepted' if accepted else 'not accepted'} {hostnames}",
        file=sys.stderr,
    )
    problems += 0 if accepted else 1

    if hostnames:
        print(f"\n  issuer: https://{hostnames[0]}", file=sys.stderr)
    print(
        f"=== tester IdP {'HEALTHY' if not problems else 'DEGRADED'} "
        f"({problems} problem(s)) ===",
        file=sys.stderr,
    )
    return 1 if problems else 0


# --- teardown ----------------------------------------------------------------
def cmd_idp_teardown(args: argparse.Namespace) -> int:
    """Remove everything deploy created. Idempotent -- a missing object is done."""
    _run(
        [*_helm(args), "uninstall", args.release, "--namespace", args.namespace, "--ignore-not-found"],
        check=False,
    )
    _run(
        [
            *_kube(args), "-n", args.namespace, "delete",
            "httproute/dex", "deploy/glauth", "svc/glauth",
            "secret/glauth-config", "secret/dex-secrets",
            "--ignore-not-found",
        ],
        check=False,
    )
    if args.delete_namespace:
        _run([*_kube(args), "delete", "namespace", args.namespace, "--ignore-not-found"], check=False)
    print("=== tester IdP torn down ===", file=sys.stderr)
    return 0


# --- parser ------------------------------------------------------------------
def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--kubeconfig",
        default=os.environ.get("KUBECONFIG"),
        help="kubeconfig for the target cluster (default: $KUBECONFIG / current context)",
    )
    parser.add_argument("--context", default=None, help="kubeconfig context")
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE, help="namespace to run the IdP in")
    parser.add_argument("--release", default=DEFAULT_RELEASE, help="helm release name for dex")


def add_idp_subparser(sub) -> None:
    """Register `dfe-ops idp` and its actions."""
    idp = sub.add_parser(
        "idp",
        help="stand up / tear down a throwaway Dex+glauth tester IdP behind the gateway",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    actions = idp.add_subparsers(dest="idp_action", required=True, metavar="<action>")

    dep = actions.add_parser(
        "deploy",
        help="install dex + glauth and route it on the deployment's gateway",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_common(dep)
    dep.add_argument("--domain", default=None, help="deployment domain; the IdP lands on <label>.<domain>")
    dep.add_argument("--base-domain", default="", metavar="DOMAIN",
                     help="estate domain under which each profile publishes "
                          "<profile>.<base>; registers one redirect URI per profile")
    dep.add_argument("--profile", action="append", default=[], metavar="NAME",
                     help=f"profile to register a redirect URI for with --base-domain "
                          f"(repeatable; default {', '.join(DEFAULT_PROFILES)})")
    dep.add_argument("--provider", default=DEFAULT_PROVIDER,
                     help="provider name in the engine's callback path")
    dep.add_argument("--host-label", default=DEFAULT_HOST_LABEL, help="host label prepended to --domain")
    dep.add_argument("--hostname", default=None, help="full IdP hostname (overrides --domain)")
    dep.add_argument("--gateway", default=DEFAULT_GATEWAY, metavar="NS/NAME",
                     help="Gateway to attach the HTTPRoute to")
    dep.add_argument("--client-id", default=DEFAULT_CLIENT_ID, help="static OAuth client id to register")
    dep.add_argument("--redirect-uri", action="append", default=[], metavar="URL",
                     help="allowed redirect URI (repeatable; every target that will log in)")
    dep.add_argument("--users-file", default=str(DEFAULT_USERS_FILE),
                     help="glauth-format TOML directory of users + groups")
    dep.add_argument("--base-dn", default=DEFAULT_BASE_DN, help="LDAP base DN for the directory")
    dep.add_argument("--secrets-out", dest="secrets_file", default=".tmp/tester-idp.env",
                     help="0600 file the generated client secret + user password are written to")
    dep.add_argument("--reuse-secrets", action="store_true",
                     help="keep the credentials in --secrets-out instead of generating new ones, "
                          "so adding a redirect URI does not invalidate every consumer")
    dep.add_argument("--dex-chart-repo", default=DEX_CHART_REPO, help="dex helm repo URL")
    dep.add_argument("--dex-chart", default=DEX_CHART, help="dex chart reference")
    dep.add_argument("--dex-chart-version", default=DEX_CHART_VERSION, help="dex chart version")
    dep.add_argument("--dex-image-repository", default=DEX_IMAGE_REPOSITORY, help="dex image repository")
    dep.add_argument("--dex-image-tag", default=DEX_IMAGE_TAG, help="dex image tag")
    dep.add_argument("--dex-image-digest", default=DEX_IMAGE_DIGEST, help="dex image digest (pull by digest)")
    dep.add_argument("--glauth-image", default=GLAUTH_IMAGE, help="glauth image, digest-pinned")
    dep.add_argument("--timeout", type=int, default=300, help="rollout/helm wait timeout (seconds)")
    dep.add_argument("--dry-run", action="store_true",
                     help="print the rendered values + route and touch nothing")
    dep.set_defaults(func=cmd_idp_deploy)

    we = actions.add_parser(
        "wire-engine",
        help="hand the IdP's client credentials, provider file and CA to a consumer namespace",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_common(we)
    we.add_argument("--secrets-file", default=".tmp/tester-idp.env",
                    help="the 0600 file `idp deploy` wrote")
    we.add_argument("--provider", default=DEFAULT_PROVIDER, help="provider name the engine registers it under")
    we.add_argument("--display-name", default="Dex (tester IdP)", help="label shown on the login page")
    we.add_argument("--secret-name", default="dfe-oidc-dex", help="Secret to create the credentials in")
    we.add_argument("--providers-configmap", default="dfe-oidc-providers",
                    help="ConfigMap to write the provider definition to")
    we.add_argument("--groups-file", default=str(DEFAULT_GROUPS_FILE),
                    help="TOML map of the fixture's group names to engine roles, scope and org_ids")
    we.add_argument("--groups-configmap", default=DEFAULT_GROUPS_CONFIGMAP,
                    help="ConfigMap to write the engine group files to")
    we.add_argument("--ca-configmap", default="dfe-oidc-ca",
                    help="ConfigMap to write the CA bundle to")
    we.add_argument("--ca-secret", default=None, metavar="NS/NAME",
                    help="TLS Secret whose ca.crt the consumer must trust to reach the issuer")
    we.add_argument("--client-id-env", default="DFE_OIDC_DEX_CLIENT_ID",
                    help="env var the consumer reads the client id from")
    we.add_argument("--client-secret-env", default="DFE_OIDC_DEX_CLIENT_SECRET",
                    help="env var the consumer reads the client secret from")
    we.add_argument("--dry-run", action="store_true", help="print the objects and apply nothing")
    we.set_defaults(func=cmd_idp_wire_engine)

    st = actions.add_parser(
        "status",
        help="report whether the tester IdP is serving",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_common(st)
    st.set_defaults(func=cmd_idp_status)

    td = actions.add_parser(
        "teardown",
        help="remove the tester IdP",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_common(td)
    td.add_argument("--delete-namespace", action="store_true", help="also delete the namespace")
    td.set_defaults(func=cmd_idp_teardown)
