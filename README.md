# dfe-infra

[![Helm Lint](https://github.com/hyperi-io/dfe-infra/actions/workflows/helm-lint.yml/badge.svg)](https://github.com/hyperi-io/dfe-infra/actions/workflows/helm-lint.yml)
[![Stack Release](https://github.com/hyperi-io/dfe-infra/actions/workflows/release.yml/badge.svg)](https://github.com/hyperi-io/dfe-infra/actions/workflows/release.yml)

> One certified pin set stands up the whole stack. `versions.yaml` is the
> manifest, every image is `tag@sha256`, and the upgrade graph is derived from
> the file rather than remembered.

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
- **Test WITH a real cluster, never code FOR one.** We validate against our own
  on-prem reference cluster, but nothing here may assume that cluster is ours.

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
[docs/deployment/index.md](docs/deployment/index.md).

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
the suite (and the hard dfe-engine boundary),
[docs/deployment/index.md](docs/deployment/index.md) for layers, tiers, and
the values cascade, and [docs/suite-graph.md](docs/suite-graph.md) for which
repos are suite members and what moves when one of them releases
(`suite.yaml` is the source).

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

## Context

### What this is

The GitOps source of truth for a DFE deployment, and the thing that stands one
up: charts, ApplicationSets, OpenTofu modules and cluster bootstrap. A release
tag here IS a certified stack version.

**The boundary that gets crossed most often: dfe-infra DEPLOYS the backing
services; dfe-engine never does.** The engine is the config control plane -- it
decides what components are configured to do and writes that to git. Anything
that creates a broker, a datastore or a cluster object belongs here.

Design and the repo-by-repo map: [docs/architecture.md](docs/architecture.md).

### Where things live

| path | what |
|---|---|
| `suite.yaml` | Membership and the edge graph. Every other repo's "where this sits" is generated from it, so it is the SSoT for what a release moves. |
| `apps.yaml` | What an app IS: multiplicity, scaling, the files it consumes, its source binding. Adding an app is a manifest edit plus a chart, NEVER an engine release. |
| `versions.yaml` | Every chart, operator and image pin, plus `digests:`, lockstep `content:` tags and `stack:` release metadata. |
| `helm/charts/` | One chart per service, over `helm/library/dfe-common`. |
| `argocd/appsets/` | The layer ApplicationSets. `argocd/values/` holds the cascade: `common.yaml`, then the environment file, then the profile. |
| `bootstrap/` | Idempotent cluster bootstrap, including the templates that WRITE the Argo cluster secret. |
| `scripts/dfe-stack` | The stack CLI: render, pins, upgrade graph, release gate. |
| `scripts/dfe-ops` | The cluster CLI: kubeconfig, deploy, teardown, verify, acceptance. |

### Commands that prove a change

```
python3 scripts/dfe-stack suite --cycles        # the build graph and its gates
python3 scripts/dfe-stack render                # the stack manifest
bash scripts/validate-charts.sh                 # chart lint and render
python3 scripts/check_versions_drift.py         # pins vs charts vs digests
python3 scripts/dfe-ops preflight               # READ-ONLY: is this cluster fit
```

**Argo reporting `Synced` is not health.** A Synced app can be `Degraded` with
every pod unable to pull an image: the manifests applied exactly as written, and
what was written was wrong. Read the pod state, never the sync status.

### What tends to bite

| Don't | Do | Why |
|---|---|---|
| Trust that a chart change reached the cluster | Check the Argo app's rendered parameters | A Helm parameter with a `name` and no `value` still OVERRIDES the values files. An appset that templates a parameter from a missing annotation emits exactly that, silently. |
| Assume the cluster secret follows the repo | Re-run bootstrap after changing what reads it | `bootstrap/templates/cluster-secret.yaml.tpl` writes the Argo cluster secret and **Argo does not reconcile it**. Appsets move forward, the secret does not, and nothing reports the gap. This is why a correct registry fix sat dead for three days (#368). |
| Read a pull failure as a credentials problem | Check the image reference has a registry HOST | `dfe-common.image` renders a registry-less ref when `global.registry` is empty rather than failing. containerd then resolves it against Docker Hub and reports `insufficient_scope`, which reads as a bad pull secret and is not. |
| Hardcode an app's shape in a chart | Put it in `apps.yaml` | Multiplicity and scaling are INDEPENDENT axes -- `single` vs `per_config` says how many, `scale_deployed` says whether KEDA drives it. |
| Cite a `suite.yaml` evidence line number | Cite the edge and its kind | The file carries a `verified:` date and the LINE NUMBERS have rotted, while every kind, note and direction still holds. |
| Use the `terraform` binary | Use `tofu` | OpenTofu is the tool; `.tf` is its format too. Terraform is being retired across HyperI. |

### Where this sits

Generated from `python3 scripts/dfe-stack suite --consumer dfe-infra` and the
same with `--producer dfe-infra`. Read those rather than trusting this summary.

**Inbound, and it is the interesting direction.** Nearly every app repo is a
producer INTO dfe-infra on an `image-pin` edge: dfe-engine, dfe-loader,
dfe-receiver, dfe-fetcher, dfe-archiver, the three transforms, dfe-ui and
dfe-hyperdx all pin their released image here, in `versions.yaml` and the
matching chart. Lockstep -- bump the tag, re-resolve the digest, and let
`check_versions_drift.py` confirm the chart's appVersion and the digest mirror
agree. dfe-schemas and dfe-deploy arrive on `version-pin` edges instead.

dfe-engine is the one two-way neighbour: it pins its image here like the rest,
AND this repo's `apps.yaml` is vendored into the engine as a byte copy, so a
catalogue entry the engine does not carry is not reflected by its management API.

**Outbound.** `dfe-infra -> dfe-docker` is a `derived-pins` edge whose check is
explicitly *nothing*: there is no second copy that can drift, and the graph
records that deliberately rather than leaving it unstated. `dfe-infra ->
dfe-deploy` is a version pin.

So a change to `versions.yaml` or a chart is a DEPLOYMENT move, not a code one,
and the repos above find out about it only when they next release into it.
