---
sidebar_position: 2.5
sidebar_label: Sources
---

# Sources

DFE ships a catalogue of ready-made sources. Browse it, pick the ones you run, and create them in one call instead of writing each definition by hand.

## Browse the catalogue

```bash
dfe sources catalogue list --intake beats --search cisco
```

`--intake` takes `beats`, `receiver` or `fetcher`. `--search` matches the entry name, its package and its data stream. Over the API it's `GET /api/v1/sources/catalogue` with the same two filters.

Entries you've already added as sources are left out, so the list doubles as a to-do list.

Each entry says:

- Which intakes it supports, and which it uses if you don't name one. That is the first of fetcher, beats and receiver the entry lists.
- Whether the table has a schema. A fetcher entry with none is refused on create with a 422, because a fetcher on its own topic needs one.
- Its transform engine and variants, and whether the deployed transform ships the variant.
- Whether this deployment can run it. A gRPC deployment can't run per-source apps (a fetcher or a transform), so the API refuses entries that need one. `runnable_reason` says why.

## What the intake changes

The intake is how the events reach DFE, and it decides the receiver's match rule.

| Intake | Arrives by | The source matches on |
|---|---|---|
| `beats` | An Elastic Agent or Beat | `data_stream.dataset`, which every Elastic-shipped event carries |
| `receiver` | A push to dfe-receiver | `_source`, set to the source name |
| `fetcher` | dfe-fetcher dials out | No receiver rule. The source carries a fetcher block instead |

## Create from it

`POST /api/v1/sources/from-catalogue` takes up to 50 entries and stores each as an ordinary source:

```json
{
  "entries": [
    { "entry": "cisco_ios" },
    { "entry": "cisco_meraki", "intake": "receiver", "name": "meraki-edge" }
  ]
}
```

Only `entry` is required. `intake`, `name`, `variant`, `transport` (`bus` or `direct`) and `archive` fall back to the entry's own default, and to the deployment's default for the transport. `archive: true` keeps the raw record and needs the bus transport. The CLI form is `dfe sources from-catalogue --entries ...`.

If one entry is refused, the others still go in. The reply lists `created` and `failed`, and each failure carries a code and a message. The apps are reconciled once for everything that was stored, so `apps_synced` and `restart_required` come back once for the whole call.

## When a variant is missing

The deployed transform publishes what it can run. Each entry reads that list and shows one of three states:

- `available`: the transform lists the variant.
- `missing_variant`: it doesn't. A create is refused with a 409.
- `unknown`: no contract could be read. A create is allowed.

If you see `missing_variant`, the transform image is older than the catalogue. Upgrade it, or pick another variant.

## Deploy the source

The result is an ordinary source. Deploy it to make the table: `POST /api/v1/sources/{name}/deploy`. [Storing and operating a source](../dfe-engine/data-plane/source-registry.md) says what each state does. [The source flow](../dfe-engine/data-plane/source-flow.md) has the routing, the archive and the catalogue's file format.
