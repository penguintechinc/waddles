# Public privacy request form

Standalone pages, outside auth and outside the site layout (no navigation, mobile-friendly):

| Route | Page |
|---|---|
| `/privacy-request/:tenantSlug` | Request form (email, request type, privacy law, optional message, captcha) plus a "Resend verification email" section with its own captcha |
| `/privacy-request/verify` | "Confirm your email" landing page for the emailed link |

Code: `pages/privacy/PublicPrivacyRequest.jsx`, `PublicPrivacyVerify.jsx`, `components/Captcha.tsx`, `services/publicPrivacyApi.ts` (plain `fetch`, `credentials: 'omit'`, no auth interceptor). Backend contract: [dsar-inbox.md](dsar-inbox.md).

## Behavior

- Submit stays disabled until the captcha produced a token (fail closed). Tokens are single use, so the captcha is reset after every submit attempt.
- A `202` always shows the same message ("if the details are valid, we will email a verification link; your request is not processed until you confirm"). Errors never say whether the tenant or email exists, and per-email/per-tenant abuse limits are silent (same 202): 429 "too many requests" now only appears for a burst from one IP, `DSAR_CAPTCHA_FAILED` "security check failed", 503 "temporarily unavailable", any other 400 "check the details".
- Verify page: reads `token` from the URL fragment (`#token=...`) or `?token=`. On load it only **previews** the token (`POST /api/v1/public/privacy-requests/verify/preview`, token in the body so it never reaches a URL or access log; NON-consuming) and shows WHICH request is being confirmed (type and privacy law) above the button. It never verifies on load, because mail scanners prefetch links and the token is single use. Only the "Confirm your email" button POSTs `POST /api/v1/public/privacy-requests/verify`. An expired, used or invalid token is caught at preview (no button) and shows one message and a resend path: if the link carries `?tenant=<slug>` it links back to the form, otherwise it explains how to find the resend option. A transient preview failure offers "Try again".
- Resend: the resend form asks for the **type of request** (defaults to the main form's choice) and sends it as `requestType`; only the pending request of that type is re-sent, so a link can never verify a different request on the same address. The response is the same uniform message whether or not anything was sent (abuse limits are silent, there is no 429 for them).
- Verification email is sent by hub-api over SMTP when `DSAR_SMTP_HOST` is configured (see dsar-inbox.md, Operations, SMTP setup). The link is `<verify page>?tenant=<slug>#token=<token>`, which the verify page reads. When SMTP is not configured the backend fails loud and the request stays unverified until it expires (72 hours); the response to the requester is identical either way, and the UI fakes nothing.

## Captcha (operator notes)

The webui holds no keys and no provider logic: `Captcha.tsx` reads `GET /api/v1/public/privacy-requests/captcha-config` and renders what it names. If that call fails or the provider is unknown or half-configured, the form shows "temporarily unavailable" and cannot be submitted.

Provider env (hub-api, never the webui): `DSAR_CAPTCHA_PROVIDER` = `turnstile` | `recaptcha` | `altcha` (unset defaults to `altcha`; the chart generates `DSAR_ALTCHA_HMAC_KEY` into `waddlebot-secrets` create-if-absent, never overwriting it, so the form works in every tier with no operator Secret; turnstile/recaptcha are opt-in with an operator-supplied `waddlebot-dsar-captcha` Secret; a configured provider missing its key logs an ERROR and answers 503, never a bypass), with `DSAR_CAPTCHA_SITE_KEY` / `DSAR_CAPTCHA_SECRET` (turnstile, recaptcha) or `DSAR_ALTCHA_HMAC_KEY` (altcha). Server-side binding (optional env): `DSAR_CAPTCHA_HOSTNAMES` (default: host of `DSAR_VERIFY_URL_BASE`), `DSAR_CAPTCHA_ACTION` (default `dsar_request`, matches the widget), `DSAR_RECAPTCHA_VERSION` (`v3` default). If the provider script does not load within 10 s the widget shows the "temporarily unavailable" state (fail closed) and drops the script so a remount retries.

| Provider | Browser behavior | Origins to allow if a CSP is added |
|---|---|---|
| turnstile | loads `https://challenges.cloudflare.com/turnstile/v0/api.js`, renders an explicit widget | `script-src`, `frame-src`, `connect-src`: `https://challenges.cloudflare.com` |
| recaptcha | loads `https://www.google.com/recaptcha/api.js?render=<siteKey>` and runs reCAPTCHA v3 `execute` (action `dsar_request`, token refreshed every 90 s) | `script-src`: `https://www.google.com`, `https://www.gstatic.com`; `frame-src`: `https://www.google.com`; `connect-src`: `https://www.google.com` |
| altcha | no third-party script; solves the signed proof-of-work challenge in the browser with WebCrypto (no npm dependency) | none (same-origin API only) |

Scripts load only on these pages, only for the configured provider. The webui server (`admin/hub_module/webui-server`) currently sets no Content-Security-Policy, so nothing needs changing; add the origins above if one is introduced.
