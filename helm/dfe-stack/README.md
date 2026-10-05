# dfe-stack -- the one-command trial chart

Installs the whole DFE stack on any CNCF-conformant Kubernetes with one
`helm install`, no ArgoCD. It wraps the SAME per-component charts in
`helm/charts/` that the ArgoCD ApplicationSets compose -- never a parallel
re-implementation -- with defaults that produce the SLIM trial shape:
single-node ClickHouse, no Kafka (receiver -> loader direct gRPC), ClusterIP +
port-forward, laptop-sized requests.

**This chart is the TRIAL path.** The PRODUCT deployment path is ArgoCD + the
ApplicationSets under `argocd/` (multi-namespace layout, GitOps deploy repo,
engine-driven overlays, sync waves). Trial and product use the same charts and
images, so graduating is a values change, not a migration.

## Prerequisites

One operator must exist before install. In a product deployment
`bootstrap/bootstrap.sh` installs cert-manager + external-secrets pre-Argo; the
trial installs external-secrets directly. Pins come from `versions.yaml` (the
pin SSoT) -- quoted here from the current stack, `versions.yaml` wins on any
mismatch:

| component | why | versions.yaml key |
|---|---|---|
| external-secrets 2.10.0 | generates the in-cluster secrets (dfe-engine JWT, ClickHouse admin, FerretDB, NextAuth) | `bootstrap.external-secrets` |

```sh
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets \
  -n external-secrets --create-namespace --version 2.10.0 --set installCRDs=true
```

The chart's NOTES print a CRD preflight on every install/template, so a
missing operator is called out rather than discovered as a failed apply.

NOT required for the trial: ArgoCD, Forgejo, KEDA, metrics-server, reloader,
external-dns, strimzi-kafka-operator, clickhouse-operator, envoy-gateway,
cert-manager. The gateway and scale tiers bring their own (below).

## Install

```sh
helm dependency build helm/dfe-stack
helm install dfe helm/dfe-stack -n dfe --create-namespace
```

The `-n dfe` matters: everything installs into ONE namespace, and the
network-policies defaults name it. Another namespace works with
`network-policies.dfeNamespaces`, `otel-collector.appNamespace` and
`links.appNamespace` overridden to match.

Access is by port-forward (no LoadBalancer required); the rendered NOTES give
the exact commands, the break-glass login, and where its credential lives.

## k3d quickstart

```sh
k3d cluster create dfe-trial --agents 1
# prerequisites above, then:
helm dependency build helm/dfe-stack
helm install dfe helm/dfe-stack -n dfe --create-namespace
```

kind works the same (`kind create cluster`). Both ship a default StorageClass,
which the PVCs (ClickHouse, FerretDB's DocumentDB backend, engine store) rely on. Note
k3d/kind's default CNIs do not enforce NetworkPolicy; the policies install
inert there and enforce on a real CNI.

## Profiles

Default values = the `slim` profile. The other tiers ship as overlay files
mirroring `argocd/values/profile-*.yaml`:

```sh
# single: one node of everything WITH Kafka (non-operator broker; no new prerequisites)
helm install dfe helm/dfe-stack -n dfe --create-namespace -f helm/dfe-stack/profiles/single.yaml

# scale: HA + operator ClickHouse/Kafka + KEDA -- needs strimzi-kafka-operator,
# clickhouse-operator and keda first (pins: versions.yaml `operators:`).
# At this tier use the ArgoCD product path; the overlay exists to exercise the
# same composition.
```

## Enabling the gateway (edge TLS instead of port-forward)

`envoy-gateway-config.enabled=true` adds the Gateway, HTTPRoutes and the
self-signed internal CA. It needs envoy-gateway v1.9.1 + Gateway API CRDs
(`argocd/bootstrap/envoy-gateway-app.yaml` is the product install),
cert-manager v1.21.2 (`bootstrap.cert-manager`), a LoadBalancer
implementation, and a real `global.domain` + `domain` value.

## How this maps to the product composition

| appset | charts | here |
|---|---|---|
| layer2-platform (wave 2) | network-policies, envoy-gateway-config | deps (gateway off by default) |
| layer2-data (waves 4-6) | clickhouse-cluster, ferretdb, kafka, kafbat, otel-collector, links | deps (kafbat off on slim) |
| layer2-deploy-repo (wave 4) | forgejo | dep, off (no GitOps loop) |
| layer2-apps (waves 5 and 7) | slim app set: dfe-engine (5), then dfe-ui, dfe-receiver, dfe-loader, hyperdx | deps (hyperdx under values key `dfe-hyperdx`) |
| layer1-addons / layer-scale | upstream operator charts | prerequisites, not deps |

Helm has no sync waves; dfe-engine's schema bootstrap and the apps retry their
backing services, which is what the waves ordered. Shared facts the appsets pass as
one values file are anchored once in `values.yaml` (`x-dfe-shared`), sourced
from `argocd/values/common.yaml`. Image pins ride each component chart's
`appVersion`, drift-checked against `versions.yaml`.

Excluded on purpose (not in a slim deployment either): dfe-archiver,
dfe-fetcher, the dfe-transform-* family and culvert (per-source or opt-in,
deploy-repo driven), and every ArgoCD-only concern.

## Validation

```sh
helm dependency build helm/dfe-stack
helm template dfe helm/dfe-stack -n dfe                                   # slim defaults
helm template dfe helm/dfe-stack -n dfe -f helm/dfe-stack/profiles/single.yaml
scripts/validate-charts.sh                                                # the component charts
```
