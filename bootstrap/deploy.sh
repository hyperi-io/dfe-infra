#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         deploy.sh
#  Purpose:      Single-command DFE deployment (terraform → bridge → bootstrap)
#  Language:     Bash
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Defaults
CLOUD="${DFE_CLOUD:-local}"
TF_DIR="${REPO_ROOT}/terraform/environments/${CLOUD}"
DRY_RUN="${DFE_DRY_RUN:-false}"

usage() {
    echo "Usage: ${0} [OPTIONS]"
    echo ""
    echo "Deploy DFE to a target cluster. Runs terraform apply + bootstrap."
    echo ""
    echo "Options:"
    echo "  --cloud CLOUD     Target cloud: local, aws, gcp, az (default: local)"
    echo "  --tf-dir DIR      Terraform environment dir (default: auto from --cloud)"
    echo "  --dry-run         Show what would happen without executing"
    echo "  --skip-terraform  Skip terraform apply (use existing state)"
    echo "  --help            Show this help"
    echo ""
    echo "Required env vars (for terraform):"
    echo "  VAULT_TOKEN       OpenBao/Vault token (local)"
    echo "  AWS_PROFILE       AWS profile (aws)"
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
    echo "ERROR: Terraform directory not found: ${TF_DIR}" >&2
    exit 1
fi

# Step 1: Terraform
if [[ "${SKIP_TF}" == "false" ]]; then
    echo "==> Step 1: Terraform apply"
    cd "${TF_DIR}"
    terraform init -input=false
    if [[ "${DRY_RUN}" == "true" ]]; then
        terraform plan
    else
        terraform apply -auto-approve
    fi
    cd "${REPO_ROOT}"
else
    echo "==> Step 1: Terraform (skipped)"
fi

# Step 1b: Pull secret credentials
# bootstrap.sh reads generic DFE_PULL_SECRET_SERVER/USER/TOKEN env vars.
# Set these before running deploy.sh, or source them from your secrets manager.
# Example (GHCR):
#   export DFE_PULL_SECRET_SERVER=ghcr.io
#   export DFE_PULL_SECRET_USER=myuser
#   export DFE_PULL_SECRET_TOKEN=$(vault kv get -field=token secret/ghcr-pat)
#
# If a .env file exists in the terraform environment dir, source it.
if [[ -f "${TF_DIR}/.env" ]]; then
  set -a; source "${TF_DIR}/.env"; set +a
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
