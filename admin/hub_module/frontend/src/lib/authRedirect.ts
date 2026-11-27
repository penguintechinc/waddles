/**
 * Shared 401 session-redirect policy for `services/api.js` and `lib/apiClient.ts`.
 *
 * Public and auth routes (`/`, `/login`, `/communities`, etc.) must NEVER force a
 * `window.location.href = '/login'` reload on 401 — doing so kicks unauthenticated
 * visitors off the landing page and causes an infinite reload + 429 rate-limit loop
 * when `/login` itself mounts and fires background checks like `GET /api/v1/flags`.
 */

const PUBLIC_ROUTE_PREFIXES = [
  '/login',
  '/register',
  '/forgot-password',
  '/auth/callback',
  '/auth/mfa',
  '/communities',
  '/live',
  '/users',
  '/secret',
  '/design-system',
  '/cookie-policy',
  '/privacy-rights',
  '/book',
] as const;

/** Extract pathname from `window.location` (supporting test stubs that only set `href`). */
export function getCurrentPathname(): string {
  if (typeof window === 'undefined' || !window.location) {
    return '';
  }
  const { pathname, href } = window.location;
  if (typeof pathname === 'string' && pathname.length > 0) {
    return pathname;
  }
  if (typeof href === 'string' && href.length > 0) {
    try {
      return new URL(href, 'http://localhost').pathname;
    } catch {
      return href.split('?')[0] ?? '';
    }
  }
  return '';
}

/** True when `pathname` is a public or auth route that must not redirect to `/login`. */
export function isPublicRoute(pathname: string): boolean {
  if (pathname === '/') {
    return true;
  }
  return PUBLIC_ROUTE_PREFIXES.some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
}

/** Pre-auth MFA login calls: a 401 there is a wrong code, never an expired session. */
const MFA_LOGIN_PREFIX = '/api/v1/auth/mfa/login/';

/**
 * Skip `/auth/refresh` for optional background flag checks on public/auth routes, and for the
 * pre-auth MFA login step (there is no session to refresh; the caller needs the real 401).
 */
export function shouldSkipAuthRefresh(url: string | undefined): boolean {
  if (typeof url === 'string' && url.startsWith(MFA_LOGIN_PREFIX)) return true;
  return url === '/api/v1/flags' && isPublicRoute(getCurrentPathname());
}

/** Redirect to `/login` only when the user is on a protected route. */
export function redirectToLoginIfProtected(): void {
  const pathname = getCurrentPathname();
  if (!isPublicRoute(pathname)) {
    window.location.href = '/login';
  }
}
