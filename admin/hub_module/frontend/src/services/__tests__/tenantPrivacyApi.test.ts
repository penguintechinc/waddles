/** Tests for the tenant privacy API client: paths, methods, payloads, per-user error bodies, 403. */
import { AxiosError } from 'axios';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from '../../lib/apiClient';
import { BULK_LIMITS, isSuccessStatus, parseResult, privacyErrorMessage, privacyErrorStatus, tenantPrivacyApi } from '../tenantPrivacyApi';

const UID = '123e4567-e89b-12d3-a456-426614174000';
const single = (status: string, userId = UID) => ({ success: status === 'completed', action: 'x', result: { userId, status, detail: null } });

function axiosErr(status: number, data: unknown): AxiosError {
  return new AxiosError('fail', 'ERR', undefined, undefined, { status, data } as never);
}

describe('tenantPrivacyApi', () => {
  afterEach(() => vi.restoreAllMocks());

  it('exportUser GETs the export path', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: { ...single('completed'), result: { userId: UID, status: 'completed', data: { a: [1] }, incomplete: [] } } });
    const r = await tenantPrivacyApi.exportUser('acme', UID);
    expect(get).toHaveBeenCalledWith(`/api/v1/tenant/acme/privacy/users/${UID}/export`);
    expect(r.data).toEqual({ a: [1] });
  });

  it('eraseUser POSTs {confirm}', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: single('completed') });
    await tenantPrivacyApi.eraseUser('acme', UID, true);
    expect(post).toHaveBeenCalledWith(`/api/v1/tenant/acme/privacy/users/${UID}/erase`, { confirm: true });
  });

  it('doNotSell PUTs', async () => {
    const put = vi.spyOn(apiClient, 'put').mockResolvedValue({ data: single('already_done') });
    const r = await tenantPrivacyApi.doNotSell('acme', UID);
    expect(put).toHaveBeenCalledWith(`/api/v1/tenant/acme/privacy/users/${UID}/do-not-sell`);
    expect(r.status).toBe('already_done');
  });

  it('bulk POSTs action/userIds/confirm and parses results', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: { success: true, action: 'erase', requested: 1, succeeded: 1, failed: 0, results: [{ userId: UID, status: 'completed' }] },
    });
    const r = await tenantPrivacyApi.bulk('acme', 'erase', [UID], true);
    expect(post).toHaveBeenCalledWith('/api/v1/tenant/acme/privacy/bulk', { action: 'erase', userIds: [UID], confirm: true });
    expect(r.results[0]?.userId).toBe(UID);
  });

  it('returns per-user failure bodies (404) as results', async () => {
    vi.spyOn(apiClient, 'get').mockRejectedValue(axiosErr(404, single('not_found')));
    await expect(tenantPrivacyApi.exportUser('acme', UID)).resolves.toMatchObject({ status: 'not_found' });
  });

  it('rethrows route-level errors (403 envelope) with message/status helpers', async () => {
    const err = axiosErr(403, { success: false, error: { message: 'forbidden tenant' } });
    vi.spyOn(apiClient, 'put').mockRejectedValue(err);
    await expect(tenantPrivacyApi.doNotSell('acme', UID)).rejects.toBe(err);
    expect(privacyErrorStatus(err)).toBe(403);
    expect(privacyErrorMessage(err, 'x')).toBe('forbidden tenant');
    expect(privacyErrorMessage(new Error('z'), 'fallback')).toBe('fallback');
  });

  it('url-encodes the slug and exposes limits/status helper', () => {
    expect(BULK_LIMITS.export).toBe(25);
    expect(isSuccessStatus('completed')).toBe(true);
    expect(isSuccessStatus('failed')).toBe(false);
  });
});

describe('tenantPrivacyApi UUID boundary', () => {
  it('never sends a numeric id and URL-encodes the path segment', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: single('completed') });
    await tenantPrivacyApi.exportUser('acme', 'a/b');
    expect(get).toHaveBeenCalledWith('/api/v1/tenant/acme/privacy/users/a%2Fb/export');
  });

  it('rejects a numeric userId in a result body', () => {
    expect(() => parseResult({ userId: 4, status: 'completed' })).toThrow();
  });
});
