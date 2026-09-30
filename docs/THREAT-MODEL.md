<!--
Project:   dfe-infra
File:      docs/THREAT-MODEL.md
Purpose:   Exposure model for DFE risk analysis -- what an attacker can reach
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# DFE exposure model

**The vast majority of this stack has no internet-facing and no user-facing
exposure.** Two components do; everything else consumes from the broker or is
reached only from inside the deployment.

Read this before assessing any security finding, advisory or scanner warning in
a DFE repo. A severity score describes the flaw. This describes whether anything
in DFE can reach it, and a finding cannot be graded on the score alone.

## What is actually exposed

| Component | Exposure | Notes |
|---|---|---|
| **dfe-receiver** | **Internet / user facing** | The ingest edge. Takes arbitrary data from whoever can route to it. The primary attack surface. |
| **dfe-ui** | **User facing** | Browser-facing Next.js app, behind auth in every deployment that has an issuer. |
| dfe-fetcher | Outbound only | Reaches external APIs, but **it initiates** every connection. Its ingest port is off by default. |
| dfe-engine | CLI and API | Reachable by operators, not by the public. The API is authenticated. |
| dfe-loader, dfe-archiver, dfe-transform-* | Not exposed | Consume from the broker and write to a datastore. On the direct transport, dfe-loader, dfe-archiver, dfe-transform-vrl and dfe-transform-vector each bind a plaintext, unauthenticated gRPC Push listener on a ClusterIP Service, port 6000 (`dfe-common.pushService`). dfe-transform-elastic's direct-transport listener is implemented but not wired in yet (dfe-transform-elastic#19). The namespace baseline NetworkPolicy (`dfe-ingress-policy`, `helm/charts/network-policies`) admits the gateway namespace and every DFE and otel namespace, so the listener is reachable only from pods in those namespaces, never from outside the cluster. |
| ClickHouse, Kafka, the collector | Not exposed | Cluster-internal. On Kubernetes the apps reach ClickHouse over verified TLS (below). dfe-docker's `docs/operating.md` covers the Compose bindings. |
| The collector's OTLP ingress | **Off by default. When a deployment sets `otel.ingress.enabled`: reachable from outside the cluster, authenticated** | `otel.<domain>` on the gateway reaches a second OTLP/HTTP receiver that refuses any request without the bearer token from the deployment's secret store. The check is the collector's own (`bearertokenauth`), so it holds however that port is reached. Before the token is checked, the collector's HTTP server and the extension parse an outsider's request: on such a deployment, grade an advisory in either as reachable. The in-cluster receiver on 4317/4318 stays unauthenticated and no route publishes it. |

## In-cluster transport

A deployment that carries a CA runs its in-cluster hops over TLS with verification on; one without stays on plain HTTP, because there is nothing to verify against. A Kubernetes deployment with the edge module on (bootstrap's default) carries one: bootstrap.sh detects or installs cert-manager, and the gateway chart's `dfe-internal-ca` ClusterIssuer signs. dfe-docker Compose, the `helm/dfe-stack` trial, and a cluster with the edge module off stay on HTTP unless an issuer is named in `clickhouse.tls.issuerRef`.

| Hop | Transport |
|---|---|
| dfe-engine, its hunt runner and keda shim -> ClickHouse | HTTPS 8443, verified against the certificate's CA |
| dfe-loader -> ClickHouse | HTTPS 8443, verified |
| dfe-hyperdx -> ClickHouse | HTTPS on the connection the engine hands it, verified |
| OTel collector -> ClickHouse | Native 9000, plaintext. Backlog: moves to 9440 |
| Smoke tests and `scripts/` -> ClickHouse | HTTP 8123, plaintext. Backlog |

8123 and 9000 stay open beside 8443 and 9440 (`clickhouse.tls.required: false`) until the last two rows move. Kafka, PostgreSQL, FerretDB and Keeper are not covered yet. The server keypair (`dfe-clickhouse-tls`, with `tls.key`) is readable only from the clickhouse namespace: the apps get a CA-only copy, through a ClusterSecretStore that serves only the namespaces it copies into. Issuer choice: [DEPLOY-TLS-TRUST.md](DEPLOY-TLS-TRUST.md).

## What that means for a finding

Grade by reachability first, then severity:

1. **Is the flawed code path reachable from dfe-receiver or dfe-ui?** If yes,
   treat it seriously whatever the score, because the input is untrusted.
2. **Is it only reachable from outbound traffic we initiate, or from an
   operator-authenticated path?** Then a mid-range score is usually a
   risk-accept, not a scramble.
3. **Is it in a dependency of a component with no listener at all?** Reaching it
   requires already being inside the deployment, which is a different incident.

A dependency advisory in dfe-loader is not the same finding as the identical
advisory in dfe-receiver, and it should not produce the same response.

## Recording the decision

Every finding ends in one of three states -- fixed, closed as not required, or
marked in the code as not-relevant or risk-accepted. **Marked means at the line
it concerns**, with the reachability reasoning, so the next reader and the next
scanner run both find it. An advisory that is re-assessed from scratch every
few weeks has not been accepted; it has been ignored repeatedly.

Where an advisory has no fixed release, marking it is the ONLY resolution
available short of replacing the dependency, so make the call and write it down
rather than leaving the warning to fire forever.

## Accepted risks

| Advisory | Package | Component | Decision |
|---|---|---|---|
| PYSEC-2026-2447 | diskcache 5.6.3 | dfe-engine | **Accepted.** No fixed release exists. dfe-engine is operator-facing (CLI + authenticated API), not internet-facing, and the cache is not fed from untrusted input. Revisit if a fix ships or if diskcache moves onto a request path. |
| GHSA-w9wp-h8wv-79jx | opentelemetry_sdk 0.31.x | every Rust service (through scalo-rs) | **Not reachable.** The unbounded allocation is in W3C Baggage propagation. scalo-rs registers no text-map propagator and never touches Baggage; it carries trace context itself in `src/transport/propagation.rs`, and no DFE service registers a propagator either. The fix (0.32.1) is out of reach while metrics-exporter-opentelemetry, which scalo-rs's `otel-metrics` feature builds on, requires opentelemetry ^0.31 -- its newest release is 0.2.1 (2025-11-15). tracing-opentelemetry is not the blocker: 0.33 pairs with opentelemetry 0.32. Revisit when that exporter supports 0.32, or if anything registers a Baggage propagator. |
| Design: VRL env and network functions | vrl 0.34 default features (`enable_env_functions`, `enable_network_functions`) | dfe-transform-vrl | **Accepted for 2.2.** A transform program can call `get_env_var` and `http_request`, so whoever can write a transform can read the pod's environment, the Kafka SASL credential included, and send it anywhere the pod can reach. Only a user the engine authenticates and authorises for transform writes can author one, through the API or dfe-ui. Ingested data cannot, so this is the operator trusting their own transform authors. Grant transform write accordingly. Revisit when the engine refuses these functions at transform save, or the crate drops the two features. |
