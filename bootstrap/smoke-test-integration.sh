#!/usr/bin/env bash
#  Project:      dfe-infra
#  File:         smoke-test-integration.sh
#  Purpose:      VERTICAL-INTEGRATION smoke test -- verify the dependency CHAINS
#                actually work end to end, not just that pods are Running.
#                "Up" != "working": a pod can pass its liveness probe while its
#                backend connection is broken. These checks exercise the real seam.
#  Language:     Bash
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage: ./smoke-test-integration.sh [kubeconfig]
#  Namespaces default to the standard layout; override via env (DFE_NS etc.).
#  NOTE: chain commands (mongosh/curl paths) are first-cut and may need tuning to
#  the exact image tooling on first live run -- the PRINCIPLE is fixed: assert the
#  chain, never just the pod.
set -uo pipefail

[ -n "${1:-}" ] && export KUBECONFIG="$1"

NS_DATA="${DFE_DATA_NS:-cnpg}"
NS_FERRET="${DFE_FERRET_NS:-cnpg}"
NS_HYPERDX="${DFE_HYPERDX_NS:-hyperdx}"
NS_CH="${DFE_CH_NS:-clickhouse}"
NS_APP="${DFE_NS:-dfe}"

PASS=0; FAIL=0
check() {
  local name="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then echo "  [PASS] $name"; PASS=$((PASS+1)); else echo "  [FAIL] $name"; FAIL=$((FAIL+1)); fi
}

echo "=== DFE VERTICAL-INTEGRATION smoke test (chains, not liveness) ==="

echo ""
echo "--- Chain: ferretdb -> PostgreSQL (DocumentDB backend) ---"
# Write+read a doc through ferretdb. If the PG backend is unreachable this fails
# even though the ferretdb pod is Ready -- that is the whole point.
check "ferretdb write+read round-trips to PG" \
  "kubectl -n $NS_FERRET exec deploy/dfe-ferretdb -- sh -c 'mongosh mongodb://localhost:27017/smoke --quiet --eval \"db.s.insertOne({k:1}); printjson(db.s.findOne({k:1}))\"' | grep -q 'k'"

echo ""
echo "--- Chain: hyperdx -> ferretdb (app state DB) ---"
# Hit a hyperdx endpoint that forces its DB connection (its app state lives in
# ferretdb/mongo). A 200 means hyperdx reached its backend, not just booted.
check "hyperdx API reaches its ferretdb backend" \
  "kubectl -n $NS_HYPERDX exec deploy/dfe-hyperdx -- sh -c 'curl -fsS localhost:8080/api/health || wget -qO- localhost:8080/api/health' | grep -qiE 'ok|healthy|true'"

echo ""
echo "--- Chain: ClickHouse actually SERVES queries ---"
# SELECT, not pod-Ready: proves the server answers + the dfe DB exists.
check "ClickHouse answers a query (dfe DB present)" \
  "CHPW=\$(kubectl -n $NS_CH get secret clickhouse-admin-password -o jsonpath='{.data.password}' | base64 -d); kubectl -n $NS_CH exec dfe-clickhouse-0 -- clickhouse-client --user admin --password \"\$CHPW\" --query 'SHOW DATABASES' | grep -q dfe"

echo ""
echo "--- Chain: data path (receiver -> [kafka ->] loader -> ClickHouse) ---"
# The full ingest path. Post a unique event to the receiver, then poll the CH
# default table for it. Covers gRPC-direct (slim) and kafka (single/scale).
MARK="smoke-$(tr -dc a-f0-9 </dev/urandom | head -c8)"
RECV_SVC="${DFE_RECEIVER_SVC:-dfe-receiver.$NS_APP.svc.cluster.local}"
check "event posted to receiver lands in dfe.default" \
  "kubectl -n $NS_APP exec deploy/dfe-receiver -- sh -c 'curl -fsS -X POST -H \"Content-Type: application/json\" -d \"{\\\"_source\\\":\\\"smoke\\\",\\\"msg\\\":\\\"$MARK\\\"}\" http://localhost:8080/ingest' && \
   for i in \$(seq 1 30); do \
     CHPW=\$(kubectl -n $NS_CH get secret clickhouse-admin-password -o jsonpath='{.data.password}' | base64 -d); \
     if kubectl -n $NS_CH exec dfe-clickhouse-0 -- clickhouse-client --user admin --password \"\$CHPW\" --query \"SELECT count() FROM dfe.default WHERE _raw LIKE '%$MARK%'\" | grep -qE '^[1-9]'; then break; fi; \
     sleep 3; \
   done; \
   kubectl -n $NS_CH exec dfe-clickhouse-0 -- clickhouse-client --user admin --password \"\$CHPW\" --query \"SELECT count() FROM dfe.default WHERE _raw LIKE '%$MARK%'\" | grep -qE '^[1-9]'"

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
if (( FAIL > 0 )); then echo "Vertical integration NOT verified."; exit 1; else echo "Vertical integration verified."; fi
