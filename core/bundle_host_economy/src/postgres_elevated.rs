//! The ELEVATED app-caller primitives of [`PostgresEconomyStore`]:
//! `transfer_from` (move a NON-actor member's funds, one atomic transaction)
//! and the `elevated_reserve` / `elevated_settle` pair that wraps every other
//! dangerous primitive an app reaches through the economy service bundle.
//!
//! Trust model: the economy service bundle is I/O; the grant that matters is
//! the CALLING app's `economy.transfer_from` / `economy.app_mint`, read here
//! from `community_permission_grants` INSIDE the write transaction (so a stale
//! host cache can never authorize a revoked grant) together with the approving
//! admin and the admin-set caps (`params_json`). Every call is serialized per
//! `(tenant, community, calling app)` by a transaction-scoped advisory lock
//! (always the transaction's FIRST lock, so it cannot cycle with the row
//! locks), the caps are summed from `economy_elevated_audit` under that lock,
//! and every call leaves an audit row naming the app, the approving grant and
//! the parties (UUIDs only).

use sea_orm::{ConnectionTrait, TransactionTrait, Value};
use uuid::Uuid;

use super::{
    backend_err, lock_scope, scope_values, stmt, write_err, PostgresEconomyStore, MEMBER_PREDICATE,
    STATEMENT_TIMEOUT_MS,
};
use crate::{
    metrics, validate_reason, EconomyError, EconomyScope, ElevatedCaps, ElevatedPermission,
    ElevatedRequest, TransferFromOutcome,
};

/// Audit `status` codes (`economy_elevated_audit.status`).
const STATUS_RESERVED: i16 = 1;
const STATUS_APPLIED: i16 = 2;
const STATUS_FAILED: i16 = 3;

/// The approving grant of an elevated call and the caps it carries.
struct GrantRow {
    grant_id: i64,
    approved_by: Option<i32>,
    caps: ElevatedCaps,
}

/// An audit row found by its call key.
struct AuditRow {
    id: i64,
    op: String,
    target: Uuid,
    counterparty: Option<Uuid>,
    amount: i64,
    reason: String,
    status: i16,
    from_balance: Option<i64>,
    to_balance: Option<i64>,
}

/// SQL extracting one numeric cap from the grant's `params_json`: only a JSON
/// number within a sane range counts (nested CASE keeps the cast from ever
/// seeing a string); anything else is NULL and falls back to the default.
fn cap_expr(name: &str) -> String {
    format!(
        "CASE WHEN jsonb_typeof(params_json->'{name}') = 'number' THEN \
            CASE WHEN (params_json #>> '{{{name}}}')::numeric BETWEEN 1 AND 1000000000 \
                 THEN floor((params_json #>> '{{{name}}}')::numeric)::BIGINT END \
         END AS {name}"
    )
}

/// Reads the calling app's live grant for `permission` in this community.
async fn load_grant<C: ConnectionTrait>(
    conn: &C,
    scope: &EconomyScope,
    caller_app: &str,
    permission: ElevatedPermission,
) -> Result<GrantRow, EconomyError> {
    let sql = format!(
        "SELECT grant_id, granted_by, {}, {}, {} \
         FROM community_permission_grants \
         WHERE tenant_id = $1 AND community_id = $2 AND app_id = $3 \
           AND permission_id = $4 AND revoked_at IS NULL",
        cap_expr("max_per_op"),
        cap_expr("max_per_day"),
        cap_expr("max_per_target_per_day"),
    );
    let Some(row) = conn
        .query_one_raw(stmt(
            &sql,
            vec![
                Value::Int(Some(scope.tenant_id)),
                Value::Int(Some(scope.community_id)),
                Value::String(Some(caller_app.to_string())),
                Value::String(Some(permission.id().to_string())),
            ],
        ))
        .await
        .map_err(backend_err)?
    else {
        tracing::warn!(
            tenant_id = scope.tenant_id,
            community_id = scope.community_id,
            caller_app_id = %caller_app,
            permission = permission.id(),
            "elevated economy call refused: the calling app holds no live grant"
        );
        return Err(EconomyError::ElevatedNotGranted);
    };
    let grant_id = row
        .try_get::<Option<i64>>("", "grant_id")
        .map_err(backend_err)?
        .ok_or_else(|| backend_err("grant row has no grant_id (migration 0082 not applied)"))?;
    let opt = |name: &str| -> Result<Option<i64>, EconomyError> {
        row.try_get::<Option<i64>>("", name).map_err(backend_err)
    };
    Ok(GrantRow {
        grant_id,
        approved_by: row
            .try_get::<Option<i32>>("", "granted_by")
            .map_err(backend_err)?,
        caps: ElevatedCaps::from_raw(
            opt("max_per_op")?,
            opt("max_per_day")?,
            opt("max_per_target_per_day")?,
        ),
    })
}

/// Sums the calling app's counted (reserved + applied) amounts of the last 24h,
/// optionally restricted to one target member and excluding one audit row.
async fn used_24h<C: ConnectionTrait>(
    conn: &C,
    scope: &EconomyScope,
    caller_app: &str,
    permission: ElevatedPermission,
    target: Option<Uuid>,
    exclude_id: Option<i64>,
) -> Result<i64, EconomyError> {
    let sql = "SELECT COALESCE(SUM(amount), 0)::BIGINT AS used FROM economy_elevated_audit \
               WHERE tenant_id = $1 AND community_id = $2 AND caller_app_id = $3 \
                 AND permission_id = $4 AND status IN (1, 2) \
                 AND created_at > NOW() - INTERVAL '24 hours' \
                 AND ($5::UUID IS NULL OR target_user = $5::UUID) \
                 AND ($6::BIGINT IS NULL OR id <> $6::BIGINT)";
    conn.query_one_raw(stmt(
        sql,
        vec![
            Value::Int(Some(scope.tenant_id)),
            Value::Int(Some(scope.community_id)),
            Value::String(Some(caller_app.to_string())),
            Value::String(Some(permission.id().to_string())),
            Value::Uuid(target),
            Value::BigInt(exclude_id),
        ],
    ))
    .await
    .map_err(backend_err)?
    .ok_or_else(|| backend_err("elevated window SUM returned no row"))?
    .try_get("", "used")
    .map_err(backend_err)
}

/// Enforces the grant's per-call, per-app-per-day and per-target-per-day caps
/// for `req`. MUST run under the advisory lock for `(community, caller app)`.
async fn enforce_caps<C: ConnectionTrait>(
    conn: &C,
    scope: &EconomyScope,
    req: &ElevatedRequest<'_>,
    caps: ElevatedCaps,
    exclude_id: Option<i64>,
) -> Result<(), EconomyError> {
    if req.amount > caps.max_per_op {
        return Err(EconomyError::ElevatedOverCap {
            cap: caps.max_per_op,
        });
    }
    let day = used_24h(
        conn,
        scope,
        req.caller_app,
        req.permission,
        None,
        exclude_id,
    )
    .await?;
    if day.saturating_add(req.amount) > caps.max_per_day {
        return Err(EconomyError::ElevatedQuotaExceeded {
            cap: caps.max_per_day,
        });
    }
    let per_target = used_24h(
        conn,
        scope,
        req.caller_app,
        req.permission,
        Some(req.target),
        exclude_id,
    )
    .await?;
    if per_target.saturating_add(req.amount) > caps.max_per_target_per_day {
        return Err(EconomyError::ElevatedQuotaExceeded {
            cap: caps.max_per_target_per_day,
        });
    }
    Ok(())
}

/// Looks up the audit row `req.key` already produced for the calling app.
async fn find_audit<C: ConnectionTrait>(
    conn: &C,
    scope: &EconomyScope,
    req: &ElevatedRequest<'_>,
) -> Result<Option<AuditRow>, EconomyError> {
    let Some(row) = conn
        .query_one_raw(stmt(
            "SELECT id, op, target_user, counterparty_user, amount, reason, status, \
                    from_balance_after, to_balance_after \
             FROM economy_elevated_audit \
             WHERE tenant_id = $1 AND community_id = $2 AND caller_app_id = $3 \
               AND idempotency_key = $4",
            vec![
                Value::Int(Some(scope.tenant_id)),
                Value::Int(Some(scope.community_id)),
                Value::String(Some(req.caller_app.to_string())),
                Value::String(Some(req.key.as_str().to_string())),
            ],
        ))
        .await
        .map_err(backend_err)?
    else {
        return Ok(None);
    };
    Ok(Some(AuditRow {
        id: row.try_get("", "id").map_err(backend_err)?,
        op: row.try_get("", "op").map_err(backend_err)?,
        target: row.try_get("", "target_user").map_err(backend_err)?,
        counterparty: row
            .try_get::<Option<Uuid>>("", "counterparty_user")
            .map_err(backend_err)?,
        amount: row.try_get("", "amount").map_err(backend_err)?,
        reason: row.try_get("", "reason").map_err(backend_err)?,
        status: row.try_get("", "status").map_err(backend_err)?,
        from_balance: row
            .try_get::<Option<i64>>("", "from_balance_after")
            .map_err(backend_err)?,
        to_balance: row
            .try_get::<Option<i64>>("", "to_balance_after")
            .map_err(backend_err)?,
    }))
}

impl AuditRow {
    /// `true` iff this prior audit row is exactly the call now being asked for.
    fn matches(&self, req: &ElevatedRequest<'_>) -> bool {
        self.op == req.op
            && self.target == req.target
            && self.counterparty == req.counterparty
            && self.amount == req.amount
            && self.reason == req.reason
    }
}

/// Logs (PII-free) and builds the conflict error for a reused call key.
fn audit_conflict(scope: &EconomyScope, req: &ElevatedRequest<'_>) -> EconomyError {
    tracing::warn!(
        op = req.op,
        tenant_id = scope.tenant_id,
        community_id = scope.community_id,
        caller_app_id = %req.caller_app,
        idempotency_key = %req.key,
        "elevated economy idempotency conflict: key already used by a different call"
    );
    EconomyError::IdempotencyConflict
}

/// Begins a transaction with the shared statement timeout.
async fn begin(store: &PostgresEconomyStore) -> Result<sea_orm::DatabaseTransaction, EconomyError> {
    let txn = store.conn.begin().await.map_err(backend_err)?;
    txn.execute_raw(stmt(
        &format!("SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"),
        vec![],
    ))
    .await
    .map_err(backend_err)?;
    Ok(txn)
}

/// The advisory-lock kind of one calling app's elevated window.
fn lock_kind(caller_app: &str) -> String {
    format!("elevated:{caller_app}")
}

/// Pure argument validation shared by both entry points.
fn validate(req: &ElevatedRequest<'_>, min_amount: i64) -> Result<(), EconomyError> {
    if req.caller_app.is_empty() || req.caller_app.len() > 255 {
        return Err(EconomyError::Invalid("caller app id is invalid".into()));
    }
    if req.amount < min_amount {
        return Err(EconomyError::Invalid(format!(
            "amount must be >= {min_amount}"
        )));
    }
    validate_reason(req.reason)?;
    Ok(())
}

impl PostgresEconomyStore {
    /// See [`crate::EconomyStore::transfer_from`].
    pub(super) async fn transfer_from_impl(
        &self,
        scope: &EconomyScope,
        req: &ElevatedRequest<'_>,
    ) -> Result<TransferFromOutcome, EconomyError> {
        validate(req, 1)?;
        let (from, Some(to)) = (req.target, req.counterparty) else {
            return Err(EconomyError::Invalid(
                "transfer_from needs a recipient".into(),
            ));
        };
        if from == to {
            return Err(EconomyError::Invalid(
                "cannot transfer to oneself".to_string(),
            ));
        }
        if req.permission != ElevatedPermission::TransferFrom {
            return Err(EconomyError::Invalid(
                "transfer_from requires the transfer_from permission".into(),
            ));
        }
        let amount = req.amount;

        let txn = begin(self).await?;
        // 0. Advisory lock (first lock), then the caller's live grant.
        lock_scope(&txn, scope, &lock_kind(req.caller_app)).await?;
        let grant = load_grant(&txn, scope, req.caller_app, req.permission).await?;

        // 1. Idempotency before any cap or balance check.
        if let Some(prior) = find_audit(&txn, scope, req).await? {
            return if prior.matches(req) && prior.status == STATUS_APPLIED {
                match (prior.from_balance, prior.to_balance) {
                    (Some(from_balance), Some(to_balance)) => {
                        metrics::record_replay("transfer_from");
                        Ok(TransferFromOutcome {
                            from_balance,
                            to_balance,
                            audit_id: prior.id,
                            grant_id: grant.grant_id,
                        })
                    }
                    _ => Err(backend_err("applied audit row has no balances")),
                }
            } else {
                Err(audit_conflict(scope, req))
            };
        }

        // 2. Admin-set caps (per call, per app per day, per victim per day).
        enforce_caps(&txn, scope, req, grant.caps, None).await?;

        // 3. Live membership of BOTH sides, inside the write transaction.
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

        // 4. Ensure the recipient's row, lock both rows in uuid order.
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

        // 5. One statement: guarded debit, credit, both ledger rows.
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
                      balance_after, idempotency_key) \
                 SELECT $1::INT, $2::INT, $6::TEXT, $3::UUID, $5::UUID, 'transfer_from', \
                        -$4::BIGINT, debit.balance, $7::TEXT FROM debit \
             ), led_in AS ( \
                 INSERT INTO economy_ledger \
                     (tenant_id, community_id, app_id, user_uuid, counterparty_uuid, kind, delta, \
                      balance_after) \
                 SELECT $1::INT, $2::INT, $6::TEXT, $5::UUID, $3::UUID, 'transfer_from_in', \
                        $4::BIGINT, credit.balance FROM credit \
             ) \
             SELECT (SELECT balance FROM debit) AS from_balance, \
                    (SELECT balance FROM credit) AS to_balance";
        let mut values = scope_values(scope, from);
        values.push(Value::BigInt(Some(amount)));
        values.push(Value::Uuid(Some(to)));
        values.push(Value::String(Some(scope.app_id.clone())));
        values.push(Value::String(Some(req.key.as_str().to_string())));
        let row = txn
            .query_one_raw(stmt(sql, values))
            .await
            .map_err(write_err)?
            .ok_or_else(|| backend_err("transfer_from statement returned no row"))?;
        let from_balance: Option<i64> = row.try_get("", "from_balance").map_err(backend_err)?;
        let to_balance: Option<i64> = row.try_get("", "to_balance").map_err(backend_err)?;
        let (from_balance, to_balance) = match (from_balance, to_balance) {
            (Some(f), Some(t)) => (f, t),
            (None, _) => {
                return Err(Self::diagnose_refusal(&txn, scope, from).await?);
            }
            (Some(_), None) => {
                return Err(backend_err("recipient balance row vanished mid-transfer"));
            }
        };

        // 6. The audit row, in the SAME transaction as the move.
        let audit_id = insert_audit(
            &txn,
            scope,
            req,
            &grant,
            STATUS_APPLIED,
            Some(from_balance),
            Some(to_balance),
        )
        .await?;
        txn.commit().await.map_err(backend_err)?;
        metrics::record_applied_amount("transfer_from", amount);
        tracing::info!(
            tenant_id = scope.tenant_id,
            community_id = scope.community_id,
            caller_app_id = %req.caller_app,
            grant_id = grant.grant_id,
            audit_id,
            amount,
            "elevated transfer_from applied"
        );
        Ok(TransferFromOutcome {
            from_balance,
            to_balance,
            audit_id,
            grant_id: grant.grant_id,
        })
    }

    /// See [`crate::EconomyStore::elevated_reserve`].
    pub(super) async fn elevated_reserve_impl(
        &self,
        scope: &EconomyScope,
        req: &ElevatedRequest<'_>,
    ) -> Result<i64, EconomyError> {
        validate(req, 0)?;
        let txn = begin(self).await?;
        lock_scope(&txn, scope, &lock_kind(req.caller_app)).await?;
        let grant = load_grant(&txn, scope, req.caller_app, req.permission).await?;

        if let Some(prior) = find_audit(&txn, scope, req).await? {
            if !prior.matches(req) {
                return Err(audit_conflict(scope, req));
            }
            if prior.status != STATUS_FAILED {
                // A replay of a call already admitted: counted once, never twice.
                txn.commit().await.map_err(backend_err)?;
                metrics::record_replay("elevated_reserve");
                return Ok(prior.id);
            }
            // A previously FAILED attempt retried under the same key: re-admit
            // it (caps re-checked without its own, uncounted, row).
            enforce_caps(&txn, scope, req, grant.caps, Some(prior.id)).await?;
            txn.execute_raw(stmt(
                "UPDATE economy_elevated_audit SET status = $2, settled_at = NULL \
                 WHERE id = $1",
                vec![
                    Value::BigInt(Some(prior.id)),
                    Value::SmallInt(Some(STATUS_RESERVED)),
                ],
            ))
            .await
            .map_err(backend_err)?;
            txn.commit().await.map_err(backend_err)?;
            return Ok(prior.id);
        }

        enforce_caps(&txn, scope, req, grant.caps, None).await?;
        let id = insert_audit(&txn, scope, req, &grant, STATUS_RESERVED, None, None).await?;
        txn.commit().await.map_err(backend_err)?;
        metrics::record_applied_amount("elevated_reserve", req.amount);
        Ok(id)
    }

    /// See [`crate::EconomyStore::elevated_settle`].
    pub(super) async fn elevated_settle_impl(
        &self,
        scope: &EconomyScope,
        audit_id: i64,
        applied: bool,
    ) -> Result<(), EconomyError> {
        let status = if applied {
            STATUS_APPLIED
        } else {
            STATUS_FAILED
        };
        self.conn
            .execute_raw(stmt(
                "UPDATE economy_elevated_audit SET status = $2, settled_at = NOW() \
                 WHERE id = $1 AND tenant_id = $3 AND community_id = $4 AND status = 1",
                vec![
                    Value::BigInt(Some(audit_id)),
                    Value::SmallInt(Some(status)),
                    Value::Int(Some(scope.tenant_id)),
                    Value::Int(Some(scope.community_id)),
                ],
            ))
            .await
            .map_err(backend_err)?;
        Ok(())
    }
}

/// Inserts the audit row of `req` under `grant`; the unique call-key index is
/// the backstop against a double insert (surfaces as an idempotency conflict).
async fn insert_audit<C: ConnectionTrait>(
    conn: &C,
    scope: &EconomyScope,
    req: &ElevatedRequest<'_>,
    grant: &GrantRow,
    status: i16,
    from_balance: Option<i64>,
    to_balance: Option<i64>,
) -> Result<i64, EconomyError> {
    let row = conn
        .query_one_raw(stmt(
            "INSERT INTO economy_elevated_audit \
                 (tenant_id, community_id, caller_app_id, service_app_id, permission_id, \
                  grant_id, approved_by, op, target_user, counterparty_user, amount, reason, \
                  idempotency_key, status, from_balance_after, to_balance_after, settled_at) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, \
                     CASE WHEN $14::SMALLINT = 1 THEN NULL ELSE NOW() END) \
             RETURNING id",
            vec![
                Value::Int(Some(scope.tenant_id)),
                Value::Int(Some(scope.community_id)),
                Value::String(Some(req.caller_app.to_string())),
                Value::String(Some(scope.app_id.clone())),
                Value::String(Some(req.permission.id().to_string())),
                Value::BigInt(Some(grant.grant_id)),
                Value::Int(grant.approved_by),
                Value::String(Some(req.op.to_string())),
                Value::Uuid(Some(req.target)),
                Value::Uuid(req.counterparty),
                Value::BigInt(Some(req.amount)),
                Value::String(Some(req.reason.to_string())),
                Value::String(Some(req.key.as_str().to_string())),
                Value::SmallInt(Some(status)),
                Value::BigInt(from_balance),
                Value::BigInt(to_balance),
            ],
        ))
        .await
        .map_err(write_err)?
        .ok_or_else(|| backend_err("audit insert returned no row"))?;
    row.try_get("", "id").map_err(backend_err)
}
