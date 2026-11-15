//! `object_storage.*` host-capability tests (spec 2026-11-06-bundle-object-storage): the
//! REAL [`CapabilityGate`], the REAL `handle` dispatch and the REAL [`StorageHost`] run
//! against an in-memory object backend; the S3 backend itself is proven against a real
//! SeaweedFS in `core/bundle_host_storage/tests/seaweedfs_real_path.rs`.

use super::*;
use crate::license::test_support::FixedGate;
use base64::Engine as _;
use bundle_capability_gate::{
    GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use bundle_host_http::egress::{ReqwestTransport, StaticFlag};
use bundle_host_storage::{MemoryBackend, TracingAuditSink};

const TENANT_ID: i32 = 7;
const COMMUNITY_ID: i32 = 3;
const APP_ID: &str = "waddles.core.test-store";
const OTHER_APP: &str = "waddles.core.other-store";
const VERSION: i64 = 1;

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

/// Builds the caps under test plus the shared host, so a test can seed another
/// bundle's prefix directly.
fn fixture(ids: &[&str], flag_on: bool, wired: bool) -> (StageCapabilities, Arc<StorageHost>) {
    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(
        GrantScopeKey {
            tenant_id: TENANT_ID,
            community_id: COMMUNITY_ID,
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
            prometheus::Opts::new("test_os_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .unwrap(),
        bundle_host_http::egress::boxed(StaticFlag(true)),
    ));
    let mut caps = StageCapabilities::new(
        "acme".to_string(),
        Some("main".to_string()),
        APP_ID.to_string(),
        TENANT_ID,
        COMMUNITY_ID,
        VERSION,
        egress,
        gate,
    );
    let host = Arc::new(StorageHost::new(
        Arc::new(MemoryBackend::new()),
        Arc::new(TracingAuditSink),
    ));
    if wired {
        caps = caps.with_object_storage(ObjectStorageWiring {
            host: Arc::clone(&host),
            flag: Arc::new(FixedGate(flag_on)),
        });
    }
    (caps, host)
}

fn os_call(op: &str, args: serde_json::Value) -> HostCallBody {
    HostCallBody {
        app_id: APP_ID.to_string(),
        capability: CapabilityKind::Db,
        op: format!("object_storage.{op}"),
        args,
        call_id: 1,
    }
}

fn put_args(key: &str, owner: Option<&str>) -> serde_json::Value {
    serde_json::json!({
        "key": key, "owner": owner, "content_type": "text/plain",
        "body_b64": base64::engine::general_purpose::STANDARD.encode(b"hello"),
    })
}

fn get_args(key: &str, owner: Option<&str>) -> serde_json::Value {
    serde_json::json!({ "key": key, "owner": owner })
}

async fn code_of(caps: &StageCapabilities, op: &str, args: serde_json::Value) -> String {
    caps.handle(os_call(op, args))
        .await
        .expect_err("expected a denial")
        .code
}

/// Seeds `key` under OTHER_APP's prefix in the same tenant/community.
async fn seed_other(host: &StorageHost, key: &str) {
    let scope = bundle_host_storage::StorageScope {
        app_id: OTHER_APP.to_string(),
        tenant: TENANT_ID as u64,
        community: COMMUNITY_ID as u64,
        grants: bundle_host_storage::GrantSet::default(),
        enabled: true,
        request_id: "seed".to_string(),
    };
    host.put(
        &scope,
        None,
        key,
        bundle_host_storage::Bytes::from_static(b"secret"),
        "text/plain",
        &[],
    )
    .await
    .expect("seed");
}

/// The owner shares `key` with APP_ID (host-side tag filter: a grant alone is not enough).
async fn share_with_caller(host: &StorageHost, key: &str) {
    let scope = bundle_host_storage::StorageScope {
        app_id: OTHER_APP.to_string(),
        tenant: TENANT_ID as u64,
        community: COMMUNITY_ID as u64,
        grants: bundle_host_storage::GrantSet::default(),
        enabled: true,
        request_id: "share".to_string(),
    };
    host.tag_add(&scope, None, key, APP_ID).await.expect("tag");
}

#[tokio::test]
async fn unprovisioned_deployment_denies_not_implemented() {
    let (caps, _) = fixture(&[], true, false);
    assert_eq!(
        code_of(&caps, "put", put_args("a.txt", None)).await,
        "not_implemented"
    );
}

#[tokio::test]
async fn provisioned_but_flag_off_denies_feature_disabled() {
    let (caps, _) = fixture(&[], false, true);
    assert_eq!(
        code_of(&caps, "put", put_args("a.txt", None)).await,
        "feature_disabled"
    );
}

#[tokio::test]
async fn provisioned_bundle_round_trips_its_own_prefix() {
    let (caps, _) = fixture(&[], true, true);
    caps.handle(os_call("put", put_args("a.txt", None)))
        .await
        .expect("put own prefix");
    let got = caps
        .handle(os_call("get", get_args("a.txt", None)))
        .await
        .expect("get own prefix");
    assert_eq!(
        got["body_b64"],
        base64::engine::general_purpose::STANDARD.encode(b"hello")
    );
}

#[tokio::test]
async fn cross_prefix_without_grant_is_refused_before_the_host() {
    let (caps, host) = fixture(&[], true, true);
    seed_other(&host, "s.txt").await;
    for op in ["get", "head"] {
        assert_eq!(
            code_of(&caps, op, get_args("s.txt", Some(OTHER_APP))).await,
            "not_granted",
            "{op}"
        );
    }
    assert_eq!(
        code_of(&caps, "put", put_args("s.txt", Some(OTHER_APP))).await,
        "not_granted"
    );
}

#[tokio::test]
async fn own_key_never_resolves_into_another_bundles_prefix() {
    let (caps, host) = fixture(&[], true, true);
    seed_other(&host, "s.txt").await;
    // Same key name, own prefix: the other bundle's object is invisible.
    let code = code_of(&caps, "get", get_args("s.txt", None)).await;
    assert_eq!(code, "not_found");
}

#[tokio::test]
async fn cross_prefix_with_read_grant_reads_but_cannot_write() {
    let (caps, host) = fixture(&["storage.read:waddles.core.other-store"], true, true);
    seed_other(&host, "s.txt").await;
    // Granted but not tagged for us: hidden exactly like a missing object.
    assert_eq!(
        code_of(&caps, "get", get_args("s.txt", Some(OTHER_APP))).await,
        "not_found"
    );
    share_with_caller(&host, "s.txt").await;
    let got = caps
        .handle(os_call("get", get_args("s.txt", Some(OTHER_APP))))
        .await
        .expect("granted cross-prefix read");
    assert_eq!(
        got["body_b64"],
        base64::engine::general_purpose::STANDARD.encode(b"secret")
    );
    assert_eq!(
        code_of(&caps, "put", put_args("s.txt", Some(OTHER_APP))).await,
        "not_granted"
    );
}

#[tokio::test]
async fn unknown_op_is_refused_loudly() {
    let (caps, _) = fixture(&[], true, true);
    assert_eq!(
        code_of(&caps, "frobnicate", serde_json::json!({})).await,
        "unknown_op"
    );
}
