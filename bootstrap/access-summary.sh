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

TAB="$(printf '\t')"

# Every HTTPRoute on the cluster, one line of name|path|Accepted|ResolvedRefs|
# hostnames -- the route's own status is what says the gateway programmed it.
# Pipe-separated, not tab: an empty path or an unprogrammed route leaves a field
# empty, which a tab IFS collapses away.
ROUTES="$(kubectl get httproute -A -o jsonpath="{range .items[*]}{.metadata.name}|{.spec.rules[0].matches[0].path.value}|{range .status.parents[*]}{range .conditions[?(@.type==\"Accepted\")]}{.status}={.reason}{\" \"}{end}{end}|{range .status.parents[*]}{range .conditions[?(@.type==\"ResolvedRefs\")]}{.status}={.reason}{\" \"}{end}{end}|{range .spec.hostnames[*]}{@}{\" \"}{end}{\"\n\"}{end}" 2>/dev/null || true)"

# The deploy's own Gateway, by name then by class -- a cluster can carry others.
GATEWAY_NAME="${DFE_GATEWAY_NAME:-dfe-gateway}"
GATEWAY_CLASS="${DFE_GATEWAY_CLASS:-dfe-envoy}"
GATEWAYS="$(kubectl get gateway -A -o jsonpath="{range .items[*]}{.metadata.name}${TAB}{.spec.gatewayClassName}${TAB}{.status.addresses[0].value}{\"\n\"}{end}" 2>/dev/null || true)"
GATEWAY_ADDR="$(printf '%s\n' "${GATEWAYS}" | awk -F'\t' -v n="${GATEWAY_NAME}" '$1 == n {print $3; exit}')"
if [ -z "${GATEWAY_ADDR}" ]; then
  GATEWAY_ADDR="$(printf '%s\n' "${GATEWAYS}" | awk -F'\t' -v c="${GATEWAY_CLASS}" '$2 == c {print $3; exit}')"
fi

# The routes a default deploy carries, as <name>|<host label>|<path>|<human name>.
# One list, so the label map and the no-routes fallback cannot drift apart.
KNOWN_ROUTES="dfe-ui|dfe||DFE UI (+ embedded HyperDX explore)
dfe-engine|dfe|/api/v1|DFE API (engine)
hyperdx|hyperdx||HyperDX
kafbat|kafbat||Kafbat (Kafka UI)
argocd|argocd||Argo CD
forgejo|git||Deploy-repo git (Forgejo)
links|links||Links page
receiver|receiver||Receiver ingest
otel|otel||OTLP ingest"

# The human name for an HTTPRoute, falling back to the route's own name so a
# route added later still gets a row.
route_label() {
  local name host path label
  while IFS='|' read -r name host path label; do
    if [ "${name}" = "$1" ]; then
      echo "${label}"
      return
    fi
  done <<KNOWN
${KNOWN_ROUTES}
KNOWN
  echo "$1"
}

# The Reach of one route, from the conditions the gateway wrote on it: a route
# it refused, or whose backends it could not resolve, answers nothing.
route_reach() {
  local accepted="$1" resolved="$2" cond
  if [ -z "${accepted//[[:space:]]/}" ]; then
    echo "not accepted (no gateway status)"
    return
  fi
  for cond in ${accepted}; do
    [ "${cond%%=*}" = "True" ] || { echo "not accepted (${cond#*=})"; return; }
  done
  for cond in ${resolved}; do
    [ "${cond%%=*}" = "True" ] || { echo "refs unresolved (${cond#*=})"; return; }
  done
  echo "exposed"
}

# The endpoints table, one row per hostname of every live HTTPRoute. The known
# routes are the fallback for a cluster carrying none, where nothing is exposed.
endpoint_rows() {
  local name path accepted resolved hosts reach host label
  if [ -n "${ROUTES//[[:space:]]/}" ]; then
    printf '%s\n' "${ROUTES}" | while IFS='|' read -r name path accepted resolved hosts; do
      [ -z "${hosts//[[:space:]]/}" ] && continue
      reach="$(route_reach "${accepted}" "${resolved}")"
      for host in ${hosts}; do
        printf '| %s | https://%s%s | %s |\n' \
          "$(route_label "${name}")" "${host}" \
          "$(printf '%s' "${path}" | sed 's:^/$::')" "${reach}"
      done
    done
    return
  fi
  while IFS='|' read -r name host path label; do
    printf '| %s | https://%s.%s%s | internal (port-forward) |\n' \
      "${label}" "${host}" "${DOMAIN}" "${path}"
  done <<KNOWN
${KNOWN_ROUTES}
KNOWN
}

# Stable named logins seeded + reconciled by the engine every boot (#106). Lists
# the configured seed accounts (username + groups) from the chart-created Secret,
# or "(none configured)" on a deploy that did not opt in.
SEED_SECRET="dfe-engine-seed-accounts"
seed_logins() {
  local json
  json="$(kubectl -n "${NS}" get secret "${SEED_SECRET}" -o jsonpath='{.data.seed-accounts}' 2>/dev/null | base64 -d 2>/dev/null || true)"
  [ -z "$json" ] && { echo "(none configured)"; return; }
  # shellcheck disable=SC2016
  printf '%s' "$json" | python3 -c 'import sys, json
try:
    accts = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for a in accts:
    print("- `%s`  groups=[%s]" % (a.get("username", ""), ", ".join(a.get("groups", []))))'
}

# The credential block comes from `dfe-ops creds`, which owns the list of logins
# this deploy carries. A second copy here drifted from the charts twice.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
creds_block() {
  # shellcheck disable=SC2016
  python3 "${REPO_ROOT}/scripts/dfe-ops" creds --markdown --namespace "${NS}" 2>/dev/null \
    || printf 'Run `python3 scripts/dfe-ops creds` for the fetch commands.\n'
}

# Which login Argo CD offers: the deployment's IdP, or only its local admin.
argocd_block() {
  # shellcheck disable=SC2016
  python3 "${REPO_ROOT}/bootstrap/argocd_login.py" summary 2>/dev/null \
    || printf -- '- Argo CD login: run `python3 bootstrap/argocd_login.py summary` to see it.\n'
}

# Whether OTLP ingest is reachable from outside the cluster, and the token it needs (#236).
otel_ingress_block() {
  # shellcheck disable=SC2016
  python3 "${REPO_ROOT}/scripts/dfe-ops" otel-ingress 2>/dev/null \
    || printf 'Run `python3 scripts/dfe-ops otel-ingress` to see whether OTLP is exposed.\n'
}

# Which CA signed the edge, and whether the root survives a rebuild (#238).
ca_block() {
  # shellcheck disable=SC2016
  python3 "${REPO_ROOT}/scripts/dfe-ops" ca --status 2>/dev/null \
    || printf 'Run `python3 scripts/dfe-ops ca --status` for the issuer mode.\n'
}

read -r -d '' BODY <<EOF || true
# DFE access -- where everything is

Deployment tier: **${PROFILE:-unknown}**   |   Domain: **${DOMAIN}**   |   App namespace: **${NS}**

All interactive UIs sit behind the Envoy Gateway on \`*.${DOMAIN}\` (wildcard TLS),
programmed at **${GATEWAY_ADDR:-<no gateway address yet>}**; DNS for that wildcard
must resolve there. Auth is the standard pattern: OIDC at the edge when configured,
otherwise the per-app local **break-glass** account from the secret store.

## Endpoints

| What | URL | Reach |
|------|-----|-------|
$(endpoint_rows)

An "internal" UI is not exposed; reach it with
\`kubectl -n <ns> port-forward svc/<service> <localport>:<port>\`.

## OTLP ingress

\`\`\`
$(otel_ingress_block)
\`\`\`

Off unless the deployment sets \`otel.ingress.enabled\` in its deploy repo's
\`infra/common.yaml\`; the stack's own telemetry never needs it.

## How to log in + get credentials

- **OIDC configured:** log in with your IdP; RBAC role (ro/rw/admin) comes from
  your OIDC group. No password to fetch.
- **Local login (no OIDC / recovery):** the deploy MINTS \`admin\` and
  \`breakglass\`; neither is a shipped default, so fetch them from the store:

$(creds_block)

Read both with the access you deployed with -- the kubeconfig for the two Secrets
here, the host \`.env\` on docker. Rotate the admin password through that same
Secret or \`.env\` key and never delete it, because the engine reasserts that value
on every boot. The break-glass plaintext MAY be deleted once you have recorded it
offline: the engine hashed it into the deploy repo on first boot and reconciles
the account from that hash.

$(argocd_block)

## Trusting the DFE certificate

\`\`\`
$(ca_block)
\`\`\`

A self-signed deployment signs \`*.${DOMAIN}\` with its own private root, so a
browser warns and the embedded HyperDX iframe fails outright -- an iframe cannot
show the interstitial. Trust it once per box:

\`\`\`sh
python3 scripts/dfe-ops ca             # the root PEM + the install lines
python3 scripts/dfe-ops ca --install   # writes .tmp/dfe-internal-ca.crt, does the non-root half
\`\`\`

With root persistence on, that survives a rebuild. A deployment chaining to an
estate PKI (\`tls.vault\` in the gateway overlay) has nothing to install.

## Stable named logins (#106)

Seeded from config and RECONCILED on every boot, so a teardown+rebuild restores
the same shared team logins. The \`admin\` account is not one of these -- it is
minted above. Configured on this deployment:

$(seed_logins)

\`\`\`sh
# Named seed accounts (username -> password -> groups), as configured
kubectl -n ${NS} get secret dfe-engine-seed-accounts -o jsonpath='{.data.seed-accounts}' | base64 -d | jq -r '.[] | "\(.username)\t\(.password)\t[\(.groups | join(","))]"'
\`\`\`

## Smoke check

\`\`\`sh
curl -fsS -o /dev/null https://dfe.${DOMAIN}/openapi.json   # the engine answers through the gateway
kubectl -n ${NS} get pods                              # everything Running
\`\`\`

## Next steps

1. Finish the first-run wizard in the console -- your organisation, your first
   user, and your identity provider if you have one.
2. Retire the bootstrap admin from the wizard's last step (or
   \`POST /api/v1/auth/setup/retire-admin\`) once your own admin exists, then
   delete the admin Secret named in the block above. The engine stops
   reasserting the account, and the deploy repo records the retirement so a
   rebuild does not bring it back.
3. Keep the break-glass password offline, delete its plaintext, and delete this
   file -- the engine keeps only the hash.
EOF

printf '%s\n' "$BODY" | tee "$OUT"
echo
echo "==> access summary written to ${OUT}"
