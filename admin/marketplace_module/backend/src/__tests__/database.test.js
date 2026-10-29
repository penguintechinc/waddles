import { beforeEach, afterEach, describe, expect, it, jest } from '@jest/globals';
import { EventEmitter } from 'node:events';

const pool = Object.assign(new EventEmitter(), {
  query: jest.fn(),
  connect: jest.fn(),
  end: jest.fn(),
  totalCount: 3,
  idleCount: 1,
  waitingCount: 0,
});
const PoolCtor = jest.fn(() => pool);
jest.unstable_mockModule('pg', () => ({ default: { Pool: PoolCtor } }));

let timerCb;
const realSetInterval = globalThis.setInterval;

const db = await (async () => {
  globalThis.setInterval = (cb) => { timerCb = cb; return { unref: jest.fn() }; };
  try {
    return await import('../config/database.js');
  } finally {
    globalThis.setInterval = realSetInterval;
  }
})();
const { logger } = await import('../utils/logger.js');

describe('database', () => {
  beforeEach(() => {
    pool.query.mockReset();
    pool.connect.mockReset();
    pool.end.mockReset();
    pool.waitingCount = 0;
    jest.spyOn(logger, 'debug').mockImplementation(() => {});
    jest.spyOn(logger, 'warn').mockImplementation(() => {});
    jest.spyOn(logger, 'error').mockImplementation(() => {});
    jest.spyOn(logger, 'info').mockImplementation(() => {});
  });
  afterEach(() => jest.restoreAllMocks());

  it('creates the pool from config', () => {
    expect(PoolCtor).toHaveBeenCalledWith(expect.objectContaining({ max: 10 }));
    expect(db.pool).toBe(pool);
    expect(db.default.query).toBe(db.query);
  });

  it('query returns the result for fast queries', async () => {
    pool.query.mockResolvedValue({ rows: [1], rowCount: 1 });
    await expect(db.query('SELECT 1')).resolves.toEqual({ rows: [1], rowCount: 1 });
    await db.query('SELECT 1', ['a']);
    expect(logger.warn).not.toHaveBeenCalled();
  });

  it('query logs moderately slow and slow queries', async () => {
    const now = jest.spyOn(Date, 'now');
    pool.query.mockResolvedValue({ rows: [], rowCount: 0 });
    now.mockReturnValueOnce(0).mockReturnValueOnce(500);
    await db.query('SELECT 1');
    expect(logger.debug).toHaveBeenCalledWith('Query executed', expect.any(Object));
    now.mockReturnValueOnce(0).mockReturnValueOnce(2000);
    await db.query('SELECT 1', ['p']);
    expect(logger.warn).toHaveBeenCalledWith('Slow query detected', expect.objectContaining({ params: '1 params' }));
    now.mockReturnValueOnce(0).mockReturnValueOnce(2000);
    await db.query('SELECT 1');
    expect(logger.warn).toHaveBeenLastCalledWith('Slow query detected', expect.objectContaining({ params: 'no params' }));
  });

  it('query logs and rethrows errors', async () => {
    pool.query.mockRejectedValue(Object.assign(new Error('boom'), { code: '42' }));
    await expect(db.query('SELECT 1', ['x'])).rejects.toThrow('boom');
    expect(logger.error).toHaveBeenCalledWith('Query error', expect.objectContaining({ code: '42' }));
    await expect(db.query('SELECT 1')).rejects.toThrow('boom');
  });

  it('getClient wraps release with logging', async () => {
    const release = jest.fn();
    pool.connect.mockResolvedValue({ release });
    const client = await db.getClient();
    client.release();
    expect(release).toHaveBeenCalled();
    expect(logger.debug).toHaveBeenCalledWith('Database client released');
  });

  it('transaction commits on success and rolls back on failure', async () => {
    const client = { query: jest.fn().mockResolvedValue({}), release: jest.fn() };
    pool.connect.mockResolvedValue(client);
    await expect(db.transaction(async () => 'ok')).resolves.toBe('ok');
    expect(client.query.mock.calls.map((c) => c[0])).toEqual(['BEGIN', 'COMMIT']);

    client.query.mockClear();
    await expect(db.transaction(async () => { throw new Error('fail'); })).rejects.toThrow('fail');
    expect(client.query.mock.calls.map((c) => c[0])).toEqual(['BEGIN', 'ROLLBACK']);
  });

  it('checkConnection reflects query health', async () => {
    pool.query.mockResolvedValueOnce({ rows: [{}], rowCount: 1 });
    expect(await db.checkConnection()).toBe(true);
    pool.query.mockResolvedValueOnce({ rows: [], rowCount: 0 });
    expect(await db.checkConnection()).toBe(false);
    pool.query.mockRejectedValueOnce(new Error('down'));
    expect(await db.checkConnection()).toBe(false);
  });

  it('exposes pool metrics and closes the pool', async () => {
    expect(db.getPoolMetrics()).toMatchObject({ totalConnections: 3, idleConnections: 1, activeConnections: 2 });
    await db.closePool();
    expect(pool.end).toHaveBeenCalled();
  });

  it('pool events are handled', async () => {
    const client = { query: jest.fn().mockResolvedValue({}) };
    pool.emit('connect', client);
    expect(client.query).toHaveBeenCalledWith('SET statement_timeout = 30000');
    const failing = { query: jest.fn().mockRejectedValue(new Error('x')) };
    pool.emit('connect', failing);
    await new Promise((r) => setTimeout(r, 0));
    expect(logger.error).toHaveBeenCalledWith('Failed to set statement timeout', expect.any(Object));
    pool.emit('acquire');
    pool.emit('remove');
    pool.emit('error', new Error('pool broke'));
    expect(logger.error).toHaveBeenCalledWith('Unexpected database pool error', expect.any(Object));
  });

  it('metrics timer warns only when clients are waiting', () => {
    timerCb();
    expect(logger.warn).not.toHaveBeenCalled();
    pool.waitingCount = 2;
    timerCb();
    expect(logger.warn).toHaveBeenCalledWith('Database pool under pressure', expect.any(Object));
  });
});
