#!/usr/bin/env bats
# The single tier's landing topic must be created by a resource Argo TRACKS.
# A PostSync hook is omitted from the Application resource tree, so one that
# never fires is invisible -- which is how dfe-loader crashlooped 1657 times on a
# deploy that reported success.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    SINGLE=(--set kafka.mode=single --set appNamespace=dfe-local)
}

@test "single tier renders a create-topic Job" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" "${SINGLE[@]}"
    [ "$status" -eq 0 ]
    [[ "$output" =~ "dfe-kafka-default-topic" ]]
    [[ "$output" =~ "kafka-topics.sh" ]]
}

@test "the create-topic Job is tracked, not a hook" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" "${SINGLE[@]}"
    [ "$status" -eq 0 ]
    [[ ! "$output" =~ "argocd.argoproj.io/hook" ]]
    [[ "$output" =~ "argocd.argoproj.io/sync-wave" ]]
    # A Job spec is immutable; without Replace a changed spec fails every sync.
    [[ "$output" =~ "Replace=true" ]]
}

@test "the topic is created at replication factor 1 on a one-broker tier" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" "${SINGLE[@]}"
    [ "$status" -eq 0 ]
    [[ "$output" =~ "--replication-factor 1" ]]
}

@test "the create is idempotent" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" "${SINGLE[@]}"
    [ "$status" -eq 0 ]
    [[ "$output" =~ "--if-not-exists" ]]
}

@test "the cluster tier uses the Strimzi CR and renders no Job" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" \
        --set kafka.mode=cluster --set appNamespace=dfe-local
    [ "$status" -eq 0 ]
    [[ "$output" =~ "kind: KafkaTopic" ]]
    [[ ! "$output" =~ "dfe-kafka-default-topic" ]]
}

@test "the single tier renders no Strimzi CR -- nothing would reconcile it" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" "${SINGLE[@]}"
    [ "$status" -eq 0 ]
    [[ ! "$output" =~ "kind: KafkaTopic" ]]
}

@test "defaultTopic.create=false opts out of both paths" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" \
        "${SINGLE[@]}" --set kafka.defaultTopic.create=false
    [ "$status" -eq 0 ]
    [[ ! "$output" =~ "dfe-kafka-default-topic" ]]
}
