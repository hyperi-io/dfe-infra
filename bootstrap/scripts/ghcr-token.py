#!/usr/bin/env python3
"""Mint a GitHub App installation token for GHCR access.

Reads the app PEM from OpenBao (or local file fallback) and prints
a short-lived installation token to stdout. Used by bootstrap.sh
to create K8s imagePullSecrets.

Usage:
    python3 ghcr-token.py                    # PEM from OpenBao
    python3 ghcr-token.py --pem /path/to.pem # PEM from file
"""

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

APP_ID = "3230495"
INSTALLATION_ID = "120265899"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def load_pem() -> str:
    """Load PEM from CLI arg, env, or OpenBao."""
    # CLI arg
    if "--pem" in sys.argv:
        idx = sys.argv.index("--pem") + 1
        return Path(sys.argv[idx]).read_text()

    # Env var
    if os.environ.get("GHCR_APP_PEM"):
        return os.environ["GHCR_APP_PEM"]

    # OpenBao via bao-admin
    bao_admin = (
        Path(__file__).resolve().parent.parent.parent.parent / "hyperi-infra/scripts/bao-admin"
    )
    if not bao_admin.exists():
        bao_admin = Path("/projects/hyperi-infra/scripts/bao-admin")

    result = subprocess.run(
        [str(bao_admin), "kv", "get", "-format=json", "secret/github/hyperi-container-mgt"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: Could not read PEM from OpenBao: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(result.stdout)["data"]["data"]
    return data["private_key"]


def mint_token(pem: str) -> str:
    import urllib.request

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    pk = serialization.load_pem_private_key(pem.encode(), password=None)
    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": APP_ID}).encode())
    sig = pk.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    jwt = f"{header}.{payload}.{b64url(sig)}"

    req = urllib.request.Request(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens",
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())["token"]


def main():
    pem = load_pem()
    token = mint_token(pem)
    print(token)


if __name__ == "__main__":
    main()
