import { beforeEach, describe, expect, it, jest } from '@jest/globals';

const query = jest.fn();
const transaction = jest.fn();
jest.unstable_mockModule('../../config/database.js', () => ({ query, transaction, default: { query, transaction } }));

const cmds = { registerModuleCommands: jest.fn(), unregisterModuleCommands: jest.fn() };
jest.unstable_mockModule('../commandRegistrationService.js', () => cmds);

const svc = await import('../installationService.js');

const dup = Object.assign(new Error('dup'), { code: '23505' });

describe('installationService', () => {
  beforeEach(() => {
    query.mockReset();
    transaction.mockReset();
    cmds.registerModuleCommands.mockReset();
    cmds.unregisterModuleCommands.mockReset();
  });

  it('getInstalledModules maps rows to camelCase', async () => {
    query.mockResolvedValueOnce({ rows: [{
      source: 'core', installation_id: 1, source_id: 2, name: 'n', display_name: 'N', description: 'd',
      category: 'c', icon_url: 'i', version: '1', is_enabled: true, config: {}, installed_at: 'a', updated_at: 'b',
    }] });
    expect(await svc.getInstalledModules(9)).toEqual([{
      source: 'core', installationId: 1, sourceId: 2, name: 'n', displayName: 'N', description: 'd',
      category: 'c', iconUrl: 'i', version: '1', isEnabled: true, config: {}, installedAt: 'a', updatedAt: 'b',
    }]);
  });

  describe('installModule', () => {
    it('installs a core module', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 11 }] });
      expect(await svc.installModule(1, 'core', 2, 3)).toEqual({ installationId: 11, source: 'core', moduleId: 2, communityId: 1 });
    });

    it('maps duplicate core installs to 409 and rethrows other errors', async () => {
      query.mockRejectedValueOnce(dup);
      await expect(svc.installModule(1, 'core', 2, 3)).rejects.toMatchObject({ statusCode: 409 });
      query.mockRejectedValueOnce(new Error('db'));
      await expect(svc.installModule(1, 'core', 2, 3)).rejects.toThrow('db');
    });

    it('rejects marketplace modules that are not approved', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.installModule(1, 'marketplace', 2, 3)).rejects.toMatchObject({ statusCode: 404 });
      expect(transaction).not.toHaveBeenCalled();
    });

    it('installs a marketplace module, registering commands inside the transaction', async () => {
      const mod = { id: 2, trigger_commands: ['!a'] };
      query.mockResolvedValueOnce({ rows: [mod] });
      const client = { query: jest.fn().mockResolvedValue({ rows: [{ id: 77 }] }) };
      transaction.mockImplementation(async (cb) => cb(client));
      expect(await svc.installModule(1, 'marketplace', 2, 3)).toEqual({ installationId: 77, source: 'marketplace', moduleId: 2, communityId: 1 });
      expect(cmds.registerModuleCommands).toHaveBeenCalledWith(1, mod);
    });

    it('maps duplicate marketplace installs to 409 and rethrows other errors', async () => {
      query.mockResolvedValue({ rows: [{ id: 2 }] });
      transaction.mockRejectedValueOnce(dup);
      await expect(svc.installModule(1, 'marketplace', 2, 3)).rejects.toMatchObject({ statusCode: 409 });
      transaction.mockRejectedValueOnce(new Error('db'));
      await expect(svc.installModule(1, 'marketplace', 2, 3)).rejects.toThrow('db');
    });

    it('rejects an invalid source', async () => {
      await expect(svc.installModule(1, 'bogus', 2, 3)).rejects.toMatchObject({ statusCode: 400 });
    });
  });

  describe('uninstallModule', () => {
    it('uninstalls core modules and 404s when not installed', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] });
      expect(await svc.uninstallModule(1, 'core', 2)).toEqual({ success: true });
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.uninstallModule(1, 'core', 2)).rejects.toMatchObject({ statusCode: 404 });
    });

    it('uninstalls marketplace modules, removing commands, and 404s when not subscribed', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] });
      expect(await svc.uninstallModule(1, 'marketplace', 2)).toEqual({ success: true });
      expect(cmds.unregisterModuleCommands).toHaveBeenCalledWith(1, 2);
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.uninstallModule(1, 'marketplace', 2)).rejects.toMatchObject({ statusCode: 404 });
    });

    it('rejects an invalid source', async () => {
      await expect(svc.uninstallModule(1, 'bogus', 2)).rejects.toMatchObject({ statusCode: 400 });
    });
  });

  describe('toggleModule', () => {
    it('toggles core and marketplace modules', async () => {
      query.mockResolvedValue({ rows: [] });
      expect(await svc.toggleModule(1, 'core', 2, false)).toEqual({ success: true });
      expect(query.mock.calls[0][1]).toEqual([1, 2, false]);
      expect(await svc.toggleModule(1, 'marketplace', 2, true)).toEqual({ success: true });
      expect(query.mock.calls[1][0]).toContain('marketplace_subscriptions');
    });
    it('rejects an invalid source', async () => {
      await expect(svc.toggleModule(1, 'bogus', 2, true)).rejects.toMatchObject({ statusCode: 400 });
      expect(query).not.toHaveBeenCalled();
    });
  });

  it('exposes a default export', () => {
    expect(svc.default.installModule).toBe(svc.installModule);
  });
});
