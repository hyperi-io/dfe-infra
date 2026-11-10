# Default composition: which apps a profile deploys

`apps.yaml` is the source of truth. Each app carries `profiles` (where it may run) and `default_in` (where it runs when nobody says otherwise). No `default_in` means the same set as `profiles`. An empty list means the app is deployed on demand and never seeded.

| App | slim | single | scale | mesh | docker-slim | docker-single |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| `dfe-receiver` | X | X | X | X | X | X |
| `dfe-loader` | X | X | X | X | X | X |
| `dfe-archiver` | X | X | X | X | | X |
| `dfe-engine` | X | X | X | X | X | X |
| `dfe-ui` | X | X | X | X | X | X |
| `hyperdx` | X | X | X | X | X | X |
| `dfe-fetcher` | | | | | n/a | X |
| `dfe-transform-vrl` | | X | X | | | X |
| `dfe-transform-elastic` | | X | X | | | X |
| `dfe-transform-vector` | | | | | | |
| `culvert` | n/a | n/a | | | n/a | n/a |

`python3 scripts/composition.py` prints that table from the manifest.

A fetcher instance is a source's processing step, so it arrives when the engine writes the source, not with the tier. Each transform declares `idle_when` instead. A tier seeds one of each, stood up empty and Ready, and the first source fills it rather than creating it. `per_config` multiplies them from there. Seeding places the first and doesn't cap the count. culvert is offered on the two HA tiers and enabled by an operator adding its values file.

Compose declares one service per app in a committed file and creates no additional instances at run time. So `docker-single` starts one archiver, one fetcher and one transform-vrl idle (see "Deployed before it has work"), and the source each is later given fills the one that's already there. `docker-slim` runs the core data path alone.

The six profile names come from `scripts/profiles.py`: four Kubernetes tiers and two Compose ones. An app can be default on a cluster and opt-in on a laptop.

## How it reaches a deployment

Helm can't read `apps.yaml`, so the derived set is rendered into each tier's values file:

    python3 scripts/composition.py --write-seed

That writes `deployRepo.seedApps` into `argocd/values/profile-<tier>.yaml`. The bundled deploy repo seeds it on a fresh deployment, with one `values/<app>-<instance>-values.yaml` per app and one Argo Application per file. The forgejo chart's own default is empty on purpose, because a list there would be a second copy of the composition that nothing keeps in step.

The engine reflects the whole manifest, not only the derived set, so the chart that mounts it carries its own copy:

    python3 scripts/composition.py --write-catalogue

That writes `helm/charts/dfe-extras/files/apps.yaml`, the manifest byte for byte under a generated banner. dfe-extras mounts it as a ConfigMap and points `DFE_ENGINE_APP_CATALOGUE_FILE` at it. Reloader restarts the engine when that ConfigMap changes, and after the restart `GET /api/v1/apps` returns the updated catalogue.

An engine with no mount falls back to the snapshot baked into its image, and adding an app would then need an engine release. So `dfe-ops stack-deploy` fails its pre-flight when the copy is stale or the render carries no catalogue.

dfe-docker projects the same manifest. `scripts/dfe-stack render --docker-profile <slim|single>` reads `default_in` for the matching `docker-<mode>` profile and writes dfe-docker's `service_profiles.yaml` block. `dfe-engine` and `dfe-ui` become the `core` footprint key, `hyperdx` becomes `hyperdx`, and every other app becomes a `services:` entry.

## Deployed before it has work

An app in the default composition is deployed whether or not anything has configured it, so an unconfigured one starts rather than crash-loops. Those apps declare `idle_when`, the config paths whose emptiness means no work, and carry the matching predicate in their own code. The Rust apps never read this manifest.

An idle app is Ready, serves health and metrics, opens no broker connection and binds no listener. It reports the `work_config` health component Degraded with the reason, holds the `pipeline_idle` gauge at 1, and starts the moment a config change gives it work. `dfe-ops acceptance --mode <tier>` asserts that against a live deploy, because the readiness gate passes an idle app by design.

An app whose work can arrive without a config change declares no `idle_when`. The loader is the case: an empty topic list is auto-discovery, so a topic appearing on the broker gives it work, and the gate only wakes on a config change.
