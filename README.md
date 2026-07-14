# dfe-infra

The deploy layer for the Data Fusion Engine (DFE) - the GitOps SSoT and deploy
vehicle that stands a full DFE stack up on any Kubernetes (Rancher/on-prem, AWS,
GCP, Azure).

## A product suite, not our internal infra

DFE is a product suite that any organisation deploys to run its own data-fusion
engine. This repo is the deploy layer of that suite, and like every core DFE repo
(engine, schemas, ui, hyperdx, the Rust data-plane apps) it is public by design.
Our own hosted instance is just ONE deployment, not "the product".

So this repo stays GENERIC:

- **No environment-specifics live here** - no cluster IDs, hostnames,
  credentials, or private-fleet assumptions. Those live in each deployment's own
  PRIVATE config repo (a per-env overlay), never in this repo.
- **Every deployment choice is a parameter.** A dev preview, a customer's
  bring-your-own cluster, and an AWS-marketplace install differ only by the inputs
  they pass - not by the code that runs.
- **Test WITH a real cluster, never code FOR one.** We validate against our devex
  cluster, but nothing here may assume that cluster is ours.

## What it does

A two-layer model, identical on any Kubernetes:

- **Layer 1** - base infra (cloud-specific): `bootstrap/bootstrap.sh` lays the
  cluster baseline (detect-or-install), with OpenTofu handling the
  cloud-side prep (secrets/IAM) where a cloud needs it.
- **Layer 2** - the DFE platform (identical everywhere), managed by ArgoCD.

A cluster-secret annotation bridge carries Layer-1 outputs into the GitOps layer.

dfe-infra is the **master deploy layer** of the suite: it deploys a VERSION-SET
given as explicit input (which ref or digest of each DFE repo to use), never "its
own checkout". That single rule lets ONE orchestrator drive dev previews,
production installs, and the marketplace wizard from the same code - and it is
why dfe-infra deploying dfe-infra is not a paradox. The deploy-context schema that
carries those inputs is specified in
[docs/plans/2026-07-08-branch-preview-cycle.md](docs/plans/2026-07-08-branch-preview-cycle.md).

## Tech

- **IaC:** OpenTofu (canonical; Terraform is being retired - see
  hyperi-io/hyperi-developer#11). HCL under `terraform/` runs on either.
- **GitOps:** ArgoCD ApplicationSets (matrix generator).
- **Registry:** Harbor (self-hosted DFE artifact registry); ghcr for public
  releases at the OSS cutover. JFrog is legacy - do not use it.
- **Ingress/auth:** Envoy Gateway + OIDC SecurityPolicy.
- **Data:** ClickHouse, CNPG PostgreSQL, FerretDB; Strimzi Kafka (KRaft,
  SASL/SCRAM); OTel -> ClickHouse -> HyperDX.
- **Secrets:** ESO + OpenBao (on-prem) / cloud secret managers.
- **Autoscaling:** KEDA from OTel metrics.

## Layout

See [docs/architecture.md](docs/architecture.md) for where this repo sits in
the suite (and the hard dfe-engine boundary), and
[docs/deployment/index.md](docs/deployment/index.md) for layers, tiers, and
the values cascade.

- `terraform/` - reusable HCL modules (OpenTofu).
- `helm/charts/` - one chart per DFE service and data component
  (+ `helm/library/dfe-common` shared templates).
- `argocd/` - ApplicationSets, AppProjects, tier + cloud values.
- `bootstrap/` - idempotent cluster bootstrap.
- `versions.yaml` - single source for chart/operator/image pins, plus the
  image `digests:`, lockstep `content:` repo tags, and `stack:` release
  metadata (upgrade order, previous-version pointer).

## Stack releases

A dfe-infra release tag IS a certified DFE stack version: `versions.yaml` at
that tag names every component, and `scripts/dfe-stack` operates on it
(render the manifest, list images for air-gap mirroring, derive the upgrade
graph from git tags, check an upgrade path, resolve a customer `pins.yaml` -
optionally emitting a helm values fragment - and verify digests against
GHCR). On each release tag the `stack-manifest` workflow renders the
manifest + image list + upgrade graph, attaches them to the GitHub release,
pushes them as an OCI artifact, and cosign-signs it keyless. Full model:
dfe-docs `deployment/stack-versioning.md`.
