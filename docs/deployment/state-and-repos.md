# State, storage and repositories

> **Coming from Splunk or Elastic?** DFE stores rule definitions in the deploy repo's `config/` directory, where git records their changes. Review rule and schema changes as pull requests, and revert the commit to restore the previous definition. A change made live outside git is reverted by reconciliation.

Each kind of DFE state lives in one place and is owned by one repository. Two questions decide where.

Is the state provided or created? HyperI ships some and has to update it cleanly. The deployment and the engine create the rest, and it has to survive those updates. The two can't share an update lifecycle.

Does it belong in git or a database? A system definition is config-as-code and goes in git for review, rollback and audit. Per-user UI state alone goes in a database.

The base is referenced by pinned version, never copied in. Updating is bumping the pin in `pins.yaml`. We don't use submodules (CI-fragile) or subtrees (they drift).

| State | Store | Written by | Survives an update |
|---|---|---|---|
| Charts, ApplicationSets, defaults | dfe-infra at the pinned tag | HyperI | n/a, it is the update |
| Baseline schemas | the `dfe-schemas` wheel inside the dfe-engine image | HyperI | n/a |
| Which apps run, scaling, pod resources | deploy repo `values/` | operators, dfe-engine | yes |
| Backing service settings | deploy repo `infra/` | operators | yes |
| Sources, custom schemas, transforms, rules, hunts, alerts, OIDC | deploy repo `config/` | dfe-engine, analysts | yes |
| Saved searches, dashboards, preferences | dfe-hyperdx's document store, no repo | end users | yes |
| Secrets | the secrets store, read through External Secrets | operators | yes |

Mutability has three levels:

- Read-only core is replaced on update, such as the charts.
- Some supplied definitions allow selected fields to change, such as the node count of a ClickHouse cluster.
- Read-write is created locally, such as a source definition.

## The repositories

One environment deploys from one repository, a copy of the [dfe-deploy](https://github.com/hyperi-io/dfe-deploy) template. It references dfe-infra by pin and never copies it in.

The engine commits into the deploy repo, and Argo CD reads `pins.yaml`, `values/` and `infra/` from it.

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
    .ingest .label-icon{color:#0E8A7D}
    .xform .label-icon{color:#7B4FB0}
    .load .label-icon{color:#0072B2}
    .bus .label-icon{color:#A8517F}
    .store .label-icon{color:#00785A}
    .ui .label-icon{color:#D9480F}
    .ext .label-icon{color:#64748B}
---
flowchart LR
    accTitle: L4 Detail -- The deploy repo
    accDescr: dfe-engine commits to the deploy repo, one per environment. It holds pins.yaml, values/ with one file per app instance, infra/ for the backing services and config/ for sources, schemas, transforms, rules and OIDC. Argo CD reads the pin, the values and the infra overlays from it.
    subgraph writer["Writer"]
        direction LR
        engine("<b>dfe-engine</b>")
    end
    subgraph deploy["Deploy repo, one per environment"]
        direction LR
        pins[("<b>pins.yaml</b>")]
        values[("<b>values/</b>")]
        infradir[("<b>infra/</b>")]
        config[("<b>config/</b>")]
        pins ~~~ values
        infradir ~~~ config
    end
    subgraph reader["Reader"]
        direction LR
        argocd("<b>Argo CD</b>")
    end
    writer -->|commits| deploy
    deploy -->|overlays| reader
    class engine,argocd control
    class pins,values,infradir,config store
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
  style writer fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style deploy fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style reader fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L4 Detail -- The deploy repo.* Up: [L3 Components -- Argo CD](../argo-cd.md).

`config/` doesn't go through Argo CD: dfe-engine reads it, and the hunt runner's git-sync sidecar pulls its hunts and rules. Per-user state goes to dfe-hyperdx's document store, never to git.

The hunt runner has no probes: it is a polling loop that opens no listener, so there is no endpoint to probe and a probe would only kill healthy pods. Its git-sync init container makes the first sync with a bound (`huntRunner.gitSync.initialMaxFailures`), because the sidecar retries forever and a bad credential would otherwise leave a Running runner with zero hunts. The failure shows as a pod that never starts.

The engine writes the deploy repo alone, by commit, and never the base. Path-scoped credentials keep a bug from reaching it.

`values/` is the one home for an app's Helm values. It holds one `{service}-{instance}-values.yaml` per app instance, and the file's presence enables the app. Operators edit it by pull request and the engine edits the same files. A base update never touches `values/`, `infra/` or `config/`, because provided and created state sit in separate repositories.

`dfe-deploy` is a template and no environment runs from it directly. Each one copies it once and owns the copy, in one of three ways:

- With no `DFE_CONFIG_REPO_URL`, bootstrap stands up the bundled Forgejo and seeds the deploy repo there, so nothing outside the cluster is needed.
- A forge template copy (GitHub "Use this template", GitLab "new project from template").
- A clone at the release tag with `origin` removed and your remote added.

Never fork it. A fork of a public repository can't be made private, and the deploy repo holds your environment's configuration. A fork invites merging template history, when base updates arrive by bumping `pins.yaml`. After instantiation the one coupling to HyperI is the pin ([stack versioning](stack-versioning.md)).

## How state reaches a running app

One ApplicationSet, `layer2-apps`, fans out one multi-source Application per file under `values/`. The chart comes from dfe-infra at the pinned revision and the deploy repo's instance file is layered last, so it wins over every default. The deploy repo's URL comes from the cluster secret's `dfe.hyperi.io/config_repo_url` annotation, and an app with no instance file yet runs on the chart defaults. Singleton apps (receiver, loader, engine, UI) are one file each, and a per-config app such as the fetcher is one file per instance. Delete the file and Argo prunes the Application.

The apps declare how they deploy in dfe-infra's `apps.yaml`, which the engine reflects, so the engine learns which dials an app has without reading chart internals. [Composition](composition.md) lists the default app set per profile.

State reaches a running component by exactly one of three routes:

1. GitOps. `values/` goes through Argo to Kubernetes objects. It's low frequency and auditable, and if the engine is down Argo keeps reconciling the last commit.
2. Hot read. `config/` is read directly by dfe-engine and, through git-sync, the hunt runner, with no Argo round trip. The engine applies the ClickHouse objects and the Kafka topics at startup, from the schema wheel in its image.
3. Application database. Per-user state goes to dfe-hyperdx's store and never to git.

Argo reconciles the engine's commits, and it reverts live changes git doesn't record. The deploy repo's layout, invariants and governed operations are in the dfe-deploy repository's `docs/architecture.md`.
