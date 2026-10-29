import { afterEach, describe, expect, it, jest } from '@jest/globals';

const ORIGINAL_ENV = { ...process.env };

async function loadConfig(env) {
  jest.resetModules();
  process.env = { ...ORIGINAL_ENV, ...env };
  for (const [k, v] of Object.entries(env)) {
    if (v === undefined) delete process.env[k];
  }
  return import('../config/index.js');
}

describe('config', () => {
  afterEach(() => {
    process.env = { ...ORIGINAL_ENV };
  });

  it('applies development defaults', async () => {
    const { config } = await loadConfig({
      NODE_ENV: undefined, MODULE_PORT: undefined, PORT: undefined, JWT_SECRET: undefined,
      SERVICE_API_KEY: undefined, DATABASE_URL: undefined,
    });
    expect(config.env).toBe('development');
    expect(config.port).toBe(8070);
    expect(config.jwt.secret).toContain('dev-secret');
    expect(config.serviceApiKey).toContain('dev-service');
    expect(config.database.url).toContain('postgresql://');
  });

  it('honours explicit env overrides', async () => {
    const { config, default: def } = await loadConfig({
      NODE_ENV: 'test', MODULE_PORT: '9000', JWT_SECRET: 's', SERVICE_API_KEY: 'k',
    });
    expect(config.port).toBe(9000);
    expect(config.jwt.secret).toBe('s');
    expect(config.serviceApiKey).toBe('k');
    expect(def).toBe(config);
  });

  it('falls back to PORT when MODULE_PORT is unset', async () => {
    const { config } = await loadConfig({ MODULE_PORT: undefined, PORT: '1234' });
    expect(config.port).toBe(1234);
  });

  it('accepts strong production secrets', async () => {
    const { config } = await loadConfig({
      NODE_ENV: 'production', JWT_SECRET: 'a-strong-random-value', SERVICE_API_KEY: 'another-strong-value',
    });
    expect(config.jwt.secret).toBe('a-strong-random-value');
  });

  it.each([
    [undefined], ['dev-secret-x'], ['change-in-production'],
  ])('rejects weak/missing JWT_SECRET in production (%s)', async (secret) => {
    await expect(loadConfig({
      NODE_ENV: 'production', JWT_SECRET: secret, SERVICE_API_KEY: 'strong-key-value',
    })).rejects.toThrow('JWT_SECRET');
  });

  it.each([
    [undefined], ['dev-service-x'], ['change-in-production'],
  ])('rejects weak/missing SERVICE_API_KEY in production (%s)', async (key) => {
    await expect(loadConfig({
      NODE_ENV: 'production', JWT_SECRET: 'strong-secret-value', SERVICE_API_KEY: key,
    })).rejects.toThrow('SERVICE_API_KEY');
  });
});
