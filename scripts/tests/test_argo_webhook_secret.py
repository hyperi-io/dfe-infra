#  Project:      dfe-infra
#  File:         test_argo_webhook_secret.py
#  Purpose:      Prove bootstrap.sh patches argocd-secret with the push webhook's
#                key only after Argo CD is installed or adopted, so the patch has
#                a Secret to land in on a fresh cluster.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Where bootstrap.sh writes the Argo half of the Forgejo push webhook.

On a fresh cluster argocd-secret is created by the Argo CD install in [6/7]. A
patch issued in [4c/7] finds no Secret, Argo rejects every delivery, and each
source write waits out the reconciliation poll instead of syncing on push.

    python3 -m pytest scripts/tests/test_argo_webhook_secret.py -q
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"

PATCH = "kubectl -n argocd patch secret argocd-secret"
FORGEJO_HALF = "kubectl -n forgejo create secret generic dfe-argo-webhook"
ARGO_INSTALL = "helm upgrade --install argocd argo/argo-cd"
ADOPT_BRANCH = "Using existing ArgoCD"
STEP_6 = "==> [6/7] ArgoCD"
STEP_7 = "==> [7/7] Applying ArgoCD AppProjects"


def _body() -> str:
    return BOOTSTRAP.read_text(encoding="utf-8")


def _position(body: str, marker: str) -> int:
    index = body.find(marker)
    assert index >= 0, f"bootstrap.sh no longer carries {marker!r}"
    return index


def test_the_argo_secret_is_patched_exactly_once() -> None:
    assert _body().count(PATCH) == 1


def test_the_patch_follows_both_the_install_and_the_adopt_branch() -> None:
    body = _body()
    patch = _position(body, PATCH)

    assert patch > _position(body, STEP_6)
    assert patch > _position(body, ARGO_INSTALL)
    assert patch > _position(body, ADOPT_BRANCH)


def test_the_key_is_minted_before_the_patch_reads_it() -> None:
    body = _body()

    assert _position(body, FORGEJO_HALF) < _position(body, PATCH)


def test_the_patch_lands_before_the_forgejo_app_is_registered() -> None:
    # The Forgejo setup Job registers the hook once Argo syncs it, from [7/7] on.
    body = _body()

    assert _position(body, PATCH) < _position(body, STEP_7)


def test_the_patch_carries_the_same_guard_that_minted_the_key() -> None:
    # set -u aborts the whole bootstrap on an unset ARGO_WEBHOOK_SECRET.
    body = _body()
    guard = '[[ "${DFE_BUNDLED_DEPLOY_REPO}" == "true" ]] && [[ "${DFE_DRY_RUN:-false}" != "true" ]]'
    patch = _position(body, PATCH)
    preceding = body[:patch].rsplit("\nif ", 1)[-1].split("\n", 1)[0]

    assert guard in preceding
