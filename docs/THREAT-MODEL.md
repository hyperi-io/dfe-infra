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
| dfe-loader, dfe-archiver, dfe-transform-* | Not exposed | Consume from the broker and write to a datastore. No listener an outsider can reach. |
| ClickHouse, Kafka, the collector | Not exposed | Cluster-internal. dfe-docker's `docs/operating.md` covers the Compose bindings. |

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
| GHSA-w9wp-h8wv-79jx | opentelemetry_sdk 0.31.x | every Rust service (through scalo-rs) | **Not reachable.** The unbounded allocation is in W3C Baggage propagation. scalo-rs registers no text-map propagator and never touches Baggage; it carries trace context itself in `src/transport/propagation.rs`, and no DFE service registers a propagator either. The fix (0.32.1) is out of reach until tracing-opentelemetry supports 0.32, which is why scalo-rs holds the 0.31 line. Revisit on that bump, or if anything registers a Baggage propagator. |
