import { beforeEach, describe, expect, it, jest } from '@jest/globals';

const query = jest.fn();
jest.unstable_mockModule('../../config/database.js', () => ({ query, transaction: jest.fn(), default: { query } }));

const svc = await import('../moduleService.js');

const mod = (over = {}) => ({
  id: 1, name: 'n', display_name: null, description: 'd', version: '1', author: 'a', category: 'c',
  icon_url: 'i', is_core: false, is_featured: true, avg_rating: '3.46', review_count: '2', install_count: '5',
  created_at: new Date('2026-01-01T00:00:00Z'), updated_at: new Date('2026-01-02T00:00:00Z'), ...over,
});

describe('moduleService', () => {
  beforeEach(() => query.mockReset());

  describe('getModules', () => {
    it('lists published modules with defaults', async () => {
      query.mockResolvedValueOnce({ rows: [{ count: '30' }] }).mockResolvedValueOnce({ rows: [mod()] });
      const out = await svc.getModules({});
      expect(out.pagination).toEqual({ page: 1, limit: 25, total: 30, totalPages: 2 });
      expect(out.modules[0]).toMatchObject({ displayName: 'n', avgRating: '3.5', reviewCount: 2, installCount: 5 });
      expect(query.mock.calls[0][0]).toContain('is_published = true');
    });

    it('applies search, category and featured filters', async () => {
      query.mockResolvedValueOnce({ rows: [] }).mockResolvedValueOnce({ rows: [mod({ avg_rating: null, review_count: null, install_count: null, display_name: 'D' })] });
      const out = await svc.getModules({ page: 3, limit: 5, search: 'x', category: 'c', featured: false });
      expect(query.mock.calls[0][1].slice(0, 3)).toEqual(['%x%', 'c', false]);
      expect(query.mock.calls[1][1]).toEqual(['%x%', 'c', false, 5, 10]);
      expect(out.pagination.total).toBe(0);
      expect(out.modules[0]).toMatchObject({ displayName: 'D', avgRating: '0.0', reviewCount: 0, installCount: 0 });
    });
  });

  describe('getModuleById', () => {
    it('404s for unknown/unpublished modules', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.getModuleById(1)).rejects.toMatchObject({ statusCode: 404 });
    });

    it('returns the module with schema and anonymised reviews', async () => {
      query.mockResolvedValueOnce({ rows: [{ ...mod(), config_schema: { a: 1 } }] }).mockResolvedValueOnce({ rows: [
        { id: 1, rating: 5, review_text: 't', created_at: new Date('2026-02-01T00:00:00Z'), display_name: 'Bob', avatar_url: 'u' },
        { id: 2, rating: 3, review_text: 't2', created_at: new Date('2026-02-02T00:00:00Z'), display_name: null, avatar_url: null },
      ] });
      const out = await svc.getModuleById(1);
      expect(out.configSchema).toEqual({ a: 1 });
      expect(out.reviews.map((r) => r.author)).toEqual(['Bob', 'Anonymous']);
    });
  });

  describe('createModule', () => {
    it('inserts an unpublished module and returns id/createdAt', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 4, created_at: new Date('2026-01-01T00:00:00Z') }] });
      expect(await svc.createModule({ name: 'n', displayName: 'N', configSchema: { a: 1 } }, 9)).toEqual({ id: 4, createdAt: '2026-01-01T00:00:00.000Z' });
      expect(query.mock.calls[0][1][7]).toBe('{"a":1}');
      expect(query.mock.calls[0][1].slice(8)).toEqual([false, false]);
    });
    it('defaults the config schema to an empty object', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 4, created_at: null }] });
      const out = await svc.createModule({ name: 'n', isCore: true, isFeatured: true }, 9);
      expect(out.createdAt).toBeUndefined();
      expect(query.mock.calls[0][1][7]).toBe('{}');
    });
  });

  describe('updateModule', () => {
    it('rejects empty updates', async () => {
      await expect(svc.updateModule(1, {}, 9)).rejects.toMatchObject({ statusCode: 400 });
      expect(query).not.toHaveBeenCalled();
    });
    it('only writes allow-listed fields and maps camelCase', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] });
      await svc.updateModule(1, { displayName: 'x', configSchema: { a: 1 }, isPublished: true, isCore: true, name: 'hijack' }, 9);
      const [sql, params] = query.mock.calls[0];
      expect(sql).toContain('display_name = $2');
      expect(sql).toContain('config_schema = $3::jsonb');
      expect(sql).not.toContain('is_core');
      expect(sql).not.toMatch(/\bname =/);
      expect(params).toEqual([1, 'x', '{"a":1}', true]);
    });
    it('404s when the module does not exist', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.updateModule(1, { version: '2' }, 9)).rejects.toMatchObject({ statusCode: 404 });
    });
  });

  describe('deleteModule', () => {
    it('deletes non-core modules', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 1, name: 'n' }] });
      expect(await svc.deleteModule(1, 9)).toEqual({ success: true });
      expect(query.mock.calls[0][0]).toContain('is_core = false');
    });
    it('refuses to delete core or missing modules', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.deleteModule(1, 9)).rejects.toMatchObject({ statusCode: 404 });
    });
  });

  it('getModuleSubscriptions maps community installs', async () => {
    query.mockResolvedValueOnce({ rows: [
      { id: 1, community_id: 2, is_enabled: true, config: {}, installed_at: new Date('2026-01-01T00:00:00Z'), updated_at: null,
        community_name: 'c', community_display_name: 'C', community_logo: 'l' },
    ] });
    expect(await svc.getModuleSubscriptions(5)).toEqual([{
      id: 1, communityId: 2, communityName: 'c', communityDisplayName: 'C', communityLogo: 'l', isEnabled: true,
      config: {}, installedAt: '2026-01-01T00:00:00.000Z', updatedAt: undefined,
    }]);
    expect(svc.default.getModules).toBe(svc.getModules);
  });
});
