# e2e-ui -- browser-level acceptance for the deployed DFE stack

Playwright against a REAL deployment: OIDC flows, RBAC roles, org tenancy.
Complements dfe-engine's API-level e2e -- this layer proves what a person at a
browser actually gets.

## Run

    cd tests/e2e-ui
    npm install
    npx playwright install chromium
    npm test                          # all apps
    npx playwright test --project=hyperdx
    npm run typecheck

Endpoints come from env (defaults match the laptop port-forward layout):
`HYPERDX_URL` (localhost:18080), `DFE_UI_URL` (localhost:13001), `ENGINE_URL`
(localhost:8000). Forward first:

    kubectl -n dfe-local port-forward svc/dfe-hyperdx 18080:8080
    kubectl -n dfe-local port-forward svc/dfe-ui 13001:3000
    kubectl -n dfe-local port-forward svc/dfe-engine 8000:8000

The engine builds its OIDC callback from the origin the browser reached it on, so `ENGINE_URL` must be an origin registered as a redirect URI at the IdP, or the IdP refuses the sign-in. The default 8000 is the engine's own port; dfe-docker publishes it on 8003.

The OIDC specs need the shared fixture password in `E2E_FIXTURE_PASSWORD` (and `E2E_OIDC_PROVIDER` when the provider is not named dex). `dfe-ops idp deploy` writes it as `TESTER_IDP_USER_PASSWORD` in its `--secrets-out` file ([`docs/AUTH-TESTING.md`](../../docs/AUTH-TESTING.md)) -- never commit it.

## Live IdP sign-in (`oidc-live`)

`npx playwright test --project=oidc-live` signs in through hosted IdPs -- Okta, Entra ID, Google Workspace, or the Dex tester IdP -- on their own login pages, running the engine's real authorization-code flow. Wire the IdP into the deployment first with `dfe-ops idp wire-external` ([`docs/AUTH-TESTING.md`](../../docs/AUTH-TESTING.md)). Per provider it checks:

- the IdP accepts the engine's authorize request: Google must not redirect to `/signin/oauth/error`, Okta must not answer 400, and Entra's 200 page must carry no `AADSTS` code
- each expected user signs in and `/api/v1/auth/me` reports exactly its expected roles
- a user expecting no roles is also refused `GET /api/v1/oidc-providers` with 403, because the engine admits any authenticated user with a zero-role account

Everything tenant-specific comes from env, never from this repo:

| Variable | Holds |
|---|---|
| `E2E_OIDC_EXPECTATIONS` | Expectations files, or directories of `expectations-*.json`, separated by `:` |
| `E2E_OIDC_PROBE` | Extra providers to probe with no users yet, comma-separated `name` or `name:kind` |
| `E2E_OIDC_PASSWORD_<PROVIDER>` | The test users' password for that provider, else `E2E_OIDC_PASSWORD` |
| `E2E_OIDC_STORAGE_STATE_<PROVIDER>` | Google only: a Playwright storage state a person seeded by signing in once |

`<PROVIDER>` is the engine provider name upper-cased, with anything but letters and digits as `_` (`google-workspace` -> `GOOGLE_WORKSPACE`). An expectations file is `{"provider": "<engine provider name>", "users": {"<fixture name>": {"login": "<sign-in identifier>", "roles": ["<role>"]}}}`; an entry given as a bare role list has no login and skips. The IdP kind is read from the provider name (`dex`, `okta`, `entra`, `google`), or from a `"kind"` key in the file. Test titles carry fixture names only, never a login.

With none of it set the project passes with every test skipped. A provider whose password or session is missing skips its sign-ins and still runs the authorize probe. Google never types a password, since it refuses automated sign-in: its sign-ins reuse the seeded session, are tagged `@optional`, and a gate leaves them out with `--grep-invert @optional`. A Google sign-in that meets a password prompt fails, saying the session needs reseeding.

The `oidc-live` and `oidc-live-selftest` projects keep no trace, screenshot or video, because a trace records typed passwords and bearer tokens. The other projects keep a trace on failure locally, and none at all when `CI` is set.

`npx playwright test --project=oidc-live-selftest` runs the same checks offline against `harness/idp/mock-idp.ts` and the synthetic `fixtures/oidc-live`. It proves the harness flow, not any IdP's markup.

The tenancy project talks to ClickHouse directly: `E2E_CH_URL` (default
localhost:18124 over a port-forward) and `E2E_CH_CREDS_JSON`, a
`{username: password}` map for the reconciler-minted identities. Specs
skip when the env is absent.

Chromium runs with a fresh profile every time -- never point this at a
personal browser profile.

## Failure artefacts

A failed test writes `error-context.md` whatever the trace, screenshot and video settings say, and its page snapshot shows a typed password in the clear, masked input or not. `playwright.config.ts` sets `PLAYWRIGHT_NO_COPY_PROMPT`, which drops the snapshot for every project. The error text and the source frame stay, so a spec must not put a password in either.

## Layout

One harness, one Playwright project per deployed app, specs under
`specs/<app>/`.

- `harness/env.ts` -- every endpoint, env-driven; the config and all helpers
  read from here.
- `harness/users.ts` -- the identities the suite acts as. Grows into the OIDC fixture identities (docs/AUTH-TESTING.md) and the per-role capability truth table the RBA specs assert against.
- `harness/auth.ts` -- login strategies. Today: hyperdx local email/password
  (register-or-login). Next: the dex redirect flow, same shape.
- `harness/fixtures.ts` -- extended `test` with `hyperdxPage`: a page already
  inside a logged-in session, state cached per worker so login runs once.
- `harness/ch.ts` -- query ClickHouse through hyperdx's clickhouse-proxy with
  the session's cookies: the byte-identical path UI queries take.

## What exists

- `specs/hyperdx/tenancy-proxy.spec.ts` -- nerk connection counts 3, acme 2,
  and a query-text override of the tenant pin is refused. DOM-independent,
  the anchor spec.
- `specs/hyperdx/tenancy.spec.ts` -- the same truth through the UI (source
  picker, search results). Selectors are calibrated against the deployed
  build; if upstream reshuffles the DOM, fix these and leave the proxy spec
  alone.
- `specs/dfe-ui/smoke.spec.ts` -- the deployed dfe-ui serves its shell.
- `specs/engine/oidc-rba.spec.ts` -- the twelve-user OIDC role matrix: real
  IdP redirect flow per fixture identity, then roles + org_ids asserted from
  `/auth/me` against the hand-written truth table.
- `specs/engine/oidc-live.spec.ts` -- sign-in through hosted IdPs against an
  env-provided expectations table, run as the `oidc-live` project (above).
  The per-IdP steps live in `harness/idp/`, the flow in `harness/oidc-live.ts`.

## Where this grows

Next: the org_id tenancy matrix through ClickHouse for the org-scoped
fixture identities (single-org, multi-org union, platform roles unfiltered,
fail-closed), and per-role SEE/DO probes across the app surfaces. Specs whose
wiring has not shipped land `test.fail()`-marked, so the suite documents the
contract before it is true.

Prerequisites the suite assumes (the deploy provides them): an auth-mode
hyperdx seeded via DEFAULT_CONNECTIONS/DEFAULT_SOURCES, and the demo rows in
`dfe.main` (3 nerk, 2 acme).
