#!/usr/bin/env bats
# Verify OIDC Helm templates render correctly and dfe-engine is not required.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
}

@test "envoy-gateway-config renders without OIDC (simple auth mode)" {
    run helm template test "${REPO_ROOT}/helm/charts/envoy-gateway-config/" \
        --set domain=example.com
    [ "$status" -eq 0 ]
    [[ ! "$output" =~ "SecurityPolicy" ]]
}

@test "envoy-gateway-config renders with single OIDC provider" {
    run helm template test "${REPO_ROOT}/helm/charts/envoy-gateway-config/" \
        --set domain=example.com \
        --set oidc.enabled=true \
        --set 'oidc.providers[0].name=google' \
        --set 'oidc.providers[0].issuerUrl=https://accounts.google.com' \
        --set 'oidc.providers[0].clientId=test-id' \
        --set 'oidc.providers[0].clientSecretName=dfe-oidc-google'
    [ "$status" -eq 0 ]
    [[ "$output" =~ "dfe-oidc-google" ]]
    [[ "$output" =~ "SecurityPolicy" ]]
}

@test "envoy-gateway-config renders with multiple OIDC providers" {
    run helm template test "${REPO_ROOT}/helm/charts/envoy-gateway-config/" \
        --set domain=example.com \
        --set oidc.enabled=true \
        --set 'oidc.providers[0].name=google' \
        --set 'oidc.providers[0].issuerUrl=https://accounts.google.com' \
        --set 'oidc.providers[0].clientId=g-id' \
        --set 'oidc.providers[0].clientSecretName=dfe-oidc-google' \
        --set 'oidc.providers[1].name=entra' \
        --set 'oidc.providers[1].issuerUrl=https://login.microsoftonline.com/tenant/v2.0' \
        --set 'oidc.providers[1].clientId=e-id' \
        --set 'oidc.providers[1].clientSecretName=dfe-oidc-entra'
    [ "$status" -eq 0 ]
    [[ "$output" =~ "dfe-oidc-google" ]]
    [[ "$output" =~ "dfe-oidc-entra" ]]
}

@test "jwt_authn forwards X-Oidc-Subject and X-Oidc-Groups headers" {
    run helm template test "${REPO_ROOT}/helm/charts/envoy-gateway-config/" \
        --set domain=example.com \
        --set jwtAuthn.enabled=true \
        --set jwtAuthn.issuer=https://accounts.google.com
    [ "$status" -eq 0 ]
    [[ "$output" =~ "X-Oidc-Subject" ]]
    [[ "$output" =~ "X-Oidc-Groups" ]]
    [[ "$output" =~ "X-Forwarded-User" ]]
}

@test "dfe-engine renders without OIDC (simple auth mode)" {
    run helm template test "${REPO_ROOT}/helm/charts/dfe-engine/" \
        --set global.registry=ghcr.io/test
    [ "$status" -eq 0 ]
    [[ ! "$output" =~ "DFE_OIDC" ]]
}

@test "dfe-engine renders with OIDC provider secrets" {
    run helm template test "${REPO_ROOT}/helm/charts/dfe-engine/" \
        --set global.registry=ghcr.io/test \
        --set oidc.enabled=true \
        --set 'oidc.providers[0].name=google' \
        --set 'oidc.providers[0].secretName=dfe-oidc-google' \
        --set 'oidc.providers[0].envMappings.DFE_OIDC_GOOGLE_CLIENT_ID=client-id'
    [ "$status" -eq 0 ]
    [[ "$output" =~ "DFE_OIDC_GOOGLE_CLIENT_ID" ]]
    [[ "$output" =~ "dfe-oidc-google" ]]
}

@test "SecurityPolicy targets HTTPRoute not Gateway" {
    run helm template test "${REPO_ROOT}/helm/charts/envoy-gateway-config/" \
        --set domain=example.com \
        --set oidc.enabled=true \
        --set 'oidc.providers[0].name=google' \
        --set 'oidc.providers[0].issuerUrl=https://accounts.google.com' \
        --set 'oidc.providers[0].clientId=test-id' \
        --set 'oidc.providers[0].clientSecretName=dfe-oidc-google'
    [ "$status" -eq 0 ]
    [[ "$output" =~ "kind: HTTPRoute" ]]
}
