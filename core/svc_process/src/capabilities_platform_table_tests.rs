//! `platform_table.*` host-capability tests (spec 2026-11-06): the REAL
//! [`CapabilityGate`] and the REAL `handle` dispatch run against a recording store
//! double; the store's SQL/scope is proven against a real Postgres in
//! `core/bundle_host_platform_table/tests/postgres_integration.rs`.

use super::*;
use crate::license::test_support::FixedGate;
use bundle_capability_gate::{
    GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use bundle_host_http::egress::{ReqwestTransport, StaticFlag};
use std::sync::Mutex;
use uuid::Uuid;

const TENANT_ID: i32 = 7;
const COMMUNITY_ID: i32 = 3;
const APP_ID: &str = "waddles.core.test-role-sync";
const VERSION: i64 = 1;
const WRITE_ROLE: &str = "platform.table.write:community_members.role";
const READ_ROLE: &str = "platform.table.read:community_members.role";

type Recorded = (String, PlatformTableScope, Uuid, Vec<String>);

#[derive(Default)]
struct RecordingStore {
    calls: Mutex<Vec<Recorded>>,
}

impl PlatformTableStore for RecordingStore {
    fn read<'a>(
        &'a self,
        scope: &'a PlatformTableScope,
        _table: &'a str,
        user: Uuid,
        columns: &'a [String],
    ) -> bundle_host_platform_table::BoxFuture<'a, Result<serde_json::Value, PlatformTableError>>
    {
        Box::pin(async move {
            self.calls
                .lock()
                .unwrap()
                .push(("read".into(), scope.clone(), user, columns.to_vec()));
            Ok(serde_json::json!({ "role": "vip" }))
        })
    }
    fn write<'a>(
        &'a self,
        scope: &'a PlatformTableScope,
        _table: &'a str,
        user: Uuid,
        changes: &'a [PlatformTableChange],
    ) -> bundle_host_platform_table::BoxFuture<'a, Result<(), PlatformTableError>> {
        Box::pin(async move {
            self.calls.lock().unwrap().push((
                "write".into(),
                scope.clone(),
                user,
                changes.iter().map(|c| c.column.clone()).collect(),
            ));
            Ok(())
        })
    }
}

struct Fixture {
    caps: StageCapabilities,
    store: Arc<RecordingStore>,
}

fn grant_set(ids: &[&str]) -> GrantSet {
    GrantSet {
        permission_snapshot_hash: "test".to_string(),
        grants: ids
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
            .collect(),
    }
}

fn fixture_with(ids: &[&str], flag_on: bool, wired: bool, community: i32) -> Fixture {
    fixture_scoped(ids, flag_on, wired, community, community)
}

/// Grants are keyed under `grant_community`; the invocation runs in `community`.
fn fixture_scoped(
    ids: &[&str],
    flag_on: bool,
    wired: bool,
    grant_community: i32,
    community: i32,
) -> Fixture {
    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(
        GrantScopeKey {
            tenant_id: TENANT_ID,
            community_id: grant_community,
            app_id: APP_ID.to_string(),
            app_version: VERSION,
        },
        grant_set(ids),
    );
    let gate = Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ));
    let egress = Arc::new(EgressGuard::new(
        Arc::new(ReqwestTransport::new()),
        bundle_host_http::egress::EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: std::time::Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        },
        HttpEgressCatalog::new(),
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("test_pt_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .unwrap(),
        bundle_host_http::egress::boxed(StaticFlag(true)),
    ));
    let mut caps = StageCapabilities::new(
        "acme".to_string(),
        (community != 0).then(|| "main".to_string()),
        APP_ID.to_string(),
        TENANT_ID,
        community,
        VERSION,
        egress,
        gate,
    );
    let store = Arc::new(RecordingStore::default());
    if wired {
        caps = caps.with_platform_table(PlatformTableWiring {
            store: Arc::clone(&store) as Arc<dyn PlatformTableStore>,
            flag: Arc::new(FixedGate(flag_on)),
        });
    }
    Fixture { caps, store }
}

fn pt_call(op: &str, args: serde_json::Value) -> HostCallBody {
    HostCallBody {
        app_id: APP_ID.to_string(),
        capability: CapabilityKind::Db,
        op: op.to_string(),
        args,
        call_id: 1,
    }
}

fn write_args(user: Uuid, column: &str, value: &str) -> serde_json::Value {
    serde_json::json!({
        "table": "community_members",
        "user": user.to_string(),
        "changes": [{"column": column, "value": value}],
    })
}

async fn code_of(f: &Fixture, op: &str, args: serde_json::Value) -> String {
    f.caps
        .handle(pt_call(op, args))
        .await
        .expect_err("expected a denial")
        .code
}

#[tokio::test]
async fn unapproved_write_is_denied_before_the_store() {
    let f = fixture_with(&[], true, true, COMMUNITY_ID);
    let code = code_of(
        &f,
        "platform_table.write",
        write_args(Uuid::new_v4(), "role", "vip"),
    )
    .await;
    assert_eq!(code, "not_granted");
    assert!(f.store.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn approved_write_reaches_the_store_with_host_derived_scope() {
    let f = fixture_with(&[WRITE_ROLE], true, true, COMMUNITY_ID);
    let user = Uuid::new_v4();
    // Guest-supplied scope fields must be ignored entirely.
    let mut args = write_args(user, "role", "vip");
    args["tenant_id"] = serde_json::json!(999);
    args["community_id"] = serde_json::json!(999);
    f.caps
        .handle(pt_call("platform_table.write", args))
        .await
        .unwrap();
    let calls = f.store.calls.lock().unwrap();
    assert_eq!(calls.len(), 1);
    assert_eq!(
        calls[0].1,
        PlatformTableScope {
            tenant_id: TENANT_ID,
            community_id: COMMUNITY_ID,
            app_id: APP_ID.to_string(),
            app_version: VERSION,
        }
    );
    assert_eq!(calls[0].2, user);
}

#[tokio::test]
async fn read_grant_does_not_authorize_write_and_vice_versa() {
    let f = fixture_with(&[READ_ROLE], true, true, COMMUNITY_ID);
    assert_eq!(
        code_of(
            &f,
            "platform_table.write",
            write_args(Uuid::new_v4(), "role", "vip")
        )
        .await,
        "not_granted"
    );
    let ok = f
        .caps
        .handle(pt_call(
            "platform_table.read",
            serde_json::json!({
                "table": "community_members",
                "user": Uuid::new_v4().to_string(),
                "columns": ["role"],
            }),
        ))
        .await
        .unwrap();
    assert_eq!(ok["columns"]["role"], "vip");
}

#[tokio::test]
async fn grant_for_another_community_does_not_apply() {
    // Approved for community 4, invoked in community 3.
    let f = fixture_scoped(&[WRITE_ROLE], true, true, 4, COMMUNITY_ID);
    assert_eq!(
        code_of(
            &f,
            "platform_table.write",
            write_args(Uuid::new_v4(), "role", "vip")
        )
        .await,
        "not_granted"
    );
    assert!(f.store.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn off_allowlist_column_or_table_is_denied_not_allowlisted() {
    let f = fixture_with(&[WRITE_ROLE, READ_ROLE], true, true, COMMUNITY_ID);
    for column in ["display_name", "is_active", "user_uuid"] {
        assert_eq!(
            code_of(
                &f,
                "platform_table.write",
                write_args(Uuid::new_v4(), column, "x")
            )
            .await,
            "not_allowlisted",
            "{column}"
        );
    }
    let hub = serde_json::json!({
        "table": "hub_users", "user": Uuid::new_v4().to_string(), "columns": ["email"],
    });
    assert_eq!(
        code_of(&f, "platform_table.read", hub).await,
        "not_allowlisted"
    );
    assert!(f.store.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn every_named_column_needs_its_own_grant() {
    // Only `role` is granted for read; also asking for `is_active` is denied.
    let f = fixture_with(&[READ_ROLE], true, true, COMMUNITY_ID);
    let args = serde_json::json!({
        "table": "community_members",
        "user": Uuid::new_v4().to_string(),
        "columns": ["role", "is_active"],
    });
    assert_eq!(
        code_of(&f, "platform_table.read", args).await,
        "not_granted"
    );
    assert!(f.store.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn unwired_flag_off_and_no_community_fail_loud() {
    let args = write_args(Uuid::new_v4(), "role", "vip");
    let unwired = fixture_with(&[WRITE_ROLE], true, false, COMMUNITY_ID);
    assert_eq!(
        code_of(&unwired, "platform_table.write", args.clone()).await,
        "not_implemented"
    );
    let off = fixture_with(&[WRITE_ROLE], false, true, COMMUNITY_ID);
    assert_eq!(
        code_of(&off, "platform_table.write", args.clone()).await,
        "feature_disabled"
    );
    let tenant_wide = fixture_with(&[WRITE_ROLE], true, true, 0);
    assert_eq!(
        code_of(&tenant_wide, "platform_table.write", args).await,
        "invalid_args"
    );
}

#[tokio::test]
async fn malformed_arguments_are_invalid_args() {
    let f = fixture_with(&[WRITE_ROLE], true, true, COMMUNITY_ID);
    for args in [
        serde_json::json!({}),
        serde_json::json!({"table": "community_members", "user": "nope", "changes": []}),
        serde_json::json!({"table": "community_members", "user": Uuid::new_v4().to_string()}),
        serde_json::json!({"table": "community_members", "user": Uuid::new_v4().to_string(), "changes": []}),
        serde_json::json!({"table": "community_members", "user": Uuid::new_v4().to_string(), "changes": [{"column": "role"}]}),
    ] {
        assert_eq!(
            code_of(&f, "platform_table.write", args).await,
            "invalid_args"
        );
    }
    assert_eq!(
        code_of(&f, "platform_table.nope", serde_json::json!({})).await,
        "unknown_op"
    );
}
