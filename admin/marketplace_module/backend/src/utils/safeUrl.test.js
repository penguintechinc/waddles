import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';
import dns from 'node:dns';
import safeUrl, { assertSafeUrl, isBlockedIp, parseHttpsUrl } from './safeUrl.js';

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
});
