#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         smoke-test-integration.sh
#  Purpose:      VERTICAL-INTEGRATION smoke test -- verify the dependency CHAINS
#                actually work end to end, not just that pods are Running.
#                "Up" != "working": a pod can pass its liveness probe while its
#                backend connection is broken. These checks exercise the real seam.
#
#                CORE e2e tests (the START -- both DEFAULT ingest pipelines must be
#                live, streaming data, not just instrumented):
#                  0. SCHEMA CONTROL: dfe-engine reports its bootstrap pass
#                     converged. It is the only thing that creates a ClickHouse
#                     object or a bootstrap topic, so checking it first keeps a
#                     failed apply from reading as three broken pipelines.
#                  1. SELF-TELEMETRY: the DFE stack's own infra OTel (logs/metrics/
#                     traces) is landing in the OTel DB on ClickHouse VIA HyperDX.
#                     Proves the instrumentation configs AND the self-telemetry
#                     ingest pipeline are live (fresh rows, not stale).
#                  2. DATA PATH: a themed, mixed-type NDJSON fixture POSTed to the
#                     receiver lands in dfe.main on ClickHouse. Proves the
#                     customer-data ingest pipeline is live AND that structured
#                     _json ingest works -- a typed sub-column read (_json.answer)
#                     and the _raw contract are asserted, not just row presence.
#                  3. KAFKA SEAM (single/scale tiers only): the default landing
#                     topic `main_land` is created, PRODUCED to (receiver) and
#                     CONSUMED from (loader). Slim has no kafka -> skipped.
#                An external ClickHouse (ClickHouse Cloud) is queried from the
#                engine's own container as one node, and a managed broker (MSK,
#                Confluent Cloud, Redpanda Cloud) from a short-lived client pod in
#                the app namespace. DFE_CLICKHOUSE_MODE and DFE_KAFKA_MODE say which,
#                else the dfe-cluster secret's clickhouse_mode and kafka_mode.
#                Ancillary sub-chains (hyperdx->documentdb, hyperdx's collections
#                in PostgreSQL) are diagnostics that localise a CORE failure; add
#                more per ancillary service over time.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage: ./smoke-test-integration.sh [kubeconfig]
#  Namespaces default to the standard layout; override via env (DFE_NS etc.).
#  Contract names (env-overridable) default to the SSoT:
#    dfe.main           -- dfe-schemas common-header + loader default
#    main_land          -- receiver default_source `main` + topic_suffix `_land`
#    dfe-loader         -- the loader's consumer group, its source default, which no chart overrides
#    dfe.otel_logs      -- dfe-hyperdx fork otel source (otel tables in the dfe db)
#  NOTE: chain commands (curl/CLI paths) are first-cut and may need tuning to the
#  exact image tooling on first live run -- the PRINCIPLE is fixed: assert the
#  chain + freshness, never just the pod.
set -uo pipefail

[ -n "${1:-}" ] && export KUBECONFIG="$1"

# Namespace defaults MUST match the layout bootstrap.sh actually creates (see its
# namespace loop: argocd ${DFE_NAMESPACE} strimzi clickhouse otel hyperdx forgejo).
# A default that names a namespace the deploy never creates does not fail loudly --
# the checks below just find nothing, and the kafka seam SKIPS itself as "no broker"
# (live-proven 2026-07-16: NS_KAFKA defaulted to `kafka` while the broker was in
# `strimzi`, so CORE 3 reported a reassuring SKIP and the seam went untested).
NS_CNPG="${DFE_CNPG_NS:-cnpg}"
NS_CH="${DFE_CH_NS:-clickhouse}"
NS_APP="${DFE_NS:-${DFE_NAMESPACE:-dfe}}"
# HyperDX ships as an app, so it lands in the app namespace; the bare `hyperdx`
# namespace bootstrap creates is empty legacy debris. Defaulting to that empty one
# made the diagnostic below announce "HyperDX is NOT DEPLOYED" on a deploy where it
# was running and CORE 1 had just passed through it.
NS_HYPERDX="${DFE_HYPERDX_NS:-$NS_APP}"
NS_KAFKA="${DFE_KAFKA_NS:-strimzi}"

# Which tier is this? A tier that runs a broker MUST prove the kafka seam; only a
# brokerless tier may skip it. Empty = unknown -> fall back to probing for a broker.
PROFILE="${DFE_PROFILE:-}"

# profile_has_kafka() -- generated from scripts/profiles.py, the one mode table.
# An unset profile answers no, leaving the CORE 3 broker probe to decide.
# shellcheck source-path=SCRIPTDIR source=scripts/profiles.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/profiles.sh"

# Contract names (SSoT defaults; override per deployment if reconfigured).
CH_DATA_TABLE="${DFE_CH_DATA_TABLE:-dfe.main}"
KAFKA_TOPIC="${DFE_KAFKA_TOPIC:-main_land}"
LOADER_GROUP="${DFE_LOADER_GROUP:-dfe-loader}"
OTEL_DB="${DFE_OTEL_DB:-dfe}"
OTEL_LOGS_TABLE="${DFE_OTEL_LOGS_TABLE:-otel_logs}"
# Freshness window (seconds): a CORE pipeline must show data NEWER than this, so
# stale rows from a previous run cannot mask a dead pipeline. Default 10 min.
FRESH_WINDOW="${DFE_FRESH_WINDOW:-600}"

# How to find the marker row. TWO live-proven traps here:
#   1. NOT _raw: a JSON POST lands with _raw = NULL unless the payload carries its
#      own _raw field, so a `_raw LIKE` match finds nothing on a working pipeline.
#   2. toString() is REQUIRED: _json is a ClickHouse JSON column, and LIKE on it
#      errors "Illegal type JSON of argument of function like" (code 43) -- the check
#      would have failed on a query error, not on the data.
# __MARK__ is substituted by the caller.
MARK_PREDICATE="${DFE_MARK_PREDICATE:-toString(_json) LIKE '%__MARK__%'}"

# POST sample payload. CORE 2 posts a real, themed, mixed-type event set through
# the receiver rather than a bare marker, so a passing run also PROVES structured
# _json ingest (not just "a row landed"). The data lives in an NDJSON FIXTURE, not
# inline here, so it can be swapped per deployment without touching this script.
# Each line carries the token __MARK__, replaced at post time with this run's
# unique marker so the per-node assertion (and any cleanup) is scoped to THIS run.
POST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POST_FIXTURE="${DFE_POST_FIXTURE:-${POST_DIR}/fixtures/post-hitchhiker.ndjson}"
# Opt-in: delete the sample rows once the pipeline is proven. Default KEEP -- the
# themed rows are useful as a live HyperDX JSON render/filter test bed, and they
# are scoped to this run's marker so they never accumulate silently.
POST_CLEANUP="${DFE_POST_CLEANUP:-false}"

PASS=0; FAIL=0; SKIP=0
pass() { echo "  [PASS] $1"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
skip() { echo "  [SKIP] $1"; SKIP=$((SKIP+1)); }

# The failure-evidence redactor, the one place that knows what a credential looks like.
DFE_OPS="${POST_DIR}/../scripts/dfe-ops"

# `    LABEL: <first non-empty line of TEXT>`, cut at 300 characters and redacted, or withheld when it cannot be.
first_line_of() {
  local label="$1" line="" scrubbed
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -n "$line" ]] && break
  done <<< "$2"
  if [[ -z "$line" ]]; then
    echo "    ${label}: (printed nothing)"
  elif scrubbed="$(printf '%s\n' "${line:0:300}" | "$DFE_OPS" redact 2>/dev/null)" && [[ -n "$scrubbed" ]]; then
    echo "    ${label}: ${scrubbed}"
  else
    echo "    ${label}: (withheld: dfe-ops redact could not mask it)"
  fi
}

# A FAIL prints the first line its command printed, so the reason is in the log.
check() {
  local name="$1" cmd="$2" out
  # pipefail is off for the check itself: a check ending in grep -q closes the
  # pipe on the first match, and the producer's SIGPIPE would fail a passing check.
  if out="$( ( set +o pipefail; eval "$cmd" ) 2>&1 )"; then
    pass "$name"
  else
    fail "$name"
    first_line_of output "$out"
  fi
}

# cluster_fact() -- one annotation off the cluster secret every appset renders from, shared with the other bootstrap scripts.
# shellcheck source-path=SCRIPTDIR source=scripts/cluster-facts.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/cluster-facts.sh"

# Where ClickHouse and the brokers run: the deploy's own env first, else the cluster secret.
CH_MODE="${DFE_CLICKHOUSE_MODE:-$(cluster_fact clickhouse_mode)}"
KAFKA_MODE="${DFE_KAFKA_MODE:-$(cluster_fact kafka_mode)}"

# Every ClickHouse pod, in operator naming (<chi>-<cluster>-<shard>-<replica>-0).
# NOT hardcoded `dfe-clickhouse-0`, which exists in no layout the operator produces --
# every chq() call would have failed on exec, and the checks below would have read as
# a broken pipeline instead of a broken test.
ch_pods() {
  # Both layouts: operator/cluster pods (-N-N-N) AND the single-mode
  # StatefulSet (-N) -- matching only the operator shape makes every check
  # a false negative on a single-mode deploy.
  kubectl -n "$NS_CH" get pods --no-headers -o custom-columns=N:.metadata.name 2>/dev/null \
    | grep -E -- '-clickhouse-[0-9]+(-[0-9]+-[0-9]+)?$'
}

# The nodes a query can be pinned to: every pod in-cluster, one node for an external server.
ch_nodes() {
  if [ "$CH_MODE" = external ]; then echo external; else ch_pods; fi
}

# engine_ch_query() -- an external server queried from the engine's own container, shared with the other smoke tests.
# shellcheck source-path=SCRIPTDIR source=scripts/engine-clickhouse.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/engine-clickhouse.sh"

# Run by python3 in dfe-engine's container: group $1's committed offsets on topic $2 through
# librdkafka's OffsetFetch, dialled with the engine's own DFE_KAFKA_* settings and credential,
# so none crosses kubectl. Its stderr is closed, which leaves kubectl's own errors the only ones.
ENGINE_GROUP_OFFSETS_PY='import os
import sys

os.dup2(os.open(os.devnull, os.O_WRONLY), 2)
group, topic = sys.argv[1], sys.argv[2]
env = os.environ.get
try:
    from confluent_kafka import ConsumerGroupTopicPartitions
    from confluent_kafka.admin import AdminClient

    conf = {"bootstrap.servers": env("DFE_KAFKA_BOOTSTRAP_SERVERS", "")}
    protocol = env("DFE_KAFKA_SECURITY_PROTOCOL", "")
    if protocol and protocol.upper() != "PLAINTEXT":
        conf["security.protocol"] = protocol
    mechanism = env("DFE_KAFKA_SASL_MECHANISM", "")
    if mechanism:
        conf["sasl.mechanisms"] = mechanism
        conf["sasl.username"] = env("DFE_KAFKA_SASL_USERNAME", "")
        conf["sasl.password"] = env("DFE_KAFKA_SASL_PASSWORD", "")
    request = [ConsumerGroupTopicPartitions(group)]
    future = AdminClient(conf).list_consumer_group_offsets(request, request_timeout=20)[group]
    partitions = future.result(timeout=30).topic_partitions or []
except TimeoutError:
    print("Error: TimeoutError: no OffsetFetch answer within 30s")
    sys.exit(1)
except Exception as exc:
    print(f"Error: {type(exc).__name__}: {exc}")
    sys.exit(1)
rows = [p for p in partitions if p.topic == topic and p.error is None and p.offset >= 0]
print(f"committed {sum(p.offset for p in rows)} over {len(rows)} partition(s) of {topic}")
'

# engine_group_offsets: ENGINE_GROUP_OFFSETS_PY's answer for the loader's group on the landing topic.
engine_group_offsets() {
  printf '%s' "$ENGINE_GROUP_OFFSETS_PY" | kubectl -n "$NS_APP" exec -i deploy/dfe-engine -c dfe-engine -- python3 - "$LOADER_GROUP" "$KAFKA_TOPIC" 2>&1
}

# The loader's offsets on the topic, summed, or empty when none is above 0. Any GROUP column must name the loader.
loader_offsets_in() {
  printf '%s\n' "$1" | awk -v g="$LOADER_GROUP" -v t="$KAFKA_TOPIC" '
    { header = 0; for (i = 1; i <= NF; i++) if ($i == "CURRENT-OFFSET") header = 1 }
    header { gc = tc = oc = 0; for (i = 1; i <= NF; i++) { if ($i == "GROUP") gc = i; if ($i == "TOPIC") tc = i; if ($i == "CURRENT-OFFSET") oc = i }; next }
    tc && $tc == t && (!gc || $gc == g) && $oc ~ /^[0-9]+$/ { s += $oc }
    END { if (s > 0) print s }'
}

# The high watermarks summed off `rpk topic describe -p`, whose last column is always HIGH-WATERMARK.
# REPLICAS prints as `[0 1 2]`, so every other column moves with the replica count.
rpk_high_watermarks_in() {
  printf '%s\n' "$1" | awk '$NF == "HIGH-WATERMARK" { hw = 1; next } hw && $NF ~ /^[0-9]+$/ { s += $NF } END { print s + 0 }'
}

# The tools print their failures to stdout and can still exit 0, so an Error line is a failed read.
read_errored() {
  [[ "$1" -ne 0 ]] || printf '%s\n' "$2" | awk '/^(Error|ERROR)/ { e = 1 } END { exit !e }'
}

# The readers CORE 3 runs, in the broker pod or the Kafka client pod KPOD names.
rpk_partitions_read() {
  kubectl -n "$NS_KAFKA" exec "$KPOD" -- rpk topic describe "$KAFKA_TOPIC" -p 2>&1
}
rpk_group_read() {
  kubectl -n "$NS_KAFKA" exec "$KPOD" -- rpk group describe "$LOADER_GROUP" 2>&1
}
loader_group_read() {
  kafka_cli "/opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server ${KAFKA_ADDR} --command-config \$P --describe --group ${LOADER_GROUP} --offsets"
}

# A message on the topic is a high watermark above 0 on one of its partitions.
rpk_produced_check() {
  local name="topic ${KAFKA_TOPIC} has messages (receiver PRODUCED)" answer sum
  answer="$(rpk_partitions_read)"
  sum="$(rpk_high_watermarks_in "$answer")"
  if [[ "$sum" -gt 0 ]]; then
    pass "$name"
  else
    fail "$name"
    first_line_of "rpk topic describe -p, no high watermark above 0" "$answer"
  fi
}

# loader_group_check READER READ_FN: the loader's own group, since the archiver commits on _land
# too. MSK Express can answer kafka-consumer-groups.sh with UnknownServerException, so only a read
# that errors falls back to OffsetFetch from dfe-engine.
loader_group_check() {
  local reader="$1" read_fn="$2"
  local name="consumer group ${LOADER_GROUP} is committed on ${KAFKA_TOPIC} (loader CONSUMED)"
  local answer rc offset fallback
  answer="$("$read_fn")"
  rc=$?
  offset="$(loader_offsets_in "$answer")"
  if [[ -n "$offset" ]]; then
    pass "${name}: offset ${offset}, read by ${reader}"
    return
  fi
  if ! read_errored "$rc" "$answer"; then
    fail "$name"
    first_line_of "${reader}, no offset above 0 for ${LOADER_GROUP}" "$answer"
    return
  fi
  fallback="$(engine_group_offsets)"
  offset="$(printf '%s\n' "$fallback" | awk '$1 == "committed" && $2 ~ /^[0-9]+$/ { n = $2 } END { if (n > 0) print n }')"
  if [[ -n "$offset" ]]; then
    pass "${name}: offset ${offset}, read by librdkafka OffsetFetch from dfe-engine"
    first_line_of "${reader} failed first" "$answer"
  else
    fail "$name"
    first_line_of "$reader" "$answer"
    first_line_of "dfe-engine OffsetFetch" "$fallback"
  fi
}

# Run by the ClickHouse pod's shell: a password line on stdin becomes clickhouse-client's CLICKHOUSE_PASSWORD, so no argv carries it.
# shellcheck disable=SC2016  # Expanded inside the ClickHouse pod, not here.
CH_CLIENT_SH='if IFS= read -r pw; then export CLICKHOUSE_PASSWORD="$pw"; fi; exec clickhouse-client "$@" </dev/null'

# ClickHouse auth, RESOLVED once against the live cluster rather than assumed.
#
# This used to hardcode `--user admin --password $(clickhouse-admin-password)`, and on
# a deploy where that user does not exist EVERY ClickHouse check failed with
# "admin: Authentication failed ... or there is no user with such name" (code 516) --
# so the gate reported a dead pipeline against a perfectly healthy one, which is the
# exact false signal it exists to prevent. Live-proven 2026-07-17.
#
# Order: an explicit DFE_CH_USER/DFE_CH_PASSWORD wins; else the admin secret IF it
# actually authenticates; else the default user (how dfe-loader itself connects here).
# Only a working login is cached, so a server not yet answering is asked again by the next query.
CH_USER=""
CH_PASSWORD=""
CH_PASSWORD_SET=0
CH_AUTH_RESOLVED=0

# clickhouse-client on pod $1 with the resolved login and the remaining args; the password goes in on stdin.
ch_client() {
  local pod="$1"
  shift
  {
    if [ "$CH_PASSWORD_SET" -eq 1 ]; then printf '%s\n' "$CH_PASSWORD"; fi
  } | kubectl -n "$NS_CH" exec -i "$pod" -- sh -c "$CH_CLIENT_SH" sh ${CH_USER:+--user "$CH_USER"} "$@"
}

resolve_ch_auth() {
  [ "$CH_AUTH_RESOLVED" -eq 1 ] && return 0
  local pod pw u
  pod="$(ch_pods | head -n1)"
  [ -z "$pod" ] && return 1

  if [ -n "${DFE_CH_USER:-}" ]; then
    CH_USER="$DFE_CH_USER" CH_PASSWORD="${DFE_CH_PASSWORD:-}" CH_PASSWORD_SET=1 CH_AUTH_RESOLVED=1
    return 0
  fi

  # `default` first: it is the account the deploy layer owns on every target.
  # `admin` stays in the list so an older deploy still resolves.
  pw="$(kubectl -n "$NS_CH" get secret "${DFE_CH_ADMIN_SECRET:-clickhouse-admin-password}" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null)"
  if [ -n "$pw" ]; then
    CH_PASSWORD="$pw" CH_PASSWORD_SET=1
    for u in default admin; do
      CH_USER="$u"
      if ch_client "$pod" --query "SELECT 1" >/dev/null 2>&1; then
        CH_AUTH_RESOLVED=1
        return 0
      fi
    done
  fi

  # Default user, no password -- prove it works rather than silently degrading.
  CH_USER="" CH_PASSWORD="" CH_PASSWORD_SET=0
  if ch_client "$pod" --query "SELECT 1" >/dev/null 2>&1; then
    CH_AUTH_RESOLVED=1
    return 0
  fi
  echo "  [WARN] no working ClickHouse credential found -- CH checks below will fail" >&2
  return 1
}

# Run a query on ONE named CH node, echo the result.
chq_on() {
  local pod="$1" q="$2"
  if [ "$CH_MODE" = external ]; then
    engine_ch_query "$NS_APP" "$q"
    return
  fi
  resolve_ch_auth
  ch_client "$pod" --query "$q" 2>/dev/null
}

# Run a query on the FIRST CH node -- for questions that are not about replication.
chq() {
  local pod
  pod="$(ch_nodes | head -n1)"
  [ -z "$pod" ] && return 1
  chq_on "$pod" "$1"
}

# Assert a marker row is present on EVERY ClickHouse node.
#
# This is the check the 2026-07-16 "green" did not do, and it is the whole point of
# the scale tier. Querying ONE node cannot tell a replicated cluster from a
# split-brained one: with unreplicated MergeTree tables behind the round-robin
# headless service, rows scatter -- live-proven 2026-07-17, the same SELECT returned
# 2 / 2 / 3 across the three nodes. A single-node count would have passed and called
# it green. Per-node counts make a split brain impossible to miss.
marker_on_all_nodes() {
  local mark="$1" pods node_count=0 hit=0 n c
  pods="$(ch_nodes)"
  [ -z "$pods" ] && { echo "    no ClickHouse pods found in ns/$NS_CH"; return 1; }
  for n in $pods; do
    node_count=$((node_count+1))
    c="$(chq_on "$n" "SELECT count() FROM ${CH_DATA_TABLE} WHERE ${MARK_PREDICATE//__MARK__/$mark}")"
    c="${c:-0}"
    echo "    ${n}: ${c}"
    [ "$c" -gt 0 ] 2>/dev/null && hit=$((hit+1))
  done
  # Every node must hold it. Some-but-not-all IS the split brain.
  [ "$node_count" -gt 0 ] && [ "$hit" -eq "$node_count" ]
}

# Name of a broker pod, a Running one in preference; non-zero when no broker pod exists.
# Brokers are found by their provider's pod labels, because Strimzi's cruise-control and
# entity-operator pods carry "kafka" in their names and sort ahead of the brokers.
kafka_broker_pod() {
  local sel pod
  for sel in \
    'strimzi.io/broker-role=true' \
    'cluster.redpanda.com/broker=true' \
    'app.kubernetes.io/name=dfe-kafka,!app.kubernetes.io/component'; do
    pod="$(kubectl -n "$NS_KAFKA" get pods -l "$sel" --no-headers \
             -o custom-columns=N:.metadata.name,P:.status.phase 2>/dev/null \
           | awk '$2 == "Running" { print $1; found = 1; exit } !first { first = $1 } END { if (!found && first) print first }')"
    if [ -n "$pod" ]; then
      printf '%s\n' "$pod"
      return 0
    fi
  done
  return 1
}

# The kafka chart's own kafka.cliImage, read from the chart so the managed-broker client cannot drift from it.
kafka_chart_image() {
  local line image="" in_kafka=0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      kafka:*) in_kafka=1 ;;
      [![:space:]#]*) in_kafka=0 ;;
      "  cliImage: "*) [ "$in_kafka" -eq 1 ] && [ -z "$image" ] && image="${line#  cliImage: }" ;;
    esac
  done < "$1"
  image="${image%% *}"
  image="${image//\"/}"
  # A reference with no digest is a tag anyone can move, so it names no image.
  case "$image" in
    *@sha256:*) printf '%s\n' "$image" ;;
    *) return 1 ;;
  esac
}

# One NetworkPolicy port entry per distinct port the bootstrap list names, which a managed broker's brokers share.
kafka_client_ports() {
  local entry port seen=" " IFS=,
  for entry in $1; do
    port="${entry##*:}"
    case "$port" in ''|*[!0-9]*) continue ;; esac
    case "$seen" in *" $port "*) continue ;; esac
    seen="$seen$port "
    printf '        - port: %s\n          protocol: TCP\n' "$port"
  done
}

# The client pod plus the broker egress the app namespace's baseline withholds, both labelled with MARK for one delete.
kafka_client_up() {
  local image ports state
  image="${DFE_KAFKA_CLI_IMAGE:-$(kafka_chart_image "${POST_DIR}/../helm/charts/kafka/values.yaml" 2>/dev/null)}"
  ports="$(kafka_client_ports "$KAFKA_ADDR")"
  if [ -z "$ports" ] || [ -z "$image" ]; then
    echo "  [WARN] managed broker: no bootstrap address with a port, or no client image -- CORE 3 will FAIL" >&2
    return 1
  fi
  trap kafka_client_down EXIT
  trap 'exit 130' INT TERM
  kubectl apply -f - >/dev/null <<MANIFEST || return 1
apiVersion: v1
kind: Pod
metadata:
  name: ${KAFKA_CLIENT}
  namespace: ${NS_APP}
  labels:
    app.kubernetes.io/name: dfe-smoke
    app.kubernetes.io/part-of: dfe
    dfe.hyperi.io/smoke-run: ${MARK}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  terminationGracePeriodSeconds: 1
  activeDeadlineSeconds: 900
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: kafka
      image: ${image}
      command: ["sleep", "900"]
      env:
        - name: LOG_DIR
          value: /tmp
      resources:
        requests:
          cpu: 100m
          memory: 256Mi
        limits:
          memory: 768Mi
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - name: credential
          mountPath: /etc/dfe-kafka
          readOnly: true
        - name: tmp
          mountPath: /tmp
  volumes:
    - name: credential
      secret:
        secretName: ${DFE_KAFKA_USER:-dfe-kafka-user}
        defaultMode: 0440
    - name: tmp
      emptyDir: {}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ${KAFKA_CLIENT}
  namespace: ${NS_APP}
  labels:
    app.kubernetes.io/name: dfe-smoke
    app.kubernetes.io/part-of: dfe
    dfe.hyperi.io/smoke-run: ${MARK}
spec:
  podSelector:
    matchLabels:
      dfe.hyperi.io/smoke-run: ${MARK}
  policyTypes:
    - Egress
  egress:
    - ports:
${ports}
MANIFEST
  if ! kubectl -n "$NS_APP" wait --for=condition=Ready "pod/${KAFKA_CLIENT}" --timeout="${KAFKA_CLIENT_WAIT}s" >/dev/null 2>&1; then
    state="$(kubectl -n "$NS_APP" get pod "$KAFKA_CLIENT" -o 'jsonpath={.status.phase} {.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null)"
    echo "  [WARN] client pod ${KAFKA_CLIENT} not Ready after ${KAFKA_CLIENT_WAIT}s: ${state:-no status}" >&2
    return 1
  fi
}

kafka_client_down() {
  kubectl -n "$NS_APP" delete pod,networkpolicy -l "dfe.hyperi.io/smoke-run=${MARK}" --ignore-not-found --wait=false >/dev/null 2>&1
}

echo "=== DFE VERTICAL-INTEGRATION smoke test (chains + freshness, not liveness) ==="
[ "$CH_MODE" = external ] && echo "  ClickHouse is external: queried from deploy/dfe-engine's own container, as one node"

# ---------------------------------------------------------------------------
echo ""
echo "=== CORE 0: schema control (dfe-engine reports its bootstrap converged) ==="
# dfe-engine applies every ClickHouse object and every bootstrap topic at its own
# startup, from the pinned dfe-schemas manifest, and registers a `schema` check on
# /readyz that is true only when the last pass converged. Everything below reads
# what that pass made, so it is asserted first.
#
# /readyz on the API port, not the engine Service: the Service drops a NotReady
# pod from its endpoints, so a failed apply would read as "no engine" rather than
# as a failed apply. `checks.schema` by NAME, not the overall status -- the
# ClickHouse ping is the other half and the two fail for different reasons.
# GET /api/v1/system/schema carries the per-object detail and needs a token this
# script does not hold, so the failure message points there instead.
ENGINE_WAIT="${DFE_ENGINE_WAIT:-300}"
ENGINE_WAIT_INTERVAL="${DFE_ENGINE_WAIT_INTERVAL:-5}"
# The thin chart names the main container after the chart.
engine_readyz() {
  kubectl -n "$NS_APP" exec deploy/dfe-engine -c dfe-engine -- \
    curl -fsS --max-time 5 http://localhost:8000/readyz 2>/dev/null
}
engine_schema_converged() {
  printf '%s' "$(engine_readyz)" | grep -q '"schema": *true'
}
if kubectl -n "$NS_APP" get deploy dfe-engine >/dev/null 2>&1; then
  engine_waited=0
  while ! engine_schema_converged; do
    [ "$engine_waited" -ge "$ENGINE_WAIT" ] && break
    sleep "$ENGINE_WAIT_INTERVAL"
    engine_waited=$((engine_waited + ENGINE_WAIT_INTERVAL))
  done
  echo "  waited ${engine_waited}s of ${ENGINE_WAIT}s for the engine's schema check"
  check "dfe-engine reports schema converged (readyz checks.schema; detail on GET /api/v1/system/schema)" \
    engine_schema_converged
  echo "  engine /readyz: $(engine_readyz)"
else
  # Not a skip: no engine means no tables and no topics, and every CORE below
  # would fail for that one reason.
  check "dfe-engine deployment present in ns/$NS_APP (nothing else creates the schema)" "false"
fi

# Resolved here in the parent shell: every query below runs inside $(...), where a resolution is lost on return.
[ "$CH_MODE" = external ] || resolve_ch_auth

# ---------------------------------------------------------------------------
echo ""
echo "=== CORE 1: self-telemetry pipeline (infra OTel -> collector gateway -> ClickHouse) ==="
# The stack instruments itself: each app/daemonset emits OTLP to the otel gateway,
# whose clickhouse exporter writes the otel tables HyperDX reads. A row NEWER than
# the freshness window proves the pipeline is STREAMING right now (instrumentation
# live + ingest live), not that some old row exists. This is the single strongest
# self-monitoring assert.
#
# POLL, do not sample once: a stack whose collectors became Ready seconds ago has
# exported nothing yet, so one SELECT reports a streaming pipeline as a dead one
# (live-proven on rc.13: FAIL here, PASS on an unchanged re-run four minutes
# later). DFE_OTEL_WAIT=0 samples once, for a test.
OTEL_WAIT="${DFE_OTEL_WAIT:-180}"
OTEL_WAIT_INTERVAL="${DFE_OTEL_WAIT_INTERVAL:-5}"
otel_logs_fresh() {
  test "$(chq "SELECT count() FROM ${OTEL_DB}.${OTEL_LOGS_TABLE} WHERE Timestamp > now() - INTERVAL ${FRESH_WINDOW} SECOND")" -gt 0 2>/dev/null
}
otel_waited=0
while ! otel_logs_fresh; do
  [ "$otel_waited" -ge "$OTEL_WAIT" ] && break
  sleep "$OTEL_WAIT_INTERVAL"
  otel_waited=$((otel_waited + OTEL_WAIT_INTERVAL))
done
echo "  waited ${otel_waited}s of ${OTEL_WAIT}s for a row newer than ${FRESH_WINDOW}s"
check "infra OTel logs landing fresh in ${OTEL_DB}.${OTEL_LOGS_TABLE} (last ${FRESH_WINDOW}s, polled up to ${OTEL_WAIT}s)" \
  otel_logs_fresh

# ---------------------------------------------------------------------------
echo ""
echo "=== CORE 2: data path (receiver -> [kafka ->] loader -> ClickHouse) ==="
# Post a unique event to the receiver, then poll the CH landing table for it.
# Covers gRPC-direct (slim, mesh) and kafka (single/scale): same two endpoints
# either way -- event IN at the receiver, row OUT in dfe.main.
# _source `main` (not `smoke`): the receiver derives the topic as
# <default_source><topic_suffix> = main_land, which is the topic the loader
# auto-discovers and the broker ships. A _source the deploy does not know about
# would route to smoke_land -- a topic nobody consumes -- and the row would never
# arrive, failing the CORE data path for a reason that is purely the test's.
MARK="smoke-$(tr -dc a-f0-9 </dev/urandom | head -c8)"
# Post every fixture line with __MARK__ replaced by this run's marker. --data-binary
# @- feeds the JSON on stdin so a payload containing quotes/apostrophes (the Vogon
# poem) survives without shell-escaping. sent counts lines tried, posted counts 2xx,
# raw_sent counts the lines that carry a _raw field of their own.
sent=0; posted=0; raw_sent=0
if [ -r "$POST_FIXTURE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    [ -z "$line" ] && continue
    sent=$((sent+1))
    case "$line" in *'"_raw"'*) raw_sent=$((raw_sent+1)) ;; esac
    if printf '%s' "${line//__MARK__/$MARK}" | kubectl -n "$NS_APP" exec -i deploy/dfe-receiver -- \
         curl -fsS -X POST -H 'Content-Type: application/json' --data-binary @- \
         http://localhost:8080/ingest >/dev/null 2>&1; then
      posted=$((posted+1))
    fi
  done < "$POST_FIXTURE"
else
  echo "  [WARN] POST fixture not readable at $POST_FIXTURE -- CORE 2 will FAIL loudly" >&2
fi
echo "  posted ${posted}/${sent} fixture events (marker ${MARK})"
# Rows of this run on the first ClickHouse node.
marker_rows() {
  chq "SELECT count() FROM ${CH_DATA_TABLE} WHERE ${MARK_PREDICATE//__MARK__/$MARK}"
}
# Poll until they land (flush interval + replication), then assert on EVERY node.
if [ "$posted" -gt 0 ]; then
  for _ in $(seq 1 30); do
    marker_on_all_nodes "$MARK" >/dev/null 2>&1 && [ "$(marker_rows)" -ge "$posted" ] 2>/dev/null && break
    sleep 3
  done
fi
echo "  marker ${MARK} per-node counts:"
# check() discards its command's output, so the counts are printed by a call of their own.
marker_on_all_nodes "$MARK" || true
check "fixture events posted to receiver land in ${CH_DATA_TABLE} on EVERY ClickHouse node" \
  "test $sent -gt 0 && test $posted -eq $sent && marker_on_all_nodes '$MARK'"

# Prove structured _json ingest, not just that a row landed: read a TYPED sub-column
# with a typed filter (the native ClickHouse JSON path, NOT a string match). The
# zaphod fixture line carries answer=42; a typed read of _json.answer must return it.
# This is the same capability HyperDX needs to render/filter otel + default JSON.
check "native JSON typed sub-column reads back (_json.answer = 42 for this run)" \
  "test \"\$(chq \"SELECT count() FROM ${CH_DATA_TABLE} WHERE ${MARK_PREDICATE//__MARK__/$MARK} AND _json.answer.:Int64 = 42\")\" -gt 0 2>/dev/null"

# dfe-loader stores a JSON payload once, in _json, so _raw stays NULL unless the payload
# carries a _raw field of its own, which is kept as sent. A value starting with "{" would
# be the whole payload copied over it. The fixture's towel-check line carries one.
if [ "$raw_sent" -gt 0 ]; then
  check "a payload's own _raw is kept as sent (${raw_sent} of ${sent} fixture lines carry one)" \
    "test \"\$(chq \"SELECT count() FROM ${CH_DATA_TABLE} WHERE ${MARK_PREDICATE//__MARK__/$MARK} AND length(_raw) > 0 AND NOT startsWith(_raw, '{')\")\" -eq $raw_sent 2>/dev/null"
else
  skip "payload-supplied _raw -- no line of ${POST_FIXTURE} carries a _raw field"
fi
check "payloads without a _raw field land with _raw NULL (stored once, in _json)" \
  "test $sent -gt $raw_sent && test \"\$(chq \"SELECT count() FROM ${CH_DATA_TABLE} WHERE ${MARK_PREDICATE//__MARK__/$MARK} AND _raw IS NULL\")\" -eq $((sent - raw_sent)) 2>/dev/null"

# ---------------------------------------------------------------------------
echo ""
echo "=== CORE 3: kafka seam (main_land created + produced + consumed) ==="
# Only the kafka-based tiers (single/scale) run a broker. On slim and mesh the
# receiver feeds the loader directly, so there is no topic to assert -> SKIP, not FAIL.
KPOD=""
# The namespace the Kafka CLI runs in and the address it dials from there.
KAFKA_CLI_NS="$NS_KAFKA"
KAFKA_ADDR="localhost:9092"
KAFKA_CLIENT_WAIT="${DFE_KAFKA_CLIENT_WAIT:-180}"
if [ "$KAFKA_MODE" = external ]; then
  # A managed broker has no pod to exec into, so the CLI runs beside the apps on their own credential.
  KAFKA_CLI_NS="$NS_APP"
  KAFKA_ADDR="${DFE_KAFKA_BOOTSTRAP:-$(cluster_fact kafka_bootstrap)}"
  KAFKA_CLIENT="dfe-${MARK}-kafka"
  echo "  managed broker: CORE 3 runs from client pod ${KAFKA_CLIENT} in ns/${NS_APP} over SASL_SSL"
  kafka_client_up && KPOD="$KAFKA_CLIENT"
elif kubectl get ns "$NS_KAFKA" >/dev/null 2>&1; then
  KPOD="$(kafka_broker_pod)"
fi
if [ -n "$KPOD" ]; then
  # Pick the broker CLI by image: redpanda -> rpk, apache/strimzi and the managed-broker client -> kafka CLI.
  if [ "$KAFKA_MODE" != external ] && kubectl -n "$NS_KAFKA" exec "$KPOD" -- sh -c 'command -v rpk' >/dev/null 2>&1; then
    # Redpanda standalone (single tier).
    check "topic ${KAFKA_TOPIC} exists (created)" \
      "kubectl -n $NS_KAFKA exec $KPOD -- rpk topic list 2>/dev/null | grep -qw '${KAFKA_TOPIC}'"
    rpk_produced_check
    loader_group_check "rpk group describe" rpk_group_read
  else
    # apache/kafka KRaft or Strimzi. Two things the first cut got wrong, both
    # live-proven broken on Strimzi 2026-07-17 (see scripts/deploy_matrix.py
    # _accept_kafka_strimzi, which already had this right):
    #   1. the kafka-*.sh tools are NOT on PATH -- they live in /opt/kafka/bin
    #      (bare `kafka-topics.sh` -> "not found"; an absolute path works for the
    #      apache/kafka single-tier image too).
    #   2. DFE brokers require SASL/SCRAM-SHA-512 on EVERY listener, so an
    #      unauthenticated client just hangs until "Timed out waiting for a node
    #      assignment" -- every check would fail for the wrong reason.
    # SCRAM creds: the operator mints them into the broker's own namespace. The props
    # file is written INSIDE the pod so the password never lands in a host-side
    # process arg. Empty password -> the checks fail loudly (correct: no credential
    # means the seam genuinely cannot be proven).
    #
    # kafka_cli() exists because the previous form inlined the JAAS string -- which
    # itself contains double quotes -- through `check`'s eval and a nested sh -c. The
    # quoting did not survive, so every CORE 3 check failed on mangled args while the
    # seam underneath was fine (live-proven 2026-07-17: the same commands run by hand
    # listed main_land immediately). One layer of quoting, one place to get right.
    #
    # A managed broker serves SASL_SSL only, and its client reads dfe-kafka-user's
    # mechanism, username and password from the Secret mounted in its own pod, so
    # no credential crosses kubectl at all.
    KPW=""
    if [ "$KAFKA_MODE" != external ]; then
      KPW="$(kubectl -n "$NS_KAFKA" get secret "${DFE_KAFKA_USER:-dfe-kafka-user}" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null)"
    fi
    # The pod-side lines writing the client config to $P, escaping the quotes and backslashes a JAAS string cannot carry raw.
    kafka_client_props() {
      if [ "$KAFKA_MODE" = external ]; then
        cat <<'PROPS'
D=/etc/dfe-kafka
M=$(cat $D/sasl.mechanism)
L=org.apache.kafka.common.security.scram.ScramLoginModule
[ "$M" = PLAIN ] && L=org.apache.kafka.common.security.plain.PlainLoginModule
{
  echo 'security.protocol=SASL_SSL'
  echo "sasl.mechanism=$M"
  printf 'sasl.jaas.config=%s required username="%s" password="%s";\n' "$L" "$(sed 's/[\\"]/\\&/g' $D/username)" "$(sed 's/[\\"]/\\&/g' $D/password)"
} > $P
PROPS
      else
        cat <<KPROPS
{
  echo 'security.protocol=SASL_PLAINTEXT'
  echo 'sasl.mechanism=SCRAM-SHA-512'
  printf 'sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username="%s" password="%s";\n' '${DFE_KAFKA_USER:-dfe-kafka-user}' '${KPW}'
} > \$P
KPROPS
      fi
    }
    # The props file holds the password, so it is private to the pod user and
    # removed before the shell exits.
    kafka_cli() {
      kubectl -n "$KAFKA_CLI_NS" exec -i "$KPOD" -- sh -s <<KSH 2>/dev/null
P=\$(umask 077; mktemp)
$(kafka_client_props)
$1
rc=\$?
rm -f \$P
exit \$rc
KSH
    }
    # Every assertion here captures kafka_cli's output before matching it. Piping
    # straight into `grep -q` makes grep exit on the first hit, which SIGPIPEs the
    # kubectl exec upstream; under pipefail the pipeline then reports 141 and a
    # matching check FAILS.
    check "topic ${KAFKA_TOPIC} exists (created)" \
      "printf '%s' \"\$(kafka_cli '/opt/kafka/bin/kafka-topics.sh --bootstrap-server ${KAFKA_ADDR} --command-config \$P --list')\" | grep -qw '${KAFKA_TOPIC}'"
    # kafka-get-offsets.sh, NOT `kafka-run-class.sh kafka.tools.GetOffsetShell`: that
    # class is GONE in Kafka 4.x (the DFE broker line), so the old check errored and
    # summed to 0 -- reporting "receiver never produced" while the topic was in fact
    # being written to and drained. Output is topic:partition:offset.
    check "topic ${KAFKA_TOPIC} has messages (receiver PRODUCED)" \
      "test \"\$(kafka_cli '/opt/kafka/bin/kafka-get-offsets.sh --bootstrap-server ${KAFKA_ADDR} --command-config \$P --topic ${KAFKA_TOPIC}' | awk -F: '{s+=\$3} END{print s+0}')\" -gt 0"
    loader_group_check kafka-consumer-groups.sh loader_group_read
    # The DLQ standard's topics must exist BEFORE the first poisoned message:
    # a DLQ write happens at failure time, when nothing can be creating topics,
    # and the file backend is an EROFS no-op under the read-only rootfs.
    for DLQ_TOPIC in dfe_receiver_dlq dfe_loader_dlq dfe_archiver_dlq dfe_fetcher_dlq dfe_transform_dlq; do
      check "DLQ topic ${DLQ_TOPIC} pre-created" \
        "printf '%s' \"\$(kafka_cli '/opt/kafka/bin/kafka-topics.sh --bootstrap-server ${KAFKA_ADDR} --command-config \$P --list')\" | grep -qw '${DLQ_TOPIC}'"
    done
    # Dead letters must outlive a weekend, against the 72h data-topic default.
    # The DFE user holds DescribeConfigs on every broker (kafka-user.yaml), so a
    # describe that shows no retention is a FAIL, never a skip.
    check "DLQ retention is longer than the data-topic default (dfe_loader_dlq retention.ms=604800000)" \
      "printf '%s' \"\$(kafka_cli '/opt/kafka/bin/kafka-topics.sh --bootstrap-server ${KAFKA_ADDR} --command-config \$P --describe --topic dfe_loader_dlq')\" | grep -q 'retention.ms=604800000'"
  fi
elif [ "$KAFKA_MODE" = external ]; then
  # A managed broker is never the brokerless tier, so a client that cannot start is a FAIL.
  check "managed-broker client pod Ready in ns/$NS_APP (dfe-kafka-user mounted, waited ${KAFKA_CLIENT_WAIT}s)" "false"
elif profile_has_kafka; then
  # The tier runs a broker, so a missing one is a REAL failure. Skipping here would
  # report the brokerless-tier story for a broken kafka deploy -- the exact false
  # reassurance this gate exists to prevent.
  check "kafka broker pod present in ns/$NS_KAFKA (required by the $PROFILE tier)" "false"
else
  skip "kafka seam -- no broker pod in ns/$NS_KAFKA (brokerless tier: receiver feeds loader directly)"
fi
if [ "$KAFKA_MODE" = external ]; then
  kafka_client_down
  trap - EXIT INT TERM
fi

# ---------------------------------------------------------------------------
echo ""
echo "=== Ancillary diagnostics (localise a CORE failure; expand per service) ==="

# ClickHouse actually SERVES queries (SELECT, not pod-Ready).
check "ClickHouse answers a query (dfe DB present)" \
  "chq 'SHOW DATABASES' | grep -q dfe"

# hyperdx -> documentdb (app state). If CORE 1 fails, this tells you whether the
# break is hyperdx<->documentdb vs gateway<->hyperdx vs hyperdx<->clickhouse.
#
# Distinguish "HyperDX is broken" from "HyperDX is not in this profile" -- the two
# need very different actions, and slim deliberately omits it while telemetry.mode
# still names it the default OTLP destination.
hyperdx_readyz() {
  kubectl -n "$NS_HYPERDX" exec deploy/dfe-hyperdx -c dfe-hyperdx -- node -e \
    "fetch('http://localhost:8000/readyz',{signal:AbortSignal.timeout(10000)}).then(async r=>{const b=await r.json();process.exit(r.status===200&&b.status==='ready'?0:1)}).catch(()=>process.exit(1))"
}
if kubectl -n "$NS_HYPERDX" get deploy dfe-hyperdx >/dev/null 2>&1; then
  # /readyz on the api port (8000) is what the pod's own readiness probe uses, and
  # it only answers once the documentdb-backed API is up. The image ships neither wget
  # nor curl, so node's fetch asks it and the exit code carries the verdict.
  check "hyperdx API reaches its documentdb backend" hyperdx_readyz
else
  skip "hyperdx->documentdb -- no dfe-hyperdx deployment in ns/$NS_HYPERDX, so this profile does not ship it. CORE 1 above says whether the OTel path still reaches ClickHouse without it."
fi

# hyperdx's state in PostgreSQL -- the layer under the gateway. The postgres
# container of the primary ships psql, and the extension records every collection
# a client created, so a hyperdx collection there proves its writes reached the
# store through the gateway.
documentdb_primary() {
  kubectl -n "$NS_CNPG" get pods -l cnpg.io/cluster=dfe-documentdb,cnpg.io/instanceRole=primary -o name 2>/dev/null | head -n 1
}
hyperdx_collections() {
  local primary
  primary="$(documentdb_primary)"
  [ -n "$primary" ] || return 1
  kubectl -n "$NS_CNPG" exec "$primary" -c postgres -- psql -d postgres -tAc \
    "SELECT count(*) FROM documentdb_api_catalog.collections WHERE database_name = 'hyperdx'" | grep -qE '^[1-9]'
}
if ! kubectl -n "$NS_HYPERDX" get deploy dfe-hyperdx >/dev/null 2>&1; then
  skip "hyperdx collections in PostgreSQL -- no dfe-hyperdx deployment in ns/$NS_HYPERDX, so nothing writes to the store"
elif [ -z "$(documentdb_primary)" ]; then
  check "the DocumentDB store in ns/$NS_CNPG has a primary" false
else
  check "hyperdx's collections are in the DocumentDB store's PostgreSQL" hyperdx_collections
fi

# Opt-in cleanup: remove THIS run's sample rows (scoped to the marker) once proven.
# Default keep -- the themed rows double as a live HyperDX JSON test bed. The delete
# is issued on every node so it covers replicated and per-node-scattered layouts alike.
if [ "$POST_CLEANUP" = "true" ]; then
  echo ""
  echo "=== POST cleanup: deleting this run's sample rows (DFE_POST_CLEANUP=true) ==="
  for n in $(ch_nodes); do
    chq_on "$n" "ALTER TABLE ${CH_DATA_TABLE} DELETE WHERE ${MARK_PREDICATE//__MARK__/$MARK}" >/dev/null 2>&1
    echo "    ${n}: delete mutation submitted"
  done
else
  echo ""
  echo "  POST sample rows KEPT (marker ${MARK}); set DFE_POST_CLEANUP=true to auto-delete after proof."
fi

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped ==="
if (( FAIL > 0 )); then echo "Vertical integration NOT verified."; exit 1; else echo "Vertical integration verified."; fi
