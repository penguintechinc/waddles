# Account MFA (two-step verification)

Hub accounts can add a second factor after the password. **Authenticator app (TOTP) is the
preferred and default method**; a 6-digit **email code** is a second option; **recovery codes**
are the fallback. Code: `hub_api/services/mfa_service.py`, routes `hub_api/blueprints/v1/mfa.py`,
webui `admin/hub_module/frontend/src/components/security/`.

## Login flow

```
POST /api/v1/auth/login (password OK, MFA active)
  -> 202 { mfaRequired, mfaToken, methods: ["totp", "email"?, "recovery"], expiresIn: 600 }
     (no cookie, no JWT: mfaToken is opaque, single-use, stored only as a SHA-256 hash)
POST /api/v1/auth/mfa/login/email   { mfaToken }                 (email method only)
POST /api/v1/auth/mfa/login/verify  { mfaToken, method, code }  -> 200 session + cookies
                                                                    amr = pwd+otp+mfa
```

`/api/v1/auth/admin` (legacy admin login) takes the same step.

## Endpoints (signed in, own account)

| Route | Purpose |
|---|---|
| `GET /api/v1/auth/mfa/status` | method states (0 none, 1 pending, 2 active), email availability, recovery codes left |
| `POST /totp/enroll` -> `/totp/confirm` | pending secret + otpauth URI (QR drawn in the browser), first code activates, 10 recovery codes shown once |
| `POST /email/enroll` -> `/email/confirm` | needs a verified email + a configured SMTP transport |
| `POST /email/send-code` | email a code for disable / regenerate |
| `POST /disable` | `{ method, code, password? }` -- re-auth + valid code |
| `POST /recovery-codes/regenerate` | re-auth + valid code -> 10 new codes |

## Security properties

| Property | Implementation |
|---|---|
| Secret at rest | Envelope encryption (`services/envelope`) under the `system` tenant DEK, AAD = table/column/user UUID |
| Drift / replay | +/-1 30 s step; accepted step persisted by conditional UPDATE |
| Recovery codes | 10, single use, SHA-256 at rest |
| Email code | 6 digits, salted hash, 10 min TTL, 5 wrong attempts burn the challenge, 3 sends / challenge, 30 s apart, 5 / user / hour |
| Rate limits | 20 verifications / user / 15 min (Valkey-backed, fail closed) + global auth tier |
| Audit | `authn.mfa_challenge_issued`, `authn.mfa_verified`, `authn.mfa_failed`, `credential.mfa_enrolled`, `credential.mfa_disabled`, `credential.mfa_recovery_regenerated` -- ids and method only |
| Telemetry | `waddles_mfa_*` counters + `waddles_mfa_verify_duration_seconds` histogram, `mfa.*` spans |

## Flags (core-mechanism kill switches)

| Flag (env fallback) | Unseen / OFF | ON |
|---|---|---|
| `waddles.disable-mfa` (`FLAG_WADDLES_DISABLE_MFA`) | MFA enforced at login, enrollment open | legacy single-factor login, no new enrollment |
| `waddles.disable-mfa-email` (`FLAG_WADDLES_DISABLE_MFA_EMAIL`) | email method usable when SMTP is configured | email method off |

## Email transport dependency

`services/mfa_email.py` imports `services.email_service` (built on `feature/email-verification`)
and needs `async send_email(*, to, subject, text)` + `is_configured()`. Until that module is
present and configured, email MFA shows as unavailable and every send fails with
`503 EMAIL_MFA_UNAVAILABLE` -- never a silent success.

## Policy

No tenant setting models "require MFA" yet; enforcement is per-user enrollment. The hook point
is `mfa_service.login_methods`.

## Migration

`alembic/versions/0145_hub_user_mfa.py` (`hub_user_mfa`, `hub_user_mfa_recovery_codes`,
`hub_mfa_challenges`); erased with the user (`services/erasure_registry.py` DELETE rules).
