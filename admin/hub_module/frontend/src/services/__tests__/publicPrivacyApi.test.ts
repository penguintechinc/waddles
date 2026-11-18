/** publicPrivacyApi: token-in-body preview, resend carries requestType, no credentials. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { publicPrivacyApi } from '../publicPrivacyApi';

const fetchMock = vi.fn();
const json = (status: number, body: unknown) =>
  ({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) }) as Response;

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe('publicPrivacyApi review hardening', () => {
  it('previewVerification POSTs the token in the body, never in the URL', async () => {
    fetchMock.mockResolvedValue(json(200, { valid: true, requestType: 2, regime: 1, message: null, contentChanged: false, expiresAt: null }));
    const out = await publicPrivacyApi.previewVerification('secret-token');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/v1/public/privacy-requests/verify/preview');
    expect(url).not.toContain('secret-token');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('omit');
    expect(init.body).toBe('{"token":"secret-token"}');
    expect(out.requestType).toBe(2);
  });

  it('resendVerification sends the requestType so only the matching request is rotated', async () => {
    fetchMock.mockResolvedValue(json(202, { accepted: true }));
    await publicPrivacyApi.resendVerification('acme', { email: 'a@b.co', requestType: 2, captchaToken: 't' });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/v1/public/acme/privacy-requests/resend-verification');
    expect(JSON.parse(init.body as string)).toEqual({ email: 'a@b.co', requestType: 2, captchaToken: 't' });
  });
});
