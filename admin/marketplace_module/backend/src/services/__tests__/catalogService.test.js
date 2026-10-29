import { beforeEach, describe, expect, it, jest } from '@jest/globals';

const query = jest.fn();
jest.unstable_mockModule('../../config/database.js', () => ({ query, default: { query } }));

const svc = await import('../catalogService.js');

const row = (over = {}) => ({
  source: 'core', source_id: 1, name: 'n', display_name: 'N', description: 'd', category: 'c',
  icon_url: 'i', is_core: true, pricing_type: 'free', price_cents: 0, pricing_model: 'flat',
  version: '1', author: 'a', communication_model: 'm', integration_type: 't',
  avg_rating: '4.256', review_count: '3', install_count: '9',
  created_at: new Date('2026-01-01T00:00:00Z'), updated_at: '2026-01-02', ...over,
});

describe('catalogService', () => {
  beforeEach(() => query.mockReset());

  describe('getCatalog', () => {
    it('applies defaults and paginates with no filters', async () => {
      query.mockResolvedValueOnce({ rows: [{ count: '51' }] }).mockResolvedValueOnce({ rows: [row()] });
      const out = await svc.getCatalog({});
      expect(out.pagination).toEqual({ page: 1, limit: 25, total: 51, totalPages: 3 });
      expect(query.mock.calls[0][0]).not.toContain('WHERE');
      expect(query.mock.calls[1][1]).toEqual([25, 0]);
      expect(out.modules[0]).toMatchObject({
        sourceId: 1, avgRating: '4.3', reviewCount: 3, installCount: 9, isInstalled: null,
        isEnabled: null, createdAt: '2026-01-01T00:00:00.000Z', updatedAt: '2026-01-02',
      });
    });

    it('builds a parameterised WHERE for every filter and joins install status', async () => {
      query.mockResolvedValueOnce({ rows: [] }).mockResolvedValueOnce({ rows: [row({ is_installed: true, is_enabled: false })] });
      const out = await svc.getCatalog({
        page: 2, limit: 10, search: 'bot', category: 'chat', pricingType: 'paid', source: 'core', communityId: 7,
      });
      expect(query.mock.calls[0][1].slice(0, 4)).toEqual(['%bot%', 'chat', 'paid', 'core']);
      expect(query.mock.calls[0][0]).toContain('WHERE');
      expect(query.mock.calls[1][1]).toEqual(['%bot%', 'chat', 'paid', 'core', 7, 10, 10]);
      expect(query.mock.calls[1][0]).toContain('is_installed');
      expect(out.pagination.total).toBe(0);
      expect(out.modules[0]).toMatchObject({ isInstalled: true, isEnabled: false });
    });

    it('treats a missing count row as zero and null rating fields as zero', async () => {
      query.mockResolvedValueOnce({ rows: [] }).mockResolvedValueOnce({ rows: [row({ avg_rating: null, review_count: null, install_count: null })] });
      const out = await svc.getCatalog({});
      expect(out.modules[0]).toMatchObject({ avgRating: '0.0', reviewCount: 0, installCount: 0 });
    });
  });

  describe('getCatalogEntry', () => {
    it('returns null when not found', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      expect(await svc.getCatalogEntry('core', 1)).toBeNull();
      expect(query.mock.calls[0][1]).toEqual(['core', 1]);
    });
    it('returns a formatted entry with community install status', async () => {
      query.mockResolvedValueOnce({ rows: [row({ is_installed: false })] });
      const out = await svc.getCatalogEntry('core', 1, 5);
      expect(query.mock.calls[0][1]).toEqual(['core', 1, 5]);
      expect(out.isInstalled).toBe(false);
    });
  });

  it('getCategories maps counts to integers', async () => {
    query.mockResolvedValueOnce({ rows: [{ category: 'a', module_count: '4' }] });
    expect(await svc.getCategories()).toEqual([{ category: 'a', count: 4 }]);
  });

  describe('getFeatured', () => {
    it('lists featured without a community', async () => {
      query.mockResolvedValueOnce({ rows: [row()] });
      expect(await svc.getFeatured()).toHaveLength(1);
      expect(query.mock.calls[0][1]).toEqual([]);
    });
    it('adds install status for a community', async () => {
      query.mockResolvedValueOnce({ rows: [row({ is_installed: true })] });
      const out = await svc.getFeatured(3);
      expect(query.mock.calls[0][1]).toEqual([3]);
      expect(out[0].isInstalled).toBe(true);
    });
  });

  it('exposes a default export', () => {
    expect(svc.default.getCatalog).toBe(svc.getCatalog);
  });
});
