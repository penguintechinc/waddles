import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';
import dns from 'node:dns';

const query = jest.fn();
jest.unstable_mockModule('../../config/database.js', () => ({ query, transaction: jest.fn(), default: { query } }));

const svc = await import('../vendorService.js');

const dup = Object.assign(new Error('dup'), { code: '23505' });

describe('vendorService', () => {
  let lookup;
  beforeEach(() => {
    query.mockReset();
    lookup = jest.spyOn(dns.promises, 'lookup').mockResolvedValue([{ address: '93.184.216.34', family: 4 }]);
  });
  afterEach(() => lookup.mockRestore());

  it('getVendorProfile returns the profile or null', async () => {
    query.mockResolvedValueOnce({ rows: [{ id: 1 }] });
    expect(await svc.getVendorProfile(9)).toEqual({ id: 1 });
    query.mockResolvedValueOnce({ rows: [] });
    expect(await svc.getVendorProfile(9)).toBeNull();
  });

  it('createVendorProfile inserts, maps duplicates to 409 and rethrows others', async () => {
    query.mockResolvedValueOnce({ rows: [{ id: 1 }] });
    expect(await svc.createVendorProfile(9, { displayName: 'd', description: 'x', websiteUrl: 'u', payoutMethod: 'p' })).toEqual({ id: 1 });
    query.mockRejectedValueOnce(dup);
    await expect(svc.createVendorProfile(9, {})).rejects.toMatchObject({ statusCode: 409 });
    query.mockRejectedValueOnce(new Error('db'));
    await expect(svc.createVendorProfile(9, {})).rejects.toThrow('db');
  });

  it('getVendorModules paginates, defaulting arguments', async () => {
    query.mockResolvedValueOnce({ rows: [{ id: 1 }] });
    expect(await svc.getVendorModules(9)).toEqual({ modules: [{ id: 1 }], pagination: { page: 1, limit: 25, total: 1 } });
    query.mockResolvedValueOnce({ rows: [] });
    await svc.getVendorModules(9, { page: 3, limit: 10 });
    expect(query.mock.calls[1][1]).toEqual([9, 10, 20]);
  });

  describe('getVendorDashboard', () => {
    it('404s without a vendor profile', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.getVendorDashboard(9)).rejects.toMatchObject({ statusCode: 404 });
    });
    it('aggregates stats, submissions and revenue', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 5 }] })
        .mockResolvedValueOnce({ rows: [{ totalModules: 2 }] })
        .mockResolvedValueOnce({ rows: [{ id: 1 }] })
        .mockResolvedValueOnce({ rows: [{ moduleId: 1 }] });
      expect(await svc.getVendorDashboard(9)).toEqual({ stats: { totalModules: 2 }, recentSubmissions: [{ id: 1 }], revenueBreakdown: [{ moduleId: 1 }] });
    });
  });

  describe('updateVendorProfile', () => {
    it('passes nulls for omitted fields so COALESCE keeps existing values', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] });
      await svc.updateVendorProfile(9, { displayName: 'd' });
      expect(query.mock.calls[0][1]).toEqual([9, 'd', null, null, null]);
    });
    it('404s when no profile exists', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.updateVendorProfile(9, {})).rejects.toMatchObject({ statusCode: 404 });
    });
  });

  describe('getVendorAnalyticsOverview', () => {
    it('404s without a profile', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.getVendorAnalyticsOverview(9)).rejects.toMatchObject({ statusCode: 404 });
    });
    it('returns installs, revenue and rating (null when unrated)', async () => {
      const installs = { rows: [{ totalInstalls: 1, totalUninstalls: 2, installsThisMonth: 3, revenueThisMonth: 4 }] };
      query.mockResolvedValueOnce({ rows: [{ id: 5 }] }).mockResolvedValueOnce(installs).mockResolvedValueOnce({ rows: [{ avgRating: '4.5' }] });
      expect(await svc.getVendorAnalyticsOverview(9)).toEqual({ totalInstalls: 1, totalUninstalls: 2, installsThisMonth: 3, revenueThisMonth: 4, avgRating: '4.5' });
      query.mockResolvedValueOnce({ rows: [{ id: 5 }] }).mockResolvedValueOnce(installs).mockResolvedValueOnce({ rows: [{ avgRating: null }] });
      expect((await svc.getVendorAnalyticsOverview(9)).avgRating).toBeNull();
    });
  });

  describe('createVendorModule', () => {
    it('forbids users without a vendor profile', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.createVendorModule(9, { name: 'X' })).rejects.toMatchObject({ statusCode: 403 });
    });

    it('slugifies the name and applies defaults', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 5 }] }).mockResolvedValueOnce({ rows: [{ id: 1, slug: 'my-cool-module' }] });
      expect(await svc.createVendorModule(9, { name: '  My Cool Module!! ' })).toEqual({ id: 1, slug: 'my-cool-module' });
      const params = query.mock.calls[1][1];
      expect(params[2]).toBe('my-cool-module');
      expect(params[7]).toBe(5000);
      expect(params[19]).toBe('{}');
    });

    it('retries with a user-suffixed slug on slug collision', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 5 }] }).mockRejectedValueOnce(dup).mockResolvedValueOnce({ rows: [{ id: 2, slug: 'x-9' }] });
      expect(await svc.createVendorModule(9, { name: 'x', authConfig: { a: 1 } })).toEqual({ id: 2, slug: 'x-9' });
      expect(query.mock.calls[2][1][2]).toBe('x-9');
      expect(query.mock.calls[2][1][19]).toBe('{"a":1}');
    });

    it('rethrows other insert failures', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 5 }] }).mockRejectedValueOnce(new Error('db'));
      await expect(svc.createVendorModule(9, { name: 'x' })).rejects.toThrow('db');
    });
  });

  describe('updateVendorModule', () => {
    it('404s unless the vendor owns the module', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.updateVendorModule(9, 1, { name: 'x' })).rejects.toMatchObject({ statusCode: 404 });
      expect(query).toHaveBeenCalledTimes(1);
    });
    it('is a no-op when no allowed fields are supplied', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] });
      expect(await svc.updateVendorModule(9, 1, { status: 'approved', seller_id: 99 })).toEqual({ success: true });
      expect(query).toHaveBeenCalledTimes(1);
    });
    it('writes only allow-listed columns (status cannot be self-approved)', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] }).mockResolvedValueOnce({ rows: [] });
      await svc.updateVendorModule(9, 1, { name: 'n', webhookUrl: 'https://vendor.example.com/h', status: 'approved' });
      const [sql, values] = query.mock.calls[1];
      expect(sql).toContain('name = $1');
      expect(sql).toContain('webhook_url = $2');
      expect(values).toEqual(['n', 'https://vendor.example.com/h', 1]);
    });
    it('does not touch status when only non-sensitive fields change', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] }).mockResolvedValueOnce({ rows: [] });
      await svc.updateVendorModule(9, 1, { name: 'n', status: 'approved' });
      expect(query.mock.calls[1][0]).not.toContain('status');
    });
    it.each([['webhookUrl', 'https://vendor.example.com/h'], ['apiBaseUrl', 'https://vendor.example.com'], ['webhookSecret', 's'], ['authType', 'hmac'], ['authConfig', {}], ['communicationModel', 'rest_pull']])(
      'changing %s on an approved module resets it to pending re-review',
      async (key, val) => {
        query.mockResolvedValueOnce({ rows: [{ id: 1 }] }).mockResolvedValueOnce({ rows: [] });
        await svc.updateVendorModule(9, 1, { [key]: val });
        expect(query.mock.calls[1][0]).toContain("status = CASE WHEN status = 'approved' THEN 'pending' ELSE status END");
      }
    );
    it('rejects unsafe URLs on update before any write', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] });
      await expect(svc.updateVendorModule(9, 1, { webhookUrl: 'http://vendor.example.com' })).rejects.toMatchObject({ statusCode: 400 });
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] });
      await expect(svc.updateVendorModule(9, 1, { apiBaseUrl: 'https://169.254.169.254' })).rejects.toMatchObject({ statusCode: 400 });
      lookup.mockResolvedValue([{ address: '10.0.0.1', family: 4 }]);
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] });
      await expect(svc.updateVendorModule(9, 1, { webhookUrl: 'https://rebind.example.com' })).rejects.toMatchObject({ statusCode: 400 });
      expect(query).toHaveBeenCalledTimes(3);
    });
    it('clamps webhookTimeoutMs server-side', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] }).mockResolvedValueOnce({ rows: [] });
      await svc.updateVendorModule(9, 1, { webhookTimeoutMs: 999999 });
      expect(query.mock.calls[1][1]).toEqual([10000, 1]);
    });
  });

  describe('createVendorModule SSRF + timeout hardening', () => {
    it('rejects http, private and metadata URLs before inserting', async () => {
      for (const bad of [{ webhookUrl: 'http://v.example.com' }, { webhookUrl: 'https://169.254.169.254/' }, { apiBaseUrl: 'https://10.0.0.1' }]) {
        query.mockResolvedValueOnce({ rows: [{ id: 5 }] });
        await expect(svc.createVendorModule(9, { name: 'x', ...bad })).rejects.toMatchObject({ statusCode: 400 });
      }
      expect(query.mock.calls.every(([sql]) => !sql.includes('INSERT'))).toBe(true);
    });
    it('clamps the timeout written on create', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 5 }] }).mockResolvedValueOnce({ rows: [{ id: 1, slug: 'x' }] });
      await svc.createVendorModule(9, { name: 'x', webhookUrl: 'https://v.example.com/h', webhookTimeoutMs: 600000 });
      expect(query.mock.calls[1][1][7]).toBe(10000);
    });
  });

  describe('submitModuleForReview', () => {
    it('404s unless owned', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.submitModuleForReview(9, 1, 'c')).rejects.toMatchObject({ statusCode: 404 });
    });
    it('creates a pending submission and flips module status', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 1, version: '2.0' }] }).mockResolvedValueOnce({ rows: [{ id: 33 }] }).mockResolvedValueOnce({ rows: [] });
      expect(await svc.submitModuleForReview(9, 1, 'changes')).toEqual({ submissionId: 33 });
      expect(query.mock.calls[1][1]).toEqual([1, '2.0', 'changes', 9]);
      expect(query.mock.calls[2][0]).toContain("status = 'pending'");
    });
  });

  describe('vendor role requests', () => {
    it('getVendorRequest returns the row, null when none, and null on error', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] });
      expect(await svc.getVendorRequest(9)).toEqual({ id: 1 });
      query.mockResolvedValueOnce({ rows: [] });
      expect(await svc.getVendorRequest(9)).toBeNull();
      query.mockRejectedValueOnce(new Error('no table'));
      expect(await svc.getVendorRequest(9)).toBeNull();
    });
    it('createVendorRequest always starts pending', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 4, status: 'ignored' }] });
      expect(await svc.createVendorRequest(9, { companyName: 'c' })).toEqual({ id: 4, status: 'pending' });
      expect(svc.default.createVendorRequest).toBe(svc.createVendorRequest);
    });
  });
});
