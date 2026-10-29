import { beforeEach, describe, expect, it, jest } from '@jest/globals';
import jwt from 'jsonwebtoken';

const query = jest.fn();
jest.unstable_mockModule('../config/database.js', () => ({ query, default: { query } }));

const auth = await import('../middleware/auth.js');
const { config } = await import('../config/index.js');
const { logger } = await import('../utils/logger.js');

const token = (payload = {}) => jwt.sign({ userId: 5, username: 'u', ...payload }, config.jwt.secret);

describe('auth middleware', () => {
  beforeEach(() => {
    query.mockReset();
    jest.spyOn(logger, 'authz').mockImplementation(() => {});
    jest.spyOn(logger, 'debug').mockImplementation(() => {});
  });

  describe('requireAuth / optionalAuth', () => {
    it('accepts a Bearer token with an active session', async () => {
      query.mockResolvedValue({ rows: [{ id: 1 }] });
      const req = { headers: { authorization: `Bearer ${token({ roles: ['x'], communityRoles: { 1: 'a' } })}` } };
      const next = jest.fn();
      await auth.requireAuth(req, {}, next);
      expect(req.user).toMatchObject({ id: 5, userId: 5, roles: ['x'], communityRoles: { 1: 'a' } });
      expect(next).toHaveBeenCalledWith();
    });

    it('accepts a cookie token and defaults roles', async () => {
      query.mockResolvedValue({ rows: [{ id: 1 }] });
      const req = { headers: {}, cookies: { token: token() } };
      await auth.optionalAuth(req, {}, jest.fn());
      expect(req.user.roles).toEqual([]);
      expect(req.user.communityRoles).toEqual({});
    });

    it('rejects when no token is present', async () => {
      const next = jest.fn();
      await auth.requireAuth({ headers: {} }, {}, next);
      expect(next.mock.calls[0][0].statusCode).toBe(401);
      expect(query).not.toHaveBeenCalled();
    });

    it('rejects a forged/invalid token', async () => {
      const next = jest.fn();
      await auth.requireAuth({ headers: { authorization: 'Bearer garbage' } }, {}, next);
      expect(next.mock.calls[0][0].statusCode).toBe(401);
    });

    it('rejects a valid JWT whose session is inactive/expired', async () => {
      query.mockResolvedValue({ rows: [] });
      const req = { headers: { authorization: `Bearer ${token()}` } };
      const next = jest.fn();
      await auth.requireAuth(req, {}, next);
      expect(req.user).toBeNull();
      expect(next.mock.calls[0][0].statusCode).toBe(401);
    });

    it('optionalAuth continues anonymously with no token', async () => {
      const req = { headers: {} };
      const next = jest.fn();
      await auth.optionalAuth(req, {}, next);
      expect(req.user).toBeNull();
      expect(next).toHaveBeenCalledWith();
    });
  });

  describe('requireSuperAdmin', () => {
    it('allows super_admin and denies everyone else', () => {
      const ok = jest.fn();
      auth.requireSuperAdmin({ user: { roles: ['super_admin'] } }, {}, ok);
      expect(ok).toHaveBeenCalledWith();
      for (const req of [{ user: { roles: ['viewer'] } }, { user: null }, {}]) {
        const next = jest.fn();
        auth.requireSuperAdmin(req, {}, next);
        expect(next.mock.calls[0][0].statusCode).toBe(403);
      }
    });
  });

  describe('requireCommunityAdmin', () => {
    const call = async (req) => {
      const next = jest.fn();
      req.params = { communityId: '3' };
      await auth.requireCommunityAdmin(req, {}, next);
      return next.mock.calls[0][0];
    };

    it('reads the community id from the body when there is no path param (premium routes)', async () => {
      query.mockResolvedValueOnce({ rows: [{ role: 'community-owner' }] });
      const next = jest.fn();
      const req = { params: {}, body: { communityId: 7 }, user: { id: 1 } };
      await auth.requireCommunityAdmin(req, {}, next);
      expect(next.mock.calls[0][0]).toBeUndefined();
      expect(query.mock.calls.at(-1)[1][0]).toBe(7);
      const bad = jest.fn();
      await auth.requireCommunityAdmin({ params: {}, body: {}, user: { id: 1 } }, {}, bad);
      expect(bad.mock.calls[0][0].statusCode).toBe(400);
    });

    it('normalizes the validated id onto req (communityId, params, body)', async () => {
      query.mockResolvedValueOnce({ rows: [{ role: 'community-owner' }] });
      const req = { params: {}, body: { communityId: '0007' }, user: { id: 1 } };
      const next = jest.fn();
      await auth.requireCommunityAdmin(req, {}, next);
      expect(next.mock.calls[0][0]).toBeUndefined();
      expect([req.communityId, req.body.communityId]).toEqual([7, 7]);
      expect(query.mock.calls.at(-1)[1][0]).toBe(7);
      const r2 = { params: { communityId: '12' }, user: { isSuperAdmin: true } };
      await auth.requireCommunityAdmin(r2, {}, jest.fn());
      expect([r2.communityId, r2.params.communityId]).toEqual([12, '12']);
    });

    it.each([
      ['trailing junk', { params: { communityId: '123abc' } }],
      ['body trailing junk', { params: {}, body: { communityId: '123abc' } }],
      ['array in body', { params: {}, body: { communityId: [1] } }],
      ['array in params', { params: { communityId: ['1'] } }],
      ['object in body', { params: {}, body: { communityId: { toString: () => '1' } } }],
      ['params/body mismatch', { params: { communityId: '1' }, body: { communityId: 2 } }],
      ['params valid, body junk', { params: { communityId: '1' }, body: { communityId: '1abc' } }],
      ['non-safe integer number', { params: {}, body: { communityId: 2 ** 53 } }],
      ['non-safe integer string', { params: { communityId: '9007199254740993' } }],
      ['float', { params: {}, body: { communityId: 1.5 } }],
      ['negative', { params: { communityId: '-1' } }],
      ['zero', { params: { communityId: '0' } }],
      ['null body value', { params: {}, body: { communityId: null } }],
      ['hex string', { params: { communityId: '0x10' } }],
    ])('rejects parser-differential input: %s', async (_n, partial) => {
      const next = jest.fn();
      await auth.requireCommunityAdmin({ user: { isSuperAdmin: true }, ...partial }, {}, next);
      expect(next.mock.calls[0][0].statusCode).toBe(400);
    });

    it('accepts agreeing params and body', async () => {
      const req = { params: { communityId: '5' }, body: { communityId: 5 }, user: { isSuperAdmin: true } };
      const next = jest.fn();
      await auth.requireCommunityAdmin(req, {}, next);
      expect(next.mock.calls[0][0]).toBeUndefined();
      expect(req.communityId).toBe(5);
    });

    it('rejects an invalid community id', async () => {
      const next = jest.fn();
      await auth.requireCommunityAdmin({ params: { communityId: 'x' }, user: { id: 1 } }, {}, next);
      expect(next.mock.calls[0][0].statusCode).toBe(400);
    });

    it('lets super admins and platform admins through', async () => {
      const r1 = { user: { isSuperAdmin: true } };
      expect(await call(r1)).toBeUndefined();
      expect(r1.communityRole).toBe('super-admin');
      const r2 = { user: { roles: ['super_admin'] } };
      expect(await call(r2)).toBeUndefined();
      const r3 = { user: { roles: ['platform-admin'] } };
      expect(await call(r3)).toBeUndefined();
      expect(r3.communityRole).toBe('platform-admin');
    });

    it('denies non-members and insufficient roles', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      expect((await call({ user: { id: 1 } })).statusCode).toBe(403);
      query.mockResolvedValueOnce({ rows: [{ role: 'member' }] });
      expect((await call({ user: { id: 1 } })).statusCode).toBe(403);
    });

    it('allows community admin roles', async () => {
      query.mockResolvedValueOnce({ rows: [{ role: 'moderator' }] });
      const req = { user: { id: 1 } };
      expect(await call(req)).toBeUndefined();
      expect(req.communityRole).toBe('moderator');
    });

    it('forwards database errors', async () => {
      query.mockRejectedValueOnce(new Error('db'));
      expect((await call({ user: { id: 1 } })).message).toBe('db');
    });
  });

  describe('requireServiceAuth', () => {
    it('rejects missing and wrong keys, accepts the right one', () => {
      let next = jest.fn();
      auth.requireServiceAuth({ headers: {} }, {}, next);
      expect(next.mock.calls[0][0].statusCode).toBe(401);
      next = jest.fn();
      auth.requireServiceAuth({ headers: { 'x-api-key': 'wrong' } }, {}, next);
      expect(next.mock.calls[0][0].message).toBe('Invalid service API key');
      next = jest.fn();
      const req = { headers: { 'x-service-key': config.serviceApiKey } };
      auth.requireServiceAuth(req, {}, next);
      expect(req.isServiceRequest).toBe(true);
      expect(next).toHaveBeenCalledWith();
    });
  });

  it('exposes a default export', () => {
    expect(auth.default.requireAuth).toBe(auth.requireAuth);
  });
});
