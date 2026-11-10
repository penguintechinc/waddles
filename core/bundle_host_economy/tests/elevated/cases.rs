//! ELEVATED app-caller primitives against a REAL Postgres: `transfer_from`
//! (grant read inside the write transaction, admin-set caps, idempotency,
//! membership, double-spend, audit) and the `elevated_reserve` /
//! `elevated_settle` pair. The schema is the exact shipped DDL
//! (`scripts/db/bundle_economy_elevated.sql`) on top of the store DDL and a
//! minimal `community_permission_grants` (the real one is alembic 0041).

use super::*;
use bundle_host_economy::{
    ElevatedPermission, ElevatedRequest, TransferFromOutcome, ELEVATED_DEFAULT_MAX_PER_OP,
    ELEVATED_MAX_PER_OP_CEILING,
};

const CALLER: &str = "waddles.app.steal-game";
const OTHER_CALLER: &str = "waddles.app.other-game";
const TF: &str = "economy.transfer_from";
const MINT: &str = "economy.app_mint";
/// The hub user who approved the grants (`community_permission_grants.granted_by`).
const APPROVER: i32 = 41;

/// Inserts a live grant of `perm` for `app` in the test community, approved by [`APPROVER`].
async fn grant(f: &Fixture, app: &str, perm: &str, params: &str) {
    grant_in(f, 10, 1, app, perm, params).await;
}

async fn grant_in(f: &Fixture, community: i32, tenant: i32, app: &str, perm: &str, params: &str) {
    exec(
        &f.su,
        &format!(
            "INSERT INTO community_permission_grants \
                 (community_id, tenant_id, app_id, permission_id, params_json, granted_by) \
             VALUES ({community}, {tenant}, '{app}', '{perm}', '{params}'::jsonb, {APPROVER})"
        ),
    )
    .await;
}

async fn add_member(f: &Fixture) -> Uuid {
    let user = Uuid::new_v4();
    exec(
        &f.su,
        &format!("INSERT INTO community_members (community_id, user_uuid) VALUES (10, '{user}')"),
    )
    .await;
    user
}

fn req<'a>(
    caller: &'a str,
    from: Uuid,
    to: Uuid,
    amount: i64,
    reason: &'a str,
    k: &'a IdempotencyKey,
) -> ElevatedRequest<'a> {
    ElevatedRequest {
        caller_app: caller,
        permission: ElevatedPermission::TransferFrom,
        op: "transfer_from",
        target: from,
        counterparty: Some(to),
        amount,
        reason,
        key: k,
    }
}

async fn steal(
    f: &Fixture,
    from: Uuid,
    to: Uuid,
    amount: i64,
) -> Result<TransferFromOutcome, EconomyError> {
    let k = key();
    f.store
        .transfer_from(&scope(), &req(CALLER, from, to, amount, "steal", &k))
        .await
}

async fn audit_count(f: &Fixture) -> i64 {
    scalar_i64(&f.su, "SELECT COUNT(*) FROM economy_elevated_audit").await
}

async fn bal(f: &Fixture, user: Uuid) -> i64 {
    f.store.balance(&scope(), user).await.unwrap()
}

#[tokio::test]
async fn transfer_from_without_a_grant_is_denied_and_moves_nothing() {
    let f = fixture().await;
    fund(&f, f.bob, 100).await;
    assert_eq!(
        steal(&f, f.bob, f.alice, 10).await,
        Err(EconomyError::ElevatedNotGranted)
    );
    assert_eq!(bal(&f, f.bob).await, 100);
    assert_eq!(bal(&f, f.alice).await, 0);
    assert_eq!(ledger_count(&f).await, 0);
    assert_eq!(audit_count(&f).await, 0);
}

#[tokio::test]
async fn a_revoked_grant_a_wrong_permission_a_wrong_app_and_a_wrong_community_all_deny() {
    let f = fixture().await;
    fund(&f, f.bob, 100).await;
    // Revoked.
    grant(&f, CALLER, TF, "{}").await;
    exec(
        &f.su,
        &format!(
            "UPDATE community_permission_grants SET revoked_at = NOW(), revoked_by = 42 \
             WHERE app_id = '{CALLER}'"
        ),
    )
    .await;
    assert_eq!(
        steal(&f, f.bob, f.alice, 10).await,
        Err(EconomyError::ElevatedNotGranted)
    );
    // The mint-class grant does not authorize transfer_from.
    grant(&f, OTHER_CALLER, MINT, "{}").await;
    let k = key();
    assert_eq!(
        f.store
            .transfer_from(
                &scope(),
                &req(OTHER_CALLER, f.bob, f.alice, 10, "steal", &k)
            )
            .await,
        Err(EconomyError::ElevatedNotGranted)
    );
    // Another app's grant does not authorize this caller.
    grant(&f, "waddles.app.third", TF, "{}").await;
    assert_eq!(
        steal(&f, f.bob, f.alice, 10).await,
        Err(EconomyError::ElevatedNotGranted)
    );
    // A grant in ANOTHER community (tenant 2) does not authorize this one.
    grant_in(&f, 20, 2, CALLER, TF, "{}").await;
    assert_eq!(
        steal(&f, f.bob, f.alice, 10).await,
        Err(EconomyError::ElevatedNotGranted)
    );
    assert_eq!(bal(&f, f.bob).await, 100);
    assert_eq!(ledger_count(&f).await, 0);
    assert_eq!(audit_count(&f).await, 0);
}

#[tokio::test]
async fn transfer_from_moves_the_funds_ledgers_both_sides_and_audits_the_approval() {
    let f = fixture().await;
    fund(&f, f.bob, 100).await;
    fund(&f, f.alice, 5).await;
    grant(&f, CALLER, TF, "{}").await;
    let k = key();
    let out = f
        .store
        .transfer_from(&scope(), &req(CALLER, f.bob, f.alice, 40, "steal", &k))
        .await
        .unwrap();
    assert_eq!((out.from_balance, out.to_balance), (60, 45));
    assert_eq!(bal(&f, f.bob).await, 60);
    assert_eq!(bal(&f, f.alice).await, 45);
    assert_eq!(total_balance(&f).await, 105, "currency is conserved");
    // Ledger: the victim's debit (carrying the key) + the recipient's credit.
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT delta FROM economy_ledger WHERE kind = 'transfer_from'"
        )
        .await,
        -40
    );
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT delta FROM economy_ledger WHERE kind = 'transfer_from_in'"
        )
        .await,
        40
    );
    // The audit row names the calling app, the approving grant + admin, the
    // parties (UUIDs), amount, reason and resulting balances.
    let grant_id = scalar_i64(
        &f.su,
        &format!("SELECT grant_id FROM community_permission_grants WHERE app_id = '{CALLER}'"),
    )
    .await;
    assert_eq!(out.grant_id, grant_id);
    assert_eq!(
        scalar_i64(
            &f.su,
            &format!(
                "SELECT COUNT(*) FROM economy_elevated_audit WHERE id = {} \
                 AND caller_app_id = '{CALLER}' AND service_app_id = '{APP_ID}' \
                 AND permission_id = '{TF}' AND grant_id = {grant_id} AND approved_by = {APPROVER} \
                 AND op = 'transfer_from' AND target_user = '{}' AND counterparty_user = '{}' \
                 AND amount = 40 AND reason = 'steal' AND status = 2 \
                 AND from_balance_after = 60 AND to_balance_after = 45 \
                 AND idempotency_key = '{k}'",
                out.audit_id, f.bob, f.alice
            )
        )
        .await,
        1
    );
}

#[tokio::test]
async fn the_victim_need_not_be_the_actor_but_both_must_be_active_members() {
    let f = fixture().await;
    fund(&f, f.bob, 100).await;
    grant(&f, CALLER, TF, "{}").await;
    let stranger = Uuid::new_v4();
    assert_eq!(
        steal(&f, stranger, f.alice, 5).await,
        Err(EconomyError::NotAMember)
    );
    assert_eq!(
        steal(&f, f.bob, stranger, 5).await,
        Err(EconomyError::NotAMember)
    );
    // A member who left is not a member either.
    exec(
        &f.su,
        &format!(
            "UPDATE community_members SET is_active = false WHERE user_uuid = '{}'",
            f.alice
        ),
    )
    .await;
    assert_eq!(
        steal(&f, f.bob, f.alice, 5).await,
        Err(EconomyError::NotAMember)
    );
    assert_eq!(bal(&f, f.bob).await, 100);
    assert_eq!(ledger_count(&f).await, 0);
    assert_eq!(audit_count(&f).await, 0);
}

#[tokio::test]
async fn insufficient_funds_refuses_without_a_partial_move_or_an_audit_row() {
    let f = fixture().await;
    fund(&f, f.bob, 5).await;
    grant(&f, CALLER, TF, "{}").await;
    assert_eq!(
        steal(&f, f.bob, f.alice, 10).await,
        Err(EconomyError::InsufficientFunds { balance: 5 })
    );
    assert_eq!(bal(&f, f.bob).await, 5);
    assert_eq!(bal(&f, f.alice).await, 0);
    assert_eq!(ledger_count(&f).await, 0);
    assert_eq!(
        audit_count(&f).await,
        0,
        "a refused call leaves no audit row"
    );
}

#[tokio::test]
async fn invalid_arguments_are_refused_before_touching_the_database() {
    let f = fixture().await;
    fund(&f, f.bob, 100).await;
    grant(&f, CALLER, TF, "{}").await;
    for (from, to, amount, reason) in [
        (f.bob, f.bob, 5, "steal"),
        (f.bob, f.alice, 0, "steal"),
        (f.bob, f.alice, -5, "steal"),
        (f.bob, f.alice, 5, ""),
        (f.bob, f.alice, 5, "Free Text With PII"),
    ] {
        let k = key();
        assert!(
            matches!(
                f.store
                    .transfer_from(&scope(), &req(CALLER, from, to, amount, reason, &k))
                    .await,
                Err(EconomyError::Invalid(_))
            ),
            "{amount} {reason:?}"
        );
    }
    assert_eq!(ledger_count(&f).await, 0);
    assert_eq!(audit_count(&f).await, 0);
}

#[tokio::test]
async fn admin_set_caps_bound_each_call_each_app_day_and_each_victim_day() {
    let f = fixture().await;
    let (carol, dave) = (add_member(&f).await, add_member(&f).await);
    for u in [f.bob, carol, dave] {
        fund(&f, u, 1_000).await;
    }
    grant(
        &f,
        CALLER,
        TF,
        r#"{"max_per_op":50,"max_per_day":120,"max_per_target_per_day":60}"#,
    )
    .await;
    // Per call.
    assert_eq!(
        steal(&f, f.bob, f.alice, 51).await,
        Err(EconomyError::ElevatedOverCap { cap: 50 })
    );
    // bob: 50 ok, then 20 more would be 70 > 60 per victim.
    assert!(steal(&f, f.bob, f.alice, 50).await.is_ok());
    assert_eq!(
        steal(&f, f.bob, f.alice, 20).await,
        Err(EconomyError::ElevatedQuotaExceeded { cap: 60 })
    );
    // carol: 50 ok (app day total 100), then dave 30 would be 130 > 120 per app.
    assert!(steal(&f, carol, f.alice, 50).await.is_ok());
    assert_eq!(
        steal(&f, dave, f.alice, 30).await,
        Err(EconomyError::ElevatedQuotaExceeded { cap: 120 })
    );
    // The day budget still has 20 left; a refused call consumed none of it.
    assert!(steal(&f, dave, f.alice, 20).await.is_ok());
    assert_eq!(bal(&f, f.alice).await, 120);
    assert_eq!(audit_count(&f).await, 3, "refused calls left no audit row");
    // Another app's budget is its own: OTHER_CALLER (same permission) is fresh.
    grant(&f, OTHER_CALLER, TF, r#"{"max_per_op":50}"#).await;
    let k = key();
    assert!(f
        .store
        .transfer_from(&scope(), &req(OTHER_CALLER, dave, f.alice, 50, "steal", &k))
        .await
        .is_ok());
}

#[tokio::test]
async fn unset_caps_default_and_oversized_caps_clamp_to_the_ceiling() {
    let f = fixture().await;
    fund(&f, f.bob, 100_000).await;
    grant(&f, CALLER, TF, "{}").await;
    assert_eq!(
        steal(&f, f.bob, f.alice, ELEVATED_DEFAULT_MAX_PER_OP + 1).await,
        Err(EconomyError::ElevatedOverCap {
            cap: ELEVATED_DEFAULT_MAX_PER_OP
        })
    );
    // An admin-set value above the hard ceiling (or garbage) cannot widen it.
    exec(
        &f.su,
        &format!(
            "UPDATE community_permission_grants SET params_json = \
             '{{\"max_per_op\": 99999999, \"max_per_day\": \"lots\"}}'::jsonb \
             WHERE app_id = '{CALLER}'"
        ),
    )
    .await;
    assert_eq!(
        steal(&f, f.bob, f.alice, ELEVATED_MAX_PER_OP_CEILING + 1).await,
        Err(EconomyError::ElevatedOverCap {
            cap: ELEVATED_MAX_PER_OP_CEILING
        })
    );
}

#[tokio::test]
async fn idempotent_replay_returns_the_prior_balances_and_a_changed_call_is_a_conflict() {
    let f = fixture().await;
    fund(&f, f.bob, 100).await;
    grant(&f, CALLER, TF, "{}").await;
    let k = key();
    let first = f
        .store
        .transfer_from(&scope(), &req(CALLER, f.bob, f.alice, 30, "steal", &k))
        .await
        .unwrap();
    // Same key, same parameters: the original result, nothing moves again.
    let replay = f
        .store
        .transfer_from(&scope(), &req(CALLER, f.bob, f.alice, 30, "steal", &k))
        .await
        .unwrap();
    assert_eq!(replay, first);
    assert_eq!(bal(&f, f.bob).await, 70);
    assert_eq!(ledger_count(&f).await, 2);
    assert_eq!(audit_count(&f).await, 1);
    // Same key, any different parameter: IdempotencyConflict, never applied.
    let carol = add_member(&f).await;
    for r in [
        req(CALLER, f.bob, f.alice, 31, "steal", &k),
        req(CALLER, f.bob, f.alice, 30, "other_reason", &k),
        req(CALLER, f.bob, carol, 30, "steal", &k),
        req(CALLER, f.alice, f.bob, 30, "steal", &k),
    ] {
        assert_eq!(
            f.store.transfer_from(&scope(), &r).await,
            Err(EconomyError::IdempotencyConflict)
        );
    }
    assert_eq!(bal(&f, f.bob).await, 70);
    assert_eq!(ledger_count(&f).await, 2);
    assert_eq!(audit_count(&f).await, 1);
}

#[tokio::test]
async fn a_second_app_cannot_replay_or_collide_with_anothers_key() {
    let f = fixture().await;
    fund(&f, f.bob, 100).await;
    grant(&f, CALLER, TF, "{}").await;
    grant(&f, OTHER_CALLER, TF, "{}").await;
    let k = key();
    f.store
        .transfer_from(&scope(), &req(CALLER, f.bob, f.alice, 10, "steal", &k))
        .await
        .unwrap();
    // The ledger key is unique per (community, service app): the other app
    // presenting the SAME key text is a conflict, not a second move.
    assert_eq!(
        f.store
            .transfer_from(
                &scope(),
                &req(OTHER_CALLER, f.bob, f.alice, 10, "steal", &k)
            )
            .await,
        Err(EconomyError::IdempotencyConflict)
    );
    assert_eq!(bal(&f, f.bob).await, 90);
    assert_eq!(audit_count(&f).await, 1);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn concurrent_transfer_from_never_double_spends_the_victim() {
    let f = fixture().await;
    fund(&f, f.bob, 100).await;
    grant(
        &f,
        CALLER,
        TF,
        r#"{"max_per_op":100,"max_per_day":100000,"max_per_target_per_day":50000}"#,
    )
    .await;
    let store = Arc::new(PostgresEconomyStore::new(f.eco_conn.clone()));
    let mut tasks = Vec::new();
    // 40 concurrent steals of 10 from a victim holding 100: exactly 10 apply.
    for _ in 0..40 {
        let store = Arc::clone(&store);
        let (bob, alice) = (f.bob, f.alice);
        tasks.push(tokio::spawn(async move {
            let k = key();
            store
                .transfer_from(&scope(), &req(CALLER, bob, alice, 10, "steal", &k))
                .await
        }));
    }
    let (mut ok, mut insufficient) = (0, 0);
    for t in tasks {
        match t.await.unwrap() {
            Ok(_) => ok += 1,
            Err(EconomyError::InsufficientFunds { .. }) => insufficient += 1,
            Err(other) => panic!("unexpected error: {other:?}"),
        }
    }
    assert_eq!(ok, 10, "exactly balance/amount steals apply");
    assert_eq!(insufficient, 30);
    assert_eq!(bal(&f, f.bob).await, 0, "never overdrawn");
    assert_eq!(bal(&f, f.alice).await, 100);
    assert_eq!(total_balance(&f).await, 100, "currency is conserved");
    assert_eq!(audit_count(&f).await, 10);
    assert_eq!(ledger_count(&f).await, 20);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn concurrent_calls_cannot_overshoot_the_per_app_day_cap() {
    let f = fixture().await;
    fund(&f, f.bob, 10_000).await;
    grant(
        &f,
        CALLER,
        TF,
        r#"{"max_per_op":100,"max_per_day":300,"max_per_target_per_day":50000}"#,
    )
    .await;
    let store = Arc::new(PostgresEconomyStore::new(f.eco_conn.clone()));
    let mut tasks = Vec::new();
    for _ in 0..30 {
        let store = Arc::clone(&store);
        let (bob, alice) = (f.bob, f.alice);
        tasks.push(tokio::spawn(async move {
            let k = key();
            store
                .transfer_from(&scope(), &req(CALLER, bob, alice, 100, "steal", &k))
                .await
        }));
    }
    let mut ok = 0;
    for t in tasks {
        match t.await.unwrap() {
            Ok(_) => ok += 1,
            Err(EconomyError::ElevatedQuotaExceeded { cap: 300 }) => {}
            Err(other) => panic!("unexpected error: {other:?}"),
        }
    }
    assert_eq!(ok, 3, "the advisory lock serializes the cap SUM: 300/100");
    assert_eq!(bal(&f, f.alice).await, 300);
}

#[tokio::test]
async fn the_runtime_role_cannot_rewrite_or_delete_audit_rows() {
    let f = fixture().await;
    fund(&f, f.bob, 100).await;
    grant(&f, CALLER, TF, "{}").await;
    steal(&f, f.bob, f.alice, 10).await.unwrap();
    for sql in [
        "DELETE FROM economy_elevated_audit",
        "UPDATE economy_elevated_audit SET amount = 0",
        "UPDATE economy_elevated_audit SET caller_app_id = 'x'",
        "UPDATE community_permission_grants SET revoked_at = NULL",
        "DELETE FROM community_permission_grants",
    ] {
        assert!(
            f.eco_conn.execute_unprepared(sql).await.is_err(),
            "the runtime role must not run: {sql}"
        );
    }
    assert_eq!(audit_count(&f).await, 1);
}

// ---- elevated_reserve / elevated_settle (the wrapper of every other primitive)

fn mint_req<'a>(
    caller: &'a str,
    perm: ElevatedPermission,
    target: Uuid,
    amount: i64,
    k: &'a IdempotencyKey,
) -> ElevatedRequest<'a> {
    ElevatedRequest {
        caller_app: caller,
        permission: perm,
        op: "credit",
        target,
        counterparty: None,
        amount,
        reason: "credit",
        key: k,
    }
}

#[tokio::test]
async fn reserve_without_the_matching_grant_is_denied() {
    let f = fixture().await;
    let k = key();
    let r = mint_req(CALLER, ElevatedPermission::AppMint, f.alice, 10, &k);
    assert_eq!(
        f.store.elevated_reserve(&scope(), &r).await,
        Err(EconomyError::ElevatedNotGranted)
    );
    // transfer_from-class grant does not satisfy the mint class.
    grant(&f, CALLER, TF, "{}").await;
    assert_eq!(
        f.store.elevated_reserve(&scope(), &r).await,
        Err(EconomyError::ElevatedNotGranted)
    );
    assert_eq!(audit_count(&f).await, 0);
}

#[tokio::test]
async fn reserve_counts_against_the_caps_until_it_is_settled_failed() {
    let f = fixture().await;
    grant(
        &f,
        CALLER,
        MINT,
        r#"{"max_per_op":60,"max_per_day":100,"max_per_target_per_day":80}"#,
    )
    .await;
    let m = ElevatedPermission::AppMint;
    let (k1, k2, k3) = (key(), key(), key());
    assert_eq!(
        f.store
            .elevated_reserve(&scope(), &mint_req(CALLER, m, f.alice, 61, &k1))
            .await,
        Err(EconomyError::ElevatedOverCap { cap: 60 })
    );
    let id1 = f
        .store
        .elevated_reserve(&scope(), &mint_req(CALLER, m, f.alice, 60, &k1))
        .await
        .unwrap();
    // 60 reserved: another 30 for alice would be 90 > 80 per target.
    assert_eq!(
        f.store
            .elevated_reserve(&scope(), &mint_req(CALLER, m, f.alice, 30, &k2))
            .await,
        Err(EconomyError::ElevatedQuotaExceeded { cap: 80 })
    );
    // ...and 50 for bob would be 110 > 100 per app per day.
    assert_eq!(
        f.store
            .elevated_reserve(&scope(), &mint_req(CALLER, m, f.bob, 50, &k2))
            .await,
        Err(EconomyError::ElevatedQuotaExceeded { cap: 100 })
    );
    // A failed settle releases the budget.
    f.store.elevated_settle(&scope(), id1, false).await.unwrap();
    assert!(f
        .store
        .elevated_reserve(&scope(), &mint_req(CALLER, m, f.bob, 50, &k3))
        .await
        .is_ok());
}

#[tokio::test]
async fn reserve_replay_is_counted_once_and_a_changed_call_conflicts() {
    let f = fixture().await;
    grant(&f, CALLER, MINT, r#"{"max_per_op":60,"max_per_day":100}"#).await;
    let m = ElevatedPermission::AppMint;
    let k = key();
    let r = mint_req(CALLER, m, f.alice, 60, &k);
    let id = f.store.elevated_reserve(&scope(), &r).await.unwrap();
    // Replays of the same call (reserved, then applied) return the same row.
    assert_eq!(f.store.elevated_reserve(&scope(), &r).await, Ok(id));
    f.store.elevated_settle(&scope(), id, true).await.unwrap();
    assert_eq!(f.store.elevated_reserve(&scope(), &r).await, Ok(id));
    assert_eq!(audit_count(&f).await, 1);
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT COALESCE(SUM(amount), 0)::BIGINT FROM economy_elevated_audit WHERE status IN (1, 2)"
        )
        .await,
        60,
        "counted once"
    );
    // A late failed-settle of an already APPLIED row never rewrites it.
    f.store.elevated_settle(&scope(), id, false).await.unwrap();
    assert_eq!(
        scalar_i64(&f.su, "SELECT status::BIGINT FROM economy_elevated_audit").await,
        2
    );
    // Same key, different amount / target: conflict.
    for other in [
        mint_req(CALLER, m, f.alice, 50, &k),
        mint_req(CALLER, m, f.bob, 60, &k),
    ] {
        assert_eq!(
            f.store.elevated_reserve(&scope(), &other).await,
            Err(EconomyError::IdempotencyConflict)
        );
    }
}

#[tokio::test]
async fn a_failed_reservation_retried_under_the_same_key_is_readmitted() {
    let f = fixture().await;
    grant(&f, CALLER, MINT, r#"{"max_per_op":60,"max_per_day":60}"#).await;
    let m = ElevatedPermission::AppMint;
    let k = key();
    let r = mint_req(CALLER, m, f.alice, 60, &k);
    let id = f.store.elevated_reserve(&scope(), &r).await.unwrap();
    f.store.elevated_settle(&scope(), id, false).await.unwrap();
    // The failed row is not counted, so the retry (the whole day budget) fits.
    assert_eq!(f.store.elevated_reserve(&scope(), &r).await, Ok(id));
    assert_eq!(
        scalar_i64(&f.su, "SELECT status::BIGINT FROM economy_elevated_audit").await,
        1
    );
    assert_eq!(audit_count(&f).await, 1);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn concurrent_reservations_cannot_overshoot_the_day_cap() {
    let f = fixture().await;
    grant(
        &f,
        CALLER,
        MINT,
        r#"{"max_per_op":50,"max_per_day":200,"max_per_target_per_day":50000}"#,
    )
    .await;
    let store = Arc::new(PostgresEconomyStore::new(f.eco_conn.clone()));
    let mut tasks = Vec::new();
    for _ in 0..30 {
        let store = Arc::clone(&store);
        let alice = f.alice;
        tasks.push(tokio::spawn(async move {
            let k = key();
            store
                .elevated_reserve(
                    &scope(),
                    &mint_req(CALLER, ElevatedPermission::AppMint, alice, 50, &k),
                )
                .await
        }));
    }
    let mut ok = 0;
    for t in tasks {
        match t.await.unwrap() {
            Ok(_) => ok += 1,
            Err(EconomyError::ElevatedQuotaExceeded { cap: 200 }) => {}
            Err(other) => panic!("unexpected error: {other:?}"),
        }
    }
    assert_eq!(ok, 4, "200/50 reservations, never more");
}
