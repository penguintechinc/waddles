//! Action-stage dispatcher of the automated reputation floor-ban outbox.
//!
//! The process stage records every verified floor ban as a QUEUED row of
//! `bundle_reputation_floor_bans` (`reputation_floor_ban_enqueue`, alembic 0099); this module is
//! the other half. Each sweep it
//!
//! 1. REAPs rows that exhausted their attempts (they become DEAD: ERROR log + metric alert);
//! 2. CLAIMs a bounded batch under a lease token (queued rows, failed / rate-limited rows past
//!    their retry time, and in-flight rows whose lease EXPIRED -- a crashed dispatcher);
//! 3. re-checks the breach in SQL immediately before sending (a recovered score cancels it);
//! 4. emits the signed outbound `mod.ban` command through the existing sender path
//!    ([`crate::capabilities::StageCapabilities::emit_floor_ban`]) and waits for the sender's
//!    CONFIRMED ack;
//! 5. COMPLETEs the row for the lease holder only: `done` on a confirmed ack, otherwise a failure
//!    with a stable code (backoff retry, or `dead` once attempts are exhausted) or a permanent
//!    refusal. A completion with a stale lease is a no-op, so two dispatchers never both record a
//!    result, and a ban is never marked done without the sender's confirmation.
//!
//! A ban is idempotent on the platform, so the at-least-once delivery that a crash between the
//! sender's ack and the completion produces is harmless. Nothing is silently dropped: every
//! non-success path leaves a row state, a log line and a `waddles_floor_ban_dispatch_total` point.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use futures_util::stream::{self, StreamExt};
use sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend, Statement, Value};
use uuid::Uuid;

use crate::capabilities::{FloorBanCommand, FloorSendError, FloorSendFailure};
use crate::stage_metrics::StageMetrics;

/// Rows claimed (leased) per sweep.
pub const CLAIM_BATCH: u32 = 20;
/// Lease length of a claimed row; an in-flight row is reclaimable only after it expires.
pub const LEASE_SECONDS: u32 = 120;
/// Idle pause between sweeps (a full batch re-sweeps immediately).
pub const SWEEP_INTERVAL: Duration = Duration::from_secs(5);
/// Rows delivered concurrently within one sweep.
const SEND_CONCURRENCY: usize = 4;
/// Dead-row reap bound per sweep.
const REAP_BATCH: u32 = 100;

/// A boxed store future (same style as the other trait seams of this crate).
pub type StoreFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T, String>> + Send + 'a>>;

/// One leased outbox row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClaimedFloorBan {
    /// The outbox row id: the correlation id.
    pub id: i64,
    /// Numeric tenant.
    pub tenant_id: i32,
    /// Numeric community the ban runs in.
    pub community_id: i32,
    /// The banned user (UUID token only).
    pub user: Uuid,
    /// SQL scope code (1 community hard, 2 tenant).
    pub scope: i16,
    /// Platform whose `mod.ban` to emit.
    pub platform: String,
    /// This claim's attempt number (1-based).
    pub attempt: i32,
    /// The lease token: only this value may complete the row.
    pub lease: Uuid,
}

/// A row reaped as DEAD (attempts exhausted).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeadFloorBan {
    /// Outbox row id.
    pub id: i64,
    /// Numeric tenant.
    pub tenant_id: i32,
    /// Numeric community.
    pub community_id: i32,
}

/// Result of the pre-send breach re-check.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Recheck {
    /// Still breached: send.
    Breached,
    /// No longer breached: the row was closed as refused; do not send.
    NotBreached,
    /// The lease is not ours any more: do nothing.
    LeaseLost,
}

/// What to record for a leased row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Completion {
    /// The sender confirmed the ban.
    Done,
    /// Failed with a stable code; retried with backoff (dead after the attempt bound).
    Failed(String),
    /// Closed for good with a stable code.
    Refused(String),
}

/// The row status a completion resulted in.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Completed {
    /// Marked done.
    Done,
    /// A retry is scheduled.
    Retrying,
    /// Attempts exhausted: dead.
    Dead,
    /// Closed as refused.
    Refused,
    /// The lease was not ours; nothing changed.
    LeaseLost,
}

/// The outbox the dispatcher reads and writes (SQL functions only; see `PgFloorBanStore`).
pub trait FloorBanStore: Send + Sync {
    /// Marks exhausted rows dead and returns them.
    fn reap(&self, limit: u32) -> StoreFuture<'_, Vec<DeadFloorBan>>;
    /// Leases up to `limit` due rows.
    fn claim(&self, limit: u32, lease_seconds: u32) -> StoreFuture<'_, Vec<ClaimedFloorBan>>;
    /// Re-verifies the breach of a leased row right before sending.
    fn recheck(&self, id: i64, lease: Uuid) -> StoreFuture<'_, Recheck>;
    /// Records the outcome for the lease holder.
    fn complete<'a>(
        &'a self,
        id: i64,
        lease: Uuid,
        outcome: &'a Completion,
        op_id: Option<&'a str>,
    ) -> StoreFuture<'a, Completed>;
}

/// Emits one leased row's outbound command and waits for the sender's confirmation.
pub trait FloorBanSender: Send + Sync {
    /// `Ok(op_id)` ONLY on a confirmed ack.
    fn send<'a>(
        &'a self,
        cmd: &'a FloorBanCommand,
    ) -> Pin<Box<dyn Future<Output = Result<String, FloorSendFailure>> + Send + 'a>>;
}

impl<Q, K> FloorBanSender for crate::capabilities::StageCapabilities<Q, K>
where
    Q: crate::capabilities::RelayQueue + 'static,
    K: bundle_host_kv::KvBackend + 'static,
{
    fn send<'a>(
        &'a self,
        cmd: &'a FloorBanCommand,
    ) -> Pin<Box<dyn Future<Output = Result<String, FloorSendFailure>> + Send + 'a>> {
        Box::pin(self.emit_floor_ban(cmd))
    }
}

/// What one sweep did (counts are the Verification-Integrity denominator).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct SweepReport {
    /// Rows leased this sweep.
    pub claimed: usize,
    /// Rows the sender confirmed.
    pub done: usize,
    /// Rows scheduled for a retry.
    pub failed: usize,
    /// Rows closed as refused (permanent or no longer breached).
    pub refused: usize,
    /// Rows that became dead (reaped or exhausted on this attempt).
    pub dead: usize,
    /// Rows whose lease was lost.
    pub lease_lost: usize,
    /// Outbox/store errors.
    pub store_errors: usize,
}

impl SweepReport {
    fn add(&mut self, other: Self) {
        self.claimed += other.claimed;
        self.done += other.done;
        self.failed += other.failed;
        self.refused += other.refused;
        self.dead += other.dead;
        self.lease_lost += other.lease_lost;
        self.store_errors += other.store_errors;
    }
}

/// Logs and counts one result; the single place the alert (`dead`) fires.
fn note(metrics: &StageMetrics, result: &'static str, row: &ClaimedFloorBan, code: &str) {
    metrics.record_floor_ban_dispatch(result);
    match result {
        "dead" => tracing::error!(
            floor_ban_id = row.id,
            community_id = row.community_id,
            code,
            "ALERT floor ban DEAD: attempts exhausted without a sender confirmation"
        ),
        "failed" => tracing::warn!(
            floor_ban_id = row.id,
            community_id = row.community_id,
            attempt = row.attempt,
            code,
            "floor ban delivery failed; retry scheduled"
        ),
        "refused" => tracing::warn!(
            floor_ban_id = row.id,
            community_id = row.community_id,
            code,
            "floor ban closed as refused"
        ),
        "lease_lost" => tracing::warn!(
            floor_ban_id = row.id,
            "floor ban lease lost; another dispatcher owns the row"
        ),
        _ => tracing::info!(
            floor_ban_id = row.id,
            community_id = row.community_id,
            attempt = row.attempt,
            "floor ban delivered and confirmed by the sender"
        ),
    }
}

/// Records `outcome` for the lease holder and folds the resulting status into a report.
async fn finish<S: FloorBanStore>(
    store: &S,
    metrics: &StageMetrics,
    row: &ClaimedFloorBan,
    outcome: Completion,
    op_id: Option<&str>,
    code: &str,
) -> SweepReport {
    let mut r = SweepReport::default();
    match store.complete(row.id, row.lease, &outcome, op_id).await {
        Ok(Completed::Done) => {
            r.done = 1;
            note(metrics, "done", row, code);
        }
        Ok(Completed::Retrying) => {
            r.failed = 1;
            note(metrics, "failed", row, code);
        }
        Ok(Completed::Dead) => {
            r.dead = 1;
            note(metrics, "dead", row, code);
        }
        Ok(Completed::Refused) => {
            r.refused = 1;
            note(metrics, "refused", row, code);
        }
        Ok(Completed::LeaseLost) => {
            r.lease_lost = 1;
            note(metrics, "lease_lost", row, code);
        }
        Err(e) => {
            r.store_errors = 1;
            metrics.record_floor_ban_dispatch("store_error");
            tracing::error!(
                floor_ban_id = row.id, error = %e,
                "floor ban outcome could not be recorded; the lease expiry will retry it"
            );
        }
    }
    r
}

/// Delivers one leased row end to end.
async fn process_one<S: FloorBanStore, T: FloorBanSender>(
    store: &S,
    sender: &T,
    metrics: &StageMetrics,
    row: ClaimedFloorBan,
) -> SweepReport {
    match store.recheck(row.id, row.lease).await {
        Ok(Recheck::Breached) => {}
        Ok(Recheck::NotBreached) => {
            note(metrics, "refused", &row, "floor_not_breached");
            return SweepReport {
                refused: 1,
                ..SweepReport::default()
            };
        }
        Ok(Recheck::LeaseLost) => {
            note(metrics, "lease_lost", &row, "");
            return SweepReport {
                lease_lost: 1,
                ..SweepReport::default()
            };
        }
        Err(e) => {
            metrics.record_floor_ban_dispatch("store_error");
            tracing::error!(
                floor_ban_id = row.id, error = %e,
                "floor ban re-check failed; the lease expiry will retry it"
            );
            return SweepReport {
                store_errors: 1,
                ..SweepReport::default()
            };
        }
    }
    let cmd = FloorBanCommand {
        id: row.id,
        tenant_id: row.tenant_id,
        community_id: row.community_id,
        user: row.user,
        platform: row.platform.clone(),
        attempt: row.attempt,
    };
    match sender.send(&cmd).await {
        Ok(op_id) => finish(store, metrics, &row, Completion::Done, Some(&op_id), "").await,
        Err(FloorSendFailure {
            error: FloorSendError::Retry(code),
            op_id,
        }) => {
            finish(
                store,
                metrics,
                &row,
                Completion::Failed(code.clone()),
                op_id.as_deref(),
                &code,
            )
            .await
        }
        Err(FloorSendFailure {
            error: FloorSendError::Permanent(code),
            op_id,
        }) => {
            finish(
                store,
                metrics,
                &row,
                Completion::Refused(code.clone()),
                op_id.as_deref(),
                &code,
            )
            .await
        }
    }
}

/// One sweep: reap dead rows, claim a batch, deliver it. Returns the counts.
pub async fn sweep_once<S: FloorBanStore, T: FloorBanSender>(
    store: &S,
    sender: &T,
    metrics: &StageMetrics,
) -> SweepReport {
    let mut report = SweepReport::default();
    match store.reap(REAP_BATCH).await {
        Ok(dead) => {
            for d in dead {
                report.dead += 1;
                metrics.record_floor_ban_dispatch("dead");
                tracing::error!(
                    floor_ban_id = d.id,
                    tenant_id = d.tenant_id,
                    community_id = d.community_id,
                    "ALERT floor ban DEAD: attempts exhausted without a sender confirmation"
                );
            }
        }
        Err(e) => {
            report.store_errors += 1;
            metrics.record_floor_ban_dispatch("store_error");
            tracing::error!(error = %e, "floor ban reap failed");
        }
    }
    let claimed = match store.claim(CLAIM_BATCH, LEASE_SECONDS).await {
        Ok(rows) => rows,
        Err(e) => {
            report.store_errors += 1;
            metrics.record_floor_ban_dispatch("store_error");
            tracing::error!(error = %e, "floor ban claim failed");
            return report;
        }
    };
    report.claimed = claimed.len();
    let results: Vec<SweepReport> = stream::iter(claimed)
        .map(|row| process_one(store, sender, metrics, row))
        .buffer_unordered(SEND_CONCURRENCY)
        .collect()
        .await;
    for r in results {
        report.add(r);
    }
    report
}

/// Runs [`sweep_once`] every [`SWEEP_INTERVAL`] (immediately again after a full batch) until
/// `shutdown` resolves.
pub async fn run_floor_ban_sweeper<S, T>(
    store: Arc<S>,
    sender: Arc<T>,
    metrics: Arc<StageMetrics>,
    shutdown: impl Future<Output = ()>,
) where
    S: FloorBanStore,
    T: FloorBanSender,
{
    tokio::pin!(shutdown);
    loop {
        let report = sweep_once(&*store, &*sender, &metrics).await;
        if report.claimed >= CLAIM_BATCH as usize {
            continue;
        }
        tokio::select! {
            () = &mut shutdown => return,
            () = tokio::time::sleep(SWEEP_INTERVAL) => {}
        }
    }
}

/// The Postgres outbox: every call goes through a SECURITY DEFINER function granted to this
/// service's own role (`waddles_svc_action`); the role has no table access.
pub struct PgFloorBanStore {
    conn: DatabaseConnection,
}

impl PgFloorBanStore {
    /// Wraps this stage's own (primary) database connection.
    #[must_use]
    pub fn new(conn: DatabaseConnection) -> Self {
        Self { conn }
    }
}

fn stmt(sql: &str, values: Vec<Value>) -> Statement {
    Statement::from_sql_and_values(DbBackend::Postgres, sql, values)
}

impl FloorBanStore for PgFloorBanStore {
    fn reap(&self, limit: u32) -> StoreFuture<'_, Vec<DeadFloorBan>> {
        Box::pin(async move {
            let rows = self
                .conn
                .query_all_raw(stmt(
                    "SELECT r_id, r_tenant_id, r_community_id FROM reputation_floor_ban_reap($1::int)",
                    vec![Value::Int(Some(i32::try_from(limit).unwrap_or(100)))],
                ))
                .await
                .map_err(|e| e.to_string())?;
            rows.into_iter()
                .map(|r| {
                    Ok(DeadFloorBan {
                        id: r.try_get("", "r_id").map_err(|e| e.to_string())?,
                        tenant_id: r.try_get("", "r_tenant_id").map_err(|e| e.to_string())?,
                        community_id: r.try_get("", "r_community_id").map_err(|e| e.to_string())?,
                    })
                })
                .collect()
        })
    }

    fn claim(&self, limit: u32, lease_seconds: u32) -> StoreFuture<'_, Vec<ClaimedFloorBan>> {
        Box::pin(async move {
            let rows = self
                .conn
                .query_all_raw(stmt(
                    "SELECT r_id, r_tenant_id, r_community_id, r_user_uuid, r_scope, r_platform, \
                     r_attempt, r_lease_token FROM reputation_floor_ban_claim($1::int, $2::int)",
                    vec![
                        Value::Int(Some(i32::try_from(limit).unwrap_or(20))),
                        Value::Int(Some(i32::try_from(lease_seconds).unwrap_or(120))),
                    ],
                ))
                .await
                .map_err(|e| e.to_string())?;
            let err = |e: sea_orm::DbErr| e.to_string();
            rows.into_iter()
                .map(|r| {
                    Ok(ClaimedFloorBan {
                        id: r.try_get("", "r_id").map_err(err)?,
                        tenant_id: r.try_get("", "r_tenant_id").map_err(err)?,
                        community_id: r.try_get("", "r_community_id").map_err(err)?,
                        user: r.try_get("", "r_user_uuid").map_err(err)?,
                        scope: r.try_get("", "r_scope").map_err(err)?,
                        platform: r.try_get("", "r_platform").map_err(err)?,
                        attempt: r.try_get("", "r_attempt").map_err(err)?,
                        lease: r.try_get("", "r_lease_token").map_err(err)?,
                    })
                })
                .collect()
        })
    }

    fn recheck(&self, id: i64, lease: Uuid) -> StoreFuture<'_, Recheck> {
        Box::pin(async move {
            let row = self
                .conn
                .query_one_raw(stmt(
                    "SELECT reputation_floor_ban_recheck($1::bigint, $2::uuid) AS state",
                    vec![Value::BigInt(Some(id)), Value::Uuid(Some(lease))],
                ))
                .await
                .map_err(|e| e.to_string())?
                .ok_or_else(|| "reputation_floor_ban_recheck returned no row".to_string())?;
            match row.try_get::<i16>("", "state").map_err(|e| e.to_string())? {
                0 => Ok(Recheck::Breached),
                1 => Ok(Recheck::NotBreached),
                2 => Ok(Recheck::LeaseLost),
                other => Err(format!("unexpected floor-ban recheck state {other}")),
            }
        })
    }

    fn complete<'a>(
        &'a self,
        id: i64,
        lease: Uuid,
        outcome: &'a Completion,
        op_id: Option<&'a str>,
    ) -> StoreFuture<'a, Completed> {
        Box::pin(async move {
            let (code, error) = match outcome {
                Completion::Done => (1_i16, ""),
                Completion::Failed(c) => (2_i16, c.as_str()),
                Completion::Refused(c) => (3_i16, c.as_str()),
            };
            let row = self
                .conn
                .query_one_raw(stmt(
                    "SELECT reputation_floor_ban_complete($1::bigint, $2::uuid, $3::smallint, \
                     $4::text, $5::text) AS status",
                    vec![
                        Value::BigInt(Some(id)),
                        Value::Uuid(Some(lease)),
                        Value::SmallInt(Some(code)),
                        Value::String(Some(error.to_string())),
                        Value::String(op_id.map(str::to_string)),
                    ],
                ))
                .await
                .map_err(|e| e.to_string())?
                .ok_or_else(|| "reputation_floor_ban_complete returned no row".to_string())?;
            match row
                .try_get::<i16>("", "status")
                .map_err(|e| e.to_string())?
            {
                0 => Ok(Completed::LeaseLost),
                2 => Ok(Completed::Done),
                3 => Ok(Completed::Retrying),
                4 => Ok(Completed::Dead),
                6 => Ok(Completed::Refused),
                other => Err(format!("unexpected floor-ban completion status {other}")),
            }
        })
    }
}

#[cfg(test)]
#[path = "floor_ban_dispatch_tests.rs"]
mod floor_ban_dispatch_tests;
