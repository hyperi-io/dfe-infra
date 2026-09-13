#!/bin/sh
# dfe-toolbox entrypoint. With no command: print every tool this image
# actually carries, with its version, and exit. With a command: exec it
# unchanged, so the image is a normal shell for `kubectl exec` / `docker run`.
#
# ONE script, shared by dfe-toolbox-base and every -aws/-gcp/-azure layer that
# FROMs it: each `ver` call is skipped silently when its tool is not on PATH,
# so the base image reports the base tool set and each cloud layer reports
# that plus its own CLI, with nothing to keep in sync between them.

ver() {
    label="$1"
    shift
    if ! command -v "$1" >/dev/null 2>&1; then
        return 0
    fi
    out="$("$@" 2>&1 | head -n1)"
    [ -n "$out" ] || out="(no output)"
    printf '%-11s %s\n' "$label" "$out"
}

if [ "$#" -eq 0 ]; then
    echo "dfe-toolbox -- pinned CLI versions (docs/deployment/toolbox.md)"
    echo
    ver kubectl    kubectl version --client
    ver helm       helm version --short
    ver argocd     argocd version --client --short
    ver tofu       tofu version
    ver yq         yq --version
    ver clickhouse clickhouse-client --version
    ver psql       psql --version
    # kcat's `-V` banner puts the version on its 4th line, not its 1st, so
    # the generic `ver` helper's head -n1 would print the project blurb
    # instead -- pull the "Version X.Y.Z" line out directly.
    if command -v kcat >/dev/null 2>&1; then
        kcat_ver="$(kcat -V 2>&1 | awk '/^Version/{print $2; exit}')"
        printf '%-11s %s\n' kcat "${kcat_ver:-unknown}"
    fi
    ver jq         jq --version
    ver openssl    openssl version
    ver curl       curl --version
    ver dig        dig -v
    ver aws         aws --version
    ver ssm-plugin  session-manager-plugin --version
    ver gcloud      gcloud --version
    ver az          az --version
    exit 0
fi

exec "$@"
