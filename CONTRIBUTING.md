# Contributing

## Writing docs

Write docs Halliday would have hidden an easter egg in. Clear first, fun second, never corporate beige.

- Read [docs-site/content-root/VOICE.md](docs-site/content-root/VOICE.md) before your first page. It's one "say X, not Y" rule a line, and it binds everyone writing DFE docs, people and agents alike.
- A positive pop-culture or geek reference is welcome, as long as it rides on a fact and the page still reads right without it. Never use one to put something down.
- Check a page with `python3 docs-site/scripts/gate.py path/to/page.md` before it merges. [docs-site/README.md](docs-site/README.md) covers how the site is built.

## Context

### What this is

The GitOps source of truth for a DFE deployment, and the thing that stands one
up: charts, ApplicationSets, OpenTofu modules and cluster bootstrap. A release
tag here IS a certified stack version.

**The boundary that gets crossed most often: dfe-infra DEPLOYS the backing
services, and dfe-engine never does.** The engine is the config control plane -- it
decides what components are configured to do and writes that to git. Anything
that creates a broker, a datastore or a cluster object belongs here.

Design and the repo-by-repo map: [docs/architecture.md](docs/architecture.md).

### Where things live

| path | what |
|---|---|
| `suite.yaml` | Membership and the edge graph. Every other repo's "where this sits" is generated from it, so it is the SSoT for what a release moves. |
| `apps.yaml` | What an app IS: multiplicity, scaling, the files it consumes, its source binding. Adding an app is a manifest edit plus a chart, NEVER an engine release. |
| `versions.yaml` | Every chart, operator and image pin, plus `digests:`, lockstep `content:` tags and `stack:` release metadata. |
| `helm/charts/` | The backing-service and platform charts, over `helm/library/dfe-common`, and `dfe-extras`, the DFE-only objects beside each service's published thin chart. |
| `argocd/appsets/` | The layer ApplicationSets. `argocd/values/` holds the cascade: `common.yaml`, then the environment file, then the profile. |
| `bootstrap/` | Idempotent cluster bootstrap, including the templates that WRITE the Argo cluster secret. |
| `scripts/dfe-stack` | The stack CLI: render, pins, upgrade graph, release gate. |
| `scripts/dfe-ops` | The cluster CLI: kubeconfig, deploy, teardown, verify, acceptance, and a local kind cluster (`kind up/status/down`). |

### Commands that prove a change

```bash
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
| Hardcode an app's multiplicity or scaling in a chart | Put it in `apps.yaml` | Multiplicity and scaling are INDEPENDENT axes -- `single` vs `per_config` says how many, `scale_deployed` says whether KEDA drives it. |
| Cite a `suite.yaml` evidence line number | Cite the edge and its kind | The file carries a `verified:` date and the LINE NUMBERS have rotted, while every kind, note and direction still holds. |
| Use the `terraform` binary | Use `tofu` | OpenTofu is the tool, and `.tf` is its format too. Terraform is being retired across HyperI. |

### Where this sits

Generated from `python3 scripts/dfe-stack suite --consumer dfe-infra` and the
same with `--producer dfe-infra`. Read those rather than trusting this summary.

**Inbound, and it is the interesting direction.** Nearly every app repo is a
producer INTO dfe-infra on an `image-pin` edge: dfe-engine, dfe-loader,
dfe-receiver, dfe-fetcher, dfe-archiver, the three transforms, dfe-ui and
dfe-hyperdx all pin their released image here, in `versions.yaml`, beside the
digest of the thin chart published at that tag. Lockstep -- `dfe-stack
bump-app` moves the tag and repins the chart, `resolve_pins.py` re-resolves the
image digest, and `check_versions_drift.py --fix` carries both into the appset
and dfe-extras copies. dfe-schemas and dfe-deploy arrive on `version-pin` edges
instead.

dfe-engine is the one two-way neighbour: it pins its image here like the rest,
AND this repo's `apps.yaml` is vendored into the engine as a byte copy, the last
resort in its resolution order, so a deployment on that fallback does not reflect a
catalogue entry the copy lacks.

**Outbound.** `dfe-infra -> dfe-docker` is a `derived-pins` edge whose check is
explicitly *nothing*: there is no second copy that can drift, and the graph
records that deliberately rather than leaving it unstated. `dfe-infra ->
dfe-deploy` is a version pin.

So a change to `versions.yaml` or a chart is a DEPLOYMENT move, not a code one,
and the repos above find out about it only when they next release into it.
