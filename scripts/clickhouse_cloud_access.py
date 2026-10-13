#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/clickhouse_cloud_access.py
#  Purpose:      Open, close and sweep one guarded cloud run's entries on a
#                ClickHouse Cloud service's IP access list.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""clickhouse_cloud_access -- a cloud run's addresses on a ClickHouse Cloud service.

    python3 scripts/clickhouse_cloud_access.py open --run-id <id> --expires-at <epoch>
    python3 scripts/clickhouse_cloud_access.py close --run-id <id>
    python3 scripts/clickhouse_cloud_access.py sweep

open adds every address in DFE_CH_ACCESS_CIDRS (comma-separated /32s: the run's
NAT egress and the runner) to the service's IP access list, each described
`dfe-run:<run id>:<expiry epoch>`; an address already on the list is left as it
is. close removes every entry describing that run. sweep removes every dfe-run
entry whose expiry has passed, which is what the reaper runs. An entry with any
other description is never touched.

Configuration is environment only: DFE_CYCLE_CH_SERVICE_ID, DFE_CYCLE_CH_API_KEY_ID
and DFE_CYCLE_CH_API_KEY_SECRET, a key holding control-plane:service:manage. The
organisation is the one the key belongs to. DFE_CYCLE_CH_API_URL replaces
https://api.clickhouse.cloud, and must be https unless it names this machine.
Nothing printed carries an address, the service, the organisation or the key.

Exit 0 on success, 1 when the API refuses or cannot be reached, 2 on a missing
or malformed setting.
"""

import argparse
import base64
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import NoReturn

DEFAULT_API_URL = "https://api.clickhouse.cloud"
SERVICE_ENV = "DFE_CYCLE_CH_SERVICE_ID"
KEY_ID_ENV = "DFE_CYCLE_CH_API_KEY_ID"
KEY_SECRET_ENV = "DFE_CYCLE_CH_API_KEY_SECRET"
API_URL_ENV = "DFE_CYCLE_CH_API_URL"
CIDRS_ENV = "DFE_CH_ACCESS_CIDRS"
REQUIRED_ENV = (SERVICE_ENV, KEY_ID_ENV, KEY_SECRET_ENV)
DESCRIPTION_PREFIX = "dfe-run"
TIMEOUT_SECONDS = 30
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class AccessError(RuntimeError):
    """A setting, an address or an API answer this script refuses; the message names no secret."""


@dataclass(frozen=True, slots=True)
class Api:
    """One ClickHouse Cloud service and the key that manages it."""

    base_url: str
    service_id: str
    key_id: str
    key_secret: str


def api_from_env(env: Mapping[str, str]) -> Api:
    """The service and key, from the environment.

    Raises:
        AccessError: A setting is unset or blank (named, never echoed), or the API
            URL would carry the key in clear off this machine.
    """
    missing = [name for name in REQUIRED_ENV if not env.get(name, "").strip()]
    if missing:
        raise AccessError(f"the environment does not set {', '.join(missing)}")
    base_url = env.get(API_URL_ENV, "").strip().rstrip("/") or DEFAULT_API_URL
    parsed = urllib.parse.urlsplit(base_url)
    plain_but_local = parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS
    if parsed.scheme != "https" and not plain_but_local:
        raise AccessError(f"{API_URL_ENV} must be an https URL, since the key travels in every request")
    return Api(
        base_url=base_url,
        service_id=env[SERVICE_ENV].strip(),
        key_id=env[KEY_ID_ENV].strip(),
        key_secret=env[KEY_SECRET_ENV].strip(),
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies Authorization onto a redirect to any host, so no redirect is followed."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> NoReturn:
        raise AccessError(f"{req.get_method()} answered a {code} redirect, which is never followed with the key")


_OPENER = urllib.request.build_opener(_NoRedirect)


def _call(api: Api, method: str, path: str, label: str, body: object | None = None) -> dict:
    """One API call; `label` names it in an error, since `path` carries the ids."""
    token = base64.b64encode(f"{api.key_id}:{api.key_secret}".encode()).decode()
    request = urllib.request.Request(
        api.base_url + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            answer = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise AccessError(f"{label} answered HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise AccessError(f"{label} could not reach the API: {type(reason).__name__}") from None
    except json.JSONDecodeError:
        raise AccessError(f"{label} answered something that is not JSON") from None
    if not isinstance(answer, dict):
        raise AccessError(f"{label} answered something that is not a JSON object")
    return answer


def organisation(api: Api) -> str:
    """The id of the one organisation the key belongs to.

    Raises:
        AccessError: The key reaches no organisation, or more than one.
    """
    result = _call(api, "GET", "/v1/organizations", "listing the key's organisation").get("result")
    ids = [org.get("id") for org in result or [] if isinstance(org, dict) and org.get("id")]
    if len(ids) != 1:
        raise AccessError(f"the key reaches {len(ids)} organisations, and one organisation is what names the service")
    return str(ids[0])


def _service_path(api: Api, org: str) -> str:
    return f"/v1/organizations/{urllib.parse.quote(org)}/services/{urllib.parse.quote(api.service_id)}"


def access_list(api: Api, org: str) -> list[dict[str, str]]:
    """The service's IP access list as the API holds it."""
    result = _call(api, "GET", _service_path(api, org), "reading the service").get("result") or {}
    entries = result.get("ipAccessList") if isinstance(result, dict) else None
    return [e for e in entries or [] if isinstance(e, dict) and isinstance(e.get("source"), str)]


def _patch(api: Api, org: str, add: list[dict[str, str]], remove: list[dict[str, str]]) -> None:
    body = {"ipAccessList": {"add": add, "remove": remove}}
    _call(api, "PATCH", _service_path(api, org), "updating the access list", body)


def description(run_id: str, expires_at: int) -> str:
    """The description that names an entry's run and when it may be swept."""
    return f"{DESCRIPTION_PREFIX}:{run_id}:{int(expires_at)}"


def parse_description(text: str) -> tuple[str, int] | None:
    """(run id, expiry) from an entry this script wrote, or None for any other entry."""
    prefix, _, rest = text.partition(":")
    run_id, _, expiry = rest.rpartition(":")
    if prefix != DESCRIPTION_PREFIX or not run_id or not expiry.isdigit():
        return None
    return run_id, int(expiry)


def _host_address(cidr: str) -> str:
    """`cidr` as one IPv4 address's /32, the only width a run opens.

    Raises:
        AccessError: Anything wider, an address with no prefix, or IPv6.
    """
    try:
        network = ipaddress.ip_network(cidr.strip(), strict=True)
    except ValueError:
        network = None
    if network is None or network.version != 4 or network.prefixlen != 32 or "/" not in cidr:
        raise AccessError("every address a run opens is one IPv4 address written as a /32")
    return str(network)


def open_access(api: Api, run_id: str, expires_at: int, cidrs: Sequence[str]) -> int:
    """Add each address for this run; return how many were added.

    Raises:
        AccessError: No address, one that is not a /32, or an API failure.
    """
    if not cidrs:
        raise AccessError(f"no address to open -- {CIDRS_ENV} is empty")
    wanted = list(dict.fromkeys(_host_address(cidr) for cidr in cidrs))
    org = organisation(api)
    present = {entry["source"] for entry in access_list(api, org)}
    note = description(run_id, expires_at)
    add = [{"source": source, "description": note} for source in wanted if source not in present]
    if add:
        _patch(api, org, add, [])
    return len(add)


def _remove(api: Api, keep: Callable[[str, int], bool]) -> int:
    """Remove every dfe-run entry `keep(run_id, expiry)` rejects; return how many."""
    org = organisation(api)
    remove = []
    for entry in access_list(api, org):
        parsed = parse_description(entry.get("description", ""))
        if parsed is not None and not keep(*parsed):
            remove.append({"source": entry["source"], "description": entry.get("description", "")})
    if remove:
        _patch(api, org, [], remove)
    return len(remove)


def close_access(api: Api, run_id: str) -> int:
    """Remove every entry this run opened; return how many."""
    return _remove(api, lambda owner, _expiry: owner != run_id)


def sweep(api: Api, now: float) -> int:
    """Remove every dfe-run entry whose expiry has passed; return how many."""
    return _remove(api, lambda _owner, expiry: expiry > now)


def _cidrs(env: Mapping[str, str]) -> list[str]:
    return [part.strip() for part in env.get(CIDRS_ENV, "").split(",") if part.strip()]


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="clickhouse_cloud_access.py",
        description="Open, close or sweep a cloud run's entries on a ClickHouse Cloud service's IP access list.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    opened = sub.add_parser("open", help=f"add the addresses in {CIDRS_ENV} for one run")
    opened.add_argument("--run-id", required=True, help="the run the entries belong to")
    opened.add_argument("--expires-at", required=True, type=int, help="epoch seconds after which sweep removes them")
    closed = sub.add_parser("close", help="remove one run's entries")
    closed.add_argument("--run-id", required=True, help="the run whose entries go")
    sub.add_parser("sweep", help="remove every run entry whose expiry has passed")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env

    try:
        api = api_from_env(env)
    except AccessError as exc:
        print(f"clickhouse_cloud_access: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "open":
            added = open_access(api, args.run_id, args.expires_at, _cidrs(env))
            print(f"clickhouse_cloud_access: opened {added} address(es) for run {args.run_id}", file=sys.stderr)
        elif args.command == "close":
            removed = close_access(api, args.run_id)
            print(f"clickhouse_cloud_access: closed {removed} address(es) for run {args.run_id}", file=sys.stderr)
        else:
            removed = sweep(api, time.time())
            print(f"clickhouse_cloud_access: swept {removed} expired run address(es)", file=sys.stderr)
    except AccessError as exc:
        print(f"clickhouse_cloud_access: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
