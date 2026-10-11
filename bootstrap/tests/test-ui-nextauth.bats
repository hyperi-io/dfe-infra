#!/usr/bin/env bats
# dfe-ui must deploy on a bare cluster. next-auth refuses to start without a
# signing secret, so requiring a secrets store for one made the UI undeployable
# wherever no store existed -- the pod sat in CreateContainerConfigError while
# the ExternalSecret reported "ClusterSecretStore is not ready" forever.
#
# dfe-extras renders the secret beside dfe-ui's thin chart, off the integration
# values the thin chart reads its secret name from.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    CHART="${REPO_ROOT}/helm/charts/dfe-extras/"
    VALUES="${REPO_ROOT}/argocd/values/apps/dfe-ui/values.yaml"
}

render() {
    helm template test "$CHART" -f "$VALUES" --set chartName=dfe-ui "$@"
}

@test "the signing secret is generated in-cluster by default" {
    run render
    [ "$status" -eq 0 ]
    [[ "$output" =~ "kind: Password" ]]
    [[ ! "$output" =~ "secretStoreRef" ]]
}

@test "the generated secret is written once, not rotated on re-sync" {
    # A rotating signing secret invalidates every live session on each sync.
    run render
    [ "$status" -eq 0 ]
    [[ "$output" =~ 'refreshInterval: "0"' ]]
}

@test "a deployer can still source it from a secrets store" {
    run render --set config.secretStoreName=dfe-secret-store
    [ "$status" -eq 0 ]
    [[ "$output" =~ "secretStoreRef" ]]
    [[ "$output" =~ "ClusterSecretStore" ]]
    [[ ! "$output" =~ "kind: Password" ]]
}

@test "the store path keeps the tf-secrets key layout" {
    run render --set config.secretStoreName=dfe-secret-store
    [ "$status" -eq 0 ]
    [[ "$output" =~ "/ui/nextauth" ]]
}

@test "both paths target the same Secret name the deployment reads" {
    # The thin chart reads the secret named by config.nextauthSecretName.
    grep -q "existingSecret: '{{ .Values.config.nextauthSecretName }}'" "$VALUES"

    run render
    [ "$status" -eq 0 ]
    [[ "$output" =~ "name: dfe-ui-nextauth"$'\n' ]]

    run render --set config.secretStoreName=dfe-secret-store
    [ "$status" -eq 0 ]
    [[ "$output" =~ "name: dfe-ui-nextauth"$'\n' ]]
}
