# DSAR Request Inbox

Level 1 of hub-api's two-level data-subject-request (DSAR) system. **Statutory, every tier, never tier-gated.** Level 2 (Enterprise) is the existing bulk route (`waddles.compliance.bulk_dsar`) and, later, automation rules. See [PRIVACY.md](PRIVACY.md) for the rights themselves and the single-subject actions this inbox drives.

Code: `hub_api/blueprints/v1/dsar_requests.py`, `hub_api/services/dsar_service.py`, `captcha_service.py`, `dsar_crypto.py`, `dsar_notifier.py`, `dsar_outbox.py`, `dsar_rate_limit.py`, `dsar_telemetry.py`; migrations `alembic/versions/0121_dsar_requests.py`, `0123_dsar_mail_outbox.py`, `0124_dsar_open_public_unique.py`; contract `openapi/v1.yaml`.

## Flag

`waddles.dsar-inbox` (PostHog -> `FLAG_WADDLES_DSAR_INBOX` env -> default OFF; helm baseline `config.dsarInboxEnabled: true`). It gates **only the rollout of this new inbox** (`404 DSAR_INBOX_DISABLED` on admin/user routes; the public form silently drops, see Non-enumeration). The existing manual export / erase / do-not-sell routes and `/api/v1/user/me/data` never consult it.

## Enum codes (ints everywhere)

| Enum | Codes |
|------|-------|
| `requestType` | 1 export, 2 erase, 3 do_not_sell, 4 consent_withdrawal, 5 correction, 6 other |
| `status` | 1 received, 2 verification_pending, 3 verified, 4 in_progress, 5 completed, 6 rejected, 7 withdrawn, 8 expired (5-8 terminal) |
| `regime` | 1 gdpr, 2 ccpa (CCPA/CPRA), 3 other |
| `source` | 1 logged_in_user, 2 public_form, 3 admin_entered |
| event `eventType` | 1 created, 2 verification_sent, 3 verification_send_failed, 4 verified, 5 status_changed, 6 assigned, 7 notes_updated, 8 extended, 9 action_executed, 10 expired, 11 contact_purged, 12 verification_resent, 13 subject_linked |
| event `actorKind` | 1 system, 2 admin, 3 subject, 4 public_requester |

Only UUIDs cross the API boundary (request ids, assignee, linked user). Admins may set `status` to 4, 5, 6, 7 only; 2, 3 and 8 are system-driven.

## SLA (statutory clock)

| Regime | Deadline (`dueAt`) | One extension (`extendedUntil`) |
|--------|--------------------|---------------------------------|
| GDPR | clock start + 1 calendar month | `dueAt` + 2 further calendar months (Art. 12(3), 3 months in total) |
| CCPA/CPRA | clock start + 45 days | `dueAt` + 45 days (90 total) |
| other (default) | clock start + 30 days | `dueAt` + 30 days |

A calendar month is the **same day of the next month, clamped to that month's last day, in UTC** (time of day kept): Jan 31 -> Feb 28 (29 in a leap year), Dec 15 -> Jan 15 of the next year. The GDPR extension adds two calendar months to `dueAt` with the same clamping, so a request received Jan 31 is due Feb 28 and extends to Apr 28 (never later than the statutory three months; because each step clamps, the chained date can be up to 3 days EARLIER than start + 3 calendar months, e.g. Jan 31 -> Apr 28 vs Apr 30, which is the conservative direction and is asserted by a test over every day of 2023-2025). The extension audit event carries `extra_months: 2` (GDPR) or `extra_days` (CCPA/CPRA, other).

- **Clock start**: `requestedAt` for signed-in and admin-entered requests (admin may back-date with `receivedAt`, max 1 year, never future); **`verifiedAt` for public-form requests** (the clock cannot start until the requester proved control of the email; `requestedAt` is still recorded). Unverified requests have `dueAt = null`.
- **Extension**: `POST .../extend {reason}`, once per request (`409 DSAR_ALREADY_EXTENDED` after), reason required (max 1000 chars), only on verified/open requests. The extension is audited.
- **`dueState`** (server-computed against the effective due date = `extendedUntil ?? dueAt`): `overdue` when now is past it; `due_soon` when 7 days or fewer remain (boundary inclusive; exactly due is still `due_soon`); else `ok`. `null` for closed and unverified requests. The `dueState` list filter only matches open, verified requests.

## Public form trust model (captcha AND email verification)

1. `POST /api/v1/public/<tenant_slug>/privacy-requests` with `{email, requestType, regime, message?, captchaToken}` (no name field). Tenant comes **only** from the URL.
2. Captcha is verified **server-side** and **fails closed**: no/unknown/half-configured provider -> `503 DSAR_CAPTCHA_UNAVAILABLE`; provider timeout/error/non-200/bad JSON -> 503; wrong token -> `400 DSAR_CAPTCHA_FAILED`. Nothing is stored unless the captcha passed. A missing `DSAR_FINGERPRINT_KEY` is also `503 DSAR_UNAVAILABLE`.
3. The request is stored `verification_pending` (status 2), **never actionable**, no SLA clock, and a single-use token is emailed.
4. The confirm page first **previews** the token (`POST /api/v1/public/privacy-requests/verify/preview {token}`, or `GET .../verify?token=`): NON-consuming, returns only `{valid, requestType, regime, expiresAt}` (no PII, no tenant internals) so the person sees WHICH request they are confirming. Only `POST .../verify {token}` consumes it (hashed at rest, 60 min default lifetime, single use, race-safe) -> `verified` and the clock starts. GET never changes state, so mail scanners that prefetch the link cannot burn it. Every failure (unknown, expired, used, malformed) is the same `400 DSAR_VERIFICATION_INVALID`. Prefer the POST preview: a token in a query string can land in proxy/ingress access logs (the app's own telemetry redacts query strings; hypercorn/ingress logs are outside the app), a body cannot. Verify responses carry `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.
5. `POST /api/v1/public/<tenant_slug>/privacy-requests/resend-verification {email, requestType, captchaToken}` rotates the token of the pending request of THAT `requestType` only (the old token dies), so an attacker's pending ERASE for someone's address can never get that person a link that verifies it. `requestType` is required (`400` without it). Captcha required, uniform `202`, soft-limited (below).

**Atomic intake.** The request row (or token rotation) and its outbox mail row are written in ONE transaction (`ban_suppression.run_in_transaction` + `dsar_outbox.enqueue_in_transaction`); a crash can never leave a request with no mail or a mail with no request. A second submit for the same tenant+email+type is not a new row: the existing pending request's token is rotated and a fresh mail goes to the address owner (so an attacker who pre-submitted a victim's email cannot stop the victim's own submit from getting a working link; trade-off: each such submit invalidates the previous link, bounded by the soft limits below, and the mail only ever reaches the email owner). **Resubmitted content is a draft, never an overwrite:** a reuse does NOT touch the committed `message`/`regime`; the new submission is stored as a draft (`pending_message_enc`, encrypted under the row's `public_id`, NULL when the submit has no message, plus `pending_regime`; alembic 0128) tied to the token minted by that rotation (`pending_token_hash`, the hash of that exact token). `consume_verification_token` commits the draft into `message_enc`/`regime` ONLY when the redeemed token's hash equals `pending_token_hash` (older tokens are dead anyway); a draft with any other hash is discarded, never committed. The draft is then cleared and, if the content actually changed, a PII-free `content_replaced` event (event type 14, detail `{fields, message_present}`) is recorded. A third-party resubmit therefore cannot change what the row holds; only the mailbox owner clicking the link minted with the draft commits it. A bare resend CLEARS any draft (an earlier submitter's content never rides a link it did not create), and closing or expiring an unverified request purges the draft with the other contact data. Admins never see `message` while a request is `verification_pending` (`reveal_contact`); the verify preview returns EXACTLY what `POST /verify` will commit for that token, computed by ONE helper (`dsar_service.verify_outcome`) shared by preview and consume: `requestType`, `regime`, `message` (plain text or `null`, never logged; the verify page renders it as escaped text only), `contentChanged` (true when confirming changes the stored content, i.e. a draft bound to this very token) and `expiresAt`. With no draft, or a draft bound to a different token (it will be discarded), it shows the committed regime and message. The preview itself writes nothing. The message is requester-controlled and attacker-controllable by design (anyone can file a request for an address), so the verify page shows it only as escaped, unlinked plain text labelled "submitted by whoever filed it (not verified)", under a warning to confirm only if the reader made the request. The database enforces it: partial unique index `ux_dsar_requests_open_public` on `(tenant_id, contact_email_hash, request_type) WHERE status = 2` (alembic 0124); a lost race raises an integrity error that the route turns into the same rotate path.
6. **Unverified requests auto-expire** after `DSAR_UNVERIFIED_EXPIRY_HOURS` (default **72**) to status 8 `expired`; their contact data and token are purged.

While unverified: `act` and the in_progress/completed status changes return `409 DSAR_UNVERIFIED`; the admin list **hides** unverified/expired rows unless `?status=2` / `?status=8` is requested; those rows are flagged `unverified: true` and **never reveal `requesterEmail`/`requesterMessage`** (admins only see them once verified). An admin may still reject (6) or withdraw (7) an unverified request, which purges its contact data.

### Captcha providers (env, never hardcoded, never logged)

| `DSAR_CAPTCHA_PROVIDER` | Env | Notes |
|--------------------------|-----|-------|
| `turnstile` | `DSAR_CAPTCHA_SITE_KEY`, `DSAR_CAPTCHA_SECRET` | Cloudflare siteverify |
| `recaptcha` | `DSAR_CAPTCHA_SITE_KEY`, `DSAR_CAPTCHA_SECRET`, optional `DSAR_CAPTCHA_MIN_SCORE` (0.5) | Google siteverify; v3 score enforced |
| `altcha` (default when unset) | `DSAR_ALTCHA_HMAC_KEY` (>= 32 chars, chart-generated create-if-absent) | self-hosted proof-of-work, **no external call**; challenge from `GET /api/v1/public/privacy-requests/altcha-challenge`, single use |

`DSAR_CAPTCHA_TIMEOUT_SECONDS` (default 5) bounds the outbound call. Provider success is also **bound to this site**: reCAPTCHA v3 (`DSAR_RECAPTCHA_VERSION`, default `v3`) must return `action == DSAR_CAPTCHA_ACTION` (default `dsar_request`), a numeric `score >= DSAR_CAPTCHA_MIN_SCORE` and a `hostname` in `DSAR_CAPTCHA_HOSTNAMES` (comma list; default: host of `DSAR_VERIFY_URL_BASE`; none resolvable -> 503); Turnstile checks `hostname`/`action` when returned (the form sends `action: dsar_request`). Altcha challenges are `sha256(salt+number)` with `salt = <24 hex>?expires=<unix>&` (the trailing `&` is a fixed terminator so no digit of `number` can shift into the salt); the MAC covers salt **and** challenge, expiry is parsed only from that exact signed shape and bounded to now+10 min, and the single-use marker lives at least as long as the signed validity.

**Rate limits and replay marker fail closed.** `dsar_rate_limit` uses the shared Valkey client (atomic `INCR`+`EXPIRE NX` window; replay = `SET NX EX`). If a shared store is configured but errors/is disconnected, public routes return the uniform `503 DSAR_UNAVAILABLE` (no per-process fallback, no fail-open). A bounded in-process LRU (never cleared; a full table of live replay markers fails closed) is used only when the app has no shared limiter at all (tests/dev). `captcha-config` (120/h/IP) and `altcha-challenge` (30/h/IP) are per-IP limited to stop proof-of-work pre-mining. Client IP comes from the trusted-proxy-aware `_client_ip(trusted_proxy_hops)`; set `trusted_proxy_hops` to the real ingress depth or all clients share the proxy's bucket. `GET /api/v1/public/privacy-requests/captcha-config` returns only `{provider, siteKey, challengeUrl}`.

### Non-enumeration, abuse controls

- Every well-formed, captcha-passing submission/resend gets the **same `202 {success:true, accepted:true}`** whether or not the tenant slug exists, the flag is on, the email already has a pending request, a row was stored, or an abuse limit was exceeded. (Malformed input is `400` *before* tenant resolution, so it reveals nothing.) Residual: response timing differs slightly for unknown tenants.
- **Soft limits, never a 429 oracle** (shared Valkey limiter when connected, bounded in-process fallback otherwise -- never fail-open): 10 submissions/hour/IP, 3/hour per tenant+email fingerprint (counted even for unknown tenants), 3 resends/hour per tenant+email, and a GLOBAL cap of 10/day per email fingerprint across ALL tenants (stops cross-tenant mail-bombing). Evaluating all of them every time (no short-circuit), a breach silently suppresses the send: decoy DB work, same `202`, no mail. The old per-tenant hard counter was removed (anyone could exhaust a tenant's intake with it) and a 429 per email revealed recent activity. Remaining hard limits are caller-only resource backstops: 100 submit/resend per IP/hour and 30 verify/preview attempts/hour/IP (`429 DSAR_RATE_LIMITED`), 30 altcha challenges and 120 captcha-config reads per IP/hour. The signed-in intake keeps its own 10/day per user (`429`, caller's own data only).
- Body cap 8 KB (`413`), strict field validation (RFC-shaped email <= 254, message <= 2000 chars, control characters rejected, enum codes), unknown tenant slugs never read from the body.
- Raw emails never reach Valkey keys, logs, spans or metric labels (keyed HMAC fingerprint only).

## Contact data at rest (PII)

For non-registered requesters only the email (and optional message) is stored, sealed with the repo's existing field-encryption vault (`services.provider_connections.vault.seal_secret`, AES-256-GCM under `CREDENTIAL_ENCRYPTION_KEY`, the row `public_id` bound as AAD; no new crypto primitive, `dsar_crypto.py` is a thin wrapper), plus a keyed BLAKE2b fingerprint (`DSAR_FINGERPRINT_KEY`, 64 hex, helm `autoProvisionedKeys.dsarFingerprint`) used solely for rate limiting/duplicate detection. A missing key fails closed (`503 DSAR_UNAVAILABLE`). Signed-in requests store no contact data (admins reach the user via the linked `subjectUserId`). All contact fields **and the admin `responseNotes`** (free text that may quote the requester) are blanked by the maintenance job: **`DSAR_CONTACT_RETENTION_DAYS` (default 30) after a request closes**, immediately on expiry, and immediately when an unverified request is rejected/withdrawn (`contactPurged: true`).

## Audit trail

`dsar_request_events` is append-only (a DB trigger refuses UPDATE and direct DELETE) and gets a row for every state change (create, verification, status, assignment, notes, extension, action, expiry, purge), with actor kind + actor UUID and **no PII** in `detail`. The `act` endpoint additionally writes the existing mandatory `audit_log` rows (`dsar.export|erase|do_not_sell`).

## Endpoints

| Method | Path | Auth | Tier |
|--------|------|------|------|
| GET | `/api/v1/tenant/<slug>/privacy/requests` | `tenant:admin` | every tier |
| POST | `/api/v1/tenant/<slug>/privacy/requests` | `tenant:admin` | every tier |
| GET | `/api/v1/tenant/<slug>/privacy/requests/<id>` | `tenant:admin` | every tier |
| PATCH | `/api/v1/tenant/<slug>/privacy/requests/<id>` | `tenant:admin` | every tier |
| POST | `/api/v1/tenant/<slug>/privacy/requests/<id>/extend` | `tenant:admin` | every tier |
| POST | `/api/v1/tenant/<slug>/privacy/requests/<id>/act` | `tenant:admin` | every tier |
| POST / GET | `/api/v1/user/me/privacy-requests` | bearer JWT (tenant from token) + scope `*:read` (every session; service tokens have none); the rollout flag gates both; non-positive user ids are `401` | every tier |
| POST | `/api/v1/public/<tenant_slug>/privacy-requests` | none (captcha) | every tier |
| POST | `/api/v1/public/<tenant_slug>/privacy-requests/resend-verification` | none (captcha) | every tier |
| GET / POST | `/api/v1/public/privacy-requests/verify` | none (token). GET is a non-consuming preview, POST consumes | every tier |
| POST | `/api/v1/public/privacy-requests/verify/preview` | none (token in body) | every tier |
| GET | `/api/v1/public/privacy-requests/captcha-config` | none | every tier |
| GET | `/api/v1/public/privacy-requests/altcha-challenge` | none | every tier |
| POST | `/api/v1/tenant/<slug>/privacy/bulk` | `tenant:admin` | **Enterprise** (`402` otherwise) |

List filters: `status`, `type`, `regime` (int codes), `dueState` (`ok|due_soon|overdue`), `assignee` (user UUID), `sort` (`requestedAt|dueAt`), `order` (`asc|desc`), `limit` (1-100, default 25), `offset`. Any invalid value or unknown parameter is `400`. Stable order: sort key then `id`. The default view excludes statuses 2 and 8. Responses carry `total`.

`act` (`{action, confirm?}`): `action` must match the request type (1 export, 2 erase, 3 do_not_sell; other types are handled manually, `422 DSAR_ACTION_NOT_APPLICABLE`); the request needs a linked user (`subjectUserId`, `422 DSAR_NO_SUBJECT_USER`); erase needs `confirm: true`. It re-uses `admin_data_privacy_service.run_single_dsar` (tenant fence, erasure rails, mandatory audit). Success: erase/do-not-sell -> request `completed`; export -> `in_progress` (complete it once the data is delivered). A refused/failed result changes nothing but the event log and returns the existing 404/409/403/500/503 mapping. Only a FULLY successful result completes a request: a `partial` export (data returned but a source failed, `incomplete` lists them) answers `success: false`, leaves the request `in_progress` and records `outcome: partial`. **Single flight:** `act` first takes a compare-and-set claim on the row (`status` + `updated_at`), so two admins acting at once run the erasure once and the loser gets `409 DSAR_CONFLICT`. **Any act on a public-form request** (export, erase, do-not-sell) also requires the requester's verified email to equal the linked account's email (compared server-side by keyed fingerprint; neither address is returned or logged), else `422 DSAR_SUBJECT_EMAIL_MISMATCH`, unless the admin sends `overrideReason` (recorded in the event trail as `email_override` + `override_reason`). The gate covers every action: an export returns the linked account's data to the admin, who relays it to the requester, so a mismatched link would disclose another user's data. **Do-not-sell** sets the subject's GLOBAL opt-out (their `cookie_consent` preference, not a tenant setting); it is opt-out only, idempotent (`already_done`), and the subject can withdraw it through their own consent controls. Nothing in the backend yet gates a data-sale/sharing decision on the flag; any future decision must read the global preference.

**Tenant scoping of references.** `subjectUserId` (create/update) must be a user IN THIS tenant by the same definition `act` enforces (`admin_data_privacy_service.resolve_scope`: member of a tenant community, or tenant admin). An unknown, malformed or other-tenant UUID all return one identical `404 NOT_FOUND` (`User not found`), so the endpoint cannot probe platform-wide user UUIDs. `assigneeId` must be an admin of this tenant; every failure is one identical `400` (`assigneeId must be an admin of this tenant`). The `assignee` list filter resolves the same way (non-admin/foreign/unknown = no matches). `act` re-checks the subject is still in the tenant before running; if not, the client gets the uniform `404` and the audit log plus the request event (`reason: subject_not_in_tenant`) keep the true cause.

**Concurrency and subject changes.** `PATCH`, `extend` and `act` are compare-and-set on the status and `updated_at` the admin read; a lost race is `409 DSAR_CONFLICT` and nothing is written (the DAL stores second-precision datetimes, so every write bumps `updated_at` by at least one second to keep it a usable version). `subjectUserId`: a signed-in request's subject is never changed (`409 DSAR_SUBJECT_LOCKED`); changing an already-linked subject needs `subjectOverrideReason` (`409 DSAR_SUBJECT_OVERRIDE_REQUIRED` without it; reason recorded on the `subject_linked` event); linking a not-yet-linked subject (the "link subject user" operation) is this same PATCH. Requesters see `extendedUntil` and the generic `extensionReason: "statutory_extension"` on their own requests (never admin notes or the admin's free-text reason).

Error codes: `DSAR_INBOX_DISABLED` 404, `DSAR_CONFLICT` / `DSAR_SUBJECT_LOCKED` / `DSAR_SUBJECT_OVERRIDE_REQUIRED` 409, `DSAR_SUBJECT_EMAIL_MISMATCH` 422, `DSAR_UNVERIFIED` 409, `DSAR_REQUEST_CLOSED` 409, `DSAR_ALREADY_EXTENDED` 409, `DSAR_DUPLICATE_OPEN` 409 (signed-in intake), `DSAR_ACTION_NOT_APPLICABLE` / `DSAR_NO_SUBJECT_USER` 422, `DSAR_RATE_LIMITED` 429, `DSAR_CAPTCHA_FAILED` 400, `DSAR_CAPTCHA_UNAVAILABLE` / `DSAR_UNAVAILABLE` 503, `DSAR_VERIFICATION_INVALID` 400, `PAYLOAD_TOO_LARGE` 413.

## Operations

- Maintenance (`dsar_service.run_maintenance`): expires unverified requests, purges contact data, refreshes the overdue gauge. Runs every 5 min in each hub-api replica (idempotent) and lazily on every admin list call.
- Telemetry (no PII, bounded labels): `waddles_dsar_requests_created_total{source,type}`, `waddles_dsar_verification_total{outcome}`, `waddles_dsar_captcha_failures_total{provider,reason}`, `waddles_dsar_captcha_verify_duration_seconds`, `waddles_dsar_actions_total{action,outcome}`, `waddles_dsar_rate_limited_total{scope}`, `waddles_dsar_unverified_expired_total`, `waddles_dsar_contacts_purged_total{reason}`, gauge `waddles_dsar_overdue_requests`; spans `dsar.create_request`, `dsar.update_request`. DEBUG logs at decision points; errors log type + traceback.
- **Verification email** (`services/dsar_email_sender.py`, SMTP via `aiosmtplib`): sends the single-use link for the public form and for resend. Wired through the `dsar_notifier` seam: an explicit `app.config["DSAR_NOTIFIER"]` wins, else the SMTP sender when `DSAR_SMTP_HOST` is set, else the seam stays **fail-loud** (request stays `verification_pending`, a `verification_send_failed` event + ERROR log are written, nothing is faked, and the public response is the same 202 either way, so the requester learns nothing). Delivery failures never fail the HTTP request; they are events + metrics (see Delivery model below). The link is `<DSAR_VERIFY_URL_BASE>?tenant=<slug>#token=<token>`: the token rides in the fragment (not in server/proxy logs or a Referer), `tenant` powers the page's "resend" action. `DSAR_VERIFY_URL_BASE` defaults (Helm) to `<webui.publicUrl>/privacy-request/verify`.
- **Delivery model (durable outbox, off the request path)**: the public submit and resend routes do their DB work, rate limit and captcha, **enqueue** a row in `dsar_mail_outbox` and return the uniform 202; they never await SMTP. Why: (1) timing: an in-band send made `resend-verification` slower when a pending request existed, a timing oracle on tenant+email existence, so the no-pending / unknown-tenant / duplicate branches now run `dsar_outbox.decoy` (the same number of real DB writes, a fresh token hashed and sealed) and are statistically indistinguishable (tested); (2) DoS: a dead relay can no longer pin a worker for `DSAR_SMTP_TOTAL_TIMEOUT` per public call. A worker loop in every hub-api replica claims due rows with a conditional-UPDATE lease (exactly one replica wins a row, same on Postgres and sqlite), sends through the notifier, and records `verification_sent`/`verification_resent`, or `verification_send_failed` + ERROR log on terminal failure. Rows survive a pod restart. Every claim stamps a per-claim `lease_token`; the lease is renewed to cover the whole send just before it starts, and every state write (retry, terminal) is a conditional UPDATE guarded by that token with a rowcount check, so a worker whose lease expired cannot send or overwrite a row another worker re-claimed (it reports `lost`). One row is claimed per iteration. Delivery is **at-least-once**: if SMTP accepted the mail but the terminal DB write fails, the `verification_sent` event is already written (it is recorded before the terminal UPDATE), an ERROR `dsar.outbox.sent_unconfirmed` is logged (metric `waddles_dsar_verification_total{outcome=sent_unconfirmed}`), and the row is re-sent after the lease expires (the earlier link stays valid, so a duplicate is harmless). The raw token is sealed in the row (nulled at the terminal state); the requester email is never copied there (the worker opens it from the request's sealed contact blob). A row is discarded, not mailed, if the request is no longer `verification_pending`, the verification token has expired (`token_expired`) or a resend rotated the token. `dsar_outbox.enqueue` commits on its own; the public submit and resend routes do NOT use it: they call `dsar_outbox.enqueue_in_transaction(dal, ...)` (sync, non-committing) inside `ban_suppression.run_in_transaction` together with the request write/rotation, so both commit or roll back together, then `dsar_outbox.after_commit_enqueued()`.
- **Worker retry policy**: permanent failures, a misconfigured or missing sender, and an unset `DSAR_VERIFY_URL_BASE` fail the row at once. Transient failures (sender-classified `transient`, plus raw timeouts and connection errors; the SMTP layer already retried within one attempt) are re-queued with exponential backoff + jitter until `DSAR_OUTBOX_MAX_ATTEMPTS`, then fail terminally. Env (Helm `config.dsarOutbox.*`): `DSAR_OUTBOX_MAX_ATTEMPTS` 5, `DSAR_OUTBOX_BACKOFF_SECONDS` 30 (cap 1 h), `DSAR_OUTBOX_LEASE_SECONDS` 180 (initial claim lease; the worker renews it to the sender's total timeout + 30 s before sending), `DSAR_OUTBOX_POLL_SECONDS` 5, `DSAR_OUTBOX_RETENTION_DAYS` 7 (terminal rows, including decoys, are pruned hourly). Mail arrives within a poll interval (an enqueue wakes the local worker immediately).
- **Queue metrics**: gauge `waddles_dsar_mail_queue_depth` (pending rows, last worker poll), histogram `waddles_dsar_mail_queue_latency_seconds{outcome=sent|failed|discarded}` (enqueue to terminal), counter `waddles_dsar_mail_queue_total{outcome}`; `waddles_dsar_verification_total{outcome=queued|sent|resent|send_failed}`. Alert on a depth that stays above 0 or on `send_failed`.
- **Message**: multipart text + simple HTML, fixed neutral subject ("Confirm your privacy request"), operator-set From name, only the link, its lifetime (`DSAR_VERIFY_TOKEN_TTL_MINUTES`, default 60) and "ignore this if you didn't request it". Headers: `Message-ID`, `Date`, `Auto-Submitted: auto-generated`; no tracking. Built with the stdlib `email` API; every header value is validated (CR/LF/control characters rejected or stripped), so requester input cannot reach Subject/From.
- **Failure policy**: 4xx, connection errors, timeouts and disconnects are retried (`DSAR_SMTP_MAX_RETRIES`, exponential backoff with jitter, capped); 5xx, TLS/certificate errors, auth errors, a relay without STARTTLS and bad config are permanent and never retried. Certificate verification is always on (no off switch); STARTTLS is required, never opportunistic.
- **Requester email is PII**: never in logs, spans, metric labels or exception text (relay replies often echo it, so errors are re-raised sanitised); logs carry only a short keyed fingerprint. The outbox failure log carries the exception class and a bounded reason only (no traceback: exception text can echo the email or token).

### SMTP setup

| Env var | Helm value (`config.dsarSmtp.*`) | Default | Notes |
|---|---|---|---|
| `DSAR_SMTP_HOST` | `host` | unset | unset = no sender (fail-loud) |
| `DSAR_SMTP_PORT` | `port` | 587 / 465 / 25 | by TLS mode |
| `DSAR_SMTP_TLS` | `tls` | `starttls` | `starttls` \| `tls` (implicit) \| `none` |
| `DSAR_SMTP_ALLOW_PLAINTEXT` | `allowPlaintext` | false | required for `none`; dev only; never with credentials |
| `DSAR_SMTP_TLS_CA_FILE` | `tlsCaFile` | system CAs | extra trust anchor for a private relay CA; verification stays on |
| `DSAR_SMTP_USERNAME` / `DSAR_SMTP_PASSWORD` | Secret (`existingSecret`) | unset | both or neither |
| `DSAR_SMTP_FROM_ADDRESS` | `fromAddress` | required | envelope + From |
| `DSAR_SMTP_FROM_NAME` | `fromName` | `Privacy Requests` | operator-set only |
| `DSAR_SMTP_REPLY_TO` | `replyTo` | unset | optional |
| `DSAR_SMTP_CONNECT_TIMEOUT` / `_SEND_TIMEOUT` / `_TOTAL_TIMEOUT` | `connectTimeoutSeconds` / `sendTimeoutSeconds` / `totalTimeoutSeconds` | 5 / 10 / 30 s | total caps all attempts |
| `DSAR_SMTP_MAX_RETRIES` / `_BACKOFF_SECONDS` | `maxRetries` / `backoffSeconds` | 2 / 0.5 | backoff capped at 8 s |

1. Create the credentials Secret (external, operator-supplied; the chart never generates or placeholders it; skip for an unauthenticated relay):
   `kubectl -n <namespace> create secret generic waddlebot-dsar-smtp --from-literal=DSAR_SMTP_USERNAME=<user> --from-literal=DSAR_SMTP_PASSWORD=<password>` (read from a file or prompt, not shell history). Use `config.dsarSmtp.existingSecret` for another name.
2. Set the non-secret values (`config.dsarSmtp.host`, `fromAddress`, optionally `fromName`/`tls`/`port`) and make sure `webui.publicUrl` (or `config.dsarVerifyUrlBase`) is the public URL of the webui.
3. `helm upgrade`; hub-api picks up the env on restart. Metrics: `waddles_dsar_email_total{outcome=sent|failed_transient|failed_permanent|failed_config}`, `waddles_dsar_email_retries_total{reason}`, `waddles_dsar_email_send_duration_seconds{outcome}`; span `dsar.email.send`.

### Troubleshooting

| Symptom | Event `reason` / log | Fix |
|---|---|---|
| Requests stay `verification_pending`, no mail | `NotifierUnavailable`, or a queue-depth gauge > 0 (worker not running / relay down: check `dsar.outbox.*` logs) | `DSAR_SMTP_HOST` unset, or `DSAR_VERIFY_URL_BASE` / `webui.publicUrl` blank |
| `config` | invalid env, message names the variable | fix that variable (never prints values) |
| `tls` | certificate/handshake failure | relay cert not trusted or wrong hostname: use a cert for the host you configured, or mount the CA and set `tlsCaFile` |
| `tls_unsupported` | relay lacks STARTTLS | use port 465 with `tls: tls`, or a relay with STARTTLS |
| `auth` | 5xx on login | wrong credentials in the Secret |
| `smtp_5xx` | relay refused the message/recipient | relay policy (sender domain, relay denied); not retried |
| `smtp_4xx` / `connect` / `timeout` / `disconnected` | retried, then failed | relay down, greylisting or network policy; check egress to the relay |

An unverified request whose mail failed can be re-sent by the requester from the verify page (resend) until it expires after `DSAR_UNVERIFIED_EXPIRY_HOURS` (72 h).

## Level 2 seam (Enterprise automation)

Bulk stays on `POST .../privacy/bulk` (Enterprise). Automation rules (auto-assign, auto-respond, SLA escalation) would hook `dsar_service.create_request()` post-insert and `run_maintenance()`; none is built. Gate any such hook with `waddles.compliance.bulk_dsar`, never `waddles.dsar-inbox`.
