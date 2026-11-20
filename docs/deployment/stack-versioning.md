# Stack versioning

DFE is made of independently released parts: the Rust data plane, dfe-engine, dfe-ui, dfe-hyperdx, the schemas, the charts and operators, and the deploy-repo template. "DFE X.Y.Z" selects the component versions recorded in `versions.yaml` at that release tag, and each stack release tests that combination. A deployment pins one number and can move individual components off the set, visibly and at its risk.

`versions.yaml` in dfe-infra is the one place every pinned version is set: bootstrap components, operators, data services and every `dfe-*` image. A drift check (`scripts/check_versions_drift.py`) fails the build when a chart, ApplicationSet or bootstrap manifest disagrees with it.

A stack release is a dfe-infra tag, so `versions.yaml` frozen at tag `X.Y.Z` is the manifest for DFE X.Y.Z. People set the `current` pointer and its stack entry, and the Stack Release workflow (`.github/workflows/release.yml`) cuts the tag once `current` has none. Nobody hand-tags. `dfe-schemas` and `dfe-deploy` hold content rather than images. They're tagged in step with each stack release and recorded in `versions.yaml` like any other component.

One pin in the deploy repo selects a `versions.yaml`, and that file names every image, chart and content tag in the stack.

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
flowchart LR
    accTitle: L4 Detail -- One pin selects the stack
    accDescr: The deploy repo's pins.yaml names one dfe-infra tag. versions.yaml at that tag pins the DFE images by tag and digest, the data services, operators and charts, and the dfe-deploy template tag.

    pins[("<b>pins.yaml</b><br/><span>base.dfe-infra</span>")]
    manifest[("<b>versions.yaml</b><br/><span>at that tag</span>")]
    images("<b>DFE images</b><br/><span>tag@sha256</span>")
    services("<b>Data services, operators, charts</b>")
    template("<b>dfe-deploy</b><br/><span>template tag</span>")

    pins -->|selects| manifest
    manifest --> images
    manifest --> services
    manifest --> template

    class pins,manifest store
    class images,services part
    class template control
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
```

*Figure 1. L4 Detail -- One pin selects the stack.* Up: [Deploying DFE with dfe-infra](README.md).

The deploy repo's `pins.yaml` has `base.dfe-infra`, the one version that selects the stack, and a `channel` (alpha, beta, rc or release). Bumping the pin moves charts, operators and every app image in one reviewed commit and leaves `values/` and `config/` alone ([state and repositories](state-and-repos.md)).

There's no `dfe-schemas` pin. The wheel rides inside the engine image, so the engine pin selects the schema release.

## Overrides and upgrades

Need one component ahead of the set, or held behind it? List it under `overrides` in `pins.yaml`.

```yaml
overrides:
  apps:
    dfe-receiver: "v<version>@sha256:..."
```

An override beats the certified manifest, and everything not listed stays on the certified version. It must be a `tag@sha256` digest, and `dfe-stack resolve` rejects a floating tag.

Every override prints a support-drift notice, from `dfe-stack resolve` and again from dfe-engine at startup. The drift sits outside the certified-set guarantee until a later stack release absorbs it, because HyperI tests the manifest combination and not arbitrary cross products.

To apply an override, `--emit-values` writes per-service fragments (`image.tag` and `image.digest`, which the thin chart joins into `tag@digest`). Merge them into the service's existing `values/{service}-{instance}-values.yaml`. A separate file would become an Argo application of its own.

Each release's `versions.yaml` has `stack.previous`, the verified from-versions for that release. `dfe-stack graph` derives the whole upgrade graph from those entries, so no graph file needs curating. `dfe-stack check-upgrade --from A --to B` gives the verdict. A declared `previous` list is authoritative for its release. Otherwise the rule is sequential minors with free patch hops.

`upgrade-order.yaml` sets the order an upgrade applies in: bootstrap, operators, data services, then apps. `stack.upgrade-order` in `versions.yaml` is a record the rendered manifest prints, and no code orders a rollout by it. [Upgrades](upgrades.md) walks an upgrade and [rollback](upgrade-rollback.md) undoes one.

Patches are cumulative. The components of one stack are tested together and move together. A patch can move an API between dfe-engine, dfe-ui and dfe-hyperdx, so upgrade all three at once.

## Cutting a release

Component repositories release on separate lines, each with a semantic-release tag and a GHCR image. A stack release is a `dfe-stack cut` against `versions.yaml` ([cutting a stack](../cutting-a-stack.md)). The drift check and the integrated test suites, which drive data from the receiver to ClickHouse, check it, and then it's tagged.

On the tag, the same workflow renders the manifest and a flat image list for air-gap mirroring. It publishes both as a cosign-signed OCI artifact beside the images, so a mirror verifies offline. The deploy tooling's version is in the manifest, because an unpinned installer makes a build impossible to reproduce. A component can be dropped from the set when keeping it costs more than it's worth, and the release notes say so.

`scripts/dfe-stack` does all of it:

| Subcommand | Does |
|---|---|
| `render` | The rendered manifest: image pins as `tag@sha256`, content repos, everything |
| `images` | The flat DFE-owned image list for air-gap mirroring |
| `graph` | The upgrade graph from the in-file `stack.previous` entries |
| `check-upgrade --from A --to B` | The verdict on an upgrade path |
| `resolve --pins pins.yaml` | The effective component set for a deployment, with support-drift per override |
| `verify` | Compares `versions.yaml` digests with the registry |
| `cut`, `bump-app`, `release-gate` | Cut a stack, move one app, and gate a release |
