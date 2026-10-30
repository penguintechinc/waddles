//! Postgres-backed [`ReputationStore`], the membership-snapshot loader and
//! the connection factory (all under the least-privilege
//! `waddles_bundle_reputation` role, alembic 0050).

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::{Duration, Instant};

use bundle_capability_gate::{MemberRow, SnapshotMembership};
use sea_orm::{
    ConnectOptions, ConnectionTrait, Database, DatabaseConnection, DatabaseTransaction, DbBackend,
    DbErr, Statement, TransactionTrait, Value,
};
use uuid::Uuid;

use crate::{
    clamp_score, metrics, validate_delta, validate_reason, BoxFuture, IdempotencyKey,
    ReputationCaps, ReputationError, ReputationScope, ReputationStore, ScoreEntry, MAX_BATCH,
    MAX_LEADERBOARD_LIMIT,
};

/// Per-transaction statement timeout, so a stuck row lock can never hold a
/// bundle invocation (or a pool connection) past the host call deadline.
const STATEMENT_TIMEOUT_MS: u32 = 5_000;

/// Active-membership predicate shared by every query: the target must be an
/// active (not left, not removed) member of exactly this tenant's community.
/// `community_members.is_active` is nullable, and a NULL is NOT an active
/// member (`IS TRUE`, fail-closed -- the same reading every other authz path
/// takes); never `COALESCE(.., TRUE)`.
/// Bind order is always `$1 = tenant_id`, `$2 = community_id`, `$3 = user`.
const MEMBER_PREDICATE: &str = "cm.community_id = $2 AND c.tenant_id = $1 AND cm.user_uuid = $3 \
     AND cm.is_active IS TRUE AND cm.removed_at IS NULL AND cm.left_at IS NULL";

/// The rolling-24h window predicate every cap SUM shares.
const WINDOW: &str = "AND occurred_at > NOW() - INTERVAL '24 hours'";

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
        caps: ReputationCaps,
    ) -> Result<i64, ReputationError> {
        validate_reason(reason)?;
        validate_delta(delta)?;
        if caps.per_user_daily_abs_max < 0 || caps.per_scope_daily_abs_max < 0 {
            return Err(ReputationError::Invalid(
                "daily caps must be >= 0".to_string(),
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

        // 2. Per-scope advisory lock (transaction-scoped, released at
        // commit/rollback). Adjusts for DIFFERENT users in this
        // (tenant, community, app) share no score row, so this is what
        // serializes them for the per-scope window SUM in step 4. Always taken
        // BEFORE any score-row lock so the lock order is uniform (no cycles).
        // A hash collision only over-serializes two unrelated scopes.
        txn.execute_raw(stmt(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            vec![Value::String(Some(format!(
                "waddles.bundle_reputation.scope:{}:{}:{}",
                scope.tenant_id, scope.community_id, scope.app_id
            )))],
        ))
        .await
        .map_err(backend_err)?;

        // 3. Ensure the score row exists, then take its row lock. Every
        // concurrent adjust for this (community, user) serializes here, which
        // is what makes the per-user window-sum cap check below race-free.
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
        let current: i64 = locked.try_get("", "balance").map_err(backend_err)?;

        // 4. Rolling-24h per-user cap over APPLIED adjustments (ledger rows).
        let used_row = txn
            .query_one_raw(stmt(
                &format!(
                "SELECT COALESCE(SUM(ABS(COALESCE(requested_value, delta))), 0)::BIGINT AS used \
                 FROM bundle_reputation_adjustments \
                 WHERE tenant_id = $1 AND community_id = $2 AND target_user_uuid = $3::text \
                   AND scope = 'community' AND kind = 'adjust' {WINDOW}"
            ),
                scope_values(scope, user),
            ))
            .await
            .map_err(backend_err)?
            .ok_or_else(|| backend_err("window SUM returned no row"))?;
        let used: i64 = used_row.try_get("", "used").map_err(backend_err)?;
        if used.saturating_add(abs_delta) > caps.per_user_daily_abs_max {
            return Err(ReputationError::DailyCapExceeded {
                cap: caps.per_user_daily_abs_max,
            });
        }

        // 5. Rolling-24h per-scope cap: the SUM of every applied |delta| by
        // this app across the whole (tenant, community), under the advisory
        // lock from step 2. Durable (derived from the ledger), so a restart or
        // a second replica cannot reset or multiply it. Served by 0041's
        // `idx_bundle_reputation_adjustments_app_day (app_id, occurred_at)`.
        let scope_used_row = txn
            .query_one_raw(stmt(
                &format!(
                "SELECT COALESCE(SUM(ABS(COALESCE(requested_value, delta))), 0)::BIGINT AS used \
                 FROM bundle_reputation_adjustments \
                 WHERE tenant_id = $1 AND community_id = $2 AND app_id = $3 \
                   AND scope = 'community' AND kind = 'adjust' {WINDOW}"
            ),
                vec![
                    Value::Int(Some(scope.tenant_id)),
                    Value::Int(Some(scope.community_id)),
                    Value::String(Some(scope.app_id.clone())),
                ],
            ))
            .await
            .map_err(backend_err)?
            .ok_or_else(|| backend_err("scope window SUM returned no row"))?;
        let scope_used: i64 = scope_used_row.try_get("", "used").map_err(backend_err)?;
        if scope_used.saturating_add(abs_delta) > caps.per_scope_daily_abs_max {
            return Err(ReputationError::ScopeQuotaExceeded {
                cap: caps.per_scope_daily_abs_max,
            });
        }

        // 6. Apply (clamped into the persisted range) + audit, same
        // transaction. The ledger always records the call -- `delta` is the
        // APPLIED change, `requested_value` what was asked (the caps count the
        // latter, so a call pinned at a bound still consumes quota and cannot
        // be spun for unbounded ledger growth).
        let new_balance = clamp_score(current.saturating_add(i64::from(delta)));
        let applied = new_balance - current;
        apply_and_ledger(
            &txn,
            scope,
            user,
            new_balance,
            LedgerRow {
                kind: "adjust",
                applied,
                requested: i64::from(delta),
                reason,
                key: None,
            },
        )
        .await?;

        txn.commit().await.map_err(backend_err)?;
        metrics::record_applied_delta(applied.unsigned_abs());
        Ok(new_balance)
    }

    /// Opens a write transaction with the statement timeout set.
    async fn begin(&self) -> Result<DatabaseTransaction, ReputationError> {
        let txn = self.conn.begin().await.map_err(backend_err)?;
        txn.execute_raw(stmt(
            &format!("SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"),
            vec![],
        ))
        .await
        .map_err(backend_err)?;
        Ok(txn)
    }

    async fn override_impl(
        &self,
        scope: &ReputationScope,
        user: Uuid,
        absolute_score: i64,
        reason: &str,
        key: &IdempotencyKey,
    ) -> Result<i64, ReputationError> {
        validate_reason(reason)?;
        let target = clamp_score(absolute_score);

        let txn = self.begin().await?;
        require_members(&txn, scope, &[user]).await?;
        scope_lock(&txn, scope).await?;
        ensure_score_rows(&txn, scope, &[user]).await?;
        let balances = lock_balances(&txn, scope, &[user]).await?;
        let current = *balances
            .get(&user)
            .ok_or_else(|| backend_err("locked balance missing"))?;

        // Idempotent replay: same key + same parameters answers with the
        // original result and moves nothing; anything else is a conflict.
        let existing = keyed_rows(&txn, scope, key).await?;
        if !existing.is_empty() {
            return match existing.as_slice() {
                [row]
                    if row.user == user.to_string()
                        && row.kind == "admin_override"
                        && row.requested == Some(absolute_score) =>
                {
                    row.balance_after
                        .ok_or_else(|| backend_err("replayed override row has no balance_after"))
                }
                _ => Err(ReputationError::IdempotencyConflict),
            };
        }

        apply_and_ledger(
            &txn,
            scope,
            user,
            target,
            LedgerRow {
                kind: "admin_override",
                applied: target - current,
                requested: absolute_score,
                reason,
                key: Some(key),
            },
        )
        .await?;
        txn.commit().await.map_err(backend_err)?;
        Ok(target)
    }

    async fn adjust_many_impl(
        &self,
        scope: &ReputationScope,
        items: &[(Uuid, i32)],
        reason: &str,
        caps: ReputationCaps,
        key: &IdempotencyKey,
    ) -> Result<(), ReputationError> {
        validate_reason(reason)?;
        if items.is_empty() || items.len() > MAX_BATCH {
            return Err(ReputationError::Invalid(format!(
                "batch must hold 1..={MAX_BATCH} items"
            )));
        }
        if caps.per_user_daily_abs_max < 0 || caps.per_scope_daily_abs_max < 0 {
            return Err(ReputationError::Invalid(
                "daily caps must be >= 0".to_string(),
            ));
        }
        let mut seen = HashSet::new();
        for (user, delta) in items {
            validate_delta(*delta)?;
            if !seen.insert(*user) {
                return Err(ReputationError::Invalid(
                    "a user may appear once per batch".to_string(),
                ));
            }
        }
        let users: Vec<Uuid> = items.iter().map(|(u, _)| *u).collect();

        let txn = self.begin().await?;
        require_members(&txn, scope, &users).await?;
        scope_lock(&txn, scope).await?;
        ensure_score_rows(&txn, scope, &users).await?;
        let balances = lock_balances(&txn, scope, &users).await?;

        let existing = keyed_rows(&txn, scope, key).await?;
        if !existing.is_empty() {
            let replay_matches = existing.len() == items.len()
                && items.iter().all(|(user, delta)| {
                    existing.iter().any(|row| {
                        row.user == user.to_string()
                            && row.kind == "adjust"
                            && row.requested == Some(i64::from(*delta))
                    })
                });
            return if replay_matches {
                Ok(())
            } else {
                Err(ReputationError::IdempotencyConflict)
            };
        }

        // Per-user rolling-24h caps, one grouped read under the locks.
        let used_by_user = {
            let list = in_list(3, users.len());
            let mut v = vec![
                Value::Int(Some(scope.tenant_id)),
                Value::Int(Some(scope.community_id)),
            ];
            v.extend(users.iter().map(|u| Value::String(Some(u.to_string()))));
            let rows = txn
                .query_all_raw(stmt(
                    &format!(
                        "SELECT target_user_uuid AS u, \
                                COALESCE(SUM(ABS(COALESCE(requested_value, delta))), 0)::BIGINT AS used \
                         FROM bundle_reputation_adjustments \
                         WHERE tenant_id = $1 AND community_id = $2 \
                           AND target_user_uuid IN ({list}) \
                           AND scope = 'community' AND kind = 'adjust' {WINDOW} \
                         GROUP BY target_user_uuid"
                    ),
                    v,
                ))
                .await
                .map_err(backend_err)?;
            let mut m: HashMap<String, i64> = HashMap::new();
            for r in rows {
                m.insert(
                    r.try_get("", "u").map_err(backend_err)?,
                    r.try_get("", "used").map_err(backend_err)?,
                );
            }
            m
        };
        let mut batch_abs: i64 = 0;
        for (user, delta) in items {
            let abs = i64::from(*delta).abs();
            let used = used_by_user.get(&user.to_string()).copied().unwrap_or(0);
            if used.saturating_add(abs) > caps.per_user_daily_abs_max {
                return Err(ReputationError::DailyCapExceeded {
                    cap: caps.per_user_daily_abs_max,
                });
            }
            batch_abs = batch_abs.saturating_add(abs);
        }
        let scope_used = scalar_scope_used(&txn, scope).await?;
        if scope_used.saturating_add(batch_abs) > caps.per_scope_daily_abs_max {
            return Err(ReputationError::ScopeQuotaExceeded {
                cap: caps.per_scope_daily_abs_max,
            });
        }

        let mut applied_abs = Vec::with_capacity(items.len());
        for (user, delta) in items {
            let current = *balances
                .get(user)
                .ok_or_else(|| backend_err("locked balance missing"))?;
            let new_balance = clamp_score(current.saturating_add(i64::from(*delta)));
            let applied = new_balance - current;
            apply_and_ledger(
                &txn,
                scope,
                *user,
                new_balance,
                LedgerRow {
                    kind: "adjust",
                    applied,
                    requested: i64::from(*delta),
                    reason,
                    key: Some(key),
                },
            )
            .await?;
            applied_abs.push(applied.unsigned_abs());
        }
        txn.commit().await.map_err(backend_err)?;
        for abs in applied_abs {
            metrics::record_applied_delta(abs);
        }
        Ok(())
    }

    async fn get_many_impl(
        &self,
        scope: &ReputationScope,
        users: &[Uuid],
    ) -> Result<Vec<ScoreEntry>, ReputationError> {
        if users.is_empty() || users.len() > MAX_BATCH {
            return Err(ReputationError::Invalid(format!(
                "batch must hold 1..={MAX_BATCH} users"
            )));
        }
        let mut seen = HashSet::new();
        let unique: Vec<Uuid> = users.iter().copied().filter(|u| seen.insert(*u)).collect();
        let list = in_list(3, unique.len());
        let mut v = vec![
            Value::Int(Some(scope.tenant_id)),
            Value::Int(Some(scope.community_id)),
        ];
        v.extend(unique.iter().map(|u| Value::Uuid(Some(*u))));
        let rows = self
            .conn
            .query_all_raw(stmt(
                &format!(
                    "SELECT cm.user_uuid AS user_uuid, COALESCE(s.balance, 0)::BIGINT AS balance \
                     FROM community_members cm \
                     JOIN communities c ON c.id = cm.community_id \
                     LEFT JOIN bundle_reputation_scores s \
                       ON s.community_id = cm.community_id AND s.user_uuid = cm.user_uuid \
                     WHERE {ACTIVE_MEMBER_PREDICATE} AND cm.user_uuid IN ({list})"
                ),
                v,
            ))
            .await
            .map_err(backend_err)?;
        let mut by_user: HashMap<Uuid, i64> = HashMap::new();
        for r in rows {
            by_user.insert(
                r.try_get("", "user_uuid").map_err(backend_err)?,
                r.try_get("", "balance").map_err(backend_err)?,
            );
        }
        unique
            .iter()
            .map(|u| {
                by_user
                    .get(u)
                    .map(|score| ScoreEntry {
                        user: *u,
                        score: *score,
                    })
                    .ok_or(ReputationError::NotAMember)
            })
            .collect()
    }

    async fn leaderboard_impl(
        &self,
        scope: &ReputationScope,
        limit: u32,
    ) -> Result<Vec<ScoreEntry>, ReputationError> {
        if limit == 0 || limit > MAX_LEADERBOARD_LIMIT {
            return Err(ReputationError::Invalid(format!(
                "limit must be 1..={MAX_LEADERBOARD_LIMIT}"
            )));
        }
        let rows = self
            .conn
            .query_all_raw(stmt(
                "SELECT s.user_uuid AS user_uuid, s.balance AS balance \
                 FROM bundle_reputation_scores s \
                 JOIN community_members cm \
                   ON cm.community_id = s.community_id AND cm.user_uuid = s.user_uuid \
                 JOIN communities c ON c.id = cm.community_id AND c.tenant_id = s.tenant_id \
                 WHERE s.tenant_id = $1 AND s.community_id = $2 \
                   AND cm.is_active IS TRUE AND cm.removed_at IS NULL AND cm.left_at IS NULL \
                 ORDER BY s.balance DESC, s.user_uuid ASC \
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
                Ok(ScoreEntry {
                    user: r.try_get("", "user_uuid").map_err(backend_err)?,
                    score: r.try_get("", "balance").map_err(backend_err)?,
                })
            })
            .collect()
    }
}

/// Active-member predicate for a SET of users (`$1 = tenant`, `$2 =
/// community`; the caller appends `AND cm.user_uuid IN (...)`). Same NULL
/// `is_active` fail-closed reading as [`MEMBER_PREDICATE`].
const ACTIVE_MEMBER_PREDICATE: &str = "cm.community_id = $2 AND c.tenant_id = $1 \
     AND cm.is_active IS TRUE AND cm.removed_at IS NULL AND cm.left_at IS NULL";

/// `$start,$start+1,...` for `n` placeholders (batches are capped at
/// [`MAX_BATCH`], so this stays tiny; avoids the sea-orm `postgres-array`
/// feature).
fn in_list(start: usize, n: usize) -> String {
    (start..start + n)
        .map(|i| format!("${i}"))
        .collect::<Vec<_>>()
        .join(",")
}

/// One ledger row's worth of audit data (see
/// `scripts/db/bundle_reputation_host_ops.sql` for the column semantics).
struct LedgerRow<'a> {
    kind: &'static str,
    applied: i64,
    requested: i64,
    reason: &'a str,
    key: Option<&'a IdempotencyKey>,
}

/// A previously-applied keyed ledger row, for replay detection.
struct KeyedRow {
    user: String,
    kind: String,
    requested: Option<i64>,
    balance_after: Option<i64>,
}

/// Every ACTIVE-member check for a set of users, inside the write txn:
/// fewer matches than distinct users means a non-member.
async fn require_members(
    txn: &DatabaseTransaction,
    scope: &ReputationScope,
    users: &[Uuid],
) -> Result<(), ReputationError> {
    let list = in_list(3, users.len());
    let mut v = vec![
        Value::Int(Some(scope.tenant_id)),
        Value::Int(Some(scope.community_id)),
    ];
    v.extend(users.iter().map(|u| Value::Uuid(Some(*u))));
    let row = txn
        .query_one_raw(stmt(
            &format!(
                "SELECT COUNT(DISTINCT cm.user_uuid)::BIGINT AS n FROM community_members cm \
                 JOIN communities c ON c.id = cm.community_id \
                 WHERE {ACTIVE_MEMBER_PREDICATE} AND cm.user_uuid IN ({list})"
            ),
            v,
        ))
        .await
        .map_err(backend_err)?
        .ok_or_else(|| backend_err("member count returned no row"))?;
    let n: i64 = row.try_get("", "n").map_err(backend_err)?;
    let distinct = users.iter().collect::<HashSet<_>>().len();
    if usize::try_from(n).ok() != Some(distinct) {
        return Err(ReputationError::NotAMember);
    }
    Ok(())
}

/// The per-scope advisory lock (see the module docs / `adjust_impl` step 2):
/// serializes every mutation of one (tenant, community, app) so the window
/// SUMs and the keyed-replay check are race-free. Always taken BEFORE any
/// score-row lock (uniform order, no cycles).
async fn scope_lock(
    txn: &DatabaseTransaction,
    scope: &ReputationScope,
) -> Result<(), ReputationError> {
    txn.execute_raw(stmt(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        vec![Value::String(Some(format!(
            "waddles.bundle_reputation.scope:{}:{}:{}",
            scope.tenant_id, scope.community_id, scope.app_id
        )))],
    ))
    .await
    .map_err(backend_err)?;
    Ok(())
}

/// Creates any missing score rows for `users` (balance 0).
async fn ensure_score_rows(
    txn: &DatabaseTransaction,
    scope: &ReputationScope,
    users: &[Uuid],
) -> Result<(), ReputationError> {
    let values = (3..3 + users.len())
        .map(|i| format!("(${i}::uuid)"))
        .collect::<Vec<_>>()
        .join(",");
    let mut v = vec![
        Value::Int(Some(scope.tenant_id)),
        Value::Int(Some(scope.community_id)),
    ];
    v.extend(users.iter().map(|u| Value::Uuid(Some(*u))));
    txn.execute_raw(stmt(
        &format!(
            "INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid) \
             SELECT $1, $2, x.u FROM (VALUES {values}) AS x(u) \
             ON CONFLICT (community_id, user_uuid) DO NOTHING"
        ),
        v,
    ))
    .await
    .map_err(backend_err)?;
    Ok(())
}

/// Row-locks every named score row in `user_uuid` order (a uniform order, so
/// two concurrent batches cannot deadlock) and returns the locked balances.
async fn lock_balances(
    txn: &DatabaseTransaction,
    scope: &ReputationScope,
    users: &[Uuid],
) -> Result<HashMap<Uuid, i64>, ReputationError> {
    let list = in_list(3, users.len());
    let mut v = vec![
        Value::Int(Some(scope.tenant_id)),
        Value::Int(Some(scope.community_id)),
    ];
    v.extend(users.iter().map(|u| Value::Uuid(Some(*u))));
    let rows = txn
        .query_all_raw(stmt(
            &format!(
                "SELECT user_uuid, balance FROM bundle_reputation_scores \
                 WHERE tenant_id = $1 AND community_id = $2 AND user_uuid IN ({list}) \
                 ORDER BY user_uuid FOR UPDATE"
            ),
            v,
        ))
        .await
        .map_err(backend_err)?;
    let mut out = HashMap::new();
    for r in rows {
        out.insert(
            r.try_get::<Uuid>("", "user_uuid").map_err(backend_err)?,
            r.try_get::<i64>("", "balance").map_err(backend_err)?,
        );
    }
    let distinct = users.iter().collect::<HashSet<_>>().len();
    if out.len() != distinct {
        // A row exists for this community under another tenant: never touch it.
        return Err(backend_err("score row not visible under this tenant"));
    }
    Ok(out)
}

/// Ledger rows already written under `key` in this (tenant, community, app).
async fn keyed_rows(
    txn: &DatabaseTransaction,
    scope: &ReputationScope,
    key: &IdempotencyKey,
) -> Result<Vec<KeyedRow>, ReputationError> {
    let rows = txn
        .query_all_raw(stmt(
            "SELECT target_user_uuid, kind, requested_value, balance_after \
             FROM bundle_reputation_adjustments \
             WHERE tenant_id = $1 AND community_id = $2 AND app_id = $3 \
               AND idempotency_key = $4",
            vec![
                Value::Int(Some(scope.tenant_id)),
                Value::Int(Some(scope.community_id)),
                Value::String(Some(scope.app_id.clone())),
                Value::String(Some(key.as_str().to_string())),
            ],
        ))
        .await
        .map_err(backend_err)?;
    rows.into_iter()
        .map(|r| {
            Ok(KeyedRow {
                user: r.try_get("", "target_user_uuid").map_err(backend_err)?,
                kind: r.try_get("", "kind").map_err(backend_err)?,
                requested: r.try_get("", "requested_value").map_err(backend_err)?,
                balance_after: r.try_get("", "balance_after").map_err(backend_err)?,
            })
        })
        .collect()
}

/// This app's rolling-24h applied-adjust total across the whole community.
async fn scalar_scope_used(
    txn: &DatabaseTransaction,
    scope: &ReputationScope,
) -> Result<i64, ReputationError> {
    let row = txn
        .query_one_raw(stmt(
            &format!(
                "SELECT COALESCE(SUM(ABS(COALESCE(requested_value, delta))), 0)::BIGINT AS used \
                 FROM bundle_reputation_adjustments \
                 WHERE tenant_id = $1 AND community_id = $2 AND app_id = $3 \
                   AND scope = 'community' AND kind = 'adjust' {WINDOW}"
            ),
            vec![
                Value::Int(Some(scope.tenant_id)),
                Value::Int(Some(scope.community_id)),
                Value::String(Some(scope.app_id.clone())),
            ],
        ))
        .await
        .map_err(backend_err)?
        .ok_or_else(|| backend_err("scope window SUM returned no row"))?;
    row.try_get("", "used").map_err(backend_err)
}

/// Writes the new balance and its ledger row (same transaction, so the score
/// can never move without a ledger row).
async fn apply_and_ledger(
    txn: &DatabaseTransaction,
    scope: &ReputationScope,
    user: Uuid,
    new_balance: i64,
    row: LedgerRow<'_>,
) -> Result<(), ReputationError> {
    let mut v = scope_values(scope, user);
    v.push(Value::BigInt(Some(new_balance)));
    txn.execute_raw(stmt(
        "UPDATE bundle_reputation_scores \
         SET balance = $4, adjustment_count = adjustment_count + 1, updated_at = NOW() \
         WHERE tenant_id = $1 AND community_id = $2 AND user_uuid = $3",
        v,
    ))
    .await
    .map_err(backend_err)?;

    let applied = i32::try_from(row.applied).map_err(backend_err)?;
    txn.execute_raw(stmt(
        "INSERT INTO bundle_reputation_adjustments \
         (app_id, tenant_id, community_id, target_user_uuid, scope, delta, reason_code, \
          kind, requested_value, balance_after, idempotency_key) \
         VALUES ($1, $2, $3, $4::text, 'community', $5, $6, $7, $8, $9, $10)",
        vec![
            Value::String(Some(scope.app_id.clone())),
            Value::Int(Some(scope.tenant_id)),
            Value::Int(Some(scope.community_id)),
            Value::Uuid(Some(user)),
            Value::Int(Some(applied)),
            Value::String(Some(row.reason.to_string())),
            Value::String(Some(row.kind.to_string())),
            Value::BigInt(Some(row.requested)),
            Value::BigInt(Some(new_balance)),
            Value::String(row.key.map(|k| k.as_str().to_string())),
        ],
    ))
    .await
    .map_err(backend_err)?;
    Ok(())
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
        caps: ReputationCaps,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.adjust_impl(scope, user, delta, reason, caps).await;
            metrics::record_call("adjust", outcome_of(&result), start.elapsed().as_secs_f64());
            result
        })
    }

    fn override_score<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        absolute_score: i64,
        reason: &'a str,
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self
                .override_impl(scope, user, absolute_score, reason, key)
                .await;
            metrics::record_call(
                "override",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }

    fn adjust_many<'a>(
        &'a self,
        scope: &'a ReputationScope,
        items: &'a [(Uuid, i32)],
        reason: &'a str,
        caps: ReputationCaps,
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<(), ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.adjust_many_impl(scope, items, reason, caps, key).await;
            metrics::record_call(
                "adjust_many",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }

    fn get_many<'a>(
        &'a self,
        scope: &'a ReputationScope,
        users: &'a [Uuid],
    ) -> BoxFuture<'a, Result<Vec<ScoreEntry>, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.get_many_impl(scope, users).await;
            metrics::record_call(
                "get_many",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }

    fn leaderboard<'a>(
        &'a self,
        scope: &'a ReputationScope,
        limit: u32,
    ) -> BoxFuture<'a, Result<Vec<ScoreEntry>, ReputationError>> {
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
/// non-members. Raising it, or replacing the snapshot with a per-community
/// lazy cache, is the follow-up if a deployment ever approaches it.
pub const MAX_SNAPSHOT_ROWS: i64 = 2_000_000;

/// Loads every active `(tenant, community, user_uuid)` membership (NULL
/// `user_uuid` rows excluded -- they cannot be a target), optionally scoped to
/// one `tenant_id` (`None` = every tenant, the multi-tenant changelog-consumer
/// path). Fails with a `DbErr::Custom` if the result would reach
/// [`MAX_SNAPSHOT_ROWS`].
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

/// Refreshes `snapshot` (scoped as in [`load_membership`]) from the DB every `interval` until the task is
/// dropped/aborted. A failed refresh keeps the previous snapshot and logs at
/// WARN: the snapshot is only a pre-filter (writes re-check live membership in
/// their own transaction), so staleness is bounded and never widens write
/// authority. The initial load happens before the first sleep so the snapshot
/// is populated promptly after startup.
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
                        "reputation membership snapshot refreshed"
                    );
                } else {
                    tracing::error!(
                        "reputation membership snapshot lock poisoned; staying fail-closed"
                    );
                }
            }
            Err(e) => {
                tracing::warn!(error = %e, ?tenant_id, "reputation membership refresh failed; keeping previous snapshot");
            }
        }
        tokio::time::sleep(interval).await;
    }
}
