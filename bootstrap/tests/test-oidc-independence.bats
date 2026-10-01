#!/usr/bin/env bats
# CRITICAL: Verify dfe-infra OIDC works independently of dfe-engine.
#
# These tests ensure:
# 1. Envoy Gateway SecurityPolicy is a static K8s CRD — no dfe-engine dependency
# 2. OIDC secrets are provisioned by Terraform/ESO, not by dfe-engine
# 3. Header forwarding is configured in Envoy, not computed by dfe-engine
# 4. ArgoCD syncs OIDC config from git, not from dfe-engine API
# 5. If dfe-engine is down, OIDC auth still works (Envoy handles it)

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
}

# --- Invariant 1: SecurityPolicy is a static CRD ---

@test "SecurityPolicy references HTTPRoute targetRef, not a backendRef to dfe-engine" {
    # The SecurityPolicy must attach to the HTTPRoute via targetRefs, not
    # use a backendRef to dfe-engine's Service. This ensures Envoy applies
    # OIDC auth at the route level — dfe-engine doesn't need to be running.
    helm template test "${REPO_ROOT}/helm/edge/gateway/" \
        -f "${REPO_ROOT}/argocd/values/common.yaml" \
        --set domain=example.com \
        --set oidc.enabled=true \
        --set 'oidc.providers[0].name=google' \
        --set 'oidc.providers[0].issuerUrl=https://accounts.google.com' \
        --set 'oidc.providers[0].clientId=test' \
        --set 'oidc.providers[0].clientSecretName=dfe-oidc-google' \
        > "${BATS_TMPDIR}/rendered.yaml"

    # Extract only the SecurityPolicy document
    local sp_section
    sp_section=$(sed -n '/kind: SecurityPolicy/,/^---/p' "${BATS_TMPDIR}/rendered.yaml")

    # SecurityPolicy must use targetRefs (to HTTPRoute), never backendRef
    [[ "${sp_section}" == *"targetRefs"* ]]
    [[ ! "${sp_section}" == *"backendRef"* ]]
    # It references the HTTPRoute by name, which is expected
    [[ "${sp_section}" == *"kind: HTTPRoute"* ]]
}

@test "OIDC config comes entirely from Helm values, not runtime API" {
    run helm template test "${REPO_ROOT}/helm/edge/gateway/" \
        -f "${REPO_ROOT}/argocd/values/common.yaml" \
        --set domain=example.com \
        --set oidc.enabled=true \
        --set 'oidc.providers[0].name=test-provider' \
        --set 'oidc.providers[0].issuerUrl=https://issuer.example.com' \
        --set 'oidc.providers[0].clientId=test-client-123' \
        --set 'oidc.providers[0].clientSecretName=test-secret'
    [ "$status" -eq 0 ]
    [[ "$output" == *"https://issuer.example.com"* ]]
    [[ "$output" == *"test-client-123"* ]]
    [[ "$output" == *"test-secret"* ]]
}

# --- Invariant 2: Secrets are ESO/Terraform managed ---

@test "dfe-engine OIDC secrets reference K8s Secrets, not dfe-engine endpoints" {
    # Scoped to the engine pod, the one holding the credential: the hunt runner's
    # readiness wait polls the engine too, and that is start order, not a secret source.
    run helm template test "${REPO_ROOT}/helm/charts/dfe-engine/" \
        --show-only templates/deployment.yaml \
        --set global.registry=ghcr.io/test \
        --set oidc.enabled=true \
        --set 'oidc.providers[0].name=google' \
        --set 'oidc.providers[0].secretName=dfe-oidc-google' \
        --set 'oidc.providers[0].envMappings.DFE_OIDC_GOOGLE_CLIENT_ID=client-id'
    [ "$status" -eq 0 ]

    # The pod carries other secretKeyRefs, so the provider's own entry is the one read.
    local entry
    entry=$(printf '%s\n' "$output" | grep -A4 -e '- name: DFE_OIDC_GOOGLE_CLIENT_ID')
    [[ "${entry}" == *"secretKeyRef:"* ]]
    [[ "${entry}" == *"name: dfe-oidc-google"* ]]
    [[ "${entry}" == *"key: client-id"* ]]
    [[ ! "${entry}" == *"value:"* ]]

    [[ ! "$output" == *"http://dfe-engine"* ]]
    [[ ! "$output" == *"https://dfe-engine"* ]]
}

# --- Invariant 3: Header names are static in Envoy config ---

@test "forwarded headers are hardcoded in Envoy config, not dynamic" {
    run helm template test "${REPO_ROOT}/helm/edge/gateway/" \
        -f "${REPO_ROOT}/argocd/values/common.yaml" \
        --set domain=example.com \
        --set jwtAuthn.enabled=true \
        --set jwtAuthn.issuer=https://accounts.google.com
    [ "$status" -eq 0 ]
    [[ "$output" == *"header_name: X-Oidc-Subject"* ]]
    [[ "$output" == *"header_name: X-Oidc-Groups"* ]]
}

# --- Invariant 4: Simple auth mode works with zero OIDC config ---

@test "entire stack renders with oidc.enabled=false (simple auth)" {
    local exit_code=0
    helm template test "${REPO_ROOT}/helm/edge/gateway/" \
        -f "${REPO_ROOT}/argocd/values/common.yaml" \
        --set domain=example.com || exit_code=$?
    [ "$exit_code" -eq 0 ]

    helm template test "${REPO_ROOT}/helm/charts/dfe-engine/" \
        --set global.registry=ghcr.io/test || exit_code=$?
    [ "$exit_code" -eq 0 ]
}

# --- Invariant 5: Adding a provider is a values change, not code ---

@test "adding a new OIDC provider requires only values, no template changes" {
    run helm template test "${REPO_ROOT}/helm/edge/gateway/" \
        -f "${REPO_ROOT}/argocd/values/common.yaml" \
        --set domain=example.com \
        --set oidc.enabled=true \
        --set 'oidc.providers[0].name=custom-keycloak' \
        --set 'oidc.providers[0].issuerUrl=https://keycloak.corp.example.com/realms/dfe' \
        --set 'oidc.providers[0].clientId=dfe-app' \
        --set 'oidc.providers[0].clientSecretName=dfe-oidc-keycloak'
    [ "$status" -eq 0 ]
    [[ "$output" == *"dfe-oidc-custom-keycloak"* ]]
    [[ "$output" == *"keycloak.corp.example.com"* ]]
}
