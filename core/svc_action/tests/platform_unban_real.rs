//! Real-path proof of the Discord-native unban sync on the action side: a signed
//! `GUILD_BAN_REMOVE` event on a REAL Valkey list, consumed by the REAL `run_consumer`, lifts the
//! floor ban in a REAL Postgres running the exact shipped SQL (the files alembic 0050/0071/0097/
//! 0114/0137/0138 execute), through the real `PgFloorBanStore` connected as the least-privilege
//! `waddles_svc_action` role (so the EXECUTE grant is exercised, not bypassed).
//!
//! Asserts: the community-scope ban goes to status 8 with the fixed gateway identity recorded and
//! lift origin = platform; NO outbox unban row is created (no echo); a tenant-scope ban is NOT
//! lifted; an event for a community outside the tenant lifts nothing; a forged entry (wrong key)
//! and a replay change nothing.
//!
//! Requires Docker (the `docker` CLI, `core/test_support`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use sea_orm::{ConnectionTrait, Database, DatabaseConnection, Statement};
use svc_action::floor_ban_dispatch::PgFloorBanStore;
use svc_action::pin_sig::{PinFields, PinKeys};
use svc_action::platform_unban::{
    run_consumer, PlatformUnban, ValkeySeen, EVENTS_LIST, MAC_NO_ORIGIN, MAC_OP, MAC_PLATFORM,
    SOURCE_APP,
};
use uuid::Uuid;
#[allow(dead_code)]
#[path = "../../test_support/tests/docker_container.rs"]
mod docker_container;
use docker_container::{start_container, DockerContainer};

const SU_PW: &str = "postgres_test_superuser_pw";
const ACTION_PW: &str = "waddles_svc_action_test_pw";
const REP_PW: &str = "waddles_bundle_reputation_test_pw";
const VALKEY_IMAGE: &str = "valkey/valkey:8.1.10-alpine";
const GUILD: &str = "111111111111111111";
const STORE_DDL: &str = include_str!("../../../scripts/db/bundle_reputation_store.sql");
const HOST_OPS_DDL: &str = include_str!("../../../scripts/db/bundle_reputation_host_ops.sql");
const CONTRACT_DDL: &str = include_str!("../../../scripts/db/bundle_reputation_unify.sql");
const FLOOR_BAN_DDL: &str = include_str!("../../../scripts/db/bundle_reputation_floor_ban.sql");
const LIFT_DDL: &str = include_str!("../../../scripts/db/bundle_reputation_floor_ban_lift.sql");
const SYNC_DDL: &str =
    include_str!("../../../scripts/db/bundle_reputation_floor_ban_platform_sync.sql");

fn keys() -> PinKeys {
    PinKeys::parse("k1:00112233445566778899aabbccddeeff", Some("k1")).unwrap()
}

async fn exec(c: &DatabaseConnection, sql: &str) {
    c.execute_unprepared(sql)
        .await
        .unwrap_or_else(|e| panic!("sql failed: {e}\n{sql}"));
}

async fn scalar(c: &DatabaseConnection, sql: &str) -> i64 {
    c.query_one_raw(Statement::from_string(sea_orm::DbBackend::Postgres, sql))
        .await
        .unwrap()
        .unwrap()
        .try_get_by_index::<i64>(0)
        .unwrap()
}

async fn text(c: &DatabaseConnection, sql: &str) -> String {
    c.query_one_raw(Statement::from_string(sea_orm::DbBackend::Postgres, sql))
        .await
        .unwrap()
        .unwrap()
        .try_get_by_index::<String>(0)
        .unwrap()
}

async fn postgres() -> DockerContainer {
    start_container(
        "postgres:17.6-bookworm",
        5432,
        &[
            ("POSTGRES_PASSWORD", SU_PW),
            ("POSTGRES_DB", "waddles_test"),
        ],
        &["pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
        Duration::from_secs(240),
    )
    .await
}

/// Builds the schema from the shipped SQL; returns (superuser conn, `waddles_svc_action` conn).
async fn database(c: &DockerContainer) -> (DatabaseConnection, DatabaseConnection) {
    let (host, port) = (c.host(), c.port());
    let su = Database::connect(format!(
        "postgres://postgres:{SU_PW}@{host}:{port}/waddles_test"
    ))
    .await
    .unwrap();
    exec(
        &su,
        "CREATE TABLE tenants (id SERIAL PRIMARY KEY, slug TEXT);
         CREATE TABLE communities (id SERIAL PRIMARY KEY, tenant_id INTEGER REFERENCES tenants(id),
             is_active BOOLEAN DEFAULT true, deleted_at TIMESTAMP);
         CREATE TABLE community_members (
             id SERIAL PRIMARY KEY,
             community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
             user_id VARCHAR(255), is_active BOOLEAN DEFAULT true,
             left_at TIMESTAMP, removed_at TIMESTAMP);
         CREATE TABLE hub_users (id SERIAL PRIMARY KEY);
         CREATE TABLE app_catalog (app_id VARCHAR(255) PRIMARY KEY);
         CREATE TABLE bundle_reputation_adjustments (
             id BIGSERIAL PRIMARY KEY,
             app_id VARCHAR(255) REFERENCES app_catalog(app_id),
             tenant_id INTEGER NOT NULL REFERENCES tenants(id),
             community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
             target_user_uuid VARCHAR(36) NOT NULL,
             scope VARCHAR(20) NOT NULL CHECK (scope IN ('community', 'tenant')),
             delta INTEGER NOT NULL, reason_code VARCHAR(100) NOT NULL,
             occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
             reversal_of BIGINT REFERENCES bundle_reputation_adjustments(id));
         CREATE TABLE bundle_tenant_availability (tenant_id INTEGER, app_id TEXT, available BOOLEAN);
         CREATE TABLE app_global_installs (app_id TEXT, version TEXT, superseded_by BIGINT, revoked_at TIMESTAMPTZ);
         CREATE TABLE app_version_uploads (app_id TEXT, version TEXT, manifest_json JSONB);
         INSERT INTO tenants (id, slug) VALUES (1, 't1'), (2, 't2');
         INSERT INTO communities (id, tenant_id) VALUES (10, 1), (20, 2);",
    )
    .await;
    exec(
        &su,
        &format!(
            "CREATE ROLE waddles_bundle_reputation LOGIN NOSUPERUSER PASSWORD '{REP_PW}';
             CREATE ROLE waddles_svc_action LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                 PASSWORD '{ACTION_PW}'"
        ),
    )
    .await;
    for ddl in [
        STORE_DDL,
        HOST_OPS_DDL,
        CONTRACT_DDL,
        FLOOR_BAN_DDL,
        LIFT_DDL,
        SYNC_DDL,
    ] {
        exec(&su, ddl).await;
    }
    let action = Database::connect(format!(
        "postgres://waddles_svc_action:{ACTION_PW}@{host}:{port}/waddles_test"
    ))
    .await
    .unwrap();
    (su, action)
}

/// Inserts a ban row in force (status 2 = delivered) and returns its user.
async fn ban(su: &DatabaseConnection, tenant: i32, community: i32, scope: i32) -> Uuid {
    let user = Uuid::new_v4();
    exec(
        su,
        &format!(
            "INSERT INTO bundle_reputation_floor_bans
                 (tenant_id, community_id, origin_community_id, user_uuid, scope, idempotency_key,
                  reason, platform, status, completed_at)
             VALUES ({tenant}, {community}, {community}, '{user}', {scope}, 'evt-{user}',
                     'community_hard_floor', 'discord', 2, NOW())"
        ),
    )
    .await;
    user
}

/// The wire event exactly as svc-ingest signs it.
fn event(k: &PinKeys, tenant: i64, community: i64, user: Uuid) -> String {
    let (event_id, user) = (Uuid::new_v4().to_string(), user.to_string());
    let exp = u64::try_from(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis(),
    )
    .unwrap()
        + 60_000;
    let f = PinFields {
        platform: MAC_PLATFORM,
        op: MAC_OP,
        channel: GUILD,
        origin_channel: MAC_NO_ORIGIN,
        message_id: "",
        text: "",
        user_id: &user,
        op_id: &event_id,
        exp_ms: exp,
        community_id: community,
        tenant_id: tenant,
    };
    let sig = k.sign(&f).unwrap();
    serde_json::json!({"v":1,"event_id":event_id,"tenant_id":tenant,"community_id":community,
        "platform":"discord","guild_id":GUILD,"user_uuid":user,"exp_ms":exp,"sig":sig})
    .to_string()
}

async fn status(su: &DatabaseConnection, user: Uuid, scope: i32) -> i64 {
    scalar(
        su,
        &format!(
            "SELECT status::bigint FROM bundle_reputation_floor_bans
             WHERE user_uuid = '{user}' AND scope = {scope} AND action = 1"
        ),
    )
    .await
}

async fn wait_for_status(su: &DatabaseConnection, user: Uuid, scope: i32, want: i64) {
    for _ in 0..80 {
        if status(su, user, scope).await == want {
            return;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    panic!("ban never reached status {want}");
}

#[tokio::test]
async fn a_discord_native_unban_lifts_the_floor_ban_without_echo() {
    let pg = postgres().await;
    let valkey = start_container(
        VALKEY_IMAGE,
        6379,
        &[],
        &["valkey-cli", "ping"],
        Duration::from_secs(120),
    )
    .await;
    let (su, action) = database(&pg).await;
    let url = format!("redis://{}:{}/", valkey.host(), valkey.port());
    let client = redis::Client::open(url).unwrap();
    let push = client.get_connection_manager().await.unwrap();
    let handler = Arc::new(PlatformUnban::new(
        Arc::new(PgFloorBanStore::new(action)),
        keys(),
        Arc::new(ValkeySeen::new(
            client.get_connection_manager().await.unwrap(),
        )),
        svc_action::stage_metrics::stage_metrics(),
    ));
    let (stop_tx, stop_rx) = tokio::sync::oneshot::channel::<()>();
    let consumer = tokio::spawn(run_consumer(
        handler,
        client.get_connection_manager().await.unwrap(),
        async move {
            let _ = stop_rx.await;
        },
    ));
    let lpush = |raw: String| {
        let mut c = push.clone();
        async move {
            redis::cmd("LPUSH")
                .arg(EVENTS_LIST)
                .arg(raw)
                .query_async::<i64>(&mut c)
                .await
                .unwrap();
        }
    };

    // Community-scope ban -> lifted (status 8), source recorded, platform origin, NO unban row.
    let banned = ban(&su, 1, 10, 1).await;
    lpush(event(&keys(), 1, 10, banned)).await;
    wait_for_status(&su, banned, 1, 8).await;
    assert_eq!(
        text(
            &su,
            &format!(
                "SELECT lift_source_app FROM bundle_reputation_floor_bans
                 WHERE user_uuid = '{banned}' AND action = 1"
            )
        )
        .await,
        SOURCE_APP
    );
    assert_eq!(
        scalar(
            &su,
            &format!(
                "SELECT lift_origin::bigint FROM bundle_reputation_floor_bans
                 WHERE user_uuid = '{banned}' AND action = 1"
            )
        )
        .await,
        2,
        "lift_origin must be platform"
    );
    assert_eq!(
        scalar(
            &su,
            "SELECT count(*) FROM bundle_reputation_floor_bans WHERE action = 2"
        )
        .await,
        0,
        "a platform-originated lift must not enqueue an unban (echo)"
    );

    // Tenant-scope ban: a platform unban never lifts it (scope-1 only).
    let tenant_banned = ban(&su, 1, 10, 2).await;
    // Forged: right shape, signed by a key the writer does not hold.
    let wrong = PinKeys::parse("k1:ffeeddccbbaa99887766554433221100", Some("k1")).unwrap();
    let forged_target = ban(&su, 1, 10, 1).await;
    // Community 20 belongs to tenant 2: an event naming tenant 1 + community 20 lifts nothing.
    let cross = ban(&su, 2, 20, 1).await;
    lpush(event(&keys(), 1, 10, tenant_banned)).await;
    lpush(event(&wrong, 1, 10, forged_target)).await;
    lpush(event(&keys(), 1, 20, cross)).await;
    // A final valid event proves the consumer drained everything before it (FIFO list).
    let marker = ban(&su, 1, 10, 1).await;
    lpush(event(&keys(), 1, 10, marker)).await;
    wait_for_status(&su, marker, 1, 8).await;
    assert_eq!(
        status(&su, tenant_banned, 2).await,
        2,
        "tenant-scope ban lifted"
    );
    assert_eq!(
        status(&su, forged_target, 1).await,
        2,
        "forged event lifted a ban"
    );
    assert_eq!(
        status(&su, cross, 1).await,
        2,
        "cross-tenant event lifted a ban"
    );

    // Replay of the first (valid, single-use) event: re-ban the same user and replay -> stays.
    let replay = event(&keys(), 1, 10, forged_target);
    lpush(replay.clone()).await;
    wait_for_status(&su, forged_target, 1, 8).await;
    exec(
        &su,
        &format!(
            "UPDATE bundle_reputation_floor_bans SET status = 2, lifted_at = NULL
             WHERE user_uuid = '{forged_target}' AND action = 1"
        ),
    )
    .await;
    lpush(replay).await;
    lpush(event(&keys(), 1, 10, Uuid::new_v4())).await;
    tokio::time::sleep(Duration::from_millis(1500)).await;
    assert_eq!(
        status(&su, forged_target, 1).await,
        2,
        "replay lifted again"
    );

    let _ = stop_tx.send(());
    consumer.await.unwrap();
}
