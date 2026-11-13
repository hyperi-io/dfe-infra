# dfe-infra

Helm charts, Argo CD ApplicationSets and OpenTofu that stand up and test a DFE deployment on Kubernetes.

[![Helm Lint](https://github.com/hyperi-io/dfe-infra/actions/workflows/helm-lint.yml/badge.svg)](https://github.com/hyperi-io/dfe-infra/actions/workflows/helm-lint.yml)
[![Stack Release](https://github.com/hyperi-io/dfe-infra/actions/workflows/release.yml/badge.svg)](https://github.com/hyperi-io/dfe-infra/actions/workflows/release.yml)
[![License](https://img.shields.io/badge/license-BUSL--1.1-blue)](LICENSE)

## What it does

- One pin set for the whole stack in `versions.yaml`: every chart, operator and image, with the image digests beside them. A release tag here is a certified stack version, and each release carries its signed manifest, image list and upgrade graph.
- Two layers on any Kubernetes, on-premises or in your cloud account. `bootstrap/bootstrap.sh` lays the base (cert-manager, External Secrets, Argo CD, adopting healthy ones already there), then Argo CD reconciles the backing services and the DFE apps from the same charts everywhere.
- The backing services deployed and wired: ClickHouse, Kafka on Strimzi or Redpanda, DocumentDB on CloudNativePG for the console, Envoy Gateway with OIDC, KEDA and External Secrets.
- `scripts/dfe-stack` renders the stack manifest, lists the DFE images to mirror for an air-gapped install, derives the upgrade graph from the stacks `versions.yaml` declares and checks a customer's pins. `scripts/dfe-ops` deploys, verifies, tears down and runs the acceptance tests against a cluster, or a local kind one.
- `DFE_REGISTRY` points a deployment at its own image mirror.

dfe-infra deploys the backing services and the apps. dfe-engine, the control plane, never does: it decides what the apps are configured to do and writes that to git. Everything here is generic, and a deployment's own values live in its private deploy repo.

## Quick start

A Kubernetes cluster you run, at a version in the `platform.kubernetes` range `versions.yaml` gives, with `kubectl` pointed at it, plus `helm`, `openssl` and Python 3. The stack version is a release tag of this repo, and `versions.yaml` on `main` names the current one:

```bash
STACK=<stack version>
git clone --branch "$STACK" https://github.com/hyperi-io/dfe-infra && cd dfe-infra
(umask 077 && cp bootstrap/local.env.example bootstrap/local.env)
```

Edit `bootstrap/local.env` before going on: `DFE_DOMAIN`, the domain every hostname sits under, `DFE_REPO_URL`, the git repository Argo CD reads the charts from, and `DFE_STORAGE_CLASS`, a StorageClass the cluster has (bootstrap installs `local-path` only when the cluster has no default StorageClass). Then:

```bash
python3 scripts/dfe-ops preflight --mode single --env-file bootstrap/local.env
python3 scripts/dfe-ops stack-deploy --stack "$STACK" --mode single \
  --target-revision "$STACK" --env-file bootstrap/local.env
```

The preflight is read-only and says whether the cluster is fit for the tier. `stack-deploy` installs or adopts the base, then hands over to Argo CD. The full walk-through, AWS and the one-host Docker install are on the published site, <https://dfe.docs.hyperi.io/quickstart/>.

## Configuration

The cluster secret's `dfe.hyperi.io/profile` label picks the tier: `slim`, `single` or `scale`. Values merge from the chart defaults, then `argocd/values/common.yaml`, the per-cloud file, the profile file, and last the deploy repo's own overlays, so you override single dials and never templates. [docs/deployment/README.md](docs/deployment/README.md) has the cascade and the per-target guides.

## Documentation

- [docs/deployment/](docs/deployment/README.md): the guides per target (on-premises, RKE2, AWS), upgrades and rollback, storage, Kafka, managed backends, the portability matrix.
- [docs/architecture.md](docs/architecture.md): where this repo sits in the suite and the boundary with dfe-engine.
- [docs/deployment/stack-versioning.md](docs/deployment/stack-versioning.md): what a stack release is and how `dfe-stack` reads it.
- [docs/suite-graph.md](docs/suite-graph.md): which repos are suite members and what moves when one releases (`suite.yaml` is the source).
- [docs/testing-cycle.md](docs/testing-cycle.md) and [docs/threat-model.md](docs/threat-model.md): how a stack is proven, and what is exposed to whom.
- [docs/README.md](docs/README.md) lists every page.

The suite's published docs are at https://dfe.docs.hyperi.io.

## Help and contributing

How to work in this repo, the commands that prove a change and what tends to bite are in [CONTRIBUTING.md](CONTRIBUTING.md). A question that is not a bug goes in a new issue.

Open bugs: [this repo](https://github.com/hyperi-io/dfe-infra/issues?q=is%3Aopen+is%3Aissue+type%3ABug+-label%3Abacklog+-label%3Anext-minor) | [whole DFE suite](https://github.com/search?type=issues&q=is%3Aopen+is%3Aissue+type%3ABug+-label%3Abacklog+-label%3Anext-minor+repo%3Ahyperi-io%2Fdfe-infra+repo%3Ahyperi-io%2Fdfe-docker+repo%3Ahyperi-io%2Fdfe-deploy+repo%3Ahyperi-io%2Fdfe-engine+repo%3Ahyperi-io%2Fdfe-loader+repo%3Ahyperi-io%2Fdfe-receiver+repo%3Ahyperi-io%2Fdfe-fetcher+repo%3Ahyperi-io%2Fdfe-archiver+repo%3Ahyperi-io%2Fdfe-transform-vrl+repo%3Ahyperi-io%2Fdfe-transform-vector+repo%3Ahyperi-io%2Fdfe-transform-elastic+repo%3Ahyperi-io%2Fdfe-ui+repo%3Ahyperi-io%2Fdfe-hyperdx+repo%3Ahyperi-io%2Fscalo-rs+repo%3Ahyperi-io%2Fscalo-py+repo%3Ahyperi-io%2Flogreducer+repo%3Ahyperi-io%2Fdfe-schemas+repo%3Ahyperi-io%2Fvector-vrl+repo%3Ahyperi-io%2Fculvert+repo%3Ahyperi-io%2Fdfe-clickhouse-rs+repo%3Ahyperi-io%2Ffactbook-rs)

## License

BUSL-1.1, see [LICENSE](LICENSE).
