//! The compound money ops of [`PostgresEconomyStore`] (P4 Task 1): `credit`,
//! `batch_credit`, `debit`, `conditional_debit`, `admin_adjust`, `pay_pot` and
//! `exchange`. Every one reuses the base ops' recipe from the parent module --
//! one transaction whose FIRST lock is the per-`(tenant, community, app, kind)`
//! advisory lock, idempotency checked before any cap or balance check, durable
//! rolling-24h caps from the append-only ledger, a guarded single-statement
//! balance move with its ledger rows in the same statement, and the table's
//! `CHECK (balance >= 0)` as the backstop.
//!
//! Mints (`credit`, `batch_credit`, `exchange`, `admin_adjust`) additionally
//! take a SECOND, community-wide advisory lock (the supply lock, always after
//! the per-kind lock, so the order is uniform and cannot cycle) and enforce
//! `SUM(balance) + amount <= max_supply` under it: the ceiling is per
//! `(tenant, community)` across every app, which no per-app lock can serialize.

use sea_orm::{ConnectionTrait, DatabaseTransaction, TransactionTrait, Value};
use uuid::Uuid;

use super::{
    backend_err, conflict, enforce_scope_cap, enforce_user_cap, find_prior, find_prior_raw,
    lock_scope, scope_values, stmt, validate_caps, write_err, PostgresEconomyStore, PriorMovement,
    MEMBER_PREDICATE, STATEMENT_TIMEOUT_MS,
};
use crate::{
    metrics, pot_account, validate_amount, validate_entries, validate_exchange, AdminAdjust,
    EconomyCaps, EconomyError, EconomyScope, IdempotencyKey, MAX_IDEMPOTENCY_KEY_LEN,
};

/// Longest suffix a multi-row op appends to its key (`:m` + up to 3 digits).
const MEMBER_KEY_SUFFIX_LEN: usize = 5;

/// The key of row `i` of a multi-row op: row 0 carries `key` itself, row `i`
/// carries `"<key>:m<i>"`. A replay recomputes the same keys; the partial
/// UNIQUE index on `(tenant, community, app, idempotency_key)` backstops each.
fn member_key(key: &IdempotencyKey, i: usize) -> String {
    if i == 0 {
        key.as_str().to_string()
    } else {
        format!("{}:m{i}", key.as_str())
    }
}

/// A multi-row op derives per-row keys, so its base key must leave room.
fn validate_multi_key(key: &IdempotencyKey) -> Result<(), EconomyError> {
    if key.as_str().len() + MEMBER_KEY_SUFFIX_LEN > MAX_IDEMPOTENCY_KEY_LEN {
        return Err(EconomyError::Invalid(format!(
            "idempotency key must be <= {} bytes for a multi-entry op",
            MAX_IDEMPOTENCY_KEY_LEN - MEMBER_KEY_SUFFIX_LEN
        )));
    }
    Ok(())
}

impl PriorMovement {
    fn replays_credit(&self, user: Uuid, amount: i64) -> bool {
        self.kind == "credit" && self.user == user && self.delta == amount
    }

    fn replays_debit(&self, user: Uuid, amount: i64) -> bool {
        self.kind == "debit" && self.user == user && self.delta.checked_neg() == Some(amount)
    }

    fn replays_exchange(&self, user: Uuid, from_amount: i64, credited: i64) -> bool {
        self.kind == "exchange"
            && self.user == user
            && self.delta == credited
            && self.stake == Some(from_amount)
            && self.payout == Some(credited)
    }

    /// A `Set` row records its absolute target in `payout`; a `Delta` row
    /// leaves `payout` NULL, so a replay cannot be mistaken for the other form.
    fn replays_admin_adjust(&self, user: Uuid, adjust: AdminAdjust) -> bool {
        if self.kind != "admin_adjust" || self.user != user {
            return false;
        }
        match adjust {
            AdminAdjust::Delta(d) => self.payout.is_none() && self.delta == d,
            AdminAdjust::Set(t) => self.payout == Some(t),
        }
    }
}

/// One `(user, amount, ord)` row of the dynamic `VALUES` list used by the
/// multi-row statements. `first_bind` is the first free `$n`; the text only
/// ever interpolates integers this module generated, never caller input.
fn values_list(
    entries: &[(Uuid, i64)],
    first_bind: usize,
    ord_base: usize,
) -> (String, Vec<Value>) {
    let mut sql = String::new();
    let mut vals = Vec::with_capacity(entries.len() * 2);
    for (i, (user, amount)) in entries.iter().enumerate() {
        if i > 0 {
            sql.push(',');
        }
        let u = first_bind + i * 2;
        sql.push_str(&format!(
            "(${u}::UUID, ${}::BIGINT, {})",
            u + 1,
            ord_base + i
        ));
        vals.push(Value::Uuid(Some(*user)));
        vals.push(Value::BigInt(Some(*amount)));
    }
    (sql, vals)
}

/// `$n`-placeholder list `($a,$b,...)` for an `IN` clause over `users`.
fn uuid_in_list(users: &[Uuid], first_bind: usize) -> (String, Vec<Value>) {
    let ph: Vec<String> = (0..users.len())
        .map(|i| format!("${}::UUID", first_bind + i))
        .collect();
    (
        ph.join(","),
        users.iter().map(|u| Value::Uuid(Some(*u))).collect(),
    )
}

impl PostgresEconomyStore {
    /// Opens a transaction with the per-transaction statement timeout set.
    async fn begin_txn(&self) -> Result<DatabaseTransaction, EconomyError> {
        let txn = self.conn.begin().await.map_err(backend_err)?;
        txn.execute_raw(stmt(
            &format!("SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"),
            vec![],
        ))
        .await
        .map_err(backend_err)?;
        Ok(txn)
    }

    /// Live active-membership check inside the write transaction.
    async fn require_member<C: ConnectionTrait>(
        conn: &C,
        scope: &EconomyScope,
        user: Uuid,
    ) -> Result<(), EconomyError> {
        let sql = format!(
            "SELECT 1 AS ok FROM community_members cm \
             JOIN communities c ON c.id = cm.community_id WHERE {MEMBER_PREDICATE}"
        );
        if conn
            .query_one_raw(stmt(&sql, scope_values(scope, user)))
            .await
            .map_err(backend_err)?
            .is_none()
        {
            return Err(EconomyError::NotAMember);
        }
        Ok(())
    }

    /// Takes the community-wide supply lock (always AFTER the per-kind scope
    /// lock) and refuses if `SUM(balance) + add` would pass `max_supply`.
    async fn check_supply<C: ConnectionTrait>(
        conn: &C,
        scope: &EconomyScope,
        add: i64,
        max_supply: i64,
    ) -> Result<(), EconomyError> {
        conn.execute_raw(stmt(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            vec![Value::String(Some(format!(
                "waddles.bundle_economy.supply:{}:{}",
                scope.tenant_id, scope.community_id
            )))],
        ))
        .await
        .map_err(backend_err)?;
        if add <= 0 {
            return Ok(());
        }
        let supply: i64 = conn
            .query_one_raw(stmt(
                "SELECT COALESCE(SUM(balance), 0)::BIGINT AS supply FROM economy_balances \
                 WHERE tenant_id = $1 AND community_id = $2",
                vec![
                    Value::Int(Some(scope.tenant_id)),
                    Value::Int(Some(scope.community_id)),
                ],
            ))
            .await
            .map_err(backend_err)?
            .ok_or_else(|| backend_err("supply SUM returned no row"))?
            .try_get("", "supply")
            .map_err(backend_err)?;
        if supply.saturating_add(add) > max_supply {
            tracing::warn!(
                tenant_id = scope.tenant_id,
                community_id = scope.community_id,
                app_id = %scope.app_id,
                max_supply,
                "economy mint refused: community money supply cap reached"
            );
            return Err(EconomyError::SupplyCapReached { cap: max_supply });
        }
        Ok(())
    }

    /// Ensures a zero-balance row exists (identity columns only: INSERT can
    /// never mint) for each user.
    async fn ensure_rows<C: ConnectionTrait>(
        conn: &C,
        scope: &EconomyScope,
        users: &[Uuid],
    ) -> Result<(), EconomyError> {
        for user in users {
            conn.execute_raw(stmt(
                "INSERT INTO economy_balances (tenant_id, community_id, user_uuid) \
                 VALUES ($1, $2, $3) ON CONFLICT (tenant_id, community_id, user_uuid) DO NOTHING",
                scope_values(scope, *user),
            ))
            .await
            .map_err(backend_err)?;
        }
        Ok(())
    }

    /// Locks the named users' balance rows in deterministic (uuid) order, so
    /// two multi-row ops (or a transfer) can never deadlock on row order.
    async fn lock_rows<C: ConnectionTrait>(
        conn: &C,
        scope: &EconomyScope,
        users: &[Uuid],
    ) -> Result<(), EconomyError> {
        let (list, vals) = uuid_in_list(users, 3);
        let mut values = vec![
            Value::Int(Some(scope.tenant_id)),
            Value::Int(Some(scope.community_id)),
        ];
        values.extend(vals);
        conn.execute_raw(stmt(
            &format!(
                "SELECT user_uuid FROM economy_balances \
                 WHERE tenant_id = $1 AND community_id = $2 AND user_uuid IN ({list}) \
                 ORDER BY user_uuid FOR UPDATE"
            ),
            values,
        ))
        .await
        .map_err(backend_err)?;
        Ok(())
    }

    /// The guarded single-statement balance move of ONE user plus its keyed
    /// ledger row: `balance += delta` only if the result stays `>= min_after`
    /// and the user is (still) an active member. `None` = the guard refused.
    #[allow(clippy::too_many_arguments)]
    async fn apply_delta<C: ConnectionTrait>(
        conn: &C,
        scope: &EconomyScope,
        user: Uuid,
        delta: i64,
        kind: &'static str,
        stake: Option<i64>,
        payout: Option<i64>,
        min_after: i64,
        key: &IdempotencyKey,
    ) -> Result<Option<i64>, EconomyError> {
        let sql = format!(
            "WITH upd AS ( \
                 UPDATE economy_balances b \
                    SET balance = b.balance + $4::BIGINT, updated_at = NOW() \
                  WHERE b.tenant_id = $1 AND b.community_id = $2 AND b.user_uuid = $3 \
                    AND b.balance + $4::BIGINT >= $10::BIGINT \
                    AND EXISTS (SELECT 1 FROM community_members cm \
                                  JOIN communities c ON c.id = cm.community_id \
                                 WHERE {MEMBER_PREDICATE}) \
              RETURNING b.balance AS balance \
             ), led AS ( \
                 INSERT INTO economy_ledger \
                     (tenant_id, community_id, app_id, user_uuid, kind, delta, stake, payout, \
                      balance_after, idempotency_key) \
                 SELECT $1::INT, $2::INT, $5::TEXT, $3::UUID, $6::TEXT, $4::BIGINT, \
                        $7::BIGINT, $8::BIGINT, upd.balance, $9::TEXT FROM upd \
             ) \
             SELECT balance FROM upd"
        );
        let mut values = scope_values(scope, user);
        values.push(Value::BigInt(Some(delta)));
        values.push(Value::String(Some(scope.app_id.clone())));
        values.push(Value::String(Some(kind.to_string())));
        values.push(Value::BigInt(stake));
        values.push(Value::BigInt(payout));
        values.push(Value::String(Some(key.as_str().to_string())));
        values.push(Value::BigInt(Some(min_after)));
        let Some(row) = conn
            .query_one_raw(stmt(&sql, values))
            .await
            .map_err(write_err)?
        else {
            return Ok(None);
        };
        Ok(Some(row.try_get("", "balance").map_err(backend_err)?))
    }

    /// Replay/conflict gate shared by the single-user ops: `Some(result)` when
    /// the key already produced exactly this call.
    async fn replay_single<C: ConnectionTrait>(
        conn: &C,
        scope: &EconomyScope,
        op: &'static str,
        key: &IdempotencyKey,
        matches: impl FnOnce(&PriorMovement) -> bool,
    ) -> Result<Option<i64>, EconomyError> {
        let Some(prior) = find_prior(conn, scope, key).await? else {
            return Ok(None);
        };
        if matches(&prior) {
            metrics::record_replay(op);
            tracing::debug!(
                tenant_id = scope.tenant_id,
                community_id = scope.community_id,
                app_id = %scope.app_id,
                idempotency_key = %key,
                op,
                "economy op replayed; nothing moved"
            );
            Ok(Some(prior.balance_after))
        } else {
            Err(conflict(op, scope, key))
        }
    }

    /// Shared body of the single-user MINTS (`credit`, `exchange`): lock,
    /// replay, mint caps, membership, supply ceiling, guarded write.
    #[allow(clippy::too_many_arguments)]
    async fn mint_one(
        &self,
        op: &'static str,
        scope: &EconomyScope,
        user: Uuid,
        credited: i64,
        stake: Option<i64>,
        payout: Option<i64>,
        max_supply: i64,
        caps: EconomyCaps,
        key: &IdempotencyKey,
        replays: impl FnOnce(&PriorMovement) -> bool,
    ) -> Result<i64, EconomyError> {
        validate_caps(caps)?;
        let txn = self.begin_txn().await?;
        lock_scope(&txn, scope, op).await?;
        if let Some(balance) = Self::replay_single(&txn, scope, op, key, replays).await? {
            return Ok(balance);
        }
        enforce_user_cap(&txn, scope, user, op, "delta", credited, caps).await?;
        enforce_scope_cap(&txn, scope, op, "delta", credited, caps).await?;
        Self::require_member(&txn, scope, user).await?;
        Self::check_supply(&txn, scope, credited, max_supply).await?;
        Self::ensure_rows(&txn, scope, &[user]).await?;
        let Some(balance) =
            Self::apply_delta(&txn, scope, user, credited, op, stake, payout, 0, key).await?
        else {
            return Err(Self::diagnose_refusal(&txn, scope, user).await?);
        };
        txn.commit().await.map_err(backend_err)?;
        metrics::record_applied_amount(op, credited);
        Ok(balance)
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) async fn credit_impl(
        &self,
        scope: &EconomyScope,
        user: Uuid,
        amount: i64,
        max_amount: i64,
        max_supply: i64,
        caps: EconomyCaps,
        key: &IdempotencyKey,
    ) -> Result<i64, EconomyError> {
        validate_amount(amount, max_amount)?;
        self.mint_one(
            "credit",
            scope,
            user,
            amount,
            None,
            None,
            max_supply,
            caps,
            key,
            |p| p.replays_credit(user, amount),
        )
        .await
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) async fn exchange_impl(
        &self,
        scope: &EconomyScope,
        user: Uuid,
        from_amount: i64,
        rate_num: i64,
        rate_den: i64,
        max_amount: i64,
        max_supply: i64,
        caps: EconomyCaps,
        key: &IdempotencyKey,
    ) -> Result<i64, EconomyError> {
        let credited = validate_exchange(from_amount, rate_num, rate_den, max_amount)?;
        // The ledgered claim of the external points: `stake` = points claimed,
        // `payout` = currency credited. There is no external balance table.
        self.mint_one(
            "exchange",
            scope,
            user,
            credited,
            Some(from_amount),
            Some(credited),
            max_supply,
            caps,
            key,
            |p| p.replays_exchange(user, from_amount, credited),
        )
        .await
    }

    pub(super) async fn batch_credit_impl(
        &self,
        scope: &EconomyScope,
        entries: &[(Uuid, i64)],
        max_amount: i64,
        max_supply: i64,
        caps: EconomyCaps,
        key: &IdempotencyKey,
    ) -> Result<(), EconomyError> {
        validate_multi_key(key)?;
        validate_caps(caps)?;
        let (sorted, total) = validate_entries(entries, Some(max_amount))?;
        let txn = self.begin_txn().await?;
        lock_scope(&txn, scope, "credit").await?;

        // Replay: every derived row present and identical, the one past the end
        // absent. A partial or different set is a conflict, never applied.
        let mut present = 0;
        let mut identical = true;
        for i in 0..=sorted.len() {
            let prior = find_prior_raw(&txn, scope, &member_key(key, i)).await?;
            match (i < sorted.len(), prior) {
                (true, Some(p)) => {
                    present += 1;
                    identical &= p.replays_credit(sorted[i].0, sorted[i].1);
                }
                (true, None) => identical = false,
                (false, Some(_)) => {
                    present += 1;
                    identical = false;
                }
                (false, None) => {}
            }
        }
        if present > 0 {
            return if identical && present == sorted.len() {
                metrics::record_replay("batch_credit");
                Ok(())
            } else {
                Err(conflict("batch_credit", scope, key))
            };
        }

        for (user, amount) in &sorted {
            enforce_user_cap(&txn, scope, *user, "credit", "delta", *amount, caps).await?;
        }
        enforce_scope_cap(&txn, scope, "credit", "delta", total, caps).await?;
        let users: Vec<Uuid> = sorted.iter().map(|(u, _)| *u).collect();
        for user in &users {
            Self::require_member(&txn, scope, *user).await?;
        }
        Self::check_supply(&txn, scope, total, max_supply).await?;
        Self::ensure_rows(&txn, scope, &users).await?;
        Self::lock_rows(&txn, scope, &users).await?;

        let (list, vals) = values_list(&sorted, 5, 0);
        let sql = format!(
            "WITH w(u, amt, ord) AS (VALUES {list}), \
             upd AS ( \
                 UPDATE economy_balances b \
                    SET balance = b.balance + w.amt, updated_at = NOW() \
                   FROM w \
                  WHERE b.tenant_id = $1 AND b.community_id = $2 AND b.user_uuid = w.u \
              RETURNING b.user_uuid AS u, b.balance AS bal, w.amt AS amt, w.ord AS ord \
             ), led AS ( \
                 INSERT INTO economy_ledger \
                     (tenant_id, community_id, app_id, user_uuid, kind, delta, balance_after, \
                      idempotency_key) \
                 SELECT $1::INT, $2::INT, $3::TEXT, u, 'credit', amt, bal, \
                        CASE WHEN ord = 0 THEN $4::TEXT ELSE $4::TEXT || ':m' || ord END \
                   FROM upd \
             ) \
             SELECT COUNT(*)::BIGINT AS n FROM upd"
        );
        let mut values = vec![
            Value::Int(Some(scope.tenant_id)),
            Value::Int(Some(scope.community_id)),
            Value::String(Some(scope.app_id.clone())),
            Value::String(Some(key.as_str().to_string())),
        ];
        values.extend(vals);
        let n: i64 = txn
            .query_one_raw(stmt(&sql, values))
            .await
            .map_err(write_err)?
            .ok_or_else(|| backend_err("batch credit returned no row"))?
            .try_get("", "n")
            .map_err(backend_err)?;
        if usize::try_from(n).ok() != Some(sorted.len()) {
            return Err(backend_err("batch credit applied an unexpected row count"));
        }
        txn.commit().await.map_err(backend_err)?;
        metrics::record_applied_amount("batch_credit", total);
        Ok(())
    }

    /// Shared body of `debit` / `conditional_debit` (`min_balance` 0 for the
    /// plain debit).
    #[allow(clippy::too_many_arguments)]
    async fn debit_inner(
        &self,
        op: &'static str,
        scope: &EconomyScope,
        user: Uuid,
        amount: i64,
        min_balance: i64,
        max_amount: i64,
        caps: EconomyCaps,
        key: &IdempotencyKey,
    ) -> Result<i64, EconomyError> {
        validate_amount(amount, max_amount)?;
        if min_balance < 0 {
            return Err(EconomyError::Invalid(
                "min_balance must be >= 0".to_string(),
            ));
        }
        validate_caps(caps)?;
        let neg = amount
            .checked_neg()
            .ok_or_else(|| EconomyError::Invalid("amount out of range".to_string()))?;
        let txn = self.begin_txn().await?;
        lock_scope(&txn, scope, "debit").await?;
        if let Some(balance) =
            Self::replay_single(&txn, scope, op, key, |p| p.replays_debit(user, amount)).await?
        {
            return Ok(balance);
        }
        enforce_user_cap(&txn, scope, user, "debit", "-delta", amount, caps).await?;
        enforce_scope_cap(&txn, scope, "debit", "-delta", amount, caps).await?;
        // `balance + (-amount) >= min_balance` (>= 0 always): rejects, never
        // partially applies; CHECK (balance >= 0) is the backstop.
        let Some(balance) = Self::apply_delta(
            &txn,
            scope,
            user,
            neg,
            "debit",
            None,
            None,
            min_balance,
            key,
        )
        .await?
        else {
            return Err(Self::diagnose_refusal(&txn, scope, user).await?);
        };
        txn.commit().await.map_err(backend_err)?;
        metrics::record_applied_amount(op, amount);
        Ok(balance)
    }

    pub(super) async fn debit_impl(
        &self,
        scope: &EconomyScope,
        user: Uuid,
        amount: i64,
        max_amount: i64,
        caps: EconomyCaps,
        key: &IdempotencyKey,
    ) -> Result<i64, EconomyError> {
        self.debit_inner("debit", scope, user, amount, 0, max_amount, caps, key)
            .await
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) async fn conditional_debit_impl(
        &self,
        scope: &EconomyScope,
        user: Uuid,
        amount: i64,
        min_balance: i64,
        max_amount: i64,
        caps: EconomyCaps,
        key: &IdempotencyKey,
    ) -> Result<i64, EconomyError> {
        self.debit_inner(
            "conditional_debit",
            scope,
            user,
            amount,
            min_balance,
            max_amount,
            caps,
            key,
        )
        .await
    }

    pub(super) async fn admin_adjust_impl(
        &self,
        scope: &EconomyScope,
        user: Uuid,
        adjust: AdminAdjust,
        max_supply: i64,
        is_admin_authorized: bool,
        key: &IdempotencyKey,
    ) -> Result<i64, EconomyError> {
        // Fail closed BEFORE any I/O: the stage's guarantee is the only gate.
        if !is_admin_authorized {
            return Err(EconomyError::NotAuthorized);
        }
        match adjust {
            AdminAdjust::Delta(0) => {
                return Err(EconomyError::Invalid("delta must be non-zero".to_string()))
            }
            AdminAdjust::Set(t) if t < 0 => {
                return Err(EconomyError::Invalid("balance must be >= 0".to_string()))
            }
            _ => {}
        }
        let txn = self.begin_txn().await?;
        lock_scope(&txn, scope, "admin_adjust").await?;
        if let Some(balance) = Self::replay_single(&txn, scope, "admin_adjust", key, |p| {
            p.replays_admin_adjust(user, adjust)
        })
        .await?
        {
            return Ok(balance);
        }
        Self::require_member(&txn, scope, user).await?;
        Self::check_supply(&txn, scope, 0, max_supply).await?;
        Self::ensure_rows(&txn, scope, &[user]).await?;
        // The row lock held from here to commit makes the old balance (and so
        // an absolute set's delta) race-free.
        let old: i64 = txn
            .query_one_raw(stmt(
                "SELECT balance FROM economy_balances \
                 WHERE tenant_id = $1 AND community_id = $2 AND user_uuid = $3 FOR UPDATE",
                scope_values(scope, user),
            ))
            .await
            .map_err(backend_err)?
            .ok_or_else(|| backend_err("balance row vanished"))?
            .try_get("", "balance")
            .map_err(backend_err)?;
        let (delta, payout) = match adjust {
            AdminAdjust::Delta(d) => (d, None),
            AdminAdjust::Set(t) => (
                t.checked_sub(old)
                    .ok_or_else(|| EconomyError::Invalid("balance out of range".to_string()))?,
                Some(t),
            ),
        };
        if delta > 0 {
            // Supply SUM includes this row's `old`, so only the increase counts.
            Self::check_supply(&txn, scope, delta, max_supply).await?;
        }
        let Some(balance) = Self::apply_delta(
            &txn,
            scope,
            user,
            delta,
            "admin_adjust",
            None,
            payout,
            0,
            key,
        )
        .await?
        else {
            return Err(Self::diagnose_refusal(&txn, scope, user).await?);
        };
        txn.commit().await.map_err(backend_err)?;
        metrics::record_applied_amount("admin_adjust", delta.saturating_abs());
        Ok(balance)
    }

    pub(super) async fn pay_pot_impl(
        &self,
        scope: &EconomyScope,
        pot_source: Uuid,
        winners: &[(Uuid, i64)],
        key: &IdempotencyKey,
    ) -> Result<(), EconomyError> {
        let pot = pot_account(scope);
        // Only the community's own reserved pot may be drained: a bundle naming
        // any other account (a member's!) is refused before any I/O.
        if pot_source != pot {
            return Err(EconomyError::Invalid(
                "pot_source is not this community's pot account".to_string(),
            ));
        }
        validate_multi_key(key)?;
        let (sorted, total) = validate_entries(winners, None)?;
        if sorted.iter().any(|(u, _)| *u == pot) {
            return Err(EconomyError::Invalid(
                "the pot cannot be a winner".to_string(),
            ));
        }
        let neg_total = total
            .checked_neg()
            .ok_or_else(|| EconomyError::Invalid("total out of range".to_string()))?;
        let txn = self.begin_txn().await?;
        lock_scope(&txn, scope, "pot_payout").await?;

        // Replay: the pot row carries `key`, winner i (1-based, uuid order)
        // carries `<key>:m<i>`, and `<key>:m<n+1>` must not exist.
        let mut present = 0;
        let mut identical = true;
        for i in 0..=sorted.len() + 1 {
            let prior = find_prior_raw(&txn, scope, &member_key(key, i)).await?;
            let matches = match (i, &prior) {
                (_, None) => None,
                (0, Some(p)) => {
                    Some(p.kind == "pot_payout" && p.user == pot && p.delta == neg_total)
                }
                (n, Some(p)) if n <= sorted.len() => Some(
                    p.kind == "pot_payout"
                        && p.user == sorted[n - 1].0
                        && p.delta == sorted[n - 1].1,
                ),
                (_, Some(_)) => Some(false),
            };
            match matches {
                Some(m) => {
                    present += 1;
                    identical &= m;
                }
                None if i <= sorted.len() => identical = false,
                None => {}
            }
        }
        if present > 0 {
            return if identical && present == sorted.len() + 1 {
                metrics::record_replay("pay_pot");
                Ok(())
            } else {
                Err(conflict("pay_pot", scope, key))
            };
        }

        let users: Vec<Uuid> = sorted.iter().map(|(u, _)| *u).collect();
        for user in &users {
            Self::require_member(&txn, scope, *user).await?;
        }
        Self::ensure_rows(&txn, scope, &users).await?;
        let mut locked = users.clone();
        locked.push(pot);
        locked.sort();
        Self::lock_rows(&txn, scope, &locked).await?;

        let (list, vals) = values_list(&sorted, 7, 1);
        let sql = format!(
            "WITH w(u, amt, ord) AS (VALUES {list}), \
             debit AS ( \
                 UPDATE economy_balances \
                    SET balance = balance - $5::BIGINT, updated_at = NOW() \
                  WHERE tenant_id = $1 AND community_id = $2 AND user_uuid = $4 \
                    AND balance >= $5::BIGINT \
              RETURNING balance \
             ), credit AS ( \
                 UPDATE economy_balances b \
                    SET balance = b.balance + w.amt, updated_at = NOW() \
                   FROM w \
                  WHERE b.tenant_id = $1 AND b.community_id = $2 AND b.user_uuid = w.u \
                    AND EXISTS (SELECT 1 FROM debit) \
              RETURNING b.user_uuid AS u, b.balance AS bal, w.amt AS amt, w.ord AS ord \
             ), led_pot AS ( \
                 INSERT INTO economy_ledger \
                     (tenant_id, community_id, app_id, user_uuid, kind, delta, balance_after, \
                      idempotency_key) \
                 SELECT $1::INT, $2::INT, $3::TEXT, $4::UUID, 'pot_payout', -$5::BIGINT, \
                        debit.balance, $6::TEXT FROM debit \
             ), led_win AS ( \
                 INSERT INTO economy_ledger \
                     (tenant_id, community_id, app_id, user_uuid, counterparty_uuid, kind, delta, \
                      balance_after, idempotency_key) \
                 SELECT $1::INT, $2::INT, $3::TEXT, u, $4::UUID, 'pot_payout', amt, bal, \
                        $6::TEXT || ':m' || ord \
                   FROM credit \
             ) \
             SELECT (SELECT balance FROM debit) AS pot_balance, \
                    (SELECT COUNT(*) FROM credit)::BIGINT AS n"
        );
        let mut values = vec![
            Value::Int(Some(scope.tenant_id)),
            Value::Int(Some(scope.community_id)),
            Value::String(Some(scope.app_id.clone())),
            Value::Uuid(Some(pot)),
            Value::BigInt(Some(total)),
            Value::String(Some(key.as_str().to_string())),
        ];
        values.extend(vals);
        let row = txn
            .query_one_raw(stmt(&sql, values))
            .await
            .map_err(write_err)?
            .ok_or_else(|| backend_err("pay_pot statement returned no row"))?;
        let pot_balance: Option<i64> = row.try_get("", "pot_balance").map_err(backend_err)?;
        let n: i64 = row.try_get("", "n").map_err(backend_err)?;
        if pot_balance.is_none() {
            // The pot is not a member, so name the refusal from its balance.
            let held: i64 = txn
                .query_one_raw(stmt(
                    "SELECT COALESCE((SELECT balance FROM economy_balances \
                       WHERE tenant_id = $1 AND community_id = $2 AND user_uuid = $3), 0)::BIGINT \
                       AS balance",
                    scope_values(scope, pot),
                ))
                .await
                .map_err(backend_err)?
                .ok_or_else(|| backend_err("pot balance query returned no row"))?
                .try_get("", "balance")
                .map_err(backend_err)?;
            return Err(EconomyError::InsufficientFunds { balance: held });
        }
        if usize::try_from(n).ok() != Some(sorted.len()) {
            return Err(backend_err("pay_pot credited an unexpected winner count"));
        }
        txn.commit().await.map_err(backend_err)?;
        metrics::record_applied_amount("pay_pot", total);
        Ok(())
    }
}
