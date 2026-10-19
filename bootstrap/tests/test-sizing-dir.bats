#!/usr/bin/env bats
# resolve_sizing.py writes every artefact under its own --out, and `sizing/`
# hangs off that directory rather than off the repo root. The deployment docs
# tell an AWS operator to resolve with --out terraform/environments/aws, so a
# bootstrap that looks in the repo's own sizing/ finds nothing there.
#
# The pools half is refused rather than omitted: with no pools the annotation
# is missing, and the node-pools chart then fails the sync of a Karpenter
# cluster instead of pruning its NodePools.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    SCRIPT="${REPO_ROOT}/bootstrap/bootstrap.sh"
    # bootstrap.sh's own pools guard, cut out of the script as it ships, run under its strict mode.
    GUARD="${BATS_TEST_TMPDIR}/pools-guard.sh"
    {
        echo 'set -euo pipefail'
        sed -n '/^# A cluster with a.*Karpenter/,/^fi$/p' "${SCRIPT}"
        echo 'echo "pools guard passed"'
    } > "${GUARD}"
}

# The guard run with no Karpenter fact and no pools, then the facts in ${@}.
run_guard() {
    run env -i PATH="${PATH}" DFE_NODE_POOLS="" DFE_SIZING_DIR=/srv/resolve \
        DFE_NODE_POOLS_FILE=/srv/resolve/sizing/scale.pools.json "$@" bash "${GUARD}"
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
    run grep -F 'DFE_NODE_POOLS_FILE="${DFE_SIZING_DIR}/sizing/${DFE_PROFILE}.pools.json"' "${SCRIPT}"
    [ "$status" -eq 0 ]
    run grep -F 'DFE_SIZING_NODES_FILE="${DFE_SIZING_DIR}/sizing/${DFE_PROFILE}.nodes.json"' "${SCRIPT}"
    [ "$status" -eq 0 ]
}

@test "neither is read from the repo root any more" {
    run grep -F '"${REPO_ROOT}/sizing/${DFE_PROFILE}.' "${SCRIPT}"
    [ "$status" -ne 0 ]
}

@test "a Karpenter cluster with no pools is refused rather than deployed" {
    grep -qF 'no NodePools were found' "${GUARD}"
    run_guard DFE_KARPENTER_DISCOVERY_TAG=dfe-test
    [ "$status" -eq 1 ]
    [[ "${output}" == *"this cluster runs Karpenter and no NodePools were found"* ]]
}

@test "any one Karpenter fact without the pools is refused, not only the discovery tag" {
    local fact
    for fact in DFE_KARPENTER_INSTANCE_PROFILE=dfe-test \
        DFE_KARPENTER_KMS_KEY_ID=arn:aws:kms:us-west-2:000000000000:key/abc; do
        run_guard "${fact}"
        [ "$status" -eq 1 ]
        [[ "${output}" == *"no NodePools were found"* ]]
    done
}

@test "no Karpenter fact, or the pools beside them, passes the guard" {
    run_guard
    [ "$status" -eq 0 ]
    [[ "${output}" == "pools guard passed" ]]
    run_guard DFE_KARPENTER_DISCOVERY_TAG=dfe-test DFE_KARPENTER_INSTANCE_PROFILE=dfe-test \
        DFE_KARPENTER_KMS_KEY_ID=arn:aws:kms:us-west-2:000000000000:key/abc DFE_NODE_POOLS='{"renderer":"karpenter-aws"}'
    [ "$status" -eq 0 ]
    [[ "${output}" == "pools guard passed" ]]
}

@test "the refusal names the path it looked in" {
    run grep -F 'Looked for: ${DFE_NODE_POOLS_FILE}' "${SCRIPT}"
    [ "$status" -eq 0 ]
}

@test "a cluster without Karpenter is not refused" {
    # The guard is gated on the discovery tag, which only a Karpenter-enabled
    # cluster module emits -- an on-prem deploy has no pools and needs none.
    run grep -c 'DFE_KARPENTER_DISCOVERY_TAG' "${SCRIPT}"
    [ "$status" -eq 0 ]
    [ "$output" -ge 3 ]
}
