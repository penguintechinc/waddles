/** DSAR API clients: public wrapper never sends credentials; error code extraction; list param hygiene. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../lib/apiClient', () => ({ apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn() } }));
import { apiClient } from '../../lib/apiClient';
import { dsarErrorCode, dsarErrorMessage, dsarErrorStatus } from '../dsarConstants';
import { PublicApiError, publicPrivacyApi } from '../publicPrivacyApi';
import { tenantDsarInboxApi } from '../tenantDsarInboxApi';

const fetchMock = vi.fn();
beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const json = (status: number, body: unknown) => ({ ok: status < 400, status, json: () => Promise.resolve(body) });

describe('publicPrivacyApi', () => {
  it('uses fetch with credentials omitted and no auth header', async () => {
    fetchMock.mockResolvedValue(json(202, { accepted: true }));
    await publicPrivacyApi.submit('acme', { email: 'a@b.co', requestType: 1, regime: 1, captchaToken: 't' });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/v1/public/acme/privacy-requests');
    expect(init.credentials).toBe('omit');
    expect(JSON.stringify(init.headers)).not.toMatch(/authorization/i);
  });

  it('verify POSTs the token in the body, never in the URL', async () => {
    fetchMock.mockResolvedValue(json(200, { verified: true }));
    await publicPrivacyApi.verify('secret-token');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/v1/public/privacy-requests/verify');
    expect(url).not.toContain('secret-token');
    expect(init.method).toBe('POST');
    expect(init.body).toBe('{"token":"secret-token"}');
  });

  it('converts error envelopes to PublicApiError with status and code', async () => {
    fetchMock.mockResolvedValue(json(429, { error: { code: 'DSAR_RATE_LIMITED', message: 'slow' } }));
    const err = await publicPrivacyApi.getCaptchaConfig().catch((e) => e);
    expect(err).toBeInstanceOf(PublicApiError);
    expect(dsarErrorStatus(err)).toBe(429);
    expect(dsarErrorCode(err)).toBe('DSAR_RATE_LIMITED');
    expect(dsarErrorMessage(err, 'f')).toMatch(/too many/i);
  });

  it('turns a network failure into status 0', async () => {
    fetchMock.mockRejectedValue(new TypeError('offline'));
    const err = await publicPrivacyApi.getCaptchaConfig().catch((e) => e);
    expect(err.status).toBe(0);
  });
});

describe('tenantDsarInboxApi', () => {
  it('drops unset filters', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: { items: [], total: 0, limit: 25, offset: 0 } });
    await tenantDsarInboxApi.list('acme', { status: 4, assignee: '', limit: 25, offset: 0 });
    expect(apiClient.get).toHaveBeenCalledWith('/api/v1/tenant/acme/privacy/requests', { params: { status: 4, limit: 25, offset: 0 } });
  });

  it('act posts the action and confirm flag', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: { request: { id: 'x' }, result: { userId: '123e4567-e89b-12d3-a456-426614174000', status: 'completed' } } });
    const r = await tenantDsarInboxApi.act('acme', 'rid', 'erase', true);
    expect(apiClient.post).toHaveBeenCalledWith('/api/v1/tenant/acme/privacy/requests/rid/act', { action: 'erase', confirm: true });
    expect(r.result.status).toBe('completed');
  });
});
