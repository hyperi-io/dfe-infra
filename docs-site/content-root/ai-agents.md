# Using these docs with an AI agent

Point your agent at https://dfe.docs.hyperi.io/llms.txt.

| URL | What it holds |
|---|---|
| `/llms.txt` | Every prose page of the newest release, with a one-line description and a link to the page as markdown |
| `/latest/<page>.md` | One page as markdown, for example https://dfe.docs.hyperi.io/latest/quickstart/docker.md |
| `/latest/llms/<section>.txt` | The prose pages of one section in one file, such as `dfe-engine` or `overview` |
| `/latest/openapi/dfe-engine.json` | The engine's HTTP API as an OpenAPI spec, in place of the API reference pages |

`/latest/` redirects to the newest release. To hold an agent to the release you run, swap `latest` for `v/<release>`, as in `/v/2.2.2/llms.txt`. Each prose page also names its markdown copy in a `<link rel="alternate" type="text/markdown">` tag.

## An instruction to paste

```text
Use the DFE documentation at https://dfe.docs.hyperi.io/v/<release>/llms.txt.
Read the pages it lists as markdown, and the section bundles when you need a
whole section. For the engine's HTTP API, read
https://dfe.docs.hyperi.io/v/<release>/openapi/dfe-engine.json.
Answer from those files, and say which page each answer came from.
```

Replace `<release>` with the version you deploy, such as `2.2.2`.

## What the agent gets

The published documentation only: these suite-level pages, each suite repository's `docs/` at the version the release pins, and the engine's published API spec. Source code stays on GitHub, where a page links to it.
