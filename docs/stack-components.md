<!--
  Project:      dfe-infra
  File:         docs/stack-components.md
  Purpose:      Component class map for the DFE stack version SSoT - which
                render class each moving part belongs to, which chart/CR or
                .env var renders it, and per-path cascade rules. The reference
                that scripts/dfe-stack render + check_versions_drift.py follow.
  Language:     Markdown
  License:      FSL-1.1-ALv2
  Copyright:    (c) 2026 HYPERI PTY LIMITED
-->

# DFE stack components - render class map

"DFE X.Y.Z" is one logical version that pins every moving part across BOTH the
k8s path (helm charts / CRs / appsets) and the docker path (dfe-docker `.env`).
One logical version, two renderings. This file classifies every component so the
render tool and the drift-check know how to treat each one.

`versions.yaml` is the SSoT. Each component below names its SSoT field, how the
k8s path renders it, how the docker path renders it, and the cascade rule.

## Classes at a glance

| Class | Meaning | k8s render | docker render |
|-------|---------|-----------|---------------|
| A | dfe-* owned app (GHCR) | app chart appVersion == `apps.<n>`; image `tag@digest` | `.env` `DFE_<N>_VERSION=tag@digest` |
| B | third-party via an operator + a CR WE author | OUR chart templates the CR image/version from the SSoT logical version; the operator chart is a SEPARATE k8s-only pin | plain upstream image in `.env` |
| C | upstream chart we do NOT template, k8s-ONLY | appset/chart pins the chart version | none (not in compose) |
| D | third-party plain image in BOTH paths | OUR chart sets the image via values from the SSoT | same upstream image in `.env` |
| E | third-party plain image, docker-ONLY (k8s uses a different mechanism, or the k8s equivalent is not yet wired) | none, or a different component does the job | plain upstream image in `.env` |

Class E is the addition this audit forced: not every docker image has a k8s
cascade (the compose stack needs a reverse proxy and a Mongo-wire backend that
k8s solves with different components). Rendering must emit these for docker only.

## Class A - dfe-* owned apps

Same GHCR image, same tag both paths. SSoT: `apps.<n>` (tag) + `digests.<n>`
(sha256). k8s: `helm/charts/dfe-<n>` appVersion == `apps.<n>` (drift-check
enforced); `dfe-common.image` renders `repo:<tag>` and a `tag@sha256` value is a
valid pull spec, so the digest flows through values. docker: `.env`
`DFE_<N>_VERSION=<tag>@<digest>`.

| App | SSoT | docker `.env` var | Published? |
|-----|------|-------------------|-----------|
| dfe-engine | apps.dfe-engine | DFE_ENGINE_VERSION | yes |
| dfe-ui | apps.dfe-ui | DFE_UI_VERSION | yes |
| dfe-receiver | apps.dfe-receiver | DFE_RECEIVER_VERSION | yes |
| dfe-loader | apps.dfe-loader | DFE_LOADER_VERSION | yes |
| dfe-archiver | apps.dfe-archiver | DFE_ARCHIVER_VERSION | yes |
| dfe-fetcher | apps.dfe-fetcher | DFE_FETCHER_VERSION | yes |
| dfe-transform-vrl | apps.dfe-transform-vrl | DFE_TRANSFORM_VRL_VERSION | yes |
| dfe-transform-vector | apps.dfe-transform-vector | DFE_TRANSFORM_VECTOR_VERSION | yes |
| dfe-transform-wasm | apps.dfe-transform-wasm | (n/a) | NO - alpha |
| dfe-transform-elastic | apps.dfe-transform-elastic | (n/a) | NO - alpha |
| dfe-transform-splack | apps.dfe-transform-splack | (n/a) | NO - alpha |
| dfe-hyperdx (fork) | content.dfe-hyperdx | DFE_HYPERDX_VERSION | NO - first image pending |

Notes:
- The three transforms (wasm/elastic/splack) are PRE-GA/alpha and NOT published
  to GHCR (no digest). `dfe-stack` already excludes any app without a digest from
  rendered manifests - loudly. They stay excluded from a stack cut until they
  publish. Do not render them.
- dfe-hyperdx is the HyperDX fork. k8s chart pulls `ghcr.io/hyperi-io/dfe-hyperdx`
  (tag = fork CODE_VERSION via appVersion). docker pulls
  `ghcr.io/hyperi-io/hyperi-hyperdx` (NOTE: a DIFFERENT image name than k8s -
  `hyperi-hyperdx` vs `dfe-hyperdx`; reconcile the name, or the render must map
  the two). Its SSoT lives in `content.dfe-hyperdx` (PENDING-FIRST-IMAGE), not
  `apps:`. Render it as excluded until the fork image publishes (landmine 7).

## Class B - third-party via an operator + a CR we author

The operator chart appVersion is NOT ours to move. The SERVICE version we own is
set in the CR our chart templates. SSoT holds the LOGICAL service version; the
operator chart is a SEPARATE, k8s-only pin on its own release line.

### ClickHouse
- SSoT logical version: `services.clickhouse-version` (26.3.17.56, LTS line).
- k8s: `helm/charts/clickhouse-cluster` values `clickhouse.version` ->
  ClickHouseCluster CR `spec.image`; keeper image tag mirrors it. Operator:
  `operators.clickhouse-operator` (0.0.7, k8s-only, scale mode only).
- docker: `CLICKHOUSE_VERSION` -> `clickhouse/clickhouse-server:<ver>@digest`.
- Cascade: SAME upstream image both sides (`clickhouse/clickhouse-server`). One
  number, both refs. Clean.

### Kafka
- SSoT logical version: `services.kafka-version` (4.2.0 -- the strimzi 0.51 ceiling).
- k8s: `helm/charts/kafka` values `kafka.version` -> Strimzi Kafka CR
  `spec.kafka.version`. Strimzi then pulls its OWN internal
  `quay.io/strimzi/kafka:*-kafka-4.2.x` image - we never name that image.
  Operator: `operators.strimzi-kafka-operator` (0.51.0, k8s-only).
- docker: `APACHE_KAFKA_VERSION` -> `apache/kafka:<ver>@digest`.
- Cascade: TWO IMAGES for one logical number (strimzi-internal vs apache/kafka).
  Same logical 4.x, two renderers each knowing their own image. This is the
  load-bearing reason the model is "logical version, two renderings".
- OPERATOR CEILING (hard constraint): Strimzi 0.51 supports Kafka 4.1.x / 4.2.0
  ONLY. The logical `kafka-version` must never exceed what the pinned strimzi
  operator supports, or the k8s path breaks. Renovate is bounded to that ceiling
  by the org preset (see "Renovate and the operator ceilings") - bump it by hand,
  verified against strimzi's kafka-versions.yaml, in lockstep with the operator.

### Redpanda (opt-in, BSL)
- SSoT logical version: `services.redpanda-version` (v26.1.8).
- k8s: `helm/charts/kafka` values `kafka.redpanda.image.tag` -> Redpanda CR
  `spec.clusterSpec.image.tag`. Operator: `operators.redpanda-operator` (26.1.6,
  k8s-only, opt-in). The broker tag is PAIRED with the operator chart - move them
  together (also bounded in the org preset, for the same reason as kafka).
- docker: `REDPANDA_VERSION` -> `redpandadata/redpanda:<ver>@digest`.
- Cascade: SAME image both sides. One number, both refs.

## Class C - upstream chart, k8s-only, no docker, no cascade

Not in compose. SSoT pins the chart version; appset/bootstrap uses it;
drift-check enforces where a hardcoded pin exists. Already works - leave as-is.

- bootstrap: `cert-manager`, `external-secrets`, `argocd`, `valkey` (plain
  manifest image), `local-path-provisioner`.
- operators: `envoy-gateway`, `external-dns`, `keda`, `metrics-server`,
  `reloader`, `cloudnative-pg`, and the three OPERATOR chart pins
  (`strimzi-kafka-operator`, `clickhouse-operator`, `redpanda-operator` - the
  operator's own release line, distinct from the class-B service version).
- data: `postgresql` (CNPG PG major), `otel-collector`, `forgejo` (bundled
  deploy-repo fallback, k8s-only).

## Class D - third-party plain image, BOTH paths

OUR chart sets the image via values from the SSoT; docker sets the same upstream
image. One number, same image ref both sides.

### Kafbat (Kafka UI)
- SSoT: `services.kafbat` (v1.5.0@sha256:...). k8s: `helm/charts/kafbat` `image.tag`.
  docker: `KAFBAT_VERSION` -> `ghcr.io/kafbat/kafka-ui`. ALIGNED today.

### FerretDB (Mongo-wire metadata store for HyperDX)
- SSoT: `services.ferretdb` (2.7.0). k8s: `helm/charts/ferretdb` (image tag defaults
  to Chart appVersion). docker: `HYPERDX_FERRETDB_VERSION` ->
  `ghcr.io/ferretdb/ferretdb`.
- KNOWN GAP (see below): the k8s ferretdb chart appVersion is 1.24.0, NOT 2.7.0.

## Class E - third-party plain image, docker-ONLY

### nginx (dfe-proxy)
- docker: `DFE_PROXY_VERSION` -> `nginx:1.31-alpine@digest`. Reverse proxy for the
  compose stack. k8s does this job with envoy-gateway instead, so there is NO k8s
  nginx cascade. NOT in versions.yaml today - ADD it as a docker-only pin so a
  complete stack version pins it too (else `make stack` cannot fill
  `DFE_PROXY_VERSION`, which is a hard-fail `${...:?}` in compose).

### postgres-documentdb (FerretDB 2.x PG backend)
- SSoT: `services.documentdb-pg` (17-0.107.0-ferretdb-2.7.0). docker:
  `HYPERDX_POSTGRES_VERSION` -> `ghcr.io/ferretdb/postgres-documentdb`.
- Docker-only FOR NOW: k8s currently backs FerretDB with vanilla CNPG PG17, not
  the documentdb-pg image. Becomes class D (both paths) once the k8s FerretDB 2.x
  migration lands (see gap below).

## Rendering contract (what dfe-stack render must emit)

- `render --target docker`: the `.env` fragment - every class-A DFE app
  `DFE_<N>_VERSION=tag@digest` (published only) + every class-B/D/E third-party
  `<SVC>_VERSION=tag@digest`, keyed to dfe-docker's exact `.env` var names (the
  right column above). Excludes unpublished apps and PENDING content, loudly.
- `render --target k8s`: the values overrides the appsets/charts consume (dfe-*
  app image tags via resolve --emit-values; EXTEND to class-B/D service versions).
- The loop-closer (`check_versions_drift.py` / `dfe-stack verify --rendered`):
  assert each OUR-chart templated image tag == the SSoT logical version, so a
  chart cannot drift from the manifest. Covers class A (already), class B (CH
  server+keeper already; ADD kafka `kafka.version`), class D (ADD kafbat,
  ferretdb once the gap below is closed).

## Known gaps this audit surfaced (track, do not silently ignore)

1. **nginx missing from the SSoT.** docker pins `DFE_PROXY_VERSION` but
   versions.yaml has no nginx field. Added as a docker-only (class E) pin.
2. **FerretDB k8s vs SSoT drift.** SSoT `services.ferretdb` = 2.7.0 (the docker +
   HyperDX-fork target, needing the DocumentDB PG extension), but the k8s
   `helm/charts/ferretdb` chart appVersion = 1.24.0 on vanilla CNPG PG17. The k8s
   hyperdx chart already flags this ("confirm/upgrade before deploy"). The 1.x ->
   2.x migration (DocumentDB backend) is REAL work, out of the stack-versioning
   scope. Until it lands, ferretdb + documentdb-pg are effectively docker-only
   (class E) on the k8s side, and ferretdb is EXCLUDED from the drift-check
   loop-closer with this note (adding it now would either break CI or force the
   out-of-scope migration).
3. **Kafka/Redpanda operator ceiling.** `kafka-version` <= strimzi 0.51's
   supported set (4.2.0), `redpanda-version` paired with the redpanda operator
   chart. Both bounded in the org preset; bumped by hand with their operator.

## Renovate and the operator ceilings

This repo extends `github>hyperi-io/renovate-config` and adds nothing but the
description at the top of `renovate.json`. It carried a standalone config from
2026-07-16 to 2026-08-04, which is why it missed the org cooldown behaviour,
the CVE bypass and the PR-only policy for that window.

The operator-coupled versions are bounded by `allowedVersions` rules in the
preset, keyed by PACKAGE:

| pin | bound | coupled to |
|---|---|---|
| `services.clickhouse-version` | `<26.4` | the 26.3 LTS line |
| `services.kafka-version` | `<=4.2.0` | strimzi 0.51's supported set |
| `services.redpanda-version` | `<26.2` | redpanda-operator 26.1.6's tested pairing |

CONSTRAINED, not disabled: patches within the line still flow, so gated does
not mean unwatched. Gating by package rather than by file is the point --
nothing parses `versions.yaml`, so gating that file alone left the same image
pinned in a compose file, a CI script or a chart values file wide open. That
produced dfe-infra#32, dfe-engine#117 and dfe-loader#94.

Widen a bound only in lockstep with its operator, and re-run
`dfe-stack compat-check --strict`.
4. **dfe-hyperdx image-name split.** k8s pulls `dfe-hyperdx`, docker pulls
   `hyperi-hyperdx`. Reconcile the name or map it explicitly in the render.
