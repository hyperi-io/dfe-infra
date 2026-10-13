#!/usr/bin/env bats
# A tool bootstrap.sh finds missing halfway through stops a deploy that has
# already built everything before it, and on a cloud that is a whole cluster.
# So the tool check is the first thing bootstrap.sh does, and a missing tool
# stops it before any cluster call.
# PATH is a directory this file builds: stand-ins for the cluster tools that
# log every call, the real python3 and dirname, and nothing else. No cluster is
# involved.

setup() {
    REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
    BOOTSTRAP="${REPO_ROOT}/bootstrap/bootstrap.sh"
    TOOL_DIR="${BATS_TEST_TMPDIR}/bin"
    CALLS="${BATS_TEST_TMPDIR}/calls.log"
    mkdir -p "${TOOL_DIR}"
    : > "${CALLS}"
    for tool in kubectl helm openssl aws; do
        printf '#!/bin/sh\necho "%s $*" >> "%s"\n' "${tool}" "${CALLS}" > "${TOOL_DIR}/${tool}"
        chmod +x "${TOOL_DIR}/${tool}"
    done
    ln -s "$(command -v python3)" "${TOOL_DIR}/python3"
    ln -s "$(command -v dirname)" "${TOOL_DIR}/dirname"
}

# bootstrap.sh under a PATH of TOOL_DIR alone, with only the NAME=value settings given.
run_bootstrap() {
    run env -i PATH="${TOOL_DIR}" HOME="${BATS_TEST_TMPDIR}" "$@" "${BASH}" "${BOOTSTRAP}"
}

@test "a missing tool stops bootstrap before any cluster call, naming it and what needs it" {
    rm "${TOOL_DIR}/openssl"
    run_bootstrap DFE_CLOUD=local
    [ "$status" -eq 1 ]
    [[ "${output}" == *"openssl: mints the bundled deploy repo's admin password"* ]]
    [ ! -s "${CALLS}" ]
}

@test "every missing tool is named at once" {
    rm "${TOOL_DIR}/kubectl" "${TOOL_DIR}/helm"
    run_bootstrap DFE_CLOUD=local
    [ "$status" -eq 1 ]
    [[ "${output}" == *"kubectl: every cluster call"* ]]
    [[ "${output}" == *"helm: every chart install"* ]]
    [ ! -s "${CALLS}" ]
}

@test "an AWS deployment without the aws CLI is stopped too" {
    rm "${TOOL_DIR}/aws"
    run_bootstrap DFE_CLOUD=aws
    [ "$status" -eq 1 ]
    [[ "${output}" == *"aws: destroy.sh step 9"* ]]
    [ ! -s "${CALLS}" ]
}

@test "with no python3 bootstrap says so rather than failing on the first helper" {
    rm "${TOOL_DIR}/python3"
    run_bootstrap DFE_CLOUD=local
    [ "$status" -eq 1 ]
    [[ "${output}" == *"python3 is not on PATH"* ]]
    [ ! -s "${CALLS}" ]
}

@test "with every tool on PATH bootstrap names them and goes on to its own settings" {
    run_bootstrap DFE_CLOUD=local DFE_SKIP_PLATFORM_CHECK=true
    [ "$status" -eq 1 ]
    [[ "${output}" == *"deploy tools on PATH: kubectl helm python3 openssl"* ]]
    [[ "${output}" == *"Missing required environment variables"* ]]
}
