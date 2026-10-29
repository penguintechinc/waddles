import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';
import crypto from 'node:crypto';

const query = jest.fn();
jest.unstable_mockModule('../../config/database.js', () => ({ query, default: { query } }));

const svc = await import('../vendorExecutionService.js');

const base = { id: 3, webhook_url: 'http://vendor/hook', webhook_secret: 'shh', webhook_timeout_ms: 1000, communication_model: 'webhook_push' };

describe('vendorExecutionService', () => {
  const realFetch = globalThis.fetch;
  let fetchMock;

  beforeEach(() => {
    query.mockReset();
    fetchMock = jest.fn();
    globalThis.fetch = fetchMock;
  });
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  it('getModuleConfig returns the approved module or null', async () => {
    query.mockResolvedValueOnce({ rows: [base] });
    expect(await svc.getModuleConfig(3)).toBe(base);
    query.mockResolvedValueOnce({ rows: [] });
    expect(await svc.getModuleConfig(3)).toBeNull();
  });

  it('refuses to execute unknown/unapproved modules', async () => {
    query.mockResolvedValueOnce({ rows: [] });
    await expect(svc.executeCommand(3, {})).rejects.toMatchObject({ statusCode: 404 });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('webhook_push signs the body with HMAC-SHA256 and returns the vendor response', async () => {
    query.mockResolvedValueOnce({ rows: [base] }).mockResolvedValue({ rows: [] });
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ reply: 'hi' }) });
    const payload = { command: '!x' };
    expect(await svc.executeCommand(3, payload)).toEqual({ reply: 'hi' });
    const [url, init] = fetchMock.mock.calls[0];
    const expectedSig = crypto.createHmac('sha256', 'shh').update(JSON.stringify(payload)).digest('hex');
    expect(url).toBe('http://vendor/hook');
    expect(init.headers['X-WaddleBot-Signature']).toBe(`sha256=${expectedSig}`);
    expect(init.headers['X-WaddleBot-Module-Id']).toBe('3');
  });

  it('treats a missing communication_model as webhook_push with a default timeout', async () => {
    query.mockResolvedValueOnce({ rows: [{ ...base, communication_model: null, webhook_timeout_ms: null }] }).mockResolvedValue({ rows: [] });
    fetchMock.mockResolvedValue({ ok: true, json: async () => 1 });
    expect(await svc.executeCommand(3, {})).toBe(1);
  });

  it('surfaces vendor HTTP failures', async () => {
    query.mockResolvedValueOnce({ rows: [base] });
    fetchMock.mockResolvedValue({ ok: false, status: 502 });
    await expect(svc.executeCommand(3, {})).rejects.toThrow('Vendor module returned 502');
  });

  describe('rest_pull auth', () => {
    const rest = { ...base, communication_model: 'rest_pull', api_base_url: 'http://api' };

    it.each([
      ['api_key', { api_key: 'K' }, 'Bearer K'],
      ['oauth2_client_credentials', { access_token: 'T' }, 'Bearer T'],
    ])('%s sends a bearer token', async (auth_type, auth_config, header) => {
      query.mockResolvedValueOnce({ rows: [{ ...rest, auth_type, auth_config }] }).mockResolvedValue({ rows: [] });
      fetchMock.mockResolvedValue({ json: async () => ({ ok: 1 }) });
      expect(await svc.executeCommand(3, { a: 1 })).toEqual({ ok: 1 });
      expect(fetchMock.mock.calls[0][0]).toBe('http://api/execute');
      expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe(header);
    });

    it('falls back to an HMAC signature header', async () => {
      query.mockResolvedValueOnce({ rows: [{ ...rest, auth_type: 'hmac' }] }).mockResolvedValue({ rows: [] });
      fetchMock.mockResolvedValue({ json: async () => ({}) });
      await svc.executeCommand(3, { a: 1 });
      expect(fetchMock.mock.calls[0][1].headers['X-WaddleBot-Signature']).toMatch(/^sha256=[0-9a-f]{64}$/);
    });
  });

  it('returns undefined data for unknown communication models', async () => {
    query.mockResolvedValueOnce({ rows: [{ ...base, communication_model: 'other' }] }).mockResolvedValue({ rows: [] });
    expect(await svc.executeCommand(3, {})).toBeUndefined();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('request counter failures never break execution', async () => {
    query.mockResolvedValueOnce({ rows: [base] }).mockRejectedValueOnce(new Error('db'));
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({}) });
    await expect(svc.executeCommand(3, {})).resolves.toEqual({});
    query.mockRejectedValueOnce(new Error('db'));
    expect(() => svc.incrementRequestCount(3)).not.toThrow();
  });
});
