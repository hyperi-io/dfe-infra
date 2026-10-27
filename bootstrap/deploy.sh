#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         deploy.sh
#  Purpose:      Single-command DFE deployment (tofu -> bridge -> bootstrap)
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly SCRIPT_DIR REPO_ROOT

# Defaults
CLOUD="${DFE_CLOUD:-local}"
TF_DIR="${REPO_ROOT}/terraform/environments/${CLOUD}"
DRY_RUN="${DFE_DRY_RUN:-false}"

usage() {
    echo "Usage: ${0} [OPTIONS]"
    echo ""
    echo "Deploy DFE to a target cluster. Runs tofu apply + bootstrap."
    echo ""
    echo "Options:"
    echo "  --cloud CLOUD     Target cloud: local, aws, gcp, azure (default: local)"
    echo "  --tf-dir DIR      OpenTofu environment dir (default: auto from --cloud)"
    echo "  --dry-run         Show what would happen without executing"
    echo "  --skip-terraform  Skip the tofu apply (use existing state)"
    echo "  --help            Show this help"
    echo ""
    echo "Env vars (for tofu):"
    echo "  DFE_SECRETS_BACKEND  openbao or none (local; empty: openbao when vault_addr is set)"
    echo "  VAULT_TOKEN          OpenBao token (local, openbao only)"
    echo "  AWS_PROFILE          AWS profile (aws)"
    echo ""
    echo "Examples:"
    echo "  ${0} --cloud local                    # Deploy to local Rancher cluster"
    echo "  ${0} --cloud aws --tf-dir envs/prod   # Deploy to AWS"
    echo "  ${0} --dry-run                        # Dry run"
}

SKIP_TF=false

while [[ $# -gt 0 ]]; do
    case "${1}" in
        --cloud) CLOUD="${2}"; TF_DIR="${REPO_ROOT}/terraform/environments/${2}"; shift 2 ;;
        --tf-dir) TF_DIR="${2}"; shift 2 ;;
        --dry-run) DRY_RUN=true; export DFE_DRY_RUN=true; shift ;;
        --skip-terraform) SKIP_TF=true; shift ;;
        --help) usage; exit 0 ;;
        *) echo "Unknown option: ${1}"; usage; exit 1 ;;
    esac
done

echo "=== DFE Deploy ==="
echo "  Cloud:     ${CLOUD}"
echo "  TF dir:    ${TF_DIR}"
echo "  Dry run:   ${DRY_RUN}"
echo ""

if [[ ! -d "${TF_DIR}" ]]; then
    echo "ERROR: OpenTofu directory not found: ${TF_DIR}" >&2
    exit 1
fi

# An operator .env in the OpenTofu environment dir sets any DFE_* input
# bootstrap.sh reads. Sourced before OpenTofu, which takes DFE_SECRETS_BACKEND too.
if [[ -f "${TF_DIR}/.env" ]]; then
  set -a
  # shellcheck source=/dev/null # the operator's own file, absent from the repo
  source "${TF_DIR}/.env"
  set +a
fi

# One store choice for the root and bootstrap. A root that declares no
# secrets_backend ignores it.
if [[ -n "${DFE_SECRETS_BACKEND:-}" ]]; then
  export TF_VAR_secrets_backend="${DFE_SECRETS_BACKEND}"
fi

# Step 1: OpenTofu
if [[ "${SKIP_TF}" == "false" ]]; then
    echo "==> Step 1: OpenTofu apply"
    cd "${TF_DIR}"
    tofu init -input=false
    if [[ "${DRY_RUN}" == "true" ]]; then
        tofu plan
    else
        tofu apply -auto-approve
    fi
    cd "${REPO_ROOT}"
else
    echo "==> Step 1: OpenTofu (skipped)"
fi

# Step 2: Bridge + Bootstrap
echo "==> Step 2: Bootstrap"
BRIDGE_ARGS=("--tf-dir" "${TF_DIR}")
if [[ "${DRY_RUN}" == "true" ]]; then
    BRIDGE_ARGS+=("--dry-run")
fi
python3 "${SCRIPT_DIR}/bridge.py" "${BRIDGE_ARGS[@]}"

echo ""
echo "=== Deploy complete ==="
echo ""
echo "Run smoke tests:"
echo "  bash bootstrap/smoke-test.sh          # Layer 1"
echo "  bash bootstrap/smoke-test-data.sh     # Data platform"
echo "  bash bootstrap/smoke-test-auth.sh     # Auth & ingress"
echo "  bash bootstrap/smoke-test-keda.sh     # KEDA"
echo ""
echo "To teardown: bash bootstrap/destroy.sh"
