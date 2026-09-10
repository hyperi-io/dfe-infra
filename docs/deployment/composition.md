# Default composition: which apps a profile deploys

`apps.yaml` is the source of truth. Each app carries `profiles` (where it MAY
run) and `default_in` (where it IS run when nobody says otherwise). `default_in`
absent means the same set as `profiles`; an empty list means the app is deployed
on demand and never seeded.

| App | slim | single | scale | mesh | docker-slim | docker-single |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| `dfe-receiver` | X | X | X | X | X | X |
| `dfe-loader` | X | X | X | X | X | X |
| `dfe-archiver` | X | X | X | X | X | X |
| `dfe-engine` | X | X | X | X | X | X |
| `dfe-ui` | X | X | X | X | X | X |
| `hyperdx` | X | X | X | X | X | X |
| `dfe-fetcher` | | | | | | |
| `dfe-transform-vrl` | | | | | | |
| `dfe-transform-vector` | | | | | | |
| `culvert` | n/a | n/a | | | n/a | n/a |

`python3 scripts/composition.py` prints that table from the manifest, and
`scripts/tests/test_composition.py` fails when the two disagree. A fetcher or a
transform instance IS a source's processing step, so it arrives when the engine
writes the source rather than with the profile. culvert is offered on the two HA
tiers and enabled by an operator adding its values file.

The six profile names are `scripts/profiles.py`'s -- four Kubernetes tiers and
two Compose ones, because an app can be default on a cluster and opt-in on a
laptop.

## How it reaches a deployment

Helm cannot read `apps.yaml`, so the derived set is rendered into each tier's
values file:

    python3 scripts/composition.py --write-seed

That writes `deployRepo.seedApps` into `argocd/values/profile-<tier>.yaml`,
which the bundled deploy repo seeds on a fresh deployment -- one
`values/<app>-<instance>-values.yaml` per app, and one Argo Application per
file. The forgejo chart's own default is empty on purpose: a list there would be
a second copy of the composition that nothing keeps in step.

dfe-docker projects the same manifest. `scripts/dfe-stack render
--docker-profile <slim|single>` reads `default_in` for the matching
`docker-<mode>` profile and writes dfe-docker's `service_profiles.yaml` block:
`dfe-engine` and `dfe-ui` become the `core` footprint key, `hyperdx` becomes
`hyperdx`, and every other app becomes a `services:` entry.

## Deployed before it has work

An app in the default composition is deployed whether or not anything has
configured it, so an unconfigured one starts rather than crash-loops. Those apps
declare `idle_when` -- the config paths whose emptiness means no work -- and
carry the matching predicate in their own code; the Rust apps never read this
manifest.

An idle app is Ready, serves health and metrics, opens no broker connection and
binds no listener, reports the `work_config` health component Degraded with the
reason, holds the `pipeline_idle` gauge at 1, and starts the moment a config
change gives it work. `dfe-ops acceptance --mode <tier>` asserts that against a
live deploy, because the readiness gate passes an idle app by design.

An app whose work can arrive WITHOUT a config change declares no `idle_when`.
The loader is the case: an empty topic list is auto-discovery, so a topic
appearing on the broker gives it work, and the gate only wakes on a config
change.
