#  Project:      dfe-infra
#  File:         profiles.sh
#  Purpose:      The deploy-profile facts the shell smoke tests need.
#  Language:     POSIX sh
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
# GENERATED from scripts/profiles.py -- do not edit. Regenerate with:
#     python3 scripts/profiles.py --shell --out bootstrap/scripts/profiles.sh

# Sourced, never executed, so it carries a shell directive not a shebang.
# shellcheck shell=sh

# Whether the deploy profile in PROFILE runs a broker. An unset or unknown
# profile answers no, so a broker check reports SKIP rather than a false FAIL.
profile_has_kafka() {
    case "${PROFILE:-}" in
        single|scale) return 0 ;;
        *) return 1 ;;
    esac
}
