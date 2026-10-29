/**
 * Authentication Middleware
 * JWT verification and role-based access control
 */
import jwt from 'jsonwebtoken';
import { config } from '../config/index.js';
import { errors } from './errorHandler.js';
import { query } from '../config/database.js';
import { logger } from '../utils/logger.js';

/**
 * Extract JWT token from request
 */
function extractToken(req) {
  const authHeader = req.headers.authorization;
  if (authHeader && authHeader.startsWith('Bearer ')) {
    return authHeader.substring(7);
  }
  return req.cookies?.token || null;
}

/**
 * Verify JWT token and attach user to request
 */
async function verifyToken(req) {
  const token = extractToken(req);
  if (!token) return null;

  try {
    const decoded = jwt.verify(token, config.jwt.secret);

    // Check if session is still active
    const result = await query(
      'SELECT * FROM hub_sessions WHERE session_token = $1 AND is_active = true AND expires_at > NOW()',
      [token]
    );

    if (result.rows.length === 0) {
      return null;
    }

    return {
      id: decoded.userId,
      userId: decoded.userId, // For backwards compatibility
      platform: decoded.platform,
      platformUserId: decoded.platformUserId,
      username: decoded.username,
      email: decoded.email,
      avatarUrl: decoded.avatarUrl,
      isSuperAdmin: decoded.isSuperAdmin,
      roles: decoded.roles || [],
      communityRoles: decoded.communityRoles || {},
    };
  } catch (err) {
    logger.debug('Token verification failed', { error: err.message });
    return null;
  }
}

/**
 * Optional authentication - sets req.user if valid token present
 */
export async function optionalAuth(req, res, next) {
  req.user = await verifyToken(req);
  next();
}

/**
 * Required authentication - rejects if no valid token
 */
export async function requireAuth(req, res, next) {
  req.user = await verifyToken(req);

  if (!req.user) {
    logger.authz('Access denied - no valid token', { path: req.path });
    return next(errors.unauthorized('Authentication required'));
  }

  next();
}

/**
 * Require super admin role (hub-level admin)
 */
export function requireSuperAdmin(req, res, next) {
  if (!req.user?.roles?.includes('super_admin')) {
    logger.authz('Access denied - not super admin', {
      userId: req.user?.id,
      path: req.path,
    });
    return next(errors.forbidden('Super admin access required'));
  }
  next();
}

/**
 * Strictly parses one community-id candidate: a JSON number that is a positive safe integer,
 * or a string of ASCII digits only. Arrays, objects, "123abc", floats and unsafe ints yield null.
 */
export function parseCommunityId(value) {
  let n;
  if (typeof value === 'number') {
    n = value;
  } else if (typeof value === 'string' && /^\d+$/.test(value)) {
    n = Number(value);
  } else {
    return null;
  }
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

/**
 * Resolves the single authoritative community id for a request. Path param and body value are
 * both validated; if both are present they must agree (no gate-vs-action differential).
 * Returns the integer, or null when absent/invalid/mismatched.
 */
function resolveCommunityId(req) {
  const hasParam = req.params?.communityId !== undefined;
  const hasBody = req.body?.communityId !== undefined;
  if (!hasParam && !hasBody) return null;
  const fromParam = hasParam ? parseCommunityId(req.params.communityId) : undefined;
  const fromBody = hasBody ? parseCommunityId(req.body.communityId) : undefined;
  if (fromParam === null || fromBody === null) return null;
  if (hasParam && hasBody && fromParam !== fromBody) return null;
  return hasParam ? fromParam : fromBody;
}

/**
 * Require community admin role (owner, admin, or moderator).
 * The id is strictly validated, then normalized onto req.communityId / req.params / req.body so
 * controllers act on exactly the value that was authorized — they must never re-parse it.
 */
export async function requireCommunityAdmin(req, res, next) {
  const communityId = resolveCommunityId(req);

  if (communityId === null) {
    return next(errors.badRequest('Invalid community ID'));
  }

  req.communityId = communityId;
  if (req.params?.communityId !== undefined) req.params.communityId = String(communityId);
  if (req.body && typeof req.body === 'object' && req.body.communityId !== undefined) {
    req.body.communityId = communityId;
  }

  // Super admins have access to all communities
  if (req.user?.isSuperAdmin || req.user?.roles?.includes('super_admin')) {
    req.communityRole = 'super-admin';
    return next();
  }

  // Platform admins have access to all communities
  if (req.user?.roles?.includes('platform-admin')) {
    req.communityRole = 'platform-admin';
    return next();
  }

  // Check community role
  try {
    const result = await query(
      `SELECT role FROM community_members
       WHERE community_id = $1 AND user_id = $2 AND is_active = true`,
      [communityId, req.user.id]
    );

    if (result.rows.length === 0) {
      logger.authz('Access denied - not a member', {
        userId: req.user.id,
        communityId,
      });
      return next(errors.forbidden('Community admin access required'));
    }

    const role = result.rows[0].role;
    const adminRoles = ['community-owner', 'community-admin', 'moderator'];

    if (!adminRoles.includes(role)) {
      logger.authz('Access denied - insufficient role', {
        userId: req.user.id,
        communityId,
        role,
      });
      return next(errors.forbidden('Community admin access required'));
    }

    req.communityRole = role;
    next();
  } catch (err) {
    next(err);
  }
}

/**
 * Require service API key for internal service-to-service calls
 */
export function requireServiceAuth(req, res, next) {
  const apiKey = req.headers['x-api-key'] || req.headers['x-service-key'];

  if (!apiKey) {
    logger.authz('Access denied - no service API key', { path: req.path });
    return next(errors.unauthorized('Service API key required'));
  }

  if (apiKey !== config.serviceApiKey) {
    logger.authz('Access denied - invalid service API key', { path: req.path });
    return next(errors.unauthorized('Invalid service API key'));
  }

  // Mark request as coming from internal service
  req.isServiceRequest = true;
  next();
}

export default {
  optionalAuth,
  requireAuth,
  requireSuperAdmin,
  requireCommunityAdmin,
  requireServiceAuth,
};
