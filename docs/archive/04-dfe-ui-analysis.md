# DFE-UI Codebase Analysis

**Date:** 2026-03-30
**Repository:** `/projects/dfe-ui/`
**License:** FSL-1.1-ALv2 (HYPERI PTY LIMITED)

---

## 1. Architecture Overview

### 1.1 Monorepo Structure (Turborepo)

Turborepo monorepo using Yarn 4.13.0 (`nodeLinker: node-modules`) and Turbo 2.8.13.

```
dfe-ui/
├── apps/
│   └── dfe-core-ui/              # Main Next.js 16 application
├── packages/
│   ├── dfe-engine-types/         # Auto-generated TypeScript types from OpenAPI spec
│   ├── dfe-icons/                # SVG icon library (~4,985 icons) with SVGR
│   ├── dev-logger/               # Dev-only logging utility
│   └── typescript-config/        # Shared tsconfig presets
├── scripts/audit.mjs             # Yarn audit wrapper
├── turbo.json                    # Task configuration
└── package.json                  # Root workspace config
```

### 1.2 Apps and Packages

| Artifact | Package Name | Purpose |
|---|---|---|
| `apps/dfe-core-ui` | `dfe-core-ui` | Main DFE management console (Next.js 16) |
| `packages/dfe-engine-types` | `@dfe/dfe-engine-types` | OpenAPI-generated types (API v1.6.9) |
| `packages/dfe-icons` | `@dfe/icons` | ~4,985 SVG icons (Tabler-style), SVGR React components |
| `packages/dev-logger` | `@dfe/dev-logger` | Console logging (dev-only) |
| `packages/typescript-config` | `@repo/typescript-config` | Shared tsconfig: base, nextjs, react-library |

### 1.3 HyperDX Integration

HyperDX is **NOT an npm dependency**. It is an **external web application** integrated via:

1. **Sidebar links** -- Dynamically added when `NEXT_PUBLIC_HYPERDX_URL` is set. Opens HyperDX pages (Search, Chart Explorer, Dashboards) in new browser tabs.

2. **Cross-origin `postMessage` bridge** -- HyperDX sends `CREATE_RULE_FROM_SEARCH` messages. dfe-ui validates origin, persists search data to IndexedDB (Dexie), navigates to `/rules/create?searchId={uuid}`.

3. **IndexedDB persistence** -- Search data survives page reloads via Dexie `RuleFromSearch` database.

4. **Source type distinction** -- Rules can have `source_type: 'raw' | 'hyperdx'` based on `searchId` query parameter.

### 1.4 Frontend Stack

- **Next.js 16.1.6** with **App Router** (not Pages Router)
- **React 19.2.3**
- **Turbopack** bundler
- Server components for auth gating; client components for interactivity
- No SSG/ISR -- all dynamically rendered

### 1.5 API Integration with dfe-engine

- **Type-safe API client** (`core/config/api/client.ts`): Generic factory with `get/post/put/delete/patch`
- **Centralized endpoints** (`core/config/api/endpoints/`): Frozen config mapping all DFE Engine API routes
- **React Query layer**: TanStack React Query for caching, mutations, optimistic updates
- **Base URL**: `NEXT_PUBLIC_API_URL` environment variable

---

## 2. Technical Implementation

### 2.1 Tech Stack

| Layer | Technology | Version |
|---|---|---|
| Framework | Next.js (App Router) | 16.1.6 |
| UI Library | React | 19.2.3 |
| Components | Ant Design | 6.3.1 |
| Styling | Tailwind CSS 4 + CSS-in-JS | 4 |
| Server State | TanStack React Query | 5.90.21 |
| Auth | next-auth (CredentialsProvider) | 4.24.13 |
| Code Editor | Ace Editor | 14.0.1 |
| Validation | Zod | 4.3.6 |
| Client Storage | Dexie (IndexedDB) | 4.3.0 |
| Testing | Vitest + Testing Library + MSW | 4.0.18 |
| Docs | Storybook | 10.2.15 |
| TypeScript | strict | 5.9.2 |

### 2.2 Key Features

- **Authentication**: Login + JWT token management + server-side session checks
- **Source Management**: Full CRUD with infinite scroll, detail split pane, create/clone/delete
- **Rule Management**: Rule creation from SQL (raw or HyperDX source), SQL validation, CEL filter
- **Navigation**: Collapsible sidebar with HyperDX links, theme toggle
- **Dark Mode**: Full light/dark via CSS custom properties + Tailwind dark variant

### 2.3 Route Structure

```
/                     → Home (placeholder)
/login               → Login page (public)
/sources             → Sources list with detail splitter
/rules               → Rules list (placeholder)
/rules/create        → Create rule form
/api/auth/[...nextauth] → NextAuth API routes
```

### 2.4 Authentication

**Current: Local credentials only, no OIDC.**

Flow:
1. `next-auth` v4 with `CredentialsProvider`
2. POSTs to dfe-engine `/api/v1/auth/login`
3. Fetches roles from `/api/v1/auth/me`
4. JWT session with `accessToken`, `expiresIn`, `roles`
5. Middleware protects all routes except `/login`, `/api/auth`, statics

### 2.5 State Management

- **Server state:** TanStack React Query v5 (no Redux/Zustand)
- **Client state:** React Context (theme, source list), URL search params, IndexedDB (Dexie), localStorage (theme)

### 2.6 Styling

**Dual system:** Tailwind CSS 4 + Ant Design 6 CSS-in-JS

**Layer ordering:** `@layer base, theme, antd, utilities;`

**Design tokens in three places:**
1. CSS custom properties (`__dfe.tokens.css`) -- light/dark colours
2. Tailwind theme (`tailwind.config.css`) -- spacing, typography
3. TypeScript tokens -- colours, typography for Ant Design ConfigProvider

### 2.7 Build and CI

- `hyperi-ci` with quality, test, build, publish stages
- Semantic release (conventional commits)
- Build: Turbo -> packages first -> Next.js build with Turbopack

---

## 3. Installation Dependencies

### 3.1 Requirements
- **Node.js:** >=18
- **Yarn:** 4.13.0
- **Turbo:** >=2.8.13

### 3.2 External Services

| Service | Variable | Purpose |
|---|---|---|
| DFE Engine API | `NEXT_PUBLIC_API_URL` | All data operations |
| HyperDX | `NEXT_PUBLIC_HYPERDX_URL` | External observability UI (optional) |
| NextAuth | `NEXTAUTH_SECRET` | JWT signing |

### 3.3 HyperDX/MongoDB Dependencies

**dfe-ui has zero MongoDB/FerretDB dependencies.** All MongoDB dependency is within HyperDX itself.

---

## 4. DFE 2.2 Upgrade Considerations

### 4.1 FerretDB Migration

**dfe-ui impact: NONE.** No MongoDB driver, no database connection. All data via DFE Engine REST API.

**HyperDX impact (external):** MongoDB connection string pointed at FerretDB. Risk: aggregation pipeline compatibility gaps.

**The `postMessage` bridge is MongoDB-agnostic** -- passes JSON payloads.

### 4.2 PostgreSQL 17

**No direct dfe-ui integration.** UI doesn't connect to PostgreSQL. Impact is entirely via dfe-engine API.

### 4.3 OIDC/OAuth2 Auth

**Required changes:**

1. Add OIDC provider to next-auth (e.g., `KeycloakProvider` or generic)
2. Update JWT callback for OIDC tokens
3. Add env vars: `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`
4. Update login page: local form + "Sign in with SSO" button
5. Token refresh handling for OIDC
6. Role mapping from ID token claims

**Effort: Moderate.** next-auth supports this natively. Multi-provider design aligns with "local fallback + external OIDC".

### 4.4 HyperDX + OTel + ClickHouse

**Current readiness: HIGH** for UI layer.

In place: sidebar links, postMessage bridge, rule creation workflow.

Gaps:
- No OTel configuration UI (Services/Deployments endpoints exist but no UI)
- No auth handoff between dfe-ui and HyperDX (both should use same OIDC)
- External-link approach works but isn't seamless (consider iframe or native viz)

### 4.5 Multi-Cloud Deployment

**UI is deployment-agnostic.** All external dependencies via env vars. No cloud-specific code. Deploys as Docker container on any K8s.

---

## 5. HyperDX Deep Dive

### 5.1 Separation of Concerns

**HyperDX:** Log/trace/metric search, Chart Explorer, Dashboards
**dfe-ui:** DFE platform config, detection rules, source management, auth

### 5.2 HyperDX Data Sources

- **ClickHouse:** Primary data store (OTel pipeline target)
- **MongoDB (-> FerretDB):** HyperDX internal metadata
- Data flow: `OTel Collectors -> ClickHouse <- HyperDX (queries) <- dfe-ui (links/bridge)`

### 5.3 Configuration

HyperDX configured via its own deployment config. dfe-ui integration is entirely via:
- `NEXT_PUBLIC_HYPERDX_URL` (sidebar links, postMessage origin validation)
- Feature-flagged: HyperDX links only appear when URL is set

### 5.4 FerretDB Risks

All in HyperDX deployment, not dfe-ui:
- Aggregation pipeline compatibility
- Index behaviour differences
- Connection string format
- Performance characteristics (wire protocol translation overhead)

---

## Appendix: Incomplete/TODO Items

1. Rules list page -- placeholder only
2. Home page -- placeholder
3. Source form: match, transform, fetcher, sigma sections have TODOs
4. Services/Deployments/FieldMaps/Alerts -- API endpoints defined but no UI
5. Sidebar version hardcoded "v1.0.0"
6. No charting library -- all viz delegated to HyperDX
