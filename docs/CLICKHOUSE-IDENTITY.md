<!--
  Project:      dfe-infra
  File:         docs/CLICKHOUSE-IDENTITY.md
  Purpose:      The ClickHouse identity set every DFE deployment gets, and which
                layer creates each part.
  License:      BUSL-1.1
  Copyright:    (c) 2026 HYPERI PTY LIMITED
-->

# ClickHouse identity

The deploy layer creates ONE credential, and supplies one password per app.
dfe-engine creates every user, role, grant, quota and row policy.

That split is forced rather than chosen. Nothing inside a deployment can mint
the first privileged account, so something outside must. Everything after that
first account is reachable by SQL, and SQL behaves the same on k8s, docker and a
local checkout -- so putting it anywhere else means writing it three times.

XML users settle it on their own: `users.xml` cannot express `GRANT`, a role, a
quota or a row policy. The engine is the only layer that can carry the model.

## The one credential

`default`, with a password, on every target.

| target | mechanism |
|---|---|
| k8s cluster | operator `spec.settings.defaultUserPassword`, secret-backed |
| k8s single | `CLICKHOUSE_USER` + `CLICKHOUSE_PASSWORD` on the StatefulSet |
| docker | `CLICKHOUSE_USER` + `CLICKHOUSE_PASSWORD` on the container |

`default` and not a nicer name, because it is the only account the ClickHouse
operator can password from a Secret. Any other name has to carry its password
inline in the `ClickHouseCluster` CR, which commits a credential to git.

This account exists for dfe-engine to create every other identity with, and to
break glass. dfe-engine is the only workload that connects as it: every app
connects as its own user, below.

## The supplied passwords

Each app runs in a pod of its own and cannot read the engine's secrets store, so
its password comes from the deploy layer. The engine creates the user, adopts the
supplied password from `DFE_CLICKHOUSE_<ROLE>_PASSWORD`, and re-asserts it on
every reconcile. The app connects with the same value.

| app | user | engine reads | k8s Secret | docker `.env` |
|---|---|---|---|---|
| hunt runner | `dfe_hunt_runner` | `DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD` | `hunt-runner-clickhouse` (`huntRunner.clickhouse`) | `DFE_HUNT_RUNNER_CLICKHOUSE_PASSWORD` |
| dfe-loader | `dfe_loader` | `DFE_CLICKHOUSE_LOADER_PASSWORD` | `loader-clickhouse` | `DFE_LOADER_CLICKHOUSE_PASSWORD` |
| keda shim, header-dev HyperDX | `dfe_query_reader` | `DFE_CLICKHOUSE_QUERY_READER_PASSWORD` | `query-reader-clickhouse` | `DFE_QUERY_READER_CLICKHOUSE_PASSWORD` |
| OTel collector | `dfe_otel_writer` | `DFE_CLICKHOUSE_OTEL_WRITER_PASSWORD` | `otel-writer-clickhouse`, copied into the collector's namespace | `DFE_OTEL_WRITER_CLICKHOUSE_PASSWORD` |

On k8s dfe-extras mints each Secret once beside the engine, with ESO's Password
generator, from `clickhouse.serviceUsers` in `argocd/values/common.yaml` (the
hunt runner's from `huntRunner.clickhouse`). They render with the engine, a wave
before every app that reads one. On docker `make init` mints each into `.env`.

HyperDX never holds the admin password. Under `oidc-proxy` the engine hands each team the connection it reads as, and on k8s HyperDX is given no ClickHouse password. On docker `DEFAULT_CONNECTIONS` carries the query reader's password in every mode, and only `header-dev` reads it.

The hunt runner's worker runs `INSERT ... SELECT` built from rule text, so its
user is the identity a crafted rule runs as: no `url()`, `s3()`, `remote()` or
`file()`, and no DDL.

Each app needs an engine whose contract names its password and dfe-schemas role
catalogue 1.2.0, which mints `otel_writer`. On an older pair the user never exists
and the app cannot connect. The apps and the keda shim wait for the engine to
answer ready, and the engine reconciles its service users before it does.

The collector's gateway waits too, on busybox's `wget`, because the collector image has no shell. It reads the logs table's columns once, at start, so a gateway that started before its user existed would write every log row without `EventName` until the pod restarted.

## The always-deployed set

Seeded by dfe-engine on every deployment, non-destructive and operator-editable.
Definitions: dfe-schemas `roles/clickhouse.yaml`.

| identity | CH objects | used by | privilege |
|---|---|---|---|
| `dfe_loader` | user + `dfe_loader_role` + `dfe_loader_profile` | dfe-loader | `INSERT ON dfe.*`, `SELECT ON system.columns` and `system.tables`, async-insert settings |
| `dfe_query_reader` | user + `dfe_query_reader_role` + profile | the platform team's HyperDX connection, the engine's parameterised views, the keda shim | `SELECT ON dfe.*` plus `system` introspection, `readonly=2` |
| `dfe_hunt_runner` | user + `dfe_hunt_runner_role` | hunt-runner | `SELECT`/`INSERT ON dfe.*` |
| `dfe_otel_writer` | user + `dfe_otel_writer_role` | the OTel collector's gateway | `INSERT ON dfe.otel_*`, `SELECT ON dfe.otel_traces`, `SELECT ON system.metrics`, `system.events` and `system.parts`, `SHOW TABLES ON dfe.*` and `system.*` |
| `dfe_otel_reader_role` | role only | admin + infra-admin group users | `SELECT ON dfe.*` |
| `dfe_tenant_role` | role + row policy | org-bound OIDC users | `SELECT` fenced by `_org_id` |
| `dfe_org_<org>` | user per org | `org_viewer` users | pins one org's `_org_id` |

A role with `mint_user` set also gets a user, with a secret the engine mints
through the scalo secrets seam unless the deploy layer supplies the password. The
rest are granted to an identity that already exists. The service users are
reconciled whether or not tenant isolation is on.

`readonly=2` on the reader is deliberate, not a slack setting: HyperDX sends
`date_time_output_format` with every query, and `readonly=1` rejects the whole
request. `allow_ddl=0` still bars DDL.

The writer's `SELECT ON dfe.otel_traces` is there because ClickHouse runs a
materialised view's query as the user whose insert fired it, and
`otel_traces_trace_id_ts_mv` reads `otel_traces`. Without it every traces batch
fails with code 497. Its `SHOW TABLES` reads no rows: `system.parts` lists only
the tables a user can see, and the collector reports every table's size.

## Accepted privilege

Each of these is wider than the app's queries need, and stays.

| who | holds | why it stays |
|---|---|---|
| keda shim | the all-orgs reader, with `system.query_log`, `text_log` and `processes` | it shares the platform reader's role, and no caller sends it SQL: it runs its catalogued queries with identifier-checked binds |
| OTel collector | `otel_traces` rows, and every `dfe` and `system` table's DDL through `SHOW TABLES` | the traces view needs the rows, the size report needs the tables listed, and no source table's rows are readable |
| dfe-loader | `INSERT ON dfe.*` | a source's table is created with the source, and the loader has no listener an outsider reaches |
| OTel collector | `INSERT ON dfe.otel_*` | a source table is named for its source, and a source name cannot hold `_` (dfe-engine `source/models.py`) |

## Quotas ride the tiers

A tier is the quota carrier. Each one reconciles to three CH objects -- a
settings profile, a quota and a role -- so nothing is granted without a
consumption envelope attached.

| object | name |
|---|---|
| role | `dfe_<tier>_role` |
| settings profile | `dfe_<tier>_profile` |
| quota | `dfe_<tier>_quota` |

Two families ship seeded and the set is extensible: `analyst` tiers for people,
`hunt` tiers for scheduled work. Each family has one default, applied to any
group that names no tier. A quota carries an interval plus per-interval maxima.

The engine hardcodes no tier list -- the reconciler applies whatever tiers the
governance config holds, so a deployment can add its own.

## Where each part is defined

| concern | home | applies on |
|---|---|---|
| the one credential | dfe-infra chart values, dfe-docker `.env` | its own target |
| each app's password | dfe-infra `clickhouse.serviceUsers` and `huntRunner.clickhouse` (dfe-extras), dfe-docker `.env` | its own target |
| service roles | `governance/ch/service-roles` (engine, versioned) | every target |
| quota tiers | `governance/ch/tiers` (engine, versioned) | every target |
| group to tier/org binding | engine governance | every target |

Do not declare a user in the `ClickHouseCluster` CR. A user there needs an
authentication method inline or ClickHouse refuses `users.yaml` outright and
every server crashloops (`CANNOT_LOAD_CONFIG`), and the result would apply on
k8s alone.

Shared-fact mechanics between dfe-infra and dfe-docker: `docs/CI-MAINTENANCE-DESIGN.md`.
