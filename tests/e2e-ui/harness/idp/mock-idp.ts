// Project:   dfe-infra
// File:      tests/e2e-ui/harness/idp/mock-idp.ts
// Purpose:   A local stand-in for the engine's OIDC endpoints and each IdP's login
//            page, so the harness mechanics can be proven with no deployment
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// For the oidc-live self-test only. It proves what the harness does -- the
// redirect=false login, login_hint, the session cookie surviving into the
// callback, callback parsing, the role and refusal checks, the authorize-error
// classification -- and nothing about a real IdP: the pages here carry the
// selectors the modules use, so a vendor changing its markup is invisible here.

import { randomBytes } from 'node:crypto';
import * as http from 'node:http';
import { type AddressInfo } from 'node:net';

import { type IdpKind } from './types';

export interface MockUser {
  readonly password: string;
  readonly roles: readonly string[];
}

export interface MockProvider {
  readonly kind: IdpKind;
  // Answer every authorize request with this IdP's measured refusal shape.
  readonly refuse?: boolean;
}

export interface MockConfig {
  readonly providers: Readonly<Record<string, MockProvider>>;
  // Keyed by sign-in identifier.
  readonly users: Readonly<Record<string, MockUser>>;
}

export interface MockOidc {
  readonly url: string;
  close(): Promise<void>;
}

// The cookie a seeded Google session carries; without it the mock asks for a password.
export const GOOGLE_SESSION_COOKIE = 'mock_google_session';
const ENGINE_SESSION_COOKIE = 'mock_engine_session';

function esc(value: string): string {
  return value.replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`);
}

function page(body: string): string {
  return `<!doctype html><html><head><title>mock</title></head><body>${body}</body></html>`;
}

function hidden(fields: Record<string, string>): string {
  return Object.entries(fields)
    .map(([name, value]) => `<input type="hidden" name="${esc(name)}" value="${esc(value)}">`)
    .join('');
}

function cookies(req: http.IncomingMessage): Map<string, string> {
  const jar = new Map<string, string>();
  for (const part of (req.headers.cookie ?? '').split(';')) {
    const [name, ...rest] = part.trim().split('=');
    if (name) jar.set(name, rest.join('='));
  }
  return jar;
}

async function readForm(req: http.IncomingMessage): Promise<URLSearchParams> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  return new URLSearchParams(Buffer.concat(chunks).toString('utf-8'));
}

export async function startMockOidc(config: MockConfig): Promise<MockOidc> {
  const states = new Set<string>();
  const codes = new Map<string, string>();
  let base = '';

  const send = (
    res: http.ServerResponse,
    status: number,
    body: string,
    headers: Record<string, string> = {},
  ): void => {
    const type = body.startsWith('{') || body.startsWith('[') ? 'application/json' : 'text/html';
    res.writeHead(status, { 'content-type': type, ...headers });
    res.end(body);
  };

  // A valid login hands back to the engine callback with a one-time code.
  const finish = (res: http.ServerResponse, form: URLSearchParams, login: string): void => {
    const code = randomBytes(8).toString('hex');
    codes.set(code, login);
    const target = new URL(form.get('redirect_uri') ?? '');
    target.searchParams.set('code', code);
    target.searchParams.set('state', form.get('state') ?? '');
    send(res, 302, '', { location: target.toString() });
  };

  const checkPassword = (login: string, passwords: string[]): boolean => {
    const user = config.users[login];
    return user !== undefined && passwords.some((value) => value === user.password);
  };

  const idpPage = (
    res: http.ServerResponse,
    req: http.IncomingMessage,
    name: string,
    kind: IdpKind,
    q: URLSearchParams,
  ): void => {
    const carry = hidden({ state: q.get('state') ?? '', redirect_uri: q.get('redirect_uri') ?? '' });
    const hint = q.get('login_hint') ?? '';
    const action = (step: string): string => `/idp/${name}/${step}`;
    switch (kind) {
      case 'dex':
        send(res, 200, page(`<form method="post" action="${action('login')}">${carry}
          <input name="login" id="login"><input name="password" id="password" type="password">
          <button id="submit-login" type="submit">Login</button></form>`));
        return;
      case 'okta':
        send(res, 200, page(`<form method="get" action="${action('okta-password')}">${carry}
          <input name="identifier" value="${esc(hint)}"><input type="submit" value="Next"></form>`));
        return;
      case 'entra':
        if (hint !== '') {
          entraPassword(res, name, q, hint);
          return;
        }
        send(res, 200, page(`<form method="get" action="${action('entra-password')}">${carry}
          <input name="loginfmt"><button id="idSIButton9" type="submit">Next</button></form>`));
        return;
      case 'google': {
        if (!cookies(req).has(GOOGLE_SESSION_COOKIE)) {
          send(res, 200, page('<input name="Passwd" type="password">'));
          return;
        }
        const next = new URL(`${base}${action('google-consent')}`);
        q.forEach((value, key) => next.searchParams.set(key, value));
        send(res, 200, page(`<a href="${esc(next.toString())}" data-identifier="${esc(hint)}">
          ${esc(hint)}</a>`));
        return;
      }
    }
  };

  const entraPassword = (
    res: http.ServerResponse,
    name: string,
    q: URLSearchParams,
    login: string,
  ): void => {
    const carry = hidden({
      state: q.get('state') ?? '',
      redirect_uri: q.get('redirect_uri') ?? '',
      loginfmt: login,
    });
    // Entra keeps a hidden password input beside the visible one.
    send(res, 200, page(`<form method="post" action="/idp/${name}/entra-kmsi">${carry}
      <input type="hidden" name="passwd" value=""><input name="passwd" type="password">
      <button id="idSIButton9" type="submit">Sign in</button></form>`));
  };

  const server = http.createServer((req, res) => {
    void (async () => {
      const url = new URL(req.url ?? '/', base);
      const parts = url.pathname.split('/').filter(Boolean);
      const q = url.searchParams;

      if (url.pathname.startsWith('/api/v1/auth/oidc/') && parts.length === 6) {
        const name = parts[4] ?? '';
        if (config.providers[name] === undefined) {
          send(res, 404, '{"detail":"not found"}');
          return;
        }
        if (parts[5] === 'login') {
          const state = randomBytes(8).toString('hex');
          states.add(state);
          const authorize = new URL(`${base}/idp/${name}/authorize`);
          authorize.searchParams.set('response_type', 'code');
          authorize.searchParams.set('client_id', 'mock-client');
          authorize.searchParams.set('redirect_uri', `${base}/api/v1/auth/oidc/${name}/callback`);
          authorize.searchParams.set('state', state);
          send(res, 200, JSON.stringify({ authorization_url: authorize.toString() }), {
            'set-cookie': `${ENGINE_SESSION_COOKIE}=${state}; Path=/; HttpOnly; SameSite=Lax`,
          });
          return;
        }
        if (parts[5] === 'callback') {
          const state = q.get('state') ?? '';
          const login = codes.get(q.get('code') ?? '');
          if (cookies(req).get(ENGINE_SESSION_COOKIE) !== state || !states.has(state)) {
            send(res, 401, '{"detail":"state does not match the session cookie"}');
            return;
          }
          if (login === undefined) {
            send(res, 401, '{"detail":"unknown code"}');
            return;
          }
          states.delete(state);
          send(res, 200, JSON.stringify({
            access_token: `mock.${login}`,
            token_type: 'bearer',
            subject: login,
            email: '',
            groups: [],
          }));
          return;
        }
      }

      if (url.pathname === '/api/v1/auth/me' || url.pathname === '/api/v1/oidc-providers') {
        const token = (req.headers.authorization ?? '').replace(/^Bearer /, '');
        const user = config.users[token.replace(/^mock\./, '')];
        if (user === undefined) {
          send(res, 401, '{"detail":"unauthorised"}');
        } else if (url.pathname === '/api/v1/auth/me') {
          send(res, 200, JSON.stringify({ roles: user.roles, org_ids: [] }));
        } else {
          send(res, user.roles.includes('admin') ? 200 : 403, '[]');
        }
        return;
      }

      if (parts[0] === 'idp' && parts.length === 3) {
        const name = parts[1] ?? '';
        const provider = config.providers[name];
        if (provider === undefined) {
          send(res, 404, page('no such provider'));
          return;
        }
        const step = parts[2];
        if (step === 'authorize') {
          if (provider.refuse) {
            if (provider.kind === 'google') {
              send(res, 302, '', { location: '/signin/oauth/error?authError=bW9jaw' });
            } else if (provider.kind === 'entra') {
              send(res, 200, page('<div>AADSTS50011: redirect URI mismatch</div>'));
            } else {
              send(res, 400, page('400 Bad Request'));
            }
            return;
          }
          idpPage(res, req, name, provider.kind, q);
          return;
        }
        if (step === 'okta-password') {
          const carry = hidden({
            state: q.get('state') ?? '',
            redirect_uri: q.get('redirect_uri') ?? '',
            identifier: q.get('identifier') ?? '',
          });
          send(res, 200, page(`<form method="post" action="/idp/${name}/login">${carry}
            <input name="credentials.passcode" type="password">
            <input type="submit" value="Verify"></form>`));
          return;
        }
        if (step === 'entra-password') {
          entraPassword(res, name, q, q.get('loginfmt') ?? '');
          return;
        }
        if (step === 'login' || step === 'entra-kmsi') {
          const form = await readForm(req);
          const login = form.get('login') ?? form.get('identifier') ?? form.get('loginfmt') ?? '';
          const passwords = [
            ...form.getAll('password'),
            ...form.getAll('credentials.passcode'),
            ...form.getAll('passwd'),
          ];
          if (!checkPassword(login, passwords)) {
            send(res, 401, page('invalid credentials'));
            return;
          }
          if (step === 'login') {
            finish(res, form, login);
            return;
          }
          const carry = hidden({
            state: form.get('state') ?? '',
            redirect_uri: form.get('redirect_uri') ?? '',
            login,
          });
          send(res, 200, page(`<form method="post" action="/idp/${name}/done">${carry}
            <input type="checkbox" id="KmsiCheckboxField">
            <button id="idSIButton9" type="submit">Yes</button></form>`));
          return;
        }
        if (step === 'google-consent') {
          const carry = hidden({
            state: q.get('state') ?? '',
            redirect_uri: q.get('redirect_uri') ?? '',
            login: q.get('login_hint') ?? '',
          });
          send(res, 200, page(`<form method="post" action="/idp/${name}/done">${carry}
            <button type="submit">Continue</button></form>`));
          return;
        }
        if (step === 'done') {
          const form = await readForm(req);
          finish(res, form, form.get('login') ?? '');
          return;
        }
      }
      send(res, 404, page('not found'));
    })().catch((error: unknown) => {
      res.writeHead(500);
      res.end(String(error));
    });
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  return {
    url: base,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}
