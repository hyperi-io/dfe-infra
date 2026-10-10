#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         external_idp.py
#  Purpose:      Register a hosted OIDC identity provider (Okta, Entra ID, Google
#                Workspace) on a deployment's engine, and remove it again.
#                Registered on dfe-ops as `idp wire-external`.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Wire a hosted IdP into a DFE engine: `dfe-ops idp wire-external`.

`idp wire-engine` hands the engine the Dex fixture `idp deploy` stood up. This is
the same hand-over for a provider that already exists -- an Okta org, an Entra ID
tenant, a Google Workspace -- so nothing is deployed, only registered.

    env OKTA_CLIENT_SECRET=... python3 scripts/dfe-ops idp wire-external \\
        --type okta --issuer https://<org>.okta.com --client-id <id> \\
        --client-secret-env OKTA_CLIENT_SECRET --groups-dir <private>/engine-groups \\
        --target k8s --namespace <engine namespace>

The client secret arrives by the NAME of an environment variable, never as a
value on the command line, where shell history and `ps` would keep it.

Group files are flat and provider-prefixed (`okta-dfe-admins.yaml`): the engine
reads one flat group directory and links each group to one provider through
`source_provider` and `source_id`. Only files carrying this provider's prefix are
taken, so one directory serves every provider. A file is the engine's own group
shape, as JSON or as plain YAML (scalars and lists); `source_id` defaults to the
stem after the prefix, which is the claim value for a provider that emits group
names.

Targets:

- `k8s` merges the provider file and the group files into the ConfigMaps the
  engine chart seeds from (`authConfig.providersConfigMap` and
  `authConfig.groupsConfigMap`) and applies a Secret holding the client secret,
  as `wire-engine` does. A ledger annotation on the providers ConfigMap records
  what was added.
- `docker` writes the files into the engine container's config directory and the
  secret into the compose env file, then recreates the engine so it reads both.
  A marked block in the env file records what was added.

`--teardown` reads that record and removes exactly what it lists, including the
copies the engine's config volume already holds. `--dry-run` prints the plan,
secret redacted, and touches nothing.

Provider files follow the engine's provider model. Okta and Entra ID read the
groups claim (`token_claim`); Entra ID also records its tenant, from --tenant-id
or the issuer path, because the engine's lookup for a user in over 200 groups
needs it. Google puts no groups in its tokens, so it runs in
`api` mode with `enrich_on_login`, the one shape the engine accepts for it. The
scopes are left to the engine's per-type default unless --scopes is given, and
Google's are never overridden.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import private_file
import tester_idp

IDP_TYPES = ("okta", "entra_id", "google")
DEFAULT_PROVIDER_NAMES = {"okta": "okta", "entra_id": "entra", "google": "google-workspace"}
DISPLAY_NAMES = {"okta": "Okta", "entra_id": "Microsoft Entra ID", "google": "Google Workspace"}

DEFAULT_PROVIDERS_CONFIGMAP = "dfe-oidc-providers"
DEFAULT_GROUPS_CONFIGMAP = tester_idp.DEFAULT_GROUPS_CONFIGMAP
DEFAULT_ENGINE = "dfe-engine"
# The engine chart's config.mountPath and dfe-docker's DFE_ENGINE_CONFIG_DIR default.
CONFIG_DIRS = {"k8s": "/config", "docker": "/app/config"}
PROVIDERS_SUBDIR = "auth/oidc-providers"
GROUPS_SUBDIR = "auth/groups"

PART_OF = "dfe-external-idp"
CREATED_BY_LABEL = "dfe.hyperi.io/created-by"
CREATED_BY = "wire-external"
LEDGER_PREFIX = "dfe.hyperi.io/wire-external-"

# The engine group fields a file may carry; members are local accounts, never an IdP's.
GROUP_FIELDS = frozenset(
    {"attributes", "description", "org_ids", "roles", "scope", "source_id", "source_provider"}
)
# A DNS label short enough that the ledger annotation name stays within 63 characters.
_PROVIDER_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,47}[a-z0-9])?\Z")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
_GUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")

# Runs inside the engine container, as the engine's own user, so the files land with
# the ownership the engine writes beside them. Reads a JSON plan on stdin.
CONTAINER_EDIT = """
import json, pathlib, sys
plan = json.load(sys.stdin)
root = pathlib.Path(plan["root"])
for rel, text in plan["write"].items():
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\\n")
    print("wrote", path)
for rel in plan["remove"]:
    path = root / rel
    if path.is_file():
        path.unlink()
        print("removed", path)
"""


@dataclass(frozen=True, slots=True)
class Wiring:
    """Everything one wire-external run adds, resolved and validated.

    Attributes:
        provider: The engine provider name, a path segment of the callback URI.
        engine_secret_env: The env var the engine reads the client secret from.
        secret_name: The k8s Secret holding the client secret.
        config_dir: The engine's config directory inside its container.
        provider_file: The provider file body; empty on a teardown.
        group_files: Group file name -> body; empty on a teardown.
        secret_value: The client secret; empty on a teardown.
    """

    provider: str
    engine_secret_env: str
    secret_name: str
    config_dir: str
    provider_file: str = ""
    group_files: dict[str, str] = field(default_factory=dict)
    secret_value: str = field(default="", repr=False)

    @property
    def provider_key(self) -> str:
        """The provider file's name, in the ConfigMap and on disk."""
        return f"{self.provider}.yaml"

    @property
    def ledger_key(self) -> str:
        """The annotation on the providers ConfigMap recording what was added."""
        return f"{LEDGER_PREFIX}{self.provider}"

    def paths(self, group_keys: list[str]) -> list[str]:
        """The provider file and the named group files, relative to the config dir."""
        return [
            f"{PROVIDERS_SUBDIR}/{self.provider_key}",
            *(f"{GROUPS_SUBDIR}/{key}" for key in group_keys),
        ]


# --- group files -------------------------------------------------------------
def _scalar(value: str, where: str) -> str:
    """One YAML scalar: quoted, or plain with a trailing comment dropped."""
    value = value.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        if end == -1:
            raise ValueError(f"{where}: unterminated quote")
        return value[1:end]
    return value.split(" #", 1)[0].strip()


def _flow_list(value: str, where: str) -> list[str]:
    """A one-line `[a, b]` list."""
    text = value.split(" #", 1)[0].strip()
    if not text.endswith("]"):
        raise ValueError(f"{where}: a list opened with '[' must close on the same line")
    inner = text[1:-1].strip()
    return [_scalar(item, where) for item in inner.split(",") if item.strip()] if inner else []


def parse_group_file(text: str, *, source: str) -> dict[str, object]:
    """Read one engine group file: a JSON object, or YAML of scalars and lists.

    The engine's group shape is flat -- strings, lists of strings and an
    attributes map -- so this reads that shape with the stdlib alone, as every
    dfe-ops script does. A nested mapping other than an empty one has to be
    written as JSON, which is valid YAML the engine reads unchanged.

    Args:
        text: The file body.
        source: The file name, for error messages.

    Returns:
        The file's fields.

    Raises:
        ValueError: The body is neither a JSON object nor YAML of that shape.
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            loaded = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source}: not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"{source}: holds a {type(loaded).__name__}, not a mapping")
        return loaded
    data: dict[str, object] = {}
    open_list: list[str] | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line == "---":
            continue
        where = f"{source} line {number}"
        if line == "-" or line.startswith("- "):
            if open_list is None:
                raise ValueError(f"{where}: a list item outside a list")
            open_list.append(_scalar(line[1:], where))
            continue
        if raw[:1].isspace():
            raise ValueError(f"{where}: nested mappings are not read; write the file as JSON")
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"{where}: expected 'key: value'")
        if key in data:
            raise ValueError(f"{where}: {key!r} is repeated")
        value = value.strip()
        open_list = None
        if not value or value.startswith("#"):
            open_list = []
            data[key] = open_list
        elif value.startswith("["):
            data[key] = _flow_list(value, where)
        elif value.startswith("{"):
            if value.split(" #", 1)[0].strip() != "{}":
                raise ValueError(f"{where}: a non-empty mapping has to be written as JSON")
            data[key] = {}
        else:
            data[key] = _scalar(value, where)
    return data


def render_group_files(groups_dir: Path, provider: str) -> dict[str, str]:
    """The engine group files for `provider`, keyed by file name.

    Each body is JSON, which the engine's YAML loader reads unchanged. A file
    that links to another provider, or carries a field the engine's group model
    does not have, is refused rather than wired half-right.

    Args:
        groups_dir: The flat directory of `<provider>-<group>.yaml` files.
        provider: The engine provider name; its files carry `<provider>-`.

    Returns:
        File name -> file body, sorted by name.

    Raises:
        ValueError: No file carries the prefix, or one is not a usable group.
    """
    prefix = f"{provider}-"
    files: dict[str, str] = {}
    for path in sorted(groups_dir.glob(f"{prefix}*.yaml")):
        entry = parse_group_file(
            path.read_text(encoding="utf-8", errors="replace"), source=path.name
        )
        unknown = sorted(set(entry) - GROUP_FIELDS)
        if unknown:
            raise ValueError(f"{path.name}: not engine group fields: {', '.join(unknown)}")
        linked = entry.get("source_provider", provider)
        if linked != provider:
            raise ValueError(f"{path.name}: source_provider is {linked!r}, not {provider!r}")
        body = tester_idp.engine_group_body(
            path.stem, entry, provider, default_source_id=path.stem[len(prefix) :]
        )
        attributes = entry.get("attributes", {})
        if not isinstance(attributes, dict):
            raise ValueError(f"{path.name}: attributes must be a mapping")
        if attributes:
            body["attributes"] = attributes
        files[path.name] = json.dumps(body, indent=2) + "\n"
    if not files:
        raise ValueError(f"{groups_dir} holds no {prefix}*.yaml group file")
    return files


# --- provider file -----------------------------------------------------------
def render_provider(
    *,
    idp_type: str,
    issuer: str,
    client_id: str,
    client_secret_env: str,
    display_name: str,
    scopes: str = "",
    tenant_id: str = "",
) -> dict:
    """The engine's provider definition for a hosted IdP.

    Args:
        idp_type: okta, entra_id or google.
        issuer: The IdP's OIDC issuer URL.
        client_id: The OIDC client id, held in the clear as the engine allows.
        client_secret_env: The env var the engine reads the client secret from.
        display_name: The label on the login page.
        scopes: Space-separated scopes; empty takes the engine's per-type default.
        tenant_id: Entra ID only: the tenant the >200-group overage lookup asks.

    Returns:
        The provider body. The provider name is the file stem, never a field.
    """
    groups: dict[str, object] = {"mode": "token_claim", "claim_name": "groups"}
    if idp_type == "google":
        groups = {"mode": "api", "enrich_on_login": True}
    if idp_type == "entra_id" and tenant_id:
        groups["tenant_id"] = tenant_id
    provider: dict[str, object] = {
        "type": idp_type,
        "enabled": True,
        "display_name": display_name,
        "issuer": issuer,
        "client_id": client_id,
        "client_secret_env": client_secret_env,
        "groups": groups,
    }
    if scopes:
        provider["scopes"] = scopes
    return provider


def entra_tenant(issuer: str, explicit: str | None) -> str:
    """The Entra tenant id: --tenant-id, else the GUID an Entra v2 issuer path carries."""
    if explicit:
        return explicit
    first = urlsplit(issuer).path.strip("/").split("/")[0]
    return first if _GUID.match(first) else ""


def default_engine_secret_env(provider: str) -> str:
    """DFE_OIDC_<PROVIDER>_CLIENT_SECRET, the env var naming dfe-docker's examples use."""
    return f"DFE_OIDC_{re.sub(r'[^A-Z0-9]', '_', provider.upper())}_CLIENT_SECRET"


def resolve_wiring(args: argparse.Namespace, environ: dict[str, str]) -> Wiring:
    """Validate the arguments and render everything the run adds.

    Args:
        args: The parsed wire-external arguments.
        environ: Where --client-secret-env is looked up.

    Returns:
        The resolved wiring; a teardown carries names only.

    Raises:
        ValueError: An argument is missing or unusable. The message never repeats
            a value that could be a secret.
    """
    provider = args.provider or DEFAULT_PROVIDER_NAMES[args.idp_type]
    if not _PROVIDER_NAME.match(provider):
        raise ValueError(
            f"--provider {provider!r} must be lowercase letters, digits and '-', at most 49"
        )
    engine_secret_env = args.engine_secret_env or default_engine_secret_env(provider)
    if not _ENV_NAME.match(engine_secret_env):
        raise ValueError("--engine-secret-env must be an environment variable name")
    if args.target == "k8s" and not args.namespace:
        raise ValueError("--target k8s needs --namespace, the engine's namespace")
    if args.target == "docker" and not args.compose_dir:
        raise ValueError("--target docker needs --compose-dir, where the engine is recreated")
    names = {
        "provider": provider,
        "engine_secret_env": engine_secret_env,
        "secret_name": args.secret_name or f"dfe-oidc-{provider}",
        "config_dir": args.config_dir or CONFIG_DIRS[args.target],
    }
    if args.teardown:
        return Wiring(**names)

    issuer = args.issuer or ""
    parts = urlsplit(issuer)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("--issuer must be the IdP's https issuer URL")
    if not args.client_id or any(ch.isspace() for ch in args.client_id):
        raise ValueError("--client-id is required and carries no whitespace")
    if not args.client_secret_env or not _ENV_NAME.match(args.client_secret_env):
        raise ValueError(
            "--client-secret-env takes the NAME of an environment variable holding the "
            "client secret, never the secret itself"
        )
    secret_value = environ.get(args.client_secret_env, "")
    if not secret_value:
        raise ValueError("the variable --client-secret-env names is unset or empty")
    if args.scopes is not None:
        if args.idp_type == "google":
            raise ValueError(
                "--scopes is refused for google: the engine's default carries the "
                "Cloud Identity groups scope its logins need"
            )
        if "openid" not in args.scopes.split():
            raise ValueError("--scopes must include openid")
    if args.tenant_id is not None and args.idp_type != "entra_id":
        raise ValueError("--tenant-id is for entra_id only")
    if args.tenant_id is not None and not _GUID.match(args.tenant_id):
        raise ValueError("--tenant-id must be the Entra tenant's GUID")
    if not args.groups_dir or not Path(args.groups_dir).is_dir():
        raise ValueError("--groups-dir must name a directory of group files")
    group_files = render_group_files(Path(args.groups_dir), provider)
    provider_body = render_provider(
        idp_type=args.idp_type,
        issuer=issuer,
        client_id=args.client_id,
        client_secret_env=engine_secret_env,
        display_name=args.display_name or DISPLAY_NAMES[args.idp_type],
        scopes=args.scopes or "",
        tenant_id=entra_tenant(issuer, args.tenant_id) if args.idp_type == "entra_id" else "",
    )
    return Wiring(
        **names,
        provider_file=json.dumps(provider_body, indent=2) + "\n",
        group_files=group_files,
        secret_value=secret_value,
    )


# --- k8s ---------------------------------------------------------------------
def _configmap(name: str, namespace: str) -> dict:
    """An empty ConfigMap this tool created, so a teardown may delete it once empty."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/part-of": PART_OF, CREATED_BY_LABEL: CREATED_BY},
        },
    }


def _secret(wiring: Wiring, namespace: str) -> dict:
    secret = tester_idp.render_secret(
        wiring.secret_name, namespace, {"client-secret": wiring.secret_value}
    )
    secret["metadata"]["labels"] = {"app.kubernetes.io/part-of": PART_OF}
    return secret


def _get_json(args: argparse.Namespace, kind: str, name: str) -> dict | None:
    """The object, or None when it does not exist. Any other failure exits."""
    cmd = [*tester_idp._kube(args), "-n", args.namespace, "get", kind, name, "-o", "json"]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if proc.returncode == 0:
        return json.loads(proc.stdout)
    if "NotFound" in proc.stderr:
        return None
    raise SystemExit(f"kubectl get {kind} {name} failed: {proc.stderr.strip()}")


def _merge_patch(args: argparse.Namespace, kind: str, name: str, patch: dict) -> None:
    tester_idp._run(
        [
            *tester_idp._kube(args),
            "-n",
            args.namespace,
            "patch",
            kind,
            name,
            "--type",
            "merge",
            "-p",
            json.dumps(patch),
        ]
    )


def _ledger(configmap: dict | None, key: str) -> dict:
    """The ledger a previous run left on the providers ConfigMap, or an empty one."""
    if configmap is None:
        return {}
    raw = configmap.get("metadata", {}).get("annotations", {}).get(key, "")
    try:
        ledger = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return ledger if isinstance(ledger, dict) else {}


def _ledger_value(wiring: Wiring) -> str:
    """The ledger annotation's value: the Secret and the group keys this run adds."""
    return json.dumps({"secret": wiring.secret_name, "groups": sorted(wiring.group_files)})


def k8s_plan(args: argparse.Namespace, wiring: Wiring) -> dict:
    """What a k8s wire applies, with the secret redacted, for --dry-run."""
    return {
        "target": "k8s",
        "namespace": args.namespace,
        "configmaps": {
            args.providers_configmap: {
                "annotations": {wiring.ledger_key: _ledger_value(wiring)},
                "data": {wiring.provider_key: wiring.provider_file},
            },
            args.groups_configmap: {"data": wiring.group_files},
        },
        "secret": tester_idp._redacted([_secret(wiring, args.namespace)])[0],
    }


def k8s_wire(args: argparse.Namespace, wiring: Wiring) -> int:
    """Merge the files into the engine's seed ConfigMaps and apply the Secret.

    The ledger is written before anything it lists, so a run that fails part
    way still leaves a teardown that knows what to remove. Group files a
    previous run of this provider added and this one does not are removed.
    """
    kube = tester_idp._kube(args)
    providers_cm = _get_json(args, "configmap", args.providers_configmap)
    if providers_cm is None:
        tester_idp._run(
            [*kube, "create", "-f", "-"],
            stdin=json.dumps(_configmap(args.providers_configmap, args.namespace)),
        )
    if _get_json(args, "configmap", args.groups_configmap) is None:
        tester_idp._run(
            [*kube, "create", "-f", "-"],
            stdin=json.dumps(_configmap(args.groups_configmap, args.namespace)),
        )
    stale = set(_ledger(providers_cm, wiring.ledger_key).get("groups", [])) - set(
        wiring.group_files
    )
    _merge_patch(
        args,
        "configmap",
        args.providers_configmap,
        {
            "metadata": {"annotations": {wiring.ledger_key: _ledger_value(wiring)}},
            "data": {wiring.provider_key: wiring.provider_file},
        },
    )
    group_data: dict[str, str | None] = dict(wiring.group_files)
    group_data.update(dict.fromkeys(sorted(stale)))
    _merge_patch(args, "configmap", args.groups_configmap, {"data": group_data})
    tester_idp._apply(args, [_secret(wiring, args.namespace)])

    print(f"\n=== wired {wiring.provider} into namespace {args.namespace} ===", file=sys.stderr)
    print(
        f"  provider file {wiring.provider_key} and {len(wiring.group_files)} group file(s)",
        file=sys.stderr,
    )
    print("  Set these on the engine's chart values to pick them up:", file=sys.stderr)
    print("    oidc.enabled: true", file=sys.stderr)
    print(
        f"    oidc.providers[]: {{name: {wiring.provider}, secretName: {wiring.secret_name}, "
        f"envMappings: {{{wiring.engine_secret_env}: client-secret}}}}",
        file=sys.stderr,
    )
    print(f"    authConfig.providersConfigMap: {args.providers_configmap}", file=sys.stderr)
    print(f"    authConfig.groupsConfigMap: {args.groups_configmap}", file=sys.stderr)
    _print_callback(wiring)
    return 0


def k8s_teardown(args: argparse.Namespace, wiring: Wiring) -> int:
    """Remove what the ledger lists: keys, the Secret, and the engine's seeded copies.

    The chart's seed step copies ConfigMap keys into the engine's config volume
    and never deletes, so a persistent volume keeps a removed provider live until
    the copies go too. A ConfigMap this tool created is deleted once empty.
    """
    kube = tester_idp._kube(args)
    providers_cm = _get_json(args, "configmap", args.providers_configmap)
    ledger = _ledger(providers_cm, wiring.ledger_key)
    if not ledger:
        print(
            f"=== nothing recorded for {wiring.provider} on configmap "
            f"{args.providers_configmap}; nothing removed ===",
            file=sys.stderr,
        )
        return 0
    group_keys = [key for key in ledger.get("groups", []) if isinstance(key, str)]
    _merge_patch(
        args,
        "configmap",
        args.providers_configmap,
        {
            "metadata": {"annotations": {wiring.ledger_key: None}},
            "data": {wiring.provider_key: None},
        },
    )
    if group_keys and _get_json(args, "configmap", args.groups_configmap) is not None:
        _merge_patch(args, "configmap", args.groups_configmap, {"data": dict.fromkeys(group_keys)})
    secret = ledger.get("secret", wiring.secret_name)
    tester_idp._run(
        [*kube, "-n", args.namespace, "delete", "secret", str(secret), "--ignore-not-found"]
    )

    exec_cmd = [*kube, "-n", args.namespace, "exec", "-i", f"deploy/{args.engine_deployment}"]
    if args.engine_container:
        exec_cmd += ["-c", args.engine_container]
    plan = {"root": wiring.config_dir, "write": {}, "remove": wiring.paths(group_keys)}
    rc = tester_idp._run(
        [*exec_cmd, "--", "python3", "-c", CONTAINER_EDIT], check=False, stdin=json.dumps(plan)
    )
    if rc != 0:
        print(
            f"WARNING: could not remove the seeded copies from deploy/{args.engine_deployment}; "
            f"remove {', '.join(plan['remove'])} under {wiring.config_dir} by hand",
            file=sys.stderr,
        )

    for name in (args.providers_configmap, args.groups_configmap):
        configmap = _get_json(args, "configmap", name)
        if configmap is None or configmap.get("data"):
            continue
        if configmap.get("metadata", {}).get("labels", {}).get(CREATED_BY_LABEL) == CREATED_BY:
            tester_idp._run(
                [*kube, "-n", args.namespace, "delete", "configmap", name, "--ignore-not-found"]
            )
    print(f"=== {wiring.provider} unwired from namespace {args.namespace} ===", file=sys.stderr)
    return 0 if rc == 0 else 1


# --- docker ------------------------------------------------------------------
def _markers(provider: str) -> tuple[str, str]:
    return (
        f"# BEGIN dfe-ops idp wire-external {provider}",
        f"# END dfe-ops idp wire-external {provider}",
    )


def split_env_block(text: str, provider: str) -> tuple[list[str], list[str]]:
    """Split an env file into (the lines outside this provider's block, the block)."""
    begin, end = _markers(provider)
    outside: list[str] = []
    block: list[str] = []
    inside = False
    for line in text.splitlines():
        if line == begin:
            inside = True
            block.append(line)
        elif line == end and inside:
            inside = False
            block.append(line)
        elif inside:
            block.append(line)
        else:
            outside.append(line)
    return outside, block


def block_files(block: list[str]) -> list[str]:
    """The config-dir paths a block records, from its `# files:` line."""
    for line in block:
        if line.startswith("# files:"):
            return line.removeprefix("# files:").split()
    return []


def _assigns(line: str, key: str) -> bool:
    stripped = line.strip().removeprefix("export ").lstrip()
    return stripped.startswith(f"{key}=")


def docker_env_text(outside: list[str], wiring: Wiring) -> str:
    """The env file with this provider's block, secret included, at the end."""
    begin, end = _markers(wiring.provider)
    block = [
        begin,
        f"# files: {' '.join(wiring.paths(sorted(wiring.group_files)))}",
        f"{wiring.engine_secret_env}={wiring.secret_value}",
        end,
    ]
    return "\n".join([*outside, *block]) + "\n"


def _env_file(args: argparse.Namespace) -> Path:
    return Path(args.env_file) if args.env_file else Path(args.compose_dir) / "env" / "engine.env"


def _recreate(args: argparse.Namespace) -> None:
    """Recreate the engine: a restart keeps the old environment, a recreate rereads env_file."""
    tester_idp._run(
        ["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", args.service],
        cwd=Path(args.compose_dir),
    )


def _container_edit(args: argparse.Namespace, plan: dict) -> None:
    tester_idp._run(
        ["docker", "exec", "-i", args.container, "python3", "-c", CONTAINER_EDIT],
        stdin=json.dumps(plan),
    )


def docker_plan(args: argparse.Namespace, wiring: Wiring) -> dict:
    """What a docker wire writes, with the secret redacted, for --dry-run."""
    files = {f"{PROVIDERS_SUBDIR}/{wiring.provider_key}": wiring.provider_file}
    files.update({f"{GROUPS_SUBDIR}/{key}": body for key, body in wiring.group_files.items()})
    return {
        "target": "docker",
        "container": args.container,
        "config_dir": wiring.config_dir,
        "files": files,
        "env_file": str(_env_file(args)),
        "env": {wiring.engine_secret_env: "<redacted>"},
        "recreate": f"docker compose up -d --no-deps --force-recreate {args.service}",
    }


def docker_wire(args: argparse.Namespace, wiring: Wiring) -> int:
    """Write the files into the engine's config dir and the secret into its env file."""
    env_file = _env_file(args)
    text = env_file.read_text(encoding="utf-8", errors="replace") if env_file.is_file() else ""
    outside, block = split_env_block(text, wiring.provider)
    if any(_assigns(line, wiring.engine_secret_env) for line in outside):
        print(
            f"ERROR: {env_file} already sets {wiring.engine_secret_env} outside this tool's "
            "block; remove it or pass another --engine-secret-env",
            file=sys.stderr,
        )
        return 2
    keep = wiring.paths(sorted(wiring.group_files))
    stale = [path for path in block_files(block) if path not in keep]
    write = {f"{PROVIDERS_SUBDIR}/{wiring.provider_key}": wiring.provider_file}
    write.update({f"{GROUPS_SUBDIR}/{key}": body for key, body in wiring.group_files.items()})
    _container_edit(args, {"root": wiring.config_dir, "write": write, "remove": stale})
    private_file.write_private(env_file, docker_env_text(outside, wiring))
    _recreate(args)
    print(f"\n=== wired {wiring.provider} into container {args.container} ===", file=sys.stderr)
    print(
        f"  {len(write)} file(s) under {wiring.config_dir}; "
        f"{wiring.engine_secret_env} in {env_file} (mode 0600)",
        file=sys.stderr,
    )
    _print_callback(wiring)
    return 0


def docker_teardown(args: argparse.Namespace, wiring: Wiring) -> int:
    """Remove the files and the env block the wire recorded, then recreate the engine."""
    env_file = _env_file(args)
    text = env_file.read_text(encoding="utf-8", errors="replace") if env_file.is_file() else ""
    outside, block = split_env_block(text, wiring.provider)
    if not block:
        print(
            f"=== nothing recorded for {wiring.provider} in {env_file}; nothing removed ===",
            file=sys.stderr,
        )
        return 0
    _container_edit(args, {"root": wiring.config_dir, "write": {}, "remove": block_files(block)})
    private_file.write_private(env_file, "\n".join(outside) + "\n" if outside else "")
    _recreate(args)
    print(f"=== {wiring.provider} unwired from container {args.container} ===", file=sys.stderr)
    return 0


# --- command -----------------------------------------------------------------
def _print_callback(wiring: Wiring) -> None:
    path = f"/api/v1/auth/oidc/{wiring.provider}"
    print(f"  Login URL: {path}/login", file=sys.stderr)
    print(
        f"  Redirect URI the IdP must allow: <engine base URL as the browser reaches it>"
        f"{path}/callback",
        file=sys.stderr,
    )


def cmd_idp_wire_external(args: argparse.Namespace) -> int:
    """Register (or with --teardown, remove) a hosted IdP on the engine."""
    if args.client_secret_on_argv is not None:
        print(
            "ERROR: a client secret is never taken on the command line; put it in an "
            "environment variable and pass that variable's NAME with --client-secret-env",
            file=sys.stderr,
        )
        return 2
    try:
        wiring = resolve_wiring(args, dict(os.environ))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        if args.teardown:
            where = (
                f"configmap {args.providers_configmap} annotation {wiring.ledger_key}"
                if args.target == "k8s"
                else f"the block in {_env_file(args)}"
            )
            print(
                json.dumps(
                    {
                        "target": args.target,
                        "teardown": wiring.provider,
                        "removes": f"what {where} records",
                    },
                    indent=2,
                )
            )
            return 0
        plan = k8s_plan(args, wiring) if args.target == "k8s" else docker_plan(args, wiring)
        print(json.dumps(plan, indent=2))
        return 0
    if args.target == "k8s":
        return k8s_teardown(args, wiring) if args.teardown else k8s_wire(args, wiring)
    return docker_teardown(args, wiring) if args.teardown else docker_wire(args, wiring)


def add_wire_external_parser(actions) -> None:
    """Register `dfe-ops idp wire-external` on the idp action parser."""
    we = actions.add_parser(
        "wire-external",
        help="register a hosted IdP (Okta, Entra ID, Google) on a deployment's engine",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    we.add_argument(
        "--type",
        dest="idp_type",
        required=True,
        choices=IDP_TYPES,
        help="provider type, as the engine's provider model names it",
    )
    we.add_argument(
        "--provider",
        default=None,
        help="name the engine registers it under, a path segment of the callback "
        "URI (default by type: okta, entra, google-workspace)",
    )
    we.add_argument("--issuer", default=None, help="the IdP's https OIDC issuer URL")
    we.add_argument(
        "--client-id",
        default=None,
        help="the OIDC client id; not secret, so it is held in the provider file",
    )
    we.add_argument(
        "--client-secret-env",
        default=None,
        metavar="NAME",
        help="NAME of the environment variable holding the client secret",
    )
    # Exists only to refuse a secret value on argv without echoing it back.
    we.add_argument(
        "--client-secret", dest="client_secret_on_argv", default=None, help=argparse.SUPPRESS
    )
    we.add_argument(
        "--groups-dir",
        default=None,
        help="flat directory of <provider>-<group>.yaml engine group files",
    )
    we.add_argument(
        "--display-name", default=None, help="label on the login page (default by type)"
    )
    we.add_argument(
        "--scopes",
        default=None,
        help="space-separated scopes; default is the engine's per-type set, and "
        "google refuses an override",
    )
    we.add_argument(
        "--tenant-id",
        default=None,
        help="entra_id: tenant GUID for the >200-group overage lookup "
        "(default: the GUID in the issuer path)",
    )
    we.add_argument(
        "--engine-secret-env",
        default=None,
        metavar="NAME",
        help="env var the engine reads the client secret from "
        "(default DFE_OIDC_<PROVIDER>_CLIENT_SECRET)",
    )
    we.add_argument(
        "--target", required=True, choices=("k8s", "docker"), help="where the engine runs"
    )
    we.add_argument(
        "--config-dir",
        default=None,
        help="engine config directory inside its container "
        f"(default {CONFIG_DIRS['k8s']} on k8s, {CONFIG_DIRS['docker']} on docker)",
    )
    we.add_argument(
        "--kubeconfig",
        default=os.environ.get("KUBECONFIG"),
        help="k8s: kubeconfig (default: $KUBECONFIG / current context)",
    )
    we.add_argument("--context", default=None, help="k8s: kubeconfig context")
    we.add_argument("--namespace", default=None, help="k8s: the engine's namespace")
    we.add_argument(
        "--secret-name",
        default=None,
        help="k8s: Secret for the client secret (default dfe-oidc-<provider>)",
    )
    we.add_argument(
        "--providers-configmap",
        default=DEFAULT_PROVIDERS_CONFIGMAP,
        help="k8s: ConfigMap authConfig.providersConfigMap names",
    )
    we.add_argument(
        "--groups-configmap",
        default=DEFAULT_GROUPS_CONFIGMAP,
        help="k8s: ConfigMap authConfig.groupsConfigMap names",
    )
    we.add_argument(
        "--engine-deployment",
        default=DEFAULT_ENGINE,
        help="k8s: engine Deployment whose config volume --teardown cleans",
    )
    we.add_argument(
        "--engine-container",
        default=None,
        help="k8s: container in that Deployment (default: its default container)",
    )
    we.add_argument("--container", default=DEFAULT_ENGINE, help="docker: the engine container")
    we.add_argument(
        "--compose-dir",
        default=None,
        help="docker: compose project directory the engine is recreated from",
    )
    we.add_argument("--service", default=DEFAULT_ENGINE, help="docker: the engine's service name")
    we.add_argument(
        "--env-file",
        default=None,
        help="docker: env file the engine reads (default <compose-dir>/env/engine.env)",
    )
    we.add_argument(
        "--teardown",
        action="store_true",
        help="remove exactly what an earlier wire of this provider added",
    )
    we.add_argument(
        "--dry-run", action="store_true", help="print the plan, secret redacted, and touch nothing"
    )
    we.set_defaults(func=cmd_idp_wire_external)
