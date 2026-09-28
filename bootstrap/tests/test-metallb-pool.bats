#!/usr/bin/env bats
# MetalLB hands out no address it holds no pool for, so on a bare on-prem
# cluster this template is the whole difference between the gateway and the
# receiver holding their published addresses and both sitting Pending.
# Addresses here are TEST-NET-1 (RFC 5737), never an estate value.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    TPL="${REPO_ROOT}/bootstrap/templates/metallb-pool.yaml.tpl"
    export DFE_GATEWAY_IP="192.0.2.10"
    export DFE_RECEIVER_IP="192.0.2.11"
}

@test "both front-door addresses render as single-address /32 entries" {
    run envsubst < "${TPL}"
    [ "$status" -eq 0 ]
    [[ "$output" == *'"192.0.2.10/32"'* ]]
    [[ "$output" == *'"192.0.2.11/32"'* ]]
}

@test "nothing is left unsubstituted" {
    run envsubst < "${TPL}"
    [ "$status" -eq 0 ]
    [[ ! "$output" == *'${'* ]]
}

@test "the pool and its advertisement render into metallb-system" {
    run envsubst < "${TPL}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"kind: IPAddressPool"* ]]
    [[ "$output" == *"kind: L2Advertisement"* ]]
    [[ "$(grep -c 'namespace: metallb-system' <<<"$output")" -eq 2 ]]
}

@test "the addresses are only handed to a Service that asks for them" {
    run envsubst < "${TPL}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"autoAssign: false"* ]]
}

@test "the advertisement names the pool it advertises" {
    run envsubst < "${TPL}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"ipAddressPools:"* ]]
    [[ "$(grep -c 'dfe-front-door' <<<"$output")" -eq 3 ]]
}

@test "an unset address renders a bare /32, which is why bootstrap.sh guards" {
    run env DFE_GATEWAY_IP="" DFE_RECEIVER_IP="192.0.2.11" envsubst < "${TPL}"
    [ "$status" -eq 0 ]
    [[ "$output" == *'"/32"'* ]]
}
