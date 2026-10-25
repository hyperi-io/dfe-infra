#!/usr/bin/env bash
#
#  Project:      dfe-infra
#  File:         docker/dfe-toolbox/base/pinned-keyring.sh
#  Purpose:      Write an OpenPGP keyring holding exactly the pinned primary keys from a key file.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage: pinned-keyring <key-file> <keyring-out> <primary-fingerprint>...
#  Fingerprints are 40 uppercase hex characters. The output is what apt's
#  signed-by and gpgv --keyring read, and it fails unless it holds every pinned
#  key and nothing else, so a key file that gains a key cannot widen the trust.

set -euo pipefail

die() {
    echo "pinned-keyring: ${1}" >&2
    exit 1
}

main() {
    local usage="usage: pinned-keyring <key-file> <keyring-out> <primary-fingerprint>..."
    [[ $# -ge 3 ]] || die "${usage}"
    local key_file="${1}"
    local keyring_out="${2}"
    shift 2

    # Global, because the EXIT trap fires after main's locals are gone.
    GNUPG_SCRATCH="$(mktemp -d)"
    trap 'rm -rf "${GNUPG_SCRATCH}"' EXIT
    gpg --homedir "${GNUPG_SCRATCH}" --batch --quiet --import "${key_file}"
    gpg --homedir "${GNUPG_SCRATCH}" --batch --export "$@" > "${keyring_out}"

    local wanted exported
    wanted="$(printf '%s\n' "$@" | sort -u)"
    exported="$(gpg --homedir "${GNUPG_SCRATCH}" --batch --show-keys --with-colons "${keyring_out}" \
        | grep -A1 '^pub:' | grep '^fpr:' | cut -d: -f10 | sort -u)" \
        || die "${key_file} holds none of: $*"
    [[ "${exported}" == "${wanted}" ]] || die "${key_file} primary keys [${exported//$'\n'/ }] are not the pinned [${wanted//$'\n'/ }]"
}

main "$@"
