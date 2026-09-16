#!/usr/bin/env bats
# EKS 1.30+ ships gp2 with no default and the CSI add-on creates no
# StorageClass object at all, so on a fresh cluster this template is the whole
# difference between the data pods binding a PVC and sitting Pending.
#
# The default-class annotation is the sharp edge: two default classes is a
# misconfiguration, and since Kubernetes 1.26 the newest wins, so claiming
# default on a cluster that already has one silently re-points every
# unannotated PVC in the cluster -- the customer's as well as DFE's.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    TPL="${REPO_ROOT}/bootstrap/templates/storageclass-aws.yaml.tpl"
    export DFE_STORAGE_CLASS="gp3"
    export DFE_STORAGE_CLASS_IS_DEFAULT="true"
}

@test "the class takes its name from DFE_STORAGE_CLASS" {
    run envsubst < "${TPL}"
    [ "$status" -eq 0 ]
    [[ "$output" =~ 'name: "gp3"' ]]
}

@test "nothing is left unsubstituted" {
    run envsubst < "${TPL}"
    [ "$status" -eq 0 ]
    [[ ! "$output" =~ '${' ]]
}

@test "it provisions gp3 on the EBS CSI driver at the free-tier baseline" {
    run envsubst < "${TPL}"
    [ "$status" -eq 0 ]
    [[ "$output" =~ "provisioner: ebs.csi.aws.com" ]]
    [[ "$output" =~ "type: gp3" ]]
    [[ "$output" =~ 'iops: "3000"' ]]
    [[ "$output" =~ 'throughput: "125"' ]]
}

@test "volumes are encrypted and bound only once a pod needs them" {
    run envsubst < "${TPL}"
    [ "$status" -eq 0 ]
    [[ "$output" =~ 'encrypted: "true"' ]]
    [[ "$output" =~ "volumeBindingMode: WaitForFirstConsumer" ]]
    [[ "$output" =~ "allowVolumeExpansion: true" ]]
}

@test "a cluster with no default gets this class as the default" {
    run envsubst < "${TPL}"
    [ "$status" -eq 0 ]
    [[ "$output" =~ 'storageclass.kubernetes.io/is-default-class: "true"' ]]
}

@test "a cluster that already has a default keeps it" {
    export DFE_STORAGE_CLASS_IS_DEFAULT="false"
    run envsubst < "${TPL}"
    [ "$status" -eq 0 ]
    [[ "$output" =~ 'storageclass.kubernetes.io/is-default-class: "false"' ]]
    [[ ! "$output" =~ 'is-default-class: "true"' ]]
}

@test "bootstrap.sh decides the annotation rather than the template hardcoding it" {
    run grep -c 'DFE_STORAGE_CLASS_IS_DEFAULT' "${REPO_ROOT}/bootstrap/bootstrap.sh"
    [ "$status" -eq 0 ]
    [ "$output" -ge 2 ]
}
