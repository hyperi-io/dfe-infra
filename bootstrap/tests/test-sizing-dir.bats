#!/usr/bin/env bats
# resolve_sizing.py writes every artefact under its own --out, and `sizing/`
# hangs off that directory rather than off the repo root. The deployment docs
# tell an AWS operator to resolve with --out terraform/environments/aws, so a
# bootstrap that looks in the repo's own sizing/ finds nothing there.
#
# The Karpenter half fails silently: with no pools the annotation is omitted,
# the chart falls through to its empty default, the Application reports Synced
# and Healthy with no resources, and every workload the fixed managed node
# groups cannot fit stays Pending.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    SCRIPT="${REPO_ROOT}/bootstrap/bootstrap.sh"
}

@test "the sizing directory defaults to the cloud's own terraform root" {
    run grep -F 'DFE_SIZING_DIR_DEFAULT="${REPO_ROOT}/terraform/environments/${DFE_CLOUD}"' "${SCRIPT}"
    [ "$status" -eq 0 ]
}

@test "a cloud with no terraform root keeps the resolver's own default" {
    run grep -F 'DFE_SIZING_DIR_DEFAULT="${REPO_ROOT}"' "${SCRIPT}"
    [ "$status" -eq 0 ]
}

@test "an operator can point it at wherever the resolve ran" {
    run grep -F 'export DFE_SIZING_DIR="${DFE_SIZING_DIR:-${DFE_SIZING_DIR_DEFAULT}}"' "${SCRIPT}"
    [ "$status" -eq 0 ]
}

@test "both sizing artefacts are read from that directory, not the repo root" {
    run grep -F 'DFE_KARPENTER_POOLS_FILE="${DFE_SIZING_DIR}/sizing/${DFE_PROFILE}.karpenter.json"' "${SCRIPT}"
    [ "$status" -eq 0 ]
    run grep -F 'DFE_SIZING_NODES_FILE="${DFE_SIZING_DIR}/sizing/${DFE_PROFILE}.nodes.json"' "${SCRIPT}"
    [ "$status" -eq 0 ]
}

@test "neither is read from the repo root any more" {
    run grep -F '"${REPO_ROOT}/sizing/${DFE_PROFILE}.' "${SCRIPT}"
    [ "$status" -ne 0 ]
}

@test "a Karpenter cluster with no pools is refused rather than deployed" {
    run grep -F 'if [[ -n "${DFE_KARPENTER_DISCOVERY_TAG:-}" && -z "${DFE_KARPENTER_POOLS}" ]]; then' "${SCRIPT}"
    [ "$status" -eq 0 ]
}

@test "the refusal names the path it looked in" {
    run grep -F 'Looked for: ${DFE_KARPENTER_POOLS_FILE}' "${SCRIPT}"
    [ "$status" -eq 0 ]
}

@test "a cluster without Karpenter is not refused" {
    # The guard is gated on the discovery tag, which only a Karpenter-enabled
    # cluster module emits -- an on-prem deploy has no pools and needs none.
    run grep -c 'DFE_KARPENTER_DISCOVERY_TAG' "${SCRIPT}"
    [ "$status" -eq 0 ]
    [ "$output" -ge 3 ]
}
