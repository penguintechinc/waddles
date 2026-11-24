/**
 * Client for the unauthenticated public privacy-request routes (`/api/v1/public/...`).
 * Plain fetch with `credentials: 'omit'`: no cookies, no auth interceptor, no refresh/redirect.
 * Every well-formed submit gets a uniform 202, so callers must never infer tenant/email existence.
 */

const API_BASE: string = import.meta.env.VITE_API_URL ?? '';

/** Error carrying the HTTP status and the server's stable `error.code`. */
export class PublicApiError extends Error {
  status: number;
  code: string | null;

  constructor(status: number, code: string | null, message: string) {
    super(message);
    this.name = 'PublicApiError';
    this.status = status;
    this.code = code;
  }
}

export interface CaptchaConfig {
  provider: string;
  siteKey: string | null;
  challengeUrl: string | null;
}

export interface AltchaChallenge {
  algorithm: string;
  challenge: string;
  maxnumber: number;
  salt: string;
  signature: string;
}

/** Non-consuming confirm-page data: what the emailed link would verify (no PII, no tenant info). */
export interface VerifyPreview {
  valid: boolean;
  requestType: number;
  /** The regime the request will hold once THIS link is confirmed (a draft bound to it, else the current one). */
  regime: number;
  /** Plain-text message the request will hold once confirmed; render escaped (never as HTML). */
  message: string | null;
  /** True when confirming changes the stored content (a resubmission waiting on this link). */
  contentChanged: boolean;
  expiresAt: string | null;
}

export interface PublicResendBody {
  email: string;
  /** Resend rotates only the pending request of THIS type (a link never verifies another one). */
  requestType: number;
  captchaToken: string;
}

export interface PublicSubmitBody {
  email: string;
  requestType: number;
  regime: number;
  message?: string;
  captchaToken: string;
}

/** fetch wrapper: JSON in/out, never sends credentials, converts non-2xx to PublicApiError. */
async function call<T>(path: string, init: { method: string; body?: unknown } = { method: 'GET' }): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  const req: RequestInit = { method: init.method, credentials: 'omit', headers };
  if (init.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    req.body = JSON.stringify(init.body);
  }
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, req);
  } catch {
    throw new PublicApiError(0, null, 'Network error');
  }
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const e = (data as { error?: { code?: unknown; message?: unknown } } | null)?.error;
    throw new PublicApiError(
      res.status,
      typeof e?.code === 'string' ? e.code : null,
      typeof e?.message === 'string' ? e.message : `Request failed (${res.status})`,
    );
  }
  return data as T;
}

const P = '/api/v1/public';

export const publicPrivacyApi = {
  /** GET provider + public site key; 503 when no provider is configured. */
  getCaptchaConfig: (): Promise<CaptchaConfig> => call<CaptchaConfig>(`${P}/privacy-requests/captcha-config`),

  /** GET a signed single-use altcha proof-of-work challenge. */
  getAltchaChallenge: (url?: string | null): Promise<AltchaChallenge> =>
    call<AltchaChallenge>(url ?? `${P}/privacy-requests/altcha-challenge`),

  /** POST the public form; uniform 202 for every valid submit. */
  submit: (tenantSlug: string, body: PublicSubmitBody): Promise<{ accepted: boolean }> =>
    call(`${P}/${encodeURIComponent(tenantSlug)}/privacy-requests`, { method: 'POST', body }),

  /** POST a verification re-send (own rate limit + captcha); uniform 202. */
  resendVerification: (tenantSlug: string, body: PublicResendBody): Promise<{ accepted: boolean }> =>
    call(`${P}/${encodeURIComponent(tenantSlug)}/privacy-requests/resend-verification`, { method: 'POST', body }),

  /**
   * POST the token for a NON-consuming preview (request type, regime, expiry). The token travels in
   * the body, never a URL, so it cannot land in proxy access logs; the link is not burned.
   */
  previewVerification: (token: string): Promise<VerifyPreview> =>
    call<VerifyPreview>(`${P}/privacy-requests/verify/preview`, { method: 'POST', body: { token } }),

  /** POST the emailed single-use token (never GET: link scanners prefetch). */
  verify: (token: string): Promise<{ verified: boolean }> =>
    call(`${P}/privacy-requests/verify`, { method: 'POST', body: { token } }),
};
