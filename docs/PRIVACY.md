# Privacy & Data Handling

Waddles' approach to user data, privacy by design, and GDPR compliance — including the reasoning behind each architectural decision. This document describes what the `hub_api` implementation actually does; where it and the code disagree, the code is authoritative and this document is the bug.

---

## Privacy by Design Principles

Waddles applies privacy as a structural property, not an afterthought:

- **Pseudonymization by default** — internal systems route by opaque IDs, never by email or username
- **Data minimization** — only the fields needed for a given operation are queried and returned
- **Aggregate-only analytics** — analytics consumers see histograms and counts, never individual rows
- **Fail-closed auth** — every analytics and data-subject endpoint is authenticated; analytics reads require an explicit OIDC scope (`analytics:read`, `community.analytics:admin`, `users:admin`), and erasure additionally re-confirms the password when the account has one
- **Audit trails without PII** — deletion records store counts and timestamps, not field values

---

## Pseudonymous Identifiers

Across most of Waddles' internal services, users are referenced by **UUID or integer ID**, not by email or username. This is a deliberate pseudonymization strategy:

- The **router module**, **analytics core**, and **AI modules** receive and log `hub_user_id` (integer) or `platform_user_id` (platform-specific opaque string like a Discord snowflake) — never an email address
- **Rate limit keys** use the pattern `ratelimit:{community_id}:{user_id}:{limit_type}:{hour_bucket}` — no PII in Redis keyspace
- **AI chatter rate limit keys** use `ai_chatter:{community_id}:{user_id}:{bucket}:{window_seconds}` — same pattern
- **Service-to-service headers** carry `X-Caller-User-ID` (integer ID) and `X-Caller-Role` — not names or emails
- **Analytics-core endpoints** accept `hub_user_id` as a path parameter, not a search by email

The hub backend is the **only** service that maps between real identifiers (email, username) and internal IDs. All other services operate purely on opaque IDs.

This means that if a non-hub service's logs or DB were compromised in isolation, no PII is directly exposed — only numeric IDs that require access to the hub DB to resolve.

---

## Cookie Consent

### What We Use Cookies For

The backend (`hub_api`) sets exactly three first-party cookies, all `Secure`:

| Cookie | Purpose | Attributes | Lifetime |
|--------|---------|------------|----------|
| `wb_session` | Session JWT for the browser app (`services/session_cookie.py`) | `HttpOnly`, `SameSite=Lax` — never readable by page script | 24 hours |
| `waddlebot_consent_id` | Opaque UUID linking the browser to its `cookie_consent` row (`services/cookie_consent_service.py`) | `SameSite=Lax`; readable by page script — it is a grouping id, not a credential | 12 months |
| `wb_sso_bind` | Binds one SSO login attempt to the browser that started it — a login-CSRF token, not a session credential (`blueprints/v1/sso.py`) | `HttpOnly`, path-scoped to that SSO connection, `SameSite=Lax` (`None` for SAML's cross-site POST) | The sign-in attempt (`state_ttl_s`) |

There is no separate CSRF cookie: CSRF is mitigated by `SameSite=Lax` on the session cookie (a cross-site request carries neither a bearer header nor the cookie). The browser also keeps the consent choice in `localStorage` (`cookie_consent`) so the banner does not reappear.

**No third-party cookies, no advertising cookies, no browser analytics.** No analytics, session-recording or advertising script is loaded by the web app or the marketing site. The vendor cookies the cookie-policy page used to list (Google Analytics, Mixpanel, Hotjar, Facebook Pixel, Google Ads, LinkedIn) were never set by Waddles and have been removed from the policy. See *Third-Party Services and Analytics* below.

### Cookie Consent Flow

Users are presented with a cookie consent banner on first visit. The choice is recorded in two server-side tables (plus the browser `localStorage` copy above):

- `cookie_consent` table: one record per consent id — `user_id` (null until the visitor signs in), the `preferences` object (`necessary`, `functional`, `analytics`, `marketing`, `doNotSell`), `consent_version`, `consent_method`, IP address, user agent, and `consented_at` / `updated_at` / `expires_at` (a consent expires after 12 months)
- `cookie_audit_log` table: one row per consent event — `ACCEPT` (carries the IP address and user agent), `UPDATE` (one row per changed category, with previous and new value) or `REVOKE` — each with the consent version and a timestamp. A failed audit write is raised and counted, never swallowed

Both tables are **retained** by the GDPR data deletion flow (see *What Is Retained* below): the controller must be able to *demonstrate* that consent was obtained (GDPR Art. 5(2) accountability), so the consent record and its change log outlive the data subject's other data.

### Preferences, Withdrawal and Global Privacy Control

Users can update (`PATCH /api/v1/cookie/preferences`) or withdraw (`DELETE /api/v1/cookie`) consent at any time. **Withdrawing does not delete the consent record**: it resets `functional`, `analytics` and `marketing` to `false` in place and writes a `REVOKE` row to `cookie_audit_log`. Preference writes *merge* over the stored object, so a CCPA/CPRA `doNotSell` opt-out survives a withdrawal or an unrelated update — only an explicit `doNotSell=false` clears it.

A `Sec-GPC: 1` (Global Privacy Control) request header is honoured server-side: it forces `doNotSell` on and `marketing` off regardless of the request body. A missing header is never read as consent. ("Do Not Track" is not a supported signal.)

---

## Data Deletion (GDPR Article 17 — Right to Erasure)

Users can request deletion of their personal data from the **Your privacy rights** page (`/privacy-rights` → *Delete my data*), which calls `DELETE /api/v1/user/me/data`. An account with a password must re-confirm it; the page tells the user, before they confirm, that linked platform identities and reputation history are kept.

### What Gets Deleted

The deletion runs as a single, **all-or-nothing** database transaction (`services/data_privacy_service.py::anonymize_user_data`, shared by self-service and the Enterprise admin console):

| Table | Action |
|-------|--------|
| `hub_user_profiles` | Hard delete |
| `hub_sessions` | Hard delete |
| `hub_temp_passwords` | Hard delete |
| `user_passkeys` | Hard delete |
| `activity_message_events` | Hard delete |
| `activity_watch_sessions` | Hard delete |
| `hub_chat_messages` | Hard delete (rows the user *sent*, every community) |
| `hub_users` | **Anonymized in-place** (see below) |
| `data_deletion_requests` | Completion row inserted (see *Deletion Audit Trail*) |

**Atomicity.** Every delete, the `hub_users` anonymization and the `completed` ledger row are issued in one executor job and committed once. If any statement fails, everything is rolled back — the account is left exactly as it was, never half-erased — a separate `failed` ledger row records the attempt, and the original error is returned (a `500`; the request can simply be retried). Erasure never ends in a state where the data is gone but the proof of erasure is missing, or vice versa.

**Export/erasure symmetry.** Everything the Art. 15 export discloses from the message/activity tables (`message_activity`, `watch_activity`, `chat_messages`) is removed by Art. 17 erasure. `hub_chat_messages` was previously exported but not erased (GRC finding #1, fixed).

### Anonymize In-Place: Why Not Hard Delete `hub_users`?

The `hub_users` row is anonymized rather than deleted:

```sql
UPDATE hub_users SET
  email        = 'deleted_{id}@deleted.waddlebot',
  username     = 'deleted_{id}',
  display_name = NULL,
  password_hash = NULL,
  avatar_url   = NULL,
  email_verification_token = NULL,
  password_reset_token     = NULL,
  is_active    = FALSE,
  updated_at   = now()
WHERE id = {userId}
```

**Reason:** `hub_users.id` is a foreign key referenced by dozens of tables across the schema (community memberships, reputation events, activity records, etc.). A hard delete would require cascading deletes across the entire database or leave orphaned rows — neither is safe at scale.

The anonymized `hub_users` row contains no PII: the email and username are non-identifiable placeholders, and the display name, avatar, password hash and verification/reset tokens are nulled. Other tables that still hold personal data about the user are listed under *What Is Retained* below — each is a per-table decision recorded there, not an omission.

### What Is Retained (and Why)

Four categories of data are deliberately **not** deleted, each with a distinct legal basis under GDPR Article 6 or accountability duty under Article 5(2):

#### 0. Consent and audit logs — `cookie_consent`, `cookie_audit_log`, `audit_log`, `data_deletion_requests`
*Legal basis: Legal obligation / accountability (Article 6(1)(c), Article 5(2))*

Consent records, the consent change log, the platform audit trail and the deletion-request ledger are **never** deleted by erasure. They are the controller's proof that consent was lawfully obtained and that the erasure request was honoured; deleting them would destroy the evidence the regulator can demand. They hold identifiers and technical metadata (user id, consent choices, IP, user agent) but not the profile, credentials, or message content that erasure removes. This is pinned in code (`ERASURE_RETAINED_TABLES`) and by regression tests (`hub_api/tests/test_data_privacy_erasure.py::TestRetention`).

#### 1. `hub_user_identities` — Platform Account Links
*Legal basis: User's own legitimate interest (Article 6(1)(f)) — account reclaim*

The rows linking a user's Discord ID, Twitch ID, etc. to their `hub_user_id` are kept. This serves two purposes:

- **Account reclaim**: If a user returns and logs in via their platform account, the OAuth flow (`_find_or_create_user_from_oauth` in `services/oauth_service.py`) finds the existing identity link and reconnects them to their original `hub_user_id` — including their full reputation history. See *Account Reclaim After Deletion* below.
- **Platform identity belongs to the platform**: Discord/Twitch user IDs are owned and managed by those platforms. Deleting them from our records does not erase them from the source platform.

The retained row is the whole `hub_user_identities` record: `platform`, `platform_user_id`, **`platform_username`**, **`avatar_url`**, `is_primary` and the `linked_at` / `last_used` timestamps. The username and avatar are the platform's public handle and picture and are refreshed from the platform on the user's next login.

#### 2. `reputation_tenant` + `reputation_events` — Score & Audit Trail
*Legal basis: Legitimate interest (Article 6(1)(f)) — platform integrity / anti-gaming*

Reputation scores and their audit trail are retained. **This is disclosed to users before they confirm deletion.**

Allowing reputation reset via deletion would be a trivially exploitable loophole (farm score → delete → repeat). Reputation is also a community-wide signal that affects other users' experiences and community health calculations. The `reputation_events` trail justifies the retained score — without it, the score would be an unexplained number with no audit basis.

#### 3. `community_members` — Membership Row (**not** modified by erasure)
*Legal basis: Same as above — FK integrity + reputation retention*

The membership row is kept **as-is**. Erasure does not touch `community_members`: `display_name`, `avatar_url`, `bio`, `social_links` and `platform_user_id` remain on the row. This is a **known gap against Article 17** — an earlier revision of this document stated these fields were nulled, but neither the self-service erasure code nor the earlier Node implementation ever did so. Tracked for a code fix; until then a user who wants a community display name removed must ask the community admin to remove the member.

### Also Not Reached by Self-Service Erasure: `ephemeral_pseudonyms.handle`

Unlinked platform accounts are represented by a pseudonym UUID; the raw platform handle behind it is stored in `ephemeral_pseudonyms.handle`. Self-service erasure does not reach it (there is no `hub_users` row to erase for these accounts). Wiping a handle is an operator/DSAR operation (`identity_resolution_service.erase_pseudonym_handles`, deliberately not exposed on the gRPC surface), recorded in `identity_resolution_events` without PII.

### Account Reclaim After Deletion

If a user deletes their data and later returns:

1. They log in via a linked platform account (Discord, Twitch, etc.)
2. `_find_or_create_user_from_oauth()` queries `hub_user_identities` by `(platform, platform_user_id)` and finds the retained record
3. The record resolves to the original `hub_user_id`, and a new session is minted for it
4. Their reputation score and community membership reputation are fully intact
5. The identity row's `platform_username`, `avatar_url` and `last_used` are refreshed from the platform. **The anonymized `hub_users` row is not re-populated and is not re-activated** — it keeps its `deleted_{id}` placeholders and `is_active = false` until the user sets new profile details

The platform identity (Discord snowflake, Twitch ID) acts as the **permanent identity anchor** across the entire account lifecycle — including through deletion and return. A login by email never adopts an existing account: an email match with no linked identity is refused with a conflict, so the retained anonymous row cannot be claimed by e-mail.

### Deletion Audit Trail

> Enterprise tenants additionally get erasure and DSAR-export requests recorded in the tamper-evident audit chain (actor is a `hub_users.uuid`, never a name or e-mail) -- see [Enterprise Audit Logging](compliance/audit-logging.md). The rights themselves are available in every tier.

Every deletion attempt is recorded in `data_deletion_requests`. This table stores no PII values — only metadata:

| Column | Description |
|--------|-------------|
| `hub_user_id` | Integer ID (retained as a number, no FK — user row is anonymized not deleted) |
| `requested_at` | Timestamp of request |
| `completed_at` | Timestamp of completion |
| `status` | `pending`, `completed`, or `failed` |
| `deletion_scope` | JSONB: row counts deleted per table (no field values), including `chat_messages` |
| `error_detail` | Failure category if status = `failed` — exception type / SQLSTATE only, never the driver message (which can echo row values) |

Superadmins can view `{ requested_at, completed_at, status }` at `GET /api/v1/superadmin/users/:userId/deletion-request` for support inquiries. No PII is returned.

---

## Analytics Data Access

### Who Can See What

All routes live under `/api/v1/analytics` (`blueprints/v1/analytics.py`) and go through the tenant middleware first; the OIDC scope, not a role name, decides access.

| Scenario | Route | Required | What is returned |
|----------|-------|----------|-----------------|
| Any user — own stats | `GET /me/stats` | Authenticated (self-service, no scope) | Their own data only |
| Any user — own reputation | `GET /me/reputation` | Authenticated (self-service) | Their own reputation |
| Community admin | `GET /community/:cid/members/:uid/stats` | `community.analytics:admin`, the community must be in the caller's own tenant, and `:uid` must be a member of it (else `404`) | One member's community activity |
| Analytics consumer | `GET /platform/*` | `analytics:read` | **Aggregates only** — no user rows. `community-health` additionally requires a Professional plan (flag `waddles.analytics.community_health`; `402` otherwise) |
| Superadmin | `GET /admin/users/:uid/stats` and `/reputation` | `users:admin` | Any user's data |

`is_analytics_consumer` is a superadmin-granted flag on the user; at token issuance it adds the `analytics:read` scope (the maintainer and admin scope bundles carry it too). It provides access **only** to aggregate platform statistics — never to individual user data. `analytics-core` itself trusts hub-api and performs no tenant or membership check of its own — hub-api is the authorization boundary — so the checks above are enforced in the hub-api route handlers, not "at the analytics_core service level" as an earlier revision of this document claimed.

### Data Minimization in Analytics Services

`PlatformStatsService` is designed with data minimization as a hard constraint:

- `get_platform_summary()` — returns total counts only, no user lists
- `get_reputation_distribution()` — returns histogram buckets (count per score range), no individual scores
- `get_growth_trends()` — returns new user/community counts per time bucket, no user IDs
- `get_activity_breakdown()` — returns segment counts (active 24h/7d/30d/90d/inactive), no user IDs
- `get_community_health_summaries()` — returns per-community aggregates (health score, bot grade), no member-level data

---

## Rate Limiting Data

Per-user rate limit counters are stored in Redis (primary) with a PostgreSQL fallback. They auto-expire:

- **Redis keys**: 2-hour TTL, set atomically on every `INCR`
- **DB fallback** (`ai_rate_limit_state`, `ai_chatter_rate_limit_state`): `expires_at` column, filtered on every read

Rate limit keys are composite identifiers (community ID + user ID + limit type + time bucket) and a count integer. They cannot identify a person in isolation and are not considered PII under GDPR Recital 26 (the key requires the hub DB to resolve the user ID to a real person).

---

## Service-to-Service Data Minimization

Internal service calls (hub → analytics-core, hub → ai-interaction) carry only what each service needs:

- **analytics-core**: receives `X-Caller-User-ID` (integer), `X-Caller-Role` (string enum), `X-Service-Key` (API key). No name, email, or platform handle is forwarded.
- **ai-interaction** (AIChatter/research): receives `community_id`, `user_id` (integer), `platform_user_id` (opaque platform string), `message` text. No email forwarded.
- **router-module**: receives platform events with platform user IDs. Resolves to `hub_user_id` for activity recording, then discards the mapping from further forwarding.

---

## Data Access and Portability (GDPR Articles 15 and 20)

`GET /api/v1/user/me/data` returns the authenticated user's own data as a JSON attachment (`waddles-data-{id}.json`). The subject is always the JWT's `sub` claim — there is no request parameter that can point it at another account. It is available in **every** tier.

Sources: `account`, `profile`, `linked_identities`, `sessions`, `passkeys`, `message_activity`, `watch_activity`, `chat_messages`, `cookie_consent`, `deletion_requests`. Every source lists its columns explicitly (never `SELECT *`), so the export never contains a password hash, a session token, a passkey public key, or a verification/reset token. A source that fails is reported in the response's `incomplete` list rather than silently omitted — a partial export the subject can see beats a `500`.

---

## Third-Party Services and Analytics

Waddles has **no browser-side analytics**: no analytics SDK, tag manager, session-recording or advertising pixel is bundled in the web app or the marketing site, and nothing sets a cookie for one. (Mixpanel, Google Analytics and Hotjar were listed in the cookie policy in error and are not used.) Usage analytics are computed in-house by `analytics-core` and exposed only as described under *Analytics Data Access*.

The services the platform itself calls with its own credentials:

| Service | Used for | What it receives |
|---------|----------|------------------|
| **PostHog** (self-hosted; default host `https://license.penguintech.io`, in-cluster for alpha) | Feature-flag evaluation, **server-side only** (`flask_core.entitlement`; the web app reads resolved flags from `GET /api/v1/flags`, never from PostHog) | The flag key, a `distinct_id` that is the tenant slug (or `tenant:community-id`) and a `tenant` group. No user id, e-mail, username, IP address or message content. No PostHog SDK or key is in the browser. |
| **License server** (`license.penguintech.io`) | License-tier validation through `penguin-licensing`, as product `waddles` | The license key (as a bearer token) and the product id. No end-user data. |
| **OAuth / OIDC providers** (Discord, Twitch, …) | Sign-in and account linking | Standard OAuth traffic (client credentials, authorization code). Waddles receives the user's platform id, username, avatar and, where the provider gives one, e-mail. |
| **OpenTelemetry collector** (deployer-chosen via the standard `OTEL_EXPORTER_OTLP_*` variables) | Logs, metrics and traces | Operational telemetry. The destination is never hard-coded; exception text is kept out of log lines by the `exc_log_audit` CI guard because driver errors can echo row values. |

---

*Last updated: 2026-10-10 — cookies, consent, erasure, data access, analytics access and third-party sections reconciled against `hub_api`; the pseudonymous-identifier, rate-limit and service-to-service sections are unchanged from the earlier revision and have not been re-audited.*
*See also: [docs/SECURITY.md](SECURITY.md)*
