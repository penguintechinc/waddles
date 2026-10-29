import { beforeEach, describe, expect, it, jest } from '@jest/globals';

const query = jest.fn();
jest.unstable_mockModule('../../config/database.js', () => ({ query, default: { query } }));

const svc = await import('../vendorAnalyticsService.js');
const { logger } = await import('../../utils/logger.js');

const count = (n) => ({ rows: [{ count: String(n) }] });
const cents = (n) => ({ rows: [{ total_cents: String(n) }] });
const seller = { rows: [{ id: 5 }] };
const noSeller = { rows: [] };

describe('vendorAnalyticsService', () => {
  beforeEach(() => {
    query.mockReset();
    jest.spyOn(logger, 'debug').mockImplementation(() => {});
  });

  describe('getSalesMetrics', () => {
    it('returns null for users without a vendor profile', async () => {
      query.mockResolvedValueOnce(noSeller);
      expect(await svc.getSalesMetrics(9)).toBeNull();
    });

    const stub = () => {
      query.mockResolvedValueOnce(seller)
        .mockResolvedValueOnce(count(10)) // installs
        .mockResolvedValueOnce(count(2)) // uninstalls
        .mockResolvedValueOnce(count(8)) // active
        .mockResolvedValueOnce(cents(1000))
        .mockResolvedValueOnce(cents(100))
        .mockResolvedValueOnce(cents(500))
        .mockResolvedValueOnce(count(1))
        .mockResolvedValueOnce(count(3))
        .mockResolvedValueOnce(count(5))
        .mockResolvedValueOnce(count(9));
    };

    it('combines installs, churn and revenue', async () => {
      stub();
      expect(await svc.getSalesMetrics(9)).toEqual({
        period: '30d',
        installations: { total: 10, active: 8, uninstalls: 2, churnRate: 20, new: { today: 1, thisWeek: 3, mtd: 5, ytd: 9 } },
        revenue: { totalCents: 1000, mtdCents: 100, ytdCents: 500 },
      });
      expect(query.mock.calls[1][0]).toContain("INTERVAL '30 days'");
    });

    it.each([
      ['7d', "INTERVAL '7 days'"], ['90d', "INTERVAL '90 days'"], ['mtd', "'month'"], ['ytd', "'year'"],
    ])('uses the %s window', async (period, fragment) => {
      stub();
      await svc.getSalesMetrics(9, { period });
      expect(query.mock.calls[1][0]).toContain(fragment);
    });

    it('applies no date filter for "all" or unknown periods and reports zero churn with no installs', async () => {
      query.mockResolvedValueOnce(seller).mockResolvedValueOnce(count(0)).mockResolvedValueOnce(count(0)).mockResolvedValueOnce(count(0))
        .mockResolvedValueOnce(cents(0)).mockResolvedValueOnce(cents(0)).mockResolvedValueOnce(cents(0))
        .mockResolvedValueOnce(count(0)).mockResolvedValueOnce(count(0)).mockResolvedValueOnce(count(0)).mockResolvedValueOnce(count(0));
      const out = await svc.getSalesMetrics(9, { period: 'bogus' });
      expect(out.installations.churnRate).toBe(0);
      expect(query.mock.calls[1][0]).not.toContain('installed_at >=');
    });
  });

  describe('getInstallTimeSeries', () => {
    it('returns [] without a vendor profile', async () => {
      query.mockResolvedValueOnce(noSeller);
      expect(await svc.getInstallTimeSeries(9)).toEqual([]);
    });
    it('maps buckets and passes a validated granularity', async () => {
      query.mockResolvedValueOnce(seller).mockResolvedValueOnce({ rows: [{ date: 'd', installs: 1, uninstalls: 2, extra: 'x' }] });
      expect(await svc.getInstallTimeSeries(9, { period: '7d', granularity: 'week' })).toEqual([{ date: 'd', installs: 1, uninstalls: 2 }]);
      expect(query.mock.calls[1][1]).toEqual(['week', 5]);
    });
    it('falls back to day for unknown granularity (no SQL injection) and handles all-time', async () => {
      query.mockResolvedValueOnce(seller).mockResolvedValueOnce({ rows: [] });
      await svc.getInstallTimeSeries(9, { period: 'all', granularity: "day'; DROP TABLE x;--" });
      expect(query.mock.calls[1][1]).toEqual(['day', 5]);
    });
  });

  it('getApiUsageMetrics reports it is a placeholder', async () => {
    expect(await svc.getApiUsageMetrics(9, { period: '7d' })).toMatchObject({ period: '7d', placeholder: true, totalRequests: 0 });
    expect((await svc.getApiUsageMetrics(9)).period).toBe('30d');
  });

  describe('getDiscountCodePerformance', () => {
    it('returns empty results without a profile', async () => {
      query.mockResolvedValueOnce(noSeller);
      expect(await svc.getDiscountCodePerformance(9)).toEqual({ codes: [], summary: { active: 0, expired: 0 } });
    });
    it('computes conversion only when max_uses is set', async () => {
      const base = { id: 1, code: 'A', discount_type: 't', discount_value: 1, is_active: true, valid_from: null, valid_until: null, current_uses: 1, total_redemptions: '5', total_discount_cents: '50', unique_communities: '2' };
      query.mockResolvedValueOnce(seller)
        .mockResolvedValueOnce({ rows: [{ ...base, max_uses: 10 }, { ...base, id: 2, max_uses: null }, { ...base, id: 3, max_uses: 0 }] })
        .mockResolvedValueOnce({ rows: [{ active_count: '2', expired_count: '1' }] });
      const out = await svc.getDiscountCodePerformance(9);
      expect(out.codes.map((c) => c.conversionRate)).toEqual([50, null, null]);
      expect(out.codes[0]).toMatchObject({ totalRedemptions: 5, totalDiscountCents: 50, uniqueCommunities: 2 });
      expect(out.summary).toEqual({ active: 2, expired: 1 });
    });
    it('defaults the summary when the aggregate row is missing', async () => {
      query.mockResolvedValueOnce(seller).mockResolvedValueOnce({ rows: [] }).mockResolvedValueOnce({ rows: [] });
      expect((await svc.getDiscountCodePerformance(9)).summary).toEqual({ active: 0, expired: 0 });
    });
  });

  describe('getCommunityDrilldown', () => {
    it('returns an empty page without a profile', async () => {
      query.mockResolvedValueOnce(noSeller);
      expect(await svc.getCommunityDrilldown(9, { page: 2, limit: 5 })).toEqual({ rows: [], total: 0, page: 2, limit: 5 });
    });
    it('lists with a module filter and allow-listed sort column', async () => {
      query.mockResolvedValueOnce(seller).mockResolvedValueOnce({ rows: [{ id: 1 }] }).mockResolvedValueOnce({ rows: [{ total: '7' }] });
      const out = await svc.getCommunityDrilldown(9, { moduleId: 3, page: 2, limit: 5, sortBy: 'last_active' });
      expect(out).toEqual({ rows: [{ id: 1 }], total: 7, page: 2, limit: 5 });
      expect(query.mock.calls[1][1]).toEqual([5, 5, 5, 3]);
      expect(query.mock.calls[1][0]).toContain('ORDER BY cvi.last_active_at');
      expect(query.mock.calls[2][1]).toEqual([5, 3]);
    });
    it('ignores unknown sort columns and omits the module filter by default', async () => {
      query.mockResolvedValueOnce(seller).mockResolvedValueOnce({ rows: [] }).mockResolvedValueOnce({ rows: [{ total: '0' }] });
      await svc.getCommunityDrilldown(9, { sortBy: '1; DROP TABLE x' });
      expect(query.mock.calls[1][0]).toContain('ORDER BY cvi.installed_at');
      expect(query.mock.calls[1][1]).toEqual([5, 25, 0]);
    });
  });

  describe('exportAnalyticsCsv', () => {
    it('exports install series as CSV', async () => {
      query.mockResolvedValueOnce(seller).mockResolvedValueOnce({ rows: [
        { date: '2026-01-01T00:00:00Z', installs: 1, uninstalls: 0 }, { date: '2026-01-02T00:00:00Z', installs: 2, uninstalls: 1 },
      ] });
      expect(await svc.exportAnalyticsCsv(9, { type: 'installs', period: '7d' })).toEqual({
        csv: 'date,installs,uninstalls\n2026-01-01,1,0\n2026-01-02,2,1', filename: 'install-timeseries-7d.csv',
      });
    });
    it('exports a sales summary by default', async () => {
      query.mockResolvedValueOnce(seller)
        .mockResolvedValueOnce(count(4)).mockResolvedValueOnce(count(1)).mockResolvedValueOnce(count(3))
        .mockResolvedValueOnce(cents(10)).mockResolvedValueOnce(cents(2)).mockResolvedValueOnce(cents(5))
        .mockResolvedValueOnce(count(0)).mockResolvedValueOnce(count(1)).mockResolvedValueOnce(count(2)).mockResolvedValueOnce(count(3));
      const out = await svc.exportAnalyticsCsv(9);
      expect(out.filename).toBe('sales-metrics-30d.csv');
      expect(out.csv.split('\n')[0]).toBe('metric,value');
      expect(out.csv).toContain('total_installations,4');
      expect(out.csv).toContain('churn_rate_pct,25');
    });
    it('returns a header-only CSV for users without a profile', async () => {
      query.mockResolvedValueOnce(noSeller);
      expect(await svc.exportAnalyticsCsv(9, { period: 'all' })).toEqual({ csv: 'metric,value\n', filename: 'sales-all.csv' });
    });
  });
});
