# Analytics Foundation (Phase 1)

Architecture contract for the analytics work items that follow (stream producer, `svc_analytics`
consumer, read endpoints, trend builder). Schema: alembic `0129_analytics_foundation`.

**There is NO export path in v3.0.** `export_runs` is an audit log; every export decision is
hard-coded `DENY` with `EXPORT_NOT_IMPLEMENTED`. Nothing in this release can send analytics data
outside the product.

## Two layers, one hard wall

| | Layer A `analytics_fp` | Layer B `analytics_trend` |
|---|---|---|
| Owner | the community / tenant (first party) | platform (anonymized trend metadata) |
| Sold / exported | NEVER | only ever this layer, only after v3.x gates; not in v3.0 |
| Grain | community (hour/day/session/cohort), member (UUID token) | tenant x week or coarser |
| Dimensions | community, member token | none of community / user / channel; no user- or event-level rows |
| Tiers | Free: basic counts. Premium: in-depth (sessions, cohorts, signals) | n/a |
| Retention | Free 90 d, Premium 400 d | never longer than its input (400 d max) |

The wall is structural: `analytics_trend` tables have no community/user columns
(`tenant_trend.cohort_size >= 50` is a CHECK, so a below-k cell cannot be stored), and the role
that can read layer B (`trend_exporter`) has no grant of any kind on layer A or anything per-user.

## Tables

Int enum codes everywhere (never strings). Every layer A table carries `purpose_code`,
`classification_code`, `retention_days` (1..400), `provenance_code`, `created_at`.

| Table | Key | Tier |
|---|---|---|
| `analytics_fp.community_hourly(tenant_id, community_id, hour_ts, metric_code, platform_code, value)` | `(community_id, hour_ts, metric_code, platform_code)` | Free |
| `analytics_fp.member_daily(community_id, user_uuid, day, msgs, cmds, presence_s, first_ts, last_ts)` | `(community_id, user_uuid, day)` | Free |
| `analytics_fp.member_sessions(community_id, user_uuid, session_start, session_end, platform_code)` | `(community_id, user_uuid, session_start, platform_code)` | Premium |
| `analytics_fp.cohort_weekly(community_id, cohort_week, week_offset, active_members, cohort_size)` | `(community_id, cohort_week, week_offset)` | Premium |
| `analytics_fp.member_signals_daily(community_id, user_uuid, day, signal_code, score)` | `(community_id, user_uuid, day, signal_code)` | Premium, admin-only, PROFILING |
| `analytics_trend.tenant_trend(tenant_id, week, metric_code, dim_code, dim_value_code, value, cohort_size, provenance_code, computed_at)` | `(tenant_id, week, metric_code, dim_code, dim_value_code)` | layer B |

`member_signals_daily` is **profiling**: first-party, Premium-only, admin-only, never used for
automated decisions with legal or similarly significant effect (recorded in the registry lineage).

Governance (public schema, nothing seeded that enables anything): `analytics_dataset_registry`
(seeded with the six datasets above: layer, purpose, classification, retention, `min_k`, lineage),
`data_use_exclusions`, `tenant_data_programme_optin` (no row = OFF), `export_recipients`,
`platform_clearance` (empty), `export_runs`.

### Enum codes

| Enum | Codes |
|---|---|
| `purpose_code` | 1 first_party, 2 aggregate_trend |
| `classification_code` | 1 aggregate_anonymous, 2 pseudonymous |
| `provenance_code` / `platform_code` | 1 waddles_native, 2 platform_discord, 3 platform_twitch, 4 platform_youtube, 5 platform_other |
| `data_use_exclusions.reason_code` | 1 do_not_sell, 2 gpc, 3 objection, 4 erased, 5 age_not_adult |
| `tenant_data_programme_optin.status_code` | 0 none, 1 in, 2 withdrawn |
| `export_runs.mode_code` / `decision_code` | mode 1 dry_run, 2 live; decision 1 allow, 2 deny |

## Roles (NOLOGIN group roles)

LOGIN roles and passwords are provisioned later by the Helm/provision work. PUBLIC is revoked on
both schemas.

| Role | analytics_fp | analytics_trend | public governance |
|---|---|---|---|
| `analytics_writer` | SELECT/INSERT/UPDATE/DELETE | none | SELECT `data_use_exclusions` |
| `analytics_reader` | SELECT | none | none |
| `trend_builder` | SELECT `member_daily` only | SELECT/INSERT/UPDATE/DELETE | SELECT `data_use_exclusions`, `tenant_data_programme_optin` |
| `trend_exporter` | none | SELECT | INSERT `export_runs` only |

All four also get `USAGE` on schema `public` (name resolution only). If a `hub_api` group role
exists, it gets SELECT/DELETE on the three user-keyed layer A tables so the Art. 17 erasure
executor can reach them.

## Ingest -> analytics stream (wire shape for W2/W4)

Valkey stream `waddles:analytics` (its own stream; approximate `MAXLEN ~`), consumer group
`svc_analytics`. Entry fields:

| Field | Value |
|---|---|
| `v` | `1` |
| `tenant_id` | numeric |
| `community_id` | numeric (never a slug / UCID) |
| `user_uuid` | UUID token |
| `platform_code` | 1..5 as above |
| `kind_code` | 1 message, 2 command, 3 join, 4 leave, 5 presence_start, 6 presence_end |
| `ts_ms` | epoch ms |
| `provenance_code` | 1..5 |

**No handles, no usernames, no message content, no free text.** The PII boundary holds: analytics
stores hold only numeric internal ids and user UUID tokens.

### Producer (`core/svc_ingest/src/analytics.rs`)

Ingest XADDs one entry per successfully published platform event, after the main spine append.

| Property | Behaviour |
|---|---|
| Flag | `waddles.analytics-stream` (ENV baseline `FLAG_WADDLES_ANALYTICS_STREAM`), default OFF, never the bypass domain. OFF = one atomic load per event, zero queueing, zero XADD |
| Bound | `XADD waddles:analytics MAXLEN ~ ANALYTICS_STREAM_MAXLEN * ...` (default 500000, min 1000) |
| Isolation | hot path only `try_send`s onto a bounded queue (`ANALYTICS_QUEUE_CAPACITY`, 4096); a worker with bounded concurrency (`ANALYTICS_TOKENIZE_CONCURRENCY`, 8) tokenizes and XADDs under timeouts (`ANALYTICS_PUBLISH_TIMEOUT_MS` 250, `ANALYTICS_TOKENIZE_TIMEOUT_MS` 500). Queue full / timeout / Valkey error => drop + counter + rate-limited WARN; never an ingest failure or delay |
| Scope | the numeric tenant/community the publish funnel already resolved. No resolved scope => skip `no_scope`; tenant-wide (`community_id` 0) => skip `tenant_wide` |
| `kind_code` | `chat.message`/`message` 1, `interaction.command`/`chat.command` 2, `member.join` 3, `member.leave` 4, `presence.start` 5, `presence.end` 6; other event types are skipped (`unmapped_kind`) |
| `provenance_code` | = `platform_code`: 1 first-party Waddles, 2 Discord, 3 Twitch, 4 YouTube, 5 other. Codes 2..5 are platform-derived, so later gates can exclude them |
| Metrics | `svc_ingest_analytics_published_total`, `..._skipped_total{reason}` (`unmapped_kind`, `no_scope`, `tenant_wide`, `no_user_token`, `bad_timestamp`, `tokenizer_unavailable`), `..._failed_total{reason}` (`queue_full`, `timeout`, `error`, `connect`), `svc_ingest_analytics_publish_duration_seconds` |

**Member token (INTERIM resolution).** `user_uuid` is taken from `payload["user_uuid"]` when an
upstream stage stamped it (the planned PII-tokenizer service bundle will; that stamp always wins).
Until it exists, the producer's worker resolves the token itself with the EXISTING hub-backed
`IdentityTokenizer` (`MintEphemeralPseudonyms`, minted under the event's `tenant_id`) from the
platform's raw user id (`user_id`/`author_id`), off the hot path, behind a bounded TTL cache
(`ANALYTICS_TOKEN_CACHE_CAPACITY`, 10000; found 10 min, not-found 30 s, tokenizer-down 5 s) keyed by
a SHA-256 of (tenant, platform, id), so a raw id is never stored as a key. The raw id/handle never
reaches the stream, a log line, a metric label or the cache key. Failure, timeout or unknown id =>
the event is skipped (`tokenizer_unavailable` / `no_user_token`), never guessed. **This step is
interim: remove it when the tokenizer bundle stamps `user_uuid`.**

**Known limitation / open compliance item (DPIA).** The tokenizer returns the registered
`hub_users.uuid` when the platform id is linked to a hub user in that tenant, else a stable
per-tenant pseudonym stored once in `ephemeral_pseudonyms` (the same value as
`community_members.user_uuid`), so erasure and `data_use_exclusions` already act on these tokens.
But (a) when a linked account is erased its pseudonym mapping is deleted, so a returning user gets
a FRESH token that is not in `data_use_exclusions` (`record_erasure_exclusions` only covers tokens
in scope at erasure time); (b) do-not-sell preferences exist only for registered accounts (global
`cookie_consent`), so unlinked pseudonymous identities cannot carry a per-user opt-out (layer B
uses tenant-level opt-in regardless). A stale cached pre-erasure token is harmless: it is in
`data_use_exclusions` (ERASED), so `svc_analytics` drops it. Flag (a) and (b) for the DPIA.

## Retention

Purge by `retention_days`: delete rows where the row's time column + `retention_days` < now
(`hour_ts`, `day`, `session_start`, `cohort_week`); indexes exist for each. Free writes
`retention_days = 90`, Premium 400. Layer B retention is never longer than its input.

## Privacy / erasure

- Art. 17: `member_daily`, `member_sessions`, `member_signals_daily` are DELETE rules keyed on
  the user UUID in `services/erasure_registry.py` (schema-qualified `analytics_fp.*` names).
- Erasure (`erasure_executor.record_erasure_exclusions`, same transaction) inserts `(user_uuid, reason_code=4 erased)` into `data_use_exclusions` for every in-scope user token, `ON CONFLICT DO NOTHING` on the `(user_uuid, reason_code)` primary key. Idempotent; uuid token only.
- **`svc_analytics`' writer MUST check `data_use_exclusions` before every layer A upsert** (it has SELECT via `analytics_writer`) and drop the event for any excluded user token, so a late-arriving event can never re-create rows for an erased or objecting subject.
- `data_use_exclusions` is retained on erasure (suppression list, pseudonymous token only) so an
  erased/objecting user is never re-included.
- Do-not-sell is a global per-user preference (cookie_consent); GDPR objection/erasure must
  propagate into `data_use_exclusions`.

## Keeping `data_use_exclusions` current (W5)

hub-api is the ONLY writer (`hub_api/services/analytics_exclusions_service.py`); `svc_analytics`
and the layer B builder only read. Rows are `(user_uuid token, reason_code, since)` -- no PII.

| Trigger | Exclusion written / cleared | Where |
|---|---|---|
| Do-not-sell set (cookie save/update, GPC, admin DSAR do-not-sell) | add `1 do_not_sell` | `cookie_consent_service`, `admin_data_privacy_service.apply_do_not_sell` |
| `Sec-GPC: 1` applied on a write | add `2 gpc` (and `1`, GPC forces do-not-sell) | same |
| Marketing consent withdrawn (true -> false, or cookie revoke) | add `1 do_not_sell` (no separate documented code) | same |
| Explicit opt-back-in (`doNotSell` true -> false, no GPC) | remove `1` and `2` only | same |
| Marketing re-granted (false -> true) while `doNotSell` is not true | remove `1` only | same |
| Erasure | add `4 erased` (never removed) | `erasure_executor.record_erasure_exclusions` |
| Objection (`3`) | `add_exclusion(..., OBJECTION)` is available and `3` is removable, but **no objection/restriction request type exists yet** (`dsar_service.RequestType` has none), so nothing writes it | gap |
| Not an adult / age unknown (`5`) | **no age signal source exists**; nothing writes it; never removable by a consent change | gap |

* **Same transaction, no window.** The consent write and its exclusion change run as one
  synchronous executor job and commit once (`apply_consent_change`); a failure in either rolls
  back both. Audit (`privacy.exclusion_added` / `privacy.exclusion_removed`, uuid token +
  `reason_code` only) and the metrics (`hub_api.analytics_exclusions.added|removed`) follow the
  commit; an audit failure is raised loudly, never swallowed.
* **Consent stays a global per-user preference.** Anonymous visitors (no `user_id`) have no subject
  token, so no exclusion is written until the consent row is tied to a user.
* **EU-tenant exclusion for layer B** is a separate gate in the policy crate and is not duplicated.
* **Reconciler.** `reconcile_exclusions()` rebuilds MISSING rows from `cookie_consent`
  (`doNotSell` true -> 1) and the erasure ledger (`data_deletion_requests.status='completed'` -> 4
  for the retained `hub_users.uuid`). Add-only (so it can never undo a re-grant), idempotent,
  bounded batches (default 500), one transaction + `pg_try_advisory_xact_lock` per batch so a single
  replica works at a time (a lost lock ends the pass: `skipped_locked`). Runs from the hub-api
  startup loop every hour, gated by flag **`waddles.analytics-exclusions-reconcile`** (default
  OFF). GPC (2) and marketing-withdrawal rows are act-time only and not reconstructable; a GPC
  subject stays covered by `1`. Erasure tokens other than `hub_users.uuid` are not recoverable from
  the ledger (erasure already writes every token at act time). Metrics:
  `reconciled`, `reconcile_examined` (denominator), `reconcile_passes`.
* **Grant.** hub-api must hold `SELECT, INSERT, DELETE` on `public.data_use_exclusions`
  (migration 0129 grants it nothing there -- PUBLIC is revoked -- so unless the hub-api role owns
  the table the grant is required); do not widen any other role.

## Layer B inclusion rules (enforced by the later builder)

`k >= 50` contributors per cell; one-way job from consent-filtered input only; exclude users
present in `data_use_exclusions` (any reason), users with no age signal, and EU tenants (until
counsel approves a transfer mechanism); tenant must have an `in` row in
`tenant_data_programme_optin`.

## Compliance gates (13) and v3.0 behaviour

The approved design defines 13 compliance gates that every export decision must pass (the gate list is owned by the compliance review; this document does not restate it).
In v3.0 the evaluator is not
built: the only outcome is `DENY` with `EXPORT_NOT_IMPLEMENTED`, logged to `export_runs`.
