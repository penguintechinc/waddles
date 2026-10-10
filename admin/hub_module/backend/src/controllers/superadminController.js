/**
 * Super Admin Controller - Global admin features for managing all communities
 */
import { query, transaction } from '../config/database.js';
import { errors } from '../middleware/errorHandler.js';
import { logger } from '../utils/logger.js';
import crypto from 'crypto';

// Valid community types for validation
const VALID_COMMUNITY_TYPES = ['shared_interest_group', 'gaming', 'creator', 'corporate', 'other'];

/**
 * List all communities with pagination and filtering
 */
export async function listCommunities(req, res, next) {
  try {
    const page = Math.max(1, parseInt(req.query.page || '1', 10));
    const limit = Math.min(100, Math.max(1, parseInt(req.query.limit || '25', 10)));
    const offset = (page - 1) * limit;
    const search = req.query.search || '';
    const platform = req.query.platform;
    const isActive = req.query.isActive;

    let whereClause = 'WHERE id > 0'; // excludes tenant-wide sentinel community (id 0)
    const params = [];
    let paramIndex = 1;

    if (search) {
      whereClause += ` AND (name ILIKE $${paramIndex} OR display_name ILIKE $${paramIndex})`;
      params.push(`%${search}%`);
      paramIndex++;
    }

    if (platform) {
      whereClause += ` AND platform = $${paramIndex}`;
      params.push(platform);
      paramIndex++;
    }

    if (isActive !== undefined) {
      whereClause += ` AND is_active = $${paramIndex}`;
      params.push(isActive === 'true');
      paramIndex++;
    }

    const countResult = await query(
      `SELECT COUNT(*) as count FROM communities ${whereClause}`,
      params
    );
    const total = parseInt(countResult.rows[0]?.count || 0, 10);

    const result = await query(
      `SELECT id, name, display_name, description, platform, platform_server_id,
              owner_id, owner_name, member_count, is_active, is_public, community_type,
              is_premium, seat_limit, created_at, updated_at
       FROM communities
       ${whereClause}
       ORDER BY created_at DESC
       LIMIT $${paramIndex} OFFSET $${paramIndex + 1}`,
      [...params, limit, offset]
    );

    const communities = result.rows.map(row => ({
      id: row.id,
      name: row.name,
      displayName: row.display_name || row.name,
      description: row.description,
      platform: row.platform,
      platformServerId: row.platform_server_id,
      ownerId: row.owner_id,
      ownerName: row.owner_name,
      memberCount: row.member_count || 0,
      isActive: row.is_active,
      isPublic: row.is_public,
      communityType: row.community_type || 'creator',
      isPremium: row.is_premium || false,
      seatLimit: row.seat_limit ?? null,
      createdAt: row.created_at?.toISOString(),
      updatedAt: row.updated_at?.toISOString(),
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
 * Get single community details
 */
export async function getCommunity(req, res, next) {
  try {
    const communityId = parseInt(req.params.id, 10);

    const result = await query(
      `SELECT id, name, display_name, description, platform, platform_server_id,
              owner_id, owner_name, member_count, is_active, is_public, community_type, config,
              created_at, updated_at
       FROM communities WHERE id = $1`,
      [communityId]
    );

    if (result.rows.length === 0) {
      return next(errors.notFound('Community not found'));
    }

    const row = result.rows[0];
    res.json({
      success: true,
      community: {
        id: row.id,
        name: row.name,
        displayName: row.display_name || row.name,
        description: row.description,
        platform: row.platform,
        platformServerId: row.platform_server_id,
        ownerId: row.owner_id,
        ownerName: row.owner_name,
        memberCount: row.member_count || 0,
        isActive: row.is_active,
        isPublic: row.is_public,
        communityType: row.community_type || 'creator',
        config: row.config,
        createdAt: row.created_at?.toISOString(),
        updatedAt: row.updated_at?.toISOString(),
      },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Create a new community
 */
export async function createCommunity(req, res, next) {
  try {
    const { name, displayName, description, platform, platformServerId, ownerId, ownerName, isPublic, communityType } = req.body;

    if (!name || !platform) {
      return next(errors.badRequest('Name and platform are required'));
    }

    // Validate community type (default to 'creator' if not provided)
    const validatedCommunityType = communityType || 'creator';
    if (!VALID_COMMUNITY_TYPES.includes(validatedCommunityType)) {
      return next(errors.badRequest(`Invalid community type. Must be one of: ${VALID_COMMUNITY_TYPES.join(', ')}`));
    }

    // Validate ownerId is a number if provided
    const parsedOwnerId = ownerId ? parseInt(ownerId, 10) : null;
    if (ownerId && isNaN(parsedOwnerId)) {
      // If ownerId is not a number, it might be a username - use it as ownerName instead
      req.body.ownerName = req.body.ownerName || ownerId;
      req.body.ownerId = null;
    }

    // Check if community name already exists
    const existingResult = await query(
      'SELECT id FROM communities WHERE name = $1',
      [name.toLowerCase().replace(/\s+/g, '-')]
    );

    if (existingResult.rows.length > 0) {
      return next(errors.conflict('Community name already exists'));
    }

    // Use transaction to ensure community and owner membership are created together
    const finalOwnerId = parsedOwnerId || null;
    const finalOwnerName = req.body.ownerName || ownerName || null;

    const result = await transaction(async (client) => {
      // Create the community
      const communityResult = await client.query(
        `INSERT INTO communities
         (name, display_name, description, platform, platform_server_id, owner_id, owner_name, is_public, community_type, member_count, created_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 1, $10)
         RETURNING id, name, display_name, platform, community_type, created_at`,
        [
          name.toLowerCase().replace(/\s+/g, '-'),
          displayName || name,
          description || '',
          platform,
          platformServerId || null,
          finalOwnerId,
          finalOwnerName,
          isPublic !== false,
          validatedCommunityType,
          req.user.id,
        ]
      );

      const newCommunity = communityResult.rows[0];

      // Add creator as community-owner member (reputation starts at 600 like credit score)
      await client.query(
        `INSERT INTO community_members
         (community_id, user_id, role, reputation, is_active, joined_at)
         VALUES ($1, $2, 'owner', 600, true, NOW())`,
        [
          newCommunity.id,
          req.user.id || null,
        ]
      );

      return newCommunity;
    });

    logger.audit('Community created', {
      adminId: req.user.id,
      communityId: result.id,
      name: result.name,
      communityType: result.community_type,
    });

    res.status(201).json({
      success: true,
      community: {
        id: result.id,
        name: result.name,
        displayName: result.display_name,
        platform: result.platform,
        communityType: result.community_type,
        createdAt: result.created_at?.toISOString(),
      },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Update community details
 */
export async function updateCommunity(req, res, next) {
  try {
    const communityId = parseInt(req.params.id, 10);
    const { displayName, description, ownerId, ownerName, isActive, isPublic, platform, platformServerId, communityType, isPremium, seatLimit } = req.body;

    // Check community exists
    const existingResult = await query(
      'SELECT id FROM communities WHERE id = $1',
      [communityId]
    );

    if (existingResult.rows.length === 0) {
      return next(errors.notFound('Community not found'));
    }

    // Validate community type if provided
    if (communityType !== undefined && !VALID_COMMUNITY_TYPES.includes(communityType)) {
      return next(errors.badRequest(`Invalid community type. Must be one of: ${VALID_COMMUNITY_TYPES.join(', ')}`));
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
    if (ownerId !== undefined) {
      updates.push(`owner_id = $${paramIndex++}`);
      params.push(ownerId);
    }
    if (ownerName !== undefined) {
      updates.push(`owner_name = $${paramIndex++}`);
      params.push(ownerName);
    }
    if (isActive !== undefined) {
      updates.push(`is_active = $${paramIndex++}`);
      params.push(isActive);
    }
    if (isPublic !== undefined) {
      updates.push(`is_public = $${paramIndex++}`);
      params.push(isPublic);
    }
    if (platform !== undefined) {
      updates.push(`platform = $${paramIndex++}`);
      params.push(platform);
    }
    if (platformServerId !== undefined) {
      updates.push(`platform_server_id = $${paramIndex++}`);
      params.push(platformServerId);
    }
    if (communityType !== undefined) {
      updates.push(`community_type = $${paramIndex++}`);
      params.push(communityType);
    }
    if (isPremium !== undefined) {
      updates.push(`is_premium = $${paramIndex++}`);
      params.push(isPremium);
    }
    if (seatLimit !== undefined) {
      updates.push(`seat_limit = $${paramIndex++}`);
      params.push(seatLimit === '' || seatLimit === null ? null : parseInt(seatLimit, 10));
    }

    if (updates.length === 0) {
      return next(errors.badRequest('No updates provided'));
    }

    updates.push('updated_at = NOW()');

    await query(
      `UPDATE communities SET ${updates.join(', ')} WHERE id = $1`,
      params
    );

    logger.audit('Community updated', {
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
 * Delete (deactivate) a community
 */
export async function deleteCommunity(req, res, next) {
  try {
    const communityId = parseInt(req.params.id, 10);

    // Check if this is a global community (cannot be deleted)
    const globalCheck = await query(
      "SELECT config->>'is_global' as is_global FROM communities WHERE id = $1",
      [communityId]
    );

    if (globalCheck.rows.length > 0 && globalCheck.rows[0].is_global === 'true') {
      return next(errors.forbidden('Global communities cannot be deleted'));
    }

    const result = await query(
      `UPDATE communities SET is_active = false, deleted_at = NOW(), deleted_by = $1
       WHERE id = $2 RETURNING name`,
      [req.user.id, communityId]
    );

    if (result.rows.length === 0) {
      return next(errors.notFound('Community not found'));
    }

    logger.audit('Community deleted', {
      adminId: req.user.id,
      communityId,
      name: result.rows[0].name,
    });

    res.json({ success: true, message: 'Community deleted' });
  } catch (err) {
    next(err);
  }
}

/**
 * Reassign community ownership
 */
export async function reassignOwner(req, res, next) {
  try {
    const communityId = parseInt(req.params.id, 10);
    const { newOwnerId, newOwnerName } = req.body;

    if (!newOwnerName) {
      return next(errors.badRequest('New owner name is required'));
    }

    // Get current owner info for audit
    const currentResult = await query(
      'SELECT owner_id, owner_name FROM communities WHERE id = $1',
      [communityId]
    );

    if (currentResult.rows.length === 0) {
      return next(errors.notFound('Community not found'));
    }

    const previousOwner = currentResult.rows[0];

    // Update owner
    await query(
      `UPDATE communities
       SET owner_id = $1, owner_name = $2, updated_at = NOW()
       WHERE id = $3`,
      [newOwnerId || null, newOwnerName, communityId]
    );

    logger.audit('Community ownership reassigned', {
      adminId: req.user.id,
      communityId,
      previousOwnerId: previousOwner.owner_id,
      previousOwnerName: previousOwner.owner_name,
      newOwnerId,
      newOwnerName,
    });

    res.json({ success: true, message: 'Ownership reassigned' });
  } catch (err) {
    next(err);
  }
}

/**
 * Get dashboard stats for super admin
 */
export async function getDashboardStats(req, res, next) {
  try {
    const statsResult = await query(`
      SELECT
        COUNT(*) as total_communities,
        COUNT(CASE WHEN is_active = true THEN 1 END) as active_communities,
        COUNT(CASE WHEN platform = 'discord' THEN 1 END) as discord_communities,
        COUNT(CASE WHEN platform = 'twitch' THEN 1 END) as twitch_communities,
        COUNT(CASE WHEN platform = 'slack' THEN 1 END) as slack_communities,
        COALESCE(SUM(member_count), 0) as total_members
      FROM communities
    `);

    const adminResult = await query(`
      SELECT COUNT(*) as admin_count FROM hub_admins WHERE is_active = true
    `);

    const recentResult = await query(`
      SELECT id, name, display_name, platform, created_at
      FROM communities
      ORDER BY created_at DESC
      LIMIT 5
    `);

    res.json({
      success: true,
      stats: {
        totalCommunities: parseInt(statsResult.rows[0]?.total_communities || 0, 10),
        activeCommunities: parseInt(statsResult.rows[0]?.active_communities || 0, 10),
        platformBreakdown: {
          discord: parseInt(statsResult.rows[0]?.discord_communities || 0, 10),
          twitch: parseInt(statsResult.rows[0]?.twitch_communities || 0, 10),
          slack: parseInt(statsResult.rows[0]?.slack_communities || 0, 10),
        },
        totalMembers: parseInt(statsResult.rows[0]?.total_members || 0, 10),
        adminCount: parseInt(adminResult.rows[0]?.admin_count || 0, 10),
      },
      recentCommunities: recentResult.rows.map(row => ({
        id: row.id,
        name: row.name,
        displayName: row.display_name || row.name,
        platform: row.platform,
        createdAt: row.created_at?.toISOString(),
      })),
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Module Registry - Super Admin Only
 */

/**
 * Get all modules (including unpublished)
 * GET /api/v1/superadmin/marketplace/modules
 */
export async function getAllModules(req, res, next) {
  try {
    const page = Math.max(1, parseInt(req.query.page || '1', 10));
    const limit = Math.min(100, Math.max(1, parseInt(req.query.limit || '25', 10)));
    const offset = (page - 1) * limit;
    const search = req.query.search || '';
    const category = req.query.category;
    const isPublished = req.query.isPublished;

    let whereClause = 'WHERE 1=1';
    const params = [];
    let paramIndex = 1;

    if (search) {
      whereClause += ` AND (name ILIKE $${paramIndex} OR display_name ILIKE $${paramIndex})`;
      params.push(`%${search}%`);
      paramIndex++;
    }

    if (category) {
      whereClause += ` AND category = $${paramIndex}`;
      params.push(category);
      paramIndex++;
    }

    if (isPublished !== undefined) {
      whereClause += ` AND is_published = $${paramIndex}`;
      params.push(isPublished === 'true');
      paramIndex++;
    }

    const countResult = await query(
      `SELECT COUNT(*) as count FROM hub_modules ${whereClause}`,
      params
    );
    const total = parseInt(countResult.rows[0]?.count || 0, 10);

    const result = await query(
      `SELECT
        m.id, m.name, m.display_name, m.description, m.version,
        m.author, m.category, m.icon_url, m.is_published, m.is_core, m.created_at,
        COALESCE(AVG(r.rating), 0) as avg_rating,
        COUNT(DISTINCT r.id) as review_count,
        COUNT(DISTINCT i.id) as install_count
       FROM hub_modules m
       LEFT JOIN hub_module_reviews r ON r.module_id = m.id
       LEFT JOIN hub_module_installations i ON i.module_id = m.id
       ${whereClause}
       GROUP BY m.id
       ORDER BY m.is_core DESC, m.created_at DESC
       LIMIT $${paramIndex} OFFSET $${paramIndex + 1}`,
      [...params, limit, offset]
    );

    const modules = result.rows.map(row => ({
      id: row.id,
      name: row.name,
      displayName: row.display_name || row.name,
      description: row.description,
      version: row.version,
      author: row.author,
      category: row.category,
      iconUrl: row.icon_url,
      isPublished: row.is_published,
      isCore: row.is_core,
      avgRating: parseFloat(row.avg_rating || 0).toFixed(1),
      reviewCount: parseInt(row.review_count || 0, 10),
      installCount: parseInt(row.install_count || 0, 10),
      createdAt: row.created_at?.toISOString(),
    }));

    res.json({
      success: true,
      modules,
      pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Create a new module
 * POST /api/v1/superadmin/marketplace/modules
 */
export async function createModule(req, res, next) {
  try {
    const { name, displayName, description, version, author, category, iconUrl, isCore, configSchema } = req.body;

    if (!name) {
      return next(errors.badRequest('Module name is required'));
    }

    // Check if module name already exists
    const existingResult = await query(
      'SELECT id FROM hub_modules WHERE name = $1',
      [name]
    );

    if (existingResult.rows.length > 0) {
      return next(errors.conflict('Module name already exists'));
    }

    const result = await query(
      `INSERT INTO hub_modules
       (name, display_name, description, version, author, category, icon_url, is_core, config_schema)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
       RETURNING id, name, display_name, created_at`,
      [
        name,
        displayName || name,
        description || '',
        version || '1.0.0',
        author || 'Waddles',
        category || 'general',
        iconUrl || null,
        isCore || false,
        JSON.stringify(configSchema || {}),
      ]
    );

        // Determine module type and permission template from category
    const moduleTypeMap = {
      'general': { type: 'interactive', template: 'interactive_standard' },
      'moderation': { type: 'core', template: 'core_broad' },
      'entertainment': { type: 'interactive', template: 'interactive_standard' },
      'music': { type: 'interactive', template: 'interactive_standard' },
      'utility': { type: 'core', template: 'core_broad' },
      'games': { type: 'interactive', template: 'interactive_standard' },
      'ai': { type: 'core', template: 'core_broad' },
    };
    const moduleCategory = category || 'general';
    const typeInfo = moduleTypeMap[moduleCategory] || moduleTypeMap['general'];

    // Generate a cryptographically secure password for the DB user
    const dbPassword = crypto.randomBytes(32).toString('base64url');

    // Provision scoped database account
    let dbAccount = null;
    try {
      const provisionResult = await query(
        `SELECT * FROM provision_module_db_account($1, $2, $3, $4, $5, $6, $7, $8)`,
        [
          name,
          typeInfo.type,
          typeInfo.template,
          dbPassword,
          null, // owned_tables - can be configured later
          null, // readable_tables - can be configured later
          null, // custom_grants
          req.user.id,
        ]
      );
      dbAccount = provisionResult.rows[0];
      if (!dbAccount?.success) {
        logger.warn('Module DB account provisioning failed', {
          moduleName: name,
          message: dbAccount?.message,
        });
      }
    } catch (dbErr) {
      // Log but don't fail module creation - DB account can be provisioned later
      logger.warn('Module DB account provisioning error', {
        moduleName: name,
        error: dbErr.message,
      });
    }

    logger.audit('Module created', {
      adminId: req.user.id,
      moduleId: result.rows[0].id,
      name: result.rows[0].name,
      dbUsername: dbAccount?.db_username || null,
    });

    res.status(201).json({
      success: true,
      module: {
        id: result.rows[0].id,
        name: result.rows[0].name,
        displayName: result.rows[0].display_name,
        createdAt: result.rows[0].created_at?.toISOString(),
        dbAccount: dbAccount?.success ? {
          username: dbAccount.db_username,
          provisioned: true,
        } : {
          provisioned: false,
          message: dbAccount?.message || 'Provisioning skipped',
        },
      },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Update a module
 * PUT /api/v1/superadmin/marketplace/modules/:id
 */
export async function updateModule(req, res, next) {
  try {
    const moduleId = parseInt(req.params.id, 10);
    const { displayName, description, version, author, category, iconUrl, isCore, configSchema } = req.body;

    // Check module exists
    const existingResult = await query(
      'SELECT id FROM hub_modules WHERE id = $1',
      [moduleId]
    );

    if (existingResult.rows.length === 0) {
      return next(errors.notFound('Module not found'));
    }

    const updates = [];
    const params = [moduleId];
    let paramIndex = 2;

    if (displayName !== undefined) {
      updates.push(`display_name = $${paramIndex++}`);
      params.push(displayName);
    }
    if (description !== undefined) {
      updates.push(`description = $${paramIndex++}`);
      params.push(description);
    }
    if (version !== undefined) {
      updates.push(`version = $${paramIndex++}`);
      params.push(version);
    }
    if (author !== undefined) {
      updates.push(`author = $${paramIndex++}`);
      params.push(author);
    }
    if (category !== undefined) {
      updates.push(`category = $${paramIndex++}`);
      params.push(category);
    }
    if (iconUrl !== undefined) {
      updates.push(`icon_url = $${paramIndex++}`);
      params.push(iconUrl);
    }
    if (isCore !== undefined) {
      updates.push(`is_core = $${paramIndex++}`);
      params.push(isCore);
    }
    if (configSchema !== undefined) {
      updates.push(`config_schema = $${paramIndex++}::jsonb`);
      params.push(JSON.stringify(configSchema));
    }

    if (updates.length === 0) {
      return next(errors.badRequest('No updates provided'));
    }

    updates.push('updated_at = NOW()');

    await query(
      `UPDATE hub_modules SET ${updates.join(', ')} WHERE id = $1`,
      params
    );

    logger.audit('Module updated', {
      adminId: req.user.id,
      moduleId,
      updates: Object.keys(req.body),
    });

    res.json({ success: true, message: 'Module updated' });
  } catch (err) {
    next(err);
  }
}

/**
 * Publish/unpublish a module
 * PUT /api/v1/superadmin/marketplace/modules/:id/publish
 */
export async function publishModule(req, res, next) {
  try {
    const moduleId = parseInt(req.params.id, 10);
    const { isPublished } = req.body;

    if (isPublished === undefined) {
      return next(errors.badRequest('isPublished field is required'));
    }

    const result = await query(
      `UPDATE hub_modules SET is_published = $1, updated_at = NOW()
       WHERE id = $2 RETURNING name`,
      [isPublished, moduleId]
    );

    if (result.rows.length === 0) {
      return next(errors.notFound('Module not found'));
    }

    logger.audit('Module publication status changed', {
      adminId: req.user.id,
      moduleId,
      moduleName: result.rows[0].name,
      isPublished,
    });

    res.json({
      success: true,
      message: isPublished ? 'Module published' : 'Module unpublished',
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Delete a module
 * DELETE /api/v1/superadmin/marketplace/modules/:id
 */
export async function deleteModule(req, res, next) {
  try {
    const moduleId = parseInt(req.params.id, 10);

    // Check for existing installations
    const installCheck = await query(
      'SELECT COUNT(*) as count FROM hub_module_installations WHERE module_id = $1',
      [moduleId]
    );

    const installCount = parseInt(installCheck.rows[0]?.count || 0, 10);
    if (installCount > 0) {
      return next(errors.badRequest(
        `Cannot delete module: ${installCount} installations exist. Unpublish instead.`
      ));
    }

    const result = await query(
      'DELETE FROM hub_modules WHERE id = $1 RETURNING name',
      [moduleId]
    );

    if (result.rows.length === 0) {
      return next(errors.notFound('Module not found'));
    }

    logger.audit('Module deleted', {
      adminId: req.user.id,
      moduleId,
      moduleName: result.rows[0].name,
    });

    res.json({ success: true, message: 'Module deleted' });
  } catch (err) {
    next(err);
  }
}

/**
 * Get all module database accounts
 * GET /api/v1/superadmin/module-db-accounts
 */
export async function getModuleDbAccounts(req, res, next) {
  try {
    const result = await query(
      `SELECT mda.id, mda.module_name, mda.db_username, mda.module_type,
              mda.permission_template, mda.is_active, mda.owned_tables,
              mda.readable_tables, mda.created_at, mda.updated_at,
              dpt.description as template_description
       FROM module_db_accounts mda
       LEFT JOIN db_permission_templates dpt ON dpt.template_name = mda.permission_template
       ORDER BY mda.module_type, mda.module_name`
    );

    const accounts = result.rows.map(row => ({
      id: row.id,
      moduleName: row.module_name,
      dbUsername: row.db_username,
      moduleType: row.module_type,
      permissionTemplate: row.permission_template,
      templateDescription: row.template_description,
      isActive: row.is_active,
      ownedTables: row.owned_tables,
      readableTables: row.readable_tables,
      createdAt: row.created_at?.toISOString(),
      updatedAt: row.updated_at?.toISOString(),
    }));

    res.json({ success: true, accounts });
  } catch (err) {
    next(err);
  }
}

/**
 * Rotate a module database password
 * POST /api/v1/superadmin/module-db-accounts/:moduleName/rotate
 */
export async function rotateModuleDbPassword(req, res, next) {
  try {
    const { moduleName } = req.params;
    const newPassword = crypto.randomBytes(32).toString('base64url');

    const result = await query(
      `SELECT * FROM rotate_module_db_password($1, $2)`,
      [moduleName, newPassword]
    );

    const outcome = result.rows[0];
    if (!outcome?.success) {
      return next(errors.badRequest(outcome?.message || 'Password rotation failed'));
    }

    logger.audit('Module DB password rotated', {
      adminId: req.user.id,
      moduleName,
    });

    res.json({
      success: true,
      message: outcome.message,
      // NOTE: Password is intentionally NOT returned in the response.
      // It should be stored in the secrets backend by a separate service call.
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Deactivate a module database account
 * POST /api/v1/superadmin/module-db-accounts/:moduleName/deactivate
 */
export async function deactivateModuleDbAccount(req, res, next) {
  try {
    const { moduleName } = req.params;

    const result = await query(
      `SELECT * FROM deactivate_module_db_account($1)`,
      [moduleName]
    );

    const outcome = result.rows[0];
    if (!outcome?.success) {
      return next(errors.badRequest(outcome?.message || 'Deactivation failed'));
    }

    logger.audit('Module DB account deactivated', {
      adminId: req.user.id,
      moduleName,
    });

    res.json({ success: true, message: outcome.message });
  } catch (err) {
    next(err);
  }
}

// ── Tenant Management ────────────────────────────────────────────────

/**
 * List all tenants with pagination
 */
export async function listTenants(req, res, next) {
  try {
    const page = Math.max(1, parseInt(req.query.page || '1', 10));
    const limit = Math.min(100, Math.max(1, parseInt(req.query.limit || '25', 10)));
    const offset = (page - 1) * limit;
    const search = req.query.search || '';

    let whereClause = 'WHERE 1=1';
    const params = [];
    let paramIndex = 1;

    if (search) {
      whereClause += ` AND (slug ILIKE $${paramIndex} OR display_name ILIKE $${paramIndex})`;
      params.push(`%${search}%`);
      paramIndex++;
    }

    const countResult = await query(`SELECT COUNT(*) as count FROM tenants ${whereClause}`, params);
    const total = parseInt(countResult.rows[0]?.count || 0, 10);

    const result = await query(
      `SELECT id, slug, display_name, description, logo_url, is_global, is_active,
              allowed_module_ids, seat_limit, created_at, updated_at
       FROM tenants ${whereClause}
       ORDER BY is_global DESC, created_at DESC
       LIMIT $${paramIndex} OFFSET $${paramIndex + 1}`,
      [...params, limit, offset]
    );

    const tenants = result.rows.map(row => ({
      id: row.id,
      slug: row.slug,
      displayName: row.display_name,
      description: row.description,
      logoUrl: row.logo_url,
      isGlobal: row.is_global,
      isActive: row.is_active,
      allowedModuleIds: row.allowed_module_ids,
      seatLimit: row.seat_limit,
      createdAt: row.created_at?.toISOString(),
      updatedAt: row.updated_at?.toISOString(),
    }));

    res.json({
      success: true,
      tenants,
      pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Create a new tenant
 */
export async function createTenant(req, res, next) {
  try {
    const { slug, displayName, description, logoUrl, seatLimit, allowedModuleIds } = req.body;

    if (!slug || !slug.trim()) return next(errors.badRequest('Tenant slug is required'));
    if (!displayName || !displayName.trim()) return next(errors.badRequest('Display name is required'));

    // Validate slug format (URL-safe)
    if (!/^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(slug)) {
      return next(errors.badRequest('Slug must be lowercase alphanumeric with hyphens, no leading/trailing hyphens'));
    }

    const result = await query(
      `INSERT INTO tenants (slug, display_name, description, logo_url, seat_limit, allowed_module_ids)
       VALUES ($1, $2, $3, $4, $5, $6)
       RETURNING id, slug, display_name, created_at`,
      [slug.trim(), displayName.trim(), description || '', logoUrl || null,
       seatLimit || null, allowedModuleIds || null]
    );

    logger.audit('Tenant created', { adminId: req.user.id, tenantId: result.rows[0].id, slug });

    res.status(201).json({
      success: true,
      tenant: {
        id: result.rows[0].id,
        slug: result.rows[0].slug,
        displayName: result.rows[0].display_name,
        createdAt: result.rows[0].created_at?.toISOString(),
      },
    });
  } catch (err) {
    if (err.code === '23505') return next(errors.conflict('A tenant with that slug already exists'));
    next(err);
  }
}

/**
 * Update a tenant
 */
export async function updateTenant(req, res, next) {
  try {
    const tenantId = parseInt(req.params.id, 10);
    const { displayName, description, logoUrl, isActive, seatLimit, allowedModuleIds, config } = req.body;

    const updates = [];
    const params = [tenantId];
    let idx = 2;

    if (displayName !== undefined) { updates.push(`display_name = $${idx++}`); params.push(displayName); }
    if (description !== undefined) { updates.push(`description = $${idx++}`); params.push(description); }
    if (logoUrl !== undefined) { updates.push(`logo_url = $${idx++}`); params.push(logoUrl); }
    if (isActive !== undefined) { updates.push(`is_active = $${idx++}`); params.push(isActive); }
    if (seatLimit !== undefined) { updates.push(`seat_limit = $${idx++}`); params.push(seatLimit === null ? null : parseInt(seatLimit, 10)); }
    if (allowedModuleIds !== undefined) { updates.push(`allowed_module_ids = $${idx++}`); params.push(allowedModuleIds); }
    if (config !== undefined) { updates.push(`config = $${idx++}::jsonb`); params.push(JSON.stringify(config)); }

    if (!updates.length) return next(errors.badRequest('No updates provided'));
    updates.push('updated_at = NOW()');

    const result = await query(`UPDATE tenants SET ${updates.join(', ')} WHERE id = $1 RETURNING slug`, params);
    if (!result.rows.length) return next(errors.notFound('Tenant not found'));

    logger.audit('Tenant updated', { adminId: req.user.id, tenantId, updates: Object.keys(req.body) });
    res.json({ success: true, message: 'Tenant updated' });
  } catch (err) {
    next(err);
  }
}

/**
 * Delete (deactivate) a tenant
 */
export async function deleteTenant(req, res, next) {
  try {
    const tenantId = parseInt(req.params.id, 10);

    // Cannot delete global tenant
    const globalCheck = await query('SELECT is_global FROM tenants WHERE id = $1', [tenantId]);
    if (!globalCheck.rows.length) return next(errors.notFound('Tenant not found'));
    if (globalCheck.rows[0].is_global) return next(errors.forbidden('Cannot delete the global tenant'));

    await query('UPDATE tenants SET is_active = false, updated_at = NOW() WHERE id = $1', [tenantId]);

    logger.audit('Tenant deleted (deactivated)', { adminId: req.user.id, tenantId });
    res.json({ success: true, message: 'Tenant deactivated' });
  } catch (err) {
    next(err);
  }
}
