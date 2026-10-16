#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         oci_registry.py
#  Purpose:      Read a public OCI repository anonymously -- its tags, manifests
#                and config blobs -- with no credential and no gh.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Anonymous reads from an OCI distribution registry, the way `docker pull` makes them.

Every DFE image and chart is public, so nothing here takes a credential. A
registry still wants a bearer token on each request: it answers the first one
401 with a `WWW-Authenticate: Bearer realm=...` challenge, and the realm issues a
pull-scoped token to anyone who asks. That is the OCI distribution token flow, so
nothing here is specific to ghcr.io, and none of it needs the GitHub Packages REST
API, which wants `read:packages` even for a public package.

    repo = Repository("ghcr.io", "hyperi-io/dfe-loader")
    repo.tags()                  # every tag, across the Link pages
    repo.manifest("v1.18.52")    # (digest, document), or None when the tag is absent
    repo.created(document)       # when the image was built, or None

A repository the registry will not hand an anonymous token for reads as absent:
for a public stack, unreadable by an outsider and unpublished are the same verdict.

stdlib only.
"""

import datetime
import hashlib
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

TIMEOUT_SECONDS = 30.0
TAGS_PAGE = 1000

# Every manifest shape a DFE image or chart is published as.
MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)

CREATED = "org.opencontainers.image.created"

# A busy or rate-limiting registry, worth another attempt after these waits (before jitter).
_TRANSIENT = frozenset({429, 500, 502, 503, 504})
_WAITS = (1.0, 2.0)

# Module attributes so tests replace them with instant, deterministic stand-ins.
_sleep = time.sleep
_random = random.random

_CHALLENGE_PARAM = re.compile(r'(\w+)="([^"]*)"')
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')


class RegistryReadError(RuntimeError):
    """A registry read that did not answer, as opposed to one that found nothing."""


class _AbsentError(Exception):
    """The registry grants no anonymous pull of the repository."""


def base_url(host: str) -> str:
    """The URL a registry host is read at."""
    return f"https://{host}"


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect, without the bearer token once it leaves the registry's host.

    A blob is served by a redirect to storage that rejects a second auth mechanism
    beside its presigned URL, and urllib would otherwise carry the header there.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        leaving = urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc
        if new is not None and leaving:
            new.remove_header("Authorization")
        return new


def _detail(exc: urllib.error.HTTPError) -> str:
    """The first error message a registry put in its refusal, else the HTTP reason."""
    try:
        body = json.loads(exc.read() or b"{}")
    except (OSError, ValueError):
        body = {}
    finally:
        exc.close()
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or errors[0].get("code") or exc.reason)
    return str(exc.reason)


def _object(body: bytes, what: str) -> dict:
    """A response body as a JSON object, or RegistryReadError naming what it was."""
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise RegistryReadError(f"{what} is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RegistryReadError(f"{what} is not a JSON object")
    return parsed


def _parse_time(value: object) -> datetime.datetime | None:
    """An RFC 3339 stamp as an aware UTC datetime, or None when there is none to read."""
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=datetime.UTC)


def _is_platform(entry: object) -> bool:
    """Whether an index entry is an image for a platform, not an attestation."""
    platform = entry.get("platform") if isinstance(entry, dict) else None
    if not isinstance(platform, dict):
        return False
    return "unknown" not in (platform.get("os"), platform.get("architecture"))


class Repository:
    """One repository on one registry, read with the anonymous token it issues.

    Args:
        host: The registry host, e.g. ``ghcr.io``.
        name: The repository path under it, e.g. ``hyperi-io/charts/dfe-engine``.
    """

    def __init__(self, host: str, name: str) -> None:
        self.host = host
        self.name = name
        self._token = ""
        self._opener = urllib.request.build_opener(_DropAuthOnRedirect)

    def tags(self) -> list[str]:
        """Every tag on the repository, empty when it is not published.

        Raises:
            RegistryReadError: The registry did not answer.
        """
        found: list[str] = []
        path = f"/v2/{self.name}/tags/list?n={TAGS_PAGE}"
        while path:
            try:
                got = self._get(path)
            except _AbsentError:
                return []
            if got is None:
                return found
            body, headers = got
            listed = _object(body, f"{self.host}/{self.name} tag list").get("tags")
            found += [str(tag) for tag in listed or []]
            path = self._next_page(headers.get("Link", ""))
        return found

    def manifest(self, reference: str) -> tuple[str, dict] | None:
        """The digest and document a tag or digest names, or None when it is absent.

        Raises:
            RegistryReadError: The registry did not answer, or served no JSON object.
        """
        try:
            got = self._get(f"/v2/{self.name}/manifests/{reference}", ", ".join(MANIFEST_TYPES))
        except _AbsentError:
            return None
        if got is None:
            return None
        body, headers = got
        served = headers.get("Docker-Content-Digest")
        digest = served or f"sha256:{hashlib.sha256(body).hexdigest()}"
        return digest, _object(body, f"{self.host}/{self.name}@{reference}")

    def blob(self, digest: str) -> bytes:
        """The bytes of one blob the repository's manifests reference.

        Raises:
            RegistryReadError: The blob is missing, unreadable, or not what its digest says.
        """
        try:
            got = self._get(f"/v2/{self.name}/blobs/{digest}")
        except _AbsentError:
            got = None
        if got is None:
            raise RegistryReadError(f"{self.host}/{self.name} has no blob {digest}")
        body = got[0]
        if digest.startswith("sha256:") and f"sha256:{hashlib.sha256(body).hexdigest()}" != digest:
            raise RegistryReadError(f"{self.host}/{self.name} served blob {digest} with other bytes")
        return body

    def created(self, document: dict) -> datetime.datetime | None:
        """When the image a manifest document describes was built, or None.

        Read from the ``org.opencontainers.image.created`` annotation, else from an
        index's first platform image, else from the image config's ``created``.

        Raises:
            RegistryReadError: A manifest or config the document names could not be read.
        """
        stamp = _parse_time((document.get("annotations") or {}).get(CREATED))
        if stamp is not None:
            return stamp
        entries = document.get("manifests")
        if isinstance(entries, list):
            image = next((entry for entry in entries if _is_platform(entry)), None)
            found = self.manifest(str(image["digest"])) if image else None
            return self.created(found[1]) if found else None
        config = document.get("config")
        if not isinstance(config, dict) or not config.get("digest"):
            return None
        digest = str(config["digest"])
        parsed = _object(self.blob(digest), f"{self.host}/{self.name} config {digest}")
        return _parse_time(parsed.get("created"))

    def _get(self, path: str, accept: str = "") -> tuple[bytes, Message] | None:
        """GET one registry path, None on a 404; _AbsentError when no anonymous token is granted."""
        url = base_url(self.host) + path
        renewed = False
        while True:
            headers = {"Accept": accept} if accept else {}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            try:
                return self._open(url, headers)
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and not renewed:
                    # The first request carries no token, and a held one may have expired.
                    renewed = True
                    challenge = exc.headers.get("WWW-Authenticate", "")
                    exc.close()
                    self._token = self._token_for(challenge)
                    continue
                if exc.code == 404:
                    exc.close()
                    return None
                raise RegistryReadError(
                    f"{self.host}/{self.name}: HTTP {exc.code} reading {path}: {_detail(exc)}"
                ) from exc
            except urllib.error.URLError as exc:
                raise RegistryReadError(f"cannot reach {self.host}: {exc.reason}") from exc

    def _open(self, url: str, headers: dict[str, str]) -> tuple[bytes, Message]:
        """Body and headers of one GET, retrying a busy registry; HTTPError for any refusal."""
        waits = list(_WAITS)
        while True:
            try:
                request = urllib.request.Request(url, headers=headers)
                with self._opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                    return response.read(), response.headers
            except urllib.error.HTTPError as exc:
                if exc.code not in _TRANSIENT or not waits:
                    raise
                exc.close()
            except TimeoutError:
                if not waits:
                    raise
            _sleep(waits.pop(0) * (0.5 + _random()))

    def _token_for(self, challenge: str) -> str:
        """The pull token the challenge's realm issues an anonymous caller."""
        scheme, _, params = challenge.partition(" ")
        if scheme.lower() != "bearer":
            raise RegistryReadError(
                f"{self.host} asks for {scheme or 'unnamed'} credentials to read {self.name}, "
                "so it serves no anonymous pull"
            )
        fields = dict(_CHALLENGE_PARAM.findall(params))
        realm = fields.get("realm")
        if not realm:
            raise RegistryReadError(f"{self.host} challenged without a token realm: {challenge!r}")
        query = {"scope": fields.get("scope") or f"repository:{self.name}:pull"}
        if fields.get("service"):
            query["service"] = fields["service"]
        try:
            body, _ = self._open(f"{realm}?{urllib.parse.urlencode(query)}", {})
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                exc.close()
                raise _AbsentError(self.name) from exc
            raise RegistryReadError(
                f"{self.host} token realm answered HTTP {exc.code} for {self.name}: {_detail(exc)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RegistryReadError(f"cannot reach {realm}: {exc.reason}") from exc
        issued = _object(body, f"{realm} token")
        token = issued.get("token") or issued.get("access_token")
        if not token:
            raise RegistryReadError(f"{realm} issued no token for {self.name}")
        return str(token)

    @staticmethod
    def _next_page(link: str) -> str:
        """The path of the next tags page a Link header names, or empty on the last page."""
        found = _NEXT_LINK.search(link)
        if not found:
            return ""
        target = urllib.parse.urlsplit(found.group(1))
        return f"{target.path}?{target.query}" if target.query else target.path
