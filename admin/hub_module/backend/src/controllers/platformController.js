/**
 * Platform Controller - Platform-wide admin features
 */
import { query } from '../config/database.js';
import { errors } from '../middleware/errorHandler.js';
import { logger } from '../utils/logger.js';

/**
 * Get all users with pagination
 */
export async function getUsers(req, res, next) {
  try {
    const page = Math.max(1, parseInt(req.query.page || '1', 10));
    const limit = Math.min(100, Math.max(1, parseInt(req.query.limit || '25', 10)));
    const offset = (page - 1) * limit;
    const search = req.query.search || '';
    const platform = req.query.platform;

    let whereClause = 'WHERE 1=1';
    const params = [];
    let paramIndex = 1;

    if (search) {
      whereClause += ` AND (display_name ILIKE $${paramIndex} OR platform_user_id ILIKE $${paramIndex})`;
      params.push(`%${search}%`);
      paramIndex++;
    }

    if (platform) {
      whereClause += ` AND platform = $${paramIndex}`;
      params.push(platform);
      paramIndex++;
    }

    const countResult = await query(
      `SELECT COUNT(DISTINCT user_id) as count FROM community_members ${whereClause}`,
      params
    );
    const total = parseInt(countResult.rows[0]?.count || 0, 10);

    const result = await query(
      `SELECT DISTINCT ON (user_id) user_id, display_name, platform, platform_user_id,
              created_at, last_activity
       FROM community_members
       ${whereClause}
       ORDER BY user_id, last_activity DESC
       LIMIT $${paramIndex} OFFSET $${paramIndex + 1}`,
      [...params, limit, offset]
    );

    const users = result.rows.map(row => ({
      userId: row.user_id,
      displayName: row.display_name,
      platform: row.platform,
      platformUserId: row.platform_user_id,
      createdAt: row.created_at?.toISOString(),
      lastActivity: row.last_activity?.toISOString(),
    }));

    res.json({
      success: true,
      users,
      pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Get single user details
 */
export async function getUser(req, res, next) {
  try {
    const userId = parseInt(req.params.id, 10);

    if (isNaN(userId)) {
      return next(errors.badRequest('Invalid user ID'));
    }

    // Get user memberships
    const membershipsResult = await query(
      `SELECT cm.community_id, c.name as community_name, cm.role, cm.reputation,
              cm.platform, cm.platform_user_id, cm.display_name, cm.joined_at, cm.last_activity
       FROM community_members cm
       JOIN communities c ON c.id = cm.community_id
       WHERE cm.user_id = $1 AND cm.is_active = true`,
      [userId]
    );

    if (membershipsResult.rows.length === 0) {
      return next(errors.notFound('User not found'));
    }

    const firstMembership = membershipsResult.rows[0];
    const memberships = membershipsResult.rows.map(row => ({
      communityId: row.community_id,
      communityName: row.community_name,
      role: row.role,
      reputationScore: row.reputation || 0,
      joinedAt: row.joined_at?.toISOString(),
      lastActivity: row.last_activity?.toISOString(),
    }));

    // Check for platform admin role
    const adminResult = await query(
      `SELECT role FROM platform_admins WHERE user_id = $1 AND is_active = true`,
      [userId]
    );

    res.json({
      success: true,
      user: {
        userId,
        displayName: firstMembership.display_name,
        platform: firstMembership.platform,
        platformUserId: firstMembership.platform_user_id,
        isPlatformAdmin: adminResult.rows.length > 0,
        platformRole: adminResult.rows[0]?.role || null,
        memberships,
      },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Update user platform role
 */
export async function updateUserRole(req, res, next) {
  try {
    const userId = parseInt(req.params.id, 10);
    const { role } = req.body;

    if (isNaN(userId)) {
      return next(errors.badRequest('Invalid user ID'));
    }

    const validRoles = ['platform-admin', 'support', null];
    if (!validRoles.includes(role)) {
      return next(errors.badRequest('Invalid role'));
    }

    if (role === null) {
      // Remove platform admin
      await query(
        `UPDATE platform_admins SET is_active = false, deactivated_at = NOW()
         WHERE user_id = $1`,
        [userId]
      );
    } else {
      // Upsert platform admin
      await query(
        `INSERT INTO platform_admins (user_id, role)
         VALUES ($1, $2)
         ON CONFLICT (user_id) DO UPDATE SET role = $2, is_active = true, updated_at = NOW()`,
        [userId, role]
      );
    }

    logger.audit('User platform role updated', {
      adminId: req.user.id,
      targetUserId: userId,
      newRole: role,
    });

    res.json({ success: true, message: 'User role updated' });
  } catch (err) {
    next(err);
  }
}

/**
 * Deactivate user across all communities
 */
export async function deactivateUser(req, res, next) {
  try {
    const userId = parseInt(req.params.id, 10);
    const { reason } = req.body;

    if (isNaN(userId)) {
      return next(errors.badRequest('Invalid user ID'));
    }

    // Deactivate all memberships
    await query(
      `UPDATE community_members
       SET is_active = false, removed_at = NOW(), removed_by = $1, removal_reason = $2
       WHERE user_id = $3`,
      [req.user.id, reason || 'Platform admin action', userId]
    );

    // Deactivate platform admin if exists
    await query(
      `UPDATE platform_admins SET is_active = false, deactivated_at = NOW()
       WHERE user_id = $1`,
      [userId]
    );

    // Invalidate all sessions
    await query(
      `UPDATE hub_sessions SET is_active = false, revoked_at = NOW()
       WHERE user_id = $1`,
      [userId]
    );

    logger.audit('User deactivated platform-wide', {
      adminId: req.user.id,
      targetUserId: userId,
      reason,
    });

    res.json({ success: true, message: 'User deactivated' });
  } catch (err) {
    next(err);
  }
}

/**
 * Get all communities
 */
export async function getCommunities(req, res, next) {
  try {
    const page = Math.max(1, parseInt(req.query.page || '1', 10));
    const limit = Math.min(100, Math.max(1, parseInt(req.query.limit || '25', 10)));
    const offset = (page - 1) * limit;
    const search = req.query.search || '';
    const isActive = req.query.isActive !== 'false';

    let whereClause = `WHERE is_active = $1 AND id > 0`; // excludes sentinel community 0
    const params = [isActive];
    let paramIndex = 2;

    if (search) {
      whereClause += ` AND (name ILIKE $${paramIndex} OR display_name ILIKE $${paramIndex})`;
      params.push(`%${search}%`);
      paramIndex++;
    }

    const countResult = await query(
      `SELECT COUNT(*) as count FROM communities ${whereClause}`,
      params
    );
    const total = parseInt(countResult.rows[0]?.count || 0, 10);

    const result = await query(
      `SELECT id, name, display_name, description, primary_platform,
              member_count, is_public, is_active, created_at
       FROM communities
       ${whereClause}
       ORDER BY member_count DESC, created_at DESC
       LIMIT $${paramIndex} OFFSET $${paramIndex + 1}`,
      [...params, limit, offset]
    );

    const communities = result.rows.map(row => ({
      id: row.id,
      name: row.name,
      displayName: row.display_name || row.name,
      description: row.description,
      primaryPlatform: row.primary_platform,
      memberCount: row.member_count || 0,
      isPublic: row.is_public,
      isActive: row.is_active,
      createdAt: row.created_at?.toISOString(),
    }));

    res.json({
      success: true,
      communities,
      pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Get single community details (admin view)
 */
export async function getCommunity(req, res, next) {
  try {
    const communityId = parseInt(req.params.id, 10);

    if (isNaN(communityId)) {
      return next(errors.badRequest('Invalid community ID'));
    }

    const communityResult = await query(
      `SELECT * FROM communities WHERE id = $1`,
      [communityId]
    );

    if (communityResult.rows.length === 0) {
      return next(errors.notFound('Community not found'));
    }

    const community = communityResult.rows[0];

    // Get owner info
    const ownerResult = await query(
      `SELECT user_id, display_name, platform, platform_user_id
       FROM community_members
       WHERE community_id = $1 AND role = 'community-owner' AND is_active = true
       LIMIT 1`,
      [communityId]
    );

    // Get module count
    const moduleResult = await query(
      `SELECT COUNT(*) as count FROM module_installations WHERE community_id = $1`,
      [communityId]
    );

    // Get domain count
    const domainResult = await query(
      `SELECT COUNT(*) as count FROM community_domains WHERE community_id = $1 AND is_active = true`,
      [communityId]
    );

    res.json({
      success: true,
      community: {
        id: community.id,
        name: community.name,
        displayName: community.display_name || community.name,
        description: community.description,
        logoUrl: community.logo_url,
        bannerUrl: community.banner_url,
        primaryPlatform: community.primary_platform,
        memberCount: community.member_count || 0,
        isPublic: community.is_public,
        isActive: community.is_active,
        createdAt: community.created_at?.toISOString(),
        owner: ownerResult.rows[0] ? {
          userId: ownerResult.rows[0].user_id,
          displayName: ownerResult.rows[0].display_name,
          platform: ownerResult.rows[0].platform,
        } : null,
        moduleCount: parseInt(moduleResult.rows[0]?.count || 0, 10),
        domainCount: parseInt(domainResult.rows[0]?.count || 0, 10),
      },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Update community (admin override)
 */
export async function updateCommunity(req, res, next) {
  try {
    const communityId = parseInt(req.params.id, 10);
    const { displayName, description, isPublic, isActive } = req.body;

    if (isNaN(communityId)) {
      return next(errors.badRequest('Invalid community ID'));
    }

    const updates = [];
    const params = [communityId];
    let paramIndex = 2;

    if (displayName !== undefined) {
      updates.push(`display_name = $${paramIndex++}`);
      params.push(displayName);
    }
    if (description !== undefined) {
      updates.push(`description = $${paramIndex++}`);
      params.push(description);
    }
    if (isPublic !== undefined) {
      updates.push(`is_public = $${paramIndex++}`);
      params.push(isPublic);
    }
    if (isActive !== undefined) {
      updates.push(`is_active = $${paramIndex++}`);
      params.push(isActive);
    }

    if (updates.length === 0) {
      return next(errors.badRequest('No updates provided'));
    }

    updates.push('updated_at = NOW()');

    await query(
      `UPDATE communities SET ${updates.join(', ')} WHERE id = $1`,
      params
    );

    logger.audit('Community updated by platform admin', {
      adminId: req.user.id,
      communityId,
      updates: Object.keys(req.body),
    });

    res.json({ success: true, message: 'Community updated' });
  } catch (err) {
    next(err);
  }
}

/**
 * Deactivate community
 */
export async function deactivateCommunity(req, res, next) {
  try {
    const communityId = parseInt(req.params.id, 10);
    const { reason } = req.body;

    if (isNaN(communityId)) {
      return next(errors.badRequest('Invalid community ID'));
    }

    await query(
      `UPDATE communities
       SET is_active = false, deactivated_at = NOW(), deactivation_reason = $1
       WHERE id = $2`,
      [reason || 'Platform admin action', communityId]
    );

    logger.audit('Community deactivated', {
      adminId: req.user.id,
      communityId,
      reason,
    });

    res.json({ success: true, message: 'Community deactivated' });
  } catch (err) {
    next(err);
  }
}

/**
 * Get system health status
 */
export async function getSystemHealth(req, res, next) {
  try {
    const checks = {
      database: false,
      timestamp: new Date().toISOString(),
    };

    // Database check
    try {
      await query('SELECT 1');
      checks.database = true;
    } catch {
      checks.database = false;
    }

    const healthy = Object.values(checks).every(v => v === true || typeof v === 'string');

    res.status(healthy ? 200 : 503).json({
      success: healthy,
      status: healthy ? 'healthy' : 'degraded',
      checks,
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Get module registry
 */
export async function getModuleRegistry(req, res, next) {
  try {
    const result = await query(
      `SELECT cm.module_name, cm.module_version, cm.platform, cm.endpoint_url,
              cm.status, cm.last_heartbeat, cm.created_at
       FROM collector_modules cm
       ORDER BY cm.module_name ASC`
    );

    const modules = result.rows.map(row => ({
      moduleName: row.module_name,
      moduleVersion: row.module_version,
      platform: row.platform,
      endpointUrl: row.endpoint_url,
      status: row.status,
      lastHeartbeat: row.last_heartbeat?.toISOString(),
      createdAt: row.created_at?.toISOString(),
    }));

    res.json({ success: true, modules });
  } catch (err) {
    next(err);
  }
}

/**
 * Get audit log
 */
export async function getAuditLog(req, res, next) {
  try {
    const page = Math.max(1, parseInt(req.query.page || '1', 10));
    const limit = Math.min(100, Math.max(1, parseInt(req.query.limit || '50', 10)));
    const offset = (page - 1) * limit;
    const action = req.query.action;
    const userId = req.query.userId ? parseInt(req.query.userId, 10) : null;

    let whereClause = 'WHERE 1=1';
    const params = [];
    let paramIndex = 1;

    if (action) {
      whereClause += ` AND action = $${paramIndex++}`;
      params.push(action);
    }

    if (userId) {
      whereClause += ` AND user_id = $${paramIndex++}`;
      params.push(userId);
    }

    const countResult = await query(
      `SELECT COUNT(*) as count FROM audit_log ${whereClause}`,
      params
    );
    const total = parseInt(countResult.rows[0]?.count || 0, 10);

    const result = await query(
      `SELECT id, user_id, action, target_type, target_id, details, ip_address, created_at
       FROM audit_log
       ${whereClause}
       ORDER BY created_at DESC
       LIMIT $${paramIndex} OFFSET $${paramIndex + 1}`,
      [...params, limit, offset]
    );

    const entries = result.rows.map(row => ({
      id: row.id,
      userId: row.user_id,
      action: row.action,
      targetType: row.target_type,
      targetId: row.target_id,
      details: row.details,
      ipAddress: row.ip_address,
      createdAt: row.created_at?.toISOString(),
    }));

    res.json({
      success: true,
      entries,
      pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Get platform statistics
 */
export async function getStats(req, res, next) {
  try {
    // User stats
    const userStats = await query(`
      SELECT
        COUNT(DISTINCT user_id) as total_users,
        COUNT(DISTINCT CASE WHEN last_activity > NOW() - INTERVAL '7 days' THEN user_id END) as active_7d,
        COUNT(DISTINCT CASE WHEN last_activity > NOW() - INTERVAL '30 days' THEN user_id END) as active_30d
      FROM community_members
      WHERE is_active = true
    `);

    // Community stats
    const communityStats = await query(`
      SELECT
        COUNT(*) as total,
        COUNT(CASE WHEN is_public = true THEN 1 END) as public,
        SUM(member_count) as total_members
      FROM communities
      WHERE is_active = true
    `);

    // Platform breakdown
    const platformStats = await query(`
      SELECT platform, COUNT(DISTINCT user_id) as users
      FROM community_members
      WHERE is_active = true
      GROUP BY platform
    `);

    // Session stats
    const sessionStats = await query(`
      SELECT
        COUNT(*) as total_sessions,
        COUNT(CASE WHEN is_active = true THEN 1 END) as active_sessions
      FROM hub_sessions
      WHERE created_at > NOW() - INTERVAL '24 hours'
    `);

    res.json({
      success: true,
      stats: {
        users: {
          total: parseInt(userStats.rows[0]?.total_users || 0, 10),
          active7d: parseInt(userStats.rows[0]?.active_7d || 0, 10),
          active30d: parseInt(userStats.rows[0]?.active_30d || 0, 10),
        },
        communities: {
          total: parseInt(communityStats.rows[0]?.total || 0, 10),
          public: parseInt(communityStats.rows[0]?.public || 0, 10),
          totalMembers: parseInt(communityStats.rows[0]?.total_members || 0, 10),
        },
        platforms: Object.fromEntries(
          platformStats.rows.map(row => [row.platform, parseInt(row.users, 10)])
        ),
        sessions: {
          last24h: parseInt(sessionStats.rows[0]?.total_sessions || 0, 10),
          active: parseInt(sessionStats.rows[0]?.active_sessions || 0, 10),
        },
        timestamp: new Date().toISOString(),
      },
    });
  } catch (err) {
    next(err);
  }
}
