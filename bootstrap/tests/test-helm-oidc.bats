#!/usr/bin/env bats
# Verify OIDC Helm templates render correctly and dfe-engine is not required.
#
# The engine's env comes from dfe-extras' dfe-engine-env ConfigMap, beside its
# thin chart, so the engine's own settings are read off that render.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
}

engine_env() {
    helm template test "${REPO_ROOT}/helm/charts/dfe-extras/" \
        --set chartName=dfe-engine \
        --show-only templates/engine-env.yaml "$@"
}

@test "envoy-gateway-config renders without OIDC (simple auth mode)" {
    # -f common.yaml supplies hostnames.*, the deploy-config SSoT every real
    # render (argocd/appsets/layer2-platform.yaml) layers in; with no values
    # files the chart's own routeHost guard correctly refuses an empty label.
    run helm template test "${REPO_ROOT}/helm/edge/gateway/" \
        -f "${REPO_ROOT}/argocd/values/common.yaml" \
        --set domain=example.com
    [ "$status" -eq 0 ]
    [[ ! "$output" =~ "SecurityPolicy" ]]
}

@test "envoy-gateway-config renders with single OIDC provider" {
    run helm template test "${REPO_ROOT}/helm/edge/gateway/" \
        -f "${REPO_ROOT}/argocd/values/common.yaml" \
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
    run helm template test "${REPO_ROOT}/helm/edge/gateway/" \
        -f "${REPO_ROOT}/argocd/values/common.yaml" \
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
    run helm template test "${REPO_ROOT}/helm/edge/gateway/" \
        -f "${REPO_ROOT}/argocd/values/common.yaml" \
        --set domain=example.com \
        --set jwtAuthn.enabled=true \
        --set jwtAuthn.issuer=https://accounts.google.com
    [ "$status" -eq 0 ]
    [[ "$output" =~ "X-Oidc-Subject" ]]
    [[ "$output" =~ "X-Oidc-Groups" ]]
    [[ "$output" =~ "X-Forwarded-User" ]]
}

@test "jwt_authn strips the identity headers before it sets them" {
    run helm template test "${REPO_ROOT}/helm/edge/gateway/" \
        -f "${REPO_ROOT}/argocd/values/common.yaml" \
        --set domain=example.com \
        --set jwtAuthn.enabled=true \
        --set jwtAuthn.issuer=https://accounts.google.com
    [ "$status" -eq 0 ]
    [[ "$output" =~ "header_mutation" ]]
    [[ "$output" =~ 'remove: "X-Oidc-Subject"' ]]
    [[ "$output" =~ 'remove: "X-Oidc-Groups"' ]]
    [[ "$output" =~ 'remove: "X-Oidc-Email"' ]]
    # The strip is only a strip if it runs first: index 0 removes, index 1 verifies.
    [[ "$output" =~ "http_filters/0\"".*$'\n'.*"header_mutation" ]] || \
        echo "$output" | grep -A3 'http_filters/0' | grep -q header_mutation
    echo "$output" | grep -A3 'http_filters/1' | grep -q jwt_authn
}

@test "the engine does not trust identity headers just because OIDC is on" {
    # oidc.enabled is Envoy's OIDC REDIRECT; it does not inject verified headers.
    # Trusting them off that switch is how a forged header becomes an identity.
    run engine_env --set oidc.enabled=true
    [ "$status" -eq 0 ]
    [[ "$output" =~ "kind: ConfigMap" ]]
    [[ ! "$output" =~ "DFE_AUTH_TRUST_PROXY_AUTH_HEADERS" ]]
}

@test "the engine trusts identity headers when the deployment says so" {
    run engine_env --set auth.trustProxyHeaders=true
    [ "$status" -eq 0 ]
    [[ "$output" =~ 'DFE_AUTH_TRUST_PROXY_AUTH_HEADERS: "true"' ]]
}

@test "dfe-engine renders without OIDC (simple auth mode)" {
    run engine_env
    [ "$status" -eq 0 ]
    [[ "$output" =~ "kind: ConfigMap" ]]
    [[ ! "$output" =~ "DFE_OIDC" ]]
}

@test "an OIDC provider secret never lands in the engine's env ConfigMap" {
    # A provider's variables are secretKeyRefs, which reach the pod through the
    # thin chart's extraEnv; a ConfigMap would carry the secret in the clear.
    run engine_env \
        --set oidc.enabled=true \
        --set 'oidc.providers[0].name=google' \
        --set 'oidc.providers[0].secretName=dfe-oidc-google' \
        --set 'oidc.providers[0].envMappings.DFE_OIDC_GOOGLE_CLIENT_ID=client-id'
    [ "$status" -eq 0 ]
    [[ "$output" =~ "kind: ConfigMap" ]]
    [[ ! "$output" =~ "DFE_OIDC_GOOGLE_CLIENT_ID" ]]
    [[ ! "$output" =~ "dfe-oidc-google" ]]
}

@test "SecurityPolicy targets HTTPRoute not Gateway" {
    run helm template test "${REPO_ROOT}/helm/edge/gateway/" \
        -f "${REPO_ROOT}/argocd/values/common.yaml" \
        --set domain=example.com \
        --set oidc.enabled=true \
        --set 'oidc.providers[0].name=google' \
        --set 'oidc.providers[0].issuerUrl=https://accounts.google.com' \
        --set 'oidc.providers[0].clientId=test-id' \
        --set 'oidc.providers[0].clientSecretName=dfe-oidc-google'
    [ "$status" -eq 0 ]
    [[ "$output" =~ "kind: HTTPRoute" ]]
}
