//! Postgres-backed [`ReputationStore`], the membership-snapshot loader and
//! the connection factory (all under the least-privilege
//! `waddles_bundle_reputation` role, alembic 0050).
//!
//! Every WRITE goes through the SECURITY DEFINER SQL functions owned by the
//! hub-api migration (`reputation_apply`, `reputation_set_floor_soft_ban`,
//! `reputation_clear_floor_soft_ban`); this crate never INSERTs/UPDATEs a
//! reputation table and never computes caps or membership authority itself
//! -- the SQL is authoritative. Reads here are plain SELECTs.

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
    metrics, validate_delta, validate_reason, ApplyOutcome, BoxFuture, HistoryEntry,
    IdempotencyKey, ReasonClass, ReputationCaps, ReputationError, ReputationScope, ReputationStore,
    ScoreEntry, ScoreScale, TenantPolicy, TenantScore, MAX_BATCH, MAX_HISTORY_LIMIT,
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

/// The only soft-ban the bundle role may set or clear is the score-floor one
/// (`reason_code` 1); manual soft bans are hub-api-only (contract).
pub const FLOOR_SOFT_BAN_REASON: &str = "community_soft_floor";

/// The `p_actor` the host passes. The bundle role is only allowed machine
/// actors (`system:[a-z_]+`, enforced in SQL); human-attributed writes are
/// hub-api only, so this is always the fixed bundle actor.
const BUNDLE_ACTOR: &str = "system:bundle";

/// Maps a host idempotency key onto the SQL contract's `[a-z0-9._:-]`
/// alphabet with an injective escape (`.` -> `..`, uppercase `X` -> `.x`), so
/// two distinct host keys can never collide after lowercasing. Over-long
/// results are rejected, never truncated.
fn sql_key(key: &str) -> Result<String, ReputationError> {
    let mut out = String::with_capacity(key.len() + 4);
    for c in key.chars() {
        match c {
            '.' => out.push_str(".."),
            c if c.is_ascii_uppercase() => {
                out.push('.');
                out.push(c.to_ascii_lowercase());
            }
            c => out.push(c),
        }
    }
    if out.len() > crate::MAX_IDEMPOTENCY_KEY_LEN {
        return Err(ReputationError::Invalid(
            "idempotency key too long after normalisation".to_string(),
        ));
    }
    Ok(out)
}

/// Maps a database error from a reputation SQL function onto the host error
/// taxonomy. The SQL is authoritative: `not_a_member` (42501) -> `NotAMember`,
/// `invalid_input` (22023) -> `Invalid`, cap/quota errors -> `RateLimited`;
/// anything else is an opaque `Backend` error.
fn map_sql_err(e: DbErr) -> ReputationError {
    let msg = e.to_string();
    if msg.contains("not_a_member") || msg.contains("is not in the named tenant") {
        ReputationError::NotAMember
    } else if msg.contains("invalid_input") {
        ReputationError::Invalid("rejected by the reputation store".to_string())
    } else if msg.contains("cap_exceeded")
        || msg.contains("quota_exceeded")
        || msg.contains("rate_limited")
    {
        ReputationError::RateLimited { cap: 0 }
    } else {
        backend_err(msg)
    }
}

/// The one write path: calls the SECURITY DEFINER `reputation_apply()` in the
/// caller's transaction. The function owns membership, caps, idempotency,
/// clamping, the ledger row and the floor soft-ban auto-clear; this crate
/// never writes a reputation table.
#[allow(clippy::too_many_arguments)]
async fn call_apply(
    txn: &DatabaseTransaction,
    scope: &ReputationScope,
    user: Uuid,
    delta: i32,
    class_code: i16,
    source_event: &str,
    key: &str,
) -> Result<ApplyOutcome, ReputationError> {
    let row = txn
        .query_one_raw(stmt(
            "SELECT new_score::BIGINT AS new_score, applied \
             FROM reputation_apply($1::bigint, $2::bigint, $3::uuid, $4::int, \
                                   $5::smallint, $6::smallint, $7::text, $8::text, \
                                   $9::text, $10::text)",
            vec![
                Value::BigInt(Some(i64::from(scope.tenant_id))),
                Value::BigInt(Some(i64::from(scope.community_id))),
                Value::Uuid(Some(user)),
                Value::Int(Some(delta)),
                Value::SmallInt(Some(class_code)),
                // reason_code mirrors the class (the free-text reason rides in
                // `source_event`).
                Value::SmallInt(Some(class_code)),
                Value::String(Some(scope.app_id.clone())),
                Value::String(Some(BUNDLE_ACTOR.to_string())),
                Value::String(Some(source_event.to_string())),
                Value::String(Some(key.to_string())),
            ],
        ))
        .await
        .map_err(map_sql_err)?
        .ok_or_else(|| backend_err("reputation_apply returned no row"))?;
    Ok(ApplyOutcome {
        new_score: row.try_get("", "new_score").map_err(backend_err)?,
        applied: row.try_get("", "applied").map_err(backend_err)?,
    })
}

/// `COALESCE(balance, community policy score_default, 600)`: the one reading
/// of an unscored member's score (contract: get, leaderboard, at_risk,
/// below_threshold all agree). Needs `s` (scores) and `p` (policy) joined.
const SCORE_EXPR: &str = "COALESCE(s.balance, p.score_default, 600)::BIGINT";

/// Joins shared by every score read: members, their community, the optional
/// score row and the optional per-community policy.
const SCORE_JOINS: &str = "FROM community_members cm \
     JOIN communities c ON c.id = cm.community_id \
     LEFT JOIN bundle_reputation_scores s \
       ON s.community_id = cm.community_id AND s.user_uuid = cm.user_uuid \
      AND s.tenant_id = c.tenant_id \
     LEFT JOIN bundle_reputation_policy p \
       ON p.community_id = cm.community_id AND p.tenant_id = c.tenant_id";

/// The production [`ReputationStore`].
pub struct PostgresReputationStore {
    conn: DatabaseConnection,
}

impl PostgresReputationStore {
    pub fn new(conn: DatabaseConnection) -> Self {
        Self { conn }
    }

    /// Opens a transaction with the statement timeout set.
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

    async fn get_impl(&self, scope: &ReputationScope, user: Uuid) -> Result<i64, ReputationError> {
        let sql = format!("SELECT {SCORE_EXPR} AS balance {SCORE_JOINS} WHERE {MEMBER_PREDICATE}");
        let row = self
            .conn
            .query_one_raw(stmt(&sql, scope_values(scope, user)))
            .await
            .map_err(backend_err)?
            .ok_or(ReputationError::NotAMember)?;
        row.try_get::<i64>("", "balance").map_err(backend_err)
    }

    /// Activity-class delta through `reputation_apply()`. Caps and membership
    /// are enforced inside the SQL function (authoritative); `_caps` is the
    /// gate's in-memory quota and is deliberately not re-computed here. The
    /// idempotency `key` is required, so a retried call is never double-counted.
    async fn adjust_impl(
        &self,
        scope: &ReputationScope,
        user: Uuid,
        delta: i32,
        reason: &str,
        _caps: ReputationCaps,
        key: &IdempotencyKey,
    ) -> Result<i64, ReputationError> {
        validate_reason(reason)?;
        validate_delta(delta)?;
        let key = sql_key(key.as_str())?;
        let txn = self.begin().await?;
        let out = call_apply(
            &txn,
            scope,
            user,
            delta,
            ReasonClass::Activity.code(),
            reason,
            &key,
        )
        .await?;
        txn.commit().await.map_err(backend_err)?;
        metrics::record_applied_delta(i64::from(delta).unsigned_abs());
        Ok(out.new_score)
    }

    /// All-or-nothing batch: one `reputation_apply()` per item in ONE
    /// transaction (sorted by user for a deterministic key/lock order), each
    /// under `"{key}:{index}"`. A replayed batch is a no-op per row.
    async fn adjust_many_impl(
        &self,
        scope: &ReputationScope,
        items: &[(Uuid, i32)],
        reason: &str,
        _caps: ReputationCaps,
        key: &IdempotencyKey,
    ) -> Result<(), ReputationError> {
        validate_reason(reason)?;
        if items.is_empty() || items.len() > MAX_BATCH {
            return Err(ReputationError::Invalid(format!(
                "batch must hold 1..={MAX_BATCH} items"
            )));
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
        let mut sorted = items.to_vec();
        sorted.sort_by_key(|(u, _)| *u);
        let base = sql_key(key.as_str())?;
        let txn = self.begin().await?;
        let mut applied_abs = Vec::with_capacity(sorted.len());
        for (i, (user, delta)) in sorted.iter().enumerate() {
            let row_key = format!("{base}:{i}");
            if row_key.len() > crate::MAX_IDEMPOTENCY_KEY_LEN {
                return Err(ReputationError::Invalid(
                    "idempotency key too long for a batch".to_string(),
                ));
            }
            let out = call_apply(
                &txn,
                scope,
                *user,
                *delta,
                ReasonClass::Activity.code(),
                reason,
                &row_key,
            )
            .await?;
            if out.applied {
                applied_abs.push(i64::from(*delta).unsigned_abs());
            }
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
                    "SELECT cm.user_uuid AS user_uuid, {SCORE_EXPR} AS balance {SCORE_JOINS} \
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

    /// Top `limit` ACTIVE members, unscored members counted at the policy
    /// default (contract: one reading of "unscored" everywhere).
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
                &format!(
                    "SELECT cm.user_uuid AS user_uuid, {SCORE_EXPR} AS balance {SCORE_JOINS} \
                     WHERE {ACTIVE_MEMBER_PREDICATE} AND cm.user_uuid IS NOT NULL \
                     ORDER BY balance DESC, cm.user_uuid ASC LIMIT $3"
                ),
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

    /// Classed, idempotent delta through `reputation_apply()`. The class is
    /// host-derived from the granted permission. A positive moderation delta
    /// is refused here (it would be class 3, which the bundle role may not
    /// call). Caps are enforced in SQL; `_caps` is not re-computed here.
    #[allow(clippy::too_many_arguments)]
    async fn apply_impl(
        &self,
        scope: &ReputationScope,
        user: Uuid,
        delta: i32,
        class: ReasonClass,
        source_event: &str,
        _caps: ReputationCaps,
        key: &IdempotencyKey,
    ) -> Result<ApplyOutcome, ReputationError> {
        validate_reason(source_event)?;
        ScoreScale::LEGACY.check_delta(class, delta)?;
        if class == ReasonClass::Moderation && delta > 0 {
            // A positive moderation delta is an admin_override (class 3), which
            // the bundle role is denied: hub-api only.
            return Err(ReputationError::Invalid(
                "a positive moderation delta is an admin override (hub-api only)".to_string(),
            ));
        }
        let class_code = class.code();
        let sql_key = sql_key(key.as_str())?;
        let txn = self.begin().await?;
        let outcome =
            call_apply(&txn, scope, user, delta, class_code, source_event, &sql_key).await?;
        txn.commit().await.map_err(backend_err)?;
        if outcome.applied {
            metrics::record_applied_delta(i64::from(delta).unsigned_abs());
        }
        Ok(outcome)
    }

    /// The user's own aggregate over this tenant's communities.
    async fn get_tenant_impl(
        &self,
        scope: &ReputationScope,
        user: Uuid,
    ) -> Result<TenantScore, ReputationError> {
        let row = self
            .conn
            .query_one_raw(stmt(
                "SELECT COALESCE(ROUND(AVG(s.balance))::BIGINT, $3) AS score, \
                        COUNT(s.user_uuid)::BIGINT AS n, \
                        BOOL_OR(TRUE) AS member \
                 FROM community_members cm \
                 JOIN communities c ON c.id = cm.community_id \
                 LEFT JOIN bundle_reputation_scores s \
                   ON s.tenant_id = c.tenant_id AND s.community_id = cm.community_id \
                  AND s.user_uuid = cm.user_uuid \
                 WHERE c.tenant_id = $1 AND cm.user_uuid = $2 \
                   AND cm.is_active IS TRUE AND cm.removed_at IS NULL AND cm.left_at IS NULL",
                vec![
                    Value::Int(Some(scope.tenant_id)),
                    Value::Uuid(Some(user)),
                    Value::BigInt(Some(crate::SCALE_DEFAULT)),
                ],
            ))
            .await
            .map_err(backend_err)?
            .ok_or(ReputationError::NotAMember)?;
        let member: Option<bool> = row.try_get("", "member").map_err(backend_err)?;
        if member != Some(true) {
            return Err(ReputationError::NotAMember);
        }
        let n: i64 = row.try_get("", "n").map_err(backend_err)?;
        Ok(TenantScore {
            score: row.try_get("", "score").map_err(backend_err)?,
            communities: u32::try_from(n).map_err(backend_err)?,
        })
    }

    /// Newest-first ledger page.
    async fn history_impl(
        &self,
        scope: &ReputationScope,
        user: Uuid,
        limit: u32,
        before: Option<i64>,
    ) -> Result<Vec<HistoryEntry>, ReputationError> {
        if limit == 0 || limit > MAX_HISTORY_LIMIT {
            return Err(ReputationError::Invalid(format!(
                "limit must be 1..={MAX_HISTORY_LIMIT}"
            )));
        }
        let rows = self
            .conn
            .query_all_raw(stmt(
                "SELECT id, delta, reason_code::text AS reason_code, \
                        COALESCE(kind, 'adjust') AS kind, balance_after, \
                        to_char(occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS ts \
                 FROM bundle_reputation_adjustments \
                 WHERE tenant_id = $1 AND community_id = $2 AND target_user_uuid = $3 \
                   AND ($4::bigint IS NULL OR id < $4) \
                 ORDER BY id DESC LIMIT $5",
                vec![
                    Value::Int(Some(scope.tenant_id)),
                    Value::Int(Some(scope.community_id)),
                    Value::String(Some(user.to_string())),
                    Value::BigInt(before),
                    Value::BigInt(Some(i64::from(limit))),
                ],
            ))
            .await
            .map_err(backend_err)?;
        rows.into_iter()
            .map(|r| {
                Ok(HistoryEntry {
                    id: r.try_get("", "id").map_err(backend_err)?,
                    delta: r.try_get("", "delta").map_err(backend_err)?,
                    reason_code: r.try_get("", "reason_code").map_err(backend_err)?,
                    kind: r.try_get("", "kind").map_err(backend_err)?,
                    balance_after: r.try_get("", "balance_after").map_err(backend_err)?,
                    occurred_at: r.try_get("", "ts").map_err(backend_err)?,
                })
            })
            .collect()
    }

    /// Active members strictly below `threshold`, lowest first; unscored
    /// members are counted at the policy default.
    async fn below_threshold_impl(
        &self,
        scope: &ReputationScope,
        threshold: i64,
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
                &format!(
                    "SELECT cm.user_uuid AS user_uuid, {SCORE_EXPR} AS balance {SCORE_JOINS} \
                     WHERE {ACTIVE_MEMBER_PREDICATE} AND cm.user_uuid IS NOT NULL \
                       AND {SCORE_EXPR} < $3 \
                     ORDER BY balance ASC, cm.user_uuid ASC LIMIT $4"
                ),
                vec![
                    Value::Int(Some(scope.tenant_id)),
                    Value::Int(Some(scope.community_id)),
                    Value::BigInt(Some(threshold)),
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

    /// The tenant's auto-ban policy from `bundle_reputation_tenant_policy`
    /// (read-only grant); `None` = no row.
    async fn tenant_policy_impl(
        &self,
        scope: &ReputationScope,
    ) -> Result<Option<TenantPolicy>, ReputationError> {
        let row = self
            .conn
            .query_one_raw(stmt(
                "SELECT tenant_auto_ban_enabled, tenant_auto_ban_floor::BIGINT AS tenant_auto_ban_floor, \
                        tenant_at_risk_threshold::BIGINT AS tenant_at_risk_threshold \
                 FROM bundle_reputation_tenant_policy WHERE tenant_id = $1",
                vec![Value::Int(Some(scope.tenant_id))],
            ))
            .await
            .map_err(backend_err)?;
        let Some(r) = row else { return Ok(None) };
        let policy = TenantPolicy {
            auto_ban_enabled: r
                .try_get("", "tenant_auto_ban_enabled")
                .map_err(backend_err)?,
            auto_ban_floor: r
                .try_get("", "tenant_auto_ban_floor")
                .map_err(backend_err)?,
            at_risk_threshold: r
                .try_get("", "tenant_at_risk_threshold")
                .map_err(backend_err)?,
        };
        policy.validate()?;
        Ok(Some(policy))
    }

    /// The tenant's active reputation-logic app id via the SQL function
    /// `reputation_active_logic_app(p_tenant)` (NULL = none).
    async fn active_logic_app_impl(
        &self,
        scope: &ReputationScope,
    ) -> Result<Option<String>, ReputationError> {
        let row = self
            .conn
            .query_one_raw(stmt(
                "SELECT reputation_active_logic_app($1::bigint) AS app",
                vec![Value::BigInt(Some(i64::from(scope.tenant_id)))],
            ))
            .await
            .map_err(map_sql_err)?
            .ok_or_else(|| backend_err("reputation_active_logic_app returned no row"))?;
        row.try_get::<Option<String>>("", "app")
            .map_err(backend_err)
    }

    /// Soft-ban state via `reputation_is_soft_banned()` (expiry-aware, SQL
    /// authoritative).
    async fn is_soft_banned_impl(
        &self,
        scope: &ReputationScope,
        user: Uuid,
    ) -> Result<bool, ReputationError> {
        let row = self
            .conn
            .query_one_raw(stmt(
                "SELECT reputation_is_soft_banned($1::bigint, $2::bigint, $3::uuid) AS banned",
                vec![
                    Value::BigInt(Some(i64::from(scope.tenant_id))),
                    Value::BigInt(Some(i64::from(scope.community_id))),
                    Value::Uuid(Some(user)),
                ],
            ))
            .await
            .map_err(map_sql_err)?
            .ok_or_else(|| backend_err("reputation_is_soft_banned returned no row"))?;
        row.try_get::<bool>("", "banned").map_err(backend_err)
    }

    /// Sets the FLOOR soft ban (reason code 1) through the bundle-role wrapper
    /// `reputation_set_floor_soft_ban()` -- the only soft-ban write this role
    /// may make; manual soft bans are hub-api-only. `Ok(true)` when the user
    /// was not banned before the call.
    async fn soft_ban_impl(
        &self,
        scope: &ReputationScope,
        user: Uuid,
        reason: &str,
    ) -> Result<bool, ReputationError> {
        validate_reason(reason)?;
        if reason != FLOOR_SOFT_BAN_REASON {
            return Err(ReputationError::Invalid(
                "only the floor soft ban may be set by a bundle".to_string(),
            ));
        }
        let was = self.is_soft_banned_impl(scope, user).await?;
        let txn = self.begin().await?;
        let set = txn
            .execute_raw(stmt(
                "SELECT reputation_set_floor_soft_ban($1::bigint, $2::bigint, $3::uuid)",
                vec![
                    Value::BigInt(Some(i64::from(scope.tenant_id))),
                    Value::BigInt(Some(i64::from(scope.community_id))),
                    Value::Uuid(Some(user)),
                ],
            ))
            .await;
        if let Err(e) = set {
            // The SQL only sets a floor ban when auto-ban is enabled and the
            // score is below the floor; otherwise nothing was set.
            if e.to_string().contains("floor_not_breached") {
                return Ok(false);
            }
            return Err(map_sql_err(e));
        }
        txn.commit().await.map_err(backend_err)?;
        Ok(!was)
    }

    /// Clears the FLOOR soft ban only (`reputation_clear_floor_soft_ban()`;
    /// a manual ban is untouched). `Ok(true)` when a ban existed before and is
    /// gone after. The lift quota and the "score recovered" check live in SQL:
    /// `floor_still_breached` -> `Ok(false)` (not recovered, never aborts
    /// accrual), `quota_exceeded` -> `RateLimited`.
    async fn lift_soft_ban_impl(
        &self,
        scope: &ReputationScope,
        user: Uuid,
        only_reason: &str,
    ) -> Result<bool, ReputationError> {
        validate_reason(only_reason)?;
        if only_reason != FLOOR_SOFT_BAN_REASON {
            return Err(ReputationError::Invalid(
                "only the floor soft ban may be cleared by a bundle".to_string(),
            ));
        }
        let txn = self.begin().await?;
        let res = txn
            .query_one_raw(stmt(
                "SELECT reputation_clear_floor_soft_ban($1::bigint, $2::bigint, $3::uuid) AS cleared",
                vec![
                    Value::BigInt(Some(i64::from(scope.tenant_id))),
                    Value::BigInt(Some(i64::from(scope.community_id))),
                    Value::Uuid(Some(user)),
                ],
            ))
            .await;
        let row = match res {
            Ok(r) => {
                r.ok_or_else(|| backend_err("reputation_clear_floor_soft_ban returned no row"))?
            }
            Err(e) if e.to_string().contains("floor_still_breached") => return Ok(false),
            Err(e) => return Err(map_sql_err(e)),
        };
        let cleared: bool = row.try_get("", "cleared").map_err(backend_err)?;
        txn.commit().await.map_err(backend_err)?;
        Ok(cleared)
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
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self
                .adjust_impl(scope, user, delta, reason, caps, key)
                .await;
            metrics::record_call("adjust", outcome_of(&result), start.elapsed().as_secs_f64());
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

    fn apply<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        delta: i32,
        class: ReasonClass,
        source_event: &'a str,
        caps: ReputationCaps,
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<ApplyOutcome, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self
                .apply_impl(scope, user, delta, class, source_event, caps, key)
                .await;
            metrics::record_call("apply", outcome_of(&result), start.elapsed().as_secs_f64());
            result
        })
    }

    fn get_tenant<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
    ) -> BoxFuture<'a, Result<TenantScore, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.get_tenant_impl(scope, user).await;
            metrics::record_call(
                "get_tenant",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }

    fn history<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        limit: u32,
        before: Option<i64>,
    ) -> BoxFuture<'a, Result<Vec<HistoryEntry>, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.history_impl(scope, user, limit, before).await;
            metrics::record_call(
                "history",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }

    fn below_threshold<'a>(
        &'a self,
        scope: &'a ReputationScope,
        threshold: i64,
        limit: u32,
    ) -> BoxFuture<'a, Result<Vec<ScoreEntry>, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.below_threshold_impl(scope, threshold, limit).await;
            metrics::record_call(
                "below_threshold",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }

    fn tenant_policy<'a>(
        &'a self,
        scope: &'a ReputationScope,
    ) -> BoxFuture<'a, Result<Option<TenantPolicy>, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.tenant_policy_impl(scope).await;
            metrics::record_call(
                "tenant_policy",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }

    fn is_soft_banned<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
    ) -> BoxFuture<'a, Result<bool, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.is_soft_banned_impl(scope, user).await;
            metrics::record_call(
                "is_soft_banned",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }

    fn active_logic_app<'a>(
        &'a self,
        scope: &'a ReputationScope,
    ) -> BoxFuture<'a, Result<Option<String>, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.active_logic_app_impl(scope).await;
            metrics::record_call(
                "active_logic_app",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }

    fn soft_ban<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        reason: &'a str,
    ) -> BoxFuture<'a, Result<bool, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.soft_ban_impl(scope, user, reason).await;
            metrics::record_call(
                "soft_ban",
                outcome_of(&result),
                start.elapsed().as_secs_f64(),
            );
            result
        })
    }

    fn lift_soft_ban<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        only_reason: &'a str,
    ) -> BoxFuture<'a, Result<bool, ReputationError>> {
        Box::pin(async move {
            let start = Instant::now();
            let result = self.lift_soft_ban_impl(scope, user, only_reason).await;
            metrics::record_call(
                "lift_soft_ban",
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
