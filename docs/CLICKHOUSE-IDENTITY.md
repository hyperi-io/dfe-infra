<!--
  Project:      dfe-infra
  File:         docs/CLICKHOUSE-IDENTITY.md
  Purpose:      The ClickHouse identity set every DFE deployment gets, and which
                layer creates each part.
  License:      BUSL-1.1
  Copyright:    (c) 2026 HYPERI PTY LIMITED
-->

# ClickHouse identity

The deploy layer creates ONE credential. dfe-engine creates every role, grant,
quota and row policy.

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

This account exists to bootstrap the engine and to break glass. Applications
move off it and onto their service identity.

## The always-deployed set

Seeded by dfe-engine on every deployment, non-destructive and operator-editable.
Definitions: `dfe_engine/governance/ch/models.py`.

| identity | CH objects | used by | privilege |
|---|---|---|---|
| `dfe_loader` | user + `dfe_loader_role` + `dfe_loader_profile` | dfe-loader | `INSERT ON dfe.*`, `INSERT ON dfe_hunts.*`, async-insert settings |
| `dfe_query_reader` | user + `dfe_query_reader_role` + profile | HyperDX connections | `SELECT` on both data databases plus `system` introspection, `readonly=2` |
| `dfe_hunt_runner_role` | role only | hunt-runner | `SELECT`/`INSERT ON dfe_hunts.*` |
| `dfe_otel_reader_role` | role only | admin + infra-admin group users | `SELECT ON dfe.*` |
| `dfe_tenant_role` | role + row policy | org-bound OIDC users | `SELECT` fenced by `_org_id` |
| `dfe_org_<org>` | role per org | `org_viewer` users | pins one org's `_org_id` |

A role with `mint_user` set also gets a user and a generated secret through the
scalo secrets seam. The rest are granted to an identity that already exists.

`readonly=2` on the reader is deliberate, not a slack setting: HyperDX sends
`date_time_output_format` with every query, and `readonly=1` rejects the whole
request. `allow_ddl=0` still bars DDL.

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
| service roles | `governance/ch/service-roles` (engine, versioned) | every target |
| quota tiers | `governance/ch/tiers` (engine, versioned) | every target |
| group to tier/org binding | engine governance | every target |

Do not declare a user in the `ClickHouseCluster` CR. A user there needs an
authentication method inline or ClickHouse refuses `users.yaml` outright and
every server crashloops (`CANNOT_LOAD_CONFIG`), and the result would apply on
k8s alone.

Shared-fact mechanics between dfe-infra and dfe-docker: `docs/SHARED-SSOT.md`.
