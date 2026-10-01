#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         bootstrap/scripts/ghcr-token.py
#  Purpose:      Mint a short-lived GitHub App installation token for pulling
#                the DFE images, with every organisation-specific value
#                supplied by the caller rather than baked in.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Mint a GitHub App installation token for registry access.

Reads the app's private key, signs a JWT with it, exchanges that for an
installation token and prints the token to stdout. It is a helper an operator
runs, not a bootstrap step: bootstrap.sh takes the token ready-made in
DFE_PULL_SECRET_TOKEN and creates the imagePullSecret from that.

    DFE_GHCR_APP_CLIENT_ID=Iv23... DFE_GHCR_APP_INSTALLATION_ID=123 \
        python3 ghcr-token.py --pem /path/to/app.pem

Which App, which installation, and where its key lives are properties of the
DEPLOYMENT, not of this repo: another organisation running the suite has its
own App. So there are no defaults -- an unset value stops the run naming the
variable, rather than authenticating as somebody else's App. Bind them in the
environment or a wrapper, never here.

    DFE_GHCR_APP_CLIENT_ID          the App's client id (Iv23...), which is
                                    what the JWT `iss` takes; the numeric app
                                    id still works and is the older spelling
    DFE_GHCR_APP_INSTALLATION_ID    the installation the token is minted for
    DFE_GHCR_APP_PEM                the private key itself, or --pem FILE
    DFE_GHCR_APP_SECRET_CMD         a command printing the key on stdout, for
                                    a deployment that keeps it in a secret store
"""

import argparse
import base64
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

GITHUB_API = os.environ.get("DFE_GITHUB_API", "https://api.github.com")


def required(name: str) -> str:
    """An environment value with no sensible default, read or refused."""
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(
            f"ERROR: {name} is not set. It names this deployment's GitHub App, "
            f"which this repo cannot know -- set it in the environment or a wrapper."
        )
    return value


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def load_pem(pem_file: Path | None) -> str:
    """The App private key, from a file, the environment, or a secret command."""
    if pem_file:
        return pem_file.read_text(encoding="utf-8")
    if os.environ.get("DFE_GHCR_APP_PEM"):
        return os.environ["DFE_GHCR_APP_PEM"]

    command = os.environ.get("DFE_GHCR_APP_SECRET_CMD", "").strip()
    if not command:
        sys.exit(
            "ERROR: no private key. Pass --pem FILE, set DFE_GHCR_APP_PEM, or set "
            "DFE_GHCR_APP_SECRET_CMD to a command that prints the key on stdout."
        )
    result = subprocess.run(
        shlex.split(command), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        sys.exit(f"ERROR: DFE_GHCR_APP_SECRET_CMD failed: {result.stderr.strip()}")
    if not result.stdout.strip():
        sys.exit("ERROR: DFE_GHCR_APP_SECRET_CMD printed nothing on stdout.")
    return result.stdout


def mint_token(pem: str, client_id: str, installation_id: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(pem.encode(), password=None)
    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": client_id}).encode())
    signature = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    jwt = f"{header}.{payload}.{b64url(signature)}"

    request = urllib.request.Request(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())["token"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pem", type=Path, help="the App private key file")
    args = parser.parse_args()

    client_id = required("DFE_GHCR_APP_CLIENT_ID")
    installation_id = required("DFE_GHCR_APP_INSTALLATION_ID")
    print(mint_token(load_pem(args.pem), client_id, installation_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
