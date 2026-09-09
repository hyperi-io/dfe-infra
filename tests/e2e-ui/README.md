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
(localhost:18000). Forward first:

    kubectl -n dfe-local port-forward svc/dfe-hyperdx 18080:8080
    kubectl -n dfe-local port-forward svc/dfe-ui 13001:3000
    kubectl -n dfe-local port-forward svc/dfe-engine 18000:8000

The OIDC specs need the shared fixture password in `E2E_FIXTURE_PASSWORD`
(and `E2E_OIDC_PROVIDER` when the provider is not named dex). Fetch it with
the identity fixture's devpack (`fetch-secrets.sh` in the infrastructure
repository's dfe-oidc-testing subproject) -- never commit it.

The tenancy project talks to ClickHouse directly: `E2E_CH_URL` (default
localhost:18124 over a port-forward) and `E2E_CH_CREDS_JSON`, a
`{username: password}` map for the reconciler-minted identities. Specs
skip when the env is absent.

Chromium runs with a fresh profile every time -- never point this at a
personal browser profile.

## Layout

One harness, one Playwright project per deployed app, specs under
`specs/<app>/`.

- `harness/env.ts` -- every endpoint, env-driven; the config and all helpers
  read from here.
- `harness/users.ts` -- the identities the suite acts as. Grows into the
  shared 12-user OIDC fixture (docs/AUTH-TESTING.md) and the per-role
  capability truth table the RBA specs assert against.
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

## Where this grows

Next: the org_id tenancy matrix through ClickHouse for the org-scoped
fixture identities (single-org, multi-org union, platform roles unfiltered,
fail-closed), and per-role SEE/DO probes across the app surfaces. Specs whose
wiring has not shipped land `test.fail()`-marked, so the suite documents the
contract before it is true.

Prerequisites the suite assumes (the deploy provides them): an auth-mode
hyperdx seeded via DEFAULT_CONNECTIONS/DEFAULT_SOURCES, and the demo rows in
`dfe.main` (3 nerk, 2 acme).
