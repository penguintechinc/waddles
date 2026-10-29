import { beforeEach, describe, expect, it, jest } from '@jest/globals';
import { call } from '../../__tests__/helpers.js';

const catalog = { getCatalog: jest.fn(), getCategories: jest.fn(), getFeatured: jest.fn(), getCatalogEntry: jest.fn() };
const modules = { getModules: jest.fn(), getModuleById: jest.fn(), createModule: jest.fn(), updateModule: jest.fn(), deleteModule: jest.fn(), getModuleSubscriptions: jest.fn() };
const installs = { getInstalledModules: jest.fn(), installModule: jest.fn(), uninstallModule: jest.fn(), toggleModule: jest.fn() };
jest.unstable_mockModule('../../services/catalogService.js', () => catalog);
jest.unstable_mockModule('../../services/moduleService.js', () => modules);
jest.unstable_mockModule('../../services/installationService.js', () => installs);

const catalogCtl = await import('../catalogController.js');
const moduleCtl = await import('../moduleController.js');
const installCtl = await import('../installationController.js');

const reset = (...groups) => groups.forEach((g) => Object.values(g).forEach((f) => f.mockReset()));

describe('catalogController', () => {
  beforeEach(() => reset(catalog));

  it('browseCatalog parses and clamps query parameters', async () => {
    catalog.getCatalog.mockResolvedValue({ modules: [], pagination: {} });
    const { res } = await call(catalogCtl.browseCatalog, { query: { page: '-4', limit: '9999', search: 's', category: 'c', pricingType: 'p', source: 'core', communityId: '7' } });
    expect(catalog.getCatalog).toHaveBeenCalledWith({ page: 1, limit: 100, search: 's', category: 'c', pricingType: 'p', source: 'core', communityId: 7 });
    expect(res.json).toHaveBeenCalledWith({ success: true, modules: [], pagination: {} });
  });

  it('browseCatalog uses defaults and forwards errors', async () => {
    catalog.getCatalog.mockRejectedValue(new Error('x'));
    const { next } = await call(catalogCtl.browseCatalog);
    expect(catalog.getCatalog).toHaveBeenCalledWith({ page: 1, limit: 25, search: '', category: null, pricingType: null, source: null, communityId: null });
    expect(next).toHaveBeenCalledWith(expect.any(Error));
  });

  it('getCategories returns categories or forwards errors', async () => {
    catalog.getCategories.mockResolvedValueOnce([1]);
    expect((await call(catalogCtl.getCategories)).res.json).toHaveBeenCalledWith({ success: true, categories: [1] });
    catalog.getCategories.mockRejectedValueOnce(new Error('x'));
    expect((await call(catalogCtl.getCategories)).next).toHaveBeenCalled();
  });

  it('getFeatured scopes to an optional community', async () => {
    catalog.getFeatured.mockResolvedValue([2]);
    await call(catalogCtl.getFeatured, { query: { communityId: '3' } });
    expect(catalog.getFeatured).toHaveBeenLastCalledWith(3);
    const { res } = await call(catalogCtl.getFeatured);
    expect(catalog.getFeatured).toHaveBeenLastCalledWith(null);
    expect(res.json).toHaveBeenCalledWith({ success: true, modules: [2] });
    catalog.getFeatured.mockRejectedValueOnce(new Error('x'));
    expect((await call(catalogCtl.getFeatured)).next).toHaveBeenCalled();
  });

  it('getCatalogEntry returns the entry, a 404, or forwards errors', async () => {
    catalog.getCatalogEntry.mockResolvedValueOnce({ a: 1 });
    const ok = await call(catalogCtl.getCatalogEntry, { params: { source: 'core', id: '5' }, query: { communityId: '2' } });
    expect(catalog.getCatalogEntry).toHaveBeenCalledWith('core', 5, 2);
    expect(ok.res.json).toHaveBeenCalledWith({ success: true, module: { a: 1 } });
    catalog.getCatalogEntry.mockResolvedValueOnce(null);
    const missing = await call(catalogCtl.getCatalogEntry, { params: { source: 'core', id: '5' } });
    expect(missing.res.status).toHaveBeenCalledWith(404);
    catalog.getCatalogEntry.mockRejectedValueOnce(new Error('x'));
    expect((await call(catalogCtl.getCatalogEntry, { params: { source: 'core', id: '5' } })).next).toHaveBeenCalled();
    expect(catalogCtl.default.browseCatalog).toBe(catalogCtl.browseCatalog);
  });
});

describe('moduleController', () => {
  beforeEach(() => reset(modules));

  it('browseModules maps featured flags and clamps paging', async () => {
    modules.getModules.mockResolvedValue({ modules: [1], pagination: { p: 1 } });
    const { res } = await call(moduleCtl.browseModules, { query: { featured: 'true', limit: '0', page: '0' } });
    expect(modules.getModules).toHaveBeenLastCalledWith({ page: 1, limit: 1, search: '', category: undefined, featured: true });
    expect(res.json).toHaveBeenCalledWith({ success: true, modules: [1], pagination: { p: 1 } });
    await call(moduleCtl.browseModules, { query: { featured: 'false' } });
    expect(modules.getModules.mock.calls[1][0].featured).toBe(false);
    await call(moduleCtl.browseModules, { query: { featured: 'maybe', limit: '500' } });
    expect(modules.getModules.mock.calls[2][0]).toMatchObject({ featured: null, limit: 100 });
    modules.getModules.mockRejectedValueOnce(new Error('x'));
    expect((await call(moduleCtl.browseModules)).next).toHaveBeenCalled();
  });

  it('getModuleDetails returns the module or forwards errors', async () => {
    modules.getModuleById.mockResolvedValueOnce({ id: 1 });
    expect((await call(moduleCtl.getModuleDetails, { params: { id: '1' } })).res.json).toHaveBeenCalledWith({ success: true, module: { id: 1 } });
    modules.getModuleById.mockRejectedValueOnce(new Error('x'));
    expect((await call(moduleCtl.getModuleDetails, { params: { id: '1' } })).next).toHaveBeenCalled();
  });

  it('createModule whitelists body fields and records the creator', async () => {
    modules.createModule.mockResolvedValue({ id: 1 });
    const { res } = await call(moduleCtl.createModule, { body: { name: 'n', displayName: 'd', isCore: true, evil: 1 }, user: { id: 9 } });
    expect(modules.createModule.mock.calls[0][0]).not.toHaveProperty('evil');
    expect(modules.createModule.mock.calls[0][1]).toBe(9);
    expect(res.status).toHaveBeenCalledWith(201);
    modules.createModule.mockRejectedValueOnce(new Error('x'));
    expect((await call(moduleCtl.createModule)).next).toHaveBeenCalled();
  });

  it('updateModule never forwards name/isCore', async () => {
    modules.updateModule.mockResolvedValue({});
    await call(moduleCtl.updateModule, { params: { id: '4' }, body: { displayName: 'd', name: 'hijack', isCore: true } });
    expect(modules.updateModule.mock.calls[0][0]).toBe(4);
    expect(modules.updateModule.mock.calls[0][1]).not.toHaveProperty('name');
    expect(modules.updateModule.mock.calls[0][1]).not.toHaveProperty('isCore');
    modules.updateModule.mockRejectedValueOnce(new Error('x'));
    expect((await call(moduleCtl.updateModule, { params: { id: '4' } })).next).toHaveBeenCalled();
  });

  it('deleteModule and getModuleSubscriptions respond or forward errors', async () => {
    modules.deleteModule.mockResolvedValueOnce({});
    expect((await call(moduleCtl.deleteModule, { params: { id: '4' } })).res.json).toHaveBeenCalledWith({ success: true, message: 'Module deleted successfully' });
    modules.deleteModule.mockRejectedValueOnce(new Error('x'));
    expect((await call(moduleCtl.deleteModule, { params: { id: '4' } })).next).toHaveBeenCalled();
    modules.getModuleSubscriptions.mockResolvedValueOnce([1, 2]);
    expect((await call(moduleCtl.getModuleSubscriptions, { params: { id: '4' } })).res.json).toHaveBeenCalledWith({ success: true, subscriptions: [1, 2], total: 2 });
    modules.getModuleSubscriptions.mockRejectedValueOnce(new Error('x'));
    expect((await call(moduleCtl.getModuleSubscriptions, { params: { id: '4' } })).next).toHaveBeenCalled();
    expect(moduleCtl.default.createModule).toBe(moduleCtl.createModule);
  });
});

describe('installationController', () => {
  beforeEach(() => reset(installs));

  it('getInstalledModules lists or forwards errors', async () => {
    installs.getInstalledModules.mockResolvedValueOnce([1]);
    expect((await call(installCtl.getInstalledModules, { params: { communityId: '3' } })).res.json).toHaveBeenCalledWith({ success: true, installed: [1], total: 1 });
    installs.getInstalledModules.mockRejectedValueOnce(new Error('x'));
    expect((await call(installCtl.getInstalledModules, { params: { communityId: '3' } })).next).toHaveBeenCalled();
  });

  it.each([
    [{ moduleId: 1 }], [{ source: 'bogus', moduleId: 1 }], [{ source: 'core', moduleId: 'abc' }], [{ source: 'core', moduleId: -3 }], [{ source: 'core' }],
  ])('installModule rejects bad input %#', async (body) => {
    const { next, res } = await call(installCtl.installModule, { params: { communityId: '3' }, body });
    expect(next.mock.calls[0][0].statusCode).toBe(400);
    expect(res.status).not.toHaveBeenCalled();
    expect(installs.installModule).not.toHaveBeenCalled();
  });

  it('installModule installs on behalf of the authenticated user', async () => {
    installs.installModule.mockResolvedValueOnce({ installationId: 1 });
    const { res } = await call(installCtl.installModule, { params: { communityId: '3' }, body: { source: 'marketplace', moduleId: '8' }, user: { id: 5 } });
    expect(installs.installModule).toHaveBeenCalledWith(3, 'marketplace', 8, 5);
    expect(res.status).toHaveBeenCalledWith(201);
    installs.installModule.mockRejectedValueOnce(new Error('x'));
    expect((await call(installCtl.installModule, { params: { communityId: '3' }, body: { source: 'core', moduleId: 1 } })).next).toHaveBeenCalled();
  });

  it('uninstallModule defaults the source to core', async () => {
    installs.uninstallModule.mockResolvedValue({});
    await call(installCtl.uninstallModule, { params: { communityId: '3', moduleId: '4' } });
    expect(installs.uninstallModule).toHaveBeenLastCalledWith(3, 'core', 4);
    await call(installCtl.uninstallModule, { params: { communityId: '3', moduleId: '4' }, query: { source: 'marketplace' } });
    expect(installs.uninstallModule).toHaveBeenLastCalledWith(3, 'marketplace', 4);
    installs.uninstallModule.mockRejectedValueOnce(new Error('x'));
    expect((await call(installCtl.uninstallModule, { params: { communityId: '3', moduleId: '4' } })).next).toHaveBeenCalled();
  });

  it('toggleModule passes the flag through', async () => {
    installs.toggleModule.mockResolvedValue({});
    await call(installCtl.toggleModule, { params: { communityId: '3', moduleId: '4' }, body: { source: 'core', isEnabled: false } });
    expect(installs.toggleModule).toHaveBeenCalledWith(3, 'core', 4, false);
    installs.toggleModule.mockRejectedValueOnce(new Error('x'));
    expect((await call(installCtl.toggleModule, { params: { communityId: '3', moduleId: '4' } })).next).toHaveBeenCalled();
    expect(installCtl.default.toggleModule).toBe(installCtl.toggleModule);
  });
});
