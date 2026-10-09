/**
 * Tests for the shared axios clients' session handling: cookie-credentialed
 * defaults and the 401 -> /auth/refresh -> retry interceptor, run against a
 * scripted HTTP adapter for both `services/api.js` and `lib/apiClient.ts`.
 */
import {
  AxiosError,
  type AxiosInstance,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from '../../lib/apiClient';
import { restoreAdapter } from '../../test/apiContract';
import api from '../api';

type Scripted = { status: number; data?: unknown } | 'network-error';
type Script = (call: string, config: InternalAxiosRequestConfig) => Scripted;

/** Installs a scripted adapter on `client`; returns the `METHOD url` call log. */
function script(client: AxiosInstance, decide: Script): { log: string[]; configs: InternalAxiosRequestConfig[] } {
  const log: string[] = [];
  const configs: InternalAxiosRequestConfig[] = [];
  client.defaults.adapter = (config) => {
    const call = `${(config.method ?? 'get').toUpperCase()} ${config.url ?? ''}`;
    log.push(call);
    configs.push(config);
    const outcome = decide(call, config);
    if (outcome === 'network-error') {
      return Promise.reject(new AxiosError('Network Error', AxiosError.ERR_NETWORK, config));
    }
    const response: AxiosResponse = {
      data: outcome.data ?? {},
      status: outcome.status,
      statusText: String(outcome.status),
      headers: {},
      config,
    };
    if (outcome.status >= 200 && outcome.status < 300) return Promise.resolve(response);
    return Promise.reject(
      new AxiosError(`Request failed with status code ${outcome.status}`, AxiosError.ERR_BAD_REQUEST, config, null, response),
    );
  };
  return { log, configs };
}

const CLIENTS: ReadonlyArray<readonly [string, AxiosInstance]> = [
  ['services/api.js', api],
  ['lib/apiClient.ts', apiClient],
];

describe.each(CLIENTS)('%s session handling', (_label, client) => {
  const originalAdapter = client.defaults.adapter;

  beforeEach(() => {
    vi.stubGlobal('location', { href: '/dashboard' });
  });

  afterEach(() => {
    restoreAdapter(client, originalAdapter);
    vi.unstubAllGlobals();
  });

  it('is cookie-credentialed JSON with a 30s timeout and never carries an Authorization header', () => {
    expect(client.defaults.withCredentials).toBe(true);
    expect(client.defaults.timeout).toBe(30000);
    expect(client.defaults.baseURL).toBe('');
    expect(client.defaults.headers['Content-Type']).toBe('application/json');
    expect(client.defaults.headers.common).not.toHaveProperty('Authorization');
  });

  it('passes successful responses straight through', async () => {
    const { log } = script(client, () => ({ status: 200, data: { ok: 1 } }));
    const res = await client.get('/api/v1/communities/my');
    expect(res.data).toEqual({ ok: 1 });
    expect(log).toEqual(['GET /api/v1/communities/my']);
  });

  it('refreshes the session on 401 and transparently retries the original request', async () => {
    let first = true;
    const { log, configs } = script(client, (call) => {
      if (call === 'POST /api/v1/auth/refresh') return { status: 200, data: { success: true } };
      if (first) {
        first = false;
        return { status: 401 };
      }
      return { status: 200, data: { communities: [1] } };
    });

    const res = await client.get('/api/v1/communities/my');

    expect(res.data).toEqual({ communities: [1] });
    expect(log).toEqual(['GET /api/v1/communities/my', 'POST /api/v1/auth/refresh', 'GET /api/v1/communities/my']);
    expect(Reflect.get(configs[2] ?? {}, '_retry')).toBe(true);
    expect(window.location.href).toBe('/dashboard');
  });

  it('rejects with the original 401 when the refresh endpoint reports success:false', async () => {
    const { log } = script(client, (call) =>
      call === 'POST /api/v1/auth/refresh' ? { status: 200, data: { success: false } } : { status: 401 },
    );

    await expect(client.get('/api/v1/user/profile')).rejects.toMatchObject({ response: { status: 401 } });
    expect(log).toEqual(['GET /api/v1/user/profile', 'POST /api/v1/auth/refresh']);
    expect(window.location.href).toBe('/dashboard');
  });

  it('redirects to /login and rejects with the refresh error when refresh itself fails', async () => {
    const { log } = script(client, (call) =>
      call === 'POST /api/v1/auth/refresh' ? 'network-error' : { status: 401 },
    );

    await expect(client.get('/api/v1/user/profile')).rejects.toMatchObject({ code: AxiosError.ERR_NETWORK });
    expect(log).toEqual(['GET /api/v1/user/profile', 'POST /api/v1/auth/refresh']);
    expect(window.location.href).toBe('/login');
  });

  it('does not refresh a second time when the retried request is also 401', async () => {
    const { log } = script(client, (call) =>
      call === 'POST /api/v1/auth/refresh' ? { status: 200, data: { success: true } } : { status: 401 },
    );

    await expect(client.get('/api/v1/user/profile')).rejects.toMatchObject({ response: { status: 401 } });
    expect(log).toEqual(['GET /api/v1/user/profile', 'POST /api/v1/auth/refresh', 'GET /api/v1/user/profile']);
  });

  // KNOWN DEFECT: the interceptor also wraps the /auth/refresh call itself, so a
  // 401 from the refresh endpoint (expired refresh cookie) re-enters it and
  // refreshes again without bound. `it.fails` goes red once that is fixed --
  // then turn this into a plain `it`.
  it.fails('does not recurse when the refresh endpoint itself returns 401', async () => {
    let refreshCalls = 0;
    script(client, (call) => {
      if (call === 'POST /api/v1/auth/refresh') {
        refreshCalls += 1;
        if (refreshCalls > 5) return 'network-error';
      }
      return { status: 401 };
    });

    await client.get('/api/v1/user/profile').catch(() => undefined);

    expect(refreshCalls).toBeLessThanOrEqual(1);
  });

  it.each([403, 404, 500])('rejects HTTP %i without attempting a refresh', async (status) => {
    const { log } = script(client, () => ({ status }));
    await expect(client.get('/api/v1/user/profile')).rejects.toMatchObject({ response: { status } });
    expect(log).toEqual(['GET /api/v1/user/profile']);
  });

  it('rejects a response-less network error untouched', async () => {
    const { log } = script(client, () => 'network-error');
    await expect(client.get('/api/v1/user/profile')).rejects.toMatchObject({ code: AxiosError.ERR_NETWORK });
    expect(log).toHaveLength(1);
  });
});

describe('lib/apiClient.ts', () => {
  it('rejects an error with no request config untouched (nothing to retry)', async () => {
    const original = apiClient.defaults.adapter;
    apiClient.defaults.adapter = () => Promise.reject(new Error('boom'));
    try {
      await expect(apiClient.get('/x')).rejects.toThrow('boom');
    } finally {
      restoreAdapter(apiClient, original);
    }
  });
});
