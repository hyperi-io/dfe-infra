// Project:   dfe-infra
// File:      tests/e2e-ui/harness/env.ts
// Purpose:   Deployment endpoints for the suite, from env with laptop defaults
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// Single source for every endpoint the suite touches. Env-driven so the same
// suite runs against any deployment (product rule: devex is ONE deployment);
// the defaults match the laptop port-forward layout in the README.

export const HYPERDX_URL = process.env.HYPERDX_URL ?? 'http://localhost:18080';
export const DFE_UI_URL = process.env.DFE_UI_URL ?? 'http://localhost:13001';
export const ENGINE_URL = process.env.ENGINE_URL ?? 'http://localhost:18000';
