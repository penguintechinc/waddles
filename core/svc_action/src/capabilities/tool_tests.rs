//! `handle_tool` tests (svc_action twin of svc_process's `tool_tests`): the
//! P3b Review Focus matrix through the real `CapabilityHandler::handle`
//! dispatch and a real handshaken host-API `Connection` talking to a scripted
//! fake executor -- the nested invoke, per-invoke scope registration and host
//! timeout run on the wire, not mocked.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use bundle_active_set::{
    ActiveBundleRow, ActiveVersionSnapshot, ExportedToolInfo, RequestedToolInfo, ToolManifestInfo,
};
use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger, PermissionId,
};
use penguin_bundle_host::wire::{
    read_frame, write_frame, CapabilityKind, ErrorBody, ErrorCode, ExportKind, Frame, HelloBody,
    HelloLimits, HelloOkBody, HostCallBody, HostResultBody, InvokeBody, Message, ResultBody,
    SandboxInfo,
};

use super::super::tests::{test_egress, FakeRelayQueue};
use super::*;
use crate::capabilities::CapabilityHandler;
use crate::usage::UsageBatcher;

const APP: &str = "waddles.test.app.caller";
const SVC: &str = "waddles.core.economy.svc";
const SVC2: &str = "waddles.core.other.svc";
const DIGEST: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const TENANT_ID: i32 = 7;
const COMMUNITY_ID: i32 = 3;
const APP_VERSION: i64 = 11;

fn row(app: &str, version_id: i64, tools: ToolManifestInfo) -> ActiveBundleRow {
    ActiveBundleRow {
        app_id: app.to_string(),
        version: "1.0.0".to_string(),
        version_id,
        digest: DIGEST.to_string(),
        component_key: String::new(),
        sidecar_key: String::new(),
        artifact_signature: None,
        artifact_signature_key_id: None,
        artifact_signed_approval_id: None,
        declared_capabilities: Vec::new(),
        tools,
    }
}

fn manifest(
    is_service: bool,
    exports: &[(&str, bool)],
    requests: &[(&str, &str, bool)],
) -> ToolManifestInfo {
    ToolManifestInfo {
        is_tool_service: is_service,
        exported: exports
            .iter()
            .map(|(n, w)| ExportedToolInfo {
                name: (*n).to_string(),
                write: *w,
            })
            .collect(),
        requested: requests
            .iter()
            .map(|(t, n, w)| RequestedToolInfo {
                target: (*t).to_string(),
                tool: (*n).to_string(),
                write: *w,
            })
            .collect(),
    }
}

/// A gate granting each `(grantee, version, target, tool)` its `tool.call`.
fn gate(grants: &[(&str, i64, &str, &str)]) -> Arc<CapabilityGate> {
    let snapshot = InMemoryGrantSnapshot::new();
    let mut by_scope: HashMap<(String, i64), HashMap<String, GrantedPermission>> = HashMap::new();
    for (grantee, version, target, tool) in grants {
        let cid = PermissionId::ToolCall {
            target: (*target).to_string(),
            tool: (*tool).to_string(),
        }
        .canonical_id();
        by_scope
            .entry(((*grantee).to_string(), *version))
            .or_default()
            .insert(
                cid.clone(),
                GrantedPermission {
                    permission_id: cid,
                    params: serde_json::json!({}),
                },
            );
    }
    for ((grantee, version), grants) in by_scope {
        snapshot.set(
            GrantScopeKey {
                tenant_id: TENANT_ID,
                community_id: COMMUNITY_ID,
                app_id: grantee,
                app_version: version,
            },
            GrantSet {
                permission_snapshot_hash: "h".to_string(),
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

/// What the fake executor does with an invoke.
#[derive(Clone)]
enum Behavior {
    Reply(serde_json::Value),
    Error(ErrorCode, Option<&'static str>),
    /// Never answers (the host timeout must fire).
    Never,
    /// While serving the invoke, issues a nested `tool.invoke` host call with
    /// these args, then answers `{"ok": {"nested": <host reply>}}`.
    NestedCallThenReply(serde_json::Value),
}

struct World {
    caps: Arc<StageCapabilities<FakeRelayQueue>>,
    invokes: Arc<Mutex<Vec<InvokeBody>>>,
}

/// Builds the stage handler over `rows`/`gate`, a handshaken connection to a
/// scripted executor (registered in the handler's own registry), and records
/// each `InvokeBody` the executor receives.
async fn world(
    rows: Vec<ActiveBundleRow>,
    gate: Arc<CapabilityGate>,
    behavior: Behavior,
    with_connection: bool,
) -> World {
    let entries = rows
        .into_iter()
        .map(|r| (TENANT_ID, COMMUNITY_ID, r))
        .collect();
    world_scoped(entries, gate, behavior, with_connection).await
}

/// Like [`world`] but each row is installed in its own `(tenant, community)`.
async fn world_scoped(
    entries: Vec<(i32, i32, ActiveBundleRow)>,
    gate: Arc<CapabilityGate>,
    behavior: Behavior,
    with_connection: bool,
) -> World {
    let snapshot = ActiveVersionSnapshot::new();
    snapshot.update_scoped_all(
        &entries
            .into_iter()
            .map(|(t, c, r)| ((t, c, r.app_id.clone()), r))
            .collect(),
    );
    let registry = Arc::new(ConnectionRegistry::new());
    let caps = Arc::new(
        StageCapabilities::new(
            FakeRelayQueue::default(),
            test_egress(),
            Arc::new(Mutex::new(UsageBatcher::new())),
            gate,
        )
        .with_tool(ToolWiring {
            connections: Arc::clone(&registry),
            snapshot,
        }),
    );
    let invokes: Arc<Mutex<Vec<InvokeBody>>> = Arc::default();
    if !with_connection {
        return World { caps, invokes };
    }
    let (stage_io, mut exec_io) = tokio::io::duplex(256 * 1024);
    let seen = Arc::clone(&invokes);
    tokio::spawn(async move {
        write_frame(
            &mut exec_io,
            &Frame::new(
                1,
                Message::Hello(HelloBody {
                    protocol_version: 1,
                    executor_version: "0.1.0".to_string(),
                    wasmtime_version: "test".to_string(),
                    wasmtime_abi: "test".to_string(),
                    collector: "drc".to_string(),
                    sandbox: SandboxInfo {
                        runtime: "runc".to_string(),
                        verified: false,
                    },
                }),
            ),
        )
        .await
        .unwrap();
        let _hello_ok = read_frame(&mut exec_io).await.unwrap();
        let (mut rd, mut wr) = tokio::io::split(exec_io);
        let (host_tx, mut host_rx) = tokio::sync::mpsc::unbounded_channel::<HostResultBody>();
        let (job_tx, mut job_rx) = tokio::sync::mpsc::unbounded_channel::<Frame>();
        tokio::spawn(async move {
            while let Ok(frame) = read_frame(&mut rd).await {
                match frame.message {
                    Message::HostResult(b) => {
                        let _ = host_tx.send(b);
                    }
                    Message::Invoke(_) => {
                        let _ = job_tx.send(frame);
                    }
                    _ => {}
                }
            }
        });
        while let Some(frame) = job_rx.recv().await {
            let Message::Invoke(body) = frame.message else {
                continue;
            };
            seen.lock().unwrap().push(body.clone());
            let reply = match behavior.clone() {
                Behavior::Reply(v) => Some(Message::Result(ResultBody {
                    payload: v,
                    duration_ms: 1,
                    fuel_used: 0,
                })),
                Behavior::Error(code, detail) => Some(Message::Error(ErrorBody {
                    code,
                    message: "m".to_string(),
                    detail: detail.map(str::to_string),
                })),
                Behavior::Never => None,
                Behavior::NestedCallThenReply(args) => {
                    write_frame(
                        &mut wr,
                        &Frame::new(
                            9000 + frame.id,
                            Message::HostCall(HostCallBody {
                                app_id: body.app_id.clone(),
                                capability: CapabilityKind::Db,
                                op: "tool.invoke".to_string(),
                                args,
                                call_id: frame.id,
                            }),
                        ),
                    )
                    .await
                    .unwrap();
                    let host = host_rx.recv().await.unwrap();
                    let nested = match (host.result, host.error) {
                        (Some(v), _) => serde_json::json!({"result": v}),
                        (_, Some(e)) => serde_json::json!({"error_code": e.code}),
                        _ => serde_json::json!(null),
                    };
                    Some(Message::Result(ResultBody {
                        payload: serde_json::json!({"ok": {"nested": nested}}),
                        duration_ms: 1,
                        fuel_used: 0,
                    }))
                }
            };
            if let Some(m) = reply {
                write_frame(&mut wr, &Frame::new(frame.id, m))
                    .await
                    .unwrap();
            }
        }
    });
    let handler: Arc<dyn CapabilityHandler> = Arc::clone(&caps) as Arc<dyn CapabilityHandler>;
    let (connection, read_loop) = crate::host_api::run_connection(
        stage_io,
        HelloOkBody {
            stage: "svc-action".to_string(),
            protocol_version: 1,
            limits: HelloLimits {
                call_timeout_ms: 2000,
                memory_mb: 64,
                max_concurrent_calls: 32,
            },
        },
        false,
        handler,
    )
    .await
    .expect("handshake");
    tokio::spawn(read_loop);
    registry.set_active(connection);
    World { caps, invokes }
}

/// `APP` (app) requests SVC's read+write tools; `SVC` (service) exports both
/// and requests `SVC2:ping`; `SVC2` exports `ping`.
fn world_rows() -> Vec<ActiveBundleRow> {
    vec![
        row(
            APP,
            APP_VERSION,
            manifest(
                false,
                &[],
                &[(SVC, "get_balance", false), (SVC, "adjust", true)],
            ),
        ),
        row(
            SVC,
            22,
            manifest(
                true,
                &[("get_balance", false), ("adjust", true)],
                &[(SVC2, "ping", false)],
            ),
        ),
        row(SVC2, 33, manifest(true, &[("ping", false)], &[])),
    ]
}

fn default_gate() -> Arc<CapabilityGate> {
    gate(&[
        (APP, APP_VERSION, SVC, "get_balance"),
        (APP, APP_VERSION, SVC, "adjust"),
        (SVC, 22, SVC2, "ping"),
    ])
}

fn scope_for(app: &str, version: i64, chain: &[&str], deadline: Option<Instant>) -> InvokeScope {
    InvokeScope {
        tenant: "acme".to_string(),
        community: Some("main".to_string()),
        app_id: app.to_string(),
        origin_channel_id: None,
        origin_platform: None,
        tenant_id: TENANT_ID,
        community_id: COMMUNITY_ID,
        app_version: version,
        tool_chain: chain.iter().map(|s| (*s).to_string()).collect(),
        deadline_at: deadline,
        trace: None,
    }
}

fn app_scope() -> InvokeScope {
    scope_for(
        APP,
        APP_VERSION,
        &[APP],
        Some(Instant::now() + Duration::from_secs(10)),
    )
}

fn tool_call(target: &str, tool: &str, key: Option<&str>) -> HostCallBody {
    HostCallBody {
        app_id: APP.to_string(),
        capability: CapabilityKind::Db,
        op: "tool.invoke".to_string(),
        args: serde_json::json!({
            "target": target, "tool": tool, "args": {"user": "u1"}, "idempotency_key": key,
        }),
        call_id: 1,
    }
}

async fn run(
    w: &World,
    scope: &InvokeScope,
    call: HostCallBody,
) -> Result<serde_json::Value, String> {
    w.caps.handle(scope, call).await.map_err(|e| e.code)
}

fn recorded(w: &World) -> Vec<InvokeBody> {
    w.invokes.lock().unwrap().clone()
}

#[tokio::test]
async fn a_app_to_service_happy_path_returns_target_result_and_pins_the_nested_invoke() {
    let w = world(
        world_rows(),
        default_gate(),
        Behavior::Reply(serde_json::json!({"ok": {"balance": 5}})),
        true,
    )
    .await;
    let out = run(&w, &app_scope(), tool_call(SVC, "get_balance", None))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"ok": {"balance": 5}}));
    let inv = recorded(&w);
    assert_eq!(inv.len(), 1);
    assert_eq!(inv[0].app_id, SVC);
    assert_eq!(inv[0].digest, DIGEST, "the platform-installed digest");
    assert_eq!(inv[0].export, ExportKind::ToolCall);
    assert_eq!(
        inv[0].payload,
        serde_json::json!({
            "tool": "get_balance",
            "args": {"user": "u1"},
            "call_chain": [APP, SVC],
            "idempotency_key": null,
        })
    );
    assert!(inv[0].deadline_ms >= 1 && inv[0].deadline_ms <= 8000);
}

#[tokio::test]
async fn a_write_tool_carries_the_idempotency_key_and_passes_through_tool_errors() {
    let w = world(
        world_rows(),
        default_gate(),
        Behavior::Reply(serde_json::json!({"tool_error": "Forbidden"})),
        true,
    )
    .await;
    let out = run(&w, &app_scope(), tool_call(SVC, "adjust", Some("k-1")))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"tool_error": "Forbidden"}));
    // Principal-scoped (sha256 hex), never the caller's raw key; the same
    // token rides top-level and in the forwarded args.
    let fwd = recorded(&w)[0].payload["idempotency_key"]
        .as_str()
        .unwrap()
        .to_string();
    assert_ne!(fwd, "k-1");
    assert_eq!(fwd.len(), 64);
    assert!(fwd.bytes().all(|b| b.is_ascii_hexdigit()));
    assert_eq!(
        recorded(&w)[0].payload["args"]["__idempotency_key"],
        fwd.as_str()
    );
}

#[tokio::test]
async fn b_app_to_app_is_forbidden_even_with_a_grant_and_no_invoke_is_sent() {
    let mut rows = world_rows();
    rows[1].tools.is_tool_service = false; // SVC demoted to a plain app
    let w = world(
        rows,
        default_gate(),
        Behavior::Reply(serde_json::json!({"ok": 1})),
        true,
    )
    .await;
    assert_eq!(
        run(&w, &app_scope(), tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "forbidden"
    );
    assert!(recorded(&w).is_empty());
}

#[tokio::test]
async fn c_a_grant_for_one_tool_does_not_authorize_another_target_or_tool() {
    let w = world(
        world_rows(),
        gate(&[(APP, APP_VERSION, SVC, "get_balance")]),
        Behavior::Reply(serde_json::json!({"ok": 1})),
        true,
    )
    .await;
    assert_eq!(
        run(&w, &app_scope(), tool_call(SVC, "adjust", Some("k")))
            .await
            .unwrap_err(),
        "not_granted"
    );
    // A grant pinned to another app VERSION is not this caller's grant.
    let w2 = world(
        world_rows(),
        gate(&[(APP, APP_VERSION + 1, SVC, "get_balance")]),
        Behavior::Reply(serde_json::json!({"ok": 1})),
        true,
    )
    .await;
    assert_eq!(
        run(&w2, &app_scope(), tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "not_granted"
    );
    assert!(recorded(&w).is_empty() && recorded(&w2).is_empty());
}

#[tokio::test]
async fn d_self_call_and_cycle_fail_fast_with_no_executor_traffic() {
    let w = world(world_rows(), default_gate(), Behavior::Never, true).await;
    let started = Instant::now();
    assert_eq!(
        run(&w, &app_scope(), tool_call(APP, "get_balance", None))
            .await
            .unwrap_err(),
        "forbidden"
    );
    let cyc = scope_for(APP, APP_VERSION, &[SVC, APP], None);
    assert_eq!(
        run(&w, &cyc, tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "forbidden"
    );
    assert!(recorded(&w).is_empty(), "no slot/invoke was requested");
    assert!(started.elapsed() < Duration::from_secs(1), "no hang");
}

#[tokio::test]
async fn d_cycle_through_a_real_nested_invoke_is_refused_inside_the_target_scope() {
    let w = world(
        world_rows(),
        default_gate(),
        Behavior::NestedCallThenReply(serde_json::json!({
            "target": APP, "tool": "x", "args": {}, "idempotency_key": null,
        })),
        true,
    )
    .await;
    let out = run(&w, &app_scope(), tool_call(SVC, "get_balance", None))
        .await
        .unwrap();
    assert_eq!(
        out,
        serde_json::json!({"ok": {"nested": {"error_code": "forbidden"}}})
    );
    assert_eq!(
        recorded(&w).len(),
        1,
        "only the first hop reached the executor"
    );
}

#[tokio::test]
async fn e_depth_three_is_refused_and_depth_two_runs() {
    let w = world(
        world_rows(),
        default_gate(),
        Behavior::Reply(serde_json::json!({"ok": 1})),
        true,
    )
    .await;
    let deep = scope_for(APP, APP_VERSION, &["waddles.x.y.z", SVC2, APP], None);
    assert_eq!(
        run(&w, &deep, tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "depth_exceeded"
    );
    assert!(recorded(&w).is_empty());
    // A service calling a service from chain [APP, SVC] is hop 2: allowed.
    let hop2 = scope_for(SVC, 22, &[APP, SVC], None);
    let mut call = tool_call(SVC2, "ping", None);
    call.app_id = SVC.to_string();
    assert_eq!(
        run(&w, &hop2, call).await.unwrap(),
        serde_json::json!({"ok": 1})
    );
    assert_eq!(
        recorded(&w)[0].payload["call_chain"],
        serde_json::json!([APP, SVC, SVC2])
    );
}

#[tokio::test]
async fn f_unknown_stale_or_tampered_target_is_target_unavailable() {
    let w = world(
        world_rows(),
        default_gate(),
        Behavior::Error(ErrorCode::DigestMismatch, None),
        true,
    )
    .await;
    assert_eq!(
        run(&w, &app_scope(), tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "target_unavailable"
    );
    let rows = world_rows()
        .into_iter()
        .filter(|r| r.app_id != SVC)
        .collect();
    let w2 = world(
        rows,
        default_gate(),
        Behavior::Reply(serde_json::json!({"ok": 1})),
        true,
    )
    .await;
    assert_eq!(
        run(&w2, &app_scope(), tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "target_unavailable"
    );
    assert!(recorded(&w2).is_empty(), "never reached the executor");
}

#[tokio::test]
async fn f_target_not_loaded_on_this_stage_executor_is_a_typed_cross_stage_refusal() {
    for code in [ErrorCode::UnknownBundle, ErrorCode::ExportMissing] {
        let w = world(
            world_rows(),
            default_gate(),
            Behavior::Error(code, None),
            true,
        )
        .await;
        assert_eq!(
            run(&w, &app_scope(), tool_call(SVC, "get_balance", None))
                .await
                .unwrap_err(),
            "target_unavailable",
            "{code:?}"
        );
    }
    let w = world(world_rows(), default_gate(), Behavior::Never, false).await;
    assert_eq!(
        run(&w, &app_scope(), tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "target_unavailable",
        "no executor connection"
    );
}

#[tokio::test]
async fn g_invalid_args_are_refused_before_any_executor_traffic() {
    let w = world(
        world_rows(),
        default_gate(),
        Behavior::Reply(serde_json::json!({"ok": 1})),
        true,
    )
    .await;
    let s = app_scope();
    assert_eq!(
        run(&w, &s, tool_call(SVC, "adjust", None))
            .await
            .unwrap_err(),
        "invalid_args"
    );
    let mut big = tool_call(SVC, "get_balance", None);
    big.args["args"] = serde_json::json!({"k": "x".repeat(70_000)});
    assert_eq!(run(&w, &s, big).await.unwrap_err(), "invalid_args");
    for args in [
        serde_json::json!("not an object"),
        serde_json::json!({"target": "nope", "tool": "t", "args": {}}),
        serde_json::json!({"target": SVC, "tool": "Bad Tool", "args": {}}),
        serde_json::json!({"target": SVC, "tool": "t"}),
    ] {
        let mut call = tool_call(SVC, "get_balance", None);
        call.args = args;
        assert_eq!(run(&w, &s, call).await.unwrap_err(), "invalid_args");
    }
    assert!(recorded(&w).is_empty());
}

#[tokio::test]
async fn h_a_slow_target_trips_the_host_timeout_not_the_wasm_trap() {
    let w = world(world_rows(), default_gate(), Behavior::Never, true).await;
    let s = scope_for(
        APP,
        APP_VERSION,
        &[APP],
        Some(Instant::now() + Duration::from_millis(250)),
    );
    let started = Instant::now();
    assert_eq!(
        run(&w, &s, tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "timeout"
    );
    let took = started.elapsed();
    assert!(
        took >= Duration::from_millis(200) && took < Duration::from_secs(3),
        "{took:?}"
    );
    // A spent outer budget is also `timeout`, without touching the executor.
    let spent = scope_for(
        APP,
        APP_VERSION,
        &[APP],
        Some(Instant::now() - Duration::from_millis(5)),
    );
    let before = recorded(&w).len();
    assert_eq!(
        run(&w, &spent, tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "timeout"
    );
    assert_eq!(recorded(&w).len(), before);
}

#[tokio::test]
async fn pool_exhaustion_and_malformed_envelope_are_typed() {
    let w = world(
        world_rows(),
        default_gate(),
        Behavior::Error(ErrorCode::ExecutorDeadline, Some("pool_exhausted")),
        true,
    )
    .await;
    assert_eq!(
        run(&w, &app_scope(), tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "pool_exhausted"
    );
    let w = world(
        world_rows(),
        default_gate(),
        Behavior::Reply(serde_json::json!({"surprise": true})),
        true,
    )
    .await;
    assert_eq!(
        run(&w, &app_scope(), tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "target_unavailable"
    );
}

#[tokio::test]
async fn unwired_tool_and_unknown_op_fail_loudly() {
    let bare = StageCapabilities::<_, redis::aio::MultiplexedConnection>::new(
        FakeRelayQueue::default(),
        test_egress(),
        Arc::new(Mutex::new(UsageBatcher::new())),
        default_gate(),
    );
    assert_eq!(
        bare.handle(&app_scope(), tool_call(SVC, "get_balance", None))
            .await
            .map_err(|e| e.code)
            .unwrap_err(),
        "target_unavailable"
    );
    let w = world(world_rows(), default_gate(), Behavior::Never, true).await;
    let mut call = tool_call(SVC, "get_balance", None);
    call.op = "tool.discover".to_string();
    assert_eq!(run(&w, &app_scope(), call).await.unwrap_err(), "unknown_op");
}

// regression: tenant-isolation -- caller/target facts resolve per tenant scope.
#[tokio::test]
async fn tenant_isolation_cross_tenant_caller_forbidden_target_unavailable_tenant_wide_ok() {
    let ok = || Behavior::Reply(serde_json::json!({"ok": 1}));
    let rows = world_rows();
    let caller = |r: &ActiveBundleRow| r.app_id == APP;

    // (a) caller installed only in ANOTHER tenant -> forbidden.
    let entries = rows
        .iter()
        .cloned()
        .map(|r| (if caller(&r) { 8 } else { TENANT_ID }, COMMUNITY_ID, r))
        .collect();
    let w = world_scoped(entries, default_gate(), ok(), true).await;
    assert_eq!(
        run(&w, &app_scope(), tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "forbidden"
    );

    // (b) target installed only in ANOTHER tenant -> target_unavailable.
    let entries = rows
        .iter()
        .cloned()
        .map(|r| (if r.app_id == SVC { 8 } else { TENANT_ID }, COMMUNITY_ID, r))
        .collect();
    let w = world_scoped(entries, default_gate(), ok(), true).await;
    assert_eq!(
        run(&w, &app_scope(), tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "target_unavailable"
    );
    assert!(recorded(&w).is_empty());

    // (c) same-tenant happy path still works.
    let w = world(rows.clone(), default_gate(), ok(), true).await;
    assert!(run(&w, &app_scope(), tool_call(SVC, "get_balance", None))
        .await
        .is_ok());

    // (d) tenant-wide (community 0) service reachable from a community call.
    let entries = rows
        .iter()
        .cloned()
        .map(|r| (TENANT_ID, if r.app_id == SVC { 0 } else { COMMUNITY_ID }, r))
        .collect();
    let w = world_scoped(entries, default_gate(), ok(), true).await;
    assert!(run(&w, &app_scope(), tool_call(SVC, "get_balance", None))
        .await
        .is_ok());
}

#[tokio::test]
async fn every_tool_call_completion_emits_a_count_and_a_latency_point() {
    let w = world(
        world_rows(),
        default_gate(),
        Behavior::Reply(serde_json::json!({"ok": {"balance": 5}})),
        true,
    )
    .await;
    run(&w, &app_scope(), tool_call(SVC, "get_balance", None))
        .await
        .unwrap();
    assert_eq!(
        run(&w, &app_scope(), tool_call("evil.<script>", "x", None))
            .await
            .unwrap_err(),
        "invalid_args"
    );
    // The process-wide test harness (`stage_metrics()` under cfg(test)).
    let h = crate::stage_metrics::test_support::shared();
    let ok = [("target", SVC), ("tool", "get_balance"), ("outcome", "ok")];
    assert!(h.count("waddles_tool_call_total", &ok) >= 1.0);
    assert!(h.count("waddles_tool_call_latency_seconds", &ok) >= 1.0);
    let unresolved = [("target", "unresolved"), ("outcome", "invalid_args")];
    assert!(h.count("waddles_tool_call_total", &unresolved) >= 1.0);
    assert!(
        !h.points()
            .iter()
            .any(|p| p.attrs.values().any(|v| v.contains("evil"))),
        "guest-supplied value leaked into a label"
    );
}
