#!/usr/bin/env bash
# Post-deploy ACCESS SUMMARY -- the "I've installed it, now where do I go and how
# do I log in" step. Prints to the console AND writes a Markdown file. Run at the
# end of bootstrap.sh, or standalone:
#   ./access-summary.sh [kubeconfig] [output-file]
#
# It reads the dfe-cluster secret annotations (domain, namespace) so it works for
# any deployment, and emits every external endpoint + how to authenticate + the
# exact command to fetch each credential from the secret store.
set -euo pipefail

if [ -n "${1:-}" ]; then export KUBECONFIG="$1"; fi
OUT="${2:-dfe-access.md}"

ann() { kubectl -n argocd get secret dfe-cluster -o jsonpath="{.metadata.annotations.dfe\.hyperi\.io/$1}" 2>/dev/null || true; }

DOMAIN="$(ann domain)"
NS="$(ann dfe_namespace)"
PROFILE="$(ann profile)"
: "${DOMAIN:=<your-domain>}"
: "${NS:=dfe}"

# Is a route exposed? (HTTPRoute exists for that host)
exposed() { kubectl get httproute "$1" -A >/dev/null 2>&1 && echo "exposed" || echo "internal (port-forward)"; }

read -r -d '' BODY <<EOF || true
# DFE access -- where everything is

Deployment tier: **${PROFILE:-unknown}**   |   Domain: **${DOMAIN}**   |   App namespace: **${NS}**

All interactive UIs sit behind the Envoy Gateway on \`*.${DOMAIN}\` (wildcard TLS).
Auth is the standard pattern: OIDC at the edge when configured, otherwise the
per-app local **break-glass** account from the secret store.

## Endpoints

| What | URL | Reach |
|------|-----|-------|
| DFE UI (+ embedded HyperDX explore) | https://dfe.${DOMAIN} | $(exposed dfe-ui) |
| DFE API (engine) | https://dfe.${DOMAIN}/api | $(exposed dfe-engine) |
| HyperDX | https://hyperdx.${DOMAIN} | $(exposed hyperdx) |
| Kafbat (Kafka UI) | https://kafbat.${DOMAIN} | $(exposed kafbat) |
| Argo CD | https://argocd.${DOMAIN} | $(exposed argocd) |
| Deploy-repo git (Forgejo) | https://forgejo.${DOMAIN} | $(exposed forgejo) |
| Receiver ingest | https://dfe.${DOMAIN} (or the receiver LB) | data plane |

An "internal" UI is not exposed; reach it with
\`kubectl -n <ns> port-forward svc/<service> <localport>:<port>\`.

## How to log in + get credentials

- **OIDC configured:** log in with your IdP; RBAC role (ro/rw/admin) comes from
  your OIDC group. No password to fetch.
- **Local break-glass (no OIDC / recovery):** fetch from the secret store:

\`\`\`sh
# DFE UI / engine admin (initial local account)
kubectl -n ${NS} get secret dfe-engine-admin -o jsonpath='{.data.password}' | base64 -d; echo
# Kafbat break-glass admin
kubectl -n kafka get secret dfe-kafbat-breakglass -o jsonpath='{.data.username}' | base64 -d; echo
kubectl -n kafka get secret dfe-kafbat-breakglass -o jsonpath='{.data.password}' | base64 -d; echo
# Argo CD initial admin
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
# Forgejo admin (deploy-repo)
kubectl -n forgejo get secret dfe-forgejo-admin -o jsonpath='{.data.password}' | base64 -d; echo
# ClickHouse admin
kubectl -n clickhouse get secret clickhouse-admin-password -o jsonpath='{.data.password}' | base64 -d; echo
\`\`\`

## Smoke check

\`\`\`sh
curl -fsS https://dfe.${DOMAIN}/api/v1/system/health   # engine health
kubectl -n ${NS} get pods                              # everything Running
\`\`\`
EOF

printf '%s\n' "$BODY" | tee "$OUT"
echo
echo "==> access summary written to ${OUT}"
