# Outbound Email and Account Email Verification

hub-api sends all mail through one service, `hub_api/services/email_service.py`, over SMTP.
Account email verification (`hub_api/services/email_verification.py`) is its first user.

## Policy

Each tenant has an effective signup allowlist. The resolver for it is
`email_verification.resolve_signup_policy`, and every signup path calls it.

- It intersects the platform list (`hub_settings.signup_allowed_domains`) with the tenant's own
  list (`tenant_settings` key `signup_allowed_domains`).
- If one of the two lists is empty, the other list is used on its own.
- If both lists are set but share no domain, no address can sign up.
- Matches are exact domain matches after normalization: lower-cased, trailing dot stripped,
  IDNA (punycode) encoded, so `Bücher.example.` and `xn--bcher-kva.example` are the same.

| Signup path | Tenant unrestricted | Tenant restricted |
|---|---|---|
| Local email/password (default tenant) | Pending until the emailed link is used | Same, and the domain must match |
| OAuth (Discord, Twitch, Google; default tenant) | No email needed; an address the provider did not verify is never attached (placeholder used) | Domain must match; pending until verified |
| Tenant SSO (SAML/OIDC) | IdP email is enough | Domain must match; pending until verified |

- No session is issued while an account is pending. Login answers `403 requiresVerification`,
  or the SSO/OAuth redirect goes to `/login?error=email_verification_required`.
- Existing users are not blocked retroactively. However, an unverified OAuth or SSO user whose
  tenant later adds a restriction gets a link by email at their next login and is held until
  they verify it.
- OAuth: a provider email counts as verified only when the provider asserts it
  (`email_verified=true`; Discord `verified`). Twitch and Slack assert nothing, so their
  addresses are treated as unverified.
- Using a link verifies the address only. It never starts a session: the user then signs in
  normally (password, OAuth or SSO), so clicking someone else's link never logs you in.
- When a link is used, the domain is checked again with `resolve_signup_policy` (platform ∩
  the tenant the account signed up into; the SSO connection's tenant for SSO accounts).
- Inactive accounts cannot verify; deactivation clears outstanding tokens.
- Any email change (admin today, self-service later) goes through
  `email_verification.email_change_fields` + `schedule_verification`: normalized, domain
  re-checked, marked unverified, old tokens cleared, a new link mailed.
- Kill switch `waddles.disable-email-verification` is opt-out: unset or OFF means verification
  is enforced. It is platform-wide: evaluated for the `system` scope only, so a per-tenant
  override has no effect.

### Tenant setting

| Method + path | Scope |
|---|---|
| `GET /api/v1/tenant/<slug>/signup-domains` | `tenant:admin` |
| `PUT /api/v1/tenant/<slug>/signup-domains` `{domains: [...]}` | `tenant:admin` + `tenant.signup_domains:write` |

- The URL slug must match the tenant in the token.
- At most 50 domains, each validated as a DNS name.
- The generic `/settings` routes never list or write this key.
- In the webui, it is the "Signup email domains" card on Tenant settings.

## Tokens

- 32 random bytes. Only the SHA-256 digest is stored, in `hub_users.email_verification_token`.
- Single use, 24 h TTL (`EMAIL_VERIFICATION_TTL_HOURS`), compared in constant time.
- Resend has a per-user cooldown (`EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS`, default 60 s),
  enforced silently: the answer never changes.
- Every verification mail (register, resend, OAuth hold, SSO hold) also charges the same
  per-IP auth-tier limiter bucket (`auth:ip:<ip>`) as the request limiter.
- The mail reads "Someone requested an account with this address" and carries two links:
  confirm `<frontend_origin>/verify-email#token=...` and "this wasn't me"
  `...#token=...&action=cancel`, which deletes the still-unverified pending account. The token
  sits in the URL fragment, so it is never sent to a server; the webui posts it.
- Migration `0145_email_verif_token_digest` clears raw tokens left by older code. Affected users
  request a new link.

## Endpoints

| Method + path | Purpose |
|---|---|
| `POST /api/v1/auth/register` | Same `202 requiresVerification` for new AND existing addresses (the existing owner is mailed "someone tried to register with your address"); `403 email_domain_not_allowed`, `503 email_not_configured` |
| `POST /api/v1/auth/verify-email` `{token}` | Verify the address. `200 {success, message}`, no session, no cookie |
| `POST /api/v1/auth/cancel-signup` `{token}` | "This wasn't me": delete the pending unverified account |
| `POST /api/v1/auth/resend-verification` `{email}` | Always the same `200` body (known, unknown, cooldown) |
| `GET /api/v1/auth/oauth/<p>/callback` | Redirects to `/login?error=email_verification_required` or `email_domain_not_allowed` |

## Failure behaviour

- SMTP host unset: any flow that needs email fails with `503 email_not_configured` before it
  creates an account. Nothing is ever auto-verified.
- Register and resend send off the request path (a tracked background task drained on
  shutdown), so latency does not reveal whether an account exists. A relay failure there is
  logged at ERROR (`email_verification.background_failed`) and counted
  (`waddles_email_verification_total{event=background,outcome=failed}`); the account stays
  pending and the user can resend. OAuth/SSO holds still send in-band and answer
  `503 verification_email_failed` on relay failure.
- SMTP set but invalid, for example a missing or non-FQDN `SMTP_EHLO_NAME`: hub-api refuses to
  start.

## Developer interface (other mail types, such as email OTP)

```python
from services import email_service

await email_service.send_email(
    to_email=address, subject="...", text_body="...", html_body=None, kind="mfa_email_otp"
)
```

- `send_email` raises `email_service.EmailUnavailableError` on any failure. The subclasses are
  `EmailNotConfiguredError`, `SmtpConfigError` and `SmtpDeliveryError`.
- `email_service.is_configured()` lets a flow fail fast before it creates any state.
- `kind` must match `^[a-z][a-z0-9_]{0,31}$`. It labels metrics and spans.
- Recipient addresses never appear in logs, spans or metric labels. Logs carry a keyed
  fingerprint instead.

## Configuration (Helm `smtp` block)

| Value | Env | Notes |
|---|---|---|
| `smtp.host` | `SMTP_HOST` | Blank turns email off |
| `smtp.port` | `SMTP_PORT` | Default 587 |
| `smtp.tls` | `SMTP_TLS` | `starttls` (mandatory: fails loud if the relay does not offer it) or `tls` |
| `smtp.ehloName` | `SMTP_EHLO_NAME` | Required. Must be an FQDN: not localhost, not an IP, never the pod name |
| `smtp.from` / `smtp.fromName` | `SMTP_FROM` / `SMTP_FROM_NAME` | `from` is required |
| `smtp.timeoutSeconds` | `SMTP_TOTAL_TIMEOUT` | Total budget across retries |
| `smtp.existingSecret` | `SMTP_USERNAME` / `SMTP_PASSWORD` | Optional AUTH. The Secret has keys `username` and `password`. Blank means no AUTH |
| `smtp.egress.enabled` | n/a | Adds a hub-api CNP rule allowing only `toFQDNs <host>:<port>` |

dal2alpha and local alpha use the Google Workspace SMTP relay (`smtp-relay.gmail.com:587`).
It authenticates by our static egress IP, so no Secret is needed.

Other settings: certificate verification is always on, the minimum TLS version is 1.2, and the
chart cannot render plaintext SMTP. The DSAR mailer uses `config.dsarSmtp` when that host is set
and this relay otherwise.
