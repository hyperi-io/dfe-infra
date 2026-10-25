<!--
  Project:      dfe-infra
  File:         docs/stack-components.md
  Purpose:      Component class map for the DFE stack version SSoT - which
                render class each moving part belongs to, which chart/CR or
                .env var renders it, and per-path cascade rules. The reference
                that scripts/dfe-stack render + check_versions_drift.py follow.
  Language:     Markdown
  License:      BUSL-1.1
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
| A | dfe-* owned app (GHCR) | published thin chart pinned by `chart-digests.<n>`, image `tag@digest` | `.env` `DFE_<N>_VERSION=tag@digest` |
| B | third-party via an operator + a CR WE author | OUR chart templates the CR image/version from the SSoT logical version; the operator chart is a SEPARATE k8s-only pin | plain upstream image in `.env` |
| C | upstream chart we do NOT template, k8s-ONLY | appset/chart pins the chart version | none (not in compose) |
| D | third-party plain image in BOTH paths | OUR chart sets the image via values from the SSoT | same upstream image in `.env` |
| E | third-party plain image, docker-ONLY (k8s uses a different mechanism, or the k8s equivalent is not yet wired) | none, or a different component does the job | plain upstream image in `.env` |

Class E is the addition this audit forced: not every docker image has a k8s
cascade (the compose stack needs a reverse proxy and a Mongo-wire backend that
k8s solves with different components). Rendering must emit these for docker only.

## Class A - dfe-* owned apps

Same GHCR image, same tag both paths. SSoT: `apps.<n>` (tag) + `digests.<n>`
(sha256). k8s: each app deploys from its published thin chart, at the version
`apps.<n>` names less its `v`, pinned by `chart-digests.<n>`. The layer 2
appsets carry a drift-checked copy of each chart digest, and the thin chart
carries the image `tag@sha256`. docker: `.env` `DFE_<N>_VERSION=<tag>@<digest>`.

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
| dfe-transform-elastic | apps.dfe-transform-elastic | (n/a) | NO - alpha |
| dfe-hyperdx (fork) | content.dfe-hyperdx | DFE_HYPERDX_VERSION | NO - first image pending |

Notes:

- dfe-transform-splack and dfe-transform-wasm are COMING, not members. Alpha and
  unpublished, so their `apps:` pins are commented out and they have no chart.
  Every line to uncomment when they ship carries the word `coming` beside one of
  the two names, so grep that.
- dfe-transform-elastic is PRE-GA/alpha and NOT published to GHCR (no digest).
  `dfe-stack` already excludes any app without a digest from rendered manifests -
  loudly. It stays excluded from a stack cut until it publishes. Do not render it.
- dfe-hyperdx is the HyperDX fork. Its k8s thin chart pulls
  `<registry>/dfe-hyperdx` at `content.dfe-hyperdx`. docker pulls
  `ghcr.io/hyperi-io/hyperi-hyperdx` (NOTE: a DIFFERENT image name than k8s -
  `hyperi-hyperdx` vs `dfe-hyperdx`; reconcile the name, or the render must map
  the two). Its SSoT lives in `content.dfe-hyperdx` (PENDING-FIRST-IMAGE), not
  `apps:`. Render it as excluded until the fork image publishes (landmine 7).

## Class B - third-party via an operator + a CR we author

The operator chart appVersion is NOT ours to move. The SERVICE version we own is
set in the CR our chart templates. SSoT holds the LOGICAL service version; the
operator chart is a SEPARATE, k8s-only pin on its own release line.

### ClickHouse

- SSoT logical version: `services.clickhouse-version` (26.3.42.3, LTS line).
- k8s: `helm/charts/clickhouse-cluster` values `clickhouse.version` ->
  ClickHouseCluster CR `spec.image`; keeper image tag mirrors it. Operator:
  `operators.clickhouse-operator` (0.0.8, k8s-only, scale mode only).
- docker: `CLICKHOUSE_VERSION` -> `clickhouse/clickhouse-server:<ver>@digest`.
- Cascade: SAME upstream image both sides (`clickhouse/clickhouse-server`). One
  number, both refs. Clean.

### Kafka

- SSoT logical version: `services.kafka-version` (4.3.1 -- the strimzi 1.2 ceiling).
- k8s: `helm/charts/kafka` values `kafka.version` -> Strimzi Kafka CR
  `spec.kafka.version`. Strimzi then pulls its OWN internal
  `quay.io/strimzi/kafka:*-kafka-4.3.x` image for the brokers.
  Operator: `operators.strimzi-kafka-operator` (1.2.0, k8s-only).
- Kafka CLI: `kafka.cliImage` in `helm/charts/kafka` values names a `quay.io/strimzi/kafka:<strimzi>-kafka-<kafka-version>@digest` image for the MSK bootstrap Job and the smoke-test and diagnostics client pods. Its `-kafka-` suffix is held to `kafka.version` by `scripts/tests/test_smoke_kafka_client_image.py`. It has no `versions.yaml` key.
- docker: `APACHE_KAFKA_VERSION` -> `apache/kafka:<ver>@digest`.
- Cascade: TWO BROKER IMAGES for one logical number (strimzi-internal vs apache/kafka).
  Same logical 4.x, two renderers each knowing their own image. Collapse the
  two into one image name and one of the two paths stops resolving, which is
  why the model is "logical version, two renderings".
- OPERATOR CEILING (hard constraint): Strimzi 1.2 supports Kafka 4.2.0 - 4.3.1
  ONLY. The logical `kafka-version` must never exceed what the pinned strimzi
  operator supports, or the k8s path breaks. Renovate is bounded to that ceiling
  by the org preset (see "Renovate and the operator ceilings") - bump it by hand,
  verified against strimzi's kafka-versions.yaml, in lockstep with the operator.

### Redpanda (opt-in, BSL)

- SSoT logical version: `services.redpanda-version` (v26.2.3).
- k8s: `helm/charts/kafka` values `kafka.redpanda.image.tag` -> Redpanda CR
  `spec.clusterSpec.image.tag`. Operator: `operators.redpanda-operator` (26.2.4,
  k8s-only, opt-in). The broker tag is PAIRED with the operator chart - move them
  together (also bounded in the org preset, for the same reason as kafka).
- docker: `REDPANDA_VERSION` -> `redpandadata/redpanda:<ver>@digest`.
- Cascade: SAME image both sides. One number, both refs.

## Class C - upstream chart, k8s-only, no docker, no cascade

Not in compose. SSoT pins the chart version; appset/bootstrap uses it;
drift-check enforces where a hardcoded pin exists. Already works - leave as-is.

- bootstrap: `cert-manager`, `external-secrets`, `argocd`, `valkey` (plain
  manifest image), `local-path-provisioner`, `metallb` (on-prem only).
- operators: `envoy-gateway`, `external-dns`, `keda`, `metrics-server`,
  `reloader`, the three OPERATOR chart pins
  (`strimzi-kafka-operator`, `clickhouse-operator`, `redpanda-operator` - the
  operator's own release line, distinct from the class-B service version), and
  `documentdb-operator` (see the DocumentDB store below).
- data: `otel-collector`, `forgejo` (bundled deploy-repo fallback, k8s-only).
  `postgresql` is no chart or image: it records the PostgreSQL major the AWS
  toolbox installs psql at.
- the DocumentDB store, HyperDX's metadata on k8s (dfe-docker runs DocumentDB
  Local, `services.documentdb-local`, class E below):
  - the operator is the OCI chart `oci://ghcr.io/documentdb/documentdb-operator`
    1.0.0, by digest, in `argocd/appsets/layer1-addons.yaml`. Its chart bundles
    CloudNativePG and installs it into `cnpg-system`, and the operator, sidecar
    injector and CloudNativePG images carry their digests in the tag overrides.
  - `helm/charts/documentdb` renders the DocumentDB CR, whose values name the
    extension, gateway and PostgreSQL 18.6 images tag@sha256.
  - `check_versions_drift.py` reads every one of these pins. Their versions.yaml
    keys start with the stack cut after 2.2.1-rc.17, and until then each check is
    noted as pending rather than compared (`PENDING_MIRRORS`).

## Class D - third-party plain image, BOTH paths

OUR chart sets the image via values from the SSoT; docker sets the same upstream
image. One number, same image ref both sides.

### Kafbat (Kafka UI)

- SSoT: `services.kafbat` (v1.5.0@sha256:...). k8s: `helm/charts/kafbat` `image.tag`.
  docker: `KAFBAT_VERSION` -> `ghcr.io/kafbat/kafka-ui`. ALIGNED today.

## Class E - third-party plain image, docker-ONLY

### DocumentDB Local (HyperDX's metadata, docker)

- SSoT: `services.documentdb-local` (pg18-1.0.0). docker: `HYPERDX_DOCUMENTDB_VERSION` -> `ghcr.io/documentdb/documentdb/documentdb-local`, the single-container DocumentDB build.
- No k8s cascade: k8s runs the DocumentDB store (class C above).

### Envoy (dfe-proxy)

- SSoT: `services.envoy-proxy` (v1.40.1@sha256:...). docker: `DFE_PROXY_VERSION`
  -> `envoyproxy/envoy`. The compose stack's entrypoint, fronting dfe-ui and
  dfe-engine on one origin.
- Docker-ONLY despite the name. k8s runs Envoy too, but installs it via
  envoy-gateway (`operators.envoy-gateway`), which carries its OWN proxy image -
  so there is no cascade from this pin, and the two versions move independently.
- Replaced nginx here on 2026-08-18 (dfe-docker#8): OSS nginx has no free inbound
  OIDC, Envoy's oauth2/jwt_authn filters are Apache-2.0 and are the seam an
  external OIDC issuer chains into.
- Pin the UBUNTU-based `vX.Y.Z` tag, not `distroless-*`: dfe-docker's compose
  healthcheck drives the admin `/ready` over bash's `/dev/tcp`, and distroless
  ships no shell. Envoy publishes no alpine variant and no LTS line - it supports
  the newest four minors, so track the newest.

## Rendering contract (what dfe-stack render must emit)

- `render --target docker`: the `.env` fragment - every class-A DFE app
  `DFE_<N>_VERSION=tag@digest` (published only) + every class-B/D/E third-party
  `<SVC>_VERSION=tag@digest`, keyed to dfe-docker's exact `.env` var names (the
  right column above). Excludes unpublished apps and PENDING content, loudly.
- `render --target k8s`: the values overrides the appsets/charts consume (dfe-*
  app image tags via resolve --emit-values; EXTEND to class-B/D service versions).
- The loop-closer (`check_versions_drift.py` / `dfe-stack verify --rendered`):
  assert each OUR-chart templated image tag == the SSoT logical version, so a
  chart cannot drift from the manifest. Covers class A, class B (CH
  server+keeper, kafka `kafka.version`), class D (kafbat) and the DocumentDB store.

## Known gaps this audit surfaced (track, do not silently ignore)

1. **CLOSED - the docker proxy is in the SSoT.** It was added as a docker-only
   (class E) pin, and 2026-08-18 it became `services.envoy-proxy` when dfe-docker
   swapped nginx for Envoy. Lesson kept: a compose image whose var is a hard-fail
   `${...:?}` MUST have an SSoT entry, and renaming the IMAGE without renaming
   that entry renders a valid tag@digest for the wrong repository - which is
   exactly how `envoyproxy/envoy:1.31-alpine@<nginx digest>` reached a VM.
2. OPEN - the DocumentDB pins have no versions.yaml keys yet. The stack cut
   after 2.2.1-rc.17 adds them, and the drift checks for them stop being pending.
3. **Kafka/Redpanda operator ceiling.** `kafka-version` <= strimzi 1.2's
   supported set (4.3.1), `redpanda-version` paired with the redpanda operator
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
| `services.kafka-version` | `<=4.3.1` | strimzi 1.2's supported set |
| `services.redpanda-version` | `<26.3` | redpanda-operator 26.2.4's tested pairing |

CONSTRAINED, not disabled: patches within the line still flow, so gated does
not mean unwatched. Gating by package rather than by file is the point --
nothing parses `versions.yaml`, so gating that file alone left the same image
pinned in a compose file, a CI script or a chart values file wide open. That
produced dfe-infra#32, dfe-engine#117 and dfe-loader#94.

Widen a bound only in lockstep with its operator, and re-run
`dfe-stack compat-check --strict`.
4. **dfe-hyperdx image-name split.** k8s pulls `dfe-hyperdx`, docker pulls
   `hyperi-hyperdx`. Reconcile the name or map it explicitly in the render.
