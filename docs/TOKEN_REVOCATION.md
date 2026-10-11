# Session Revocation and Short-Lived Access Tokens

How a login session ends *before* its token expires, and why access tokens now live 15 minutes.
This closes the enterprise / SOC 2 session-management finding (OWASP Session Management; SOC 2
CC6.1 / CC6.3): a compromised, logged-out, admin-revoked or credential-changed token used to stay
valid for 24 hours because nothing server-side could say "no". It builds on the verifier hardening
in [JWT Verification](JWT_VERIFICATION.md).

Code: `libs/flask_core/flask_core/token_revocation.py` (store, cache, outage policy),
`flask_core/auth.py` (mint + verify), `hub_api/services/session_service.py` (sessions, refresh
rotation, revocation triggers), `hub_api/blueprints/v1/auth.py` and `user_management.py` (routes).

## What changed, in one table

| Before | Now |
|---|---|
| Access token lives 24 h; nothing can revoke it | Access token lives **15 min** (`JWT_ACCESS_TTL_SECONDS`, 300-900) and is revocable before `exp` |
| "Refresh" needed a still-valid access token, so a short TTL would log everyone out | A separate, rotating, HttpOnly **refresh token** renews the session with no forced logout |
| `hub_sessions.session_token` stored the raw JWT | It stores a SHA-256 *digest* of the refresh token - a database leak yields no usable credential |
| Logout flipped a DB flag the verifier never read | Logout, admin revoke, password / authenticator change write the revocation store the verifier reads on **every** verification |
| Verifier accepted any lifetime | Verifier refuses any token lived longer than **1 h** (`lifetime_exceeded`) |

## Access tokens

Minted by `flask_core.auth.create_jwt_token`, HS256, `kid`-stamped (see JWT Verification). New claims:

| Claim | Meaning |
|---|---|
| `jti` | Random 128-bit hex id, unique per token. Revocation by token. |
| `sid` | The login session (`hub_sessions.id`) the token belongs to. Absent on tokens that belong to no login session (service tokens, the temp-password pending-link session). Revocation by session. |

* Default lifetime `ACCESS_TOKEN_TTL_SECONDS` = **900 s**. `JWT_ACCESS_TTL_SECONDS` may tune it
  inside **[300, 900]**; anything else stops the service at import.
* `create_jwt_token` refuses a lifetime above `MAX_JWT_LIFETIME_SECONDS` (3600): machine tokens
  (`svc-ingest`, `svc-action`, `svc-process`) keep asking for `expiration_hours=1`; nothing can
  mint a day-long token any more.
* `verify_jwt_token` enforces the same ceiling independently (`exp - iat <= 3600 + 30 s skew`), so
  a mis-configured or compromised minter cannot hand out long-lived bearer tokens either.
* `jti`/`sid`, when present, must match `[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}`; `sub` is at most 128
  characters. They are used as store keys and must never be attacker-shaped. `jti` is not yet
  *required* (service tokens minted before this change carry none); a later change can make it
  mandatory once those have aged out.

### Rollout: one forced sign-in

Tokens minted before this change have a 24 h lifetime and are therefore refused by the new
ceiling. **Every browser session signs in once more at deploy time** (alpha/beta: expected; the
SPA lands on the login page). Service tokens are minted fresh on every call and are unaffected.

## The revocation store

`flask_core.token_revocation`. Three kinds of record, all written with a TTL so the store forgets
a record the moment no token it could match is still alive (`REVOCATION_RECORD_TTL_SECONDS` =
3900 s):

| Kind | Key | Value | A token is rejected when |
|---|---|---|---|
| one token | `waddles:jwt:rev:jti:<jti>` | epoch seconds | its `jti` claim matches |
| one login session | `waddles:jwt:rev:sid:<sid>` | epoch seconds | its `sid` claim matches |
| one user ("revoke all") | `waddles:jwt:rev:sub:<sub>` | **cutoff** (epoch s) | `iat <= cutoff` |

A `jti` record's TTL is the token's own remaining life (+ the 30 s skew); `sid` / `sub` records live
the full 3900 s. `put_max` (an atomic Lua `SET ... EX` that never lowers a cutoff and never
shortens a TTL) is the only write. Short-lived "was just rotated" markers share the store
(`waddles:jwt:mark:<name>`, see Refresh tokens).

### What a verification costs

`verify_jwt_token` -> `TokenRevocation.check(claims)` runs **last**, after every signature / claim /
lifetime check, so only an otherwise-valid token costs a lookup:

```
local cache (per key, default 5 s) -- hit --------------------------------> verdict
        | miss
        `-> ONE Valkey MGET(jti, sid, sub) --> cache --> verdict
              `-- store unreachable --> circuit opens for 5 s
                    |-- within the grace window of the last good contact: serve the last-known
                    |   view, outcome `degraded_allowed`
                    `-- beyond it: FAIL CLOSED (`unavailable_rejected` -> token rejected,
                        reason `revocation_unavailable`)
```

A cached *revoked* `jti`/`sid` answers immediately without asking about the other keys (a client
retrying a dead token costs nothing). Revoked `jti`/`sid` records stay cached for the token's
whole life; "not revoked" answers and user cutoffs are re-read after the cache TTL.

### Tradeoffs (decided, not accidental)

| Concern | Decision |
|---|---|
| **Propagation delay** | A revocation reaches *other* replicas within `JWT_REVOCATION_CACHE_TTL_SECONDS` (default **5 s**, max 30). The replica that processed the revocation rejects the token immediately (its cache is written through). |
| **Store outage** | For at most `JWT_REVOCATION_GRACE_SECONDS` (default **60 s**) after the store was last reachable, serving continues on the last-known view (`degraded_allowed`; a revoked token already known stays rejected). A revocation issued *during* the outage cannot propagate. **Past the grace window the default is to fail closed** (reject every token) rather than silently run without revocation. `JWT_REVOCATION_FAIL_MODE=open` is an explicit operator opt-in (logs CRITICAL, counts `unavailable_allowed`). |
| **Blocking I/O** | `verify_jwt_token` is synchronous (it is called from async handlers *and* sync gRPC threads). The cache keeps the store off the hot path; a miss is one sub-millisecond `MGET`; a dead store costs at most `JWT_REVOCATION_TIMEOUT_SECONDS` (0.25 s) **once per circuit window (5 s)**, never once per request. Writes (`arevoke_*`) run in a thread. |
| **Eviction** | Records are volatile (TTL'd), but the chart's default `maxmemory-policy allkeys-lru` could evict one under memory pressure. Exposure is bounded by the access-token TTL (<= 15 min). Prefer `volatile-*` on the instance that carries revocation if no other consumer needs `allkeys-*`. |
| **No store configured** | `VALKEY_URL` / `REDIS_URL` unset -> a **process-local** store with a loud WARNING (single replica / dev, like the rate limiter's in-memory mode). There is no way to switch revocation off. |
| **Misconfiguration** | An invalid `JWT_REVOCATION_*` value raises on first use (hub-api builds it at startup, so it stops the boot) - never a silent default. |
| **Clock skew** | Cutoffs compare the revoking replica's clock with the minting replica's `iat`; the platform already assumes NTP-grade sync (30 s `iat` skew). |
| **`iat` resolution** | One second. A per-user cutoff rejects `iat <= cutoff` (the safe side: a token minted in the revoke second is rejected). A **minter** that must survive a same-second revoke (user logins) asks `TokenRevocation.issue_floor(sub)` and stamps `iat = cutoff + 1` - at most one second ahead, inside the verifier's skew - so *password change, then immediate login* works. |

Valkey connection: `VALKEY_URL` (else `REDIS_URL`), with `VALKEY_PASSWORD` injected when the URL
carries no credentials (hub-api's credential-free `VALKEY_URL_TLS`), TLS CA from `VALKEY_CA_FILE`
- the same conventions as the rate limiter and `valkey_admin_client`.

## Login sessions and refresh tokens

One `hub_sessions` row per login (no schema change):

| Column | Now holds |
|---|---|
| `session_token` | `rt1:` + SHA-256 of the **current refresh nonce** (never a bearer value) |
| `expires_at` | Sliding idle deadline (`REFRESH_TOKEN_TTL_SECONDS`, default 7 d), each rotation slides it forward |
| `created_at` | Anchors the absolute cap (`SESSION_ABSOLUTE_TTL_SECONDS`, default 30 d) - no amount of activity extends past it |
| `is_active`, `revoked_at` | Revocation state |

**Refresh token** = `rt1.<sid>.<nonce>.<tenant>.<tag>`: the session id, a 256-bit random nonce, the
tenant the session was opened for (base64url) and an HMAC-SHA256 tag over all of it, keyed by a
key *derived* from the platform secret (domain-separated, so a tag can never be mistaken for a JWT
signature). It travels only in the `wb_refresh` cookie: **HttpOnly, Secure, SameSite=Lax, Path
`/api/v1/auth`** - the browser sends it to login/refresh/logout and nowhere else. The access
cookie `wb_session` now has `Max-Age` = the access TTL, so the browser drops it when the JWT dies.

* **Rotation on every use.** `POST /api/v1/auth/refresh` authenticates the token (tag), loads the
  session, re-reads the user and tenant (a demotion or deactivation lands within one refresh;
  tenant-admin scopes are kept - the old refresh dropped the tenant and silently downgraded them),
  rotates the nonce with a compare-and-swap (exactly one concurrent caller wins), and mints a new
  access token under the same `sid`. Renewals are not individually audited; issuance, revocations
  and reuse detection are.
* **Reuse detection.** The HMAC tag distinguishes a *genuine, previously issued* token from a
  guess without storing history. A genuine token that is no longer current, presented outside the
  grace window, means the credential was copied: the **whole session is revoked** (database +
  `sid` in the store) and an `authn.session_revoked` audit event (`reason=refresh_reuse`, system
  actor) is written. Forging a token for someone else's session id fails the tag and revokes
  nothing.
* **Parallel-refresh grace** (`REFRESH_REUSE_GRACE_SECONDS`, default **10 s**). One SPA page whose
  five requests all 401 at the same expiry sends the same cookie five times. The loser(s) find the
  "just rotated" marker in the store and receive a fresh *access* token only - no second rotation,
  no revocation (the browser already holds the winner's new refresh cookie). If the marker store is
  unreachable the stale token is refused with 401 **without** revoking the session: a racing page
  and a thief cannot be told apart, and guessing wrong must not sign the user out.
* `GET /api/v1/auth/me` renews silently from the refresh cookie when the access cookie is gone
  (SPA load after 15 minutes) - it answers 200/`user: null` rather than 401, so the SPA's 401
  interceptor would otherwise never fire.
* A pending-link (temp-password) session has no refresh token and no `hub_sessions` row; it lives
  its 15-minute access TTL.

## Revocation triggers

| Trigger | Database (stops refresh) | Store (kills live access tokens) | Audit event |
|---|---|---|---|
| `POST /auth/logout` | the session | `sid` + presented `jti` | `authn.session_revoked` (`logout`) |
| `POST /auth/logout-all` | every session of the user | per-user cutoff | `logout_all` |
| `POST /auth/password` | every session **including this one** | per-user cutoff | `password_changed` |
| passkey removed / linked identity unlinked | every *other* session (the caller's stays) | `sid` of each | - |
| refresh-token reuse | the session | `sid` | `refresh_reuse` (system actor) |
| admin `POST .../users/{id}/revoke-sessions` | every session of the user | per-user cutoff | via the audit hook |
| admin `POST .../users/{id}/sessions/{sid}/revoke` | that session | `sid` | via the audit hook |
| user deactivated / deleted, super-admin demoted, DSAR erasure | every session | per-user cutoff | existing events |

* A revoke that cannot reach the shared store still stops refresh (database) and rejects the token
  on the revoking replica; the access token merely runs out its remaining <= 15 minutes elsewhere.
  Admin endpoints say so: `accessTokensRevokedImmediately: false` means "retry".
* The revocation audit event is **best-effort**: the revocation has already taken effect, and
  failing a password change or logout because the audit store hiccuped would be worse. A failed
  audit write is still loud (`AuditService.record` logs ERROR and counts it).
* **Why password change revokes the current session too**: it is the simplest rule that cannot be
  bypassed (the caller signs in again with the new password). Authenticator changes (passkey,
  identity) keep the caller's own session and end the others by `sid`.

### Admin API (scope `users:admin`)

| Method + path | Result |
|---|---|
| `GET /api/v1/superadmin/users/{user_id}/sessions` | `{success, sessions:[{id, createdAt, expiresAt, isActive, revokedAt}]}` - identifiers and timestamps only, never a credential |
| `POST /api/v1/superadmin/users/{user_id}/revoke-sessions` | `{success, sessionsRevoked, accessTokensRevokedImmediately}` |
| `POST /api/v1/superadmin/users/{user_id}/sessions/{session_id}/revoke` | same shape; 404 if the session is not that user's |

Self-service: `POST /api/v1/auth/logout-all` ("sign out everywhere").

## Configuration

| Variable | Default | Range | Meaning |
|---|---|---|---|
| `JWT_ACCESS_TTL_SECONDS` | 900 | 300-900 | access-token lifetime |
| `REFRESH_TOKEN_TTL_SECONDS` | 604800 (7 d) | 900-7776000 | idle window, slides on every rotation |
| `SESSION_ABSOLUTE_TTL_SECONDS` | 2592000 (30 d) | 3600-31536000 | hard cap per login (idle must not exceed it) |
| `REFRESH_REUSE_GRACE_SECONDS` | 10 | 0-60 | parallel-refresh grace; 0 = every replay revokes |
| `JWT_REVOCATION_BACKEND` | `auto` | `auto`/`valkey`/`memory` | `auto` = Valkey when a URL is set, else process-local |
| `JWT_REVOCATION_CACHE_TTL_SECONDS` | 5 | 0-30 | cross-replica propagation bound |
| `JWT_REVOCATION_GRACE_SECONDS` | 60 | 0-600 | serve the last-known view this long after the store is lost |
| `JWT_REVOCATION_FAIL_MODE` | `closed` | `closed`/`open` | behaviour past the grace window |
| `JWT_REVOCATION_CIRCUIT_OPEN_SECONDS` | 5 | 0.5-60 | how long a failed lookup suppresses retries |
| `JWT_REVOCATION_TIMEOUT_SECONDS` | 0.25 | 0.05-2 | store socket timeout |
| `VALKEY_URL` / `REDIS_URL`, `VALKEY_PASSWORD`, `VALKEY_CA_FILE` | chart-provided | | the shared store |

Every out-of-range or malformed value is a startup error.

## Observability

OpenTelemetry API only; destination via the standard `OTEL_EXPORTER_OTLP_*` configuration.

| Instrument | Type | Labels |
|---|---|---|
| `waddles_jwt_revocation_checks_total` | counter | `outcome`: `ok`, `revoked_jti`, `revoked_sid`, `revoked_user`, `degraded_allowed`, `unavailable_allowed`, `unavailable_rejected` |
| `waddles_jwt_revocations_total` | counter | `kind`: `jti`/`sid`/`sub` |
| `waddles_jwt_revocation_write_errors_total` | counter | `kind` |
| `waddles_jwt_revocation_store_errors_total` | counter | `backend` |
| `waddles_jwt_revocation_lookup_seconds` | histogram | `backend` |
| `waddles_jwt_verifications_total{outcome="revoked"\|"revocation_unavailable"\|"lifetime_exceeded"}` | counter | (existing metric, new outcomes) |
| `waddles_hub_session_events_total` | counter | `event`: `issued`, `refreshed`, `refresh_grace_replay`, `refresh_reuse_revoked`, `refresh_rejected`, `revoked_session`, `revoked_user`, `revocation_store_failed` |

Spans: `jwt.revocation.lookup` (store round trip), `hub.session.refresh`. Alert on
`unavailable_rejected > 0` (revocation store lost past grace: logins failing closed),
`revocation_store_errors_total` rate, and any `refresh_reuse_revoked` (a copied refresh token).

Logs are PII-free by construction: only closed-vocabulary outcomes, exception *type* names, and
integer session / user ids. A token, a `jti`/`sid`, a nonce, a digest or the store's error text is
never rendered. A rejected token logs `JWT rejected: ... reason=revoked` at WARNING;
`revocation_unavailable` and `lifetime_exceeded` at ERROR.

## Operating it

* **Revoke a compromised user now**: `POST /api/v1/superadmin/users/{id}/revoke-sessions`; check
  `accessTokensRevokedImmediately`. If `false`, the store was down - retry once it is back.
* **Valkey lost > grace window**: authenticated requests start failing closed
  (`revocation_unavailable`, ERROR logs, `unavailable_rejected`). Restore Valkey; the circuit
  re-probes within 5 s and service resumes without a restart. Revocations written while it was down
  were only held by the replica that processed them.
* **Valkey flushed / failed over without persistence**: revocation records vanish; revoked access
  tokens come back to life for at most their remaining <= 15 minutes, and refresh stays dead (the
  database half is durable). Re-issue the revocation for anything that matters.
* **Rolling out**: deploy hub-api and every service that verifies platform tokens together with the
  access-token change; users sign in once (see Rollout above).

## Rust verifiers: paired follow-up (NOT built here)

The Rust `svc_*` verifiers (`service_auth`, `svc-streaming`, the egress assertion verifier) must
perform the **identical** check against the same Valkey keys, or a revoked token is still accepted
by the data plane. Contract:

1. After the existing Phase 0 checks, require `exp - iat <= 3600 + 30` (`lifetime_exceeded`).
2. Build the keys exactly as in the table above (`waddles:jwt:rev:{jti|sid|sub}:<value>`; `jti` /
   `sid` match `[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}`, `sub` <= 128 chars, absent claims are skipped).
3. One `MGET` of those keys; reject when the `jti` or `sid` key exists, or when the `sub` key exists
   and `iat <= int(value)` (an unparsable value counts as revoked).
4. Cache each answer briefly (5 s); keep revoked `jti`/`sid` answers for the token's life.
5. Outage policy and metric outcomes identical to the Python verifier (grace 60 s then fail closed;
   `waddles_jwt_revocation_checks_total{outcome}` with the same label values) so the dashboards line
   up.

Until that lands the data plane validates signature, claims and lifetime but not revocation; its
exposure is the 15-minute access-token TTL.

## Known limits

* The ~30 module-local `jwt.decode` call sites (listed in JWT Verification) do not go through
  `verify_jwt_token`, so they do not consult the revocation store; migrate them onto it.
* Service EdDSA tokens (`service_jwt`) and external IdP ID tokens are not session tokens and are
  not covered; the machine-token TTL ceiling (1 h) bounds them.
* Revocation is as durable as the store; see the Valkey-flush note above.
* `jti` is not yet mandatory on platform tokens (see Access tokens).
