/**
 * Database Configuration
 * PostgreSQL connection pool using node-postgres (pg).
 */
import pg from 'pg';
import { config } from './index.js';
import { logger } from '../utils/logger.js';

const { Pool } = pg;

// Parse connection URL and create pool
const pool = new Pool({
  connectionString: config.database.url,
  max: config.database.poolSize,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 5000,
  // Query timeout: 30 seconds (prevents runaway queries)
  statement_timeout: 30000,
  // Idle transaction timeout: 5 minutes
  query_timeout: 30000,
});

// Pool metrics tracking
let poolMetrics = {
  totalConnections: 0,
  idleConnections: 0,
  activeConnections: 0,
  waitingClients: 0,
  totalQueries: 0,
  slowQueries: 0,
  erroredQueries: 0,
};

// Update pool metrics periodically. The timer is unref'd so a monitoring loop
// never keeps the process (or a test runner) alive after shutdown.
const metricsTimer = setInterval(() => {
  poolMetrics.totalConnections = pool.totalCount;
  poolMetrics.idleConnections = pool.idleCount;
  poolMetrics.activeConnections = pool.totalCount - pool.idleCount;
  poolMetrics.waitingClients = pool.waitingCount;

  // Log if pool is under pressure
  if (poolMetrics.waitingClients > 0) {
    logger.warn('Database pool under pressure', poolMetrics);
  }
}, 60000); // Every 60 seconds
metricsTimer.unref();

// Connection event handlers
pool.on('connect', (client) => {
  logger.debug('New database connection established');

  // Set session-level query timeout
  client.query('SET statement_timeout = 30000').catch(err => {
    logger.error('Failed to set statement timeout', { error: err.message });
  });
});

pool.on('acquire', () => {
  logger.debug('Client acquired from pool', {
    total: pool.totalCount,
    idle: pool.idleCount,
    waiting: pool.waitingCount,
  });
});

pool.on('remove', () => {
  logger.debug('Client removed from pool');
});

pool.on('error', (err) => {
  logger.error('Unexpected database pool error', { error: err.message, stack: err.stack });
  poolMetrics.erroredQueries++;
});

/**
 * Execute a query with parameters
 * @param {string} text - SQL query
 * @param {Array} params - Query parameters
 * @returns {Promise<pg.QueryResult>}
 */
export async function query(text, params = []) {
  const start = Date.now();
  poolMetrics.totalQueries++;

  try {
    const result = await pool.query(text, params);
    const duration = Date.now() - start;

    // Log slow queries (>1 second)
    if (duration > 1000) {
      poolMetrics.slowQueries++;
      logger.warn('Slow query detected', {
        query: text.substring(0, 200),
        duration: `${duration}ms`,
        rows: result.rowCount,
        params: params.length > 0 ? `${params.length} params` : 'no params',
      });
    } else if (duration > 100) {
      // Log moderately slow queries at debug level
      logger.debug('Query executed', {
        query: text.substring(0, 100),
        duration: `${duration}ms`,
        rows: result.rowCount
      });
    }

    return result;
  } catch (error) {
    poolMetrics.erroredQueries++;
    logger.error('Query error', {
      query: text.substring(0, 200),
      error: error.message,
      code: error.code,
      params: params.length > 0 ? `${params.length} params` : 'no params',
    });
    throw error;
  }
}

/**
 * Get a client from the pool for transactions
 * @returns {Promise<pg.PoolClient>}
 */
export async function getClient() {
  const client = await pool.connect();
  const originalRelease = client.release.bind(client);

  // Override release to log
  client.release = () => {
    logger.debug('Database client released');
    return originalRelease();
  };

  return client;
}

/**
 * Execute a transaction with automatic rollback on error
 * @param {Function} callback - Async function receiving client
 * @returns {Promise<any>}
 */
export async function transaction(callback) {
  const client = await getClient();
  try {
    await client.query('BEGIN');
    const result = await callback(client);
    await client.query('COMMIT');
    return result;
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally {
    client.release();
  }
}

/**
 * Check database connectivity
 * @returns {Promise<boolean>}
 */
export async function checkConnection() {
  try {
    const result = await query('SELECT NOW()');
    return result.rows.length > 0;
  } catch {
    return false;
  }
}

/**
 * Get current pool metrics
 * @returns {Object} Pool metrics
 */
export function getPoolMetrics() {
  return {
    ...poolMetrics,
    totalConnections: pool.totalCount,
    idleConnections: pool.idleCount,
    activeConnections: pool.totalCount - pool.idleCount,
    waitingClients: pool.waitingCount,
  };
}

/**
 * Close all pool connections
 */
export async function closePool() {
  logger.info('Closing database pool', poolMetrics);
  await pool.end();
  logger.info('Database pool closed');
}

export { pool };
export default { query, getClient, transaction, checkConnection, closePool, getPoolMetrics, pool };
