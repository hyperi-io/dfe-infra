// Project:   dfe-infra
// File:      tests/e2e-ui/harness/ch.ts
// Purpose:   Query ClickHouse through hyperdx's own clickhouse-proxy
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// The proxy path is byte-identical to what the UI's queries take, so asserting
// through it proves the deployed tenancy chain (session -> connection ->
// pinned ClickHouse user -> row policy) without depending on the DOM.

import { type APIRequestContext, type APIResponse } from '@playwright/test';

interface NamedObject {
  readonly name: string;
  readonly id?: string;
  readonly _id?: string;
}

async function idsByName(
  api: APIRequestContext,
  path: string,
): Promise<Map<string, string>> {
  // One retry: kubectl port-forward drops idle keep-alive sockets, which
  // surfaces as "socket hang up" on the first request after a pause.
  let resp;
  try {
    resp = await api.get(path);
  } catch {
    resp = await api.get(path);
  }
  if (!resp.ok()) {
    throw new Error(`GET ${path} failed: ${resp.status()}`);
  }
  const objects = (await resp.json()) as NamedObject[];
  const byName = new Map<string, string>();
  for (const o of objects) {
    const id = o.id ?? o._id;
    if (id === undefined) {
      throw new Error(`${path}: ${o.name} has no id field`);
    }
    byName.set(o.name, id);
  }
  return byName;
}

export function connectionsByName(
  api: APIRequestContext,
): Promise<Map<string, string>> {
  return idsByName(api, '/api/connections');
}

export function sourcesByName(
  api: APIRequestContext,
): Promise<Map<string, string>> {
  return idsByName(api, '/api/sources');
}

export async function chQuery(
  api: APIRequestContext,
  connectionId: string,
  sql: string,
): Promise<APIResponse> {
  return api.post('/api/clickhouse-proxy?default_format=JSONCompact', {
    headers: {
      'Content-Type': 'text/plain',
      'x-hyperdx-connection-id': connectionId,
    },
    data: sql,
  });
}

export async function chCount(
  api: APIRequestContext,
  connectionId: string,
  sql: string,
): Promise<number> {
  const resp = await chQuery(api, connectionId, sql);
  if (!resp.ok()) {
    throw new Error(`proxy query failed: ${resp.status()} ${await resp.text()}`);
  }
  const body = (await resp.json()) as { data: [[string | number]] };
  const cell = body.data[0]?.[0];
  if (cell === undefined) {
    throw new Error('proxy query returned no rows');
  }
  return Number(cell);
}
