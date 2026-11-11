/**
 * Auth Controller - Unified local login with OAuth platform linking
 *
 * Authentication Flow:
 * 1. Local login: email/password -> creates/validates hub_users record
 * 2. OAuth login: platform auth -> finds/creates hub_users record, links identity
 * 3. Platform linking: adds platform identity to existing user
 */
import jwt from 'jsonwebtoken';
import bcrypt from 'bcrypt';
import crypto from 'crypto';
import { v4 as uuidv4 } from 'uuid';
import axios from 'axios';
import { query, transaction } from '../config/database.js';
import { config } from '../config/index.js';
import { errors } from '../middleware/errorHandler.js';
import { logger } from '../utils/logger.js';
import { generateVerificationToken, sendVerificationEmail } from '../utils/email.js';
import { setSessionCookie } from '../utils/sessionCookie.js';

const SALT_ROUNDS = 12;

// Short-lived (60s), single-use opaque code standing in for a just-issued
// session JWT -- see createOAuthExchangeCode()'s docstring below. Mirrors
// hub_api's EXCHANGE_CODE_TTL_SECONDS (services/oauth_service.py).
const OAUTH_EXCHANGE_CODE_TTL_SECONDS = 60;

/**
 * Get hub settings helper
 */
async function getHubSettingsMap() {
  const result = await query('SELECT setting_key, setting_value FROM hub_settings');
  const settings = {};
  for (const row of result.rows) {
    settings[row.setting_key] = row.setting_value;
  }
  return settings;
}

/**
 * Validate CAPTCHA token against the configured provider
 */
async function validateCaptcha(provider, secretKey, token, remoteIp) {
  try {
    const url = provider === 'turnstile'
      ? 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
      : 'https://www.google.com/recaptcha/api/siteverify';
    const params = new URLSearchParams({ secret: secretKey, response: token });
    if (remoteIp) params.append('remoteip', remoteIp);
    const res = await axios.post(url, params.toString(), {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    return res.data?.success === true;
  } catch (err) {
    logger.warn('CAPTCHA validation request failed', { err: err.message });
    return false;
  }
}

/**
 * Register new user with local credentials
 */
export async function register(req, res, next) {
  try {
    const { email, password, username } = req.body;

    if (!email || !password) {
      return next(errors.badRequest('Email and password required'));
    }

    // Check signup settings
    const settings = await getHubSettingsMap();
    const signupEnabled = settings.signup_enabled === 'true';
    const emailConfigured = settings.email_configured === 'true';
    const requireVerification = settings.signup_require_email_verification === 'true';
    const allowedDomains = settings.signup_allowed_domains
      ? settings.signup_allowed_domains.split(',').map(d => d.trim().toLowerCase()).filter(Boolean)
      : [];

    // Check if signup is enabled
    if (!signupEnabled || !emailConfigured) {
      return next(errors.forbidden('Registration is currently disabled'));
    }

    // Check CAPTCHA if configured
    const captchaProvider = settings.captcha_provider || 'none';
    const captchaSecretKey = settings.captcha_secret_key || '';
    if (captchaProvider !== 'none' && captchaSecretKey) {
      const captchaToken = req.body.captcha_token;
      if (!captchaToken) {
        return next(errors.badRequest('CAPTCHA verification required'));
      }
      const captchaValid = await validateCaptcha(captchaProvider, captchaSecretKey, captchaToken, req.ip);
      if (!captchaValid) {
        return next(errors.badRequest('CAPTCHA verification failed'));
      }
    }

    // Check domain restriction
    const emailDomain = email.toLowerCase().split('@')[1];
    if (allowedDomains.length > 0 && !allowedDomains.includes(emailDomain)) {
      return next(errors.forbidden(`Registration is restricted to specific email domains`));
    }

    // Check if email already exists
    const existingUser = await query(
      'SELECT id FROM hub_users WHERE email = $1',
      [email.toLowerCase()]
    );

    if (existingUser.rows.length > 0) {
      return next(errors.conflict('Email already registered'));
    }

    // Hash password
    const passwordHash = await bcrypt.hash(password, SALT_ROUNDS);

    // Generate verification token if required
    let verificationToken = null;
    let verificationExpires = null;
    if (requireVerification) {
      verificationToken = generateVerificationToken();
      verificationExpires = new Date(Date.now() + 24 * 60 * 60 * 1000); // 24 hours
    }

    // Create user
    // Username defaults to email for consistency across SSO/local logins
    const result = await query(
      `INSERT INTO hub_users
       (email, password_hash, username, is_active, email_verified, email_verification_token, email_verification_expires, created_at)
       VALUES ($1, $2, $3, true, $4, $5, $6, NOW())
       RETURNING id, email, username, email_verified`,
      [
        email.toLowerCase(),
        passwordHash,
        username || email.toLowerCase(),
        !requireVerification, // email_verified is true only if verification not required
        verificationToken,
        verificationExpires
      ]
    );

    const user = result.rows[0];

    // Auto-add user to global community
    try {
      await addUserToGlobalCommunity(user.id);
    } catch (err) {
      logger.error('CRITICAL: Failed to add user to global community', { userId: user.id, error: err.message });
      // Don't fail registration, but log loudly
    }

    // Send verification email if required
    if (requireVerification) {
      const emailResult = await sendVerificationEmail(user.email, user.username, verificationToken);
      if (!emailResult.success) {
        logger.error('Failed to send verification email', { email: user.email, error: emailResult.error });
      }

      logger.auth('User registered - verification required', { userId: user.id, email: user.email });

      return res.json({
        success: true,
        requiresVerification: true,
        message: 'Please check your email to verify your account',
      });
    }

    // Create session (only if no verification required)
    const sessionToken = await createSession({
      userId: user.id,
      username: user.username,
      email: user.email,
    });

    logger.auth('User registered', { userId: user.id, email: user.email });

    res.json({
      success: true,
      token: sessionToken,
      user: {
        id: user.id,
        email: user.email,
        username: user.username,
      },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Verify email address
 */
export async function verifyEmail(req, res, next) {
  try {
    const { token } = req.query;

    if (!token) {
      return next(errors.badRequest('Verification token required'));
    }

    // Find user with this token
    const result = await query(
      `SELECT id, email, username, email_verification_expires
       FROM hub_users
       WHERE email_verification_token = $1`,
      [token]
    );

    if (result.rows.length === 0) {
      return next(errors.badRequest('Invalid or expired verification token'));
    }

    const user = result.rows[0];

    // Check if token expired
    if (new Date() > new Date(user.email_verification_expires)) {
      return next(errors.badRequest('Verification token has expired'));
    }

    // Mark email as verified
    await query(
      `UPDATE hub_users
       SET email_verified = true, email_verification_token = NULL, email_verification_expires = NULL
       WHERE id = $1`,
      [user.id]
    );

    logger.auth('Email verified', { userId: user.id, email: user.email });

    // Create session so user is logged in
    const sessionToken = await createSession({
      userId: user.id,
      username: user.username,
      email: user.email,
    });

    res.json({
      success: true,
      message: 'Email verified successfully',
      token: sessionToken,
      user: {
        id: user.id,
        email: user.email,
        username: user.username,
      },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Resend verification email
 */
export async function resendVerification(req, res, next) {
  try {
    const { email } = req.body;

    if (!email) {
      return next(errors.badRequest('Email required'));
    }

    // Find user
    const result = await query(
      `SELECT id, email, username, email_verified
       FROM hub_users WHERE email = $1`,
      [email.toLowerCase()]
    );

    if (result.rows.length === 0) {
      // Don't reveal if user exists
      return res.json({ success: true, message: 'If the email exists, a verification link has been sent' });
    }

    const user = result.rows[0];

    if (user.email_verified) {
      return next(errors.badRequest('Email is already verified'));
    }

    // Generate new token
    const verificationToken = generateVerificationToken();
    const verificationExpires = new Date(Date.now() + 24 * 60 * 60 * 1000);

    await query(
      `UPDATE hub_users
       SET email_verification_token = $1, email_verification_expires = $2
       WHERE id = $3`,
      [verificationToken, verificationExpires, user.id]
    );

    // Send email
    const emailResult = await sendVerificationEmail(user.email, user.username, verificationToken);
    if (!emailResult.success) {
      logger.error('Failed to resend verification email', { email: user.email, error: emailResult.error });
    }

    res.json({ success: true, message: 'Verification email sent' });
  } catch (err) {
    next(err);
  }
}

/**
 * Local login with email/password
 */
export async function login(req, res, next) {
  try {
    const { email, password } = req.body;

    if (!email || !password) {
      return next(errors.badRequest('Email and password required'));
    }

    // Resolve tenant context
    const tenantSlug = req.body.tenantSlug || 'default';
    const tenantResult = await query('SELECT id, slug FROM tenants WHERE slug = $1 AND is_active = true', [tenantSlug]);
    const tenantId = tenantResult.rows[0]?.id || null;

    // Find user
    const result = await query(
      `SELECT u.id, u.email, u.username, u.password_hash, u.avatar_url, u.is_active, u.is_super_admin, u.is_vendor, u.is_analytics_consumer, u.email_verified,
              array_agg(DISTINCT ui.platform) FILTER (WHERE ui.platform IS NOT NULL) as linked_platforms
       FROM hub_users u
       LEFT JOIN hub_user_identities ui ON ui.hub_user_id = u.id
       WHERE u.email = $1
       GROUP BY u.id`,
      [email.toLowerCase()]
    );

    if (result.rows.length === 0) {
      logger.auth('Login failed - user not found', { email });
      return next(errors.unauthorized('Invalid credentials'));
    }

    const user = result.rows[0];

    if (!user.is_active) {
      logger.auth('Login failed - user inactive', { email });
      return next(errors.unauthorized('Account is inactive'));
    }

    // Check if email verification is required
    if (user.email_verified === false) {
      logger.auth('Login failed - email not verified', { email });
      return res.status(403).json({
        success: false,
        requiresVerification: true,
        message: 'Please verify your email address before logging in',
      });
    }

    // Verify password
    const valid = await bcrypt.compare(password, user.password_hash);
    if (!valid) {
      logger.auth('Login failed - invalid password', { email });
      return next(errors.unauthorized('Invalid credentials'));
    }

    // Update last login
    await query(
      'UPDATE hub_users SET last_login = NOW() WHERE id = $1',
      [user.id]
    );

    // Create session
    const sessionToken = await createSession({
      userId: user.id,
      username: user.username,
      email: user.email,
      avatarUrl: user.avatar_url,
      isSuperAdmin: user.is_super_admin,
      isVendor: user.is_vendor,
      isAnalyticsConsumer: user.is_analytics_consumer,
      tenantId,
      tenantSlug,
    });

    logger.auth('Login successful', { userId: user.id, email: user.email });

    res.json({
      success: true,
      token: sessionToken,
      user: {
        id: user.id,
        email: user.email,
        username: user.username,
        avatarUrl: user.avatar_url,
        isSuperAdmin: user.is_super_admin,
        isVendor: user.is_vendor,
        isAnalyticsConsumer: user.is_analytics_consumer,
        linkedPlatforms: user.linked_platforms || [],
      },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Legacy admin login - maps to hub_users with is_super_admin flag
 */
export async function adminLogin(req, res, next) {
  try {
    const { username, password } = req.body;

    if (!username || !password) {
      return next(errors.badRequest('Username and password required'));
    }

    // Find user by username or email
    const result = await query(
      `SELECT id, email, username, password_hash, avatar_url, is_active, is_super_admin, is_analytics_consumer
       FROM hub_users
       WHERE (username = $1 OR email = $1) AND is_active = true`,
      [username]
    );

    if (result.rows.length === 0) {
      logger.auth('Admin login failed - user not found', { username });
      return next(errors.unauthorized('Invalid credentials'));
    }

    const user = result.rows[0];

    // Verify password
    const valid = await bcrypt.compare(password, user.password_hash);
    if (!valid) {
      logger.auth('Admin login failed - invalid password', { username });
      return next(errors.unauthorized('Invalid credentials'));
    }

    // Update last login
    await query(
      'UPDATE hub_users SET last_login = NOW() WHERE id = $1',
      [user.id]
    );

    // Create session
    const sessionToken = await createSession({
      userId: user.id,
      username: user.username,
      email: user.email,
      avatarUrl: user.avatar_url,
      isSuperAdmin: user.is_super_admin,
      isVendor: user.is_vendor,
      isAnalyticsConsumer: user.is_analytics_consumer,
    });

    logger.auth('Admin login successful', { username, isSuperAdmin: user.is_super_admin });

    res.json({
      success: true,
      token: sessionToken,
      user: {
        id: user.id,
        email: user.email,
        username: user.username,
        avatarUrl: user.avatar_url,
        isAdmin: user.is_super_admin, // Legacy compatibility
        isSuperAdmin: user.is_super_admin,
      },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Start OAuth flow - redirect to identity module
 */
export async function startOAuth(req, res, next) {
  try {
    const { platform } = req.params;
    const { mode } = req.query; // 'login' or 'link'
    const validPlatforms = ['discord', 'twitch', 'slack', 'youtube', 'kick'];

    if (!validPlatforms.includes(platform)) {
      return next(errors.badRequest('Invalid platform'));
    }

    const state = uuidv4();
    const redirectUri = `${config.identity.callbackBaseUrl}/api/v1/auth/oauth/${platform}/callback`;
    const tenantSlug = req.query.tenantSlug || 'default';

    // Store state with mode and tenant context for callback
    await query(
      `INSERT INTO hub_oauth_states (state, mode, platform, metadata, expires_at)
       VALUES ($1, $2, $3, $4, NOW() + INTERVAL '10 minutes')`,
      [state, mode || 'login', platform, JSON.stringify({ tenantSlug })]
    );

    // Get OAuth URL from Identity Core or direct platform config
    let authorizeUrl;
    try {
      const response = await axios.get(
        `${config.identity.apiUrl}/auth/oauth/${platform}/authorize`,
        { params: { redirect_uri: redirectUri, state } }
      );
      authorizeUrl = response.data.authorize_url;
    } catch {
      // Fallback to generating URL directly from platform config
      authorizeUrl = await generateOAuthUrl(platform, redirectUri, state);
    }

    logger.auth('OAuth flow started', { platform, state, mode: mode || 'login' });
    res.json({ success: true, authorizeUrl, state });
  } catch (err) {
    logger.error('OAuth start error', { error: err.message });
    next(err);
  }
}

/**
 * OAuth callback - exchange code for user info
 */
export async function oauthCallback(req, res, next) {
  try {
    const { platform } = req.params;
    const { code, state, error: oauthError } = req.query;
    const validPlatforms = ['discord', 'twitch', 'slack', 'youtube', 'kick'];

    if (!validPlatforms.includes(platform)) {
      return next(errors.badRequest('Invalid platform'));
    }

    if (oauthError) {
      logger.auth('OAuth error from provider', { platform, error: oauthError });
      return res.redirect(`${config.cors.origin}/login?error=oauth_denied`);
    }

    if (!code) {
      return next(errors.badRequest('Missing authorization code'));
    }

    // Verify state
    const stateResult = await query(
      `SELECT mode, metadata FROM hub_oauth_states
       WHERE state = $1 AND platform = $2 AND expires_at > NOW()`,
      [state, platform]
    );

    if (stateResult.rows.length === 0) {
      logger.auth('OAuth invalid state', { platform, state });
      return res.redirect(`${config.cors.origin}/login?error=invalid_state`);
    }

    const { mode, metadata: stateMetadata } = stateResult.rows[0];
    const oauthTenantSlug = stateMetadata?.tenantSlug || 'default';

    // Clean up state
    await query('DELETE FROM hub_oauth_states WHERE state = $1', [state]);

    const redirectUri = `${config.identity.callbackBaseUrl}/api/v1/auth/oauth/${platform}/callback`;

    // Exchange code with Identity Core
    let userData;
    try {
      const response = await axios.post(
        `${config.identity.apiUrl}/auth/oauth/${platform}/callback`,
        { code, redirect_uri: redirectUri, state }
      );
      userData = response.data.user;
    } catch {
      // Fallback to direct platform exchange
      userData = await exchangeOAuthCode(platform, code, redirectUri);
    }

    // Find or create user
    const user = await findOrCreateUserFromOAuth(platform, userData, mode);

    // Resolve tenant context from state metadata
    const oauthTenantResult = await query('SELECT id FROM tenants WHERE slug = $1 AND is_active = true', [oauthTenantSlug]);
    const oauthTenantId = oauthTenantResult.rows[0]?.id || null;

    // Create session
    const sessionToken = await createSession({
      userId: user.id,
      username: user.username,
      email: user.email,
      avatarUrl: user.avatar_url,
      isSuperAdmin: user.is_super_admin,
      isVendor: user.is_vendor,
      isAnalyticsConsumer: user.is_analytics_consumer,
      tenantId: oauthTenantId,
      tenantSlug: oauthTenantSlug,
    });

    logger.auth('OAuth login successful', { platform, userId: user.id });

    // SECURITY (OWASP A07): do NOT put the session JWT in the redirect
    // URL/query string -- query strings leak into proxy/access logs,
    // browser history, and the Referer header of any outbound request the
    // callback page happens to make. Mint a short-lived, single-use opaque
    // exchange code instead and redirect with THAT; the frontend
    // immediately redeems it for the real JWT via POST /api/v1/auth/exchange
    // (redeemOAuthExchangeCode below), delivered over the response BODY
    // (and an HttpOnly wb_session cookie), never the URL. Mirrors hub_api's
    // oauth_callback / exchange_oauth_code (services/oauth_service.py).
    //
    // NOTE: this is deliberately NOT named exchangeOAuthCode() -- that name
    // is already taken by the direct-platform-token-exchange helper below
    // (used as the Identity Core fallback a few lines up).
    const exchangeCode = await createOAuthExchangeCode(sessionToken, platform);
    res.redirect(`${config.cors.origin}/auth/callback?code=${exchangeCode}`);
  } catch (err) {
    logger.error('OAuth callback error', { error: err.message });
    res.redirect(`${config.cors.origin}/login?error=oauth_failed`);
  }
}

/**
 * Mint a short-lived (60s), single-use opaque code standing in for a
 * just-issued session JWT. Used by oauthCallback() to hand the session off
 * to the frontend via the OAuth redirect WITHOUT putting the JWT itself in
 * the URL. The code is redeemed exactly once, server-side, via
 * redeemOAuthExchangeCode() (POST /api/v1/auth/exchange).
 */
async function createOAuthExchangeCode(token, platform) {
  const code = crypto.randomBytes(32).toString('base64url');
  const expiresAt = new Date(Date.now() + OAUTH_EXCHANGE_CODE_TTL_SECONDS * 1000);

  await query(
    `INSERT INTO hub_oauth_exchange_codes (code, token, platform, used, expires_at, created_at)
     VALUES ($1, $2, $3, false, $4, NOW())`,
    [code, token, platform, expiresAt]
  );

  return code;
}

/**
 * Exchange a short-lived, single-use OAuth callback code for the session
 * JWT. Completes the exchange-code handoff oauthCallback() starts -- see
 * that function's docstring. No auth middleware: like login/refresh, there
 * is no session yet at this point, only the opaque code the callback
 * redirect just handed the frontend via the URL.
 *
 * The `UPDATE ... WHERE used = false AND expires_at > NOW()` clause is the
 * atomic single-use claim -- the database, not application logic,
 * arbitrates a concurrent-redemption race against the same code.
 */
export async function redeemOAuthExchangeCode(req, res, next) {
  try {
    const { code } = req.body;

    if (!code) {
      return next(errors.badRequest('Exchange code required'));
    }

    const result = await query(
      `UPDATE hub_oauth_exchange_codes
       SET used = true, used_at = NOW()
       WHERE code = $1 AND used = false AND expires_at > NOW()
       RETURNING token`,
      [code]
    );

    if (result.rows.length === 0) {
      logger.auth('OAuth exchange code invalid or expired');
      return next(errors.badRequest('Invalid or expired exchange code'));
    }

    const { token } = result.rows[0];
    setSessionCookie(res, token);

    logger.auth('OAuth exchange code redeemed');
    res.json({ success: true, token });
  } catch (err) {
    next(err);
  }
}

/**
 * Find or create user from OAuth data
 */
async function findOrCreateUserFromOAuth(platform, userData, mode) {
  // Check if this platform identity already exists
  const identityResult = await query(
    `SELECT ui.hub_user_id, u.id, u.email, u.username, u.avatar_url, u.is_super_admin, u.is_analytics_consumer
     FROM hub_user_identities ui
     JOIN hub_users u ON u.id = ui.hub_user_id
     WHERE ui.platform = $1 AND ui.platform_user_id = $2`,
    [platform, userData.id]
  );

  if (identityResult.rows.length > 0) {
    // Existing user, update their platform info
    await query(
      `UPDATE hub_user_identities
       SET platform_username = $1, avatar_url = $2, last_used = NOW()
       WHERE platform = $3 AND platform_user_id = $4`,
      [userData.username, userData.avatar_url, platform, userData.id]
    );
    return identityResult.rows[0];
  }

  // No existing identity, create new user + identity
  const email = userData.email || `${userData.username}@${platform}.local`;
  const username = userData.username;

  // Check if email matches existing user
  const existingUser = await query(
    'SELECT id, email, username, avatar_url, is_super_admin, is_analytics_consumer FROM hub_users WHERE email = $1',
    [email.toLowerCase()]
  );

  let userId;
  let user;

  if (existingUser.rows.length > 0) {
    // Link to existing user
    user = existingUser.rows[0];
    userId = user.id;
    logger.auth('Linking OAuth to existing user', { platform, userId, email });
  } else {
    // Create new user
    const newUser = await query(
      `INSERT INTO hub_users (email, username, avatar_url, is_active, created_at)
       VALUES ($1, $2, $3, true, NOW())
       RETURNING id, email, username, avatar_url, is_super_admin, is_analytics_consumer`,
      [email.toLowerCase(), username, userData.avatar_url]
    );
    user = newUser.rows[0];
    userId = user.id;
    logger.auth('Created new user from OAuth', { platform, userId, email });

    // Auto-add new user to global community
    try {
      await addUserToGlobalCommunity(userId);
    } catch (err) {
      logger.error('CRITICAL: Failed to add user to global community', { userId, platform, error: err.message });
      // Don't fail registration, but log loudly
    }
  }

  // Create identity link
  await query(
    `INSERT INTO hub_user_identities
     (hub_user_id, platform, platform_user_id, platform_username, avatar_url, linked_at)
     VALUES ($1, $2, $3, $4, $5, NOW())`,
    [userId, platform, userData.id, userData.username, userData.avatar_url]
  );

  return user;
}

/**
 * Link OAuth account to current user
 */
export async function linkOAuthAccount(req, res, next) {
  try {
    if (!req.user) {
      return next(errors.unauthorized());
    }

    const { platform } = req.params;
    const validPlatforms = ['discord', 'twitch', 'slack', 'youtube', 'kick'];

    if (!validPlatforms.includes(platform)) {
      return next(errors.badRequest('Invalid platform'));
    }

    const state = uuidv4();
    const redirectUri = `${config.identity.callbackBaseUrl}/api/v1/auth/oauth/${platform}/link-callback`;

    // Store state with user ID for callback
    await query(
      `INSERT INTO hub_oauth_states (state, mode, platform, user_id, expires_at)
       VALUES ($1, 'link', $2, $3, NOW() + INTERVAL '10 minutes')`,
      [state, platform, req.user.userId]
    );

    // Get OAuth URL
    let authorizeUrl;
    try {
      const response = await axios.get(
        `${config.identity.apiUrl}/auth/oauth/${platform}/authorize`,
        { params: { redirect_uri: redirectUri, state } }
      );
      authorizeUrl = response.data.authorize_url;
    } catch {
      authorizeUrl = await generateOAuthUrl(platform, redirectUri, state);
    }

    logger.auth('OAuth link flow started', { platform, userId: req.user.userId });
    res.json({ success: true, authorizeUrl, state });
  } catch (err) {
    next(err);
  }
}

/**
 * OAuth link callback - link platform to existing user
 */
export async function oauthLinkCallback(req, res, next) {
  try {
    const { platform } = req.params;
    const { code, state, error: oauthError } = req.query;
    const validPlatforms = ['discord', 'twitch', 'slack', 'youtube', 'kick'];

    if (!validPlatforms.includes(platform)) {
      return next(errors.badRequest('Invalid platform'));
    }

    if (oauthError) {
      return res.redirect(`${config.cors.origin}/dashboard/settings?error=link_denied`);
    }

    // Verify state and get user ID
    const stateResult = await query(
      `SELECT user_id FROM hub_oauth_states
       WHERE state = $1 AND platform = $2 AND mode = 'link' AND expires_at > NOW()`,
      [state, platform]
    );

    if (stateResult.rows.length === 0) {
      return res.redirect(`${config.cors.origin}/dashboard/settings?error=invalid_state`);
    }

    const { user_id: userId } = stateResult.rows[0];
    await query('DELETE FROM hub_oauth_states WHERE state = $1', [state]);

    const redirectUri = `${config.identity.callbackBaseUrl}/api/v1/auth/oauth/${platform}/link-callback`;

    // Exchange code
    let userData;
    try {
      const response = await axios.post(
        `${config.identity.apiUrl}/auth/oauth/${platform}/callback`,
        { code, redirect_uri: redirectUri, state }
      );
      userData = response.data.user;
    } catch {
      userData = await exchangeOAuthCode(platform, code, redirectUri);
    }

    // Check if this platform identity is already linked to another user
    const existingIdentity = await query(
      'SELECT hub_user_id FROM hub_user_identities WHERE platform = $1 AND platform_user_id = $2',
      [platform, userData.id]
    );

    if (existingIdentity.rows.length > 0 && existingIdentity.rows[0].hub_user_id !== userId) {
      return res.redirect(`${config.cors.origin}/dashboard/settings?error=platform_already_linked`);
    }

    // Upsert identity
    await query(
      `INSERT INTO hub_user_identities
       (hub_user_id, platform, platform_user_id, platform_username, avatar_url, linked_at, last_used)
       VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
       ON CONFLICT (platform, platform_user_id)
       DO UPDATE SET platform_username = $4, avatar_url = $5, last_used = NOW()`,
      [userId, platform, userData.id, userData.username, userData.avatar_url]
    );

    logger.auth('OAuth account linked', { platform, userId, platformUserId: userData.id });
    res.redirect(`${config.cors.origin}/dashboard/settings?linked=${platform}`);
  } catch (err) {
    logger.error('OAuth link callback error', { error: err.message });
    res.redirect(`${config.cors.origin}/dashboard/settings?error=link_failed`);
  }
}

/**
 * Unlink OAuth account from current user
 */
export async function unlinkOAuthAccount(req, res, next) {
  try {
    if (!req.user) {
      return next(errors.unauthorized());
    }

    const { platform } = req.params;

    // Check user has password set (can't unlink all if no password)
    const userResult = await query(
      'SELECT password_hash FROM hub_users WHERE id = $1',
      [req.user.userId]
    );

    if (!userResult.rows[0]?.password_hash) {
      // Count linked platforms
      const countResult = await query(
        'SELECT COUNT(*) as count FROM hub_user_identities WHERE hub_user_id = $1',
        [req.user.userId]
      );

      if (parseInt(countResult.rows[0].count) <= 1) {
        return next(errors.badRequest('Cannot unlink last platform without a password. Please set a password first.'));
      }
    }

    await query(
      'DELETE FROM hub_user_identities WHERE hub_user_id = $1 AND platform = $2',
      [req.user.userId, platform]
    );

    logger.auth('OAuth account unlinked', { platform, userId: req.user.userId });
    res.json({ success: true });
  } catch (err) {
    next(err);
  }
}

/**
 * Temp password login
 */
export async function tempPasswordLogin(req, res, next) {
  try {
    const { identifier, password } = req.body;

    if (!identifier || !password) {
      return next(errors.badRequest('Identifier and password required'));
    }

    // Find temp password entry
    const result = await query(
      `SELECT * FROM hub_temp_passwords
       WHERE user_identifier = $1 AND is_used = false AND expires_at > NOW()`,
      [identifier]
    );

    if (result.rows.length === 0) {
      logger.auth('Temp password login failed - not found', { identifier });
      return next(errors.unauthorized('Invalid credentials or expired'));
    }

    const tempPw = result.rows[0];

    // Verify password
    const valid = await bcrypt.compare(password, tempPw.password_hash);
    if (!valid) {
      logger.auth('Temp password login failed - invalid password', { identifier });
      return next(errors.unauthorized('Invalid credentials'));
    }

    // Mark as used
    await query(
      'UPDATE hub_temp_passwords SET is_used = true, used_at = NOW() WHERE id = $1',
      [tempPw.id]
    );

    // Create session with pending OAuth link
    const sessionToken = await createSession({
      userId: null,
      username: identifier,
      requiresOAuthLink: tempPw.force_oauth_link,
      communityId: tempPw.community_id,
    });

    logger.auth('Temp password login successful', { identifier });

    res.json({
      success: true,
      token: sessionToken,
      requiresOAuthLink: tempPw.force_oauth_link,
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Legacy link OAuth (temp password flow)
 */
export async function linkOAuth(req, res, next) {
  try {
    if (!req.user) {
      return next(errors.unauthorized());
    }

    const { platform, platformUserId, username } = req.body;

    if (!platform || !platformUserId) {
      return next(errors.badRequest('Platform and platformUserId required'));
    }

    // Update temp password record with linked OAuth
    await query(
      `UPDATE hub_temp_passwords
       SET linked_oauth_platform = $1, linked_oauth_user_id = $2
       WHERE id = $3`,
      [platform, platformUserId, req.user.platformUserId]
    );

    // Create new session with OAuth identity
    const sessionToken = await createSession({
      userId: null,
      username,
    });

    logger.auth('OAuth linked to temp account', { platform, platformUserId });

    res.json({ success: true, token: sessionToken });
  } catch (err) {
    next(err);
  }
}

/**
 * Refresh JWT token
 */
export async function refreshToken(req, res, next) {
  try {
    const token = req.headers.authorization?.replace('Bearer ', '') || req.body.token;

    if (!token) {
      return next(errors.badRequest('Token required'));
    }

    // Verify current token
    const decoded = jwt.verify(token, config.jwt.secret, { ignoreExpiration: true });

    // Check if session exists and is active
    const result = await query(
      'SELECT * FROM hub_sessions WHERE session_token = $1 AND is_active = true',
      [token]
    );

    if (result.rows.length === 0) {
      return next(errors.unauthorized('Invalid session'));
    }

    // Create new token
    const newToken = await createSession({
      userId: decoded.userId,
      username: decoded.username,
      email: decoded.email,
      avatarUrl: decoded.avatarUrl,
      isSuperAdmin: decoded.isSuperAdmin,
      isVendor: decoded.isVendor,
      isAnalyticsConsumer: decoded.isAnalyticsConsumer,
    });

    // Invalidate old token
    await query(
      'UPDATE hub_sessions SET is_active = false, revoked_at = NOW() WHERE session_token = $1',
      [token]
    );

    res.json({ success: true, token: newToken });
  } catch (err) {
    if (err.name === 'JsonWebTokenError') {
      return next(errors.unauthorized('Invalid token'));
    }
    next(err);
  }
}

/**
 * Logout - invalidate session
 */
export async function logout(req, res, next) {
  try {
    const token = req.headers.authorization?.replace('Bearer ', '');

    if (token) {
      await query(
        'UPDATE hub_sessions SET is_active = false, revoked_at = NOW() WHERE session_token = $1',
        [token]
      );
      logger.auth('User logged out');
    }

    res.json({ success: true });
  } catch (err) {
    next(err);
  }
}

/**
 * Get current user info
 */
export async function getCurrentUser(req, res, next) {
  try {
    if (!req.user) {
      return res.json({ success: true, user: null });
    }

    // Get user with linked platforms
    const userResult = await query(
      `SELECT u.id, u.email, u.username, u.avatar_url, u.is_super_admin, u.is_vendor, u.is_analytics_consumer, u.password_hash IS NOT NULL as has_password,
              COALESCE(json_agg(
                json_build_object(
                  'platform', ui.platform,
                  'username', ui.platform_username,
                  'avatar', ui.avatar_url
                )
              ) FILTER (WHERE ui.platform IS NOT NULL), '[]') as linked_platforms
       FROM hub_users u
       LEFT JOIN hub_user_identities ui ON ui.hub_user_id = u.id
       WHERE u.id = $1
       GROUP BY u.id`,
      [req.user.userId]
    );

    if (userResult.rows.length === 0) {
      return res.json({ success: true, user: null });
    }

    const user = userResult.rows[0];

    // Get user's communities
    const communitiesResult = await query(
      `SELECT c.id, c.name, c.display_name, cm.role
       FROM community_members cm
       JOIN communities c ON c.id = cm.community_id
       WHERE cm.user_id = $1 AND cm.is_active = true AND c.is_active = true`,
      [user.id]
    );

    const communities = communitiesResult.rows.map(r => ({
      id: r.id,
      name: r.name,
      displayName: r.display_name || r.name,
      role: r.role,
    }));

    res.json({
      success: true,
      user: {
        id: user.id,
        email: user.email,
        username: user.username,
        avatarUrl: user.avatar_url,
        isSuperAdmin: user.is_super_admin,
        isVendor: user.is_vendor,
        isAnalyticsConsumer: user.is_analytics_consumer,
        hasPassword: user.has_password,
        linkedPlatforms: user.linked_platforms,
        roles: req.user.roles,
        communities,
      },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Set password for current user
 */
export async function setPassword(req, res, next) {
  try {
    if (!req.user) {
      return next(errors.unauthorized());
    }

    const { currentPassword, newPassword } = req.body;

    if (!newPassword || newPassword.length < 8) {
      return next(errors.badRequest('Password must be at least 8 characters'));
    }

    // Get current user
    const userResult = await query(
      'SELECT password_hash FROM hub_users WHERE id = $1',
      [req.user.userId]
    );

    if (userResult.rows.length === 0) {
      return next(errors.notFound('User not found'));
    }

    const user = userResult.rows[0];

    // If user has password, verify current password
    if (user.password_hash) {
      if (!currentPassword) {
        return next(errors.badRequest('Current password required'));
      }
      const valid = await bcrypt.compare(currentPassword, user.password_hash);
      if (!valid) {
        return next(errors.unauthorized('Current password is incorrect'));
      }
    }

    // Hash and save new password
    const passwordHash = await bcrypt.hash(newPassword, SALT_ROUNDS);
    await query(
      'UPDATE hub_users SET password_hash = $1 WHERE id = $2',
      [passwordHash, req.user.userId]
    );

    logger.auth('Password set', { userId: req.user.userId });
    res.json({ success: true });
  } catch (err) {
    next(err);
  }
}

/**
 * Create session and JWT token
 */
async function createSession({ userId, username, email, avatarUrl, isSuperAdmin, isVendor, isAnalyticsConsumer, requiresOAuthLink, communityId, tenantId, tenantSlug }) {
  // Build roles array
  const roles = [];
  if (isSuperAdmin) roles.push('admin', 'super_admin');
  if (isVendor) roles.push('vendor');

  // Resolve tenant scopes
  const tenantScopes = [];
  if (tenantId) {
    const taResult = await query(
      'SELECT role FROM tenant_admins WHERE tenant_id = $1 AND user_id = $2',
      [tenantId, userId]
    );
    if (taResult.rows.length > 0) {
      const role = taResult.rows[0].role;
      if (role === 'tenant-owner') tenantScopes.push('tenant:manage', 'tenant:manage_modules', 'tenant:manage_billing', 'tenant:manage_admins');
      else if (role === 'tenant-admin') tenantScopes.push('tenant:manage', 'tenant:manage_modules');
    }
  }

  const payload = {
    userId,
    username,
    email,
    avatarUrl,
    requiresOAuthLink: requiresOAuthLink || false,
    communityId,
    isSuperAdmin: isSuperAdmin || false,
    isVendor: isVendor || false,
    isAnalyticsConsumer: isAnalyticsConsumer || false,
    roles,
    tenantId: tenantId || null,
    tenantSlug: tenantSlug || 'default',
    tenantScopes,
  };

  // Generate token
  const token = jwt.sign(payload, config.jwt.secret, {
    expiresIn: config.jwt.expiresIn,
  });

  // Store session
  const expiresAt = new Date(Date.now() + config.jwt.expiresIn * 1000);

  await query(
    `INSERT INTO hub_sessions
     (session_token, user_id, platform_username, avatar_url, expires_at)
     VALUES ($1, $2, $3, $4, $5)`,
    [token, userId, username, avatarUrl, expiresAt]
  );

  return token;
}

/**
 * Generate OAuth URL directly from platform config
 */
async function generateOAuthUrl(platform, redirectUri, state) {
  // Get platform credentials from database or env
  const result = await query(
    `SELECT config_key, config_value, is_encrypted FROM platform_configs WHERE platform = $1`,
    [platform]
  );

  const creds = {};
  for (const row of result.rows) {
    creds[row.config_key] = row.config_value;
  }

  // Fallback to env vars
  const envPrefix = platform.toUpperCase();
  if (!creds.client_id) creds.client_id = process.env[`${envPrefix}_CLIENT_ID`];

  if (!creds.client_id) {
    throw new Error(`No OAuth credentials configured for ${platform}`);
  }

  switch (platform) {
    case 'discord':
      return `https://discord.com/api/oauth2/authorize?client_id=${creds.client_id}&redirect_uri=${encodeURIComponent(redirectUri)}&response_type=code&scope=identify%20email&state=${state}`;
    case 'twitch':
      return `https://id.twitch.tv/oauth2/authorize?client_id=${creds.client_id}&redirect_uri=${encodeURIComponent(redirectUri)}&response_type=code&scope=user:read:email&state=${state}`;
    case 'slack':
      return `https://slack.com/oauth/v2/authorize?client_id=${creds.client_id}&redirect_uri=${encodeURIComponent(redirectUri)}&scope=identity.basic,identity.email,identity.avatar&state=${state}`;
    case 'youtube':
      return `https://accounts.google.com/o/oauth2/v2/auth?client_id=${creds.client_id}&redirect_uri=${encodeURIComponent(redirectUri)}&response_type=code&scope=https://www.googleapis.com/auth/youtube.readonly%20email&state=${state}`;
    case 'kick':
      return `https://id.kick.com/oauth/authorize?client_id=${creds.client_id}&redirect_uri=${encodeURIComponent(redirectUri)}&response_type=code&scope=user:read%20channel:read%20chat:read%20chat:write&state=${state}`;
    default:
      throw new Error(`Unknown platform: ${platform}`);
  }
}

/**
 * Exchange OAuth code directly with platform
 */
async function exchangeOAuthCode(platform, code, redirectUri) {
  // Get platform credentials
  const result = await query(
    `SELECT config_key, config_value FROM platform_configs WHERE platform = $1`,
    [platform]
  );

  const creds = {};
  for (const row of result.rows) {
    creds[row.config_key] = row.config_value;
  }

  const envPrefix = platform.toUpperCase();
  if (!creds.client_id) creds.client_id = process.env[`${envPrefix}_CLIENT_ID`];
  if (!creds.client_secret) creds.client_secret = process.env[`${envPrefix}_CLIENT_SECRET`];

  switch (platform) {
    case 'discord': {
      const tokenResp = await axios.post(
        'https://discord.com/api/oauth2/token',
        new URLSearchParams({
          client_id: creds.client_id,
          client_secret: creds.client_secret,
          grant_type: 'authorization_code',
          code,
          redirect_uri: redirectUri,
        }),
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } }
      );
      const tokens = tokenResp.data;
      const userResp = await axios.get('https://discord.com/api/users/@me', {
        headers: { Authorization: `Bearer ${tokens.access_token}` },
      });
      const user = userResp.data;
      return {
        id: user.id,
        username: user.username,
        email: user.email,
        avatar_url: user.avatar ? `https://cdn.discordapp.com/avatars/${user.id}/${user.avatar}.png` : null,
        access_token: tokens.access_token,
        refresh_token: tokens.refresh_token,
      };
    }
    case 'twitch': {
      const tokenResp = await axios.post(
        'https://id.twitch.tv/oauth2/token',
        new URLSearchParams({
          client_id: creds.client_id,
          client_secret: creds.client_secret,
          grant_type: 'authorization_code',
          code,
          redirect_uri: redirectUri,
        }),
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } }
      );
      const tokens = tokenResp.data;
      const userResp = await axios.get('https://api.twitch.tv/helix/users', {
        headers: {
          Authorization: `Bearer ${tokens.access_token}`,
          'Client-Id': creds.client_id,
        },
      });
      const user = userResp.data.data[0];
      return {
        id: user.id,
        username: user.login,
        email: user.email,
        avatar_url: user.profile_image_url,
        access_token: tokens.access_token,
        refresh_token: tokens.refresh_token,
      };
    }
    case 'youtube': {
      // Google OAuth for YouTube
      const tokenResp = await axios.post(
        'https://oauth2.googleapis.com/token',
        new URLSearchParams({
          client_id: creds.client_id,
          client_secret: creds.client_secret,
          grant_type: 'authorization_code',
          code,
          redirect_uri: redirectUri,
        }),
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } }
      );
      const tokens = tokenResp.data;

      // Get user info from Google
      const userInfoResp = await axios.get('https://www.googleapis.com/oauth2/v2/userinfo', {
        headers: { Authorization: `Bearer ${tokens.access_token}` },
      });
      const userInfo = userInfoResp.data;

      // Get YouTube channel info
      let channelName = userInfo.name;
      try {
        const ytResp = await axios.get('https://www.googleapis.com/youtube/v3/channels', {
          params: { part: 'snippet', mine: true },
          headers: { Authorization: `Bearer ${tokens.access_token}` },
        });
        if (ytResp.data.items && ytResp.data.items.length > 0) {
          channelName = ytResp.data.items[0].snippet.title;
        }
      } catch {
        // YouTube channel not required, use Google name
      }

      return {
        id: userInfo.id,
        username: channelName || userInfo.email.toLowerCase(),
        email: userInfo.email,
        avatar_url: userInfo.picture,
        access_token: tokens.access_token,
        refresh_token: tokens.refresh_token,
      };
    }
    case 'slack': {
      const tokenResp = await axios.post(
        'https://slack.com/api/oauth.v2.access',
        new URLSearchParams({
          client_id: creds.client_id,
          client_secret: creds.client_secret,
          code,
          redirect_uri: redirectUri,
        }),
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } }
      );
      const data = tokenResp.data;
      if (!data.ok) {
        throw new Error(`Slack OAuth error: ${data.error}`);
      }

      // Get user identity
      const identityResp = await axios.get('https://slack.com/api/users.identity', {
        headers: { Authorization: `Bearer ${data.authed_user.access_token}` },
      });
      const identity = identityResp.data;

      return {
        id: identity.user.id,
        username: identity.user.name,
        email: identity.user.email,
        avatar_url: identity.user.image_192 || identity.user.image_72,
        access_token: data.authed_user.access_token,
        refresh_token: null,
      };
    }
    case 'kick': {
      // KICK OAuth token exchange
      const tokenResp = await axios.post(
        'https://id.kick.com/oauth/token',
        new URLSearchParams({
          client_id: creds.client_id,
          client_secret: creds.client_secret,
          grant_type: 'authorization_code',
          code,
          redirect_uri: redirectUri,
        }),
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } }
      );
      const tokens = tokenResp.data;

      // Get user info from KICK
      const userResp = await axios.get('https://kick.com/api/v2/user', {
        headers: { Authorization: `Bearer ${tokens.access_token}` },
      });
      const user = userResp.data;

      return {
        id: user.id.toString(),
        username: user.username,
        email: user.email || null,
        avatar_url: user.profile_pic || user.avatar || null,
        access_token: tokens.access_token,
        refresh_token: tokens.refresh_token,
      };
    }
    default:
      throw new Error(`OAuth exchange not implemented for ${platform}`);
  }
}

/**
 * Get tenant login info (public endpoint)
 */
export async function getTenantLoginInfo(req, res, next) {
  try {
    const { slug } = req.params;
    const result = await query(
      `SELECT t.slug, t.display_name, t.logo_url, t.is_global, t.is_active, t.config
       FROM tenants t WHERE t.slug = $1`,
      [slug]
    );

    if (!result.rows.length) {
      return next(errors.notFound('Tenant not found'));
    }

    const tenant = result.rows[0];
    if (!tenant.is_active) {
      return next(errors.forbidden('This tenant is currently inactive'));
    }

    // Get enabled auth methods for this tenant
    const pcResult = await query(
      `SELECT platform, enabled FROM platform_configs WHERE tenant_id = (SELECT id FROM tenants WHERE slug = $1) AND enabled = true`,
      [slug]
    );
    const enabledPlatforms = pcResult.rows.map(r => r.platform);

    res.json({
      success: true,
      tenant: {
        slug: tenant.slug,
        displayName: tenant.display_name,
        logoUrl: tenant.logo_url,
        isGlobal: tenant.is_global,
        enabledPlatforms,
        config: {
          theme: tenant.config?.theme || null,
          welcomeMessage: tenant.config?.welcomeMessage || null,
        },
      },
    });
  } catch (err) {
    next(err);
  }
}

/**
 * Add user to global community as a member
 * Called automatically when new users register or sign in via OAuth
 */
async function addUserToGlobalCommunity(userId) {
  try {
    // Find the global community (check config->>'is_global')
    const globalCommunity = await query(
      "SELECT id FROM communities WHERE config->>'is_global' = 'true' AND is_active = true LIMIT 1"
    );

    if (globalCommunity.rows.length === 0) {
      logger.warn('CRITICAL: No global community found - user will not be auto-added to community', { userId });
      return;
    }

    const communityId = globalCommunity.rows[0].id;

    // Add user as member (ignore if already exists)
    await query(
      `INSERT INTO community_members (community_id, user_id, role, is_active, joined_at)
       VALUES ($1, $2, 'member', true, NOW())
       ON CONFLICT (community_id, user_id) DO NOTHING`,
      [communityId, userId]
    );

    // Update member count
    await query(
      `UPDATE communities SET member_count = (
        SELECT COUNT(*) FROM community_members WHERE community_id = $1 AND is_active = true
      ) WHERE id = $1`,
      [communityId]
    );

    logger.debug('User added to global community', { userId, communityId });
  } catch (err) {
    // Don't fail registration if global community add fails, but log loudly for visibility
    logger.error('CRITICAL: Failed to add user to global community', { userId, error: err.message, stack: err.stack });
  }
}
