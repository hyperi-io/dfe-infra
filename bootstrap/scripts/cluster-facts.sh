#  Project:      dfe-infra
#  File:         cluster-facts.sh
#  Purpose:      Read the deployment facts bootstrap.sh stamps on the dfe-cluster Secret.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

# Sourced, never executed, so it carries a shell directive not a shebang.
# shellcheck shell=bash

# One dfe.hyperi.io/* annotation off the dfe-cluster Secret's metadata, never its data.
# Empty when the Secret is unreadable, and always status 0 so a caller under `set -e` survives.
cluster_fact() {
    kubectl -n argocd get secret dfe-cluster -o "jsonpath={.metadata.annotations.dfe\.hyperi\.io/${1}}" 2>/dev/null || true
}
