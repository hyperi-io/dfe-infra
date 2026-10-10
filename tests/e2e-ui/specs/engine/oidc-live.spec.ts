// Project:   dfe-infra
// File:      tests/e2e-ui/specs/engine/oidc-live.spec.ts
// Purpose:   Live sign-in through hosted IdPs, checked against an expectations table
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// Run as the oidc-live project, never the engine one: a failed run's trace would
// keep the typed password. Per provider: the IdP must accept the engine's
// authorize request, then each expected user signs in through the IdP's own
// page and must hold exactly its expected roles; a user expecting none must
// also be refused a role-gated route. Each provider, and the whole project,
// skips cleanly when its env is absent (tests/e2e-ui/README.md).

import { ENGINE_URL } from '../../harness/env';
import { defineOidcLiveTests, loadLiveProviders } from '../../harness/oidc-live';

defineOidcLiveTests(loadLiveProviders(process.env), () => ENGINE_URL, process.env);
