#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         smoke-test-transforms.sh
#  Purpose:      Verify each transform instance mounts the files the engine lists
#                for it, so a chart reading its file sets from another key cannot
#                pass as healthy with an empty pipeline (scripts/check_transform_files.py).
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage: ./smoke-test-transforms.sh [kubeconfig]
#  DFE_NS names the app namespace (default dfe); DFE_ENGINE_DEPLOY the engine.
set -uo pipefail

[ -n "${1:-}" ] && export KUBECONFIG="$1"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
NS_APP="${DFE_NS:-${DFE_NAMESPACE:-dfe}}"
ENGINE_DEPLOY="${DFE_ENGINE_DEPLOY:-dfe-engine}"

echo "=== transform file sets: the engine's list against each instance's mount (ns/${NS_APP}) ==="
python3 "${SCRIPT_DIR}/../scripts/check_transform_files.py" --namespace "${NS_APP}" --engine "deploy/${ENGINE_DEPLOY}"
