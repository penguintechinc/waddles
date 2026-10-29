import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';
import dns from 'node:dns';
import { fetch as undiciFetch } from 'undici';
import safeUrl, { assertSafeUrl, guardedLookup, isBlockedIp, parseHttpsUrl } from './safeUrl.js';

describe('safeUrl', () => {
  let lookup;
  beforeEach(() => {
    lookup = jest.spyOn(dns.promises, 'lookup').mockResolvedValue([{ address: '93.184.216.34', family: 4 }]);
  });
  afterEach(() => lookup.mockRestore());

  it.each([
    '0.0.0.0', '127.0.0.1', '10.1.2.3', '172.16.0.1', '172.31.255.255', '192.168.1.1', '169.254.169.254',
    '100.64.0.1', '100.127.0.1', '192.0.0.1', '198.18.0.1', '224.0.0.1', '255.255.255.255',
    '::', '::1', 'fd00::1', 'fc00::1', 'fe80::1', 'ff02::1', '::ffff:127.0.0.1', '::ffff:7f00:1', '::ffff:a9fe:a9fe', 'not-an-ip',
    '64:ff9b::7f00:1', '64:ff9b::a00:1', '64:ff9b::a9fe:a9fe', '2002:7f00:1::1', '2002:a9fe:a9fe::1', '2001:0:4136:e378:8000:63bf:3fff:fdd2',
    '::10.0.0.1', '::7f00:1', '100::1', '::ffff:10.0.0.1', '::ffff:0:10.0.0.1', '2001:db8::1',
  ])('blocks %s', (ip) => expect(isBlockedIp(ip)).toBe(true));

  it.each(['93.184.216.34', '8.8.8.8', '172.32.0.1', '100.128.0.1', '2606:2800:220:1:248:1893:25c8:1946', '::ffff:5db8:d822'])(
    'allows public %s',
    (ip) => expect(isBlockedIp(ip)).toBe(false)
  );

  it('parseHttpsUrl enforces scheme, validity and no credentials', () => {
    expect(parseHttpsUrl('https://a.example.com/x').hostname).toBe('a.example.com');
    expect(() => parseHttpsUrl('http://a.example.com')).toThrow('https');
    expect(() => parseHttpsUrl('ftp://a.example.com')).toThrow('https');
    expect(() => parseHttpsUrl('nope')).toThrow('valid URL');
    expect(() => parseHttpsUrl('https://u:p@a.example.com')).toThrow('credentials');
  });

  it('assertSafeUrl resolves DNS and accepts public hosts', async () => {
    const out = await assertSafeUrl('https://a.example.com/x');
    expect(out.addresses).toEqual(['93.184.216.34']);
  });

  it('assertSafeUrl rejects http, private IP literals, metadata IP and rebinding DNS', async () => {
    await expect(assertSafeUrl('http://a.example.com')).rejects.toMatchObject({ statusCode: 400 });
    await expect(assertSafeUrl('https://10.0.0.1')).rejects.toMatchObject({ statusCode: 400 });
    await expect(assertSafeUrl('https://169.254.169.254/latest')).rejects.toMatchObject({ statusCode: 400 });
    await expect(assertSafeUrl('https://[::1]/')).rejects.toMatchObject({ statusCode: 400 });
    lookup.mockResolvedValue([{ address: '127.0.0.1', family: 4 }]);
    await expect(assertSafeUrl('https://rebind.example.com')).rejects.toMatchObject({ statusCode: 400 });
    lookup.mockResolvedValue([]);
    await expect(assertSafeUrl('https://empty.example.com')).rejects.toMatchObject({ statusCode: 400 });
  });

  it('assertSafeUrl rejects unresolvable hosts', async () => {
    lookup.mockRejectedValue(new Error('ENOTFOUND'));
    await expect(assertSafeUrl('https://nx.example.com')).rejects.toThrow('could not be resolved');
  });

  it('default export exposes the helpers', () => {
    expect(safeUrl.assertSafeUrl).toBe(assertSafeUrl);
  });

  describe('guardedLookup (connect-time IP pinning)', () => {
    let dnsLookup;
    afterEach(() => dnsLookup?.mockRestore());
    const run = (opts) => new Promise((resolve) => guardedLookup('h.example.com', opts, (...a) => resolve(a)));

    it('returns only validated addresses (all and single forms, optional options arg)', async () => {
      dnsLookup = jest.spyOn(dns, 'lookup').mockImplementation((_h, _o, cb) => cb(null, [{ address: '93.184.216.34', family: 4 }]));
      expect(await run({ all: true })).toEqual([null, [{ address: '93.184.216.34', family: 4 }]]);
      expect(await run({})).toEqual([null, '93.184.216.34', 4]);
      const viaFn = await new Promise((r) => guardedLookup('h.example.com', (...a) => r(a)));
      expect(viaFn).toEqual([null, '93.184.216.34', 4]);
    });

    it('DNS rebinding: a public answer then a private one is blocked on the connect-time lookup', async () => {
      const answers = ['93.184.216.34', '169.254.169.254'];
      dnsLookup = jest.spyOn(dns, 'lookup').mockImplementation((_h, _o, cb) => cb(null, [{ address: answers.shift(), family: 4 }]));
      await assertSafeUrl('https://rebind.example.com').catch(() => {}); // pre-check uses dns.promises (mocked public)
      expect((await run({ all: true }))[0]).toBeNull();
      const [err] = await run({ all: true });
      expect(err.code).toBe('ESSRFBLOCKED');
    });

    it.each(['64:ff9b::7f00:1', '2002:a9fe:a9fe::1', '2001:0:4136:e378:8000:63bf:3fff:fdd2', '::ffff:10.0.0.1'])('blocks %s at connect time', async (addr) => {
      dnsLookup = jest.spyOn(dns, 'lookup').mockImplementation((_h, _o, cb) => cb(null, [{ address: addr, family: 6 }]));
      expect((await run({ all: true }))[0].code).toBe('ESSRFBLOCKED');
    });

    it('blocks mixed public+private answers, empty answers, and propagates resolver errors', async () => {
      dnsLookup = jest.spyOn(dns, 'lookup').mockImplementation((_h, _o, cb) => cb(null, [{ address: '93.184.216.34', family: 4 }, { address: '10.0.0.1', family: 4 }]));
      expect((await run({ all: true }))[0].code).toBe('ESSRFBLOCKED');
      dnsLookup.mockImplementation((_h, _o, cb) => cb(null, []));
      expect((await run({ all: true }))[0].code).toBe('ESSRFBLOCKED');
      dnsLookup.mockImplementation((_h, _o, cb) => cb(new Error('ENOTFOUND')));
      expect((await run({ all: true }))[0].message).toBe('ENOTFOUND');
    });

    it('real undici request through pinnedAgent refuses a hostname that resolves to loopback', async () => {
      dnsLookup = jest.spyOn(dns, 'lookup').mockImplementation((_h, _o, cb) => cb(null, [{ address: '127.0.0.1', family: 4 }]));
      await expect(undiciFetch('https://rebind.example.com/', { dispatcher: safeUrl.pinnedAgent })).rejects.toThrow();
      expect(dnsLookup).toHaveBeenCalled();
    });
  });
});
