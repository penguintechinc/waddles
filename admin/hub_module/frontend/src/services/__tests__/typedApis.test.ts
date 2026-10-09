/**
 * Tests for the typed per-domain service clients (bundle activation /
 * availability, Discord bot install, platform communities, super-admin
 * tenants) and the shared TanStack Query client: request shape on the wire,
 * response unwrapping, and HTTP-error propagation.
 */
import { AxiosError, type InternalAxiosRequestConfig } from 'axios';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { apiClient } from '../../lib/apiClient';
import { queryClient } from '../../lib/queryClient';
import { recordRequests } from '../../test/apiContract';
import { bundleActivationApi } from '../bundleActivationApi';
import { bundleAvailabilityApi } from '../bundleAvailabilityApi';
import { discordBotInstallApi, isProviderNotConfigured } from '../discordBotInstallApi';
import { platformCommunitiesApi } from '../platformCommunitiesApi';
import { superTenantApi } from '../superTenantApi';

let rec: ReturnType<typeof recordRequests>;

beforeEach(() => {
  rec = recordRequests(apiClient);
});

afterEach(() => {
  rec.restore();
});

function sent(): InternalAxiosRequestConfig {
  const call = rec.calls[rec.calls.length - 1];
  if (!call) throw new Error('no request was recorded');
  return call;
}

function bodyOf(config: InternalAxiosRequestConfig): unknown {
  return typeof config.data === 'string' ? (JSON.parse(config.data) as unknown) : config.data;
}

/** Makes every request fail with an HTTP error (non-401, so no session refresh). */
function failWith(status: number, data: unknown): void {
  apiClient.defaults.adapter = (config) =>
    Promise.reject(
      new AxiosError('Request failed', AxiosError.ERR_BAD_RESPONSE, config, null, {
        data,
        status,
        statusText: String(status),
        headers: {},
        config,
      }),
    );
}

describe('bundleActivationApi', () => {
  it('list unwraps the bundles array from the community activation path', async () => {
    const row = { appId: 'a.b', communityId: 7, tenantId: 1, versionId: 3, activatedAt: null };
    rec.reply.data = { success: true, bundles: [row] };

    await expect(bundleActivationApi.list(7)).resolves.toEqual([row]);
    expect(sent().method).toBe('get');
    expect(sent().url).toBe('/api/v1/apps/community/7/activation');
  });

  it('activate POSTs the appId and returns the message envelope', async () => {
    rec.reply.data = { success: true, message: 'activated' };

    await expect(bundleActivationApi.activate(7, 'a.b')).resolves.toEqual({ success: true, message: 'activated' });
    expect(sent().method).toBe('post');
    expect(sent().url).toBe('/api/v1/apps/community/7/activation');
    expect(bodyOf(sent())).toEqual({ appId: 'a.b' });
  });

  it('deactivate DELETEs the per-app activation path', async () => {
    rec.reply.data = { success: true, message: 'deactivated' };

    await expect(bundleActivationApi.deactivate(7, 'a.b')).resolves.toMatchObject({ message: 'deactivated' });
    expect(sent().method).toBe('delete');
    expect(sent().url).toBe('/api/v1/apps/community/7/activation/a.b');
  });

  it('propagates a 403 to the caller', async () => {
    failWith(403, { success: false });
    await expect(bundleActivationApi.list(7)).rejects.toMatchObject({ response: { status: 403 } });
  });
});

describe('bundleAvailabilityApi', () => {
  it('list unwraps bundles for the tenant slug', async () => {
    const row = { appId: 'a.b', tenantId: 1, available: true, pinnedVersionId: null, updatedAt: null };
    rec.reply.data = { success: true, bundles: [row] };

    await expect(bundleAvailabilityApi.list('acme')).resolves.toEqual([row]);
    expect(sent().url).toBe('/api/v1/apps/tenant/acme/availability');
  });

  it('enable POSTs the body, including a pinned version', async () => {
    rec.reply.data = { success: true, message: 'enabled' };

    await bundleAvailabilityApi.enable('acme', { appId: 'a.b', pinnedVersionId: 4 });

    expect(sent().method).toBe('post');
    expect(sent().url).toBe('/api/v1/apps/tenant/acme/availability');
    expect(bodyOf(sent())).toEqual({ appId: 'a.b', pinnedVersionId: 4 });
  });

  it('disable URL-encodes the app id', async () => {
    rec.reply.data = { success: true, message: 'disabled' };

    await bundleAvailabilityApi.disable('acme', 'vendor/app one');

    expect(sent().method).toBe('delete');
    expect(sent().url).toBe('/api/v1/apps/tenant/acme/availability/vendor%2Fapp%20one');
  });

  it('surfaces the 409 error envelope when the app has no platform install', async () => {
    const body = { success: false, error: { message: 'not installed', code: 'conflict', timestamp: 't' } };
    failWith(409, body);

    await expect(bundleAvailabilityApi.enable('acme', { appId: 'x' })).rejects.toMatchObject({
      response: { status: 409, data: body },
    });
  });
});

describe('discordBotInstallApi', () => {
  it('getInstallUrl returns just the installUrl from the pre-auth route', async () => {
    rec.reply.data = { success: true, installUrl: 'https://discord.com/oauth2/authorize?x=1' };

    await expect(discordBotInstallApi.getInstallUrl()).resolves.toBe('https://discord.com/oauth2/authorize?x=1');
    expect(sent().method).toBe('get');
    expect(sent().url).toBe('/api/v1/public/discord/bot-install');
  });

  it('getInstallUrl rejects with the 503 provider_not_configured error', async () => {
    failWith(503, { error: 'provider_not_configured', provider: 'discord' });

    const err: unknown = await discordBotInstallApi.getInstallUrl().catch((e: unknown) => e);

    expect(isProviderNotConfigured(err)).toBe(true);
  });
});

describe('isProviderNotConfigured', () => {
  it.each([
    ['null', null],
    ['a string', 'boom'],
    ['an error without a response', new Error('x')],
    ['a 500 response', { response: { status: 500, data: { error: 'provider_not_configured' } } }],
    ['a 503 with a different error code', { response: { status: 503, data: { error: 'other' } } }],
    ['a 503 with no body', { response: { status: 503 } }],
  ])('is false for %s', (_label, err) => {
    expect(isProviderNotConfigured(err)).toBe(false);
  });

  it('is true for the 503 provider_not_configured body', () => {
    expect(isProviderNotConfigured({ response: { status: 503, data: { error: 'provider_not_configured' } } })).toBe(true);
  });
});

describe('platformCommunitiesApi', () => {
  it('list sends only the provided filters, stringifying isActive and dropping empty search', async () => {
    const payload = { success: true, communities: [], pagination: { page: 2, limit: 10, total: 0, totalPages: 0 } };
    rec.reply.data = payload;

    await expect(
      platformCommunitiesApi.list({ page: 2, limit: 10, search: '', isActive: false }),
    ).resolves.toEqual(payload);

    expect(sent().url).toBe('/api/v1/platform/communities');
    expect(sent().params).toEqual({ page: 2, limit: 10, search: undefined, isActive: 'false' });
  });

  it('list with no arguments sends all-undefined params (server defaults apply)', async () => {
    await platformCommunitiesApi.list();
    expect(sent().params).toEqual({ page: undefined, limit: undefined, search: undefined, isActive: undefined });
  });

  it('list forwards a non-empty search term and isActive=true', async () => {
    await platformCommunitiesApi.list({ search: 'penguin', isActive: true });
    expect(sent().params).toMatchObject({ search: 'penguin', isActive: 'true' });
  });

  it('get returns the detail envelope for one community', async () => {
    rec.reply.data = { success: true, community: { id: 5, name: 'c' } };

    await expect(platformCommunitiesApi.get(5)).resolves.toEqual({ success: true, community: { id: 5, name: 'c' } });
    expect(sent().url).toBe('/api/v1/platform/communities/5');
  });

  it('update PUTs the partial payload', async () => {
    await platformCommunitiesApi.update(5, { displayName: 'New', isPublic: false });
    expect(sent().method).toBe('put');
    expect(sent().url).toBe('/api/v1/platform/communities/5');
    expect(bodyOf(sent())).toEqual({ displayName: 'New', isPublic: false });
  });

  it('deactivate sends the reason as a DELETE body, or an empty body without one', async () => {
    await platformCommunitiesApi.deactivate(5, 'abuse');
    expect(sent().method).toBe('delete');
    expect(bodyOf(sent())).toEqual({ reason: 'abuse' });

    await platformCommunitiesApi.deactivate(5);
    expect(bodyOf(sent())).toEqual({});
  });

  it('propagates a 404', async () => {
    failWith(404, { success: false });
    await expect(platformCommunitiesApi.get(99)).rejects.toMatchObject({ response: { status: 404 } });
  });
});

describe('superTenantApi', () => {
  it('list GETs with pagination + search params and returns the raw axios response', async () => {
    rec.reply.data = { success: true, tenants: [], pagination: { page: 1, limit: 25, total: 0, totalPages: 0 } };

    const res = await superTenantApi.list({ page: 1, limit: 25, search: 'acme' });

    expect(res.data).toMatchObject({ success: true, tenants: [] });
    expect(sent().url).toBe('/api/v1/superadmin/tenants');
    expect(sent().params).toEqual({ page: 1, limit: 25, search: 'acme' });
  });

  it('create POSTs the payload', async () => {
    rec.reply.data = { success: true, tenant: { id: 9, slug: 'acme', displayName: 'Acme', createdAt: null } };

    const res = await superTenantApi.create({ slug: 'acme', displayName: 'Acme', seatLimit: 10 });

    expect(res.data.tenant.id).toBe(9);
    expect(sent().method).toBe('post');
    expect(bodyOf(sent())).toEqual({ slug: 'acme', displayName: 'Acme', seatLimit: 10 });
  });

  it('update PUTs to the tenant id', async () => {
    await superTenantApi.update(9, { isActive: false });
    expect(sent().method).toBe('put');
    expect(sent().url).toBe('/api/v1/superadmin/tenants/9');
    expect(bodyOf(sent())).toEqual({ isActive: false });
  });

  it('deactivate DELETEs the tenant id', async () => {
    await superTenantApi.deactivate(9);
    expect(sent().method).toBe('delete');
    expect(sent().url).toBe('/api/v1/superadmin/tenants/9');
  });

  it('propagates a 403 for non-super-admins', async () => {
    failWith(403, { success: false });
    await expect(superTenantApi.deactivate(9)).rejects.toMatchObject({ response: { status: 403 } });
  });
});

describe('queryClient', () => {
  it('caches for 5 minutes, retries queries once, never retries mutations, no focus refetch', () => {
    const defaults = queryClient.getDefaultOptions();
    expect(defaults.queries).toMatchObject({ staleTime: 5 * 60 * 1000, retry: 1, refetchOnWindowFocus: false });
    expect(defaults.mutations).toMatchObject({ retry: 0 });
  });
});
