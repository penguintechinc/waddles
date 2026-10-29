/**
 * Vendor Service — vendor profile, module management, and submission workflow
 */
import { query, transaction } from '../config/database.js';
import { logger } from '../utils/logger.js';
import { errors } from '../middleware/errorHandler.js';
import { assertSafeUrl } from '../utils/safeUrl.js';
import { clampTimeout } from './vendorExecutionService.js';

/** Validates every vendor-controlled outbound URL present in a module payload (SSRF guard). */
async function validateVendorUrls({ webhookUrl, apiBaseUrl }) {
  if (webhookUrl != null) await assertSafeUrl(webhookUrl, 'webhookUrl');
  if (apiBaseUrl != null) await assertSafeUrl(apiBaseUrl, 'apiBaseUrl');
}

/**
 * Converts a display name to a URL-safe slug.
 * @param {string} name
 * @returns {string}
 */
function slugify(name) {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '');
}

/**
 * Returns the vendor profile for a user, or null if none exists.
 * @param {number} userId
 */
export async function getVendorProfile(userId) {
  const result = await query(
    `SELECT * FROM marketplace_sellers WHERE user_id = $1 LIMIT 1`,
    [userId]
  );
  return result.rows[0] || null;
}

/**
 * Creates a new vendor profile for a user.
 * @param {number} userId
 * @param {object} profileData
 * @param {string} profileData.displayName
 * @param {string} profileData.description
 * @param {string} profileData.websiteUrl
 * @param {string} profileData.payoutMethod
 */
export async function createVendorProfile(userId, { displayName, description, websiteUrl, payoutMethod }) {
  let result;
  try {
    result = await query(
      `INSERT INTO marketplace_sellers
         (user_id, display_name, description, website_url, payout_method, created_at, updated_at)
       VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
       RETURNING *`,
      [userId, displayName, description, websiteUrl, payoutMethod]
    );
  } catch (err) {
    if (err.code === '23505') {
      throw errors.conflict('Vendor profile already exists');
    }
    throw err;
  }
  return result.rows[0];
}

/**
 * Returns paginated modules owned by a vendor.
 * @param {number} userId
 * @param {object} options
 * @param {number} options.page
 * @param {number} options.limit
 */
export async function getVendorModules(userId, { page = 1, limit = 25 } = {}) {
  const offset = (page - 1) * limit;

  const result = await query(
    `SELECT mm.*
     FROM marketplace_modules mm
     JOIN marketplace_sellers ms ON ms.id = mm.seller_id
     WHERE ms.user_id = $1 AND mm.deleted_at IS NULL
     ORDER BY mm.created_at DESC
     LIMIT $2 OFFSET $3`,
    [userId, limit, offset]
  );

  return {
    modules: result.rows,
    pagination: {
      page,
      limit,
      total: result.rows.length,
    },
  };
}

/**
 * Returns enhanced dashboard data for a vendor: stats, recent submissions, and revenue breakdown.
 * @param {number} userId
 */
export async function getVendorDashboard(userId) {
  const sellerResult = await query(
    `SELECT id FROM marketplace_sellers WHERE user_id = $1`,
    [userId]
  );
  if (!sellerResult.rows[0]) {
    throw errors.notFound('Vendor profile not found');
  }
  const sellerId = sellerResult.rows[0].id;

  const [statsResult, recentResult, revenueResult] = await Promise.all([
    query(
      `SELECT
         COUNT(mm.id)                                           AS "totalModules",
         COUNT(mm.id) FILTER (WHERE mm.status = 'approved')    AS "publishedModules",
         COUNT(mm.id) FILTER (WHERE mm.status = 'pending')     AS "pendingReview",
         COALESCE(SUM(ci.install_count), 0)                    AS "totalInstalls",
         COALESCE(SUM(vp.amount_paid), 0)                      AS "totalRevenue",
         COALESCE(SUM(vp.expected_payout), 0)                  AS "expectedRevenue"
       FROM marketplace_sellers ms
       LEFT JOIN approved_vendor_modules mm ON mm.seller_id = ms.id
       LEFT JOIN community_vendor_installations ci ON ci.module_id = mm.id
       LEFT JOIN vendor_payments vp ON vp.seller_id = ms.id
       WHERE ms.id = $1`,
      [sellerId]
    ),
    query(
      `SELECT vs.id, vs.module_id, vs.status, vs.submitted_at, mm.name AS "moduleName"
       FROM vendor_submissions vs
       JOIN approved_vendor_modules mm ON mm.id = vs.module_id
       WHERE mm.seller_id = $1
       ORDER BY vs.submitted_at DESC
       LIMIT 5`,
      [sellerId]
    ),
    query(
      `SELECT
         mm.id AS "moduleId",
         mm.name AS "moduleName",
         COALESCE(SUM(vp.amount_paid), 0) AS revenue
       FROM approved_vendor_modules mm
       LEFT JOIN vendor_payments vp ON vp.module_id = mm.id
       WHERE mm.seller_id = $1
       GROUP BY mm.id, mm.name
       ORDER BY revenue DESC`,
      [sellerId]
    ),
  ]);

  return {
    stats: statsResult.rows[0],
    recentSubmissions: recentResult.rows,
    revenueBreakdown: revenueResult.rows,
  };
}

/**
 * Updates the vendor profile for a user.
 * @param {number} userId
 * @param {object} profileData
 * @param {string} profileData.displayName
 * @param {string} [profileData.description]
 * @param {string} [profileData.websiteUrl]
 * @param {string} [profileData.payoutMethod]
 */
export async function updateVendorProfile(userId, { displayName, description, websiteUrl, payoutMethod }) {
  const result = await query(
    `UPDATE marketplace_sellers
     SET display_name  = COALESCE($2, display_name),
         description   = COALESCE($3, description),
         website_url   = COALESCE($4, website_url),
         payout_method = COALESCE($5, payout_method),
         updated_at    = NOW()
     WHERE user_id = $1
     RETURNING *`,
    [userId, displayName ?? null, description ?? null, websiteUrl ?? null, payoutMethod ?? null]
  );
  if (!result.rows[0]) {
    throw errors.notFound('Vendor profile not found');
  }
  return result.rows[0];
}

/**
 * Returns a basic analytics summary for a vendor's modules.
 * @param {number} userId
 */
export async function getVendorAnalyticsOverview(userId) {
  const sellerResult = await query(
    `SELECT id FROM marketplace_sellers WHERE user_id = $1`,
    [userId]
  );
  if (!sellerResult.rows[0]) {
    throw errors.notFound('Vendor profile not found');
  }
  const sellerId = sellerResult.rows[0].id;

  const [installResult, ratingResult] = await Promise.all([
    query(
      `SELECT
         COALESCE(SUM(ci.install_count), 0)                                      AS "totalInstalls",
         COALESCE(SUM(ci.uninstall_count), 0)                                    AS "totalUninstalls",
         COALESCE(SUM(ci.install_count) FILTER (
           WHERE DATE_TRUNC('month', ci.updated_at) = DATE_TRUNC('month', NOW())
         ), 0)                                                                    AS "installsThisMonth",
         COALESCE(SUM(vp.amount_paid) FILTER (
           WHERE DATE_TRUNC('month', vp.paid_at) = DATE_TRUNC('month', NOW())
         ), 0)                                                                    AS "revenueThisMonth"
       FROM approved_vendor_modules mm
       LEFT JOIN community_vendor_installations ci ON ci.module_id = mm.id
       LEFT JOIN vendor_payments vp ON vp.module_id = mm.id
       WHERE mm.seller_id = $1`,
      [sellerId]
    ),
    query(
      `SELECT ROUND(AVG(vmr.rating), 2) AS "avgRating"
       FROM vendor_module_reviews vmr
       JOIN approved_vendor_modules mm ON mm.id = vmr.module_id
       WHERE mm.seller_id = $1`,
      [sellerId]
    ),
  ]);

  return {
    totalInstalls: installResult.rows[0]['totalInstalls'],
    totalUninstalls: installResult.rows[0]['totalUninstalls'],
    installsThisMonth: installResult.rows[0]['installsThisMonth'],
    revenueThisMonth: installResult.rows[0]['revenueThisMonth'],
    avgRating: ratingResult.rows[0]['avgRating'] ?? null,
  };
}

/**
 * Creates a new marketplace module for a vendor.
 * @param {number} userId
 * @param {object} moduleData
 */
export async function createVendorModule(userId, moduleData) {
  const sellerResult = await query(
    `SELECT id FROM marketplace_sellers WHERE user_id = $1`,
    [userId]
  );

  if (!sellerResult.rows[0]) {
    throw errors.forbidden('Vendor profile required. Please create a vendor profile first.');
  }

  const sellerId = sellerResult.rows[0].id;

  const {
    name,
    description,
    category,
    webhookUrl,
    webhookSecret,
    webhookTimeoutMs = 5000,
    triggerCommands = [],
    triggerEvents = [],
    requestedScopes = [],
    responseTypes = [],
    pricingType = 'free',
    priceCents = 0,
    pricingModel = 'flat',
    billingPeriod = 'monthly',
    currency = 'USD',
    communicationModel = 'webhook_push',
    authType = 'hmac',
    authConfig = {},
    apiBaseUrl = null,
    integrationType = 'command_handler',
  } = moduleData;

  await validateVendorUrls({ webhookUrl, apiBaseUrl });
  const safeTimeoutMs = clampTimeout(webhookTimeoutMs);

  let slug = slugify(name);

  let result;
  try {
    result = await query(
      `INSERT INTO marketplace_modules
         (seller_id, name, slug, description, category, webhook_url, webhook_secret,
          webhook_timeout_ms, trigger_commands, trigger_events, requested_scopes,
          response_types, pricing_type, price_cents, pricing_model, billing_period,
          currency, communication_model, auth_type, auth_config, api_base_url,
          integration_type, created_at, updated_at)
       VALUES
         ($1, $2, $3, $4, $5, $6, $7,
          $8, $9, $10, $11,
          $12, $13, $14, $15, $16,
          $17, $18, $19, $20, $21,
          $22, NOW(), NOW())
       RETURNING id, slug, created_at AS "createdAt"`,
      [
        sellerId, name, slug, description, category, webhookUrl, webhookSecret,
        safeTimeoutMs, triggerCommands, triggerEvents, requestedScopes,
        responseTypes, pricingType, priceCents, pricingModel, billingPeriod,
        currency, communicationModel, authType, JSON.stringify(authConfig), apiBaseUrl,
        integrationType,
      ]
    );
  } catch (err) {
    if (err.code === '23505') {
      slug = slug + '-' + userId;
      result = await query(
        `INSERT INTO marketplace_modules
           (seller_id, name, slug, description, category, webhook_url, webhook_secret,
            webhook_timeout_ms, trigger_commands, trigger_events, requested_scopes,
            response_types, pricing_type, price_cents, pricing_model, billing_period,
            currency, communication_model, auth_type, auth_config, api_base_url,
            integration_type, created_at, updated_at)
         VALUES
           ($1, $2, $3, $4, $5, $6, $7,
            $8, $9, $10, $11,
            $12, $13, $14, $15, $16,
            $17, $18, $19, $20, $21,
            $22, NOW(), NOW())
         RETURNING id, slug, created_at AS "createdAt"`,
        [
          sellerId, name, slug, description, category, webhookUrl, webhookSecret,
          safeTimeoutMs, triggerCommands, triggerEvents, requestedScopes,
          responseTypes, pricingType, priceCents, pricingModel, billingPeriod,
          currency, communicationModel, authType, JSON.stringify(authConfig), apiBaseUrl,
          integrationType,
        ]
      );
    } else {
      throw err;
    }
  }

  return result.rows[0];
}

/**
 * Updates allowed fields on a vendor-owned module.
 * @param {number} userId
 * @param {number} moduleId
 * @param {object} updates
 */
export async function updateVendorModule(userId, moduleId, updates) {
  const ownerCheck = await query(
    `SELECT mm.id
     FROM marketplace_modules mm
     JOIN marketplace_sellers ms ON ms.id = mm.seller_id
     WHERE mm.id = $1 AND ms.user_id = $2 AND mm.deleted_at IS NULL`,
    [moduleId, userId]
  );

  if (!ownerCheck.rows[0]) {
    throw errors.notFound('Module not found');
  }

  const allowedFields = {
    name: 'name',
    description: 'description',
    category: 'category',
    webhookUrl: 'webhook_url',
    webhookSecret: 'webhook_secret',
    webhookTimeoutMs: 'webhook_timeout_ms',
    triggerCommands: 'trigger_commands',
    triggerEvents: 'trigger_events',
    requestedScopes: 'requested_scopes',
    pricingType: 'pricing_type',
    priceCents: 'price_cents',
    pricingModel: 'pricing_model',
    billingPeriod: 'billing_period',
    currency: 'currency',
    communicationModel: 'communication_model',
    authType: 'auth_type',
    authConfig: 'auth_config',
    apiBaseUrl: 'api_base_url',
    integrationType: 'integration_type',
  };

  const setClauses = [];
  const values = [];
  let paramIndex = 1;

  await validateVendorUrls({
    webhookUrl: updates.webhookUrl,
    apiBaseUrl: updates.apiBaseUrl,
  });

  for (const [jsKey, dbColumn] of Object.entries(allowedFields)) {
    if (jsKey in updates) {
      setClauses.push(`${dbColumn} = $${paramIndex}`);
      values.push(jsKey === 'webhookTimeoutMs' ? clampTimeout(updates[jsKey]) : updates[jsKey]);
      paramIndex++;
    }
  }

  if (setClauses.length === 0) {
    return { success: true };
  }

  // Endpoint/credential changes on an approved module force re-review.
  const reviewSensitive = [
    'webhookUrl', 'apiBaseUrl', 'webhookSecret', 'authType', 'authConfig', 'communicationModel',
  ];
  if (reviewSensitive.some((k) => k in updates)) {
    setClauses.push(`status = CASE WHEN status = 'approved' THEN 'pending' ELSE status END`);
  }

  setClauses.push(`updated_at = NOW()`);
  values.push(moduleId);

  await query(
    `UPDATE marketplace_modules SET ${setClauses.join(', ')} WHERE id = $${paramIndex}`,
    values
  );

  return { success: true };
}

/**
 * Submits a vendor module for review.
 * @param {number} userId
 * @param {number} moduleId
 * @param {string} changesDescription
 */
export async function submitModuleForReview(userId, moduleId, changesDescription) {
  const ownerCheck = await query(
    `SELECT mm.id, mm.version
     FROM marketplace_modules mm
     JOIN marketplace_sellers ms ON ms.id = mm.seller_id
     WHERE mm.id = $1 AND ms.user_id = $2 AND mm.deleted_at IS NULL`,
    [moduleId, userId]
  );

  if (!ownerCheck.rows[0]) {
    throw errors.notFound('Module not found');
  }

  const { version } = ownerCheck.rows[0];

  const submissionResult = await query(
    `INSERT INTO marketplace_submissions
       (module_id, version, changes_description, submitted_by, status, submitted_at)
     VALUES ($1, $2, $3, $4, 'pending', NOW())
     RETURNING id`,
    [moduleId, version, changesDescription, userId]
  );

  await query(
    `UPDATE marketplace_modules SET status = 'pending', updated_at = NOW() WHERE id = $1`,
    [moduleId]
  );

  return { submissionId: submissionResult.rows[0].id };
}

/**
 * Returns the most recent vendor role request for a user, or null.
 * @param {number} userId
 */
export async function getVendorRequest(userId) {
  try {
    const result = await query(
      `SELECT * FROM vendor_role_requests WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1`,
      [userId]
    );
    return result.rows[0] || null;
  } catch (err) {
    return null;
  }
}

/**
 * Creates a new vendor role request for a user.
 * @param {number} userId
 * @param {object} requestData
 */
export async function createVendorRequest(userId, {
  companyName,
  businessDescription,
  experienceSummary,
  contactEmail,
  contactPhone,
  websiteUrl,
}) {
  const result = await query(
    `INSERT INTO vendor_role_requests
       (user_id, company_name, business_description, experience_summary,
        contact_email, contact_phone, website_url, status, created_at)
     VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', NOW())
     RETURNING id, status`,
    [userId, companyName, businessDescription, experienceSummary, contactEmail, contactPhone, websiteUrl]
  );

  return { id: result.rows[0].id, status: 'pending' };
}

export default {
  getVendorProfile,
  createVendorProfile,
  getVendorModules,
  createVendorModule,
  updateVendorModule,
  submitModuleForReview,
  getVendorRequest,
  createVendorRequest,
};
