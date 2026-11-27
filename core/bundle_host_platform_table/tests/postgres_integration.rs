//! Real-Postgres integration tests for the platform-table store: scope injection
//! (own community only; cross-community and cross-tenant indistinguishable from
//! absent), allowlist/value denial, protected roles, per-write audit, and the
//! `waddles_platform_table` role's privilege boundary. The schema under test is
//! the EXACT shipped DDL (`scripts/db/bundle_platform_table.sql`, also run by
//! alembic 0079) on minimal prerequisites. Requires Docker.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::time::Duration;

use bundle_host_platform_table::{
    connect, Change, ConnectConfig, PlatformTableError, PlatformTableScope, PlatformTableStore,
    PostgresPlatformTableStore,
};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection, Statement};
use uuid::Uuid;
#[allow(dead_code)]
#[path = "../../test_support/tests/docker_container.rs"]
mod docker_container;
use docker_container::{start_container_blocking, DockerContainer};

const IMAGE: &str = "postgres:17.6-bookworm";
const SU_PW: &str = "postgres_test_superuser_pw";
const ROLE_PW: &str = "waddles_platform_table_test_pw";
const APP_ID: &str = "penguintechinc.waddles.fixture-test-role-sync";
const DDL: &str = include_str!("../../../scripts/db/bundle_platform_table.sql");
const LINK_CONTRACT: &str = include_str!("../../../scripts/db/community_role_link_contract.sql");

const TENANT: i32 = 1;
const COMMUNITY: i32 = 10;
/// Same tenant as `COMMUNITY`, but it has NO system `moderator` row -- only a custom one.
const CUSTOM_ONLY_COMMUNITY: i32 = 11;
const OTHER_TENANT: i32 = 2;
const OTHER_COMMUNITY: i32 = 20;

struct Fixture {
    _container: DockerContainer,
    su: DatabaseConnection,
    store: PostgresPlatformTableStore,
    cfg: ConnectConfig,
    member: Uuid,
    other_member: Uuid,
    admin: Uuid,
    owner: Uuid,
    custom_only_member: Uuid,
}

static IMAGE_PULL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

async fn start() -> DockerContainer {
    let _guard = IMAGE_PULL.lock().await;
    for _ in 0..3 {
        let r = tokio::task::spawn_blocking(|| {
            start_container_blocking(
                IMAGE,
                5432,
                &[
                    ("POSTGRES_PASSWORD", SU_PW),
                    ("POSTGRES_DB", "waddles_test"),
                ],
                &["pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                Duration::from_secs(240),
            )
        })
        .await
        .unwrap();
        if let Ok(c) = r {
            return c;
        }
    }
    panic!("postgres test container failed to start");
}

async fn exec(conn: &DatabaseConnection, sql: &str) {
    conn.execute_unprepared(sql)
        .await
        .unwrap_or_else(|e| panic!("sql failed: {e}\n{sql}"));
}

async fn text(conn: &DatabaseConnection, sql: &str) -> String {
    conn.query_one_raw(Statement::from_string(sea_orm::DbBackend::Postgres, sql))
        .await
        .unwrap()
        .unwrap()
        .try_get_by_index::<String>(0)
        .unwrap()
}

/// The member's linked role name (the post-drop source of truth), `NULL` rendered as `<none>`.
async fn linked_role(conn: &DatabaseConnection, user: Uuid) -> String {
    text(
        conn,
        &format!(
            "SELECT COALESCE(cr.name, '<none>') FROM community_members cm \
             LEFT JOIN community_roles cr ON cr.id = cm.community_role_id \
             WHERE cm.user_uuid = '{user}'"
        ),
    )
    .await
}

async fn fixture() -> Fixture {
    let container = start().await;
    let (host, port) = (container.host().to_string(), container.port());
    let su = Database::connect(format!(
        "postgres://postgres:{SU_PW}@{host}:{port}/waddles_test"
    ))
    .await
    .unwrap();
    let (member, other_member, admin, owner, custom_only_member) = (
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
    );
    exec(
        &su,
        "CREATE TABLE tenants (id SERIAL PRIMARY KEY);
         CREATE TABLE communities (id SERIAL PRIMARY KEY, tenant_id INTEGER REFERENCES tenants(id));
         CREATE TABLE community_members (
             id SERIAL PRIMARY KEY,
             community_id INTEGER REFERENCES communities(id),
             user_uuid UUID,
             user_id VARCHAR(255),
             platform VARCHAR(50),
             platform_user_id VARCHAR(255),
             user_uuid_unavailable_reason TEXT,
             display_name VARCHAR(255),
             role VARCHAR(50) DEFAULT 'member',
             community_role_id INTEGER,
             claims_cache TEXT,
             is_active BOOLEAN DEFAULT true,
             joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
             updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
             left_at TIMESTAMP,
             removed_at TIMESTAMP);
         CREATE TABLE community_roles (
             id SERIAL PRIMARY KEY,
             community_id INTEGER NOT NULL REFERENCES communities(id),
             name VARCHAR(100) NOT NULL,
             is_system BOOLEAN NOT NULL DEFAULT false);
         CREATE TABLE hub_users (id SERIAL PRIMARY KEY, uuid UUID DEFAULT gen_random_uuid(), email TEXT);
         INSERT INTO tenants (id) VALUES (1), (2);
         INSERT INTO communities (id, tenant_id) VALUES (10, 1), (11, 1), (20, 2);
         INSERT INTO community_roles (community_id, name, is_system) VALUES
             (10, 'member', true), (10, 'moderator', true), (10, 'community-admin', true),
             (10, 'community-owner', true), (10, 'vip', false),
             (11, 'member', true), (11, 'moderator', false),
             (20, 'member', true);
         INSERT INTO hub_users (id, email) VALUES (1, 'x@example.com');",
    )
    .await;
    exec(
        &su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid, display_name, role) VALUES
             (10, '{member}', 'Alice', 'member'),
             (20, '{other_member}', 'Bob', 'member'),
             (10, '{admin}', 'Root', 'community-admin'),
             (10, '{owner}', 'Boss', 'community-owner'),
             (11, '{custom_only_member}', 'Carol', 'member');
             UPDATE community_members cm SET community_role_id = cr.id, claims_cache = 'stale'
               FROM community_roles cr
              WHERE cr.community_id = cm.community_id AND cr.name = cm.role AND cr.is_system;"
        ),
    )
    .await;
    exec(
        &su,
        &format!("CREATE ROLE waddles_platform_table LOGIN PASSWORD '{ROLE_PW}'"),
    )
    .await;
    su.execute_unprepared(DDL).await.unwrap();
    // Post-0147 contract: link-based grants/view, then the legacy string column is gone.
    su.execute_unprepared(LINK_CONTRACT).await.unwrap();
    exec(&su, "ALTER TABLE community_members DROP COLUMN role").await;
    let cfg = ConnectConfig {
        host,
        port,
        name: "waddles_test".into(),
        user: "waddles_platform_table".into(),
    };
    let store = PostgresPlatformTableStore::new(connect(&cfg, ROLE_PW).await.unwrap());
    Fixture {
        _container: container,
        su,
        store,
        cfg,
        member,
        other_member,
        admin,
        owner,
        custom_only_member,
    }
}

fn scope(tenant: i32, community: i32) -> PlatformTableScope {
    PlatformTableScope {
        tenant_id: tenant,
        community_id: community,
        app_id: APP_ID.to_string(),
        app_version: 1,
    }
}

fn role(v: &str) -> Vec<Change> {
    vec![Change {
        column: "role".into(),
        value: v.into(),
    }]
}

const T: &str = "community_members";

#[tokio::test(flavor = "multi_thread")]
async fn approved_write_applies_inside_own_community_and_is_audited() {
    let f = fixture().await;
    f.store
        .write(&scope(TENANT, COMMUNITY), T, f.member, &role("vip"))
        .await
        .unwrap();
    let got = f
        .store
        .read(
            &scope(TENANT, COMMUNITY),
            T,
            f.member,
            &["role".into(), "is_active".into()],
        )
        .await
        .unwrap();
    assert_eq!(got["role"], "vip");
    assert_eq!(got["is_active"], true);
    assert_eq!(
        text(
            &f.su,
            "SELECT old_value || '>' || new_value FROM bundle_platform_table_audit"
        )
        .await,
        "member>vip"
    );
    assert_eq!(
        text(&f.su, "SELECT app_id || '/' || tenant_id || '/' || community_id FROM bundle_platform_table_audit").await,
        format!("{APP_ID}/{TENANT}/{COMMUNITY}")
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn cross_community_and_cross_tenant_are_denied_and_untouched() {
    let f = fixture().await;
    // Member of community 20 / tenant 2, addressed from community 10 / tenant 1.
    let err = f
        .store
        .write(&scope(TENANT, COMMUNITY), T, f.other_member, &role("vip"))
        .await
        .unwrap_err();
    assert_eq!(err, PlatformTableError::NotAMember);
    // Right community id, WRONG tenant: the tenant filter alone must deny it.
    let err = f
        .store
        .write(&scope(OTHER_TENANT, COMMUNITY), T, f.member, &role("vip"))
        .await
        .unwrap_err();
    assert_eq!(err, PlatformTableError::NotAMember);
    let err = f
        .store
        .read(
            &scope(TENANT, OTHER_COMMUNITY),
            T,
            f.other_member,
            &["role".into()],
        )
        .await
        .unwrap_err();
    assert_eq!(err, PlatformTableError::NotAMember);
    assert_eq!(
        linked_role(&f.su, f.other_member).await,
        "member"
    );
    assert_eq!(
        text(
            &f.su,
            "SELECT count(*)::text FROM bundle_platform_table_audit"
        )
        .await,
        "0"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn columns_and_values_outside_the_allowlist_are_denied() {
    let f = fixture().await;
    let s = scope(TENANT, COMMUNITY);
    for col in [
        "display_name",
        "user_uuid",
        "role; DROP TABLE community_members",
    ] {
        let err = f
            .store
            .read(&s, T, f.member, &[col.into()])
            .await
            .unwrap_err();
        assert!(
            matches!(err, PlatformTableError::NotAllowlisted(_)),
            "{col}"
        );
    }
    let err = f
        .store
        .write(
            &s,
            T,
            f.member,
            &[Change {
                column: "is_active".into(),
                value: "false".into(),
            }],
        )
        .await
        .unwrap_err();
    assert!(matches!(err, PlatformTableError::NotAllowlisted(_)));
    for bad in ["community-admin", "community-owner", "root"] {
        let err = f
            .store
            .write(&s, T, f.member, &role(bad))
            .await
            .unwrap_err();
        assert!(matches!(err, PlatformTableError::Invalid(_)), "{bad}");
    }
    let err = f
        .store
        .read(&s, "hub_users", f.member, &["email".into()])
        .await
        .unwrap_err();
    assert!(matches!(err, PlatformTableError::NotAllowlisted(_)));
    assert_eq!(
        linked_role(&f.su, f.member).await,
        "member"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn protected_roles_are_never_modified() {
    let f = fixture().await;
    let err = f
        .store
        .write(&scope(TENANT, COMMUNITY), T, f.admin, &role("member"))
        .await
        .unwrap_err();
    assert_eq!(err, PlatformTableError::NotAMember);
    assert_eq!(
        linked_role(&f.su, f.admin).await,
        "community-admin"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn db_role_cannot_touch_anything_beyond_its_grants() {
    let f = fixture().await;
    let conn = connect(&f.cfg, ROLE_PW).await.unwrap();
    for sql in [
        "UPDATE community_members SET display_name = 'x'",
        "SELECT display_name FROM community_members",
        "SELECT email FROM hub_users",
        "UPDATE community_members SET is_active = false",
        "DELETE FROM community_members",
        "UPDATE bundle_platform_table_audit SET new_value = 'x'",
    ] {
        assert!(
            conn.execute_unprepared(sql).await.is_err(),
            "{sql} must be denied"
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn legacy_role_column_is_gone_and_read_returns_linked_name() {
    let f = fixture().await;
    assert_eq!(
        text(
            &f.su,
            "SELECT count(*)::text FROM information_schema.columns \
             WHERE table_name = 'community_members' AND column_name = 'role'"
        )
        .await,
        "0"
    );
    let got = f
        .store
        .read(&scope(TENANT, COMMUNITY), T, f.owner, &["role".into()])
        .await
        .unwrap();
    assert_eq!(got["role"], "community-owner");
    // Unlinked member -> null.
    exec(
        &f.su,
        &format!(
            "UPDATE community_members SET community_role_id = NULL WHERE user_uuid = '{}'",
            f.member
        ),
    )
    .await;
    let got = f
        .store
        .read(&scope(TENANT, COMMUNITY), T, f.member, &["role".into()])
        .await
        .unwrap();
    assert!(got["role"].is_null());
}

#[tokio::test(flavor = "multi_thread")]
async fn write_relinks_role_id_and_clears_claims_cache() {
    let f = fixture().await;
    let s = scope(TENANT, COMMUNITY);
    for (name, system) in [("vip", false), ("moderator", true), ("member", true)] {
        f.store.write(&s, T, f.member, &role(name)).await.unwrap();
        assert_eq!(linked_role(&f.su, f.member).await, name);
        assert_eq!(
            text(
                &f.su,
                &format!(
                    "SELECT cr.is_system::text || '/' || COALESCE(cm.claims_cache, 'NULL') \
                     FROM community_members cm JOIN community_roles cr \
                       ON cr.id = cm.community_role_id WHERE cm.user_uuid = '{}'",
                    f.member
                )
            )
            .await,
            format!("{system}/NULL")
        );
        exec(
            &f.su,
            &format!(
                "UPDATE community_members SET claims_cache = 'stale' WHERE user_uuid = '{}'",
                f.member
            ),
        )
        .await;
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn unknown_role_name_is_a_loud_error_and_changes_nothing() {
    let f = fixture().await;
    // `vip` has no row in community 20 (only `member`): must not write NULL.
    let err = f
        .store
        .write(
            &scope(OTHER_TENANT, OTHER_COMMUNITY),
            T,
            f.other_member,
            &role("vip"),
        )
        .await
        .unwrap_err();
    assert!(matches!(err, PlatformTableError::Invalid(_)), "{err:?}");
    assert_eq!(linked_role(&f.su, f.other_member).await, "member");
    assert_eq!(
        text(
            &f.su,
            "SELECT count(*)::text FROM bundle_platform_table_audit"
        )
        .await,
        "0"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn custom_role_never_stands_in_for_a_reserved_system_name() {
    let f = fixture().await;
    // Community 11 has only a CUSTOM `moderator`; the reserved name needs the system row.
    let err = f
        .store
        .write(
            &scope(TENANT, CUSTOM_ONLY_COMMUNITY),
            T,
            f.custom_only_member,
            &role("moderator"),
        )
        .await
        .unwrap_err();
    assert!(matches!(err, PlatformTableError::Invalid(_)), "{err:?}");
    assert_eq!(linked_role(&f.su, f.custom_only_member).await, "member");
}

#[tokio::test(flavor = "multi_thread")]
async fn protected_owner_is_never_modified() {
    let f = fixture().await;
    let err = f
        .store
        .write(&scope(TENANT, COMMUNITY), T, f.owner, &role("member"))
        .await
        .unwrap_err();
    assert_eq!(err, PlatformTableError::NotAMember);
    assert_eq!(linked_role(&f.su, f.owner).await, "community-owner");
}
