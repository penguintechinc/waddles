import { jest } from '@jest/globals';

/**
 * Builds a chainable Express response double that records status/json/send/header calls.
 */
export function mockRes() {
  const res = {};
  res.status = jest.fn().mockReturnValue(res);
  res.json = jest.fn().mockReturnValue(res);
  res.send = jest.fn().mockReturnValue(res);
  res.setHeader = jest.fn().mockReturnValue(res);
  return res;
}

/**
 * Invokes an Express handler with a request double and returns the response and next spies.
 */
export async function call(handler, req = {}) {
  const res = mockRes();
  const next = jest.fn();
  await handler({ query: {}, params: {}, body: {}, user: { id: 1 }, ...req }, res, next);
  return { res, next };
}
