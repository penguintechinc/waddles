# Outbound Email and Account Email Verification

hub-api sends all mail through one service, `hub_api/services/email_service.py`, over SMTP.
Account email verification (`hub_api/services/email_verification.py`) is its first user.

## Policy

| Signup path | Allowlist (`signup_allowed_domains`) empty | Allowlist set |
|---|---|---|
| Local email/password | Pending until the emailed link is used | Same, and the domain must match |
| OAuth (Discord, Twitch, Google...) | No email needed | Provider email must be in an allowed domain; account pending until verified |

- No session is issued while an account is pending. Login answers `403 requiresVerification`.
- The domain match is exact. It is checked at signup and again when the link is used.
- Kill switch: `waddles.disable-email-verification` (opt-out). Unset or OFF means verification
  is enforced. ON restores the legacy `signup_require_email_verification` setting for local
  signups and skips the OAuth domain gate.

## Tokens

- 32 random bytes. Only the SHA-256 digest is stored, in `hub_users.email_verification_token`.
- Single use, 24 h TTL (`EMAIL_VERIFICATION_TTL_HOURS`), compared in constant time.
- Resend has a per-user cooldown (`EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS`, default 60 s).
  It answers `429` with `retryAfterSeconds`. The auth-tier IP rate limiter also applies.
- The link is `<frontend_origin>/verify-email#token=...`. The token sits in the URL fragment, so
  it is never sent to a server. The webui posts it to `POST /api/v1/auth/verify-email`.
- Migration `0145_email_verif_token_digest` clears raw tokens left by older code. Affected users
  request a new link.

## Endpoints

| Method + path | Purpose |
|---|---|
| `POST /api/v1/auth/register` | `202 requiresVerification`, `403 email_domain_not_allowed`, `503 email_not_configured` / `verification_email_failed` |
| `POST /api/v1/auth/verify-email` `{token}` | Consume the token and start a session |
| `POST /api/v1/auth/resend-verification` `{email}` | New link. Same answer for unknown addresses; `429` on cooldown |
| `GET /api/v1/auth/oauth/<p>/callback` | Redirects to `/login?error=email_verification_required` or `email_domain_not_allowed` |

## Failure behaviour

- SMTP host unset: any flow that needs email fails with `503 email_not_configured` before it
  creates an account. Nothing is ever auto-verified.
- Relay failure after the account exists: `503 verification_email_failed`. The account stays
  pending and the user can resend.
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
