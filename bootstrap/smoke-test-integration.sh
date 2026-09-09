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
#                  1. SELF-TELEMETRY: the DFE stack's own infra OTel (logs/metrics/
#                     traces) is landing in the OTel DB on ClickHouse VIA HyperDX.
#                     Proves the instrumentation configs AND the self-telemetry
#                     ingest pipeline are live (fresh rows, not stale).
#                  2. DATA PATH: a themed, mixed-type NDJSON fixture POSTed to the
#                     receiver lands in dfe.default on ClickHouse. Proves the
#                     customer-data ingest pipeline is live AND that structured
#                     _json ingest works -- a typed sub-column read (_json.answer)
#                     and a populated _raw are asserted, not just row presence.
#                  3. KAFKA SEAM (single/scale tiers only): the default landing
#                     topic `default_land` is created, PRODUCED to (receiver) and
#                     CONSUMED from (loader). Slim has no kafka -> skipped.
#                Ancillary sub-chains (ferretdb->PG, hyperdx->ferretdb) are
#                diagnostics that localise a CORE failure; add more per ancillary
#                service over time.
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage: ./smoke-test-integration.sh [kubeconfig]
#  Namespaces default to the standard layout; override via env (DFE_NS etc.).
#  Contract names (env-overridable) default to the SSoT:
#    dfe.default        -- dfe-schemas common-header + loader default
#    default_land       -- receiver default_source `default` + topic_suffix `_land`
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
NS_FERRET="${DFE_FERRET_NS:-cnpg}"
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
CH_DATA_TABLE="${DFE_CH_DATA_TABLE:-dfe.default}"
KAFKA_TOPIC="${DFE_KAFKA_TOPIC:-default_land}"
OTEL_DB="${DFE_OTEL_DB:-dfe}"
OTEL_LOGS_TABLE="${DFE_OTEL_LOGS_TABLE:-otel_logs}"
# Freshness window (seconds): a CORE pipeline must show data NEWER than this, so
# stale rows from a previous run cannot mask a dead pipeline. Default 10 min.
FRESH_WINDOW="${DFE_FRESH_WINDOW:-600}"

# How to find the marker row. TWO live-proven traps here:
#   1. NOT _raw: the loader only fills _raw from `raw_source_fields` (logoriginal),
#      so a plain JSON POST lands with _raw = NULL and the old `_raw LIKE` matched
#      nothing -- a real, working pipeline would have reported FAIL.
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
check() {
  local name="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then echo "  [PASS] $name"; PASS=$((PASS+1)); else echo "  [FAIL] $name"; FAIL=$((FAIL+1)); fi
}
skip() { echo "  [SKIP] $1"; SKIP=$((SKIP+1)); }

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
CH_AUTH_ARGS=""
CH_AUTH_RESOLVED=0
resolve_ch_auth() {
  [ "$CH_AUTH_RESOLVED" -eq 1 ] && return 0
  CH_AUTH_RESOLVED=1
  local pod pw
  pod="$(ch_pods | head -n1)"
  [ -z "$pod" ] && return 1

  if [ -n "${DFE_CH_USER:-}" ]; then
    CH_AUTH_ARGS="--user ${DFE_CH_USER} --password ${DFE_CH_PASSWORD:-}"
    return 0
  fi

  # `default` first: it is the account the deploy layer owns on every target.
  # `admin` stays in the list so an older deploy still resolves.
  pw="$(kubectl -n "$NS_CH" get secret "${DFE_CH_ADMIN_SECRET:-clickhouse-admin-password}" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null)"
  if [ -n "$pw" ]; then
    for u in default admin; do
      if kubectl -n "$NS_CH" exec "$pod" -- \
           clickhouse-client --user "$u" --password "$pw" --query "SELECT 1" >/dev/null 2>&1; then
        CH_AUTH_ARGS="--user $u --password $pw"
        return 0
      fi
    done
  fi

  # Default user, no password -- prove it works rather than silently degrading.
  if kubectl -n "$NS_CH" exec "$pod" -- clickhouse-client --query "SELECT 1" >/dev/null 2>&1; then
    CH_AUTH_ARGS=""
    return 0
  fi
  echo "  [WARN] no working ClickHouse credential found -- CH checks below will fail" >&2
  return 1
}

# Run a query on ONE named CH pod, echo the result.
chq_on() {
  local pod="$1" q="$2"
  resolve_ch_auth
  # shellcheck disable=SC2086  # CH_AUTH_ARGS is deliberately word-split.
  kubectl -n "$NS_CH" exec "$pod" -- clickhouse-client $CH_AUTH_ARGS --query "$q" 2>/dev/null
}

# Run a query on the FIRST CH pod -- for questions that are not about replication.
chq() {
  local pod
  pod="$(ch_pods | head -n1)"
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
  pods="$(ch_pods)"
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

echo "=== DFE VERTICAL-INTEGRATION smoke test (chains + freshness, not liveness) ==="

# ---------------------------------------------------------------------------
echo ""
echo "=== CORE 1: self-telemetry pipeline (infra OTel -> HyperDX -> ClickHouse) ==="
# The stack instruments itself: each app/daemonset emits OTLP -> otel gateway ->
# HyperDX -> ClickHouse otel tables. A row NEWER than the freshness window proves
# the pipeline is STREAMING right now (instrumentation live + ingest live), not
# that some old row exists. This is the single strongest self-monitoring assert.
check "infra OTel logs landing fresh in ${OTEL_DB}.${OTEL_LOGS_TABLE} (last ${FRESH_WINDOW}s)" \
  "test \"\$(chq \"SELECT count() FROM ${OTEL_DB}.${OTEL_LOGS_TABLE} WHERE Timestamp > now() - INTERVAL ${FRESH_WINDOW} SECOND\")\" -gt 0 2>/dev/null"

# ---------------------------------------------------------------------------
echo ""
echo "=== CORE 2: data path (receiver -> [kafka ->] loader -> ClickHouse) ==="
# Post a unique event to the receiver, then poll the CH default table for it.
# Covers gRPC-direct (slim, scale-mesh) and kafka (single/scale): same two endpoints
# either way -- event IN at the receiver, row OUT in dfe.default.
# _source `default` (not `smoke`): the receiver derives the topic as
# <default_source><topic_suffix> = default_land, which is the topic the loader
# auto-discovers and the broker ships. A _source the deploy does not know about
# would route to smoke_land -- a topic nobody consumes -- and the row would never
# arrive, failing the CORE data path for a reason that is purely the test's.
MARK="smoke-$(tr -dc a-f0-9 </dev/urandom | head -c8)"
# Post every fixture line with __MARK__ replaced by this run's marker. --data-binary
# @- feeds the JSON on stdin so a payload containing quotes/apostrophes (the Vogon
# poem) survives without shell-escaping. sent counts lines tried, posted counts 2xx.
sent=0; posted=0
if [ -r "$POST_FIXTURE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    [ -z "$line" ] && continue
    sent=$((sent+1))
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
# Poll until they land (flush interval + replication), then assert on EVERY node.
if [ "$posted" -gt 0 ]; then
  for _ in $(seq 1 30); do
    marker_on_all_nodes "$MARK" >/dev/null 2>&1 && break
    sleep 3
  done
fi
echo "  marker ${MARK} per-node counts:"
check "fixture events posted to receiver land in ${CH_DATA_TABLE} on EVERY ClickHouse node" \
  "test $sent -gt 0 && test $posted -eq $sent && marker_on_all_nodes '$MARK'"

# Prove structured _json ingest, not just that a row landed: read a TYPED sub-column
# with a typed filter (the native ClickHouse JSON path, NOT a string match). The
# zaphod fixture line carries answer=42; a typed read of _json.answer must return it.
# This is the same capability HyperDX needs to render/filter otel + default JSON.
check "native JSON typed sub-column reads back (_json.answer = 42 for this run)" \
  "test \"\$(chq \"SELECT count() FROM ${CH_DATA_TABLE} WHERE ${MARK_PREDICATE//__MARK__/$MARK} AND _json.answer.:Int64 = 42\")\" -gt 0 2>/dev/null"

# _raw carries the full source payload on the API ingest path. The loader once
# filled it only via the logoriginal->_raw rename, so a plain JSON POST landed with
# _raw = NULL (dfe-engine#182); loader v1.18.22 (#113) writes the full payload to
# _raw in Full mode, so an API-posted event now carries it regardless of logoriginal.
check "_raw is populated on the API ingest path (full payload captured for this run)" \
  "test \"\$(chq \"SELECT count() FROM ${CH_DATA_TABLE} WHERE ${MARK_PREDICATE//__MARK__/$MARK} AND length(_raw) > 0\")\" -gt 0 2>/dev/null"

# ---------------------------------------------------------------------------
echo ""
echo "=== CORE 3: kafka seam (default_land created + produced + consumed) ==="
# Only the kafka-based tiers (single/scale) run a broker. On slim and scale-mesh the
# receiver feeds the loader directly, so there is no topic to assert -> SKIP, not FAIL.
if kubectl get ns "$NS_KAFKA" >/dev/null 2>&1 && kubectl -n "$NS_KAFKA" get pods --no-headers 2>/dev/null | grep -qiE 'kafka|redpanda'; then
  # Pick the broker CLI by image: redpanda -> rpk, apache/strimzi -> kafka CLI.
  KPOD="$(kubectl -n "$NS_KAFKA" get pods --no-headers -o custom-columns=N:.metadata.name 2>/dev/null | grep -iE 'kafka|redpanda' | head -n1)"
  if kubectl -n "$NS_KAFKA" exec "$KPOD" -- sh -c 'command -v rpk' >/dev/null 2>&1; then
    # Redpanda standalone (single tier).
    check "topic ${KAFKA_TOPIC} exists (created)" \
      "kubectl -n $NS_KAFKA exec $KPOD -- rpk topic list 2>/dev/null | grep -qw '${KAFKA_TOPIC}'"
    check "topic ${KAFKA_TOPIC} has messages (receiver PRODUCED)" \
      "test \"\$(kubectl -n $NS_KAFKA exec $KPOD -- rpk topic describe ${KAFKA_TOPIC} -p 2>/dev/null | awk 'NR>1{s+=\$5} END{print s+0}')\" -gt 0"
    check "a consumer group is committed on ${KAFKA_TOPIC} (loader CONSUMED)" \
      "kubectl -n $NS_KAFKA exec $KPOD -- rpk group list 2>/dev/null | grep -q ."
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
    # listed default_land immediately). One layer of quoting, one place to get right.
    KPW="$(kubectl -n "$NS_KAFKA" get secret "${DFE_KAFKA_USER:-dfe-kafka-user}" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null)"
    kafka_cli() {
      kubectl -n "$NS_KAFKA" exec -i "$KPOD" -- sh -s <<KSH 2>/dev/null
P=/tmp/dfe-smoke.props
{
  echo 'security.protocol=SASL_PLAINTEXT'
  echo 'sasl.mechanism=SCRAM-SHA-512'
  printf 'sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username="%s" password="%s";\n' '${DFE_KAFKA_USER:-dfe-kafka-user}' '${KPW}'
} > \$P
$1
KSH
    }
    # Every assertion here captures kafka_cli's output before matching it. Piping
    # straight into `grep -q` makes grep exit on the first hit, which SIGPIPEs the
    # kubectl exec upstream; under pipefail the pipeline then reports 141 and a
    # matching check FAILS.
    check "topic ${KAFKA_TOPIC} exists (created)" \
      "printf '%s' \"\$(kafka_cli '/opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --command-config \$P --list')\" | grep -qw '${KAFKA_TOPIC}'"
    # kafka-get-offsets.sh, NOT `kafka-run-class.sh kafka.tools.GetOffsetShell`: that
    # class is GONE in Kafka 4.x (the DFE broker line), so the old check errored and
    # summed to 0 -- reporting "receiver never produced" while the topic was in fact
    # being written to and drained. Output is topic:partition:offset.
    check "topic ${KAFKA_TOPIC} has messages (receiver PRODUCED)" \
      "test \"\$(kafka_cli '/opt/kafka/bin/kafka-get-offsets.sh --bootstrap-server localhost:9092 --command-config \$P --topic ${KAFKA_TOPIC}' | awk -F: '{s+=\$3} END{print s+0}')\" -gt 0"
    check "a consumer group is committed on ${KAFKA_TOPIC} (loader CONSUMED)" \
      "test -n \"\$(kafka_cli '/opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --command-config \$P --list')\""
    # The DLQ standard's topics must exist BEFORE the first poisoned message:
    # a DLQ write happens at failure time, when nothing can be creating topics,
    # and the file backend is an EROFS no-op under the read-only rootfs.
    for DLQ_TOPIC in dfe_receiver_dlq dfe_loader_dlq dfe_archiver_dlq dfe_fetcher_dlq dfe_transform_dlq; do
      check "DLQ topic ${DLQ_TOPIC} pre-created" \
        "printf '%s' \"\$(kafka_cli '/opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --command-config \$P --list')\" | grep -qw '${DLQ_TOPIC}'"
    done
    # Dead letters must outlive a weekend, against the 72h data-topic default.
    #
    # Read from the KafkaTopic CR where one exists. Reading it from the broker
    # needs DescribeConfigs, which the app's KafkaUser is not granted and should
    # not be widened to hold for a test's sake -- under Strimzi's authorizer that
    # call returns TopicAuthorizationException and the check reports a
    # misconfigured DLQ against a correctly configured one. A Ready CR means the
    # operator has applied the spec, so it is both declared and reconciled state.
    # Redpanda runs the same user as a superuser, so the CLI path works there.
    if kubectl -n "$NS_KAFKA" get kafkatopic dfe-loader-dlq >/dev/null 2>&1; then
      check "DLQ retention is longer than the data-topic default" \
        "kubectl -n ${NS_KAFKA} get kafkatopic dfe-loader-dlq -o jsonpath='{.spec.config.retention\\.ms}' | grep -q '^604800000$'"
      check "DLQ topic CR is reconciled onto the broker" \
        "kubectl -n ${NS_KAFKA} get kafkatopic dfe-loader-dlq -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}' | grep -q True"
    else
      check "DLQ retention is longer than the data-topic default" \
        "printf '%s' \"\$(kafka_cli '/opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --command-config \$P --describe --topic dfe_loader_dlq')\" | grep -q 'retention.ms=604800000'"
    fi
  fi
elif profile_has_kafka; then
  # The tier runs a broker, so a missing one is a REAL failure. Skipping here would
  # report the brokerless-tier story for a broken kafka deploy -- the exact false
  # reassurance this gate exists to prevent.
  check "kafka broker present in ns/$NS_KAFKA (required by the $PROFILE tier)" "false"
else
  skip "kafka seam -- no broker in ns/$NS_KAFKA (brokerless tier: receiver feeds loader directly)"
fi

# ---------------------------------------------------------------------------
echo ""
echo "=== Ancillary diagnostics (localise a CORE failure; expand per service) ==="

# ClickHouse actually SERVES queries (SELECT, not pod-Ready).
check "ClickHouse answers a query (dfe DB present)" \
  "chq 'SHOW DATABASES' | grep -q dfe"

# hyperdx -> ferretdb (app state). If CORE 1 fails, this tells you whether the
# break is hyperdx<->ferretdb vs gateway<->hyperdx vs hyperdx<->clickhouse.
#
# Distinguish "HyperDX is broken" from "HyperDX is not in this profile" -- the two
# need very different actions, and slim deliberately omits it while telemetry.mode
# still names it the default OTLP destination.
if kubectl -n "$NS_HYPERDX" get deploy dfe-hyperdx >/dev/null 2>&1; then
  # /readyz on the api port (8000) is what the pod's own readiness probe uses, and
  # it only answers once the ferretdb-backed API is up. The image ships no curl.
  check "hyperdx API reaches its ferretdb backend" \
    "printf '%s' \"\$(kubectl -n $NS_HYPERDX exec deploy/dfe-hyperdx -c hyperdx -- wget -qO- http://localhost:8000/readyz)\" | grep -qi 'ready'"
else
  skip "hyperdx->ferretdb -- no dfe-hyperdx deployment in ns/$NS_HYPERDX, so this profile does not ship it. CORE 1 above says whether the OTel path still reaches ClickHouse without it."
fi

# ferretdb -> PostgreSQL (DocumentDB backend) -- the layer under hyperdx state.
#
# This check CANNOT run as written, and said FAIL for it: the ferretdb image is
# distroless -- it has no mongosh and no shell at all ("exec: sh: executable file
# not found in $PATH"), so the exec dies before mongosh is even reached. FAIL claimed
# the ferretdb->PG chain was broken while ferretdb sat 1/1 Ready and Healthy; the
# same shape of lie as the pgrep probes (assume tooling the image does not ship).
#
# SKIP, not FAIL, and LOUDLY -- an unrunnable check must never read as a broken
# chain, and must never read as a pass either (the 2026-07-16 kafka seam went
# untested behind a reassuring SKIP, which is why this prints WHY).
# To make it real, run mongosh from a client pod that has it, or assert on the PG
# side (dfe-pg-* ships psql) that ferretdb's DocumentDB schema is being written.
if kubectl -n "$NS_FERRET" exec deploy/dfe-ferretdb -- mongosh --version >/dev/null 2>&1; then
  check "ferretdb write+read round-trips to PG" \
    "kubectl -n $NS_FERRET exec deploy/dfe-ferretdb -- mongosh mongodb://localhost:27017/smoke --quiet --eval 'db.s.insertOne({k:1}); printjson(db.s.findOne({k:1}))' | grep -q 'k'"
else
  skip "ferretdb->PG round-trip -- no mongosh/shell in the ferretdb image (distroless); needs a client pod or a PG-side assert. NOT evidence the chain works."
fi

# Opt-in cleanup: remove THIS run's sample rows (scoped to the marker) once proven.
# Default keep -- the themed rows double as a live HyperDX JSON test bed. The delete
# is issued on every node so it covers replicated and per-node-scattered layouts alike.
if [ "$POST_CLEANUP" = "true" ]; then
  echo ""
  echo "=== POST cleanup: deleting this run's sample rows (DFE_POST_CLEANUP=true) ==="
  for n in $(ch_pods); do
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
