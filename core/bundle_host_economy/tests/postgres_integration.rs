//! Integration tests against a **real** Postgres container: the store's
//! single-statement atomic debit/credit, the concurrent-wager overdraw proof,
//! the deadlock-free opposite-direction transfers, the ledger, the live
//! membership predicate and the `waddles_economy_runtime` role's privilege
//! boundary can only be proven by a real server. The schema under test is the
//! EXACT shipped DDL -- `scripts/db/bundle_economy_store.sql`, which alembic
//! 0047 also executes -- applied on top of hand-written minimal prerequisites
//! (`tenants`, `communities`, `community_members`; the full legacy baseline
//! needs the whole docker stack, see `alembic/tests/pg_docker.py`).
//!
//! Requires Docker via `testcontainers`. Run with
//! `cargo test -p bundle-host-economy --test postgres_integration`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::{Arc, OnceLock};
use std::time::Duration;

use bundle_capability_gate::{
    HostInvokeScopeBuilder, MembershipCheck, ScopeKind, SnapshotMembership, TenantTier,
};
use bundle_host_economy::{
    connect, load_membership, ConnectConfig, EconomyCaps, EconomyError, EconomyScope, EconomyStore,
    IdempotencyKey, LeaderboardEntry, PostgresEconomyStore, MAX_LEADERBOARD_LIMIT,
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
/// Alembic 0049's DDL, applied after the store DDL exactly as the migration
/// chain does.
const IDEMPOTENCY_DDL: &str = include_str!("../../../scripts/db/bundle_economy_idempotency.sql");

const TENANT: i32 = 1;
const COMMUNITY: i32 = 10;
const OTHER_TENANT: i32 = 2;
const OTHER_COMMUNITY: i32 = 20;

/// Every test owns its own Postgres container (full isolation), but starting
/// 25 of them at once starves a small CI runner past the default 60s startup
/// timeout. A small permit pool throttles only the START (the permit is dropped
/// as soon as the container is ready); the tests themselves still run in
/// parallel.
const MAX_CONCURRENT_CONTAINER_STARTS: usize = 3;
const CONTAINER_STARTUP_TIMEOUT: Duration = Duration::from_secs(240);

fn start_gate() -> &'static tokio::sync::Semaphore {
    static GATE: OnceLock<tokio::sync::Semaphore> = OnceLock::new();
    GATE.get_or_init(|| tokio::sync::Semaphore::new(MAX_CONCURRENT_CONTAINER_STARTS))
}

struct Fixture {
    _container: ContainerAsync<GenericImage>,
    su: DatabaseConnection,
    store: PostgresEconomyStore,
    eco_conn: DatabaseConnection,
    host: String,
    port: u16,
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
    let permit = start_gate()
        .acquire()
        .await
        .expect("the start gate is never closed");
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
        .with_startup_timeout(CONTAINER_STARTUP_TIMEOUT)
        .start()
        .await
        .expect("postgres test container starts");
    drop(permit);
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
    // (same ordering alembic 0047 guarantees).
    exec(
        &su,
        &format!(
            "CREATE ROLE waddles_economy_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE \
             NOREPLICATION PASSWORD '{ECONOMY_PASSWORD}'"
        ),
    )
    .await;
    exec(&su, SHIPPED_DDL).await;
    exec(&su, IDEMPOTENCY_DDL).await;
    // Shipped DDL is idempotent: a second application must succeed.
    exec(&su, SHIPPED_DDL).await;
    exec(&su, IDEMPOTENCY_DDL).await;

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
            host: host.clone(),
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
        host,
        port,
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
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 10, 25, 50, caps(), &key())
            .await,
        Ok(115)
    );
    // loss: stake 40, payout 0 -> -40
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 40, 0, 50, caps(), &key())
            .await,
        Ok(75)
    );
    // push: stake 5 payout 5 -> unchanged
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 5, 5, 50, caps(), &key())
            .await,
        Ok(75)
    );
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
        f.store
            .wager(&scope(), f.alice, 11, 1_000, 50, caps(), &key())
            .await,
        Err(EconomyError::InsufficientFunds { balance: 10 })
    );
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(10));
    assert_eq!(ledger_count(&f).await, 0);
    // An unfunded member (no row) reads as insufficient with balance 0.
    assert_eq!(
        f.store
            .wager(&scope(), f.bob, 1, 0, 50, caps(), &key())
            .await,
        Err(EconomyError::InsufficientFunds { balance: 0 })
    );
}

#[tokio::test]
async fn max_bet_and_payout_multiple_are_enforced_server_side_before_any_write() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000_000).await;
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 51, 0, 50, caps(), &key())
            .await,
        Err(EconomyError::OverCap { cap: 50 })
    );
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 10, 1_001, 50, caps(), &key())
            .await,
        Err(EconomyError::OverCap { cap: 1_000 })
    );
    for (stake, payout) in [(0, 0), (-1, 0), (5, -1)] {
        assert!(matches!(
            f.store
                .wager(&scope(), f.alice, stake, payout, 50, caps(), &key())
                .await,
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
            store.wager(&scope(), user, 10, 0, 50, caps(), &key()).await
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
            store
                .wager(&scope(), user, 10, payout, 50, caps(), &key())
                .await
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
        f.store
            .transfer(&scope(), f.alice, f.bob, 30, 100, caps(), &key())
            .await,
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
        f.store
            .transfer(&scope(), f.alice, f.bob, 11, 100, caps(), &key())
            .await,
        Err(EconomyError::InsufficientFunds { balance: 10 })
    );
    // cap
    assert_eq!(
        f.store
            .transfer(&scope(), f.alice, f.bob, 6, 5, caps(), &key())
            .await,
        Err(EconomyError::OverCap { cap: 5 })
    );
    // self / non-positive
    for (to, amount) in [(f.alice, 1), (f.bob, 0), (f.bob, -4)] {
        assert!(matches!(
            f.store
                .transfer(&scope(), f.alice, to, amount, 100, caps(), &key())
                .await,
            Err(EconomyError::Invalid(_))
        ));
    }
    // sender with no row at all
    assert_eq!(
        f.store
            .transfer(&scope(), f.bob, f.alice, 1, 100, caps(), &key())
            .await,
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
                store.transfer(&scope(), a, b, 7, 100, caps(), &key()).await
            } else {
                store.transfer(&scope(), b, a, 3, 100, caps(), &key()).await
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
            store
                .transfer(&scope(), a, b, 10, 100, caps(), &key())
                .await
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
            f.store
                .wager(&scope(), who, 1, 0, 100, caps(), &key())
                .await,
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
        f.store
            .transfer(&scope(), f.alice, left, 1, 100, caps(), &key())
            .await,
        Err(EconomyError::NotAMember)
    );
    assert_eq!(
        f.store
            .transfer(&scope(), removed, f.alice, 1, 100, caps(), &key())
            .await,
        Err(EconomyError::NotAMember)
    );
    // Tenant mismatch: the member is real, the scope names the wrong tenant.
    let wrong_tenant = EconomyScope {
        tenant_id: OTHER_TENANT,
        community_id: COMMUNITY,
        app_id: APP_ID.to_string(),
    };
    assert_eq!(
        f.store
            .wager(&wrong_tenant, f.alice, 1, 0, 100, caps(), &key())
            .await,
        Err(EconomyError::NotAMember)
    );
    // A member of community 20 is not reachable through tenant 1 + community 20.
    let cross = EconomyScope {
        tenant_id: TENANT,
        community_id: OTHER_COMMUNITY,
        app_id: APP_ID.to_string(),
    };
    assert_eq!(
        f.store
            .wager(&cross, other_community, 1, 0, 100, caps(), &key())
            .await,
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
        f.store
            .wager(&scope(), Uuid::nil(), 1, 0, 10, caps(), &key())
            .await,
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
        .wager(&scope(), f.bob, 10, 1_000, 50, caps(), &key())
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
    f.store
        .wager(&scope(), f.alice, 1, 0, 50, caps(), &key())
        .await
        .unwrap();
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

// ---- durable rolling-24h aggregates (review hardening, mirrors #741) -------

/// Generous caps for the tests that are not about the aggregates.
/// A fresh, never-reused idempotency key: every call that is NOT deliberately
/// a replay gets its own, exactly as the host mints one per mutation.
fn key() -> IdempotencyKey {
    IdempotencyKey::new(format!("test:{}", Uuid::new_v4())).expect("a valid key")
}

fn caps() -> EconomyCaps {
    EconomyCaps {
        per_user_daily_max: 10_000,
        per_scope_daily_max: 250_000,
    }
}

fn caps_of(per_user: i64, per_scope: i64) -> EconomyCaps {
    EconomyCaps {
        per_user_daily_max: per_user,
        per_scope_daily_max: per_scope,
    }
}

fn scope_of(tenant: i32, community: i32, app: &str) -> EconomyScope {
    EconomyScope {
        tenant_id: tenant,
        community_id: community,
        app_id: app.to_string(),
    }
}

#[tokio::test]
async fn the_per_user_daily_stake_cap_is_enforced_from_the_ledger_and_rolls_off() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000).await;
    let c = caps_of(30, 250_000);
    for _ in 0..3 {
        f.store
            .wager(&scope(), f.alice, 10, 0, 50, c, &key())
            .await
            .unwrap();
    }
    // 30 staked: one more unit is over the per-user cap.
    assert_eq!(
        f.store.wager(&scope(), f.alice, 1, 0, 50, c, &key()).await,
        Err(EconomyError::UserQuotaExceeded { cap: 30 })
    );
    // A refused call wrote no ledger row and so consumed nothing.
    assert_eq!(ledger_count(&f).await, 3);
    // Insufficient-funds refusals do not count either: bob (unfunded) fails
    // on funds, never on the shared scope budget.
    assert_eq!(
        f.store.wager(&scope(), f.bob, 5, 0, 50, c, &key()).await,
        Err(EconomyError::InsufficientFunds { balance: 0 })
    );
    // Age every ledger row out of the window: the cap resets.
    exec(
        &f.su,
        "UPDATE economy_ledger SET occurred_at = NOW() - INTERVAL '25 hours'",
    )
    .await;
    assert!(f
        .store
        .wager(&scope(), f.alice, 30, 0, 50, c, &key())
        .await
        .is_ok());
    assert_eq!(
        f.store.wager(&scope(), f.alice, 1, 0, 50, c, &key()).await,
        Err(EconomyError::UserQuotaExceeded { cap: 30 })
    );
}

#[tokio::test]
async fn the_per_scope_cap_binds_across_users_and_is_per_app_and_per_community() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000).await;
    fund(&f, f.bob, 1_000).await;
    let c = caps_of(10_000, 25);
    f.store
        .wager(&scope(), f.alice, 10, 0, 50, c, &key())
        .await
        .unwrap();
    f.store
        .wager(&scope(), f.bob, 10, 0, 50, c, &key())
        .await
        .unwrap();
    // 20 staked by this app in this community across two users; 6 more passes 25.
    assert_eq!(
        f.store.wager(&scope(), f.alice, 6, 0, 50, c, &key()).await,
        Err(EconomyError::ScopeQuotaExceeded { cap: 25 })
    );
    assert!(f
        .store
        .wager(&scope(), f.alice, 5, 0, 50, c, &key())
        .await
        .is_ok());
    assert_eq!(
        f.store.wager(&scope(), f.bob, 1, 0, 50, c, &key()).await,
        Err(EconomyError::ScopeQuotaExceeded { cap: 25 })
    );
    // A different APP has its own budget in the same community...
    let other_app = scope_of(TENANT, COMMUNITY, "waddles.core.other-economy-app");
    assert!(f
        .store
        .wager(&other_app, f.alice, 20, 0, 50, c, &key())
        .await
        .is_ok());
    // ...and so does a different COMMUNITY.
    let outsider = Uuid::new_v4();
    exec(
        &f.su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid) VALUES (20, '{outsider}');
             INSERT INTO economy_balances (tenant_id, community_id, user_uuid, balance) \
             VALUES (2, 20, '{outsider}', 500)"
        ),
    )
    .await;
    let other_community = scope_of(OTHER_TENANT, OTHER_COMMUNITY, APP_ID);
    assert!(f
        .store
        .wager(&other_community, outsider, 25, 0, 50, c, &key())
        .await
        .is_ok());
    // The window rolls off.
    exec(
        &f.su,
        "UPDATE economy_ledger SET occurred_at = NOW() - INTERVAL '25 hours'",
    )
    .await;
    assert!(f
        .store
        .wager(&scope(), f.bob, 25, 0, 50, c, &key())
        .await
        .is_ok());
}

#[tokio::test]
async fn the_durable_caps_survive_a_restart_and_a_second_replica() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000).await;
    let c = caps_of(20, 250_000);
    f.store
        .wager(&scope(), f.alice, 20, 0, 50, c, &key())
        .await
        .unwrap();
    // A brand-new pool + store stands in for a restarted process or a second
    // replica: it has no in-memory state at all, yet must still refuse.
    let fresh = PostgresEconomyStore::new(
        connect(
            &ConnectConfig {
                host: f.host.clone(),
                port: f.port,
                name: "waddles_test".to_string(),
                user: "waddles_economy_runtime".to_string(),
            },
            ECONOMY_PASSWORD,
        )
        .await
        .unwrap(),
    );
    assert_eq!(
        fresh.wager(&scope(), f.alice, 1, 0, 50, c, &key()).await,
        Err(EconomyError::UserQuotaExceeded { cap: 20 })
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn concurrent_cross_user_and_cross_replica_wagers_never_overshoot_the_scope_cap() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000).await;
    fund(&f, f.bob, 1_000).await;
    // Two independent pools = two replicas, each with its own connections.
    let mut stores = vec![Arc::new(PostgresEconomyStore::new(f.eco_conn.clone()))];
    stores.push(Arc::new(PostgresEconomyStore::new(
        connect(
            &ConnectConfig {
                host: f.host.clone(),
                port: f.port,
                name: "waddles_test".to_string(),
                user: "waddles_economy_runtime".to_string(),
            },
            ECONOMY_PASSWORD,
        )
        .await
        .unwrap(),
    )));
    let c = caps_of(10_000, 100);
    let mut tasks = Vec::new();
    for i in 0..60 {
        let store = Arc::clone(&stores[i % 2]);
        let user = if i % 3 == 0 { f.alice } else { f.bob };
        tasks.push(tokio::spawn(async move {
            store.wager(&scope(), user, 5, 0, 50, c, &key()).await
        }));
    }
    let (mut ok, mut capped) = (0, 0);
    for t in tasks {
        match t.await.unwrap() {
            Ok(_) => ok += 1,
            Err(EconomyError::ScopeQuotaExceeded { .. }) => capped += 1,
            Err(other) => panic!("unexpected error: {other:?}"),
        }
    }
    assert_eq!(ok, 20, "exactly cap/stake wagers apply, never more");
    assert_eq!(capped, 40);
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT COALESCE(SUM(stake), 0)::BIGINT FROM economy_ledger"
        )
        .await,
        100
    );
}

#[tokio::test]
async fn transfer_caps_are_durable_per_sender_and_per_scope_and_separate_from_wagers() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000).await;
    fund(&f, f.bob, 1_000).await;
    let c = caps_of(30, 45);
    // Wager volume does not eat the transfer budget (different kind).
    f.store
        .wager(
            &scope(),
            f.alice,
            50,
            0,
            50,
            caps_of(10_000, 10_000),
            &key(),
        )
        .await
        .unwrap();
    for _ in 0..3 {
        f.store
            .transfer(&scope(), f.alice, f.bob, 10, 50, c, &key())
            .await
            .unwrap();
    }
    // alice has sent 30: per-SENDER cap is hit...
    assert_eq!(
        f.store
            .transfer(&scope(), f.alice, f.bob, 1, 50, c, &key())
            .await,
        Err(EconomyError::UserQuotaExceeded { cap: 30 })
    );
    // ...bob (a different sender) still has budget but the SCOPE total is 30
    // of 45: 16 would pass it.
    assert_eq!(
        f.store
            .transfer(&scope(), f.bob, f.alice, 16, 50, c, &key())
            .await,
        Err(EconomyError::ScopeQuotaExceeded { cap: 45 })
    );
    assert!(f
        .store
        .transfer(&scope(), f.bob, f.alice, 15, 50, c, &key())
        .await
        .is_ok());
    // Nothing moved on a refusal; only applied transfers wrote ledger rows.
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT COUNT(*) FROM economy_ledger WHERE kind = 'transfer_out'"
        )
        .await,
        4
    );
    // The window rolls off.
    exec(
        &f.su,
        "UPDATE economy_ledger SET occurred_at = NOW() - INTERVAL '25 hours'",
    )
    .await;
    assert!(f
        .store
        .transfer(&scope(), f.alice, f.bob, 30, 50, c, &key())
        .await
        .is_ok());
}

#[tokio::test]
async fn negative_caps_are_invalid_and_zero_caps_refuse_everything() {
    let f = fixture().await;
    fund(&f, f.alice, 100).await;
    assert!(matches!(
        f.store
            .wager(&scope(), f.alice, 1, 0, 50, caps_of(-1, 10), &key())
            .await,
        Err(EconomyError::Invalid(_))
    ));
    assert!(matches!(
        f.store
            .transfer(&scope(), f.alice, f.bob, 1, 50, caps_of(10, -1), &key())
            .await,
        Err(EconomyError::Invalid(_))
    ));
    // Zero is the stage's fail-closed fallback for an unrecognized catalog.
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 1, 0, 50, caps_of(0, 0), &key())
            .await,
        Err(EconomyError::UserQuotaExceeded { cap: 0 })
    );
    assert_eq!(ledger_count(&f).await, 0);
}

// ---- NULL is_active is NOT an active member (review hardening, mirrors #741)

#[tokio::test]
async fn a_null_is_active_member_is_a_non_member_everywhere_fail_closed() {
    let f = fixture().await;
    let ghost = Uuid::new_v4();
    exec(
        &f.su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid, is_active) \
             VALUES (10, '{ghost}', NULL)"
        ),
    )
    .await;
    fund(&f, ghost, 1_000).await;
    fund(&f, f.alice, 1_000).await;
    assert_eq!(
        f.store.balance(&scope(), ghost).await,
        Err(EconomyError::NotAMember)
    );
    assert_eq!(
        f.store.max_bet(&scope(), ghost, 10).await,
        Err(EconomyError::NotAMember)
    );
    assert_eq!(
        f.store
            .wager(&scope(), ghost, 1, 0, 50, caps(), &key())
            .await,
        Err(EconomyError::NotAMember)
    );
    // Either side of a transfer.
    assert_eq!(
        f.store
            .transfer(&scope(), f.alice, ghost, 1, 50, caps(), &key())
            .await,
        Err(EconomyError::NotAMember)
    );
    assert_eq!(
        f.store
            .transfer(&scope(), ghost, f.alice, 1, 50, caps(), &key())
            .await,
        Err(EconomyError::NotAMember)
    );
    // Not on the leaderboard, not in the snapshot the gate reads.
    let board = f.store.leaderboard(&scope(), 10).await.unwrap();
    assert!(board.iter().all(|e| e.user != ghost), "{board:?}");
    let rows = load_membership(&f.eco_conn, Some(TENANT)).await.unwrap();
    assert!(rows.iter().all(|r| r.user != ghost), "{rows:?}");
    assert_eq!(ledger_count(&f).await, 0);
}

// ---- column-level grants: INSERT can never mint --------------------------

#[tokio::test]
async fn the_runtime_role_cannot_mint_through_insert_or_re_key_a_balance_row() {
    let f = fixture().await;
    fund(&f, f.alice, 100).await;
    let carol = Uuid::new_v4();
    // INSERT may name only the identity columns: a row can only start at 0.
    assert!(f
        .eco_conn
        .execute_unprepared(&format!(
            "INSERT INTO economy_balances (tenant_id, community_id, user_uuid, balance) \
             VALUES (1, 10, '{carol}', 999999)"
        ))
        .await
        .is_err());
    assert!(f
        .eco_conn
        .execute_unprepared(&format!(
            "INSERT INTO economy_balances (tenant_id, community_id, user_uuid) \
             VALUES (1, 10, '{carol}')"
        ))
        .await
        .is_ok());
    assert_eq!(
        scalar_i64(
            &f.su,
            &format!("SELECT balance FROM economy_balances WHERE user_uuid = '{carol}'")
        )
        .await,
        0
    );
    // UPDATE may touch only balance/updated_at: no re-keying a row to another
    // tenant/community/user.
    for sql in [
        "UPDATE economy_balances SET tenant_id = 2",
        "UPDATE economy_balances SET community_id = 20",
        "UPDATE economy_balances SET user_uuid = gen_random_uuid()",
    ] {
        assert!(
            f.eco_conn.execute_unprepared(sql).await.is_err(),
            "should be denied: {sql}"
        );
    }
    // Whole-table privileges are NOT held (only the column-level ones).
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT has_table_privilege('waddles_economy_runtime', 'economy_balances', 'INSERT')::int::bigint"
        )
        .await,
        0
    );
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT has_table_privilege('waddles_economy_runtime', 'economy_balances', 'UPDATE')::int::bigint"
        )
        .await,
        0
    );
}

// ---- #751 money-safety regressions: idempotency (double-spend) ------------

fn key_of(raw: &str) -> IdempotencyKey {
    IdempotencyKey::new(raw).expect("a valid key")
}

async fn ledger_rows_for_key(f: &Fixture, key: &str) -> i64 {
    scalar_i64(
        &f.su,
        &format!("SELECT COUNT(*) FROM economy_ledger WHERE idempotency_key = '{key}'"),
    )
    .await
}

/// Regression (#751 review, double-spend): a retried or replayed wager must
/// credit ONCE. The replay answers with the ORIGINAL result and moves nothing.
#[tokio::test]
async fn a_replayed_wager_credits_once_and_returns_the_original_balance() {
    let f = fixture().await;
    fund(&f, f.alice, 100).await;
    let k = key_of("evt-1:wager:0");
    // win: stake 10, payout 25 -> 115
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 10, 25, 50, caps(), &k)
            .await,
        Ok(115)
    );
    // The retry (same key, same parameters) is a replay, not a second credit.
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 10, 25, 50, caps(), &k)
            .await,
        Ok(115)
    );
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 10, 25, 50, caps(), &k)
            .await,
        Ok(115)
    );
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(115));
    assert_eq!(ledger_count(&f).await, 1, "one movement, one ledger row");
    assert_eq!(ledger_rows_for_key(&f, k.as_str()).await, 1);

    // A later, different wager moves the balance on; replaying the first
    // STILL answers with the balance it originally produced, not the current.
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 40, 0, 50, caps(), &key())
            .await,
        Ok(75)
    );
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 10, 25, 50, caps(), &k)
            .await,
        Ok(115)
    );
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(75));
    assert_eq!(ledger_count(&f).await, 2);
}

#[tokio::test]
async fn a_replayed_transfer_moves_money_once() {
    let f = fixture().await;
    fund(&f, f.alice, 100).await;
    let k = key_of("evt-2:transfer:0");
    for _ in 0..4 {
        assert_eq!(
            f.store
                .transfer(&scope(), f.alice, f.bob, 30, 100, caps(), &k)
                .await,
            Ok(())
        );
    }
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(70));
    assert_eq!(f.store.balance(&scope(), f.bob).await, Ok(30));
    // out + in, once.
    assert_eq!(ledger_count(&f).await, 2);
    assert_eq!(total_balance(&f).await, 100, "money is conserved");
}

/// The same key with DIFFERENT parameters is never applied: a replayed bundle
/// that re-rolls its payout (or swaps the recipient) cannot credit twice.
#[tokio::test]
async fn a_key_reused_with_different_parameters_is_a_conflict_and_moves_nothing() {
    let f = fixture().await;
    fund(&f, f.alice, 100).await;
    fund(&f, f.bob, 100).await;
    let w = key_of("evt-3:wager:0");
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 10, 25, 50, caps(), &w)
            .await,
        Ok(115)
    );
    for (user, stake, payout) in [
        (f.alice, 10, 99), // re-rolled payout
        (f.alice, 11, 25), // different stake
        (f.bob, 10, 25),   // different player
    ] {
        assert_eq!(
            f.store
                .wager(&scope(), user, stake, payout, 50, caps(), &w)
                .await,
            Err(EconomyError::IdempotencyConflict),
            "{user} {stake} {payout}"
        );
    }
    let t = key_of("evt-3:transfer:0");
    f.store
        .transfer(&scope(), f.alice, f.bob, 5, 100, caps(), &t)
        .await
        .unwrap();
    for (from, to, amount) in [
        (f.alice, f.bob, 6), // different amount
        (f.bob, f.alice, 5), // reversed direction
    ] {
        assert_eq!(
            f.store
                .transfer(&scope(), from, to, amount, 100, caps(), &t)
                .await,
            Err(EconomyError::IdempotencyConflict)
        );
    }
    // A key never crosses kinds: a wager key cannot front a transfer, nor a
    // transfer key a wager.
    assert_eq!(
        f.store
            .transfer(&scope(), f.alice, f.bob, 1, 100, caps(), &w)
            .await,
        Err(EconomyError::IdempotencyConflict)
    );
    assert_eq!(
        f.store.wager(&scope(), f.alice, 1, 0, 50, caps(), &t).await,
        Err(EconomyError::IdempotencyConflict)
    );
    // Only the two original movements exist (wager 1 row, transfer 2 rows).
    assert_eq!(ledger_count(&f).await, 3);
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(110));
    assert_eq!(f.store.balance(&scope(), f.bob).await, Ok(105));
}

/// A refused call writes no ledger row, so it records no key: once the cause
/// is gone, a retry under the same key applies (and then dedupes).
#[tokio::test]
async fn a_refused_call_records_no_key_so_a_later_retry_applies_once() {
    let f = fixture().await;
    let k = key_of("evt-4:wager:0");
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 10, 25, 50, caps(), &k)
            .await,
        Err(EconomyError::InsufficientFunds { balance: 0 })
    );
    assert_eq!(ledger_count(&f).await, 0);
    fund(&f, f.alice, 100).await;
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 10, 25, 50, caps(), &k)
            .await,
        Ok(115)
    );
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 10, 25, 50, caps(), &k)
            .await,
        Ok(115)
    );
    assert_eq!(ledger_count(&f).await, 1);
}

/// A replay is answered BEFORE any budget check: an op that already applied
/// is not refused because its own spend used the budget up.
#[tokio::test]
async fn a_replay_is_not_refused_by_the_budget_its_own_first_run_used_up() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000).await;
    let c = caps_of(20, 250_000);
    let k = key_of("evt-5:wager:0");
    f.store
        .wager(&scope(), f.alice, 20, 0, 50, c, &k)
        .await
        .unwrap();
    // The budget is spent: a NEW call is over cap, the replay is not.
    assert_eq!(
        f.store.wager(&scope(), f.alice, 1, 0, 50, c, &key()).await,
        Err(EconomyError::UserQuotaExceeded { cap: 20 })
    );
    assert!(f
        .store
        .wager(&scope(), f.alice, 20, 0, 50, c, &k)
        .await
        .is_ok());
    assert_eq!(ledger_count(&f).await, 1);
}

/// Keys are scoped to (tenant, community, app): the same event handled by two
/// different bundles (or the same key text elsewhere) is two independent ops.
#[tokio::test]
async fn keys_are_scoped_per_app() {
    let f = fixture().await;
    fund(&f, f.alice, 100).await;
    let k = key_of("evt-6:wager:0");
    let other_app = scope_of(TENANT, COMMUNITY, "waddles.core.other-game");
    assert_eq!(
        f.store
            .wager(&scope(), f.alice, 10, 0, 50, caps(), &k)
            .await,
        Ok(90)
    );
    assert_eq!(
        f.store
            .wager(&other_app, f.alice, 10, 0, 50, caps(), &k)
            .await,
        Ok(80),
        "another app's call under the same key text is its own operation"
    );
    assert_eq!(ledger_count(&f).await, 2);
}

/// N concurrent identical calls (a flood of retries) credit exactly once.
#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn concurrent_retries_of_one_wager_credit_exactly_once() {
    let f = fixture().await;
    fund(&f, f.alice, 100).await;
    let store = Arc::new(PostgresEconomyStore::new(f.eco_conn.clone()));
    let k = Arc::new(key_of("evt-7:wager:0"));
    let mut tasks = Vec::new();
    for _ in 0..30 {
        let (store, k, user) = (Arc::clone(&store), Arc::clone(&k), f.alice);
        tasks.push(tokio::spawn(async move {
            store.wager(&scope(), user, 10, 25, 50, caps(), &k).await
        }));
    }
    for t in tasks {
        assert_eq!(t.await.unwrap(), Ok(115), "every retry sees the one result");
    }
    assert_eq!(f.store.balance(&scope(), f.alice).await, Ok(115));
    assert_eq!(ledger_count(&f).await, 1);
}

/// A wager and a transfer racing on ONE key (different advisory locks) can
/// never both apply: whichever loses sees a conflict, never a second movement.
#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn a_wager_and_a_transfer_racing_on_one_key_apply_at_most_one() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000).await;
    let store = Arc::new(PostgresEconomyStore::new(f.eco_conn.clone()));
    for round in 0..20 {
        let k = Arc::new(key_of(&format!("evt-8-{round}:race:0")));
        let (s1, k1, a) = (Arc::clone(&store), Arc::clone(&k), f.alice);
        let (s2, k2, b, a2) = (Arc::clone(&store), Arc::clone(&k), f.bob, f.alice);
        let w = tokio::spawn(async move {
            s1.wager(&scope(), a, 10, 0, 50, caps(), &k1)
                .await
                .map(|_| ())
        });
        let t =
            tokio::spawn(async move { s2.transfer(&scope(), a2, b, 10, 100, caps(), &k2).await });
        let (w, t) = (w.await.unwrap(), t.await.unwrap());
        let applied = [w.is_ok(), t.is_ok()].iter().filter(|ok| **ok).count();
        assert_eq!(applied, 1, "round {round}: exactly one of {w:?} / {t:?}");
        let loser = if w.is_err() { w } else { t };
        assert_eq!(
            loser,
            Err(EconomyError::IdempotencyConflict),
            "round {round}"
        );
        assert_eq!(
            scalar_i64(
                &f.su,
                &format!(
                    "SELECT COUNT(*) FROM economy_ledger WHERE idempotency_key = 'evt-8-{round}:race:0'"
                )
            )
            .await,
            1
        );
    }
}

/// The database's own backstop, independent of the store: a keyed ledger row
/// can be claimed once per (tenant, community, app); unkeyed rows never collide.
#[tokio::test]
async fn the_database_refuses_a_second_ledger_row_for_one_key() {
    let f = fixture().await;
    let insert = |app: &str, key: &str| {
        format!(
            "INSERT INTO economy_ledger (tenant_id, community_id, app_id, user_uuid, kind, delta, \
             stake, payout, balance_after, idempotency_key) \
             VALUES (1, 10, '{app}', '{}', 'wager', 0, 1, 1, 0, {key})",
            f.alice
        )
    };
    exec(&f.su, &insert("a", "'k1'")).await;
    assert!(f.su.execute_unprepared(&insert("a", "'k1'")).await.is_err());
    exec(&f.su, &insert("b", "'k1'")).await;
    exec(&f.su, &insert("a", "NULL")).await;
    exec(&f.su, &insert("a", "NULL")).await;
    // The shape CHECK keeps keys to the host alphabet.
    assert!(f
        .su
        .execute_unprepared(&insert("a", "'has space'"))
        .await
        .is_err());
    // The runtime role can append keyed rows but never rewrite a key.
    assert!(f
        .eco_conn
        .execute_unprepared("UPDATE economy_ledger SET idempotency_key = NULL")
        .await
        .is_err());
}

// ---- #751 money-safety regressions: the mint cap is on the PAYOUT ---------

/// Regression (#751 review, unbounded mint): the daily cap that bounds how
/// much a wager can MINT used to sum the STAKE, so a stake-1 wager paying 100
/// only ever spent 1 of the budget. The mint budget is the PAYOUT.
#[tokio::test]
async fn the_per_user_mint_cap_is_enforced_on_the_payout_not_the_stake() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000).await;
    // Plenty of STAKE headroom (10_000), but only 100 of daily MINT budget.
    let c = caps_of(100, 1_000_000);
    // stake 1 -> payout 60, twice would be stake 2 << 100 under the old
    // stake-metered cap, but payout 120 > 100.
    f.store
        .wager(&scope(), f.alice, 1, 60, 50, c, &key())
        .await
        .unwrap();
    assert_eq!(
        f.store.wager(&scope(), f.alice, 1, 60, 50, c, &key()).await,
        Err(EconomyError::UserQuotaExceeded { cap: 100 }),
        "60 + 60 minted would pass the 100 mint cap"
    );
    // Exactly the remaining budget fits.
    assert!(f
        .store
        .wager(&scope(), f.alice, 1, 40, 50, c, &key())
        .await
        .is_ok());
    // The mint budget is spent: even a payout of 1 is refused ...
    assert_eq!(
        f.store.wager(&scope(), f.alice, 1, 1, 50, c, &key()).await,
        Err(EconomyError::UserQuotaExceeded { cap: 100 })
    );
    // ... but a LOSING wager mints nothing and is not a mint.
    assert!(f
        .store
        .wager(&scope(), f.alice, 5, 0, 50, c, &key())
        .await
        .is_ok());
    // Refused calls wrote no ledger row and consumed no budget: two applied
    // paying wagers plus the loss.
    assert_eq!(ledger_count(&f).await, 3);
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT SUM(payout)::BIGINT FROM economy_ledger WHERE kind = 'wager'"
        )
        .await,
        100,
        "total minted never passed the cap"
    );
}

#[tokio::test]
async fn a_large_losing_stake_spends_the_stake_budget_not_the_mint_budget() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000).await;
    // Stake budget 60, mint budget 60 (the same ceilings, separate windows).
    let c = caps_of(60, 1_000_000);
    f.store
        .wager(&scope(), f.alice, 50, 0, 50, c, &key())
        .await
        .unwrap();
    // 50 staked: a stake of 11 is over the STAKE window ...
    assert_eq!(
        f.store.wager(&scope(), f.alice, 11, 0, 50, c, &key()).await,
        Err(EconomyError::UserQuotaExceeded { cap: 60 })
    );
    // ... while the MINT window is untouched by that loss: a stake-10 wager
    // that pays 60 (the whole mint budget) still fits both windows.
    assert!(f
        .store
        .wager(&scope(), f.alice, 10, 60, 50, c, &key())
        .await
        .is_ok());
}

#[tokio::test]
async fn the_per_scope_mint_cap_binds_across_users_on_the_payout() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000).await;
    fund(&f, f.bob, 1_000).await;
    // 30 of community-wide mint budget; each user alone has plenty.
    let c = caps_of(10_000, 30);
    f.store
        .wager(&scope(), f.alice, 1, 20, 50, c, &key())
        .await
        .unwrap();
    assert_eq!(
        f.store.wager(&scope(), f.bob, 1, 11, 50, c, &key()).await,
        Err(EconomyError::ScopeQuotaExceeded { cap: 30 }),
        "20 + 11 minted by this app in this community passes the 30 cap"
    );
    assert!(f
        .store
        .wager(&scope(), f.bob, 1, 10, 50, c, &key())
        .await
        .is_ok());
    // Another app in the same community has its own mint budget.
    let other_app = scope_of(TENANT, COMMUNITY, "waddles.core.other-game");
    assert!(f
        .store
        .wager(&other_app, f.bob, 1, 30, 50, c, &key())
        .await
        .is_ok());
}

/// Mint is a ledger fact: it survives a restart and a second replica (the
/// durable window is the ledger, not process memory).
#[tokio::test]
async fn the_mint_cap_is_durable_across_a_fresh_store() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000).await;
    let c = caps_of(25, 250_000);
    f.store
        .wager(&scope(), f.alice, 1, 25, 50, c, &key())
        .await
        .unwrap();
    let fresh = PostgresEconomyStore::new(
        connect(
            &ConnectConfig {
                host: f.host.clone(),
                port: f.port,
                name: "waddles_test".to_string(),
                user: "waddles_economy_runtime".to_string(),
            },
            ECONOMY_PASSWORD,
        )
        .await
        .unwrap(),
    );
    assert_eq!(
        fresh.wager(&scope(), f.alice, 1, 1, 50, c, &key()).await,
        Err(EconomyError::UserQuotaExceeded { cap: 25 })
    );
}

#[tokio::test]
async fn a_payout_window_rolls_off_after_24_hours() {
    let f = fixture().await;
    fund(&f, f.alice, 1_000).await;
    let c = caps_of(40, 250_000);
    f.store
        .wager(&scope(), f.alice, 1, 40, 50, c, &key())
        .await
        .unwrap();
    assert!(f
        .store
        .wager(&scope(), f.alice, 1, 1, 50, c, &key())
        .await
        .is_err());
    exec(
        &f.su,
        "UPDATE economy_ledger SET occurred_at = NOW() - INTERVAL '25 hours'",
    )
    .await;
    assert!(f
        .store
        .wager(&scope(), f.alice, 1, 40, 50, c, &key())
        .await
        .is_ok());
}
