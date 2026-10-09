//! Integration tests against a **real** Postgres container: the store's
//! single-statement atomic debit/credit, the concurrent-wager overdraw proof,
//! the deadlock-free opposite-direction transfers, the ledger, the live
//! membership predicate and the `waddles_economy_runtime` role's privilege
//! boundary can only be proven by a real server. The schema under test is the
//! EXACT shipped DDL -- `scripts/db/bundle_economy_store.sql`, which alembic
//! 0044 also executes -- applied on top of hand-written minimal prerequisites
//! (`tenants`, `communities`, `community_members`; the full legacy baseline
//! needs the whole docker stack, see `alembic/tests/pg_docker.py`).
//!
//! Requires Docker via `testcontainers`. Run with
//! `cargo test -p bundle-host-economy --test postgres_integration`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bundle_capability_gate::{
    HostInvokeScopeBuilder, MembershipCheck, ScopeKind, SnapshotMembership, TenantTier,
};
use bundle_host_economy::{
    connect, load_membership, ConnectConfig, EconomyError, EconomyScope, EconomyStore,
    LeaderboardEntry, PostgresEconomyStore, MAX_LEADERBOARD_LIMIT,
};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection, Statement};
use testcontainers::core::logs::LogSource;
use testcontainers::core::wait::LogWaitStrategy;
use testcontainers::core::{ContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{ContainerAsync, GenericImage, ImageExt};
use uuid::Uuid;

const POSTGRES_IMAGE: &str = "postgres";
const POSTGRES_TAG: &str = "17.6-bookworm";
const SUPERUSER_PASSWORD: &str = "postgres_test_superuser_pw";
const ECONOMY_PASSWORD: &str = "waddles_economy_runtime_test_pw";
const APP_ID: &str = "waddles.core.test-economy";
const SHIPPED_DDL: &str = include_str!("../../../scripts/db/bundle_economy_store.sql");

const TENANT: i32 = 1;
const COMMUNITY: i32 = 10;
const OTHER_TENANT: i32 = 2;
const OTHER_COMMUNITY: i32 = 20;

struct Fixture {
    _container: ContainerAsync<GenericImage>,
    su: DatabaseConnection,
    store: PostgresEconomyStore,
    eco_conn: DatabaseConnection,
    alice: Uuid,
    bob: Uuid,
}

fn scope() -> EconomyScope {
    EconomyScope {
        tenant_id: TENANT,
        community_id: COMMUNITY,
        app_id: APP_ID.to_string(),
    }
}

async fn exec(conn: &DatabaseConnection, sql: &str) {
    conn.execute_unprepared(sql)
        .await
        .unwrap_or_else(|e| panic!("sql failed: {e}\n{sql}"));
}

async fn scalar_i64(conn: &DatabaseConnection, sql: &str) -> i64 {
    conn.query_one_raw(Statement::from_string(sea_orm::DbBackend::Postgres, sql))
        .await
        .unwrap()
        .unwrap()
        .try_get_by_index::<i64>(0)
        .unwrap()
}

async fn fixture() -> Fixture {
    let container = GenericImage::new(POSTGRES_IMAGE, POSTGRES_TAG)
        .with_exposed_port(ContainerPort::Tcp(5432))
        .with_wait_for(WaitFor::log(
            LogWaitStrategy::new(
                LogSource::BothStd,
                "database system is ready to accept connections",
            )
            .with_times(2),
        ))
        .with_env_var("POSTGRES_PASSWORD", SUPERUSER_PASSWORD)
        .with_env_var("POSTGRES_DB", "waddles_test")
        .start()
        .await
        .expect("postgres test container starts");
    let host = container.get_host().await.unwrap().to_string();
    let port = container.get_host_port_ipv4(5432).await.unwrap();
    let su_url = format!("postgres://postgres:{SUPERUSER_PASSWORD}@{host}:{port}/waddles_test");
    let su = Database::connect(&su_url)
        .await
        .expect("superuser connects");

    // Prerequisites (minimal shapes of the legacy tables).
    exec(
        &su,
        "CREATE TABLE tenants (id SERIAL PRIMARY KEY, slug TEXT);
         CREATE TABLE communities (id SERIAL PRIMARY KEY, tenant_id INTEGER REFERENCES tenants(id));
         CREATE TABLE community_members (
             id SERIAL PRIMARY KEY,
             community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
             user_id VARCHAR(255),
             is_active BOOLEAN DEFAULT true,
             left_at TIMESTAMP,
             removed_at TIMESTAMP);
         CREATE TABLE hub_users (id SERIAL PRIMARY KEY);
         INSERT INTO tenants (id, slug) VALUES (1, 't1'), (2, 't2');
         INSERT INTO communities (id, tenant_id) VALUES (10, 1), (20, 2);",
    )
    .await;
    // The role must exist BEFORE the shipped DDL so its grants block fires
    // (same ordering alembic 0044 guarantees).
    exec(
        &su,
        &format!(
            "CREATE ROLE waddles_economy_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE \
             NOREPLICATION PASSWORD '{ECONOMY_PASSWORD}'"
        ),
    )
    .await;
    exec(&su, SHIPPED_DDL).await;
    // Shipped DDL is idempotent: a second application must succeed.
    exec(&su, SHIPPED_DDL).await;

    let (alice, bob) = (Uuid::new_v4(), Uuid::new_v4());
    exec(
        &su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid) VALUES (10, '{alice}'), (10, '{bob}')"
        ),
    )
    .await;

    let eco_conn = connect(
        &ConnectConfig {
            host,
            port,
            name: "waddles_test".to_string(),
            user: "waddles_economy_runtime".to_string(),
        },
        ECONOMY_PASSWORD,
    )
    .await
    .expect("economy role connects");
    let store = PostgresEconomyStore::new(eco_conn.clone());
    Fixture {
        _container: container,
        su,
        store,
        eco_conn,
        alice,
        bob,
    }
}

/// Funds `user` as the privileged hub-side writer (the runtime role cannot mint).
async fn fund(f: &Fixture, user: Uuid, amount: i64) {
    exec(
        &f.su,
        &format!(
            "INSERT INTO economy_balances (tenant_id, community_id, user_uuid, balance) \
             VALUES (1, 10, '{user}', {amount}) \
             ON CONFLICT (tenant_id, community_id, user_uuid) DO UPDATE SET balance = {amount}"
        ),
    )
    .await;
}

async fn ledger_count(f: &Fixture) -> i64 {
    scalar_i64(&f.su, "SELECT COUNT(*) FROM economy_ledger").await
}

async fn total_balance(f: &Fixture) -> i64 {
    scalar_i64(
        &f.su,
        "SELECT COALESCE(SUM(balance), 0)::BIGINT FROM economy_balances",
    )
    .await
}

#[tokio::test]
async fn balance_and_max_bet_read_the_funded_row_and_zero_for_an_unfunded_member() {
    let f = fixture().await;
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(0));
    assert_eq!(f.store.max_bet(&scope(), f.alice, 50).await, Ok(0));
    fund(&f, f.alice, 120).await;
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(120));
    // min(cap, balance): cap below balance, then balance below cap.
    assert_eq!(f.store.max_bet(&scope(), f.alice, 50).await, Ok(50));
    assert_eq!(f.store.max_bet(&scope(), f.alice, 500).await, Ok(120));
    assert!(matches!(
        f.store.max_bet(&scope(), f.alice, -1).await,
        Err(EconomyError::Invalid(_))
    ));
    // Reads wrote nothing.
    assert_eq!(ledger_count(&f).await, 0);
}

#[tokio::test]
async fn wager_win_and_loss_apply_net_delta_with_one_ledger_row_each() {
    let f = fixture().await;
    fund(&f, f.alice, 100).await;
    // win: stake 10, payout 25 -> +15
    assert_eq!(f.store.wager(&scope(), f.alice, 10, 25, 50).await, Ok(115));
    // loss: stake 40, payout 0 -> -40
    assert_eq!(f.store.wager(&scope(), f.alice, 40, 0, 50).await, Ok(75));
    // push: stake 5 payout 5 -> unchanged
    assert_eq!(f.store.wager(&scope(), f.alice, 5, 5, 50).await, Ok(75));
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(75));
    assert_eq!(ledger_count(&f).await, 3);
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT SUM(delta)::BIGINT FROM economy_ledger WHERE kind = 'wager'"
        )
        .await,
        15 - 40,
        "ledger deltas reconcile with the balance movement"
    );
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT balance_after FROM economy_ledger ORDER BY id DESC LIMIT 1"
        )
        .await,
        75
    );
    // The ledger carries the host-derived app id, not guest input.
    assert_eq!(
        scalar_i64(
            &f.su,
            &format!("SELECT COUNT(*) FROM economy_ledger WHERE app_id = '{APP_ID}'")
        )
        .await,
        3
    );
}

#[tokio::test]
async fn wager_cannot_stake_more_than_the_balance_even_with_a_big_payout() {
    let f = fixture().await;
    fund(&f, f.alice, 10).await;
    // balance 10 < stake 11: refused even though payout would more than cover it.
    assert_eq!(
        f.store.wager(&scope(), f.alice, 11, 1_000, 50).await,
        Err(EconomyError::InsufficientFunds { balance: 10 })
    );
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(10));
    assert_eq!(ledger_count(&f).await, 0);
    // An unfunded member (no row) reads as insufficient with balance 0.
    assert_eq!(
        f.store.wager(&scope(), f.bob, 1, 0, 50).await,
        Err(EconomyError::InsufficientFunds { balance: 0 })
    );
}

#[tokio::test]
async fn max_bet_and_payout_multiple_are_enforced_server_side_before_any_write() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000_000).await;
    assert_eq!(
        f.store.wager(&scope(), f.alice, 51, 0, 50).await,
        Err(EconomyError::OverCap { cap: 50 })
    );
    assert_eq!(
        f.store.wager(&scope(), f.alice, 10, 1_001, 50).await,
        Err(EconomyError::OverCap { cap: 1_000 })
    );
    for (stake, payout) in [(0, 0), (-1, 0), (5, -1)] {
        assert!(matches!(
            f.store.wager(&scope(), f.alice, stake, payout, 50).await,
            Err(EconomyError::Invalid(_))
        ));
    }
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(1_000_000));
    assert_eq!(ledger_count(&f).await, 0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn concurrent_wagers_never_overdraw_the_balance() {
    let f = fixture().await;
    fund(&f, f.alice, 100).await;
    let store = Arc::new(PostgresEconomyStore::new(f.eco_conn.clone()));
    let mut tasks = Vec::new();
    // 60 concurrent losing wagers of 10 against a balance of 100: exactly 10
    // can ever succeed; the other 50 must be refused, never overdraw.
    for _ in 0..60 {
        let store = Arc::clone(&store);
        let user = f.alice;
        tasks.push(tokio::spawn(async move {
            store.wager(&scope(), user, 10, 0, 50).await
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
    assert_eq!(ok, 10, "exactly balance/stake wagers apply");
    assert_eq!(insufficient, 50);
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(0));
    assert_eq!(ledger_count(&f).await, 10);
    assert_eq!(
        scalar_i64(&f.su, "SELECT MIN(balance) FROM economy_balances").await,
        0,
        "the balance never went negative"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn concurrent_mixed_wagers_keep_the_ledger_and_balance_in_lockstep() {
    let f = fixture().await;
    fund(&f, f.alice, 200).await;
    let store = Arc::new(PostgresEconomyStore::new(f.eco_conn.clone()));
    let mut tasks = Vec::new();
    // Alternating win (stake 10, payout 30: +20) and loss (stake 10: -10).
    for i in 0..40 {
        let store = Arc::clone(&store);
        let user = f.alice;
        tasks.push(tokio::spawn(async move {
            let payout = if i % 2 == 0 { 30 } else { 0 };
            store.wager(&scope(), user, 10, payout, 50).await
        }));
    }
    let mut applied = 0;
    for t in tasks {
        if t.await.unwrap().is_ok() {
            applied += 1;
        }
    }
    let balance = f.store.balance(&scope(), f.alice).await.unwrap();
    assert!(balance >= 0);
    // Every applied wager has exactly one ledger row, and the balance equals
    // the opening balance plus the ledger's net delta: no lost update.
    assert_eq!(ledger_count(&f).await, applied);
    assert_eq!(
        balance,
        200 + scalar_i64(
            &f.su,
            "SELECT COALESCE(SUM(delta), 0)::BIGINT FROM economy_ledger"
        )
        .await
    );
}

#[tokio::test]
async fn transfer_moves_money_atomically_and_writes_both_ledger_sides() {
    let f = fixture().await;
    fund(&f, f.alice, 100).await;
    assert_eq!(
        f.store.transfer(&scope(), f.alice, f.bob, 30, 100).await,
        Ok(())
    );
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(70));
    assert_eq!(
        f.store.balance(&scope(), f.bob).await,
        Ok(30),
        "a recipient with no prior row is created by the transfer"
    );
    assert_eq!(total_balance(&f).await, 100, "currency is conserved");
    assert_eq!(ledger_count(&f).await, 2);
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT COUNT(*) FROM economy_ledger WHERE kind = 'transfer_out' AND delta = -30 \
             AND balance_after = 70"
        )
        .await,
        1
    );
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT COUNT(*) FROM economy_ledger WHERE kind = 'transfer_in' AND delta = 30 \
             AND balance_after = 30"
        )
        .await,
        1
    );
}

#[tokio::test]
async fn transfer_refusals_move_nothing() {
    let f = fixture().await;
    fund(&f, f.alice, 10).await;
    // insufficient funds
    assert_eq!(
        f.store.transfer(&scope(), f.alice, f.bob, 11, 100).await,
        Err(EconomyError::InsufficientFunds { balance: 10 })
    );
    // cap
    assert_eq!(
        f.store.transfer(&scope(), f.alice, f.bob, 6, 5).await,
        Err(EconomyError::OverCap { cap: 5 })
    );
    // self / non-positive
    for (to, amount) in [(f.alice, 1), (f.bob, 0), (f.bob, -4)] {
        assert!(matches!(
            f.store.transfer(&scope(), f.alice, to, amount, 100).await,
            Err(EconomyError::Invalid(_))
        ));
    }
    // sender with no row at all
    assert_eq!(
        f.store.transfer(&scope(), f.bob, f.alice, 1, 100).await,
        Err(EconomyError::InsufficientFunds { balance: 0 })
    );
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(10));
    assert_eq!(total_balance(&f).await, 10);
    assert_eq!(ledger_count(&f).await, 0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn opposite_direction_concurrent_transfers_do_not_deadlock_and_conserve_currency() {
    let f = fixture().await;
    fund(&f, f.alice, 500).await;
    fund(&f, f.bob, 500).await;
    let store = Arc::new(PostgresEconomyStore::new(f.eco_conn.clone()));
    let mut tasks = Vec::new();
    for i in 0..60 {
        let store = Arc::clone(&store);
        let (a, b) = (f.alice, f.bob);
        tasks.push(tokio::spawn(async move {
            if i % 2 == 0 {
                store.transfer(&scope(), a, b, 7, 100).await
            } else {
                store.transfer(&scope(), b, a, 3, 100).await
            }
        }));
    }
    for t in tasks {
        // 1000 total funds, tiny amounts: every transfer must apply -- a
        // deadlock-abort would surface here as a Backend error.
        assert_eq!(t.await.unwrap(), Ok(()));
    }
    assert_eq!(total_balance(&f).await, 1_000);
    assert_eq!(
        f.store.balance(&scope(), f.alice).await,
        Ok(500 - 30 * 7 + 30 * 3)
    );
    assert_eq!(
        f.store.balance(&scope(), f.bob).await,
        Ok(500 + 30 * 7 - 30 * 3)
    );
    assert_eq!(ledger_count(&f).await, 120);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn concurrent_transfers_out_of_one_balance_never_overdraw() {
    let f = fixture().await;
    fund(&f, f.alice, 100).await;
    let store = Arc::new(PostgresEconomyStore::new(f.eco_conn.clone()));
    let mut tasks = Vec::new();
    for _ in 0..40 {
        let store = Arc::clone(&store);
        let (a, b) = (f.alice, f.bob);
        tasks.push(tokio::spawn(async move {
            store.transfer(&scope(), a, b, 10, 100).await
        }));
    }
    let (mut ok, mut insufficient) = (0, 0);
    for t in tasks {
        match t.await.unwrap() {
            Ok(()) => ok += 1,
            Err(EconomyError::InsufficientFunds { .. }) => insufficient += 1,
            Err(other) => panic!("unexpected error: {other:?}"),
        }
    }
    assert_eq!((ok, insufficient), (10, 30));
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(0));
    assert_eq!(f.store.balance(&scope(), f.bob).await, Ok(100));
}

#[tokio::test]
async fn non_members_are_rejected_for_every_flavor_of_non_membership() {
    let f = fixture().await;
    let stranger = Uuid::new_v4();
    let left = Uuid::new_v4();
    let removed = Uuid::new_v4();
    let inactive = Uuid::new_v4();
    let other_community = Uuid::new_v4();
    exec(
        &f.su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid, left_at) VALUES (10, '{left}', NOW());
             INSERT INTO community_members (community_id, user_uuid, removed_at) VALUES (10, '{removed}', NOW());
             INSERT INTO community_members (community_id, user_uuid, is_active) VALUES (10, '{inactive}', false);
             INSERT INTO community_members (community_id, user_uuid) VALUES (20, '{other_community}');"
        ),
    )
    .await;
    // Give every one of them a (privileged-written) balance: the membership
    // predicate, not the absence of a row, is what must refuse them.
    for who in [stranger, left, removed, inactive] {
        fund(&f, who, 1_000).await;
    }
    for who in [stranger, left, removed, inactive, other_community] {
        assert_eq!(
            f.store.wager(&scope(), who, 1, 0, 100).await,
            Err(EconomyError::NotAMember),
            "wager {who}"
        );
        assert_eq!(
            f.store.balance(&scope(), who).await,
            Err(EconomyError::NotAMember),
            "balance {who}"
        );
        assert_eq!(
            f.store.max_bet(&scope(), who, 10).await,
            Err(EconomyError::NotAMember),
            "max_bet {who}"
        );
    }
    fund(&f, f.alice, 100).await;
    // Transfer: a non-member on EITHER side is refused, funded or not.
    assert_eq!(
        f.store.transfer(&scope(), f.alice, left, 1, 100).await,
        Err(EconomyError::NotAMember)
    );
    assert_eq!(
        f.store.transfer(&scope(), removed, f.alice, 1, 100).await,
        Err(EconomyError::NotAMember)
    );
    // Tenant mismatch: the member is real, the scope names the wrong tenant.
    let wrong_tenant = EconomyScope {
        tenant_id: OTHER_TENANT,
        community_id: COMMUNITY,
        app_id: APP_ID.to_string(),
    };
    assert_eq!(
        f.store.wager(&wrong_tenant, f.alice, 1, 0, 100).await,
        Err(EconomyError::NotAMember)
    );
    // A member of community 20 is not reachable through tenant 1 + community 20.
    let cross = EconomyScope {
        tenant_id: TENANT,
        community_id: OTHER_COMMUNITY,
        app_id: APP_ID.to_string(),
    };
    assert_eq!(
        f.store.wager(&cross, other_community, 1, 0, 100).await,
        Err(EconomyError::NotAMember)
    );
    // Nothing was written by any rejected call.
    assert_eq!(ledger_count(&f).await, 0);
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(100));
}

#[tokio::test]
async fn a_member_with_a_null_user_uuid_is_unreachable_fail_closed() {
    let f = fixture().await;
    exec(
        &f.su,
        "INSERT INTO community_members (community_id) VALUES (10), (10)",
    )
    .await;
    // No UUID can ever name a NULL-uuid row (#429: identity not yet minted).
    assert_eq!(
        f.store.balance(&scope(), Uuid::nil()).await,
        Err(EconomyError::NotAMember)
    );
    assert_eq!(
        f.store.wager(&scope(), Uuid::nil(), 1, 0, 10).await,
        Err(EconomyError::NotAMember)
    );
}

#[tokio::test]
async fn leaderboard_ranks_active_members_only_within_the_community() {
    let f = fixture().await;
    let carol = Uuid::new_v4();
    let departed = Uuid::new_v4();
    let t2_member = Uuid::new_v4();
    exec(
        &f.su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid) VALUES (10, '{carol}');
             INSERT INTO community_members (community_id, user_uuid, left_at) VALUES (10, '{departed}', NOW());
             INSERT INTO community_members (community_id, user_uuid) VALUES (20, '{t2_member}');"
        ),
    )
    .await;
    fund(&f, f.alice, 50).await;
    fund(&f, f.bob, 300).await;
    fund(&f, carol, 50).await;
    fund(&f, departed, 9_999).await;
    exec(
        &f.su,
        &format!(
            "INSERT INTO economy_balances (tenant_id, community_id, user_uuid, balance) \
             VALUES (2, 20, '{t2_member}', 8888)"
        ),
    )
    .await;
    let board = f.store.leaderboard(&scope(), 10).await.unwrap();
    let mut tied = [f.alice, carol];
    tied.sort();
    assert_eq!(
        board,
        vec![
            LeaderboardEntry {
                user: f.bob,
                balance: 300
            },
            LeaderboardEntry {
                user: tied[0],
                balance: 50
            },
            LeaderboardEntry {
                user: tied[1],
                balance: 50
            },
        ],
        "highest first, ties by user id; departed and other-community members excluded"
    );
    assert_eq!(f.store.leaderboard(&scope(), 1).await.unwrap().len(), 1);
    for bad in [0, MAX_LEADERBOARD_LIMIT + 1] {
        assert!(matches!(
            f.store.leaderboard(&scope(), bad).await,
            Err(EconomyError::Invalid(_))
        ));
    }
}

#[tokio::test]
async fn the_database_check_constraint_is_an_independent_overdraw_backstop() {
    let f = fixture().await;
    fund(&f, f.alice, 5).await;
    // Even a privileged writer bypassing the store cannot drive a balance negative.
    assert!(f
        .su
        .execute_unprepared("UPDATE economy_balances SET balance = balance - 6")
        .await
        .is_err());
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(5));
}

#[tokio::test]
async fn a_failing_statement_rolls_everything_back() {
    let f = fixture().await;
    fund(&f, f.alice, 100).await;
    // A balance near i64::MAX makes `balance - stake + payout` overflow inside the
    // guarded UPDATE: the statement fails as a whole (Backend), moving no money
    // and writing no ledger row.
    fund(&f, f.bob, i64::MAX - 1).await;
    let err = f
        .store
        .wager(&scope(), f.bob, 10, 1_000, 50)
        .await
        .unwrap_err();
    assert!(matches!(err, EconomyError::Backend(_)), "{err:?}");
    assert_eq!(f.store.balance(&scope(), f.bob).await, Ok(i64::MAX - 1));
    assert_eq!(ledger_count(&f).await, 0);
}

#[tokio::test]
async fn role_is_least_privilege_dml_only_append_only_ledger_no_foreign_tables() {
    let f = fixture().await;
    fund(&f, f.alice, 100).await;
    f.store.wager(&scope(), f.alice, 1, 0, 50).await.unwrap();
    for (sql, why) in [
        ("DELETE FROM economy_balances", "no DELETE on balances"),
        (
            "UPDATE economy_ledger SET delta = 99",
            "ledger is append-only (no UPDATE)",
        ),
        (
            "DELETE FROM economy_ledger",
            "ledger is append-only (no DELETE)",
        ),
        ("SELECT * FROM hub_users", "no access to unrelated tables"),
        (
            "SELECT * FROM community_members",
            "membership SELECT is column-scoped, not whole-table",
        ),
        (
            "INSERT INTO community_members (community_id) VALUES (10)",
            "no DML on membership",
        ),
        (
            "UPDATE communities SET tenant_id = 2",
            "no DML on communities",
        ),
        ("CREATE TABLE evil (x int)", "no DDL"),
        (
            "UPDATE economy_balances SET balance = balance - 1000",
            "the CHECK constraint holds for the runtime role too",
        ),
    ] {
        assert!(
            f.eco_conn.execute_unprepared(sql).await.is_err(),
            "should be denied ({why}): {sql}"
        );
    }
    // The grants it DOES have.
    assert!(f
        .eco_conn
        .execute_unprepared("SELECT community_id, user_uuid, is_active FROM community_members")
        .await
        .is_ok());
}

#[tokio::test]
async fn membership_loader_returns_only_active_identified_members() {
    let f = fixture().await;
    let departed = Uuid::new_v4();
    let t2_member = Uuid::new_v4();
    exec(
        &f.su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid, removed_at) VALUES (10, '{departed}', NOW());
             INSERT INTO community_members (community_id, user_uuid) VALUES (20, '{t2_member}');
             INSERT INTO community_members (community_id) VALUES (10);"
        ),
    )
    .await;
    let rows = load_membership(&f.eco_conn, Some(TENANT)).await.unwrap();
    assert_eq!(rows.len(), 2, "alice + bob: {rows:?}");
    assert!(rows
        .iter()
        .all(|r| (r.tenant_id, r.community_id) == (TENANT, COMMUNITY)));

    // `None` = every tenant: includes the other tenant's identified member.
    let all = load_membership(&f.eco_conn, None).await.unwrap();
    assert_eq!(all.len(), 3, "{all:?}");

    // Feed the gate's production MembershipCheck end to end.
    let snapshot = SnapshotMembership::new();
    assert!(snapshot.replace_all(rows));
    let gate_scope = HostInvokeScopeBuilder::new()
        .tenant_id(TENANT)
        .community_id(COMMUNITY)
        .app_id(APP_ID)
        .app_version(1)
        .tenant_tier(TenantTier::Free)
        .build()
        .unwrap();
    assert!(snapshot.is_member(&gate_scope, f.alice, ScopeKind::Community));
    assert!(!snapshot.is_member(&gate_scope, departed, ScopeKind::Community));
    assert!(!snapshot.is_member(&gate_scope, t2_member, ScopeKind::Community));
}
