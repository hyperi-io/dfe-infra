#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         _oci.py
#  Purpose:      A loopback OCI registry for the tests: the anonymous Bearer
#                challenge, tag pages, manifests, and blobs served from another host.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A local registry that answers an anonymous caller the way ghcr.io does.

The client under test runs its real HTTP path against it: the 401 challenge, the
token realm, Link-paged tag lists, manifests by tag and by digest, and a blob read
that redirects to a second host refusing any request that carries a bearer token,
as presigned storage does. Only the host the client talks to differs, through
oci_registry.base_url.

    registry = FakeRegistry()
    registry.add("hyperi-io/dfe-loader", "v1.18.52", digest, created="2026-10-15T01:06:27Z")
    with registry.serving(monkeypatch):
        ...  # every oci_registry.Repository now reads this registry
"""

import contextlib
import hashlib
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import oci_registry

INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def _digest(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _encode(document: dict) -> bytes:
    return json.dumps(document, sort_keys=True).encode("utf-8")


@dataclass(slots=True)
class Request:
    """One request the registry or the blob store received."""

    method: str
    path: str
    authorization: str


@dataclass(slots=True)
class FakeRegistry:
    """Repositories, their manifests and blobs, and every request made of them.

    Attributes:
        page_size: Tags per tag-list page the registry serves, whatever ``n`` asks.
        fail: Statuses answered, in order, before any registry request is served.
        basic: Challenge with Basic rather than Bearer, as a private registry does.
        requests: Every request received, in order.
    """

    page_size: int = 1000
    fail: list[int] = field(default_factory=list)
    basic: bool = False
    requests: list[Request] = field(default_factory=list)
    _tags: dict[str, dict[str, str]] = field(default_factory=dict)
    _manifests: dict[str, dict[str, tuple[str, bytes]]] = field(default_factory=dict)
    _blobs: dict[str, bytes] = field(default_factory=dict)

    def add(
        self,
        repository: str,
        tag: str,
        digest: str,
        *,
        created: str | None = None,
        built: str | None = None,
        kind: str = INDEX,
    ) -> None:
        """Publish ``tag`` as a manifest the registry reports as ``digest``.

        Args:
            repository: The repository path, e.g. ``hyperi-io/dfe-loader``.
            tag: The tag to publish.
            digest: What Docker-Content-Digest reports for the tag's manifest.
            created: The org.opencontainers.image.created annotation, if any.
            built: The image config's ``created``, reached through the index's
                platform image when there is no annotation.
            kind: INDEX for a multi-arch image, MANIFEST for a single one (a chart).
        """
        config = self._blob({"architecture": "amd64", "os": "linux", "created": built})
        annotations = {oci_registry.CREATED: created} if created else {}
        image = {"mediaType": MANIFEST, "config": {"digest": config}, "layers": []}
        if kind == MANIFEST:
            document = {**image, "annotations": annotations}
        else:
            platform = self._manifest(repository, _encode(image))
            unknown = {"os": "unknown", "architecture": "unknown"}
            attestation = {"digest": _digest(b"attestation"), "platform": unknown}
            entry = {"digest": platform, "platform": {"os": "linux", "architecture": "amd64"}}
            entries = [attestation, entry]
            document = {"mediaType": INDEX, "manifests": entries, "annotations": annotations}
        self._manifests.setdefault(repository, {})[tag] = (digest, _encode(document))
        self._tags.setdefault(repository, {})[tag] = digest

    def tag_only(self, repository: str, tag: str) -> None:
        """List ``tag`` with no manifest behind it."""
        self._tags.setdefault(repository, {})[tag] = ""

    def token_requests(self) -> list[Request]:
        """Every request made of the token realm."""
        return [r for r in self.requests if r.path.startswith("/token")]

    def _blob(self, document: dict) -> str:
        body = _encode(document)
        self._blobs[_digest(body)] = body
        return _digest(body)

    def _manifest(self, repository: str, body: bytes) -> str:
        digest = _digest(body)
        self._manifests.setdefault(repository, {})[digest] = (digest, body)
        return digest

    @contextlib.contextmanager
    def serving(self, monkeypatch=None) -> Iterator[str]:
        """Serve the registry and its blob store, and point oci_registry at them.

        Args:
            monkeypatch: Undoes the repointing at the test's end; without one it is
                undone when the block exits.

        Yields:
            The registry's base URL.
        """
        store = ThreadingHTTPServer(("127.0.0.1", 0), _store_handler(self))
        registry = ThreadingHTTPServer(("127.0.0.1", 0), _registry_handler(self, store.server_port))
        servers = (store, registry)
        # A short poll, because shutdown() waits out the serve loop's current one.
        threads = [
            threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            for s in servers
        ]
        for thread in threads:
            thread.start()
        base = f"http://127.0.0.1:{registry.server_port}"
        replaced = {"base_url": lambda host: base, "_sleep": lambda seconds: None}
        original = {name: getattr(oci_registry, name) for name in replaced}
        for name, value in replaced.items():
            if monkeypatch is None:
                setattr(oci_registry, name, value)
            else:
                monkeypatch.setattr(oci_registry, name, value)
        try:
            yield base
        finally:
            if monkeypatch is None:
                for name, value in original.items():
                    setattr(oci_registry, name, value)
            for server in (registry, store):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=5)


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        return

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, code: str, headers: dict[str, str] | None = None) -> None:
        self._send(status, _encode({"errors": [{"code": code, "message": code.lower()}]}), headers)


def _store_handler(fake: FakeRegistry) -> type[BaseHTTPRequestHandler]:
    class Store(_Quiet):
        def do_GET(self) -> None:
            fake.requests.append(Request("GET", self.path, self.headers.get("Authorization", "")))
            if self.headers.get("Authorization"):
                self._error(400, "INVALID_ARGUMENT")
                return
            body = fake._blobs.get(self.path.lstrip("/"))
            if body is None:
                self._error(404, "BLOB_UNKNOWN")
                return
            self._send(200, body)

    return Store


def _registry_handler(fake: FakeRegistry, store_port: int) -> type[BaseHTTPRequestHandler]:
    class Registry(_Quiet):
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            fake.requests.append(Request("GET", self.path, self.headers.get("Authorization", "")))
            if fake.fail:
                self._error(fake.fail.pop(0), "BUSY")
                return
            if url.path == "/token":
                self._token(parse_qs(url.query))
                return
            repository, action, rest = self._split(url.path)
            if repository is None:
                self._error(404, "NAME_UNKNOWN")
                return
            if self.headers.get("Authorization") != f"Bearer anon-{repository}":
                self._challenge(repository)
                return
            if action == "tags":
                self._tag_page(repository, parse_qs(url.query))
            elif action == "manifests":
                found = fake._manifests.get(repository, {}).get(rest)
                if found is None:
                    self._error(404, "MANIFEST_UNKNOWN")
                    return
                digest, body = found
                media = json.loads(body).get("mediaType", MANIFEST)
                self._send(200, body, {"Content-Type": media, "Docker-Content-Digest": digest})
            else:
                self._send(307, b"", {"Location": f"http://localhost:{store_port}/{rest}"})

        @staticmethod
        def _split(path: str) -> tuple[str | None, str, str]:
            markers = (("/tags/list", "tags"), ("/manifests/", "manifests"), ("/blobs/", "blobs"))
            for marker, action in markers:
                head, found, rest = path.partition(marker)
                if found and head.startswith("/v2/"):
                    return head.removeprefix("/v2/"), action, rest
            return None, "", ""

        def _challenge(self, repository: str) -> None:
            realm = f"http://127.0.0.1:{self.server.server_port}/token"
            if fake.basic:
                header = 'Basic realm="fake"'
            else:
                header = f'Bearer realm="{realm}",service="fake",scope="repository:{repository}:pull"'
            self._error(401, "UNAUTHORIZED", {"WWW-Authenticate": header})

        def _token(self, query: dict[str, list[str]]) -> None:
            scope = (query.get("scope") or [""])[0]
            repository = scope.removeprefix("repository:").removesuffix(":pull")
            if repository not in fake._tags or self.headers.get("Authorization"):
                self._error(403, "DENIED")
                return
            self._send(200, _encode({"token": f"anon-{repository}"}))

        def _tag_page(self, repository: str, query: dict[str, list[str]]) -> None:
            tags = sorted(fake._tags.get(repository, {}))
            last = (query.get("last") or [""])[0]
            start = tags.index(last) + 1 if last in tags else 0
            page = tags[start : start + fake.page_size]
            headers = {"Content-Type": "application/json"}
            if start + fake.page_size < len(tags):
                following = f"/v2/{repository}/tags/list?last={page[-1]}&n={fake.page_size}"
                headers["Link"] = f'<{following}>; rel="next"'
            self._send(200, _encode({"name": repository, "tags": page}), headers)

    return Registry
