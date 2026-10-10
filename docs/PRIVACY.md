# Privacy & Data Handling

WaddleBot's approach to user data, privacy by design, and GDPR compliance — including the reasoning behind each architectural decision.

---

## Privacy by Design Principles

WaddleBot applies privacy as a structural property, not an afterthought:

- **Pseudonymization by default** — internal systems route by opaque IDs, never by email or username
- **Data minimization** — only the fields needed for a given operation are queried and returned
- **Aggregate-only analytics** — analytics consumers see histograms and counts, never individual rows
- **Fail-closed auth** — analytics and deletion endpoints require explicit role grants, not opt-out
- **Audit trails without PII** — deletion records store counts and timestamps, not field values

---

## Pseudonymous Identifiers

Across most of WaddleBot's internal services, users are referenced by **UUID or integer ID**, not by email or username. This is a deliberate pseudonymization strategy:

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

WaddleBot uses cookies for:

| Cookie | Purpose | Lifetime |
|--------|---------|---------|
| `jwt` / auth token | Session authentication | Session / configurable expiry |
| `XSRF-TOKEN` | CSRF protection | Session |
| Consent cookie | Records the user's cookie preferences | 12 months |

We do not use third-party tracking cookies, advertising cookies, or analytics cookies that send data to external services.

### Cookie Consent Flow

Users are presented with a cookie consent banner on first visit. The choice is recorded in two places:

- `cookie_consent` table: stores `user_id`, consent type, and timestamp
- `cookie_audit_log` table: immutable audit record of every consent event (granted, revoked, updated)

Both tables are **hard deleted** as part of the GDPR data deletion flow (see below) — a user's consent history is itself PII.

### "Do Not Track" / Preferences

Users can update or withdraw cookie consent at any time from Account Settings. Withdrawing consent revokes non-essential cookies and deletes the consent record from `cookie_consent` (the audit log entry noting the withdrawal is retained for compliance).

---

## Data Deletion (GDPR Article 17 — Right to Erasure)

Users can request deletion of their personal data via **Account Settings → Data & Privacy → Delete My Data**.

### What Gets Deleted

The deletion runs as a single database transaction:

| Table | Action |
|-------|--------|
| `hub_user_profiles` | Hard delete |
| `hub_sessions` | Hard delete |
| `hub_temp_passwords` | Hard delete |
| `user_passkeys` | Hard delete |
| `activity_message_events` | Hard delete |
| `activity_watch_sessions` | Hard delete |
| `cookie_consent` | Hard delete |
| `cookie_audit_log` | Hard delete |
| `hub_users` | **Anonymized in-place** (see below) |

### Anonymize In-Place: Why Not Hard Delete `hub_users`?

The `hub_users` row is anonymized rather than deleted:

```sql
UPDATE hub_users SET
  email        = 'deleted_{id}@deleted.waddlebot',
  username     = 'deleted_{id}',
  display_name = NULL,
  password_hash = NULL,
  avatar_url   = NULL,
  is_active    = FALSE
WHERE id = {userId}
```

**Reason:** `hub_users.id` is a foreign key referenced by dozens of tables across the schema (community memberships, reputation events, activity records, etc.). A hard delete would require cascading deletes across the entire database or leave orphaned rows — neither is safe at scale.

The retained row contains no PII: the email is a non-identifiable placeholder, and all identifying fields are nulled. This satisfies Article 17.

### What Is Retained (and Why)

Three categories of data are deliberately **not** deleted, each with a distinct legal basis under GDPR Article 6:

#### 1. `hub_user_identities` — Platform Account Links
*Legal basis: User's own legitimate interest (Article 6(1)(f)) — account reclaim*

The rows linking a user's Discord ID, Twitch ID, etc. to their `hub_user_id` are kept. This serves two purposes:

- **Account reclaim**: If a user returns and logs in via their platform account, the OAuth flow (`findOrCreateUserFromOAuth`) finds the existing identity link and reconnects them to their original account — including their full reputation history. See *Account Reclaim After Deletion* below.
- **Platform identity is not WaddleBot PII**: Discord/Twitch user IDs are owned and managed by those platforms. Deleting them from our records does not erase them from the source platform.

#### 2. `reputation_tenant` + `reputation_events` — Score & Audit Trail
*Legal basis: Legitimate interest (Article 6(1)(f)) — platform integrity / anti-gaming*

Reputation scores and their audit trail are retained. **This is disclosed to users before they confirm deletion.**

Allowing reputation reset via deletion would be a trivially exploitable loophole (farm score → delete → repeat). Reputation is also a community-wide signal that affects other users' experiences and community health calculations. The `reputation_events` trail justifies the retained score — without it, the score would be an unexplained number with no audit basis.

#### 3. `community_members` — Membership Row (PII fields nulled)
*Legal basis: Same as above — FK integrity + reputation retention*

The row itself is kept for FK integrity. Display fields are nulled: `display_name`, `avatar_url`, `bio`, `social_links`. The retained columns are: `community_id`, `hub_user_id`, `reputation`, `role`, timestamps.

### Account Reclaim After Deletion

If a user deletes their data and later returns:

1. They log in via a linked platform account (Discord, Twitch, etc.)
2. `findOrCreateUserFromOAuth()` queries `hub_user_identities` and finds the existing record
3. The record resolves to the original `hub_user_id`
4. Their reputation score and community membership reputation are fully intact
5. The `hub_users` row is updated with fresh profile data from the platform; `is_active` is set back to `true`

The platform identity (Discord snowflake, Twitch ID) acts as the **permanent identity anchor** across the entire account lifecycle — including through deletion and return.

### Deletion Audit Trail

> Enterprise tenants additionally get erasure and DSAR-export requests recorded in the tamper-evident audit chain (actor is a `hub_users.uuid`, never a name or e-mail) -- see [Enterprise Audit Logging](compliance/audit-logging.md). The rights themselves are available in every tier.

Every deletion attempt is recorded in `data_deletion_requests`. This table stores no PII values — only metadata:

| Column | Description |
|--------|-------------|
| `hub_user_id` | Integer ID (retained as a number, no FK — user row is anonymized not deleted) |
| `requested_at` | Timestamp of request |
| `completed_at` | Timestamp of completion |
| `status` | `pending`, `completed`, or `failed` |
| `deletion_scope` | JSONB: row counts deleted per table (no field values) |
| `error_detail` | Failure reason if status = `failed` |

Superadmins can view `{ requested_at, completed_at, status }` at `GET /api/v1/superadmin/users/:userId/deletion-request` for support inquiries. No PII is returned.

---

## Analytics Data Access

### Who Can See What

| Scenario | Route | What is returned |
|----------|-------|-----------------|
| Any user — own stats | `GET /analytics/me/stats` | Their own data only |
| Any user — own reputation | `GET /analytics/me/reputation` | Their own reputation |
| Community admin | `GET /analytics/community/:cid/members/:uid/stats` | One member's community activity |
| Analytics consumer | `GET /analytics/platform/*` | **Aggregates only** — no user rows |
| Superadmin | `GET /analytics/admin/users/:uid/stats` | Any user's data |

The `is_analytics_consumer` role is superadmin-granted and provides access **only** to aggregate platform statistics — never to individual user data. This is enforced at the `analytics_core` service level, not just at the route level.

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

*Last updated: 2026-02-26*
*See also: [docs/SECURITY.md](SECURITY.md)*
