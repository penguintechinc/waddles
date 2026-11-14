//! Action-stage bundle storage v2 against REAL Postgres and Valkey containers.
//!
//! Drives `StageCapabilities::handle` (the exact entry the executor host-call
//! reaches) with `db2.*` and the kv v2 ops, proving the action stage serves them
//! (it previously answered `unknown_op`) with scope injected host-side. The table
//! DDL restates what `hub_api/services/bundle_data_v2.py` emits (platform columns,
//! FORCE RLS, tenant+community policy), exactly as
//! `core/bundle_host_db/tests/postgres_v2_integration.rs` does -- keep in lockstep.
//!
//! Requires Docker (the `docker` CLI, `core/test_support`).

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use bundle_host_db::v2::{
    BundleSchemaV2, DbV2Host, IndexColumn, IndexDef, PostgresV2Backend, SchemaRegistryV2,
    TableDefV2,
};
use bundle_host_db::{AppSchema, CapabilitySnapshot, ColumnDef, ColumnType, TableSchema};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody};
use redis::aio::MultiplexedConnection;
use sea_orm::{ConnectionTrait, Database, DatabaseConnection};
use svc_action::capabilities::{
    CapabilityHandler, DbV2Wiring, DbWiring, InvokeScope, RelayQueue, StageCapabilities,
};
use svc_action::distribution::BundleCatalog;
use svc_action::egress::{EgressGuard, EgressLimits};
use svc_action::flags::{boxed, StaticFlag};
use svc_action::usage::UsageBatcher;

#[allow(dead_code)]
#[path = "../../test_support/tests/docker_container.rs"]
mod docker_container;
use docker_container::{start_container, DockerContainer};

const POSTGRES_IMAGE: &str = "postgres:17.6-bookworm";
const VALKEY_IMAGE: &str = "valkey/valkey:8.1.10-alpine";
const SUPERUSER_PASSWORD: &str = "postgres_test_superuser_pw";
const RUNTIME_PASSWORD: &str = "waddles_bundle_runtime_test_pw";
const RUNTIME_ROLE: &str = "waddles_bundle_runtime";
const APP: &str = "waddles.cal.test";

/// The relay queue is not exercised here; every method fails loudly if reached.
struct UnusedQueue;

impl RelayQueue for UnusedQueue {
    fn lpush<'a>(
        &'a self,
        _key: &'a str,
        _value: String,
    ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>> {
        Box::pin(async { Err("relay queue must not be used by storage tests".to_string()) })
    }
    fn key_exists<'a>(
        &'a self,
        _key: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<bool, String>> + Send + 'a>> {
        Box::pin(async { Err("relay queue must not be used by storage tests".to_string()) })
    }
    fn incr_window<'a>(
        &'a self,
        _key: &'a str,
        _window_secs: u64,
    ) -> Pin<Box<dyn Future<Output = Result<u64, String>> + Send + 'a>> {
        Box::pin(async { Err("relay queue must not be used by storage tests".to_string()) })
    }
    fn take<'a>(
        &'a self,
        _key: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Option<String>, String>> + Send + 'a>> {
        Box::pin(async { Err("relay queue must not be used by storage tests".to_string()) })
    }
}

fn scope(community_id: i32) -> InvokeScope {
    InvokeScope {
        tenant: "acme".to_string(),
        community: Some(format!("c{community_id}")),
        app_id: APP.to_string(),
        origin_channel_id: None,
        origin_platform: None,
        origin_guild_id: None,
        origin_user_id: None,
        tenant_id: 7,
        community_id,
        app_version: 1,
        tool_chain: Vec::new(),
        deadline_at: None,
        trace: None,
    }
}

fn gate(communities: &[i32]) -> Arc<CapabilityGate> {
    let snapshot = InMemoryGrantSnapshot::new();
    for community_id in communities {
        let grants = ["storage.kv", "storage.tables"]
            .iter()
            .map(|id| {
                (
                    (*id).to_string(),
                    GrantedPermission {
                        permission_id: (*id).to_string(),
                        params: serde_json::json!({}),
                    },
                )
            })
            .collect::<HashMap<_, _>>();
        snapshot.set(
            GrantScopeKey {
                tenant_id: 7,
                community_id: *community_id,
                app_id: APP.to_string(),
                app_version: 1,
            },
            GrantSet {
                permission_snapshot_hash: "test".to_string(),
                grants,
            },
        );
    }
    Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ))
}

fn egress() -> Arc<EgressGuard> {
    Arc::new(EgressGuard::new(
        Arc::new(svc_action::egress::ReqwestTransport::new()),
        EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        },
        Arc::new(BundleCatalog::new()),
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("storage_v2_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition"),
        boxed(StaticFlag(true)),
    ))
}

async fn start_postgres() -> (DockerContainer, String) {
    let container = start_container(
        POSTGRES_IMAGE,
        5432,
        &[
            ("POSTGRES_PASSWORD", SUPERUSER_PASSWORD),
            ("POSTGRES_DB", "waddles_test"),
        ],
        &["pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
        Duration::from_secs(240),
    )
    .await;
    let url = format!(
        "postgres://postgres:{SUPERUSER_PASSWORD}@{}:{}/waddles_test",
        container.host(),
        container.port()
    );
    (container, url)
}

async fn start_valkey() -> (DockerContainer, String) {
    let container = start_container(
        VALKEY_IMAGE,
        6379,
        &[],
        &["valkey-cli", "ping"],
        Duration::from_secs(120),
    )
    .await;
    let url = format!("redis://{}:{}/", container.host(), container.port());
    (container, url)
}

/// Creates the `bundle_data` platform tables and the one declared `events` table.
async fn setup_postgres(conn: &DatabaseConnection) {
    let q = "\"app_community\".\"calapp__events\"";
    let stmts: Vec<String> = vec![
        "CREATE SCHEMA IF NOT EXISTS app_core".into(),
        "CREATE SCHEMA IF NOT EXISTS app_community".into(),
        "CREATE SCHEMA IF NOT EXISTS bundle_data".into(),
        format!(
            "CREATE ROLE {RUNTIME_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{RUNTIME_PASSWORD}'"
        ),
        format!("GRANT USAGE ON SCHEMA app_core, app_community, bundle_data TO {RUNTIME_ROLE}"),
        "CREATE TABLE bundle_data.usage (app_id text NOT NULL, tenant_id integer NOT NULL, \
         community_id integer NOT NULL, row_count bigint NOT NULL DEFAULT 0 CHECK (row_count >= 0), \
         byte_count bigint NOT NULL DEFAULT 0 CHECK (byte_count >= 0), \
         PRIMARY KEY (app_id, tenant_id, community_id))"
            .into(),
        "ALTER TABLE bundle_data.usage ENABLE ROW LEVEL SECURITY".into(),
        "ALTER TABLE bundle_data.usage FORCE ROW LEVEL SECURITY".into(),
        format!(
            "CREATE POLICY usage_scope ON bundle_data.usage TO {RUNTIME_ROLE} USING (\
             tenant_id = NULLIF(current_setting('waddles.tenant_id', true), '')::integer \
             AND community_id = NULLIF(current_setting('waddles.community_id', true), '')::integer)"
        ),
        format!("GRANT SELECT, INSERT, UPDATE ON bundle_data.usage TO {RUNTIME_ROLE}"),
        format!(
            "CREATE TABLE {q} (row_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), \
             tenant_id integer NOT NULL, community_id integer NOT NULL, \
             version integer NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(), \
             updated_at timestamptz NOT NULL DEFAULT now(), \
             \"title\" varchar(200) NOT NULL, \"kind\" integer)"
        ),
        format!("CREATE INDEX ON {q} (tenant_id, community_id, \"kind\")"),
        format!("ALTER TABLE {q} ENABLE ROW LEVEL SECURITY"),
        format!("ALTER TABLE {q} FORCE ROW LEVEL SECURITY"),
        format!(
            "CREATE POLICY \"events_tenant_isolation\" ON {q} USING \
             (tenant_id = NULLIF(current_setting('waddles.tenant_id', true), '')::integer \
             AND community_id = NULLIF(current_setting('waddles.community_id', true), '')::integer)"
        ),
        format!("GRANT SELECT, INSERT, UPDATE, DELETE ON {q} TO \"{RUNTIME_ROLE}\""),
    ];
    for s in stmts {
        conn.execute_unprepared(&s)
            .await
            .unwrap_or_else(|e| panic!("setup failed ({s:?}): {e}"));
    }
}

fn events_registry() -> SchemaRegistryV2 {
    let col = |name: &str, t: ColumnType, nullable: bool| ColumnDef {
        name: name.into(),
        sql_type: t,
        nullable,
        is_user_ref: false,
    };
    let events = TableSchema::validated(
        AppSchema::Community,
        "calapp__events",
        vec![
            col("title", ColumnType::Text, false),
            col("kind", ColumnType::Int4, true),
        ],
    )
    .expect("valid table schema");
    let table = TableDefV2::validated(
        "events",
        events,
        vec![IndexDef {
            unique: false,
            columns: vec![IndexColumn {
                name: "kind".into(),
                descending: false,
                prefix_ops: false,
            }],
        }],
    )
    .expect("valid table def");
    let registry = SchemaRegistryV2::new();
    registry.update(BundleSchemaV2::validated(APP, vec![table]).expect("valid bundle schema"));
    registry
}

fn call(capability: CapabilityKind, op: &str, args: serde_json::Value) -> HostCallBody {
    HostCallBody {
        app_id: APP.to_string(),
        capability,
        op: op.to_string(),
        args,
        call_id: 1,
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn action_stage_serves_db_v2_and_kv_v2_against_real_postgres_and_valkey() {
    let (_pg, admin_url) = start_postgres().await;
    let (_valkey, valkey_url) = start_valkey().await;

    let admin = Database::connect(&admin_url).await.expect("admin connect");
    setup_postgres(&admin).await;
    let runtime_url = admin_url.replacen(
        &format!("postgres:{SUPERUSER_PASSWORD}"),
        &format!("{RUNTIME_ROLE}:{RUNTIME_PASSWORD}"),
        1,
    );
    let runtime = Database::connect(&runtime_url)
        .await
        .expect("runtime connect");

    let kv_conn: MultiplexedConnection = redis::Client::open(valkey_url.as_str())
        .expect("valkey client")
        .get_multiplexed_async_connection()
        .await
        .expect("valkey connection");
    let kv_caps = bundle_host_kv::CapabilitySnapshot::new();
    kv_caps.update(APP, ["storage.kv".to_string()]);

    let db = DbWiring {
        host: Arc::new(bundle_host_db::DbHost::new(
            bundle_host_db::PostgresBackend::new(runtime.clone()),
        )),
        schemas: Arc::new(bundle_host_db::SchemaCache::new()),
        capabilities: Arc::new(CapabilitySnapshot::new()),
        flag: boxed(StaticFlag(true)),
        v2: DbV2Wiring {
            host: Arc::new(DbV2Host::new(PostgresV2Backend::new(runtime))),
            registry: Arc::new(events_registry()),
            capabilities: Arc::new(CapabilitySnapshot::new()),
        },
    };
    let caps = StageCapabilities::<_, MultiplexedConnection>::new(
        UnusedQueue,
        egress(),
        Arc::new(Mutex::new(UsageBatcher::new())),
        gate(&[3, 4]),
    )
    .with_kv(kv_conn, Arc::new(kv_caps))
    .with_db(db);

    // ---- db2: insert / get / update (CAS) / query / count, all scope-injected ----
    let inserted = caps
        .handle(
            &scope(3),
            call(
                CapabilityKind::Db,
                "db2.insert",
                serde_json::json!({"table": "events", "columns": {"title": "launch", "kind": 1}}),
            ),
        )
        .await
        .expect("db2.insert is served by the action stage");
    let row_id = inserted["row_id"].as_str().expect("row_id").to_string();
    assert_eq!(inserted["version"], 1);

    let got = caps
        .handle(
            &scope(3),
            call(
                CapabilityKind::Db,
                "db2.get",
                serde_json::json!({"table": "events", "row_id": row_id}),
            ),
        )
        .await
        .expect("db2.get");
    assert_eq!(got["columns"]["title"], "launch");

    let updated = caps
        .handle(
            &scope(3),
            call(
                CapabilityKind::Db,
                "db2.update",
                serde_json::json!({"table": "events", "row_id": row_id, "expected_version": 1,
                    "columns": {"title": "launch2"}}),
            ),
        )
        .await
        .expect("db2.update");
    assert_eq!(updated["version"], 2);

    let stale = caps
        .handle(
            &scope(3),
            call(
                CapabilityKind::Db,
                "db2.update",
                serde_json::json!({"table": "events", "row_id": row_id, "expected_version": 1,
                    "columns": {"title": "stale"}}),
            ),
        )
        .await
        .expect_err("stale CAS must conflict");
    assert_eq!(stale.code, "conflict");

    let page = caps
        .handle(
            &scope(3),
            call(
                CapabilityKind::Db,
                "db2.query",
                serde_json::json!({"table": "events",
                    "filter": [{"op": "eq", "column": "kind", "value": 1}], "limit": 10}),
            ),
        )
        .await
        .expect("db2.query");
    assert_eq!(page["rows"].as_array().expect("rows").len(), 1);

    // Another community in the same tenant sees nothing (host-injected scope + RLS).
    let other_count = caps
        .handle(
            &scope(4),
            call(
                CapabilityKind::Db,
                "db2.count",
                serde_json::json!({"table": "events", "filter": []}),
            ),
        )
        .await
        .expect("db2.count");
    assert_eq!(other_count["count"], 0);

    let unknown = caps
        .handle(
            &scope(3),
            call(CapabilityKind::Db, "db2.drop", serde_json::json!({})),
        )
        .await
        .expect_err("unknown db2 op fails loud");
    assert_eq!(unknown.code, "unknown_op");

    // ---- kv v2: get_versioned / cas / delete_if / list ----
    let created = caps
        .handle(
            &scope(3),
            call(
                CapabilityKind::Kv,
                "cas",
                serde_json::json!({"key": "cfg.a", "expected_version": 0, "value": [1, 2, 3],
                    "ttl_seconds": 0}),
            ),
        )
        .await
        .expect("kv cas create");
    assert_eq!(created["swapped"], true);
    let v1 = created["version"].as_u64().expect("version");

    let versioned = caps
        .handle(
            &scope(3),
            call(
                CapabilityKind::Kv,
                "get_versioned",
                serde_json::json!({"key": "cfg.a"}),
            ),
        )
        .await
        .expect("kv get_versioned");
    assert_eq!(versioned["version"].as_u64(), Some(v1));
    assert_eq!(versioned["value"], serde_json::json!([1, 2, 3]));

    let lost = caps
        .handle(
            &scope(3),
            call(
                CapabilityKind::Kv,
                "cas",
                serde_json::json!({"key": "cfg.a", "expected_version": v1 + 1, "value": [9],
                    "ttl_seconds": 0}),
            ),
        )
        .await
        .expect("stale cas is a clean conflict result, not an error");
    assert_eq!(lost["swapped"], false);

    let listed = caps
        .handle(
            &scope(3),
            call(
                CapabilityKind::Kv,
                "list",
                serde_json::json!({"prefix": "cfg.", "limit": 10}),
            ),
        )
        .await
        .expect("kv list");
    assert_eq!(listed["keys"], serde_json::json!(["cfg.a"]));

    // kv is community-scoped: community 4 sees none of community 3's keys.
    let other_listed = caps
        .handle(
            &scope(4),
            call(
                CapabilityKind::Kv,
                "list",
                serde_json::json!({"prefix": "cfg.", "limit": 10}),
            ),
        )
        .await
        .expect("kv list other community");
    assert_eq!(other_listed["keys"], serde_json::json!([]));

    let deleted = caps
        .handle(
            &scope(3),
            call(
                CapabilityKind::Kv,
                "delete_if",
                serde_json::json!({"key": "cfg.a", "expected_version": v1}),
            ),
        )
        .await
        .expect("kv delete_if");
    assert_eq!(deleted["deleted"], true);
    let gone = caps
        .handle(
            &scope(3),
            call(
                CapabilityKind::Kv,
                "get_versioned",
                serde_json::json!({"key": "cfg.a"}),
            ),
        )
        .await
        .expect("kv get_versioned after delete");
    assert!(gone["value"].is_null());
}
