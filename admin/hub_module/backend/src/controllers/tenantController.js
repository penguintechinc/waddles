/**
 * Tenant Controller - Tenant admin CRUD for WaddleBot v2.0.0 multi-tenancy
 */
import { query } from '../config/database.js';
import { errors } from '../middleware/errorHandler.js';
import { logger } from '../utils/logger.js';

const VALID_TENANT_ADMIN_ROLES = ['tenant-admin', 'tenant-owner'];

/**
 * Resolve a tenant row by slug. Returns null if not found.
 */
async function getTenantBySlug(slug) {
  const result = await query(
    'SELECT id, slug, is_global FROM tenants WHERE slug = $1',
    [slug]
  );
  return result.rows[0] || null;
}

/**
 * Get tenant by slug
 * GET /:tenantSlug
 */
export async function getTenant(req, res, next) {
  try {
    const { tenantSlug } = req.params;

    const result = await query(
      `SELECT id, slug, display_name, description, logo_url, is_global,
              is_active, config, allowed_module_ids, seat_limit, created_at
       FROM tenants
       WHERE slug = $1`,
      [tenantSlug]
    );

    const row = result.rows[0];
    if (!row) {
      return next(errors.notFound('Tenant not found'));
    }

    res.json({
      success: true,
      tenant: {
        id: row.id,
        slug: row.slug,
        displayName: row.display_name,
        description: row.description,
        logoUrl: row.logo_url,
        isGlobal: row.is_global,
        isActive: row.is_active,
        config: row.config ?? {},
        allowedModuleIds: row.allowed_module_ids ?? null,
        seatLimit: row.seat_limit ?? null,
        createdAt: row.created_at?.toISOString(),
      },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Update tenant core fields
 * PUT /:tenantSlug
 */
export async function updateTenant(req, res, next) {
  try {
    const { tenantSlug } = req.params;
    const { displayName, description, logoUrl, config } = req.body;

    const tenant = await getTenantBySlug(tenantSlug);
    if (!tenant) {
      return next(errors.notFound('Tenant not found'));
    }

    const updates = [];
    const params = [tenant.id];
    let paramIndex = 2;

    if (displayName !== undefined) {
      updates.push(`display_name = $${paramIndex++}`);
      params.push(displayName);
    }
    if (description !== undefined) {
      updates.push(`description = $${paramIndex++}`);
      params.push(description);
    }
    if (logoUrl !== undefined) {
      updates.push(`logo_url = $${paramIndex++}`);
      params.push(logoUrl);
    }
    if (config !== undefined) {
      updates.push(`config = $${paramIndex++}`);
      params.push(JSON.stringify(config));
    }

    if (updates.length === 0) {
      return next(errors.badRequest('No updates provided'));
    }

    updates.push('updated_at = NOW()');

    await query(
      `UPDATE tenants SET ${updates.join(', ')} WHERE id = $1`,
      params
    );

    logger.audit('Tenant updated', {
      adminId: req.user?.id,
      tenantId: tenant.id,
      tenantSlug,
      updates: Object.keys(req.body),
    });

    res.json({ success: true, message: 'Tenant updated' });
  } catch (err) {
    next(err);
  }
}

/**
 * Get all settings for a tenant
 * GET /:tenantSlug/settings
 */
export async function getTenantSettings(req, res, next) {
  try {
    const { tenantSlug } = req.params;

    const tenant = await getTenantBySlug(tenantSlug);
    if (!tenant) {
      return next(errors.notFound('Tenant not found'));
    }

    const result = await query(
      'SELECT key, value FROM tenant_settings WHERE tenant_id = $1 ORDER BY key ASC',
      [tenant.id]
    );

    res.json({
      success: true,
      settings: result.rows.map(row => ({ key: row.key, value: row.value })),
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Upsert tenant settings
 * PUT /:tenantSlug/settings
 * Body: { settings: [{key, value}] }
 */
export async function updateTenantSettings(req, res, next) {
  try {
    const { tenantSlug } = req.params;
    const { settings } = req.body;

    if (!Array.isArray(settings) || settings.length === 0) {
      return next(errors.badRequest('settings must be a non-empty array of {key, value}'));
    }

    const tenant = await getTenantBySlug(tenantSlug);
    if (!tenant) {
      return next(errors.notFound('Tenant not found'));
    }

    for (const { key, value } of settings) {
      if (!key || typeof key !== 'string') {
        return next(errors.badRequest('Each setting must have a valid string key'));
      }
      await query(
        `INSERT INTO tenant_settings (tenant_id, key, value, updated_at)
         VALUES ($1, $2, $3, NOW())
         ON CONFLICT (tenant_id, key) DO UPDATE
           SET value = EXCLUDED.value, updated_at = NOW()`,
        [tenant.id, key, value]
      );
    }

    logger.audit('Tenant settings updated', {
      adminId: req.user?.id,
      tenantId: tenant.id,
      tenantSlug,
      keys: settings.map(s => s.key),
    });

    res.json({ success: true, message: 'Tenant settings updated' });
  } catch (err) {
    next(err);
  }
}

/**
 * List communities belonging to a tenant (paginated)
 * GET /:tenantSlug/communities
 */
export async function getTenantCommunities(req, res, next) {
  try {
    const { tenantSlug } = req.params;
    const page = Math.max(1, parseInt(req.query.page || '1', 10));
    const limit = Math.min(100, Math.max(1, parseInt(req.query.limit || '25', 10)));
    const offset = (page - 1) * limit;

    const tenant = await getTenantBySlug(tenantSlug);
    if (!tenant) {
      return next(errors.notFound('Tenant not found'));
    }

    const countResult = await query(
      'SELECT COUNT(*) AS count FROM communities WHERE tenant_id = $1 AND id > 0',
      [tenant.id]
    );
    const total = parseInt(countResult.rows[0]?.count || 0, 10);

    const result = await query(
      `SELECT id, name, display_name, member_count, is_active, is_public,
              community_type, created_at
       FROM communities
       WHERE tenant_id = $1 AND id > 0
       ORDER BY created_at DESC
       LIMIT $2 OFFSET $3`,
      [tenant.id, limit, offset]
    );

    const communities = result.rows.map(row => ({
      id: row.id,
      name: row.name,
      displayName: row.display_name || row.name,
      memberCount: row.member_count || 0,
      isActive: row.is_active,
      isPublic: row.is_public,
      communityType: row.community_type || 'other',
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
 * Get modules allowed for a tenant (joins hub_modules for full info)
 * GET /:tenantSlug/modules
 */
export async function getTenantModules(req, res, next) {
  try {
    const { tenantSlug } = req.params;

    const tenantResult = await query(
      'SELECT id, allowed_module_ids FROM tenants WHERE slug = $1',
      [tenantSlug]
    );
    const tenant = tenantResult.rows[0];
    if (!tenant) {
      return next(errors.notFound('Tenant not found'));
    }

    // null means all modules are permitted — return full published list
    if (tenant.allowed_module_ids === null) {
      const result = await query(
        `SELECT id, name, display_name, description, category, is_core,
                is_published, version, created_at
         FROM hub_modules
         WHERE is_published = true
         ORDER BY category ASC, display_name ASC`
      );
      return res.json({
        success: true,
        allModulesAllowed: true,
        modules: result.rows.map(row => ({
          id: row.id,
          name: row.name,
          displayName: row.display_name,
          description: row.description,
          category: row.category,
          isCore: row.is_core,
          isPublished: row.is_published,
          version: row.version,
          createdAt: row.created_at?.toISOString(),
        })),
      });
    }

    if (tenant.allowed_module_ids.length === 0) {
      return res.json({ success: true, allModulesAllowed: false, modules: [] });
    }

    const result = await query(
      `SELECT id, name, display_name, description, category, is_core,
              is_published, version, created_at
       FROM hub_modules
       WHERE id = ANY($1::int[])
       ORDER BY category ASC, display_name ASC`,
      [tenant.allowed_module_ids]
    );

    res.json({
      success: true,
      allModulesAllowed: false,
      modules: result.rows.map(row => ({
        id: row.id,
        name: row.name,
        displayName: row.display_name,
        description: row.description,
        category: row.category,
        isCore: row.is_core,
        isPublished: row.is_published,
        version: row.version,
        createdAt: row.created_at?.toISOString(),
      })),
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Update allowed module IDs for a tenant
 * PUT /:tenantSlug/modules
 * Body: { allowedModuleIds: [1, 2, 3] | null }
 */
export async function updateTenantModules(req, res, next) {
  try {
    const { tenantSlug } = req.params;
    const { allowedModuleIds } = req.body;

    if (allowedModuleIds !== null && !Array.isArray(allowedModuleIds)) {
      return next(errors.badRequest('allowedModuleIds must be an array of integers or null'));
    }

    if (Array.isArray(allowedModuleIds)) {
      for (const id of allowedModuleIds) {
        if (!Number.isInteger(id) || id <= 0) {
          return next(errors.badRequest('Each allowedModuleId must be a positive integer'));
        }
      }
    }

    const tenant = await getTenantBySlug(tenantSlug);
    if (!tenant) {
      return next(errors.notFound('Tenant not found'));
    }

    await query(
      'UPDATE tenants SET allowed_module_ids = $1, updated_at = NOW() WHERE id = $2',
      [allowedModuleIds, tenant.id]
    );

    logger.audit('Tenant allowed modules updated', {
      adminId: req.user?.id,
      tenantId: tenant.id,
      tenantSlug,
      allowedModuleIds,
    });

    res.json({ success: true, message: 'Tenant modules updated' });
  } catch (err) {
    next(err);
  }
}

/**
 * List tenant admins
 * GET /:tenantSlug/admins
 */
export async function getTenantAdmins(req, res, next) {
  try {
    const { tenantSlug } = req.params;

    const tenant = await getTenantBySlug(tenantSlug);
    if (!tenant) {
      return next(errors.notFound('Tenant not found'));
    }

    const result = await query(
      `SELECT ta.user_id, u.username, u.display_name, u.email, ta.role, ta.created_at
       FROM tenant_admins ta
       JOIN hub_users u ON u.id = ta.user_id
       WHERE ta.tenant_id = $1
       ORDER BY ta.role ASC, ta.created_at ASC`,
      [tenant.id]
    );

    res.json({
      success: true,
      admins: result.rows.map(row => ({
        userId: row.user_id,
        displayName: row.display_name || row.username,
        email: row.email,
        role: row.role,
        createdAt: row.created_at?.toISOString(),
      })),
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Add a tenant admin
 * POST /:tenantSlug/admins
 * Body: { userId, role? }
 */
export async function addTenantAdmin(req, res, next) {
  try {
    const { tenantSlug } = req.params;
    const { userId, role = 'tenant-admin' } = req.body;

    if (!userId) {
      return next(errors.badRequest('userId is required'));
    }
    if (!VALID_TENANT_ADMIN_ROLES.includes(role)) {
      return next(errors.badRequest(`role must be one of: ${VALID_TENANT_ADMIN_ROLES.join(', ')}`));
    }

    const tenant = await getTenantBySlug(tenantSlug);
    if (!tenant) {
      return next(errors.notFound('Tenant not found'));
    }

    // Verify the user exists
    const userCheck = await query('SELECT id FROM hub_users WHERE id = $1', [userId]);
    if (!userCheck.rows[0]) {
      return next(errors.notFound('User not found'));
    }

    await query(
      `INSERT INTO tenant_admins (tenant_id, user_id, role, created_at)
       VALUES ($1, $2, $3, NOW())
       ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role`,
      [tenant.id, userId, role]
    );

    logger.audit('Tenant admin added', {
      adminId: req.user?.id,
      tenantId: tenant.id,
      tenantSlug,
      targetUserId: userId,
      role,
    });

    res.json({ success: true, message: 'Tenant admin added' });
  } catch (err) {
    next(err);
  }
}

/**
 * Remove a tenant admin
 * DELETE /:tenantSlug/admins/:userId
 */
export async function removeTenantAdmin(req, res, next) {
  try {
    const { tenantSlug, userId } = req.params;

    const tenant = await getTenantBySlug(tenantSlug);
    if (!tenant) {
      return next(errors.notFound('Tenant not found'));
    }

    const result = await query(
      'DELETE FROM tenant_admins WHERE tenant_id = $1 AND user_id = $2',
      [tenant.id, userId]
    );

    if (result.rowCount === 0) {
      return next(errors.notFound('Tenant admin not found'));
    }

    logger.audit('Tenant admin removed', {
      adminId: req.user?.id,
      tenantId: tenant.id,
      tenantSlug,
      targetUserId: userId,
    });

    res.json({ success: true, message: 'Tenant admin removed' });
  } catch (err) {
    next(err);
  }
}
