# docs-site

The source of https://dfe.docs.hyperi.io. A Docusaurus site with one build per released DFE stack under `/v/<stack>/`, plus an unlisted `/next/` built from each repository's default branch. This README is not published.

## Where each page lives

| On the site | Edit it in | Reaches the site |
|---|---|---|
| Overview (`/overview/`) | `content-root/README.md` | every version, when the change merges to main |
| Quickstarts (`/quickstart/...`) | `content-root/quickstart/` | every version, on merge |
| A repository's section (`/dfe-infra/...`, `/dfe-ui/...`) | that repository's `docs/` | `/next/` nightly at 16:17 UTC, a version only once a stack pins the new tag |
| The HTTP API reference | dfe-engine's `openapi-spec/openapi.json` | with the dfe-engine tag a stack pins |
| The landing page (`/`) | its words in `copy/landing.yaml`, its layout in `src/pages/index.js` and `src/components/landing/` | every version, on merge |
| Navbar, footer and release bar | `copy/site.yaml` | every version, on merge |

A change to dfe-infra's own `docs/` also refreshes `/next/` on merge.

The sidebar runs Overview, then four journeys: Evaluate, Deploy, Operate, Reference. `journeys.yaml` names each journey's sections in order and places every page in one, by repository and path, with a `default` per repository for pages it does not name. A new page in a repository's `docs/` lands in that repository's default section until a line places it. Pages keep their URLs, whichever section they sit in. Within a section, pages follow the lane order `suite.yaml` gives, then each repository's own order.

`publish.yaml` holds the publishing decisions: the released stacks the site does not build, the pages each repository keeps internal, the repositories whose section sits inside another's (dfe-hyperdx inside dfe-ui), and docs pins. A published page that links to an internal one links to it on GitHub.

To fix a released version's docs without a DFE release, merge the fix in the repository, then pin that commit under `docs_refs` for the stack in a `docs:` commit here. The merge deploys it. The API reference stays at the release.

`content-root/VOICE.md` is the voice ledger, one "say X, not Y" rule a line. It is never published.

`scripts/gate.py` is the docs gate. It reads every published page and reports process talk, hedges, the register words in `gate.yaml`, semicolons in prose, non-ASCII typography and "Elasticsearch", each with the repo, file, line and what to write instead. A pull request fails only on findings main does not already have. To keep a line as written, put `<!-- docs-gate: allow <rule> -- <reason> -->` at its end or on the line above. `python3 scripts/gate.py --help` has the block form, and `python3 scripts/gate.py path/to/page.md` checks a page before it merges.

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
