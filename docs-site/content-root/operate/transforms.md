---
sidebar_position: 3
sidebar_label: Transforms
---

# Transforms

A transform reshapes a source's records between the receiver and the loader. It is optional. Without one, records go from landing straight to the loader. With one, they go landing, transform, load, and the engine runs one transform instance for that source.

## Pick an engine

| Engine | Use it for | Read |
|---|---|---|
| `elastic` | Beats, Elastic Agent and raw vendor syslog. It produces the ECS output Elastic's own ingest pipeline would | [Parity](../dfe-transform-elastic/parity.md), then [configuration](../dfe-transform-elastic/configuration.md) |
| `vrl` | Your own VRL programs, compiled into one binary with no subprocess | [Architecture](../dfe-transform-vrl/architecture.md) |
| `vector` | Pipelines that need a transform VRL does not have: `lua`, `aggregate`, `dedupe`, `throttle`, `sample` | [Architecture](../dfe-transform-vector/architecture.md), and the [migration guide](../dfe-transform-vector/MIGRATION.md) for an existing pipeline |

If VRL does the job, use `vrl`. The `vector` engine keeps a subprocess alive beside the supervisor, and `vrl` does not.

## Attach one to a source

A source's `transform` block names the engine:

```yaml
transform:
  engine: elastic
  variant: filebeat.okta.default   # the compiled-in program, where the engine offers a catalogue
```

- `variant` picks one program from the engine's catalogue. `elastic` has one per integration.
- `mode: agentless` is `elastic` only. It works out which program each event needs, and it takes no `variant`. That is how `make demo` sorts firewall syslog by vendor.
- `config_file`, `env` and `files` carry the engine's own config, per-transform environment and enrichment files (CSV, MMDB).

The console's source page shows the transform stage on its Flow card, with the app, the variant and the files it reads. [The source definition](../dfe-engine/data-plane/source-definition.md) has every field and what a save is checked against. A change reaches the pod through the deploy repo, so a hand edit to a deployed artefact is drift the engine reports and re-syncs.

## Enrichment

`elastic` adds GeoIP, user agent and community ID. [Enrichment](../dfe-transform-elastic/enrichment.md) says what each reads. [Parsers and processors](../dfe-transform-elastic/parsers.md) lists every ingest processor it implements.
