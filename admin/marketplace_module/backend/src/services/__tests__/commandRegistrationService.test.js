import { beforeEach, describe, expect, it, jest } from '@jest/globals';

const query = jest.fn();
jest.unstable_mockModule('../../config/database.js', () => ({ query, default: { query } }));

const redis = { on: jest.fn(), connect: jest.fn(), publish: jest.fn() };
const createClient = jest.fn(() => redis);
jest.unstable_mockModule('redis', () => ({ createClient }));

const svc = await import('../commandRegistrationService.js');
const { logger } = await import('../../utils/logger.js');

describe('commandRegistrationService', () => {
  beforeEach(() => {
    query.mockReset();
    redis.publish.mockReset();
    redis.connect.mockReset().mockResolvedValue();
    jest.spyOn(logger, 'warn').mockImplementation(() => {});
    jest.spyOn(logger, 'audit').mockImplementation(() => {});
    jest.spyOn(logger, 'error').mockImplementation(() => {});
  });

  it('skips registration when the module has no trigger commands', async () => {
    await svc.registerModuleCommands(1, { id: 5 });
    await svc.registerModuleCommands(1, { id: 5, trigger_commands: [] });
    expect(query).not.toHaveBeenCalled();
    expect(redis.publish).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalledTimes(2);
  });

  it('registers each command scoped to the community and publishes a reload', async () => {
    query.mockResolvedValue({ rows: [] });
    await svc.registerModuleCommands(9, { id: 5, trigger_commands: ['!a', '!b'], description: 'desc', category: 'fun' });
    expect(query).toHaveBeenCalledTimes(2);
    expect(query.mock.calls[0][1]).toEqual(['!a', 'marketplace:5', expect.stringContaining('/execute/5'), 'desc', '!a', 'fun', 9]);
    expect(createClient).toHaveBeenCalledTimes(1);
    expect(redis.connect).toHaveBeenCalled();
    expect(JSON.parse(redis.publish.mock.calls[0][1])).toEqual({ communityId: 9, action: 'register', moduleId: 5 });
    // the redis error handler logs
    redis.on.mock.calls[0][1](new Error('r'));
    expect(logger.error).toHaveBeenCalled();
  });

  it('falls back to command text/category defaults', async () => {
    query.mockResolvedValue({ rows: [] });
    await svc.registerModuleCommands(9, { id: 6, trigger_commands: ['!z'] });
    expect(query.mock.calls[0][1]).toEqual(['!z', 'marketplace:6', expect.any(String), '!z', '!z', 'marketplace', 9]);
    expect(createClient).toHaveBeenCalledTimes(1); // client is reused
  });

  it('unregisters commands and publishes a reload', async () => {
    query.mockResolvedValue({ rows: [] });
    await svc.unregisterModuleCommands(9, 5);
    expect(query.mock.calls[0][1]).toEqual([9, 'marketplace:5']);
    expect(JSON.parse(redis.publish.mock.calls[0][1])).toEqual({ communityId: 9, action: 'unregister', moduleId: 5 });
  });

  it('lists registered commands for a community', async () => {
    query.mockResolvedValue({ rows: [{ command: '!a' }] });
    expect(await svc.getRegisteredCommands(9)).toEqual([{ command: '!a' }]);
    expect(svc.default.getRegisteredCommands).toBe(svc.getRegisteredCommands);
  });
});
