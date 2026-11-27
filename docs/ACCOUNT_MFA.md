# Account MFA (two-step verification)

Hub accounts can add a second factor after the password. **Authenticator app (TOTP) is the
preferred and default method**; a 6-digit **email code** is a second option; **recovery codes**
are the fallback. Code: `hub_api/services/mfa_service.py`, routes `hub_api/blueprints/v1/mfa.py`,
webui `admin/hub_module/frontend/src/components/security/`.

## Login flow

```
POST /api/v1/auth/login (tenant slug valid, password OK, MFA active)
  -> 202 { mfaRequired, mfaToken, methods: ["totp", "email"?, "recovery"], expiresIn: 600 }
     (no cookie, no JWT: mfaToken is opaque, single-use, stored only as a SHA-256 hash)
POST /api/v1/auth/mfa/login/email   { mfaToken }                 (email method only)
POST /api/v1/auth/mfa/login/verify  { mfaToken, method, code }  -> 200 session + cookies
                                                                    amr = pwd+otp+mfa
```

Every path that mints a session from a local credential takes the same step when the account
has an ACTIVE factor:

| Path | MFA step |
|---|---|
| `POST /api/v1/auth/login` | yes; an unknown or inactive `tenantSlug` is refused (401) BEFORE the password or any MFA decision |
| `POST /api/v1/auth/admin` (legacy admin) | yes |
| `GET /api/v1/auth/verify-email` | yes: the address is marked verified, but the session waits for the factor (202) |
| `POST /api/v1/auth/temp-password` | yes when the identifier names an account; after MFA the SAME pending-link temp session is minted (`sub` 0), never the full account session |
| Passkey login | the passkey is itself the strong factor (`amr` from `services/auth_amr.py`) |
| OAuth / SSO callback | yes: redirect to the webui MFA step with the pending token in the URL fragment (see "Federated sign-in") |

## Endpoints (signed in, own account)

| Route | Purpose |
|---|---|
| `GET /api/v1/auth/mfa/status` | method states (0 none, 1 pending, 2 active), email availability, recovery codes left |
| `POST /totp/enroll` -> `/totp/confirm` | pending secret + otpauth URI (QR drawn in the browser), first code activates |
| `POST /email/enroll` -> `/email/confirm` | needs a verified email + a configured SMTP transport |
| `POST /email/send-code` | email a code for an account action (add a factor, disable, regenerate) |
| `POST /disable` | `{ method, code, password? }` -- re-auth + valid code |
| `POST /recovery-codes/regenerate` | re-auth + valid code -> 10 new codes |

**Adding a factor when one is already ACTIVE** (`/totp/enroll`, `/email/enroll`) takes the same
body as `/disable`: `{ password?, code }` -- re-auth plus a valid code from an existing factor. The
first factor needs no body. Activating a method cancels any other method still PENDING, so a
pending enrollment started before the first factor existed cannot be confirmed later.

**Recovery codes** are issued only when the user has none left (the first factor, or after all
were used); confirming a second method never replaces an existing set.

**Re-auth** = the current password, or for a password-less account a sign-in at most 5 minutes
old; when a factor is ACTIVE that sign-in must itself have completed MFA (`amr` contains
`mfa`/`otp`). A fresh OAuth/SSO session alone never adds or removes a factor.

**Notifications** (not a gate): the account's verified address is emailed whenever a factor is
added or removed, and when failed verification attempts reach 5 in a day. When email is
unavailable the change still happens and the miss is logged.

## Security properties

| Property | Implementation |
|---|---|
| Secret at rest | Envelope encryption (`services/envelope`) under the `system` tenant DEK, AAD = table/column/user UUID |
| Drift / replay | +/-1 30 s step; accepted step persisted by conditional UPDATE |
| Recovery codes | 10, single use, HMAC-SHA256 under a pepper derived from the `system` tenant's envelope data key (`key_version` per row survives DEK rotation); never in the DB; 503 `MFA_UNAVAILABLE` when the key service cannot answer |
| Attempt cap | 5 per challenge, reserved atomically BEFORE the check: `UPDATE ... SET attempts = attempts + 1 WHERE id = ? AND attempts < 5 AND consumed_at IS NULL` (0 rows = locked / burned) -- concurrent guesses cannot exceed it |
| Email code | 6 digits, salted hash, 10 min TTL, 3 sends / challenge, 30 s apart, 5 / user / hour; an account challenge is consumed by its first successful use |
| Rate limits | Valkey-backed, fail closed. Separate LOGIN and ACCOUNT buckets per user (login abuse cannot lock the owner out of settings): 20 / 15 min, 60 / day, exponential backoff 2^(n-5) s (cap 1 h) after 5 failures in a day. Per client IP on `/login/verify` (30 / 15 min) and `/login/email` (10 / 15 min). Global auth tier on top |
| Audit | `authn.mfa_challenge_issued`, `authn.mfa_verified`, `authn.mfa_failed`, `credential.mfa_enrolled`, `credential.mfa_disabled`, `credential.mfa_recovery_regenerated` -- ids and method only |
| Telemetry | `waddles_mfa_*` counters + `waddles_mfa_verify_duration_seconds` histogram, `mfa.*` spans |

## Flags (core-mechanism kill switches)

Evaluated at **global scope only** (the operator-plane `system` tenant) -- never with a tenant
slug taken from a request, so a request cannot flip them.

| Flag (env fallback) | Unseen / OFF | ON |
|---|---|---|
| `waddles.disable-mfa` (`FLAG_WADDLES_DISABLE_MFA`) | MFA enforced at login, enrollment open | **no NEW enrollment**; users with an ACTIVE factor are still challenged (never silently downgraded) |
| `waddles.disable-mfa-email` (`FLAG_WADDLES_DISABLE_MFA_EMAIL`) | email method usable when SMTP is configured | email method off; email-only users fall back to recovery codes |

There is no bypass flag.

## Federated sign-in (OAuth / SSO)

OAuth provider sign-ins (Discord, Twitch, Google, ...; `services/oauth_service.py::oauth_callback`)
and tenant SSO sign-ins (OIDC / Google Workspace / SAML; `services/sso_service.py::_finish_login`)
call the SAME `auth_service._require_mfa_if_enrolled` gate as the password path, right after the
identity is resolved. An account with an ACTIVE local factor gets **no session and no exchange
code**; one flow, one verify endpoint:

```
GET  /api/v1/auth/oauth/<platform>/callback      (or SSO /callback, /acs)
  -> 302/303 <frontend>/auth/mfa#pending=<token>&methods=totp,recovery&expiresIn=600
     no session / refresh cookie, no exchange code; Cache-Control: no-store,
     Referrer-Policy: no-referrer. The token is in the FRAGMENT only (never sent to a server,
     so never in access logs or Referer) and the webui scrubs it from the address bar.
webui /auth/mfa  -> POST /api/v1/auth/mfa/login/verify { mfaToken, method, code }
  -> 200 session + cookies, then the same finish as the password path (refresh user, dashboard)
```

| Detail | Behaviour |
|---|---|
| Challenge `auth_method` | `oauth_<platform>` / `sso_<protocol>` -- stored on the challenge, on the audit events (`mfa.challenge_issued`, `mfa.verified`) and the session's `auth_method` (`..._mfa`) |
| Tenant | SSO challenges carry the connection's tenant; the post-MFA session is minted for it (OAuth: `default`, as before) |
| `amr` after verify | `otp`+`mfa` (TOTP/email) or `mfa` (recovery) -- no `pwd`, the first factor was federated |
| Rate limits, lockout, single use, expiry | identical to the password path (same challenge row, same verify route) |
| Kill switch `waddles.disable-mfa` | never removes the step: enrolled users are always challenged |
| Not enrolled | unchanged: exchange-code redirect to `/auth/callback` |
| SSO telemetry | `sso.login` counter outcome `mfa_required` (not a failure) |

### Trusting the IdP's MFA (OIDC only, opt-in, default off)

A tenant may set `tenants.config.ssoTrustIdpMfa = true`. Then, for an OIDC / Google SSO sign-in
whose ID token `amr` contains `mfa`, `otp`, `hwk` or `swk`, the IdP's MFA stands in for the local
factor: the session is minted directly with `amr` = those values + `mfa` and `auth_method`
`sso_<protocol>_idp_mfa`, and an `mfa.verified` audit event (method `idp_amr`) is recorded so the
skip is never silent. Off (the default), absent or malformed `amr`, SAML (no `amr`), and OAuth
providers (plain OAuth2, no `amr`) always require the local factor.

## Email transport dependency

`services/mfa_email.py` imports `services.email_service` (PR #1541, `feature/email-verification`)
and calls `async send_email(*, to_email, subject, text_body, kind)` + `is_configured()`; kinds are
`mfa_code`, `mfa_factor_change`, `mfa_failed_attempts`. The transport's `EmailUnavailableError`
maps to `503 EMAIL_MFA_UNAVAILABLE` for codes; notifications log and continue. Until that module
is present and configured, email MFA shows as unavailable -- never a silent success.

## Policy

No tenant setting models "require MFA" yet; enforcement is per-user enrollment. The hook point
is `mfa_service.login_methods`.

## Migration

`alembic/versions/0145_hub_user_mfa.py` (`hub_user_mfa`, `hub_user_mfa_recovery_codes`,
`hub_mfa_challenges`); erased with the user (`services/erasure_registry.py` DELETE rules).
`0146_mfa_recovery_hmac.py` adds `hub_user_mfa_recovery_codes.key_version` and deletes the old
unkeyed SHA-256 rows (dev only; plaintext codes are never stored, so they cannot be re-keyed):
affected users keep their factors and regenerate codes from the security page.
