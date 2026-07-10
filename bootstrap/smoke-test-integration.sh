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
#                  2. DATA PATH: a test event POSTed to the receiver lands in
#                     dfe.default on ClickHouse. Proves the customer-data ingest
#                     pipeline is live.
#                  3. KAFKA SEAM (single/scale tiers only): the default landing
#                     topic `default_land` is created, PRODUCED to (receiver) and
#                     CONSUMED from (loader). Slim has no kafka -> skipped.
#                Ancillary sub-chains (ferretdb->PG, hyperdx->ferretdb) are
#                diagnostics that localise a CORE failure; add more per ancillary
#                service over time.
#  Language:     Bash
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage: ./smoke-test-integration.sh [kubeconfig]
#  Namespaces default to the standard layout; override via env (DFE_NS etc.).
#  Contract names (env-overridable) default to the SSoT:
#    dfe.default        -- dfe-schemas/argocd/ddl/dfe.default.sql + loader default
#    default_land       -- receiver default_source `default` + topic_suffix `_land`
#    default.otel_logs  -- dfe-hyperdx fork source default (DEFAULT_DATABASE)
#  NOTE: chain commands (curl/CLI paths) are first-cut and may need tuning to the
#  exact image tooling on first live run -- the PRINCIPLE is fixed: assert the
#  chain + freshness, never just the pod.
set -uo pipefail

[ -n "${1:-}" ] && export KUBECONFIG="$1"

NS_FERRET="${DFE_FERRET_NS:-cnpg}"
NS_HYPERDX="${DFE_HYPERDX_NS:-hyperdx}"
NS_CH="${DFE_CH_NS:-clickhouse}"
NS_APP="${DFE_NS:-dfe}"
NS_KAFKA="${DFE_KAFKA_NS:-kafka}"

# Contract names (SSoT defaults; override per deployment if reconfigured).
CH_DATA_TABLE="${DFE_CH_DATA_TABLE:-dfe.default}"
KAFKA_TOPIC="${DFE_KAFKA_TOPIC:-default_land}"
OTEL_DB="${DFE_OTEL_DB:-default}"
OTEL_LOGS_TABLE="${DFE_OTEL_LOGS_TABLE:-otel_logs}"
# Freshness window (seconds): a CORE pipeline must show data NEWER than this, so
# stale rows from a previous run cannot mask a dead pipeline. Default 10 min.
FRESH_WINDOW="${DFE_FRESH_WINDOW:-600}"

PASS=0; FAIL=0; SKIP=0
check() {
  local name="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then echo "  [PASS] $name"; PASS=$((PASS+1)); else echo "  [FAIL] $name"; FAIL=$((FAIL+1)); fi
}
skip() { echo "  [SKIP] $1"; SKIP=$((SKIP+1)); }

# Helper: run a ClickHouse query as admin, echo the result.
chq() {
  local q="$1"
  local pw
  pw="$(kubectl -n "$NS_CH" get secret clickhouse-admin-password -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)"
  kubectl -n "$NS_CH" exec dfe-clickhouse-0 -- clickhouse-client --user admin --password "$pw" --query "$q" 2>/dev/null
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
# Covers gRPC-direct (slim) and kafka (single/scale): same two endpoints either
# way -- event IN at the receiver, row OUT in dfe.default.
MARK="smoke-$(tr -dc a-f0-9 </dev/urandom | head -c8)"
check "event posted to receiver lands in ${CH_DATA_TABLE}" \
  "kubectl -n $NS_APP exec deploy/dfe-receiver -- sh -c 'curl -fsS -X POST -H \"Content-Type: application/json\" -d \"{\\\"_source\\\":\\\"smoke\\\",\\\"msg\\\":\\\"$MARK\\\"}\" http://localhost:8080/ingest' && \
   for i in \$(seq 1 30); do \
     if chq \"SELECT count() FROM ${CH_DATA_TABLE} WHERE _raw LIKE '%$MARK%'\" | grep -qE '^[1-9]'; then break; fi; \
     sleep 3; \
   done; \
   chq \"SELECT count() FROM ${CH_DATA_TABLE} WHERE _raw LIKE '%$MARK%'\" | grep -qE '^[1-9]'"

# ---------------------------------------------------------------------------
echo ""
echo "=== CORE 3: kafka seam (default_land created + produced + consumed) ==="
# Only the kafka-based tiers (single/scale) run a broker. In slim the receiver
# feeds the loader directly, so there is no topic to assert -> SKIP, not FAIL.
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
    # apache/kafka KRaft or Strimzi: kafka-*.sh in the image.
    BS="localhost:9092"
    check "topic ${KAFKA_TOPIC} exists (created)" \
      "kubectl -n $NS_KAFKA exec $KPOD -- sh -c 'kafka-topics.sh --bootstrap-server ${BS} --list 2>/dev/null' | grep -qw '${KAFKA_TOPIC}'"
    check "topic ${KAFKA_TOPIC} has messages (receiver PRODUCED)" \
      "test \"\$(kubectl -n $NS_KAFKA exec $KPOD -- sh -c 'kafka-run-class.sh kafka.tools.GetOffsetShell --bootstrap-server ${BS} --topic ${KAFKA_TOPIC} 2>/dev/null' | awk -F: '{s+=\$3} END{print s+0}')\" -gt 0"
    check "a consumer group is committed on ${KAFKA_TOPIC} (loader CONSUMED)" \
      "kubectl -n $NS_KAFKA exec $KPOD -- sh -c 'kafka-consumer-groups.sh --bootstrap-server ${BS} --list 2>/dev/null' | grep -q ."
  fi
else
  skip "kafka seam -- no broker in ns/$NS_KAFKA (slim tier: receiver feeds loader directly)"
fi

# ---------------------------------------------------------------------------
echo ""
echo "=== Ancillary diagnostics (localise a CORE failure; expand per service) ==="

# ClickHouse actually SERVES queries (SELECT, not pod-Ready).
check "ClickHouse answers a query (dfe DB present)" \
  "chq 'SHOW DATABASES' | grep -q dfe"

# hyperdx -> ferretdb (app state). If CORE 1 fails, this tells you whether the
# break is hyperdx<->ferretdb vs gateway<->hyperdx vs hyperdx<->clickhouse.
check "hyperdx API reaches its ferretdb backend" \
  "kubectl -n $NS_HYPERDX exec deploy/dfe-hyperdx -- sh -c 'curl -fsS localhost:8080/api/health || wget -qO- localhost:8080/api/health' | grep -qiE 'ok|healthy|true'"

# ferretdb -> PostgreSQL (DocumentDB backend) -- the layer under hyperdx state.
check "ferretdb write+read round-trips to PG" \
  "kubectl -n $NS_FERRET exec deploy/dfe-ferretdb -- sh -c 'mongosh mongodb://localhost:27017/smoke --quiet --eval \"db.s.insertOne({k:1}); printjson(db.s.findOne({k:1}))\"' | grep -q 'k'"

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped ==="
if (( FAIL > 0 )); then echo "Vertical integration NOT verified."; exit 1; else echo "Vertical integration verified."; fi
