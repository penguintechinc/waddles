/* global Response, AbortSignal, ReadableStream, TextEncoder */
import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';
import crypto from 'node:crypto';
import dns from 'node:dns';

const query = jest.fn();
jest.unstable_mockModule('../../config/database.js', () => ({ query, default: { query } }));

const svc = await import('../vendorExecutionService.js');

const base = { id: 3, webhook_url: 'https://vendor.example.com/hook', webhook_secret: 'shh', webhook_timeout_ms: 1000, communication_model: 'webhook_push' };

/** Builds a real Response so the streamed, size-capped body reader is exercised. */
const json = (obj, init = {}) => new Response(JSON.stringify(obj), { status: 200, ...init });

describe('vendorExecutionService', () => {
  const realFetch = globalThis.fetch;
  let fetchMock;
  let lookup;

  beforeEach(() => {
    query.mockReset();
    fetchMock = jest.fn();
    globalThis.fetch = fetchMock;
    lookup = jest.spyOn(dns.promises, 'lookup').mockResolvedValue([{ address: '93.184.216.34', family: 4 }]);
  });
  afterEach(() => {
    globalThis.fetch = realFetch;
    lookup.mockRestore();
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

  it('webhook_push signs the body with HMAC-SHA256, refuses redirects, and returns the vendor response', async () => {
    query.mockResolvedValueOnce({ rows: [base] }).mockResolvedValue({ rows: [] });
    fetchMock.mockResolvedValue(json({ reply: 'hi' }));
    const payload = { command: '!x' };
    expect(await svc.executeCommand(3, payload)).toEqual({ reply: 'hi' });
    const [url, init] = fetchMock.mock.calls[0];
    const expectedSig = crypto.createHmac('sha256', 'shh').update(JSON.stringify(payload)).digest('hex');
    expect(url).toBe('https://vendor.example.com/hook');
    expect(init.headers['X-WaddleBot-Signature']).toBe(`sha256=${expectedSig}`);
    expect(init.headers['X-WaddleBot-Module-Id']).toBe('3');
    expect(init.redirect).toBe('manual');
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it('treats a missing communication_model as webhook_push with a default timeout', async () => {
    query.mockResolvedValueOnce({ rows: [{ ...base, communication_model: null, webhook_timeout_ms: null }] }).mockResolvedValue({ rows: [] });
    fetchMock.mockResolvedValue(json({ a: 1 }));
    expect(await svc.executeCommand(3, {})).toEqual({ a: 1 });
  });

  it('surfaces vendor HTTP failures without echoing the vendor body', async () => {
    query.mockResolvedValueOnce({ rows: [base] });
    fetchMock.mockResolvedValue(new Response('secret-internal-detail', { status: 502 }));
    await expect(svc.executeCommand(3, {})).rejects.toThrow('Vendor module returned 502');
  });

  describe('SSRF protection at fetch time', () => {
    const run = (mod) => {
      query.mockResolvedValueOnce({ rows: [{ ...base, ...mod }] });
      return svc.executeCommand(3, {});
    };

    it('rejects non-https URLs', async () => {
      await expect(run({ webhook_url: 'http://vendor.example.com/hook' })).rejects.toMatchObject({ statusCode: 400 });
      expect(fetchMock).not.toHaveBeenCalled();
    });

    it.each(['https://127.0.0.1/x', 'https://169.254.169.254/latest/meta-data', 'https://10.0.0.5/x', 'https://[::1]/x', 'https://[fd00::1]/x'])(
      'rejects literal private/metadata address %s',
      async (u) => {
        await expect(run({ webhook_url: u })).rejects.toMatchObject({ statusCode: 400 });
        expect(fetchMock).not.toHaveBeenCalled();
      }
    );

    it('rejects a hostname that resolves private (DNS rebinding re-check at fetch time)', async () => {
      lookup.mockResolvedValue([{ address: '169.254.169.254', family: 4 }]);
      await expect(run({})).rejects.toMatchObject({ statusCode: 400 });
      expect(fetchMock).not.toHaveBeenCalled();
    });

    it('rejects when any one of several resolved addresses is private', async () => {
      lookup.mockResolvedValue([{ address: '93.184.216.34', family: 4 }, { address: '192.168.1.1', family: 4 }]);
      await expect(run({})).rejects.toMatchObject({ statusCode: 400 });
    });

    it('applies the same guard to rest_pull api_base_url', async () => {
      await expect(run({ communication_model: 'rest_pull', api_base_url: 'https://192.168.0.9', auth_type: 'hmac' })).rejects.toMatchObject({ statusCode: 400 });
    });

    it('rejects redirect responses', async () => {
      fetchMock.mockResolvedValue(new Response(null, { status: 302, headers: { location: 'http://169.254.169.254/' } }));
      await expect(run({})).rejects.toThrow('redirect');
    });
  });

  describe('timeout and size bounds', () => {
    it('clampTimeout caps at 10s and defaults bad values', () => {
      expect(svc.clampTimeout(999999)).toBe(10000);
      expect(svc.clampTimeout(2500)).toBe(2500);
      expect(svc.clampTimeout(null)).toBe(5000);
      expect(svc.clampTimeout(-4)).toBe(5000);
      expect(svc.clampTimeout('abc')).toBe(5000);
    });

    it('passes a clamped AbortSignal timeout to fetch', async () => {
      const spy = jest.spyOn(AbortSignal, 'timeout');
      query.mockResolvedValueOnce({ rows: [{ ...base, webhook_timeout_ms: 600000 }] }).mockResolvedValue({ rows: [] });
      fetchMock.mockResolvedValue(json({}));
      await svc.executeCommand(3, {});
      expect(spy).toHaveBeenCalledWith(10000);
      spy.mockRestore();
    });

    it('keeps the timeout active through the body read (slow-drip body is aborted)', async () => {
      query.mockResolvedValueOnce({ rows: [{ ...base, webhook_timeout_ms: 50 }] });
      fetchMock.mockImplementation(async (_u, init) => {
        const body = new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode('{"a":'));
            init.signal.addEventListener('abort', () => controller.error(init.signal.reason));
          },
        });
        return new Response(body, { status: 200 });
      });
      await expect(svc.executeCommand(3, {})).rejects.toBeDefined();
    });

    it('rejects an oversized body (streamed) and an oversized content-length', async () => {
      query.mockResolvedValueOnce({ rows: [base] });
      fetchMock.mockResolvedValue(new Response('x'.repeat(svc.MAX_RESPONSE_BYTES + 10), { status: 200 }));
      await expect(svc.executeCommand(3, {})).rejects.toThrow('too large');

      query.mockResolvedValueOnce({ rows: [base] });
      fetchMock.mockResolvedValue(new Response('{}', { status: 200, headers: { 'content-length': String(svc.MAX_RESPONSE_BYTES + 1) } }));
      await expect(svc.executeCommand(3, {})).rejects.toThrow('too large');
    });

    it('rejects responses with no readable body, invalid JSON, or non-object JSON', async () => {
      query.mockResolvedValue({ rows: [base] });
      fetchMock.mockResolvedValueOnce({ ok: true, status: 200 });
      await expect(svc.executeCommand(3, {})).rejects.toThrow('no readable body');
      fetchMock.mockResolvedValueOnce(new Response('not json', { status: 200 }));
      await expect(svc.executeCommand(3, {})).rejects.toThrow('invalid JSON');
      fetchMock.mockResolvedValueOnce(json([1, 2]));
      await expect(svc.executeCommand(3, {})).rejects.toThrow('invalid response');
      fetchMock.mockResolvedValueOnce(new Response('null', { status: 200 }));
      await expect(svc.executeCommand(3, {})).rejects.toThrow('invalid response');
    });

    it('strips prototype-pollution keys from vendor data', async () => {
      query.mockResolvedValueOnce({ rows: [base] }).mockResolvedValue({ rows: [] });
      fetchMock.mockResolvedValue(new Response('{"__proto__":{"x":1},"constructor":1,"ok":true}', { status: 200 }));
      const out = await svc.executeCommand(3, {});
      expect(out).toEqual({ ok: true });
      expect({}.x).toBeUndefined();
    });
  });

  describe('rest_pull auth', () => {
    const rest = { ...base, communication_model: 'rest_pull', api_base_url: 'https://api.example.com' };

    it.each([
      ['api_key', { api_key: 'K' }, 'Bearer K'],
      ['oauth2_client_credentials', { access_token: 'T' }, 'Bearer T'],
    ])('%s sends a bearer token', async (auth_type, auth_config, header) => {
      query.mockResolvedValueOnce({ rows: [{ ...rest, auth_type, auth_config }] }).mockResolvedValue({ rows: [] });
      fetchMock.mockResolvedValue(json({ ok: 1 }));
      expect(await svc.executeCommand(3, { a: 1 })).toEqual({ ok: 1 });
      expect(fetchMock.mock.calls[0][0]).toBe('https://api.example.com/execute');
      expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe(header);
      expect(fetchMock.mock.calls[0][1].redirect).toBe('manual');
    });

    it('falls back to an HMAC signature header', async () => {
      query.mockResolvedValueOnce({ rows: [{ ...rest, auth_type: 'hmac' }] }).mockResolvedValue({ rows: [] });
      fetchMock.mockResolvedValue(json({}));
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
    fetchMock.mockResolvedValue(json({}));
    await expect(svc.executeCommand(3, {})).resolves.toEqual({});
    query.mockRejectedValueOnce(new Error('db'));
    expect(() => svc.incrementRequestCount(3)).not.toThrow();
  });
});
