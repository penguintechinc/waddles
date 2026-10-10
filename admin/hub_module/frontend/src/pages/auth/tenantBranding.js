// Stock Waddles logo -- what every tenant gets unless the server says its
// custom branding is entitled (Professional+ whitelabel).
export const DEFAULT_LOGO = '/waddlebot-logo.png';

/**
 * Resolve the login-page branding for a tenant.
 *
 * `tenant` is the `GET /api/v1/auth/tenant/:slug` payload. The server is the
 * gate: it only sets `whitelabeled: true` (and only then returns a custom logo
 * / welcome message) for a tenant entitled to `tenancy.whitelabel`, so the
 * client applies custom branding strictly when that flag is true and never
 * tries to decide entitlement itself.
 *
 * @param {object|null|undefined} tenant - the `tenant` object from the login-info response
 * @param {string|undefined} tenantSlug - the slug from the route, if any
 * @returns {{appName: string, logo: string, tagline: string}}
 */
export function resolveBranding(tenant, tenantSlug) {
  const base = {
    appName: 'Welcome to Waddles',
    logo: DEFAULT_LOGO,
    tagline: tenantSlug ? `Signing into: ${tenantSlug}` : 'Access your communities',
  };
  if (!tenant || tenant.whitelabeled !== true) {
    return base;
  }
  return {
    appName: tenant.displayName ? `Welcome to ${tenant.displayName}` : base.appName,
    logo: tenant.logoUrl || base.logo,
    tagline: tenant.config?.welcomeMessage || base.tagline,
  };
}
