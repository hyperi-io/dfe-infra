# docs-site

The source of https://dfe.docs.hyperi.io. A Docusaurus site with one build per released DFE stack under `/v/<stack>/`, plus an unlisted `/next/` built from each repository's default branch. This README is not published.

## Where each page lives

| On the site | Edit it in | Reaches the site |
|---|---|---|
| Overview (`/overview/`) | `content-root/README.md` | every version, when the change merges to main |
| Quickstarts (`/quickstart/...`) | `content-root/quickstart/` | every version, on merge |
| A repository's section (`/dfe-infra/...`, `/dfe-ui/...`) | that repository's `docs/` | `/next/` nightly at 16:17 UTC, a version only once a stack pins the new tag |
| The HTTP API reference | dfe-engine's `openapi-spec/openapi.json` | with the dfe-engine tag a stack pins |
| The landing page (`/`) | `src/pages/index.js` and `src/components/landing/` | every version, on merge |

A change to dfe-infra's own `docs/` also refreshes `/next/` on merge.

The sidebar is generated from the tree. A root page sets its place below 10, with `sidebar_position` front matter, or `position` in a directory's `_category_.json`. Each repository follows in the lane order `suite.yaml` gives, opening on its `docs/README.md`, else its `architecture.md`.

`publish.yaml` holds the three publishing decisions: the released stacks the site does not build, the pages each repository keeps internal, and the repositories whose section sits inside another's (dfe-hyperdx inside dfe-ui). A published page that links to an internal one links to it on GitHub.

`content-root/VOICE.md` is the voice ledger, one "say X, not Y" rule a line. It is never published.

## Running it locally

Once, from this directory:

```bash
npm ci
```

Then:

```bash
npm run preview
```

That assembles the newest release (about a minute while it fetches each repository), then serves it at http://localhost:3000/v/2.2.1/. Save a page under `content-root/` and the browser reloads it in a few seconds. `python3 scripts/preview.py --help` lists the options: `--work-dir` keeps the checkouts so the next start skips the fetch, and `--no-aggregate` serves what is already assembled.

Edits to a repository's `docs/` do not show in the preview, because the build fetches each repository at the tag its stack pins. The full build, every version as CI deploys it:

```bash
python3 scripts/aggregate.py --next
python3 scripts/build_site.py
npm run serve
```

`build_site.py` builds every version into `build/` and writes the root redirect to the newest release. CI runs the same two steps in `.github/workflows/docs.yml` for every pull request that touches this directory, and deploys from main.
