# Enterprise Audit Logging (tamper-evident)

Enterprise-tier audit & compliance tooling for Waddles hub-api. It replaces a mutable,
sparsely-populated `audit_log` table whose writer swallowed failures with an **append-only
hash chain**: every security-relevant event is recorded, and altering, removing, inserting or
re-ordering any record is detectable by anyone who can re-run the hash.

This closes GRC audit finding #3 (the audit log was not tamper-evident, lacked coverage of
security-relevant events, and had an `except: pass` swallowing write failures).

| GRC #3 finding | Fix |
|---|---|
| Not tamper-evident | SHA-256 hash chain, one chain per tenant + a platform chain (`audit_events`, migration `0053_audit_events_hash_chain`); verifier API, CLI and offline checker |
| Coverage gaps | App-wide HTTP hook + session-mint hook + legacy bundle bridge: authz decisions, tenant/role changes, erasure/DSAR, SSO/passkey/password logins, admin actions, license/subscription changes |
| `except: pass` | Removed. A write failure is logged at ERROR (type + value-free cause + traceback), counted, and raised as `AuditWriteError`; entitled requests fail closed |

## Tiering

| Capability | Gate | Tier |
|---|---|---|
| Recording into the chain, `GET /events`, `/head`, `/verify` | PostHog flag **and** entitlement `waddles.compliance.audit_logs` | Enterprise |
| `GET /export` | `waddles.compliance.audit_export` **and** `audit_logs` | Enterprise |
| Legacy `audit_log` basic trail (bundle lifecycle) | none | every tier (unchanged) |
| DSAR export / erasure / consent withdrawal | **never gated** | every tier |

Both flags default **OFF** until validated and never grant a feature by flag alone: the tenant's
effective tier (`max(tenant, community)`) must also be Enterprise (`flask_core.tier_catalog`).
An un-entitled tenant's events are *skipped by policy* (counted, DEBUG log) -- that is the gate
working, not a failure. The statutory privacy routes are still *audited* for entitled tenants, but the
rights themselves are never tier-gated, and a retry after an audit outage is idempotent (erasure
answers `already_deleted`).

### Scopes

All four routes require `compliance.audit:admin` -- deliberately **not** `:read`. Every session is
minted with the `*:read` wildcard (`auth_service.create_session_token`) and `require_scope` honours
`*:<action>` wildcards, so a `compliance.audit:read` requirement would be satisfied by any logged-in
user. `*:admin` is held only by platform super-admins; the **tenant-owner** bundle
(`SCOPE_BUNDLES["tenant"]["admin"]`) lists `compliance.audit:admin` explicitly. Regression tests:
`libs/flask_core/tests/test_authz_decision.py`, `hub_api/tests/test_compliance_audit_blueprint.py`.

A caller only ever reads **their own tenant's** chain (tenant comes from the JWT, never the request).
The platform chain (`?chain=platform`) additionally needs the platform-only `users:admin` scope.

## Architecture

```
 request ──► tenant_middleware ──► require_scope ──► handler ──► response
                  │                    │ publishes request.authz_decision
                  ▼                    ▼
              TenantContext      AuthzDecision(required_scopes, allowed, reason, subject)
                                          │
        after_request  audit_http._audit_response  ◄─────────────┘
              │  classify_request(method, URL *rule*, status, decision)   (services/audit_events.py)
              ▼
        AuditEvent (validated, PII-free)  ──►  AuditService.record
                                                  │ 1. gate: feature_enabled(audit_logs, tenant)
                                                  │ 2. resolve actor int -> hub_users.uuid
                                                  │ 3. append (advisory lock / PK retry)  ─►  audit_events
                                                  │ 4. failure => ERROR log + metric + AuditWriteError

 create_session_token ──► record_session_issued ─► AuditService.record   (every login path)
 bundle_audit.record  ──► legacy audit_log row (all tiers) + AuditService.record (Enterprise)
```

Code map: `hub_api/services/audit_chain.py` (pure hashing/verification, stdlib only),
`audit_events.py` (vocabulary, validation, request classification), `audit_service.py`
(persistence, gate, telemetry), `audit_http.py` (hooks), `hub_api/blueprints/v1/compliance_audit.py`
(API), `alembic/versions/0053_audit_events_hash_chain.py` (schema).

## The hash chain

Each chain (`tenant:<id>` or `platform`) is a gapless sequence `seq = 1, 2, 3 ...`.

```
record_hash = SHA-256( "waddles.audit.chain.v1\n" || canonical_json(fields) )
prev_hash   = previous record's record_hash        (64 zeros for seq = 1)
```

`canonical_json` is every audited column -- `chain_id, seq, event_id, occurred_at, actor_uuid,
actor_kind, category, action, outcome, target_type, target_id, details, prev_hash, hash_version` --
as sorted-key, whitespace-free, ASCII-escaped JSON; `occurred_at` is UTC with microseconds
(`2026-10-10T12:00:00.123456Z`). `record_hash` itself is excluded. `hash_version` (`sha256-v1`)
is stored and hashed, so a future canonical form is a new value, never a silent re-interpretation.

| Attack | Detected as |
|---|---|
| Edit any field of a record | `hash_mismatch` at that `seq` |
| Edit a record **and** recompute its hash | `prev_hash_mismatch` at the *next* record |
| Delete a record | `seq_gap` |
| Insert / re-order records | `seq_gap` / `prev_hash_mismatch` |
| Move a record to another chain | `chain_id_mismatch` |
| Truncate the **tail** | only with a pinned head -> `head_mismatch` (see below) |

Database-level defence in depth (the chain, not these, is the proof): `PRIMARY KEY (chain_id, seq)`
and `UNIQUE (chain_id, prev_hash)` (no fork, no repeated seq); `BEFORE UPDATE OR DELETE` and
`BEFORE TRUNCATE` triggers that raise; `hub_api` granted `SELECT, INSERT` only; CHECK constraints on the
vocabularies and the hex shape of both hashes. No foreign keys: erasing a user or tenant must never
cascade into immutable history.

### Honest limits (threat model)

* SHA-256 is **not keyed**. Someone who can rewrite *every* record from the tamper point to the head can
  recompute a self-consistent chain, and a superuser can `DROP TRIGGER`. The countermeasure is
  **anchoring**: pin the head `(seq, hash)` somewhere the database operator cannot write (ticket, WORM
  bucket, SIEM) and re-verify against it. Tail truncation is invisible without a pin.
* The log proves integrity after the fact; it does not prevent a privileged insider from *adding* false
  records. Restrict INSERT to the `hub_api` role (already the case) and alert on `AUDIT CHAIN TAMPER DETECTED`.
* Source IP and user agent are intentionally **not** stored (PII). Correlate through the platform's
  access logs if needed.

## What is recorded

| Category | Action | Source |
|---|---|---|
| authz | `authz.denied` | any authenticated 403 (scope check or inline authz); unauthenticated 401/403 are *not* recorded (no identity; cannot bloat the chain) |
| authn | `authn.session_issued` (`auth_method`: password, admin_password, register, email_verification, temp_password, refresh, passkey, `oauth_<platform>`) | `create_session_token`, the single mint site of every login path; the audit write happens **before** the session row is persisted |
| tenant | `tenant.created/updated/deleted/settings_changed/modules_changed` | route map `SEMANTIC_ROUTES` |
| role | `tenant.admin_added/removed`, `role.super_admin_changed/vendor_changed/analytics_consumer_changed/member_changed/platform_user_changed/definition_changed` | route map |
| user | `user.created/updated/deleted/password_reset` | route map |
| privacy | `privacy.dsar_export`, `privacy.erasure_requested` | route map (GET + DELETE `/api/v1/user/me/data`) |
| license | `license.subscription_changed`, `license.provider_event` (Stripe/PayPal webhooks, actor `external`) | route map |
| admin | `admin.platform_config_changed`; `admin.action` for **every other scope-protected state-changing request** | route map + generic rule |
| bundle | `app_installed_globally`, `permissions_approved_globally`, `tenant_availability_*`, ... | `bundle_audit.record` bridge (also kept in the legacy `audit_log`) |
| audit | `audit.exported` | export endpoint (recorded **before** data is returned) |

Coverage is a table, not a memory exercise: any new scope-protected `POST/PUT/PATCH/DELETE` route is an
`admin.action` automatically. Add a `SEMANTIC_ROUTES` entry only to give a route a more specific
action; `tests/test_audit_http.py::TestAppFactoryWiring::test_every_semantic_route_names_a_real_route`
fails if an entry no longer matches a live route. Service-to-service plumbing (`/api/v1/internal/*`,
`/internal/*`, `/mcp/*`) is never classified.

### PII contract

The chain is PII-free **by construction** (`AuditEvent.__post_init__` rejects violations):

* the actor is a `hub_users.uuid` (`uuid.UUID`), resolved from the JWT subject; `actor_kind` is
  `user | service | system | external | unresolved` and only `user` carries a UUID;
* the URL **rule** (`/api/v1/tenant/<tenant_slug>/admins`) is recorded, never the concrete path, query
  string or body;
* `details` is flat, keys are `snake_case`, values are `bool/int/None`, identifier-shaped strings
  (`[A-Za-z0-9_.:/<>=-]`, <=128, or `name@semver`) or short lists of those -- no spaces, no e-mail
  shapes, no floats, no nesting, and key names such as `email`, `username`, `name`, `ip`, `message`,
  `token`, `password` are refused outright;
* the legacy bundle bridge is *lenient*: non-conforming entries are dropped from the chain (the legacy
  `audit_log` row keeps them) and counted in `dropped_detail_keys`, so the omission is visible.

## Failure behaviour (fail loud, no silent fallback)

`AuditService.record` never swallows. On any failure it logs at ERROR --
`audit write FAILED -- event was NOT recorded: category=... action=... chain=... attempts=... cause=type=<ExcType> ...`
followed by a sanitised traceback (driver messages can embed bound values, so only the exception
type and frames are logged) -- increments `waddles.audit.write_failures`, and raises `AuditWriteError`.

* **HTTP hook:** the request is answered `500 {"error": {"code": "AUDIT_UNAVAILABLE"}}` rather than a
  success the platform cannot account for (NIST AU-5, fail closed) -- *for entitled tenants only*.
* **Logins:** no session is issued if its audit event cannot be written.
* **Export:** the `audit.exported` event is recorded first; if it fails, nothing is disclosed.
* **Legacy `bundle_audit.record`:** was best-effort (`except: pass`); now raises `AuditWriteError`. Flows with required
  follow-on work (cascades, Valkey invalidation, signed-sidecar upload) use `bundle_audit.DeferredAudit`: the audit
  failure is logged immediately, the follow-on work still runs, and the error is raised afterwards -- loud, never dropped,
  never half-applied.
* **Other former swallows:** the consent-proof trail (`cookie_consent_service.log_audit_event`) now raises, and the
  failed-deletion bookkeeping row in `data_privacy_service` logs loudly without masking the original error.
* Un-entitled tenants never touch the table, so a lagging migration cannot break them.

Concurrent writers are safe: Postgres takes a per-chain `pg_advisory_xact_lock`; on any engine the
primary key makes the loser of a race re-read the head and retry (bounded; sustained contention fails
loudly with `attempts=N`). A genuine constraint violation is *not* retried.

## API

Base `/api/v1/compliance/audit`. Responses are explicit DTOs (`@validate_response`); `402` when the
tenant lacks the Enterprise entitlement; `400` for invalid query values; spec in `openapi/v1.yaml`.

| Route | Purpose | Notable params |
|---|---|---|
| `GET /events` | newest-first page of the caller's chain | `category, action, outcome, actor (uuid), since, until, page, limit<=200`, `chain=platform` (super-admin) |
| `GET /head` | newest record `(seq, record_hash)` -- the value to pin | |
| `GET /verify` | recompute the chain; **200** `intact`/`empty`, **409** `broken` | `expected_head_seq`, `expected_head_hash`, `from_seq` + `anchor_hash` (resume), `max_records` |
| `GET /export` | ascending slice + manifest (`Content-Disposition: attachment`) | `after_seq, limit<=1000`; page with `manifest.next_after_seq` |

`status` is three-valued: `intact`, `broken`, **`empty`** -- an empty chain examined zero records and is
never reported as `ok`. Verification over a very long chain is resumable: an incomplete slice returns
`next_seq` and `anchor_hash`; pass them back as `from_seq` / `anchor_hash` (the database is never trusted
to supply its own anchor).

## Operating it

**Enable.** Apply migration `0053_audit_events_hash_chain` (hub-api's bootstrap Job runs alembic), then turn
on `waddles.compliance.audit_logs` (and `waddles.compliance.audit_export`) for the tenant in PostHog. The
tenant must hold an Enterprise entitlement; bypass is by hardcoded domain only, never env/config.

**Verify on a schedule.** The chain only proves anything if somebody re-computes it.

```bash
make verify-audit-chain                                  # exits 1 on tampering, 2 if zero records examined, 3 if it couldn't run
make verify-audit-chain EXPECT="tenant:1=42:<head-hash>" # also catches tail truncation
make verify-audit-chain ALLOW_EMPTY=1                    # deployments with no Enterprise tenant yet
```

A Kubernetes `CronJob` running `python3 -m cli.verify_audit_chain` in the hub-api image (same env as the
bootstrap Job) is the intended production schedule; alert on a non-zero exit or on the
`AUDIT CHAIN TAMPER DETECTED` log line / `waddles.audit.chain_breaks` metric.

**Pin the head.** Record `GET /head` (`seq`, `record_hash`) outside the database on a cadence you can defend to
an auditor, and pass it back via `expected_head_*` / `EXPECT=`.

**Hand an auditor the log.** Export, then let them verify with no access to Waddles at all:

```bash
make verify-audit-export EXPORT="page1.json page2.json"            # chain starting at seq 1
make verify-audit-export EXPORT="page3.json" ANCHOR=<hash of seq N-1 from a head you pinned>
make verify-audit-export EXPORT="all.json" HEAD_SEQ=42 HEAD_HASH=<pinned head hash>
```

`scripts/verify_audit_export.py` loads the production `audit_chain.py` by path (stdlib only). A slice that
does not start at seq 1 *requires* `--anchor-hash`; the file's own `first_prev_hash` is not trusted.

**Retention.** Records are immutable by design (triggers + grants); there is no pruning. Archive by export,
verify, then store the export and the pinned head; the platform imposes no deletion path.

## Telemetry

Standard OTLP env configuration only (`OTEL_EXPORTER_OTLP_*`); no vendor SDK. A dead exporter never fails a
request. No PII or tenant ids in labels.

| Signal | Name | Labels |
|---|---|---|
| counter | `waddles.audit.events` | `result=recorded|skipped`, `category` |
| counter | `waddles.audit.write_failures` | `category` |
| counter | `waddles.audit.chain_breaks` | `reason` |
| counter | `waddles.audit.verifications` | `status` |
| histogram | `waddles.audit.append_duration_ms`, `waddles.audit.append_attempts`, `waddles.audit.verify_records` | |
| spans | `audit.append`, `audit.verify` | `audit.category`, `audit.action`, `audit.seq`, `audit.verify.status` |

## Adding an audited event

```python
from services.audit_events import AuditAction, AuditCategory, AuditEvent
from services.audit_service import get_audit_service

await get_audit_service(install_dal).record(
    AuditEvent(
        category=AuditCategory.TENANT,
        action=AuditAction.TENANT_SETTINGS_CHANGED,
        actor_user_id=user_id,          # resolved to hub_users.uuid for you
        tenant_id=ctx.tenant_id,
        target_type="tenant_settings",
        target_id=f"{ctx.tenant_id}",
        details={"changed_keys": ["theme"]},   # identifiers only -- never user input
    )
)   # raises AuditWriteError on failure: do NOT wrap it in try/except
```

## A defect the old swallow was hiding

`bundle_audit.record` inserted `datetime.now(UTC)` (timezone-aware) into `audit_log.created_at`, a plain
`TIMESTAMP`. asyncpg rejects that combination, so on real Postgres **every legacy bundle-lifecycle audit row failed to
insert** -- and `except: pass` hid it, leaving the trail empty. Removing the swallow surfaced it immediately in the real-Postgres
seeder test; the value is now passed in whichever form the reflected column accepts
(`bundle_audit._legacy_created_at`), pinned by `hub_api/tests/test_audit_pg_integration.py`.
