#!/usr/bin/env bats
# The single tier's bootstrap topics (landing + DLQ) must be created by a
# resource Argo TRACKS. A PostSync hook is omitted from the Application
# resource tree, so one that never fires is invisible -- which is how
# dfe-loader crashlooped 1657 times on a deploy that reported success.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    SINGLE=(--set kafka.mode=single --set appNamespace=dfe-local)
}

@test "single tier renders a bootstrap-topics Job" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" "${SINGLE[@]}"
    [ "$status" -eq 0 ]
    [[ "$output" =~ "dfe-kafka-bootstrap-topics" ]]
    [[ "$output" =~ "kafka-topics.sh" ]]
}

@test "the bootstrap-topics Job is tracked, not a hook" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" "${SINGLE[@]}"
    [ "$status" -eq 0 ]
    [[ ! "$output" =~ "argocd.argoproj.io/hook" ]]
    [[ "$output" =~ "argocd.argoproj.io/sync-wave" ]]
    # A Job spec is immutable; without Replace a changed spec fails every sync.
    [[ "$output" =~ "Replace=true" ]]
}

@test "topics are created at replication factor 1 on a one-broker tier" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" "${SINGLE[@]}"
    [ "$status" -eq 0 ]
    [[ "$output" =~ "--replication-factor 1" ]]
}

@test "the create is idempotent" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" "${SINGLE[@]}"
    [ "$status" -eq 0 ]
    [[ "$output" =~ "--if-not-exists" ]]
}

@test "the single-tier Job creates every DLQ topic" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" "${SINGLE[@]}"
    [ "$status" -eq 0 ]
    [[ "$output" =~ "dfe_receiver_dlq" ]]
    [[ "$output" =~ "dfe_loader_dlq" ]]
    [[ "$output" =~ "dfe_archiver_dlq" ]]
    [[ "$output" =~ "dfe_fetcher_dlq" ]]
    [[ "$output" =~ "dfe_transform_dlq" ]]
}

@test "numeric per-topic config renders as an integer, not scientific notation" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" "${SINGLE[@]}" \
        --set kafka.dlqTopics.config."retention\.ms"=604800000
    [ "$status" -eq 0 ]
    [[ "$output" =~ "retention.ms=604800000" ]]
    [[ ! "$output" =~ "e+0" ]]
}

@test "the cluster tier uses Strimzi CRs and renders no Job" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" \
        --set kafka.mode=cluster --set appNamespace=dfe-local
    [ "$status" -eq 0 ]
    [[ "$output" =~ "kind: KafkaTopic" ]]
    [[ ! "$output" =~ "dfe-kafka-bootstrap-topics" ]]
}

@test "the single tier renders no Strimzi CR -- nothing would reconcile it" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" "${SINGLE[@]}"
    [ "$status" -eq 0 ]
    [[ ! "$output" =~ "kind: KafkaTopic" ]]
}

@test "defaultTopic.create=false still creates the DLQ topics" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" \
        "${SINGLE[@]}" --set kafka.defaultTopic.create=false
    [ "$status" -eq 0 ]
    [[ "$output" =~ "dfe-kafka-bootstrap-topics" ]]
    [[ ! "$output" =~ "main_land" ]]
    [[ "$output" =~ "dfe_loader_dlq" ]]
}

@test "both creates disabled renders neither Job nor CR" {
    run helm template test "${REPO_ROOT}/helm/charts/kafka/" \
        "${SINGLE[@]}" --set kafka.defaultTopic.create=false \
        --set kafka.dlqTopics.create=false
    [ "$status" -eq 0 ]
    [[ ! "$output" =~ "dfe-kafka-bootstrap-topics" ]]
    [[ ! "$output" =~ "kind: KafkaTopic" ]]
}
