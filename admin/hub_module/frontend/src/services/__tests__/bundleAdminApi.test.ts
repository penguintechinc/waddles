/**
 * Tests for the bundle-admin gateway client: URL shapes, PUT body, and the
 * axios-error -> BundleAdminError normalisation the settings page branches on.
 */
import { AxiosError, AxiosHeaders } from 'axios';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from '../../lib/apiClient';
import { BundleAdminError, bundleAdminApi } from '../bundleAdminApi';

vi.mock('../../lib/apiClient', () => ({ apiClient: { get: vi.fn(), put: vi.fn() } }));
const client = vi.mocked(apiClient);
const BASE = '/api/v1/apps/community/7/bundles/waddles.a.b.c';

function axiosFail(status: number, data: unknown): AxiosError {
  const headers = new AxiosHeaders();
  return new AxiosError('boom', 'ERR', { headers }, null, {
    status, statusText: '', headers: {}, config: { headers }, data,
  });
}

beforeEach(() => vi.clearAllMocks());

describe('bundleAdminApi', () => {
  it('hits descriptor, config and history routes', async () => {
    client.get.mockResolvedValue({ data: { versions: [{ version: 1 }] } });
    await bundleAdminApi.descriptor(7, 'waddles.a.b.c');
    await bundleAdminApi.getConfig(7, 'waddles.a.b.c');
    expect(await bundleAdminApi.history(7, 'waddles.a.b.c')).toEqual([{ version: 1 }]);
    expect(client.get).toHaveBeenNthCalledWith(1, `${BASE}/admin`);
    expect(client.get).toHaveBeenNthCalledWith(2, `${BASE}/config`);
    expect(client.get).toHaveBeenNthCalledWith(3, `${BASE}/config/history`, { params: { limit: 20 } });
  });

  it('PUTs config with expectedVersion', async () => {
    client.put.mockResolvedValue({ data: { version: 3 } });
    expect(await bundleAdminApi.saveConfig(7, 'waddles.a.b.c', { x: 1 }, 2)).toEqual({ version: 3 });
    expect(client.put).toHaveBeenCalledWith(`${BASE}/config`, { config: { x: 1 }, expectedVersion: 2 });
  });

  it('normalises a 422 envelope into BundleAdminError with field errors', async () => {
    client.put.mockRejectedValue(
      axiosFail(422, { error: { code: 'config_invalid', message: 'bad', details: { errors: [{ path: '$.x', rule: 'type' }] } } }),
    );
    const err = await bundleAdminApi.saveConfig(7, 'waddles.a.b.c', {}, 0).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(BundleAdminError);
    expect((err as BundleAdminError).status).toBe(422);
    expect((err as BundleAdminError).code).toBe('config_invalid');
    expect((err as BundleAdminError).fieldErrors).toEqual([{ path: '$.x', rule: 'type' }]);
  });

  it('falls back to a status code when the body has no envelope', async () => {
    client.get.mockRejectedValue(axiosFail(500, null));
    const err = (await bundleAdminApi.getConfig(7, 'waddles.a.b.c').catch((e: unknown) => e)) as BundleAdminError;
    expect(err.code).toBe('ERROR_500');
    expect(err.fieldErrors).toEqual([]);
  });

  it('rethrows non-HTTP failures unchanged', async () => {
    const net = new Error('offline');
    client.get.mockRejectedValue(net);
    await expect(bundleAdminApi.descriptor(7, 'waddles.a.b.c')).rejects.toBe(net);
  });
});
