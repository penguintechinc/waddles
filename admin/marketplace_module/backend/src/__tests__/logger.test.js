import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';

describe('logger', () => {
  let logger;
  let spies;

  beforeEach(async () => {
    jest.resetModules();
    process.env.LOG_LEVEL = 'debug';
    spies = {
      error: jest.spyOn(console, 'error').mockImplementation(() => {}),
      warn: jest.spyOn(console, 'warn').mockImplementation(() => {}),
      log: jest.spyOn(console, 'log').mockImplementation(() => {}),
    };
    ({ logger } = await import('../utils/logger.js'));
  });

  afterEach(() => {
    delete process.env.LOG_LEVEL;
    jest.restoreAllMocks();
  });

  it('routes levels to the matching console method as structured JSON', () => {
    logger.error('e', { a: 1 });
    logger.warn('w');
    logger.info('i');
    logger.debug('d');
    expect(JSON.parse(spies.error.mock.calls[0][0])).toMatchObject({ level: 'ERROR', message: 'e', a: 1, module: 'marketplace_module' });
    expect(JSON.parse(spies.warn.mock.calls[0][0]).level).toBe('WARN');
    expect(spies.log).toHaveBeenCalledTimes(2);
  });

  it('emits categorised AAA events', () => {
    logger.auth('login', { x: 1 });
    logger.authz('deny');
    logger.audit('change');
    logger.system('boot');
    const entries = spies.log.mock.calls.map((c) => JSON.parse(c[0]));
    expect(entries.map((e) => e.category)).toEqual(['AUTH', 'AUTHZ', 'AUDIT', 'SYSTEM']);
    expect(entries[0].message).toBe('AUTH: login');
  });

  it('logs http requests with and without a user', () => {
    const req = { method: 'GET', path: '/x', ip: '1.1.1.1', get: () => 'agent' };
    logger.http(req, { statusCode: 200 }, 5);
    logger.http({ ...req, user: { id: 7 }, get: () => undefined }, { statusCode: 500 }, 6);
    const [a, b] = spies.log.mock.calls.map((c) => JSON.parse(c[0]));
    expect(a.userId).toBeUndefined();
    expect(a.userAgent).toBe('agent');
    expect(b.userId).toBe(7);
  });

  it('suppresses messages below the configured level', async () => {
    jest.resetModules();
    process.env.LOG_LEVEL = 'error';
    const { logger: quiet } = await import('../utils/logger.js');
    quiet.info('hidden');
    quiet.debug('hidden');
    expect(spies.log).not.toHaveBeenCalled();
    quiet.error('shown');
    expect(spies.error).toHaveBeenCalled();
  });

  it('falls back to info for unknown level names', async () => {
    jest.resetModules();
    process.env.LOG_LEVEL = 'bogus';
    const { default: l } = await import('../utils/logger.js');
    l.debug('hidden');
    l.info('shown');
    expect(spies.log).toHaveBeenCalledTimes(1);
  });
});
