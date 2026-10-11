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

Users can request deletion of their personal data from the **Your privacy rights** page (`/privacy-rights` → *Delete my data*), which calls `DELETE /api/v1/user/me/data`. An account with a password must re-confirm it; the page tells the user, before they confirm, that **everything is deleted** — profile, memberships, linked platform accounts and reputation — with one exception: a user under an *active ban* leaves a one-way coded suppression record (see *What Is Retained* §1).

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
| `community_members` | **Hard delete** — every membership row of the account, in every community, **including its reputation** (hub-linked rows and platform-keyed rows matching a linked identity) |
| `hub_user_identities` | **Hard delete** — the Discord/Twitch/… account links (there is no "identity anchor" any more; see *Returning After Deletion*) |
| `reputation_events`, `reputation_tenant` | **Hard delete** — the reputation score and its audit trail |
| `bundle_reputation_scores`, `bundle_reputation_adjustments` | **Hard delete** — the app-bundle reputation balance and ledger, keyed on the account's pseudonymous `user_uuid`s |
| `ephemeral_pseudonyms` | **Hard delete** — the platform-id → pseudonym mapping (and its raw handle) for the account's linked platform ids, in every tenant |
| `suppression_records` | **Written only if the account had an active ban** — one keyed one-way code per (tenant, platform account); see *What Is Retained* §1 |
| `hub_users` | **Anonymized in-place** (see below) |
| `data_deletion_requests` | Completion row inserted (see *Deletion Audit Trail*) |

**Atomicity.** Every delete, the suppression record (when the account has an active ban), the `hub_users` anonymization and the `completed` ledger row are issued in one executor job and committed once. If any statement fails, everything is rolled back — the account is left exactly as it was, never half-erased — a separate `failed` ledger row records the attempt, and the original error is returned (a `500`; the request can simply be retried). Erasure never ends in a state where the data is gone but the proof of erasure is missing, or vice versa. A banned user's erasure also needs the platform key to compute the suppression code: if it is unavailable the whole erasure fails and rolls back (a `failed` ledger row, a `500`, retry once fixed) rather than erasing without suppressing — a user with no active ban never touches the key.

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

Two categories of data are deliberately **not** deleted, each with a distinct legal basis under GDPR Article 6 or accountability duty under Article 5(2).

> **Status — counsel to verify.** §1 below is a *draft* disclosure for the ban-evasion suppression record. The legitimate-interest assessment, DPIA and the appeal / human-review process are drafts or open items: see [Suppression LIA](compliance/suppression-LIA.md) and [Suppression DPIA](compliance/suppression-DPIA.md). Nothing here is a final legal artifact.

#### 0. Consent and audit logs — `cookie_consent`, `cookie_audit_log`, `audit_log`, `data_deletion_requests`
*Legal basis: Legal obligation / accountability (Article 6(1)(c), Article 5(2))*

Consent records, the consent change log, the platform audit trail and the deletion-request ledger are **never** deleted by erasure. They are the controller's proof that consent was lawfully obtained and that the erasure request was honoured; deleting them would destroy the evidence the regulator can demand. They hold identifiers and technical metadata (user id, consent choices, IP, user agent) but not the profile, credentials, or message content that erasure removes. This is pinned in code (`ERASURE_RETAINED_TABLES`) and by regression tests (`hub_api/tests/test_data_privacy_erasure.py::TestRetention`).

#### 1. `suppression_records` — Ban-Evasion Suppression (only for a user with an active ban)
*Legal basis: Legitimate interest (Article 6(1)(f)) — preventing evasion of a community ban. This is **not** an Article 17(3) exemption: it is a separate, narrowly-scoped retention with its own lawful basis, necessity and balancing test (counsel to verify).*

**When anything is kept.** Only if, at the moment of erasure, the account had an **active ban** — a `community_members` row whose `role` is `banned` (set by the reputation policy enforcer's auto-ban, or a moderator). For every other user **nothing at all is kept**: no reputation, no score, no tombstone, no platform-identity link. A user with no active ban who returns gets a fresh start.

**What is kept.** One `suppression_records` row per (tenant, platform account):

| Column | Content |
|---|---|
| `tenant_id` | The tenant (the controller) that banned them — a code only ever matches inside this tenant |
| `platform` | The platform *name* (`discord`, `twitch`) — never an id |
| `key_ver` | Which master key version produced the code (key rotation) |
| `hmac` | **A keyed one-way code** of the platform id: `HMAC-SHA256(HKDF(K_master, tenant_id), platform ‖ platform_user_id)` |
| `reason_enum` | A **closed** list of restriction types — `ban` or `auto_ban`. No free text, no offence detail (Art. 9/10) |
| `expires_at` | The restriction's expiry, or empty = until a moderator lifts it |
| `created_at` | When retention began |

**What is not kept:** the platform id, username, e-mail, display name, hub user id, community, the offence, the moderator, any reason text.

**Why a keyed code and not a salted hash.** A Discord/Twitch id is a short numeric string; an unkeyed or statically-salted hash of one is a lookup table an attacker enumerates in seconds. The code is an HMAC under a secret per-tenant key derived from the platform tenant KEK, which is mounted into `hub-api` only and never stored in the database: a database dump alone can neither reverse nor even test a guess against it (pinned by `hub_api/tests/test_suppression_keys.py::TestNotReversibleWithoutTheKey` and an enumeration test). It is deterministic, so a returning user's id recomputes to the same code and matches. The platform name is bound into the MAC input so equal ids on two platforms are not linkable either.

**It is still personal data.** A pseudonymised code is personal data (Recital 26): it is covered by the access, objection and erasure rights, it is deleted at expiry (an hourly purge deletes expired rows; lookups ignore an expired row regardless) or when a moderator lifts the restriction, and an optional operator ceiling (`suppression.maxRetentionDays`) bounds how long even an indefinite ban is kept. The Art. 15 export of the deletion ledger discloses that a suppression record was written (`suppression_records_written`).

**Per tenant only.** Each tenant is the controller of its own bans; Waddles is the processor. The same platform id has a **different** code in every tenant and a code is never matched outside the tenant that wrote it — a platform-wide ban list would make Waddles a controller (pinned by `test_ban_suppression.py::TestReturnRecatch::test_the_same_platform_id_in_another_tenant_never_matches`).

**How a return is re-caught.** When a user is about to be admitted to a community — a join request is approved, an account is created through OAuth, or a platform account is linked — the code for the incoming platform id is recomputed with the *destination tenant's* key; a non-expired match re-applies the restriction (the membership is written `banned` + inactive and the request is refused). If the tenant has no live suppression row the key is never read; if it has one and the key is unavailable the check fails closed.

**Your rights.** You may object (Art. 21) or ask for human review of the restriction (Art. 22 for automated `auto_ban` decisions) by contacting the community or tenant administrator, who can lift it — which deletes the row at once. **Process follow-ups, not yet built:** a DSA Art. 17 statement of reasons and an internal appeal for platform-level restrictions, and a documented human-review path for automated bans.

#### No longer retained (changed from earlier versions of this document)

Earlier versions kept three things for every erased user — the platform account links (`hub_user_identities`, as a "permanent identity anchor" for account reclaim), the reputation score and its audit trail (`reputation_tenant` + `reputation_events`, as an anti-gaming signal), and the membership rows with only their personal columns nulled (`community_members`). **All three are now deleted** for every user. The anti-gaming purpose is served instead — and only for users with an active ban — by the suppression record in §1, which is far narrower than retaining every user's score and identity. The visible consequence: a user who deletes their data and returns starts with a fresh account and fresh reputation.

### Pseudonym Mapping — Erased With The Account

Unlinked platform accounts are represented by a pseudonym UUID; the raw platform handle behind it is stored in `ephemeral_pseudonyms.handle`. For every platform account **linked to the erased hub user**, the mapping and its handle are now deleted in every tenant, together with any bundle reputation keyed on that pseudonym. For a platform account that was never linked to a hub user there is no `hub_users` row to erase; wiping its handle remains an operator/DSAR operation (`identity_resolution_service.erase_pseudonym_handles`, deliberately not exposed on the gRPC surface), recorded in `identity_resolution_events` without PII.

### Returning After Deletion

If a user deletes their data and later returns:

1. They log in via a platform account (Discord, Twitch, etc.). No `hub_user_identities` link exists any more, so `_find_or_create_user_from_oauth()` creates a **new** hub account — new `hub_users.id`, new `uuid`, a fresh reputation. (A login by e-mail never adopts an existing account: an e-mail match with no linked identity is refused with a conflict, so the anonymized row cannot be claimed.)
2. The anonymized `hub_users` row of the old account is never re-populated or re-activated.
3. If the platform account had an active ban in a tenant, its suppression record matches when the new account is linked or admitted to a community of that tenant, and the restriction is re-applied there (and only there). A user with no active ban is not matched by anything.

### Deletion Audit Trail

> Enterprise tenants additionally get erasure and DSAR-export requests recorded in the tamper-evident audit chain (actor is a `hub_users.uuid`, never a name or e-mail) -- see [Enterprise Audit Logging](compliance/audit-logging.md). The rights themselves are available in every tier.

Every deletion attempt is recorded in `data_deletion_requests`. This table stores no PII values — only metadata:

| Column | Description |
|--------|-------------|
| `hub_user_id` | Integer ID (retained as a number, no FK — user row is anonymized not deleted) |
| `requested_at` | Timestamp of request |
| `completed_at` | Timestamp of completion |
| `status` | `pending`, `completed`, or `failed` |
| `deletion_scope` | JSONB: row counts deleted per table (no field values) — `chat_messages`, `community_members`, `hub_user_identities`, the reputation tables, `ephemeral_pseudonyms` — plus `suppression_records_written` (rows *written*, not deleted; `0` for a user with no active ban) |
| `error_detail` | Failure category if status = `failed` — exception type / SQLSTATE only, never the driver message (which can echo row values) |

Superadmins can view `{ requested_at, completed_at, status }` at `GET /api/v1/superadmin/users/:userId/deletion-request` for support inquiries. No PII is returned.

### Admin / Bulk DSAR Console (Enterprise)

Self-service data-subject rights (`GET`/`DELETE /api/v1/user/me/data`, above) are available in **every** tier — statutory rights are never tier-gated, and that path is unchanged. On top of it, a **tenant admin** on an Enterprise plan can run the same operations for a *caller-supplied* user, singly or in bulk. Feature contract `compliance.bulk_dsar` (Enterprise, flag `waddles.compliance.bulk_dsar`); the gate is the two-gate entitlement check (PostHog flag **and** license tier) and **fails closed** — either gate off or unreachable is a `402`.

| Method | Path (under `/api/v1/tenant/<slug>/privacy`) | Action |
|--------|----------------------------------------------|--------|
| `GET` | `/users/<id>/export` | Access export (GDPR Art. 15/20) |
| `POST` | `/users/<id>/erase` `{"confirm": true}` | Erasure — same anonymize-in-place core as self-service |
| `PUT` | `/users/<id>/do-not-sell` | CCPA/CPRA Do-Not-Sell opt-out |
| `POST` | `/bulk` `{"action", "userIds", "confirm"}` | Any of the three over many users |

Bulk requests return `200` with a per-user `status` (`completed`, `already_done`, `not_found`, `conflict`, `failed`, `audit_unavailable`) — one user's failure never aborts the rest. Erasure deletes the target's memberships, which are what proves a user belongs to the admin's tenant, so a *repeat* erase of an already-erased user returns `not_found` (never a second erasure and never an existence oracle). Caps per request: 100 users (25 for `export`, which returns data inline).

**Guardrails** (all enforced in `hub_api/services/admin_data_privacy_service.py`):

1. **Authz order** — tenant (from the JWT, never a request field) → `tenant:admin` scope → URL slug must equal the JWT tenant → Enterprise gate.
2. **Tenant fence** — the target must belong to the admin's own tenant (an active `tenant_admins` row, or membership of a community owned by that tenant). Anything else is `404`, never `403`, so the console is not a cross-tenant user-existence oracle.
3. **Tenant-scoped export** — message/watch activity and chat rows are limited to the admin's tenant's communities; another tenant's rows are never disclosed.
4. **Erasure rails** — `hub_users` is one global identity row, so an erase is refused (`conflict`) if the target is also a tenant admin or non-global community member of *another* tenant, is a platform super-admin, or is the acting admin (use self-service). Global-community membership is tenant-neutral and does not block. Erasure additionally requires `confirm: true`.
5. **Mandatory audit, fail-closed** — every attempt writes an `audit_log` row **before** any data is touched; if that write fails the action is not performed (`503`, `audit_unavailable`). Rows are PII-free.

`audit_log` row shape: `user_id` = acting admin; `action` = `dsar.export` / `dsar.erase` / `dsar.do_not_sell`; `target_type` = `user`; `target_id` = target `hub_users.id`; `details` = `{tenant_id, tenant_slug, bulk_id, outcome, ...}` where `outcome` is `attempted` → `completed` / `already_done` / `failed`, or `denied_not_in_tenant` / `denied_self` / `denied_super_admin` / `denied_shared_identity` for refused attempts (also audited). Exports add per-source `row_counts`; failures add `error_type` only.

**Do-Not-Sell** is stored where the opt-out already lives, `cookie_consent.preferences.doNotSell`, and is **one-way** (opt-out only, forces `marketing` off; mirrored into the subject-visible `cookie_audit_log` as `ADMIN_DO_NOT_SELL`). Withdrawing an opt-out is the subject's own consent decision. A user with no consent record gets a new privacy-maximal one (`consent_method = admin_dsar`). The opt-out is durable: later self-service consent writes merge key-by-key and never drop `doNotSell`.

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
