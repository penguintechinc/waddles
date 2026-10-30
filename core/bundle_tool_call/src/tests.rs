//! Policy tests -- the P3b Review Focus matrix against the pure planner.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use bundle_active_set::{
    ActiveBundleRow, ActiveVersionSnapshot, ExportedToolInfo, RequestedToolInfo, ToolManifestInfo,
};
use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, HostInvokeScopeBuilder,
    InMemoryGrantSnapshot, InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
    InvokeScope, PermissionId, TenantTier,
};
use penguin_bundle_host::wire::{ErrorBody, ErrorCode, Message, ResultBody};

use super::*;

const APP: &str = "waddles.test.app.caller";
const SVC: &str = "waddles.core.economy.svc";
const SVC2: &str = "waddles.core.other.svc";
const DIGEST: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn gate_scope(app: &str) -> InvokeScope {
    HostInvokeScopeBuilder::new()
        .tenant_id(7)
        .community_id(3)
        .app_id(app)
        .app_version(11)
        .tenant_tier(TenantTier::Free)
        .build()
        .unwrap()
}

/// A gate whose grant set for `app` holds exactly `grants` (`target`, `tool`).
fn gate_with(app: &str, grants: &[(&str, &str)]) -> CapabilityGate {
    let snapshot = InMemoryGrantSnapshot::new();
    let mut map = HashMap::new();
    for (target, tool) in grants {
        let cid = PermissionId::ToolCall {
            target: (*target).to_string(),
            tool: (*tool).to_string(),
        }
        .canonical_id();
        map.insert(
            cid.clone(),
            GrantedPermission {
                permission_id: cid,
                params: serde_json::json!({}),
            },
        );
    }
    snapshot.set(
        GrantScopeKey::from_scope(&gate_scope(app)),
        GrantSet {
            permission_snapshot_hash: "h".to_string(),
            grants: map,
        },
    );
    CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    )
}

fn row(app: &str, version_id: i64, digest: &str, tools: ToolManifestInfo) -> ActiveBundleRow {
    ActiveBundleRow {
        app_id: app.to_string(),
        version: "1.0.0".to_string(),
        version_id,
        digest: digest.to_string(),
        component_key: String::new(),
        sidecar_key: String::new(),
        artifact_signature: None,
        artifact_signature_key_id: None,
        artifact_signed_approval_id: None,
        declared_capabilities: Vec::new(),
        tools,
    }
}

fn service(exports: &[(&str, bool)]) -> ToolManifestInfo {
    ToolManifestInfo {
        is_tool_service: true,
        exported: exports
            .iter()
            .map(|(n, w)| ExportedToolInfo {
                name: (*n).to_string(),
                write: *w,
            })
            .collect(),
        requested: Vec::new(),
    }
}

fn requests(reqs: &[(&str, &str, bool)]) -> ToolManifestInfo {
    ToolManifestInfo {
        is_tool_service: false,
        exported: Vec::new(),
        requested: reqs
            .iter()
            .map(|(t, n, w)| RequestedToolInfo {
                target: (*t).to_string(),
                tool: (*n).to_string(),
                write: *w,
            })
            .collect(),
    }
}

fn snapshot(rows: Vec<ActiveBundleRow>) -> ActiveVersionSnapshot {
    let s = ActiveVersionSnapshot::new();
    s.update_scope(7, 3, &rows);
    s
}

fn req(target: &str, tool: &str, key: Option<&str>) -> ToolRequest {
    ToolRequest {
        target: target.to_string(),
        tool: tool.to_string(),
        args: serde_json::json!({"x": 1}),
        idempotency_key: key.map(str::to_string),
    }
}

/// The happy-path world: `APP` requests+is granted `SVC:get_balance` (read) and
/// `SVC:adjust` (write); `SVC` is a service exporting both.
fn world() -> (ActiveVersionSnapshot, CapabilityGate) {
    let snap = snapshot(vec![
        row(
            APP,
            11,
            DIGEST,
            requests(&[(SVC, "get_balance", false), (SVC, "adjust", true)]),
        ),
        row(
            SVC,
            22,
            DIGEST,
            service(&[("get_balance", false), ("adjust", true)]),
        ),
    ]);
    (
        snap,
        gate_with(APP, &[(SVC, "get_balance"), (SVC, "adjust")]),
    )
}

fn plan(
    chain: &[&str],
    r: ToolRequest,
    snap: &ActiveVersionSnapshot,
    gate: &CapabilityGate,
) -> Result<ToolPlan, ToolRefusal> {
    let caller = *chain.last().unwrap();
    let chain: Vec<String> = chain.iter().map(|s| s.to_string()).collect();
    plan_tool_call(caller, &chain, r, snap, gate, &gate_scope(caller))
}

#[test]
fn a_app_to_service_with_grant_and_matrix_is_authorized_and_pinned() {
    let (snap, gate) = world();
    let p = plan(&[APP], req(SVC, "get_balance", None), &snap, &gate).unwrap();
    assert_eq!(p.target_app_id, SVC);
    assert_eq!(p.target_version, 22);
    assert_eq!(p.target_digest, DIGEST);
    assert!(!p.write);
    assert_eq!(p.next_chain, vec![APP.to_string(), SVC.to_string()]);
    let w = plan(&[APP], req(SVC, "adjust", Some("k-1")), &snap, &gate).unwrap();
    assert!(w.write);
    assert_eq!(
        nested_payload(&w),
        serde_json::json!({
            "tool": "adjust",
            "args": {"x": 1},
            "call_chain": [APP, SVC],
            "idempotency_key": "k-1",
        })
    );
}

#[test]
fn b_app_to_app_is_forbidden_even_with_a_grant() {
    // The "target" is an APP bundle (no service capability) that nonetheless
    // lists the tool, and the caller holds a grant for it.
    let mut fake_app = service(&[("get_balance", false)]);
    fake_app.is_tool_service = false;
    let snap = snapshot(vec![
        row(APP, 11, DIGEST, requests(&[(SVC, "get_balance", false)])),
        row(SVC, 22, DIGEST, fake_app),
    ]);
    let gate = gate_with(APP, &[(SVC, "get_balance")]);
    let err = plan(&[APP], req(SVC, "get_balance", None), &snap, &gate).unwrap_err();
    assert_eq!(err.code(), "forbidden", "{err:?}");
}

#[test]
fn c_grant_for_one_tool_does_not_authorize_another() {
    let snap = snapshot(vec![
        row(
            APP,
            11,
            DIGEST,
            requests(&[(SVC, "get_balance", false), (SVC2, "other", false)]),
        ),
        row(SVC, 22, DIGEST, service(&[("get_balance", false)])),
        row(SVC2, 23, DIGEST, service(&[("other", false)])),
    ]);
    // Granted only for SVC:get_balance; the caller (which requested SVC2:other)
    // calls SVC2:other.
    let gate = gate_with(APP, &[(SVC, "get_balance")]);
    let err = plan(&[APP], req(SVC2, "other", None), &snap, &gate).unwrap_err();
    assert_eq!(err.code(), "not_granted", "{err:?}");
    // And a grant is not enough on its own: no request in the manifest.
    let snap2 = snapshot(vec![
        row(APP, 11, DIGEST, requests(&[])),
        row(SVC, 22, DIGEST, service(&[("get_balance", false)])),
    ]);
    let gate2 = gate_with(APP, &[(SVC, "get_balance")]);
    let err = plan(&[APP], req(SVC, "get_balance", None), &snap2, &gate2).unwrap_err();
    assert_eq!(err.code(), "forbidden", "{err:?}");
}

#[test]
fn d_self_call_and_cycle_fail_fast_before_any_other_check() {
    // An empty snapshot and gate: if any later stage ran, the code would differ.
    let snap = snapshot(vec![]);
    let gate = gate_with(APP, &[]);
    let err = plan(&[APP], req(APP, "t", None), &snap, &gate).unwrap_err();
    assert_eq!(err.code(), "forbidden");
    assert!(err.message().contains("self-call"));
    // A -> B -> A: B calls A.
    let err = plan(&[APP, SVC], req(APP, "t", None), &snap, &gate).unwrap_err();
    assert_eq!(err.code(), "forbidden");
    assert!(err.message().contains("cycle"));
}

#[test]
fn e_depth_three_is_depth_exceeded_and_depth_two_is_allowed() {
    let (snap, gate) = world();
    // chain A -> B -> C is two hops taken; calling D is the third.
    let err = plan(
        &[APP, SVC, SVC2],
        req("waddles.core.fourth.svc", "t", None),
        &snap,
        &gate,
    )
    .unwrap_err();
    assert_eq!(err, ToolRefusal::DepthExceeded);
    // A -> B (chain len 2) calling C is hop 2: passes the depth guard (and then
    // fails on the caller's manifest, proving the guard itself did not trip).
    let err = plan(&[APP, SVC], req(SVC2, "t", None), &snap, &gate).unwrap_err();
    assert_ne!(err, ToolRefusal::DepthExceeded);
}

#[test]
fn f_unknown_or_deactivated_target_is_target_unavailable() {
    let snap = snapshot(vec![row(
        APP,
        11,
        DIGEST,
        requests(&[(SVC, "get_balance", false)]),
    )]);
    let gate = gate_with(APP, &[(SVC, "get_balance")]);
    let err = plan(&[APP], req(SVC, "get_balance", None), &snap, &gate).unwrap_err();
    assert_eq!(err.code(), "target_unavailable");
}

#[test]
fn f_target_resolves_to_the_live_digest_after_a_hot_swap() {
    let (snap, gate) = world();
    let new_digest = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    snap.update_scope(
        7,
        3,
        &[
            row(
                APP,
                11,
                DIGEST,
                requests(&[(SVC, "get_balance", false), (SVC, "adjust", true)]),
            ),
            row(SVC, 33, new_digest, service(&[("get_balance", false)])),
        ],
    );
    let p = plan(&[APP], req(SVC, "get_balance", None), &snap, &gate).unwrap();
    assert_eq!(
        (p.target_version, p.target_digest.as_str()),
        (33, new_digest)
    );
    // The swapped-in version no longer exports `adjust`: refused, not run.
    let err = plan(&[APP], req(SVC, "adjust", Some("k")), &snap, &gate).unwrap_err();
    assert_eq!(err.code(), "forbidden");
}

#[test]
fn g_write_without_key_and_read_requested_for_write_tool_are_refused() {
    let (snap, gate) = world();
    let err = plan(&[APP], req(SVC, "adjust", None), &snap, &gate).unwrap_err();
    assert_eq!(err.code(), "invalid_args");
    // Caller only requested READ of a tool the service exports as WRITE.
    let snap2 = snapshot(vec![
        row(APP, 11, DIGEST, requests(&[(SVC, "adjust", false)])),
        row(SVC, 22, DIGEST, service(&[("adjust", true)])),
    ]);
    let gate2 = gate_with(APP, &[(SVC, "adjust")]);
    let err = plan(&[APP], req(SVC, "adjust", Some("k")), &snap2, &gate2).unwrap_err();
    assert_eq!(err.code(), "forbidden");
}

#[test]
fn g_parse_request_rejects_every_malformed_shape() {
    let ok = serde_json::json!({
        "target": SVC, "tool": "get_balance", "args": {"a": 1}, "idempotency_key": null
    });
    assert!(parse_request(&ok).is_ok());
    let big = "x".repeat(MAX_ARGS_BYTES + 1);
    let cases = [
        serde_json::json!("nope"),
        serde_json::json!({"tool": "t", "args": {}}),
        serde_json::json!({"target": "not-an-app", "tool": "t", "args": {}}),
        serde_json::json!({"target": SVC, "tool": "Bad-Name", "args": {}}),
        serde_json::json!({"target": SVC, "tool": "t"}),
        serde_json::json!({"target": SVC, "tool": "t", "args": {"k": big}}),
        serde_json::json!({"target": SVC, "tool": "t", "args": {}, "idempotency_key": 5}),
        serde_json::json!({"target": SVC, "tool": "t", "args": {}, "idempotency_key": ""}),
        serde_json::json!({"target": SVC, "tool": "t", "args": {}, "idempotency_key": "has space"}),
    ];
    for c in cases {
        let err = parse_request(&c).unwrap_err();
        assert_eq!(err.code(), "invalid_args", "{c}");
    }
}

#[test]
fn h_nested_deadline_is_min_of_remaining_and_cap_and_spent_is_timeout() {
    assert_eq!(nested_deadline(None).unwrap(), TOOL_CALL_CAP);
    let far = Instant::now() + Duration::from_secs(60);
    assert_eq!(nested_deadline(Some(far)).unwrap(), TOOL_CALL_CAP);
    let near = Instant::now() + Duration::from_millis(500);
    assert!(nested_deadline(Some(near)).unwrap() <= Duration::from_millis(500));
    let spent = Instant::now() - Duration::from_millis(1);
    assert_eq!(
        nested_deadline(Some(spent)).unwrap_err(),
        ToolRefusal::Timeout
    );
}

#[test]
fn interpret_reply_maps_envelopes_and_executor_errors() {
    let result = |payload| {
        Message::Result(ResultBody {
            payload,
            duration_ms: 1,
            fuel_used: 0,
        })
    };
    let err = |code, detail: Option<&str>| {
        Message::Error(ErrorBody {
            code,
            message: "m".to_string(),
            detail: detail.map(str::to_string),
        })
    };
    let ok = serde_json::json!({"ok": {"v": 1}});
    assert_eq!(interpret_reply(result(ok.clone())).unwrap(), ok);
    let te = serde_json::json!({"tool_error": "Forbidden"});
    assert_eq!(interpret_reply(result(te.clone())).unwrap(), te);
    for bad in [
        serde_json::json!({"ok": 1, "tool_error": 2}),
        serde_json::json!({"other": 1}),
        serde_json::json!(null),
        serde_json::json!(5),
    ] {
        assert_eq!(
            interpret_reply(result(bad)).unwrap_err().code(),
            "target_unavailable"
        );
    }
    assert_eq!(
        interpret_reply(err(ErrorCode::ExecutorDeadline, Some("pool_exhausted"))).unwrap_err(),
        ToolRefusal::PoolExhausted
    );
    assert_eq!(
        interpret_reply(err(ErrorCode::ExecutorDeadline, None)).unwrap_err(),
        ToolRefusal::Timeout
    );
    assert_eq!(
        interpret_reply(err(ErrorCode::UnknownBundle, None))
            .unwrap_err()
            .code(),
        "target_unavailable"
    );
    assert_eq!(
        interpret_reply(err(ErrorCode::WasmTrap, None))
            .unwrap_err()
            .code(),
        "target_unavailable"
    );
}

/// Builds a snapshot with `rows` installed in `(tenant, community)` only.
fn snapshot_in(tenant: i32, community: i32, rows: Vec<ActiveBundleRow>) -> ActiveVersionSnapshot {
    let s = ActiveVersionSnapshot::new();
    s.update_scope(tenant, community, &rows);
    s
}

fn caller_row() -> ActiveBundleRow {
    row(
        APP,
        11,
        DIGEST,
        requests(&[(SVC, "get_balance", false), (SVC, "adjust", true)]),
    )
}

fn svc_row() -> ActiveBundleRow {
    row(
        SVC,
        22,
        DIGEST,
        service(&[("get_balance", false), ("adjust", true)]),
    )
}

// regression: tenant-isolation -- (a) a caller installed only in ANOTHER
// tenant never satisfies the caller lookup.
#[test]
fn tenant_a_caller_in_a_different_tenant_is_forbidden() {
    let snap = ActiveVersionSnapshot::new();
    snap.update_scope(8, 3, &[caller_row()]);
    snap.update_scope(7, 3, &[svc_row()]);
    let gate = gate_with(APP, &[(SVC, "get_balance")]);
    let err = plan(&[APP], req(SVC, "get_balance", None), &snap, &gate).unwrap_err();
    assert_eq!(err.code(), "forbidden");
}

// (b) a target installed only in ANOTHER tenant is target_unavailable.
#[test]
fn tenant_b_target_in_a_different_tenant_is_target_unavailable() {
    let snap = ActiveVersionSnapshot::new();
    snap.update_scope(7, 3, &[caller_row()]);
    snap.update_scope(8, 3, &[svc_row()]);
    let gate = gate_with(APP, &[(SVC, "get_balance")]);
    let err = plan(&[APP], req(SVC, "get_balance", None), &snap, &gate).unwrap_err();
    assert_eq!(err.code(), "target_unavailable");
}

// (c) same-tenant caller + target still resolves, even with an unrelated
// look-alike installed in another tenant.
#[test]
fn tenant_c_same_tenant_caller_and_target_resolve() {
    let snap = ActiveVersionSnapshot::new();
    snap.update_scope(7, 3, &[caller_row(), svc_row()]);
    snap.update_scope(
        8,
        3,
        &[row(
            SVC,
            99,
            "sha256:other",
            service(&[("get_balance", false)]),
        )],
    );
    let gate = gate_with(APP, &[(SVC, "get_balance")]);
    let p = plan(&[APP], req(SVC, "get_balance", None), &snap, &gate).unwrap();
    assert_eq!((p.target_version, p.target_digest.as_str()), (22, DIGEST));
}

// (d) a tenant-wide (community = 0) service resolves from a community-scoped
// call; a community-0 service in ANOTHER tenant does not.
#[test]
fn tenant_d_tenant_wide_service_resolves_from_a_community_scoped_call() {
    let snap = snapshot_in(7, 3, vec![caller_row()]);
    snap.update_scope(7, 0, &[svc_row()]);
    let gate = gate_with(APP, &[(SVC, "get_balance")]);
    let p = plan(&[APP], req(SVC, "get_balance", None), &snap, &gate).unwrap();
    assert_eq!(p.target_version, 22);

    let other = snapshot_in(7, 3, vec![caller_row()]);
    other.update_scope(8, 0, &[svc_row()]);
    let err = plan(&[APP], req(SVC, "get_balance", None), &other, &gate).unwrap_err();
    assert_eq!(err.code(), "target_unavailable");
}
