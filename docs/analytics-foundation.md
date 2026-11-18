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

## Retention

Purge by `retention_days`: delete rows where the row's time column + `retention_days` < now
(`hour_ts`, `day`, `session_start`, `cohort_week`); indexes exist for each. Free writes
`retention_days = 90`, Premium 400. Layer B retention is never longer than its input.

## Privacy / erasure

- Art. 17: `member_daily`, `member_sessions`, `member_signals_daily` are DELETE rules keyed on
  the user UUID in `services/erasure_registry.py` (schema-qualified `analytics_fp.*` names).
- `data_use_exclusions` is retained on erasure (suppression list, pseudonymous token only) so an
  erased/objecting user is never re-included.
- Do-not-sell is a global per-user preference (cookie_consent); GDPR objection/erasure must
  propagate into `data_use_exclusions`.

## Layer B inclusion rules (enforced by the later builder)

`k >= 50` contributors per cell; one-way job from consent-filtered input only; exclude users
present in `data_use_exclusions` (any reason), users with no age signal, and EU tenants (until
counsel approves a transfer mechanism); tenant must have an `in` row in
`tenant_data_programme_optin`.

## Compliance gates (13) and v3.0 behaviour

The approved design defines 13 compliance gates that every export decision must pass (the gate list is owned by the compliance review; this document does not restate it).
In v3.0 the evaluator is not
built: the only outcome is `DENY` with `EXPORT_NOT_IMPLEMENTED`, logged to `export_runs`.
