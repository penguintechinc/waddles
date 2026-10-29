import { afterEach, describe, expect, it, jest } from '@jest/globals';

async function load(env) {
  jest.resetModules();
  process.env.NODE_ENV = env;
  jest.spyOn(console, 'error').mockImplementation(() => {});
  process.env.JWT_SECRET = 'strong-secret-value';
  process.env.SERVICE_API_KEY = 'strong-service-key';
  return import('../middleware/errorHandler.js');
}

function mockRes() {
  const res = {};
  res.status = jest.fn().mockReturnValue(res);
  res.json = jest.fn().mockReturnValue(res);
  return res;
}

describe('errorHandler', () => {
  const origEnv = process.env.NODE_ENV;
  afterEach(() => {
    process.env.NODE_ENV = origEnv;
    jest.restoreAllMocks();
  });

  it('builds typed errors with the right status codes', async () => {
    const { errors, AppError } = await load('test');
    const cases = {
      badRequest: 400, validation: 400, unauthorized: 401, forbidden: 403, notFound: 404,
      conflict: 409, rateLimited: 429, internal: 500,
    };
    for (const [name, status] of Object.entries(cases)) {
      const e = errors[name]();
      expect(e).toBeInstanceOf(AppError);
      expect(e.statusCode).toBe(status);
      expect(e.isOperational).toBe(true);
      expect(errors[name]('custom').message).toBe('custom');
    }
  });

  it('notFoundHandler returns a 404 envelope', async () => {
    const { notFoundHandler } = await load('test');
    const res = mockRes();
    notFoundHandler({ method: 'GET', path: '/nope' }, res);
    expect(res.status).toHaveBeenCalledWith(404);
    expect(res.json.mock.calls[0][0].error.message).toContain('GET /nope');
  });

  it('includes the stack in development', async () => {
    const { errorHandler, errors } = await load('development');
    const res = mockRes();
    errorHandler(errors.forbidden('nope'), { path: '/', method: 'GET', user: { id: 1 } }, res, jest.fn());
    expect(res.status).toHaveBeenCalledWith(403);
    expect(res.json.mock.calls[0][0].error.stack).toBeDefined();
  });

  it('hides internal error details in production for non-operational errors', async () => {
    const { errorHandler } = await load('production');
    const res = mockRes();
    errorHandler(new Error('db password leaked'), { path: '/', method: 'GET' }, res, jest.fn());
    expect(res.status).toHaveBeenCalledWith(500);
    const body = res.json.mock.calls[0][0];
    expect(body.error.message).toBe('An unexpected error occurred');
    expect(body.error.stack).toBeUndefined();
  });

  it('keeps operational error messages in production and defaults missing fields', async () => {
    const { errorHandler, errors } = await load('production');
    const res = mockRes();
    errorHandler(errors.badRequest('bad input'), { path: '/', method: 'POST' }, res, jest.fn());
    expect(res.json.mock.calls[0][0].error.message).toBe('bad input');

    const res2 = mockRes();
    const bare = { isOperational: true };
    errorHandler(bare, { path: '/', method: 'POST' }, res2, jest.fn());
    expect(res2.status).toHaveBeenCalledWith(500);
    expect(res2.json.mock.calls[0][0].error).toMatchObject({ code: 'INTERNAL_ERROR', message: 'An unexpected error occurred' });
  });
});
