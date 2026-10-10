//! Postgres-backed [`EconomyStore`], the membership-snapshot loader and the
//! connection factory (all under the least-privilege `waddles_economy_runtime`
//! role, alembic 0050).

use std::sync::Arc;
use std::time::{Duration, Instant};

use bundle_capability_gate::{MemberRow, SnapshotMembership};
use sea_orm::{
    ConnectOptions, ConnectionTrait, Database, DatabaseConnection, DbBackend, DbErr, Statement,
    TransactionTrait, Value,
};
use uuid::Uuid;

use crate::{
    metrics, validate_transfer, validate_wager, BoxFuture, EconomyCaps, EconomyError, EconomyScope,
    EconomyStore, LeaderboardEntry, MAX_LEADERBOARD_LIMIT,
};

/// Per-transaction statement timeout, so a stuck row lock can never hold a
/// bundle invocation (or a pool connection) past the host call deadline.
const STATEMENT_TIMEOUT_MS: u32 = 5_000;

/// Active-membership predicate shared by every query: the user must be an
/// active (not left, not removed) member of exactly this tenant's community.
/// `community_members.is_active` is nullable, and a NULL is NOT an active
/// member (`IS TRUE`, fail-closed -- the same reading every other authz path
/// takes); never `COALESCE(.., TRUE)`.
/// Bind order is always `$1 = tenant_id`, `$2 = community_id`, `$3 = user`.
const MEMBER_PREDICATE: &str = "cm.community_id = $2 AND c.tenant_id = $1 AND cm.user_uuid = $3 \
     AND cm.is_active IS TRUE AND cm.removed_at IS NULL AND cm.left_at IS NULL";

/// Non-secret connection settings (the password is passed separately to
/// [`connect`] -- Token & Secret Hygiene).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConnectConfig {
    pub host: String,
    pub port: u16,
    pub name: String,
    /// `waddles_economy_runtime` in every real deployment.
    pub user: String,
}

/// Opens the pooled read-write connection for the economy store.
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

fn backend_err(e: impl std::fmt::Display) -> EconomyError {
    EconomyError::Backend(e.to_string())
}

fn stmt(sql: &str, values: Vec<Value>) -> Statement {
    Statement::from_sql_and_values(DbBackend::Postgres, sql, values)
}

fn scope_values(scope: &EconomyScope, user: Uuid) -> Vec<Value> {
    vec![
        Value::Int(Some(scope.tenant_id)),
        Value::Int(Some(scope.community_id)),
        Value::Uuid(Some(user)),
    ]
}

/// Rejects a negative aggregate ceiling (a zero one is legal: it refuses every
/// call, the fail-closed fallback the stage uses for an unrecognized catalog).
fn validate_caps(caps: EconomyCaps) -> Result<(), EconomyError> {
    if caps.per_user_daily_max < 0 || caps.per_scope_daily_max < 0 {
        return Err(EconomyError::Invalid("daily caps must be >= 0".to_string()));
    }
    Ok(())
}

fn outcome_of<T>(r: &Result<T, EconomyError>) -> &'static str {
    match r {
        Ok(_) => "ok",
        Err(e) => e.wire_code(),
    }
}

/// Takes the transaction-scoped advisory lock serializing every money-moving
/// call of `kind` (`wager` | `transfer`) for one `(tenant, community, app)`.
/// Always the FIRST lock a transaction takes (one per transaction), so the lock
/// order is uniform and cannot cycle with the row locks that follow. A hash
/// collision only over-serializes two unrelated scopes.
async fn lock_scope<C: ConnectionTrait>(
    conn: &C,
    scope: &EconomyScope,
    kind: &str,
) -> Result<(), EconomyError> {
    conn.execute_raw(stmt(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        vec![Value::String(Some(format!(
            "waddles.bundle_economy.scope:{}:{}:{}:{kind}",
            scope.tenant_id, scope.community_id, scope.app_id
        )))],
    ))
    .await
    .map_err(backend_err)?;
    Ok(())
}

/// Enforces both durable rolling-24h aggregates for `amount` about to be
/// applied by `user`, derived from the append-only ledger of APPLIED movements
/// (a refused call wrote no row and so consumed no budget). MUST run under
/// [`lock_scope`] for the same `kind`, which is what makes the SUMs race-free.
/// `ledger_kind`/`amount_expr` are internal constants (`wager`/`stake`,
/// `transfer_out`/`-delta`), never caller input.
async fn enforce_daily_caps<C: ConnectionTrait>(
    conn: &C,
    scope: &EconomyScope,
    user: Uuid,
    ledger_kind: &'static str,
    amount_expr: &'static str,
    amount: i64,
    caps: EconomyCaps,
) -> Result<(), EconomyError> {
    let base = |per_user: bool| {
        format!(
            "SELECT COALESCE(SUM({amount_expr}), 0)::BIGINT AS used FROM economy_ledger \
             WHERE tenant_id = $1 AND community_id = $2 AND app_id = $3 \
               AND kind = '{ledger_kind}' AND occurred_at > NOW() - INTERVAL '24 hours'{}",
            if per_user { " AND user_uuid = $4" } else { "" }
        )
    };
    let scope_values = |per_user: bool| {
        let mut v = vec![
            Value::Int(Some(scope.tenant_id)),
            Value::Int(Some(scope.community_id)),
            Value::String(Some(scope.app_id.clone())),
        ];
        if per_user {
            v.push(Value::Uuid(Some(user)));
        }
        v
    };

    let user_used: i64 = conn
        .query_one_raw(stmt(&base(true), scope_values(true)))
        .await
        .map_err(backend_err)?
        .ok_or_else(|| backend_err("per-user window SUM returned no row"))?
        .try_get("", "used")
        .map_err(backend_err)?;
    if user_used.saturating_add(amount) > caps.per_user_daily_max {
        return Err(EconomyError::UserQuotaExceeded {
            cap: caps.per_user_daily_max,
        });
    }
    let scope_used: i64 = conn
        .query_one_raw(stmt(&base(false), scope_values(false)))
        .await
        .map_err(backend_err)?
        .ok_or_else(|| backend_err("per-scope window SUM returned no row"))?
        .try_get("", "used")
        .map_err(backend_err)?;
    if scope_used.saturating_add(amount) > caps.per_scope_daily_max {
        return Err(EconomyError::ScopeQuotaExceeded {
            cap: caps.per_scope_daily_max,
        });
    }
    Ok(())
}

/// The production [`EconomyStore`].
pub struct PostgresEconomyStore {
    conn: DatabaseConnection,
}

impl PostgresEconomyStore {
    pub fn new(conn: DatabaseConnection) -> Self {
        Self { conn }
    }

    /// Explains why a guarded UPDATE matched no row: `user` is not an active
    /// member (`NotAMember`) or holds less than the amount being debited
    /// (`InsufficientFunds`, carrying what they do hold). Read AFTER the failed
    /// write; it never gates money movement, only names the refusal.
    async fn diagnose_refusal<C: ConnectionTrait>(
        conn: &C,
        scope: &EconomyScope,
        user: Uuid,
    ) -> Result<EconomyError, EconomyError> {
        let sql = format!(
            "SELECT EXISTS (SELECT 1 FROM community_members cm \
                 JOIN communities c ON c.id = cm.community_id WHERE {MEMBER_PREDICATE}) \
                 AS is_member, \
             COALESCE((SELECT b.balance FROM economy_balances b \
                 WHERE b.tenant_id = $1 AND b.community_id = $2 AND b.user_uuid = $3), 0)::BIGINT \
                 AS balance"
        );
        let row = conn
            .query_one_raw(stmt(&sql, scope_values(scope, user)))
            .await
            .map_err(backend_err)?
            .ok_or_else(|| backend_err("diagnostic query returned no row"))?;
        let is_member: bool = row.try_get("", "is_member").map_err(backend_err)?;
        let balance: i64 = row.try_get("", "balance").map_err(backend_err)?;
        Ok(if is_member {
            EconomyError::InsufficientFunds { balance }
        } else {
            EconomyError::NotAMember
        })
    }

    async fn balance_impl(&self, scope: &EconomyScope, user: Uuid) -> Result<i64, EconomyError> {
        let sql = format!(
            "SELECT COALESCE(b.balance, 0)::BIGINT AS balance \
             FROM community_members cm \
             JOIN communities c ON c.id = cm.community_id \
             LEFT JOIN economy_balances b \
               ON b.tenant_id = c.tenant_id AND b.community_id = cm.community_id \
              AND b.user_uuid = cm.user_uuid \
             WHERE {MEMBER_PREDICATE}"
        );
        let row = self
            .conn
            .query_one_raw(stmt(&sql, scope_values(scope, user)))
            .await
            .map_err(backend_err)?
            .ok_or(EconomyError::NotAMember)?;
        row.try_get::<i64>("", "balance").map_err(backend_err)
    }

    async fn max_bet_impl(
        &self,
        scope: &EconomyScope,
        user: Uuid,
        cap: i64,
    ) -> Result<i64, EconomyError> {
        if cap < 0 {
            return Err(EconomyError::Invalid("cap must be >= 0".to_string()));
        }
        Ok(self.balance_impl(scope, user).await?.min(cap))
    }

    async fn wager_impl(
        &self,
        scope: &EconomyScope,
        user: Uuid,
        stake: i64,
        payout: i64,
        max_bet: i64,
        caps: EconomyCaps,
    ) -> Result<i64, EconomyError> {
        validate_wager(stake, payout, max_bet)?;
        validate_caps(caps)?;

        // The whole wager is ONE statement: the guarded UPDATE is the
        // atomic debit+credit (`balance >= stake` re-evaluated against the
        // committed row under its lock -- no read-modify-write window), the
        // membership predicate is part of its WHERE, and the ledger row is a
        // data-modifying CTE of the same statement.
        let sql = format!(
            "WITH upd AS ( \
                 UPDATE economy_balances b \
                    SET balance = b.balance - $4::BIGINT + $5::BIGINT, updated_at = NOW() \
                  WHERE b.tenant_id = $1 AND b.community_id = $2 AND b.user_uuid = $3 \
                    AND b.balance >= $4::BIGINT \
                    AND EXISTS (SELECT 1 FROM community_members cm \
                                  JOIN communities c ON c.id = cm.community_id \
                                 WHERE {MEMBER_PREDICATE}) \
              RETURNING b.balance AS balance \
             ), led AS ( \
                 INSERT INTO economy_ledger \
                     (tenant_id, community_id, app_id, user_uuid, kind, delta, stake, payout, \
                      balance_after) \
                 SELECT $1::INT, $2::INT, $6::TEXT, $3::UUID, 'wager', \
                        $5::BIGINT - $4::BIGINT, $4::BIGINT, $5::BIGINT, upd.balance FROM upd \
             ) \
             SELECT balance FROM upd"
        );
        let mut values = scope_values(scope, user);
        values.push(Value::BigInt(Some(stake)));
        values.push(Value::BigInt(Some(payout)));
        values.push(Value::String(Some(scope.app_id.clone())));

        let txn = self.conn.begin().await.map_err(backend_err)?;
        txn.execute_raw(stmt(
            &format!("SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"),
            vec![],
        ))
        .await
        .map_err(backend_err)?;

        // Durable daily aggregates first (under the per-scope advisory lock,
        // always this transaction's first lock), then the one atomic statement.
        lock_scope(&txn, scope, "wager").await?;
        enforce_daily_caps(&txn, scope, user, "wager", "stake", stake, caps).await?;

        let Some(row) = txn
            .query_one_raw(stmt(&sql, values))
            .await
            .map_err(backend_err)?
        else {
            let refusal = Self::diagnose_refusal(&txn, scope, user).await?;
            return Err(refusal);
        };
        let new_balance: i64 = row.try_get("", "balance").map_err(backend_err)?;
        txn.commit().await.map_err(backend_err)?;
        metrics::record_applied_amount("wager", stake);
        Ok(new_balance)
    }

    async fn transfer_impl(
        &self,
        scope: &EconomyScope,
        from: Uuid,
        to: Uuid,
        amount: i64,
        max_amount: i64,
        caps: EconomyCaps,
    ) -> Result<(), EconomyError> {
        validate_transfer(from, to, amount, max_amount)?;
        validate_caps(caps)?;

        let txn = self.conn.begin().await.map_err(backend_err)?;
        txn.execute_raw(stmt(
            &format!("SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"),
            vec![],
        ))
        .await
        .map_err(backend_err)?;

        // 0. Per-scope advisory lock (this transaction's first lock) and the
        // durable daily aggregates, charged against the SENDER.
        lock_scope(&txn, scope, "transfer").await?;
        enforce_daily_caps(&txn, scope, from, "transfer_out", "-delta", amount, caps).await?;

        // 1. Live membership of BOTH sides, inside the write transaction.
        let member_sql = format!(
            "SELECT 1 AS ok FROM community_members cm \
             JOIN communities c ON c.id = cm.community_id WHERE {MEMBER_PREDICATE}"
        );
        for user in [from, to] {
            if txn
                .query_one_raw(stmt(&member_sql, scope_values(scope, user)))
                .await
                .map_err(backend_err)?
                .is_none()
            {
                return Err(EconomyError::NotAMember);
            }
        }

        // 2. The recipient may never have held currency: ensure its row, then
        // lock both rows in deterministic (uuid) order so two opposite
        // transfers can never deadlock.
        txn.execute_raw(stmt(
            "INSERT INTO economy_balances (tenant_id, community_id, user_uuid) \
             VALUES ($1, $2, $3) ON CONFLICT (tenant_id, community_id, user_uuid) DO NOTHING",
            scope_values(scope, to),
        ))
        .await
        .map_err(backend_err)?;
        let (first, second) = if from < to { (from, to) } else { (to, from) };
        txn.execute_raw(stmt(
            "SELECT user_uuid FROM economy_balances \
             WHERE tenant_id = $1 AND community_id = $2 AND user_uuid IN ($3, $4) \
             ORDER BY user_uuid FOR UPDATE",
            {
                let mut v = scope_values(scope, first);
                v.push(Value::Uuid(Some(second)));
                v
            },
        ))
        .await
        .map_err(backend_err)?;

        // 3. One statement: guarded debit, credit, both ledger rows. The
        // credit and ledger CTEs depend on the debit having matched a row.
        let sql = "WITH debit AS ( \
                 UPDATE economy_balances \
                    SET balance = balance - $4::BIGINT, updated_at = NOW() \
                  WHERE tenant_id = $1 AND community_id = $2 AND user_uuid = $3 \
                    AND balance >= $4::BIGINT \
              RETURNING balance \
             ), credit AS ( \
                 UPDATE economy_balances \
                    SET balance = balance + $4::BIGINT, updated_at = NOW() \
                  WHERE tenant_id = $1 AND community_id = $2 AND user_uuid = $5 \
                    AND EXISTS (SELECT 1 FROM debit) \
              RETURNING balance \
             ), led_out AS ( \
                 INSERT INTO economy_ledger \
                     (tenant_id, community_id, app_id, user_uuid, counterparty_uuid, kind, delta, \
                      balance_after) \
                 SELECT $1::INT, $2::INT, $6::TEXT, $3::UUID, $5::UUID, 'transfer_out', \
                        -$4::BIGINT, debit.balance FROM debit \
             ), led_in AS ( \
                 INSERT INTO economy_ledger \
                     (tenant_id, community_id, app_id, user_uuid, counterparty_uuid, kind, delta, \
                      balance_after) \
                 SELECT $1::INT, $2::INT, $6::TEXT, $5::UUID, $3::UUID, 'transfer_in', \
                        $4::BIGINT, credit.balance FROM credit \
             ) \
             SELECT (SELECT balance FROM debit) AS from_balance, \
                    (SELECT balance FROM credit) AS to_balance";
        let mut values = scope_values(scope, from);
        values.push(Value::BigInt(Some(amount)));
        values.push(Value::Uuid(Some(to)));
        values.push(Value::String(Some(scope.app_id.clone())));
        let row = txn
            .query_one_raw(stmt(sql, values))
            .await
            .map_err(backend_err)?
            .ok_or_else(|| backend_err("transfer statement returned no row"))?;
        let from_balance: Option<i64> = row.try_get("", "from_balance").map_err(backend_err)?;
        let to_balance: Option<i64> = row.try_get("", "to_balance").map_err(backend_err)?;
        match (from_balance, to_balance) {
            (Some(_), Some(_)) => {}
            (None, _) => {
                let refusal = Self::diagnose_refusal(&txn, scope, from).await?;
                return Err(refusal);
            }
            (Some(_), None) => {
                return Err(backend_err("recipient balance row vanished mid-transfer"));
            }
        }
        txn.commit().await.map_err(backend_err)?;
        metrics::record_applied_amount("transfer", amount);
        Ok(())
    }

    async fn leaderboard_impl(
        &self,
        scope: &EconomyScope,
        limit: u32,
    ) -> Result<Vec<LeaderboardEntry>, EconomyError> {
        if limit == 0 || limit > MAX_LEADERBOARD_LIMIT {
            return Err(EconomyError::Invalid(format!(
                "limit must be 1..={MAX_LEADERBOARD_LIMIT}"
            )));
        }
        let rows = self
            .conn
            .query_all_raw(stmt(
                "SELECT b.user_uuid AS user_uuid, b.balance AS balance \
                 FROM economy_balances b \
                 JOIN community_members cm \
                   ON cm.community_id = b.community_id AND cm.user_uuid = b.user_uuid \
                 JOIN communities c ON c.id = cm.community_id AND c.tenant_id = b.tenant_id \
                 WHERE b.tenant_id = $1 AND b.community_id = $2 \
                   AND cm.is_active IS TRUE AND cm.removed_at IS NULL \
                   AND cm.left_at IS NULL \
                 ORDER BY b.balance DESC, b.user_uuid ASC \
                 LIMIT $3",
                vec![
                    Value::Int(Some(scope.tenant_id)),
                    Value::Int(Some(scope.community_id)),
                    Value::BigInt(Some(i64::from(limit))),
                ],
            ))
            .await
            .map_err(backend_err)?;
        rows.into_iter()
            .map(|r| {
                Ok(LeaderboardEntry {
                    user: r.try_get("", "user_uuid").map_err(backend_err)?,
                    balance: r.try_get("", "balance").map_err(backend_err)?,
                })
            })
            .collect()
    }
}

impl EconomyStore for PostgresEconomyStore {
    fn balance<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: Uuid,
    ) -> BoxFuture<'a, Result<i64, EconomyError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.balance_impl(scope, user).await;
            metrics::record_call(
                "balance",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }

    fn max_bet<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: Uuid,
        cap: i64,
    ) -> BoxFuture<'a, Result<i64, EconomyError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.max_bet_impl(scope, user, cap).await;
            metrics::record_call(
                "max_bet",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }

    fn wager<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: Uuid,
        stake: i64,
        payout: i64,
        max_bet: i64,
        caps: EconomyCaps,
    ) -> BoxFuture<'a, Result<i64, EconomyError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self
                .wager_impl(scope, user, stake, payout, max_bet, caps)
                .await;
            metrics::record_call("wager", outcome_of(&result), start.elapsed().as_secs_f64());
            result
        })
    }

    fn transfer<'a>(
        &'a self,
        scope: &'a EconomyScope,
        from: Uuid,
        to: Uuid,
        amount: i64,
        max_amount: i64,
        caps: EconomyCaps,
    ) -> BoxFuture<'a, Result<(), EconomyError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self
                .transfer_impl(scope, from, to, amount, max_amount, caps)
                .await;
            metrics::record_call(
                "transfer",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }

    fn leaderboard<'a>(
        &'a self,
        scope: &'a EconomyScope,
        limit: u32,
    ) -> BoxFuture<'a, Result<Vec<LeaderboardEntry>, EconomyError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.leaderboard_impl(scope, limit).await;
            metrics::record_call(
                "leaderboard",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }
}

/// Hard ceiling on snapshot size: if the active identified-membership row
/// count reaches this, the load is rejected (previous snapshot kept) rather
/// than silently truncated -- a truncated snapshot would read real members as
/// non-members. Same ceiling and rationale as the reputation loader.
pub const MAX_SNAPSHOT_ROWS: i64 = 2_000_000;

/// Loads every active `(tenant, community, user_uuid)` membership (NULL
/// `user_uuid` rows excluded -- they cannot be a target), optionally scoped to
/// one `tenant_id` (`None` = every tenant). Fails with a `DbErr::Custom` if
/// the result would reach [`MAX_SNAPSHOT_ROWS`].
pub async fn load_membership(
    conn: &DatabaseConnection,
    tenant_id: Option<i32>,
) -> Result<Vec<MemberRow>, DbErr> {
    let rows = conn
        .query_all_raw(stmt(
            "SELECT c.tenant_id AS tenant_id, cm.community_id AS community_id, \
                    cm.user_uuid AS user_uuid \
             FROM community_members cm JOIN communities c ON c.id = cm.community_id \
             WHERE ($1::int IS NULL OR c.tenant_id = $1) AND cm.user_uuid IS NOT NULL \
               AND cm.is_active IS TRUE AND cm.removed_at IS NULL \
               AND cm.left_at IS NULL \
             LIMIT $2",
            vec![
                Value::Int(tenant_id),
                Value::BigInt(Some(MAX_SNAPSHOT_ROWS)),
            ],
        ))
        .await?;
    if i64::try_from(rows.len()).unwrap_or(i64::MAX) >= MAX_SNAPSHOT_ROWS {
        return Err(DbErr::Custom(format!(
            "membership snapshot would reach the {MAX_SNAPSHOT_ROWS}-row ceiling; refusing a truncated load"
        )));
    }
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

/// Refreshes `snapshot` (scoped as in [`load_membership`]) from the DB every
/// `interval` until the task is dropped/aborted. A failed refresh keeps the
/// previous snapshot and logs at WARN: the snapshot is only a pre-filter
/// (writes re-check live membership inside the write itself), so staleness is
/// bounded and never widens write authority.
pub async fn run_membership_refresh(
    conn: DatabaseConnection,
    tenant_id: Option<i32>,
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
                        "economy membership snapshot refreshed"
                    );
                } else {
                    tracing::error!(
                        "economy membership snapshot lock poisoned; staying fail-closed"
                    );
                }
            }
            Err(e) => {
                tracing::warn!(error = %e, ?tenant_id, "economy membership refresh failed; keeping previous snapshot");
            }
        }
        tokio::time::sleep(interval).await;
    }
}
