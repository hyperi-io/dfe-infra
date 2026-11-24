---
sidebar_position: 2.2
sidebar_label: Organisations and _org_id
---

# Organisations and `_org_id`

DFE is multi-tenant from the first row. Every record has an `_org_id`, every source table has the column, and ClickHouse decides who reads which rows from it. That's why the setup wizard asks for your organisation before anything else, even on a deployment that sends everything to the default `main` source and table.

## Your org

The setup wizard registers an organisation and names it yours, the organisation running the deployment. It can't be deleted or disabled. Register more organisations (`/api/v1/orgs`) when you host customers, or when you want business units, regions or teams to see their data and no one else's.

Each organisation owns one or more tenant ids, the `_org_id` values its records hold. When DFE stamps a record for an org, it uses the org's first tenant id.

## `_org_id` in the tables

Every common header a source table starts from defines `_org_id` as a non-null, low-cardinality string in the table's sort key ([meta schema](../dfe-schemas/meta-schema.md)). dfe-engine creates the tables from the `dfe-schemas` definitions, all in one database, so tenants are kept apart by row, not by database. There's no tenant table to create and no view to write per customer.

## How a record gets its `_org_id`

- For pushed data, dfe-receiver reads the bearer token a sender presents. Each ingest credential belongs to an organisation and binds the orgs its records may land in, one of them its default. By default the receiver drops an `_org_id` the sender put in the record, and the record takes the default org's tenant id. Where the deployment lets senders set it, or a routing rule picks one, an org outside the binding is replaced by the default.
- For fetched data, the source the fetcher polls names its org, and every record it fetches gets that org's tenant id. A source that names none fetches under `unassigned`.
- A bearer listener no credential lists runs without auth, and its records land under `unassigned`. No organisation can hold that id, so a login that reads every org, such as `admin`, is the one that sees them. Create an ingest credential for your org and point your senders at it, and their records land in your org.

dfe-engine compiles `unassigned` into the receiver, the fetcher and the loader as the tenant for a record that names no org, so a record that reaches the loader without an `_org_id` takes it there too. A topic your own pipeline writes is the exception: a record there without one takes the org of the source the topic belongs to. The loader writes each record to the table its `_source` names, in the shared database, unless the source maps it to another table ([source routing](../dfe-engine/data-plane/source-routing.md)).

## How ClickHouse holds each org to its rows

dfe-engine's ClickHouse reconcile gives each organisation a ClickHouse user and pins that user's tenant ids read-only, so a query can't change them. It runs at startup and after every org or group change. Every table with `_org_id` has a restrictive row policy for org users, which returns the rows whose `_org_id` is one of the user's ids and no others. A table without the column, such as the stack's own telemetry, returns nothing to an org user. A platform user isn't an org user, and reads every org. Adding an org needs a user and no new policy ([ClickHouse identity](../dfe-infra/clickhouse-identity.md)).

The fence sits in ClickHouse itself, so it holds the same for the console and a plain SQL client.

## The console and your IdP

Your identity provider's groups map to DFE groups, and a group bound to an organisation reads through that org's ClickHouse user, whatever roles it holds. `org_viewer` is the role that holds a person to their organisations' rows. A platform group, such as the one holding `admin`, reads every org ([users and OIDC](users-and-oidc.md)).

In the DFE console, a person held to an organisation joins that org's team, and the team queries ClickHouse as the org's user. A search or a dashboard sees that org's rows and no others, and nobody has to remember to filter. Platform people join a platform team, which reads every org ([RBAC and multi-tenant ClickHouse](../dfe-engine/control-plane/rbac.md)).
