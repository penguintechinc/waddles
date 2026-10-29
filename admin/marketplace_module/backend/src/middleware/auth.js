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
 * Require community admin role (owner, admin, or moderator).
 * The community id comes from the route param, or from the request body for
 * body-addressed routes (premium /subscribe and /cancel) — the same value the controller uses.
 */
export async function requireCommunityAdmin(req, res, next) {
  const communityId = parseInt(req.params?.communityId ?? req.body?.communityId, 10);

  if (isNaN(communityId)) {
    return next(errors.badRequest('Invalid community ID'));
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
