# svc-analytics

The analytics **layer A** writer. It consumes the Valkey stream `waddles:analytics` (consumer
group `svc_analytics`), validates every entry against the wire contract, drops excluded
subjects, and upserts first-party per-community aggregates into `analytics_fp`. Contract,
schemas, roles and tiering: [`docs/analytics-foundation.md`](../../docs/analytics-foundation.md).

Rust, standalone crate (own `Cargo.toml`/`Cargo.lock`/`deny.toml`, no workspace). In-cluster
Rust => **gRPC only**: the only network surface is a plaintext `grpc.health.v1` listener
(`GRPC_HEALTH_PORT`, default `50209`) for native kubelet probes. No REST, no
Prometheus scrape port; logs/metrics/traces leave via OTLP (`OTEL_*` env, `penguin-logging`).

## Flow

```
waddles:analytics --XREADGROUP--> validate --> exclusion gate --> tier gate --> ONE tx:
   (own PEL, idle-claim, new)        |   reject -> DLQ     |  drop+count        ledger insert (idempotency)
                                     v                     v                    exclusion re-check (in-tx)
                         waddles:analytics:dlq        (fail closed on outage)   community_hourly / member_daily
                                                                                member_sessions (Premium)
                                          XACK only after COMMIT <-------------
```

* **Backpressure**: one bounded batch (`ANALYTICS_BATCH_SIZE`) is read, written and acked before the next read.
* **Idempotency**: the stream entry ids a batch applies are inserted into
  `analytics_ops.applied_entries` in the SAME transaction as the upserts (alembic
  `0131_analytics_applied_entries`); only entries whose insert succeeded are applied. A crash
  between COMMIT and XACK therefore redelivers harmlessly (works across several consumers).
  Ledger rows older than 7 days are purged.
* **Poison handling**: contract violations, rows the database rejects (SQLSTATE class 22/23,
  isolated one by one) and an exclusion lookup that never recovers are written to
  `waddles:analytics:dlq` (`orig_id`, `reason`, `at_ms`, `consumer` -- never payload fields) and
  acked; counter `svc_analytics_dlq_total{reason}`. Transient database/Valkey faults leave entries
  pending and back off (no data loss).

## Contract validation (`src/contract.rs`)

Rejects (PII-free reason code): unknown/missing `v`, missing/duplicate/unexpected fields (free
text is never accepted), non-canonical numbers, `tenant_id` outside `0..=i32::MAX`,
`community_id < 1`, `user_uuid` not a canonical non-nil UUID, codes outside their enums,
`ts_ms` > now + 5 min or older than 400 days.

## Exclusions (erasure / objection / do-not-sell / not-adult)

Before every layer A write the user token is checked against `public.data_use_exclusions`
(any reason): a TTL cache (`ANALYTICS_EXCLUSION_TTL_SECS`, default 5 s, bounded by
`ANALYTICS_EXCLUSION_CAP`) that is **seeded at startup and lazy-loaded on a miss** (never only
invalidated). Excluded events are dropped and counted (`svc_analytics_dropped_total{reason="excluded"}`),
never written. **Fail closed**: if the lookup is unavailable nothing is written, the batch stays
pending (`svc_analytics_exclusion_lookup_failures_total` -- alert on it) and after
`ANALYTICS_MAX_ATTEMPTS` the entries are dead-lettered. The write transaction additionally
re-checks the table, so a subject erased inside the cache TTL cannot be re-created
(`reason="late_excluded"`). There is no community-level exclusion table in the schema.

## Tiering (Free vs Premium)

A community is Premium iff it has an active `community_tier_allocations` row (`tier_code = 2`,
`released_at IS NULL`); cached `ANALYTICS_TIER_TTL_SECS` (300 s). Any lookup failure => Free.

| Table | Free | Premium |
|---|---|---|
| `community_hourly` (messages, commands, joins, leaves) | yes, retention 90 d | yes, 400 d |
| `member_daily` (msgs, cmds, first/last activity) | yes, 90 d | yes, 400 d |
| `community_hourly` `sessions_started` (metric 5), `member_daily.presence_s` | no | yes |
| `member_sessions` (presence sessionization) | no | yes |
| `cohort_weekly` | no | yes (recomputed hourly) |

**Grant** (alembic `0131_analytics_applied_entries`, role-guarded, column-level -- nothing else on
that table and no other table): `GRANT SELECT (community_id, tier_code, released_at) ON
public.community_tier_allocations TO analytics_writer`. The pod connects as the LOGIN role
`waddles_svc_analytics` (member of `analytics_writer`, `config/postgres/service-roles.yaml`) and
inherits it. If the grant is missing every community resolves to Free and
`svc_analytics_tier_lookup_failures_total` rises.

## Sessionization (Premium)

`presence_start`/`presence_end` events are applied in event-time order within a batch.
Start inside an open/covering session = duplicate (counted); end with no open session = orphan
(counted, nothing written); an end past `ANALYTICS_SESSION_MAX_SECS` (default 12 h) is clamped.
Open sessions idle beyond the bound are closed by the sweeper at `start + bound`. Presence
seconds are credited to `member_daily.presence_s`, split at UTC midnights. Known limit: an end
event arriving *before* its start across batches is an orphan and the later start is closed by
the sweeper at the bound.

## Maintenance (`src/jobs.rs`, run even when the flag is OFF)

* stale-session sweep and cohort refresh every `ANALYTICS_MAINTENANCE_SECS` (default 1 h);
  cohorts are recomputed (not accumulated) from `member_daily` for the Premium communities seen
  since start. Cohorts older than ~386 days are left as written (their first-activity day may be
  purged).
* **retention purge**: honours each row's `retention_days`, bounded (`ANALYTICS_PURGE_BATCH` rows
  x `ANALYTICS_PURGE_MAX_BATCHES` per table), only inside the off-peak UTC window
  (`ANALYTICS_PURGE_START_HOUR_UTC`..`END`, default 03-05), at most once per UTC day.

## Flag

`waddles.analytics-stream`: **OFF everywhere by default**, including `*.waddles.app`,
`*.penguincloud.io` and `*.penguintech.cloud`. Data collection is an explicit per-environment
opt-in: ENV baseline `FLAG_WADDLES_ANALYTICS_STREAM` (chart `config.analyticsStreamEnabled`) or the
PostHog flag. A license-bypass domain never turns it on (a bypass-active client is ignored for this
flag; no bypass domain is registered) -- identical to the svc-ingest producer, so the two cannot
disagree. OFF => the consumer idles (no group created, nothing read) but health stays SERVING.
Re-checked every loop (live kill-switch).

## Configuration

Chart-provided (see `docs/svc-analytics-deployment.md`): `VALKEY_URL` + `VALKEY_PASSWORD` (the
password is applied to the URL when it carries none), `DATABASE_URL` (LOGIN role
`waddles_svc_analytics`; `DB_*` are ignored), `GRPC_HEALTH_PORT` (default 50209),
`ANALYTICS_STREAM_KEY` (default `waddles:analytics`), `ANALYTICS_CONSUMER_GROUP` (default
`svc_analytics`), `FLAG_WADDLES_ANALYTICS_STREAM`. Secrets are env-only and never logged
(`Config` `Debug` redacts them). Optional knobs and defaults: `src/config.rs`. Telemetry: standard
`OTEL_*` variables. Binary `/app/svc-analytics`; `--healthcheck` for container probes.

Bounded backlog: a poison entry or an exclusion outage is retried at most
`ANALYTICS_MAX_ATTEMPTS` (5) times, then dead-lettered (`dlq_total{reason}`), so the group's
pending list cannot grow without bound; the DLQ stream itself is capped (`ANALYTICS_DLQ_MAXLEN`,
approximate MAXLEN). Alert on `exclusion_lookup_failures_total`, `dlq_total` and `stream_pending`.

## Telemetry (PII-free: never a user token or content)

Metrics `svc_analytics_*`: `events_total{outcome}`, `rejected_total{reason}`,
`dropped_total{reason}`, `dlq_total{reason}`, `exclusion_lookup_failures_total`,
`tier_lookup_failures_total`, `cache_total{result}`, `db_faults_total{kind}`, `sessions_total`,
`purge_rows_total{table}`, `jobs_total`, `batch_duration_seconds` and `batch_size` histograms,
`flag_enabled`, `stream_pending`. Spans `analytics.batch`, `analytics.maintenance`.

## Extension points (not built here)

* **Layer B trend builder**: separate service as role `trend_builder` reading `member_daily`.
* **Bad-actor/bot signals**: owns `analytics_fp.member_signals_daily` (Premium, admin-only,
  profiling); add a producer-side event kind and extend `aggregate::build_batch`.
* **hub-api endpoints / webui / Helm**: later work items.

## Tests

`TEST_DATABASE_URL` (owner credential) and `VALKEY_URL` must be set -- tests fail loudly, never
skip. `cargo test --locked`; coverage gate `cargo llvm-cov --locked --fail-under-lines 90
--ignore-filename-regex 'src/main\.rs'`.
