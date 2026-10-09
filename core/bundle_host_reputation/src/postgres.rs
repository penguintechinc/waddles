//! Postgres-backed [`ReputationStore`], the membership-snapshot loader and
//! the connection factory (all under the least-privilege
//! `waddles_bundle_reputation` role, alembic 0043).

use std::sync::Arc;
use std::time::{Duration, Instant};

use bundle_capability_gate::{MemberRow, SnapshotMembership};
use sea_orm::{
    ConnectOptions, ConnectionTrait, Database, DatabaseConnection, DbBackend, DbErr, Statement,
    TransactionTrait, Value,
};
use uuid::Uuid;

use crate::{
    metrics, validate_reason, BoxFuture, ReputationError, ReputationScope, ReputationStore,
};

/// Per-transaction statement timeout, so a stuck row lock can never hold a
/// bundle invocation (or a pool connection) past the host call deadline.
const STATEMENT_TIMEOUT_MS: u32 = 5_000;

/// Active-membership predicate shared by every query: the target must be an
/// active (not left, not removed) member of exactly this tenant's community.
/// Bind order is always `$1 = tenant_id`, `$2 = community_id`, `$3 = user`.
const MEMBER_PREDICATE: &str = "cm.community_id = $2 AND c.tenant_id = $1 AND cm.user_uuid = $3 \
     AND COALESCE(cm.is_active, TRUE) AND cm.removed_at IS NULL AND cm.left_at IS NULL";

/// Non-secret connection settings (the password is passed separately to
/// [`connect`] -- Token & Secret Hygiene).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConnectConfig {
    pub host: String,
    pub port: u16,
    pub name: String,
    /// `waddles_bundle_reputation` in every real deployment.
    pub user: String,
}

/// Opens the pooled read-write connection for the reputation store.
/// `sqlx_logging(false)` -- never echo the password-bearing URL.
pub async fn connect(cfg: &ConnectConfig, password: &str) -> Result<DatabaseConnection, DbErr> {
    let url = format!(
        "postgres://{user}:{password}@{host}:{port}/{name}",
        user = cfg.user,
        host = cfg.host,
        port = cfg.port,
        name = cfg.name,
    );
    let mut opts = ConnectOptions::new(url);
    opts.max_connections(8)
        .min_connections(1)
        .sqlx_logging(false);
    Database::connect(opts).await
}

fn backend_err(e: impl std::fmt::Display) -> ReputationError {
    ReputationError::Backend(e.to_string())
}

fn stmt(sql: &str, values: Vec<Value>) -> Statement {
    Statement::from_sql_and_values(DbBackend::Postgres, sql, values)
}

fn scope_values(scope: &ReputationScope, user: Uuid) -> Vec<Value> {
    vec![
        Value::Int(Some(scope.tenant_id)),
        Value::Int(Some(scope.community_id)),
        Value::Uuid(Some(user)),
    ]
}

fn outcome_of<T>(r: &Result<T, ReputationError>) -> &'static str {
    match r {
        Ok(_) => "ok",
        Err(e) => e.wire_code(),
    }
}

/// The production [`ReputationStore`].
pub struct PostgresReputationStore {
    conn: DatabaseConnection,
}

impl PostgresReputationStore {
    pub fn new(conn: DatabaseConnection) -> Self {
        Self { conn }
    }

    async fn get_impl(&self, scope: &ReputationScope, user: Uuid) -> Result<i64, ReputationError> {
        let sql = format!(
            "SELECT COALESCE(s.balance, 0)::BIGINT AS balance \
             FROM community_members cm \
             JOIN communities c ON c.id = cm.community_id \
             LEFT JOIN bundle_reputation_scores s \
               ON s.community_id = cm.community_id AND s.user_uuid = cm.user_uuid \
             WHERE {MEMBER_PREDICATE}"
        );
        let row = self
            .conn
            .query_one_raw(stmt(&sql, scope_values(scope, user)))
            .await
            .map_err(backend_err)?
            .ok_or(ReputationError::NotAMember)?;
        row.try_get::<i64>("", "balance").map_err(backend_err)
    }

    async fn adjust_impl(
        &self,
        scope: &ReputationScope,
        user: Uuid,
        delta: i32,
        reason: &str,
        daily_cap: i64,
    ) -> Result<i64, ReputationError> {
        validate_reason(reason)?;
        if daily_cap < 0 {
            return Err(ReputationError::Invalid(
                "daily_cap must be >= 0".to_string(),
            ));
        }
        let abs_delta = i64::from(delta).abs();

        let txn = self.conn.begin().await.map_err(backend_err)?;
        txn.execute_raw(stmt(
            &format!("SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"),
            vec![],
        ))
        .await
        .map_err(backend_err)?;

        // 1. Live membership re-check, inside the write transaction.
        let member_sql = format!(
            "SELECT 1 AS ok FROM community_members cm \
             JOIN communities c ON c.id = cm.community_id WHERE {MEMBER_PREDICATE}"
        );
        if txn
            .query_one_raw(stmt(&member_sql, scope_values(scope, user)))
            .await
            .map_err(backend_err)?
            .is_none()
        {
            return Err(ReputationError::NotAMember);
        }

        // 2. Ensure the score row exists, then take its row lock. Every
        // concurrent adjust for this (community, user) serializes here, which
        // is what makes the window-sum cap check below race-free.
        txn.execute_raw(stmt(
            "INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid) \
             VALUES ($1, $2, $3) ON CONFLICT (community_id, user_uuid) DO NOTHING",
            scope_values(scope, user),
        ))
        .await
        .map_err(backend_err)?;
        let locked = txn
            .query_one_raw(stmt(
                "SELECT balance FROM bundle_reputation_scores \
                 WHERE tenant_id = $1 AND community_id = $2 AND user_uuid = $3 FOR UPDATE",
                scope_values(scope, user),
            ))
            .await
            .map_err(backend_err)?
            .ok_or_else(|| {
                // The row exists for this community but under another tenant:
                // never operate on it.
                ReputationError::Backend("score row not visible under this tenant".to_string())
            })?;
        let _: i64 = locked.try_get("", "balance").map_err(backend_err)?;

        // 3. Rolling-24h cap over APPLIED adjustments (ledger rows).
        let used_row = txn
            .query_one_raw(stmt(
                "SELECT COALESCE(SUM(ABS(delta)), 0)::BIGINT AS used \
                 FROM bundle_reputation_adjustments \
                 WHERE tenant_id = $1 AND community_id = $2 AND target_user_uuid = $3::text \
                   AND scope = 'community' AND occurred_at > NOW() - INTERVAL '24 hours'",
                scope_values(scope, user),
            ))
            .await
            .map_err(backend_err)?
            .ok_or_else(|| backend_err("window SUM returned no row"))?;
        let used: i64 = used_row.try_get("", "used").map_err(backend_err)?;
        if used.saturating_add(abs_delta) > daily_cap {
            return Err(ReputationError::DailyCapExceeded { cap: daily_cap });
        }

        // 4. Apply + audit, same transaction.
        let updated = txn
            .query_one_raw(stmt(
                "UPDATE bundle_reputation_scores \
                 SET balance = balance + $4, adjustment_count = adjustment_count + 1, \
                     updated_at = NOW() \
                 WHERE tenant_id = $1 AND community_id = $2 AND user_uuid = $3 \
                 RETURNING balance",
                {
                    let mut v = scope_values(scope, user);
                    v.push(Value::BigInt(Some(i64::from(delta))));
                    v
                },
            ))
            .await
            .map_err(backend_err)?
            .ok_or_else(|| backend_err("UPDATE ... RETURNING produced no row"))?;
        let new_balance: i64 = updated.try_get("", "balance").map_err(backend_err)?;

        txn.execute_raw(stmt(
            "INSERT INTO bundle_reputation_adjustments \
             (app_id, tenant_id, community_id, target_user_uuid, scope, delta, reason_code) \
             VALUES ($1, $2, $3, $4::text, 'community', $5, $6)",
            vec![
                Value::String(Some(scope.app_id.clone())),
                Value::Int(Some(scope.tenant_id)),
                Value::Int(Some(scope.community_id)),
                Value::Uuid(Some(user)),
                Value::Int(Some(delta)),
                Value::String(Some(reason.to_string())),
            ],
        ))
        .await
        .map_err(backend_err)?;

        txn.commit().await.map_err(backend_err)?;
        metrics::record_applied_delta(delta.unsigned_abs());
        Ok(new_balance)
    }
}

impl ReputationStore for PostgresReputationStore {
    fn get<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.get_impl(scope, user).await;
            metrics::record_call("get", outcome_of(&result), start.elapsed().as_secs_f64());
            result
        })
    }

    fn adjust<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        delta: i32,
        reason: &'a str,
        daily_cap: i64,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self
                .adjust_impl(scope, user, delta, reason, daily_cap)
                .await;
            metrics::record_call("adjust", outcome_of(&result), start.elapsed().as_secs_f64());
            result
        })
    }
}

/// Loads every active `(tenant, community, user_uuid)` membership for
/// `tenant_id` (NULL `user_uuid` rows excluded -- they cannot be a target).
/// Scoped to one tenant so the in-memory snapshot is bounded by that tenant's
/// membership, not the platform's.
pub async fn load_membership(
    conn: &DatabaseConnection,
    tenant_id: i32,
) -> Result<Vec<MemberRow>, DbErr> {
    let rows = conn
        .query_all_raw(stmt(
            "SELECT c.tenant_id AS tenant_id, cm.community_id AS community_id, \
                    cm.user_uuid AS user_uuid \
             FROM community_members cm JOIN communities c ON c.id = cm.community_id \
             WHERE c.tenant_id = $1 AND cm.user_uuid IS NOT NULL \
               AND COALESCE(cm.is_active, TRUE) AND cm.removed_at IS NULL \
               AND cm.left_at IS NULL",
            vec![Value::Int(Some(tenant_id))],
        ))
        .await?;
    rows.into_iter()
        .map(|r| {
            Ok(MemberRow {
                tenant_id: r.try_get("", "tenant_id")?,
                community_id: r.try_get("", "community_id")?,
                user: r.try_get("", "user_uuid")?,
            })
        })
        .collect()
}

/// Refreshes `snapshot` from the DB every `interval` until the task is
/// dropped/aborted. A failed refresh keeps the previous snapshot and logs at
/// WARN: the snapshot is only a pre-filter (writes re-check live membership in
/// their own transaction), so staleness is bounded and never widens write
/// authority. The initial load happens before the first sleep so the snapshot
/// is populated promptly after startup.
pub async fn run_membership_refresh(
    conn: DatabaseConnection,
    tenant_id: i32,
    snapshot: Arc<SnapshotMembership>,
    interval: Duration,
) {
    loop {
        match load_membership(&conn, tenant_id).await {
            Ok(rows) => {
                let n = rows.len();
                if snapshot.replace_all(rows) {
                    tracing::debug!(
                        tenant_id,
                        members = n,
                        "reputation membership snapshot refreshed"
                    );
                } else {
                    tracing::error!(
                        "reputation membership snapshot lock poisoned; staying fail-closed"
                    );
                }
            }
            Err(e) => {
                tracing::warn!(error = %e, tenant_id, "reputation membership refresh failed; keeping previous snapshot");
            }
        }
        tokio::time::sleep(interval).await;
    }
}
