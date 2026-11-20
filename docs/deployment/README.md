# Deploying DFE with dfe-infra

You hand it a bare cluster and get a running DFE back. Bootstrap lays the Layer 0 baseline, then DFE's Argo CD reconciles the backing services (Layer 1) and the apps with their engine-authored overlays (Layer 2).

Tier and every default live in values files. You override single dials, never templates.

## The three layers

Bootstrap installs Argo CD, and Argo CD builds both layers from this repo's charts and the deploy repo's overlays.

```mermaid
---
config:
  theme: base
  look: neo
  fontFamily: Inter, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif
  flowchart:
    curve: linear
    nodeSpacing: 40
    rankSpacing: 64
    padding: 18
    wrappingWidth: 280
    htmlLabels: true
  elk:
    preset: modelOrder
  themeVariables:
    fontSize: 15px
    lineColor: "#64748B"
    primaryTextColor: "#0F172A"
    nodeBorder: "#334155"
    clusterBorder: "#0072B2"
    edgeLabelBackground: "#FFFFFF"
  themeCSS: >-
    p{margin:0}
    .node[data-look] rect,.node[data-look] path,.node[data-look] polygon,.node[data-look] .outer-path{filter:drop-shadow(0 1px 2px rgba(15,23,42,.10)) drop-shadow(0 6px 14px rgba(15,23,42,.10))}
    .node rect{rx:10px;ry:10px}
    .cluster rect{rx:16px;ry:16px}
    .node .label div{min-width:150px}
    .node.decision .label div{min-width:0}
    .cluster[data-look] rect{filter:none}
    .nodeLabel{line-height:1.35}
    .nodeLabel b{font-weight:600;font-size:15px;letter-spacing:-0.01em}
    .nodeLabel span{font-size:12.5px;font-weight:400;color:#334155}
    .cluster-label span.nodeLabel{display:inline-block;background:#FFFFFF;color:#0B2A3F;border:1.5px solid #0072B2;border-radius:999px;padding:2px 12px;font-size:11.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}
    .flowchart-link{stroke-width:1.75px}
    .labelBkg{background:transparent!important}
    span.edgeLabel{display:inline-block;background:#FFFFFF!important;color:#1E293B;border:1px solid #94A3B8;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:500}
    span.edgeLabel p{background:transparent!important}
    .label-icon{width:20px;height:20px;vertical-align:-4px;margin-right:6px}
    .icon-shape path[stroke-width="0"][fill="none"]{fill:#FFFFFF;stroke:#FFFFFF;stroke-width:12px;stroke-linejoin:round}
    .icon-shape p{background:transparent!important;color:#0F172A}
    .ingest .label-icon{color:#0E8A7D}
    .xform .label-icon{color:#7B4FB0}
    .load .label-icon{color:#0072B2}
    .bus .label-icon{color:#A8517F}
    .store .label-icon{color:#00785A}
    .ui .label-icon{color:#D9480F}
    .ext .label-icon{color:#64748B}
---
flowchart TB
    accTitle: L4 Detail -- Deployment layers
    accDescr: bootstrap.sh installs DFE's Argo CD as Layer 0. Argo CD reads the charts and ApplicationSets from dfe-infra and the overlays from the deploy repo, and applies Layer 1, the backing services, and Layer 2, the dfe apps.
    subgraph src["Layer 0 and sources"]
        direction LR
        boot("<b>bootstrap.sh</b><br/><span>Layer 0, installs Argo CD</span>")
        infrarepo("<b>dfe-infra</b><br/><span>charts, appsets</span>")
        deployrepo("<b>Deploy repo</b><br/><span>overlays</span>")
        boot ~~~ infrarepo ~~~ deployrepo
    end
    subgraph argo["DFE's Argo CD"]
        direction LR
        argocd("<b>DFE's Argo CD</b>")
    end
    subgraph layers["Applied by helm"]
        direction LR
        layer1("<b>Layer 1</b><br/><span>backing services</span>")
        layer2("<b>Layer 2</b><br/><span>dfe apps</span>")
        layer1 ~~~ layer2
    end
    src --> argo
    argo -->|helm| layers
    class boot,infrarepo,deployrepo,argocd control
    class layer1,layer2 part
  classDef ext fill:#F4F6F8,stroke:#94A3B8,stroke-width:1.5px,stroke-dasharray:5 4,color:#0F172A
  classDef ingest fill:#FFFFFF,stroke:#0E8A7D,stroke-width:3px,color:#0F172A
  classDef xform fill:#FFFFFF,stroke:#7B4FB0,stroke-width:3px,color:#0F172A
  classDef load fill:#FFFFFF,stroke:#0072B2,stroke-width:3px,color:#0F172A
  classDef bus fill:#FFFFFF,stroke:#A8517F,stroke-width:3px,color:#0F172A
  classDef store fill:#FFFFFF,stroke:#00785A,stroke-width:3px,color:#0F172A
  classDef ui fill:#FFFFFF,stroke:#D9480F,stroke-width:3px,color:#0F172A
  classDef decision fill:#FFFFFF,stroke:#9A6700,stroke-width:3px,color:#0F172A
  classDef error fill:#FFFFFF,stroke:#C2410C,stroke-width:3px,color:#0F172A
  classDef control fill:#FFFFFF,stroke:#334155,stroke-width:3px,color:#0F172A
  classDef part fill:#FFFFFF,stroke:#94A3B8,stroke-width:1.5px,color:#0F172A
  style src fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style argo fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style layers fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L4 Detail -- Deployment layers.* Up: [L3 Components -- Argo CD](../argo-cd.md).

- Bootstrap (`bootstrap/bootstrap.sh`) probes each dependency: StorageClass, cert-manager, External Secrets and Argo CD. A healthy controller on the cluster is adopted, otherwise we install our copy. The Argo CD we installed (release `argocd` in namespace `argocd`) is upgraded in place on every re-run, so a chart pin or an install flag reaches a live cluster. An adopted Argo is never reconfigured.
- Layer 1 (`argocd/appsets/layer1-addons.yaml`, `layer2-data.yaml` and, on `scale`, `layer-scale.yaml`) is the addons and the backing services: ClickHouse, DocumentDB and Kafka where the tier has one, from base charts plus the deploy repo's `infra/` overlay.
- Layer 2 (`argocd/appsets/layer2-apps.yaml`) is one Application per deploy-repo `values/*-values.yaml`, dfe-hyperdx included. Each is the component's thin chart over OCI, pinned by digest, beside `helm/charts/dfe-extras`, with the `$values` overlay layered last.

The reasoning behind each ApplicationSet is in [ApplicationSets](appsets.md).

## Tiers and the values cascade

The cluster secret's `dfe.hyperi.io/profile` picks the tier. Values merge in this order, and later wins:

| Order | File | What it sets |
|---|---|---|
| 1 | `<chart>/values.yaml` | chart defaults (`helm/charts/`, or the app's thin chart) |
| 2 | `argocd/values/common.yaml` | fleet-wide overrides |
| 3 | `argocd/values/<cloud or site>.yaml` | per-target overrides |
| 4 | `argocd/values/profile-<tier>.yaml` | tier composition: slim, single or scale, plus mesh, which can't be selected |
| 5 | deploy repo `sizing/<tier>.data-charts.values.yaml` | resolver sizing, layer2-data only |
| 6 | deploy repo `infra/common.yaml` | deployment-wide, every appset |
| 7 | deploy repo `infra/<chart>.yaml` | one data-layer or platform chart |
| 8 | deploy repo `values/<app>-...yaml` | engine-authored overlay, Layer 2 apps |

Everything from `infra/` down is yours, and it merges last. So `clickhouse.mode`, `kafka.mode` and the storage models are reachable without editing this repo. The profile file is a tier default, not a lock.

That makes the data layer depend on the deploy repo resolving. On a bundled deploy (no external git) the git host is `layer2-deploy-repo`'s single-source Application. The data-layer and platform Applications report ComparisonError until it's up, then converge.

What each tier is tested to do:

| Tier | Transport | Status |
|---|---|---|
| `single`, `scale` | Kafka between every stage | Supported and tested. They need Kafka: a broker in the cluster, or a managed one ([managed-backends.md](managed-backends.md)). |
| `slim` | gRPC, stage to stage, one replica each | Supported and tested without archiving. A source with archive on routes a copy to the archiver as a second gRPC destination, and that fan-out is not yet tested. |
| `mesh` | gRPC through a balancer per stage pool, no Kafka | In the charts, never run against a live cluster, and not supported in this release: `dfe-ops`, `bootstrap.sh` and the OpenTofu `profile` variable reject it with `use scale`. |

gRPC itself is tested app to app: receiver to loader, and receiver to transform to loader, on the docker stack's `*-grpc` profiles and on `slim`.

Kafka on tier `single` runs the non-operator single-broker KRaft path (`helm/charts/kafka`, `kafka.mode: single`). The Strimzi operator installs on `scale` clusters alone (`layer-scale.yaml`).

Which apps a tier deploys comes from `apps.yaml`, not the profile file. [composition.md](composition.md) has the table and how it's derived.

## Secrets store

| Tier | Reads the secrets store | What to set |
|---|---|---|
| slim, single | nothing | nothing: `DFE_SECRETS_BACKEND` resolves to `none`, no ClusterSecretStore |
| scale | the Strimzi broker's client credential | `DFE_SECRETS_BACKEND=openbao` with `DFE_VAULT_ADDR`, `DFE_VAULT_ROLE_ID`, `DFE_VAULT_SECRET_ID`, or `aws-sm` with `DFE_SECRETS_REGION` |

`dfe-ops stack-deploy` checks each store-referencing template's render condition and rejects `none` where one fires. `DFE_CA_PERSIST=true`, gateway OIDC (on in `aws.yaml`), an external or managed broker, an external ClickHouse and object storage each add a consumer on any tier. So can a deploy-repo overlay, and `stack-deploy` can't see that one before the deploy. On AWS the backend is `aws-sm` and `openbao` is rejected. Elsewhere an unnamed backend is `openbao`, as is any OpenBao value ([managed-backends.md](managed-backends.md)).

By default every DFE image and thin chart pulls anonymously from `DFE_REGISTRY`, so no pod names a pull secret. A private mirror that needs a login takes `DFE_PULL_SECRET_TOKEN`, with `DFE_PULL_SECRET_USER` (default `token`) and `DFE_PULL_SECRET_SERVER` (default `DFE_REGISTRY`'s host). Bootstrap then creates `ghcr-pull-secret` in every DFE namespace, records it on the cluster secret (`dfe.hyperi.io/image_pull_secret`) for the appsets to hand their charts, and registers the token as Argo CD's read credential for each thin chart.

The stack mints its own secrets in-cluster where nothing outside needs the value: the engine's JWT signing key, the admin and break-glass passwords, each app's ClickHouse password, HyperDX's session and token keys and dfe-ui's NextAuth secret. dfe-extras renders an ESO Password generator and an ExternalSecret with `refreshPolicy: CreatedOnce` for each. Argo renders with `helm template`, so a value minted in a template would change on every sync, and Reloader would roll pods onto a value another pod hadn't been handed yet. `refreshInterval: "0"` alone doesn't hold it, because any spec-generation bump (an Argo re-label is one) makes ESO try the generator again.

The ExternalSecret carries `helm.sh/resource-policy: keep` and the Secret carries ESO's ownerReference, so deleting the ExternalSecret by hand garbage-collects the value with it. A pinned value (`auth.jwtSecret`, `auth.adminPassword`, `auth.breakglassPassword`, `huntRunner.clickhouse.password`) renders a plain Secret instead.

## The mesh tier

Not supported in this release. The pieces below render and pass their chart tests, but no mesh deploy has run against a live cluster.

`mesh` is `scale` without a broker. The stages hand records to each other over gRPC. `mesh.enabled` puts a Gateway API listener in front of every stage pool. A Kubernetes Service balances per connection and gRPC holds one open, so without the listener a sender pins itself to one pod however many replicas KEDA adds. Each pool's route has its policy (`mesh.routePolicy`): a request timeout at or above the sender's 30 s deadline, retries on connect failure and resource exhausted alone, and ejection of a pod that keeps answering unavailable.

With `acknowledgements.enabled` (the default) the receiver answers a sender once the loader confirms delivery, and builds no buffer or spool. A loader outage reaches senders as a 503 once the hold expires, and they retry from their copy. With acknowledgements off, the raised receiver buffer is the one slack in the chain. An outage shorter than it is invisible to senders. A held record has had its 2xx, so it dies with the pod.

## Storage model - decided at deploy, not after

> **Coming from Splunk or Elastic?** Retention capacity and hot-disk capacity are separate choices here. `tiered-block` moves older parts to a second volume, and `cached-object` keeps them in object storage behind a local cache. The model is fixed at deploy because a PVC can't grow in place without a StatefulSet recreate.

Both data stores take a `storageModel`, and it fixes the on-disk layout for the life of the deployment. It defaults to `local`, which changes nothing. Models are named `<family>-<bulk>`. The family says whether data moves to the bulk store or is copied to it, and the bulk half says what that store is.

| chart | `local` | the alternatives |
|---|---|---|
| `clickhouse-cluster` | parts on the PVC | `cached-object`: parts on an object-store disk behind a local read-through cache. `tiered-block`: a hot SSD volume and a cold bulk volume, parts demoted as the hot one fills |
| `kafka` | every segment on the PVC | `tiered-object`: closed segments to object storage (KIP-405 tiered storage), with the PVC sizing the hot window |

The alternative is growing a PVC later. That needs `allowVolumeExpansion: true` on the StorageClass and a StatefulSet recreate, because `volumeClaimTemplates` are immutable. `local-path` can't resize at all. Choose the storage model before deploying.

Every model with an object bulk store reads its credentials from the environment. ESO materialises them from `<project>/<env>/clickhouse/s3` and `<project>/<env>/kafka/tiered`, never inline in a values file.

The engine holds the models and the modes as protected vars, so the API rejects a post-deploy edit. See the deploy repo's `infra/README.md`.

[storage.md](storage.md) has every combination with its status and evidence. That's the deploy-time matrix, including the cells that are rejected and the ones not built yet.

## Upgrade notes

The first upgrade onto the generated JWT signing key invalidates every issued token once. The ExternalSecret adopts the `dfe-engine-jwt` Secret older releases minted from the chart template (`creationPolicy: Owner`) and writes a key into it, the engine rolls, and operators and machine callers log in again. `refreshPolicy: CreatedOnce` then holds that key for the life of the deployment. Can't take the one invalidation? Set `auth.jwtSecret` to the key you have, which renders a plain Secret and no generator.

The landing table and the catch-all source are both named `main`. A deployment cut before that rename keeps its rows in a `default` table that nothing moves or drops. The engine creates `main` beside it, the receiver stamps an unmatched record `_source: main` and the loader writes there, so query `default` for anything older than the upgrade and drop it once nothing needs it. The Kafka landing topic moves from `default_land` to `main_land`, and the old topic keeps what it holds until you delete it.

## Version pins

`versions.yaml` is the one place chart, operator and image versions are set. `scripts/check_versions_drift.py` checks it. Images are pinned as `name:tag@sha256`. The tag lives in `apps:` and the immutable digest in `digests:`. `scripts/dfe-stack` renders the combined form and `dfe-stack verify` re-checks the digests against GHCR.

Every DFE Rust app links its C libraries statically. librdkafka and OpenSSL are compiled in, so an image needs nothing C beyond libc. The exception is dfe-fetcher's unixODBC driver manager (`libodbc.so.2`, LGPL-2.1), which is linked dynamically and loads each ODBC driver at run time (https://github.com/hyperi-io/dfe-fetcher/blob/main/docs/reference/db-drivers.md).

A dfe-infra release tag certifies the whole set as one stack version (`stack:` metadata plus lockstep `content:` repo tags). The full release model is in [stack-versioning.md](stack-versioning.md).

`content.dfe-docker` records the dfe-docker release tag that runs the stack. To reproduce an old stack's docker path, check dfe-docker out at that stack's recorded tag.

The engine serves authored files an app ships: the reference transform pipelines and each app's config contract. The engine chart's `content.entries` turns each into one init container that fills `/etc/dfe-engine/content` from the pinned app image. The engine reads that directory through `DFE_ENGINE_LIBRARY_SEED_DIR` and `DFE_ENGINE_APP_CONTRACT_DIR`. The files never travel through a value or a ConfigMap, because either form re-serialises them into etcd on every Argo sync. The source catalogue the console offers isn't one of them. It's dfe-schemas package data, installed with the engine.

Every DFE service boots with a release check you can turn off. It's one POST of `{product, current_version, os, arch, instance_id}` to the HyperI releases endpoint, and it logs whether a newer version exists. The id is a one-way UUIDv5 derived from the platform. Deployment names are never sent. Any failure costs one WARN line and nothing else.

The `versionCheck` values key overrides it. The `dfe-common.versionCheckEnv` helper renders it into every app. Set it in your deploy repo's `infra/common.yaml`, or per app in its overlay. `argocd/values/common.yaml` is the pinned default, not a file to edit:

```yaml
versionCheck:
  enabled: false        # total opt-out -- no check, nothing sent (air-gap)
  sendInstanceId: false # keep the check, strip the install id
  apiUrl: ""            # point the check at a mirror
```

The pages in this directory, and the ones beside it a deployer reads:

| Page | Covers |
|---|---|
| [architecture.md](../architecture.md) | where this repo sits in the suite |
| [wizard.md](wizard.md) | `dfe-ops init`, the wizard that writes a deployment.yaml dial |
| `python3 scripts/dfe-ops --help` | every `dfe-ops` subcommand, and `dfe-ops <subcommand> --help` for its flags |
| [composition.md](composition.md) | which apps a profile deploys, and what an idle app does |
| [storage.md](storage.md) | the storage-deploy matrix, with the evidence behind every cell |
| [edge.md](edge.md) | every door into a deployment, and the dial key that opens it |
| [gateway-oidc.md](gateway-oidc.md) | edge OIDC and the private-CA IdP setup |
| [edge-vpn.md](edge-vpn.md) | the opt-in tunnel a field appliance dials in on |
| [values-shared.md](values-shared.md), [values-clouds-edge.md](values-clouds-edge.md), [values-tiers-apps.md](values-tiers-apps.md) | why the keys in `argocd/values/` are set as they are |
| [toolbox.md](toolbox.md) | the troubleshooting image, pod and AWS bastion |
| [rke2.md](rke2.md) | the default distribution |
| [on-prem.md](on-prem.md) | the on-prem cluster cycle through the `local` target |
| [aws.md](aws.md), [aws-operations.md](aws-operations.md), [aws-teardown.md](aws-teardown.md) | deploying, running and tearing down on AWS |
| [state-and-repos.md](state-and-repos.md) | which repository owns each kind of state |
| [stack-versioning.md](stack-versioning.md) | one pin selects a certified stack |
| [upgrades.md](upgrades.md), [upgrade-rollback.md](upgrade-rollback.md) | upgrade paths and the rollback runbook |
| [node-autoscaling.md](node-autoscaling.md) | what an elastic on-prem node layer needs |
| [deploy-helpers.md](../deploy-helpers.md), [deploy-tls-trust.md](../deploy-tls-trust.md) | release helpers, and which CA signs the gateway certificate |
| [kafka/](kafka/README.md), [clickhouse.md](clickhouse.md), [managed-backends.md](managed-backends.md) | broker and ClickHouse targets |
| [migrating-from-2.1.md](migrating-from-2.1.md) | moving a pre-2.2 private-cloud deployment onto cached-object |
| [deployment-logs/](deployment-logs/template.md) | record every deploy |
