//! Real-Postgres test of the durable audit sink: the EXACT shipped DDL
//! (`scripts/db/bundle_storage_audit.sql`, also run by alembic 0080), a real
//! `StorageHost` over `MemoryBackend` auditing into it, and the INSERT-only role's
//! privilege boundary. Requires Docker.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use bundle_host_storage::pg_audit::{connect, AuditConnectConfig};
use bundle_host_storage::{
    GrantSet, MemoryBackend, PgAuditSink, StorageError, StorageHost, StorageScope,
};
use bytes::Bytes;
use sea_orm::{ConnectionTrait, Database, DbBackend, Statement};
#[allow(dead_code)]
#[path = "../../test_support/tests/docker_container.rs"]
mod docker_container;
use docker_container::start_container_blocking;

const IMAGE: &str = "postgres:17.6-bookworm";
const SU_PW: &str = "postgres_test_superuser_pw";
const ROLE_PW: &str = "waddles_bundle_storage_audit_test_pw";
const DDL: &str = include_str!("../../../scripts/db/bundle_storage_audit.sql");
const OWNER: &str = "waddles.harness.media";
const CONSUMER: &str = "waddles.app.clips";

fn scope(app: &str, grants: &[&str]) -> StorageScope {
    StorageScope {
        app_id: app.into(),
        tenant: 1,
        community: 7,
        grants: GrantSet::from_permission_ids(grants.iter().copied()).expect("grants"),
        enabled: true,
        request_id: "req-pg-1".into(),
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn audit_rows_land_in_postgres_and_role_is_insert_only() {
    let container = tokio::task::spawn_blocking(|| {
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
    .unwrap()
    .expect("postgres container");
    let (host, port) = (container.host().to_string(), container.port());
    let su = Database::connect(format!(
        "postgres://postgres:{SU_PW}@{host}:{port}/waddles_test"
    ))
    .await
    .unwrap();
    su.execute_unprepared(&format!(
        "CREATE ROLE waddles_bundle_storage_audit LOGIN PASSWORD '{ROLE_PW}'"
    ))
    .await
    .unwrap();
    su.execute_unprepared(DDL).await.unwrap();
    su.execute_unprepared(DDL).await.expect("DDL is idempotent");

    let cfg = AuditConnectConfig {
        host: host.clone(),
        port,
        name: "waddles_test".into(),
        user: "waddles_bundle_storage_audit".into(),
    };
    let role_db = connect(&cfg, ROLE_PW).await.unwrap();
    let backend = Arc::new(MemoryBackend::new());
    let h =
        StorageHost::with_rate_limit(backend, Arc::new(PgAuditSink::new(role_db.clone())), 1000);

    let owner = scope(OWNER, &[]);
    let consumer = scope(CONSUMER, &["storage.read:waddles.harness.media"]);
    h.put(
        &owner,
        None,
        "k.mp4",
        Bytes::from_static(b"x"),
        "video/mp4",
        &[],
    )
    .await
    .unwrap();
    // denied (untagged) -> row; tag op -> row; allowed cross-prefix read -> row
    assert_eq!(
        h.get(&consumer, Some(OWNER), "k.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
    h.flush_denials().await; // denials are written asynchronously; keep ledger order deterministic
    h.tag_add(&owner, None, "k.mp4", CONSUMER).await.unwrap();
    assert!(h.get(&consumer, Some(OWNER), "k.mp4").await.is_ok());
    let nogrant = scope(CONSUMER, &[]);
    assert!(h.get(&nogrant, Some(OWNER), "k.mp4").await.is_err());

    h.flush_denials().await;
    let rows = su
        .query_all_raw(Statement::from_string(
            DbBackend::Postgres,
            "SELECT op, outcome, caller_app_id, owner_app_id, tenant_id, community_id, request_id \
             FROM bundle_storage_audit ORDER BY id",
        ))
        .await
        .unwrap();
    let got: Vec<(String, String)> = rows
        .iter()
        .map(|r| {
            (
                r.try_get_by_index::<String>(0).unwrap(),
                r.try_get_by_index::<String>(1).unwrap(),
            )
        })
        .collect();
    assert_eq!(
        got,
        vec![
            ("get".to_string(), "denied".to_string()),
            ("tag_add".to_string(), "allowed".to_string()),
            ("get".to_string(), "allowed".to_string()),
            // no approved grant: audited under the grant op that was missing
            ("read".to_string(), "denied".to_string()),
        ]
    );
    let first = &rows[0];
    assert_eq!(first.try_get_by_index::<String>(2).unwrap(), CONSUMER);
    assert_eq!(first.try_get_by_index::<String>(3).unwrap(), OWNER);
    assert_eq!(first.try_get_by_index::<i64>(4).unwrap(), 1);
    assert_eq!(first.try_get_by_index::<i64>(5).unwrap(), 7);
    assert_eq!(first.try_get_by_index::<String>(6).unwrap(), "req-pg-1");

    // Guest-influenced strings with NUL/control chars: the row is STILL written, field -> <invalid>.
    use bundle_host_storage::{AuditEvent, AuditOutcome, AuditSink};
    PgAuditSink::new(role_db.clone())
        .record(AuditEvent {
            caller: CONSUMER.into(),
            owner: "bad\0owner".into(),
            tenant: 1,
            community: 7,
            op: "tag_add",
            key: "k\u{1}".into(),
            outcome: AuditOutcome::Denied,
            reason: "tag ops are owner-only".into(),
            request_id: "req-pg-2".into(),
        })
        .await
        .expect("a hostile field must not fail the insert");
    let hostile = su
        .query_one_raw(Statement::from_string(
            DbBackend::Postgres,
            "SELECT owner_app_id, object_key FROM bundle_storage_audit WHERE request_id = 'req-pg-2'",
        ))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(hostile.try_get_by_index::<String>(0).unwrap(), "<invalid>");
    assert_eq!(hostile.try_get_by_index::<String>(1).unwrap(), "<invalid>");

    // Privilege boundary: INSERT-only (no read-back, no tamper, no delete).
    for sql in [
        "SELECT * FROM bundle_storage_audit",
        "UPDATE bundle_storage_audit SET reason = 'x'",
        "DELETE FROM bundle_storage_audit",
        "TRUNCATE bundle_storage_audit",
        "DROP TABLE bundle_storage_audit",
    ] {
        assert!(
            role_db.execute_unprepared(sql).await.is_err(),
            "{sql} must be denied"
        );
    }

    // Sink failure surfaces: a dropped table makes an ALLOWED cross-prefix op fail closed.
    su.execute_unprepared("ALTER TABLE bundle_storage_audit RENAME TO audit_moved")
        .await
        .unwrap();
    let r = h.get(&consumer, Some(OWNER), "k.mp4").await;
    assert!(matches!(r, Err(StorageError::Backend(_))), "{r:?}");
}
