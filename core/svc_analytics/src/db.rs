//! Database port + the Postgres implementation. All SQL here is parameterized; values
//! are bound, never interpolated. The service connects as the `analytics_writer` role:
//! DML on `analytics_fp.*`, SELECT on `public.data_use_exclusions`, and (see
//! `docs/analytics-foundation.md`, "As built") a narrow column SELECT on
//! `public.community_tier_allocations` that must be granted for Premium detection.

use std::collections::HashSet;
use std::future::Future;
use std::time::Duration;

use sea_orm::{
    ConnectOptions, ConnectionTrait, Database, DatabaseConnection, DatabaseTransaction, DbBackend,
    DbErr, RuntimeErr, Statement, TransactionTrait, Value,
};
use uuid::Uuid;

use crate::aggregate::{build_batch, split_by_day, Accepted, EntryId, Presence};
use crate::config::PREMIUM_RETENTION_DAYS;

/// How a database failure should be handled.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultKind {
    /// Outage/lock/privilege problem: keep the entries pending and retry with backoff.
    Transient,
    /// The data itself is rejected (SQLSTATE class 22/23): isolate and dead-letter.
    Data,
}

/// A classified, PII-free database failure (`detail` is a SQLSTATE or error class, never SQL
/// text or values).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("database fault ({kind:?}): {detail}")]
pub struct DbFault {
    /// Handling class.
    pub kind: FaultKind,
    /// SQLSTATE / variant name.
    pub detail: String,
}

impl DbFault {
    /// A transient fault.
    pub fn transient(detail: impl Into<String>) -> Self {
        Self {
            kind: FaultKind::Transient,
            detail: detail.into(),
        }
    }
    /// A data fault.
    pub fn data(detail: impl Into<String>) -> Self {
        Self {
            kind: FaultKind::Data,
            detail: detail.into(),
        }
    }
}

fn sqlstate(e: &DbErr) -> Option<String> {
    match e {
        DbErr::Exec(RuntimeErr::SqlxError(x)) | DbErr::Query(RuntimeErr::SqlxError(x)) => x
            .as_database_error()
            .and_then(|d| d.code())
            .map(|c| c.to_string()),
        _ => None,
    }
}

/// Maps a driver error to a [`DbFault`].
pub fn classify(e: &DbErr) -> DbFault {
    match sqlstate(e) {
        Some(c) if c.starts_with("22") || c.starts_with("23") => DbFault::data(c),
        Some(c) => DbFault::transient(c),
        None => DbFault::transient("driver"),
    }
}

/// Counters describing one applied batch.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct ApplyReport {
    /// Events whose deltas were written.
    pub applied: u64,
    /// Events skipped because the entry id was already applied (replay).
    pub duplicates: u64,
    /// Events dropped by the in-transaction exclusion re-check.
    pub late_excluded: u64,
    /// New sessions opened.
    pub sessions_opened: u64,
    /// Sessions closed by an end event.
    pub sessions_closed: u64,
    /// Start events inside an already-open/covering session.
    pub duplicate_starts: u64,
    /// End events with no matching session.
    pub orphan_ends: u64,
}

/// Rows deleted per table by one purge run.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct PurgeReport {
    /// `(table, rows deleted)`.
    pub tables: Vec<(&'static str, u64)>,
}

/// Storage port used by the consumer, maintenance and purge jobs.
pub trait AnalyticsDb: Send + Sync + 'static {
    /// Loads up to `cap` excluded user tokens (cache seed).
    fn load_exclusions(
        &self,
        cap: usize,
    ) -> impl Future<Output = Result<Vec<Uuid>, DbFault>> + Send;
    /// Returns the subset of `ids` present in `data_use_exclusions` (any reason).
    fn lookup_exclusions(
        &self,
        ids: &[Uuid],
    ) -> impl Future<Output = Result<Vec<Uuid>, DbFault>> + Send;
    /// Returns the subset of `ids` that are currently Premium communities.
    fn premium_communities(
        &self,
        ids: &[i32],
    ) -> impl Future<Output = Result<Vec<i32>, DbFault>> + Send;
    /// Idempotently applies `batch` in ONE transaction.
    fn apply(
        &self,
        batch: &[Accepted],
    ) -> impl Future<Output = Result<ApplyReport, DbFault>> + Send;
    /// Closes open sessions older than `max_s`; returns how many.
    fn close_stale_sessions(
        &self,
        now_s: i64,
        max_s: i64,
        limit: i64,
    ) -> impl Future<Output = Result<u64, DbFault>> + Send;
    /// Recomputes `cohort_weekly` for Premium `communities`; returns rows upserted.
    fn refresh_cohorts(
        &self,
        communities: &[i32],
        now_s: i64,
    ) -> impl Future<Output = Result<u64, DbFault>> + Send;
    /// Deletes expired rows in bounded batches.
    fn purge(
        &self,
        now_s: i64,
        batch: i64,
        max_batches: u32,
    ) -> impl Future<Output = Result<PurgeReport, DbFault>> + Send;
}

/// Postgres-backed [`AnalyticsDb`].
#[derive(Clone)]
pub struct PgDb {
    conn: DatabaseConnection,
}

fn stmt(sql: &str, values: Vec<Value>) -> Statement {
    Statement::from_sql_and_values(DbBackend::Postgres, sql, values)
}

fn uuid_strings<'a>(ids: impl IntoIterator<Item = &'a Uuid>) -> Vec<String> {
    ids.into_iter().map(Uuid::to_string).collect()
}

impl PgDb {
    /// Connects with a small bounded pool.
    ///
    /// # Errors
    /// Connection failure.
    pub async fn connect(url: &str) -> Result<Self, DbFault> {
        let mut o = ConnectOptions::new(url.to_string());
        o.max_connections(5)
            .min_connections(1)
            .connect_timeout(Duration::from_secs(10))
            .acquire_timeout(Duration::from_secs(10))
            .sqlx_logging(false);
        let conn = Database::connect(o).await.map_err(|e| classify(&e))?;
        Ok(Self { conn })
    }

    /// Wraps an existing connection (tests).
    pub fn from_connection(conn: DatabaseConnection) -> Self {
        Self { conn }
    }

    /// Cheap liveness probe (`SELECT 1`).
    ///
    /// # Errors
    /// Any database failure.
    pub async fn ping(&self) -> Result<(), DbFault> {
        self.conn
            .query_one_raw(stmt("SELECT 1", vec![]))
            .await
            .map(|_| ())
            .map_err(|e| classify(&e))
    }
}

async fn first_i64(txn: &impl ConnectionTrait, s: Statement) -> Result<Option<i64>, DbErr> {
    Ok(match txn.query_one_raw(s).await? {
        Some(r) => Some(r.try_get_by_index::<i64>(0)?),
        None => None,
    })
}

/// Adds `[start_s, end_s]` presence seconds to `member_daily`, split at UTC midnights.
async fn credit_presence(
    txn: &DatabaseTransaction,
    p: &Presence,
    start_s: i64,
    end_s: i64,
) -> Result<(), DbErr> {
    for (day, secs) in split_by_day(start_s, end_s) {
        txn.execute_raw(stmt(
            "INSERT INTO analytics_fp.member_daily \
               (community_id, user_uuid, day, presence_s, retention_days, provenance_code) \
             VALUES ($1::int, $2::uuid, DATE '1970-01-01' + $3::int, $4::int, $5::int, $6::smallint) \
             ON CONFLICT (community_id, user_uuid, day) DO UPDATE SET \
               presence_s = analytics_fp.member_daily.presence_s + EXCLUDED.presence_s, \
               retention_days = GREATEST(analytics_fp.member_daily.retention_days, EXCLUDED.retention_days)",
            vec![
                i64::from(p.community_id).into(),
                p.user_uuid.to_string().into(),
                i64::from(day).into(),
                i64::from(secs).into(),
                i64::from(p.retention).into(),
                i64::from(p.provenance).into(),
            ],
        ))
        .await?;
    }
    Ok(())
}

enum Outcome {
    Opened,
    Closed,
    DuplicateStart,
    OrphanEnd,
    Duplicate,
}

const SESSION_KEY: &str =
    "community_id = $1::int AND user_uuid = $2::uuid AND platform_code = $3::smallint";

async fn presence_start(
    txn: &DatabaseTransaction,
    p: &Presence,
    max_s: i64,
) -> Result<Outcome, DbErr> {
    let keyvals = |extra: i64| -> Vec<Value> {
        vec![
            i64::from(p.community_id).into(),
            p.user_uuid.to_string().into(),
            i64::from(p.platform_code).into(),
            extra.into(),
        ]
    };
    // A previous open session that has been idle past the bound is closed at the bound.
    let stale = txn
        .query_all_raw(stmt(
            &format!(
                "SELECT EXTRACT(EPOCH FROM session_start)::bigint FROM analytics_fp.member_sessions \
                 WHERE {SESSION_KEY} AND session_end IS NULL AND session_start < to_timestamp($4) - make_interval(secs => {max_s}) \
                 FOR UPDATE"
            ),
            keyvals(p.ts_s),
        ))
        .await?;
    for row in stale {
        let s: i64 = row.try_get_by_index(0)?;
        let end = s + max_s;
        txn.execute_raw(stmt(
            &format!(
                "UPDATE analytics_fp.member_sessions SET session_end = to_timestamp($4) \
                 WHERE {SESSION_KEY} AND session_start = to_timestamp($5)"
            ),
            vec![
                i64::from(p.community_id).into(),
                p.user_uuid.to_string().into(),
                i64::from(p.platform_code).into(),
                end.into(),
                s.into(),
            ],
        ))
        .await?;
        credit_presence(txn, p, s, end).await?;
    }
    let covered = first_i64(
        txn,
        stmt(
            &format!(
                "SELECT 1::bigint FROM analytics_fp.member_sessions WHERE {SESSION_KEY} \
                 AND session_start <= to_timestamp($4) AND (session_end IS NULL OR session_end >= to_timestamp($4)) LIMIT 1"
            ),
            keyvals(p.ts_s),
        ),
    )
    .await?;
    if covered.is_some() {
        return Ok(Outcome::DuplicateStart);
    }
    txn.execute_raw(stmt(
        "INSERT INTO analytics_fp.member_sessions \
           (community_id, user_uuid, session_start, session_end, platform_code, retention_days, provenance_code) \
         VALUES ($1::int, $2::uuid, to_timestamp($4), NULL, $3::smallint, $5::int, $6::smallint) \
         ON CONFLICT DO NOTHING",
        vec![
            i64::from(p.community_id).into(),
            p.user_uuid.to_string().into(),
            i64::from(p.platform_code).into(),
            p.ts_s.into(),
            i64::from(p.retention).into(),
            i64::from(p.provenance).into(),
        ],
    ))
    .await?;
    Ok(Outcome::Opened)
}

async fn presence_end(
    txn: &DatabaseTransaction,
    p: &Presence,
    max_s: i64,
) -> Result<Outcome, DbErr> {
    let keyvals = |extra: i64| -> Vec<Value> {
        vec![
            i64::from(p.community_id).into(),
            p.user_uuid.to_string().into(),
            i64::from(p.platform_code).into(),
            extra.into(),
        ]
    };
    let open = first_i64(
        txn,
        stmt(
            &format!(
                "SELECT EXTRACT(EPOCH FROM session_start)::bigint FROM analytics_fp.member_sessions \
                 WHERE {SESSION_KEY} AND session_end IS NULL AND session_start <= to_timestamp($4) \
                 ORDER BY session_start DESC LIMIT 1 FOR UPDATE"
            ),
            keyvals(p.ts_s),
        ),
    )
    .await?;
    if let Some(s) = open {
        let end = p.ts_s.min(s + max_s);
        txn.execute_raw(stmt(
            &format!(
                "UPDATE analytics_fp.member_sessions SET session_end = to_timestamp($4) \
                 WHERE {SESSION_KEY} AND session_start = to_timestamp($5)"
            ),
            vec![
                i64::from(p.community_id).into(),
                p.user_uuid.to_string().into(),
                i64::from(p.platform_code).into(),
                end.into(),
                s.into(),
            ],
        ))
        .await?;
        credit_presence(txn, p, s, end).await?;
        return Ok(Outcome::Closed);
    }
    let closed = first_i64(
        txn,
        stmt(
            &format!(
                "SELECT 1::bigint FROM analytics_fp.member_sessions WHERE {SESSION_KEY} \
                 AND session_end IS NOT NULL AND session_start <= to_timestamp($4) AND session_end >= to_timestamp($4) LIMIT 1"
            ),
            keyvals(p.ts_s),
        ),
    )
    .await?;
    Ok(if closed.is_some() {
        Outcome::Duplicate
    } else {
        Outcome::OrphanEnd
    })
}

impl PgStore {
    async fn apply_inner(&self, batch: &[Accepted], max_s: i64) -> Result<ApplyReport, DbErr> {
        let mut rep = ApplyReport::default();
        let txn = self.db.conn.begin().await?;
        // 1. Idempotency: only entries inserted here are applied; replays are no-ops.
        let ms: Vec<i64> = batch.iter().map(|a| a.entry.ms).collect();
        let seq: Vec<i64> = batch.iter().map(|a| a.entry.seq).collect();
        let fresh_rows = txn
            .query_all_raw(stmt(
                "INSERT INTO analytics_ops.applied_entries (entry_ms, entry_seq) \
                 SELECT * FROM unnest($1::bigint[], $2::bigint[]) ON CONFLICT DO NOTHING \
                 RETURNING entry_ms, entry_seq",
                vec![ms.into(), seq.into()],
            ))
            .await?;
        let mut fresh_ids: HashSet<EntryId> = HashSet::new();
        for r in fresh_rows {
            fresh_ids.insert(EntryId {
                ms: r.try_get_by_index(0)?,
                seq: r.try_get_by_index(1)?,
            });
        }
        let fresh: Vec<&Accepted> = batch
            .iter()
            .filter(|a| fresh_ids.contains(&a.entry))
            .collect();
        rep.duplicates = (batch.len() - fresh.len()) as u64;
        // 2. Authoritative exclusion re-check inside the write transaction.
        let users: HashSet<Uuid> = fresh.iter().map(|a| a.event.user_uuid).collect();
        let excluded: HashSet<String> = if users.is_empty() {
            HashSet::new()
        } else {
            txn.query_all_raw(stmt(
                "SELECT DISTINCT user_uuid::text FROM public.data_use_exclusions \
                 WHERE user_uuid = ANY($1::text[]::uuid[])",
                vec![uuid_strings(&users).into()],
            ))
            .await?
            .into_iter()
            .map(|r| r.try_get_by_index::<String>(0))
            .collect::<Result<_, _>>()?
        };
        let before = fresh.len();
        let fresh: Vec<&Accepted> = fresh
            .into_iter()
            .filter(|a| !excluded.contains(&a.event.user_uuid.to_string()))
            .collect();
        rep.late_excluded = (before - fresh.len()) as u64;
        rep.applied = fresh.len() as u64;
        let wb = build_batch(&fresh);

        if !wb.hourly.is_empty() {
            let (mut t, mut c, mut h, mut m, mut p, mut v, mut r, mut pr) = (
                vec![],
                vec![],
                vec![],
                vec![],
                vec![],
                vec![],
                vec![],
                vec![],
            );
            for (k, d) in &wb.hourly {
                t.push(i64::from(k.0));
                c.push(i64::from(k.1));
                h.push(k.2);
                m.push(i64::from(k.3));
                p.push(i64::from(k.4));
                v.push(d.value);
                r.push(i64::from(d.retention));
                pr.push(i64::from(d.provenance));
            }
            txn.execute_raw(stmt(
                "INSERT INTO analytics_fp.community_hourly \
                   (tenant_id, community_id, hour_ts, metric_code, platform_code, value, retention_days, provenance_code) \
                 SELECT t::int, c::int, to_timestamp(h), m::smallint, p::smallint, v, r::int, pr::smallint \
                   FROM unnest($1::bigint[], $2::bigint[], $3::bigint[], $4::bigint[], $5::bigint[], $6::bigint[], $7::bigint[], $8::bigint[]) \
                        AS x(t, c, h, m, p, v, r, pr) \
                 ON CONFLICT (community_id, hour_ts, metric_code, platform_code) DO UPDATE SET \
                   value = analytics_fp.community_hourly.value + EXCLUDED.value, \
                   retention_days = GREATEST(analytics_fp.community_hourly.retention_days, EXCLUDED.retention_days)",
                vec![t.into(), c.into(), h.into(), m.into(), p.into(), v.into(), r.into(), pr.into()],
            ))
            .await?;
        }
        if !wb.daily.is_empty() {
            let (mut c, mut u, mut d, mut ms_, mut cm, mut f, mut l, mut r, mut pr) = (
                vec![],
                vec![],
                vec![],
                vec![],
                vec![],
                vec![],
                vec![],
                vec![],
                vec![],
            );
            for (k, x) in &wb.daily {
                c.push(i64::from(k.0));
                u.push(k.1.to_string());
                d.push(i64::from(k.2));
                ms_.push(i64::from(x.msgs));
                cm.push(i64::from(x.cmds));
                f.push(x.first_ms);
                l.push(x.last_ms);
                r.push(i64::from(x.retention));
                pr.push(i64::from(x.provenance));
            }
            txn.execute_raw(stmt(
                "INSERT INTO analytics_fp.member_daily \
                   (community_id, user_uuid, day, msgs, cmds, first_ts, last_ts, retention_days, provenance_code) \
                 SELECT c::int, u::uuid, DATE '1970-01-01' + d::int, ms, cm, to_timestamp(f / 1000.0), to_timestamp(l / 1000.0), r::int, pr::smallint \
                   FROM unnest($1::bigint[], $2::text[], $3::bigint[], $4::bigint[], $5::bigint[], $6::bigint[], $7::bigint[], $8::bigint[], $9::bigint[]) \
                        AS x(c, u, d, ms, cm, f, l, r, pr) \
                 ON CONFLICT (community_id, user_uuid, day) DO UPDATE SET \
                   msgs = analytics_fp.member_daily.msgs + EXCLUDED.msgs, \
                   cmds = analytics_fp.member_daily.cmds + EXCLUDED.cmds, \
                   first_ts = LEAST(analytics_fp.member_daily.first_ts, EXCLUDED.first_ts), \
                   last_ts = GREATEST(analytics_fp.member_daily.last_ts, EXCLUDED.last_ts), \
                   retention_days = GREATEST(analytics_fp.member_daily.retention_days, EXCLUDED.retention_days)",
                vec![c.into(), u.into(), d.into(), ms_.into(), cm.into(), f.into(), l.into(), r.into(), pr.into()],
            ))
            .await?;
        }
        for p in &wb.presence {
            let o = if p.start {
                presence_start(&txn, p, max_s).await?
            } else {
                presence_end(&txn, p, max_s).await?
            };
            match o {
                Outcome::Opened => rep.sessions_opened += 1,
                Outcome::Closed => rep.sessions_closed += 1,
                Outcome::DuplicateStart | Outcome::Duplicate => rep.duplicate_starts += 1,
                Outcome::OrphanEnd => rep.orphan_ends += 1,
            }
        }
        txn.commit().await?;
        Ok(rep)
    }
}

/// Postgres [`AnalyticsDb`] bundling the connection with its session bound.
#[derive(Clone)]
pub struct PgStore {
    /// Connection.
    pub db: PgDb,
    /// Session bound (seconds).
    pub session_max_secs: i64,
}

impl AnalyticsDb for PgStore {
    async fn load_exclusions(&self, cap: usize) -> Result<Vec<Uuid>, DbFault> {
        let rows = self
            .db
            .conn
            .query_all_raw(stmt(
                "SELECT DISTINCT user_uuid::text FROM public.data_use_exclusions LIMIT $1",
                vec![(cap as i64).into()],
            ))
            .await
            .map_err(|e| classify(&e))?;
        rows.into_iter()
            .map(|r| {
                let s: String = r.try_get_by_index(0).map_err(|e| classify(&e))?;
                Uuid::try_parse(&s).map_err(|_| DbFault::data("uuid"))
            })
            .collect()
    }

    async fn lookup_exclusions(&self, ids: &[Uuid]) -> Result<Vec<Uuid>, DbFault> {
        if ids.is_empty() {
            return Ok(vec![]);
        }
        let rows = self
            .db
            .conn
            .query_all_raw(stmt(
                "SELECT DISTINCT user_uuid::text FROM public.data_use_exclusions \
                 WHERE user_uuid = ANY($1::text[]::uuid[])",
                vec![uuid_strings(ids).into()],
            ))
            .await
            .map_err(|e| classify(&e))?;
        rows.into_iter()
            .map(|r| {
                let s: String = r.try_get_by_index(0).map_err(|e| classify(&e))?;
                Uuid::try_parse(&s).map_err(|_| DbFault::data("uuid"))
            })
            .collect()
    }

    async fn premium_communities(&self, ids: &[i32]) -> Result<Vec<i32>, DbFault> {
        if ids.is_empty() {
            return Ok(vec![]);
        }
        let v: Vec<i64> = ids.iter().map(|i| i64::from(*i)).collect();
        let rows = self
            .db
            .conn
            .query_all_raw(stmt(
                "SELECT DISTINCT community_id::bigint FROM public.community_tier_allocations \
                 WHERE community_id = ANY($1::bigint[]::int[]) AND released_at IS NULL AND tier_code = 2",
                vec![v.into()],
            ))
            .await
            .map_err(|e| classify(&e))?;
        rows.into_iter()
            .map(|r| {
                r.try_get_by_index::<i64>(0)
                    .map(|n| n as i32)
                    .map_err(|e| classify(&e))
            })
            .collect()
    }

    async fn apply(&self, batch: &[Accepted]) -> Result<ApplyReport, DbFault> {
        self.apply_inner(batch, self.session_max_secs)
            .await
            .map_err(|e| classify(&e))
    }

    async fn close_stale_sessions(
        &self,
        now_s: i64,
        max_s: i64,
        limit: i64,
    ) -> Result<u64, DbFault> {
        let inner = async {
            let txn = self.db.conn.begin().await?;
            let rows = txn
                .query_all_raw(stmt(
                    "SELECT community_id::bigint, user_uuid::text, EXTRACT(EPOCH FROM session_start)::bigint, \
                            platform_code::bigint, retention_days::bigint, provenance_code::bigint \
                       FROM analytics_fp.member_sessions \
                      WHERE session_end IS NULL AND session_start < to_timestamp($1) - make_interval(secs => $2::int) \
                      ORDER BY session_start LIMIT $3 FOR UPDATE SKIP LOCKED",
                    vec![now_s.into(), max_s.into(), limit.into()],
                ))
                .await?;
            let mut closed = 0u64;
            for r in rows {
                let user: String = r.try_get_by_index(1)?;
                let p = Presence {
                    community_id: r.try_get_by_index::<i64>(0)? as i32,
                    user_uuid: Uuid::try_parse(&user).map_err(|_| DbErr::Custom("uuid".into()))?,
                    platform_code: r.try_get_by_index::<i64>(3)? as i16,
                    ts_s: now_s,
                    start: false,
                    retention: r.try_get_by_index::<i64>(4)? as i32,
                    provenance: r.try_get_by_index::<i64>(5)? as i16,
                };
                let s: i64 = r.try_get_by_index(2)?;
                let end = s + max_s;
                txn.execute_raw(stmt(
                    &format!(
                        "UPDATE analytics_fp.member_sessions SET session_end = to_timestamp($4) \
                         WHERE {SESSION_KEY} AND session_start = to_timestamp($5)"
                    ),
                    vec![
                        i64::from(p.community_id).into(),
                        p.user_uuid.to_string().into(),
                        i64::from(p.platform_code).into(),
                        end.into(),
                        s.into(),
                    ],
                ))
                .await?;
                credit_presence(&txn, &p, s, end).await?;
                closed += 1;
            }
            txn.commit().await?;
            Ok::<u64, DbErr>(closed)
        };
        inner.await.map_err(|e| classify(&e))
    }

    async fn refresh_cohorts(&self, communities: &[i32], now_s: i64) -> Result<u64, DbFault> {
        if communities.is_empty() {
            return Ok(0);
        }
        let ids: Vec<i64> = communities.iter().map(|i| i64::from(*i)).collect();
        // Cohorts older than the retention horizon are left as written: their members' first
        // activity may already be purged, so recomputing them would misattribute members.
        let horizon_days = i64::from(PREMIUM_RETENTION_DAYS) - 14;
        self.db
            .conn
            .execute_raw(stmt(
                "WITH first_seen AS ( \
                   SELECT community_id, user_uuid, MIN(day) AS first_day \
                     FROM analytics_fp.member_daily WHERE community_id = ANY($1::bigint[]::int[]) GROUP BY 1, 2), \
                 cohort AS (SELECT community_id, user_uuid, date_trunc('week', first_day)::date AS cohort_week FROM first_seen), \
                 act AS (SELECT DISTINCT community_id, user_uuid, date_trunc('week', day)::date AS wk \
                           FROM analytics_fp.member_daily WHERE community_id = ANY($1::bigint[]::int[])), \
                 sizes AS (SELECT community_id, cohort_week, COUNT(*) AS size FROM cohort GROUP BY 1, 2), \
                 joined AS (SELECT c.community_id, c.cohort_week, (a.wk - c.cohort_week) / 7 AS week_offset, COUNT(*) AS active \
                              FROM cohort c JOIN act a USING (community_id, user_uuid) \
                             WHERE a.wk >= c.cohort_week GROUP BY 1, 2, 3) \
                 INSERT INTO analytics_fp.cohort_weekly \
                   (community_id, cohort_week, week_offset, active_members, cohort_size, retention_days) \
                 SELECT j.community_id, j.cohort_week, j.week_offset::smallint, j.active::int, s.size::int, $3::int \
                   FROM joined j JOIN sizes s USING (community_id, cohort_week) \
                  WHERE j.cohort_week >= ((to_timestamp($2) AT TIME ZONE 'UTC')::date - $4::int) \
                 ON CONFLICT (community_id, cohort_week, week_offset) DO UPDATE SET \
                   active_members = EXCLUDED.active_members, cohort_size = EXCLUDED.cohort_size",
                vec![
                    ids.into(),
                    now_s.into(),
                    i64::from(PREMIUM_RETENTION_DAYS).into(),
                    horizon_days.into(),
                ],
            ))
            .await
            .map(|r| r.rows_affected())
            .map_err(|e| classify(&e))
    }

    async fn purge(
        &self,
        now_s: i64,
        batch: i64,
        max_batches: u32,
    ) -> Result<PurgeReport, DbFault> {
        // (table, expiry predicate over the row's own retention_days)
        const TABLES: [(&str, &str); 6] = [
            (
                "analytics_fp.community_hourly",
                "hour_ts + retention_days * interval '1 day' < to_timestamp($1)",
            ),
            (
                "analytics_fp.member_daily",
                "day + retention_days < (to_timestamp($1) AT TIME ZONE 'UTC')::date",
            ),
            (
                "analytics_fp.member_sessions",
                "session_start + retention_days * interval '1 day' < to_timestamp($1)",
            ),
            (
                "analytics_fp.cohort_weekly",
                "cohort_week + retention_days < (to_timestamp($1) AT TIME ZONE 'UTC')::date",
            ),
            (
                "analytics_fp.member_signals_daily",
                "day + retention_days < (to_timestamp($1) AT TIME ZONE 'UTC')::date",
            ),
            (
                "analytics_ops.applied_entries",
                "applied_at < to_timestamp($1) - interval '7 days'",
            ),
        ];
        let mut report = PurgeReport::default();
        for (table, pred) in TABLES {
            let sql = format!(
                "DELETE FROM {table} WHERE ctid = ANY(ARRAY(SELECT ctid FROM {table} WHERE {pred} LIMIT $2))"
            );
            let mut total = 0u64;
            for _ in 0..max_batches {
                let n = self
                    .db
                    .conn
                    .execute_raw(stmt(&sql, vec![now_s.into(), batch.into()]))
                    .await
                    .map_err(|e| classify(&e))?
                    .rows_affected();
                total += n;
                if (n as i64) < batch {
                    break;
                }
            }
            report.tables.push((table, total));
        }
        Ok(report)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_non_database_error_is_transient() {
        let f = classify(&DbErr::Custom("x".into()));
        assert_eq!(f, DbFault::transient("driver"));
        assert_eq!(DbFault::data("23505").kind, FaultKind::Data);
        assert!(DbFault::data("23505").to_string().contains("23505"));
    }
}
