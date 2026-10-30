//! `handle_tool` tests: the P3b Review Focus matrix driven through the real
//! `CapabilityHandler::handle` dispatch and a real handshaken host-API
//! `Connection` talking to a scripted fake executor (so the nested invoke,
//! the per-invoke capability scope, and the host timeout are all exercised on
//! the wire, not mocked).

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

use super::super::tests::test_egress;
use super::*;
use crate::capabilities::CapabilityHandler;

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

fn service(exports: &[(&str, bool)], requests: &[(&str, &str, bool)]) -> ToolManifestInfo {
    ToolManifestInfo {
        is_tool_service: true,
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

fn app(requests: &[(&str, &str, bool)]) -> ToolManifestInfo {
    ToolManifestInfo {
        is_tool_service: false,
        exported: Vec::new(),
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

struct FakeExecutor {
    registry: Arc<ConnectionRegistry>,
    invokes: Arc<Mutex<Vec<InvokeBody>>>,
}

/// A handshaken connection to a scripted executor answering every invoke per
/// `behavior`; records each `InvokeBody` it receives.
async fn fake_executor(behavior: Behavior) -> FakeExecutor {
    let (stage_io, mut exec_io) = tokio::io::duplex(256 * 1024);
    let invokes: Arc<Mutex<Vec<InvokeBody>>> = Arc::default();
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
        // Replies the stage sends to our host calls arrive on this channel.
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
    let (connection, read_loop) = crate::host_api::run_connection(
        stage_io,
        HelloOkBody {
            stage: "svc-process".to_string(),
            protocol_version: 1,
            limits: HelloLimits {
                call_timeout_ms: 2000,
                memory_mb: 64,
                max_concurrent_calls: 32,
            },
        },
        false,
        Arc::new(crate::capabilities::DenyAllCapabilities),
    )
    .await
    .expect("handshake");
    tokio::spawn(read_loop);
    let registry = Arc::new(ConnectionRegistry::new());
    registry.set_active(connection);
    FakeExecutor { registry, invokes }
}

/// The happy-path world's rows: `APP` (an app) requests SVC's read+write tools;
/// `SVC` (a service) exports both and requests `SVC2:ping`; `SVC2` exports
/// `ping` and requests `APP`-less nothing.
fn world_rows() -> Vec<ActiveBundleRow> {
    vec![
        row(
            APP,
            APP_VERSION,
            app(&[(SVC, "get_balance", false), (SVC, "adjust", true)]),
        ),
        row(
            SVC,
            22,
            service(
                &[("get_balance", false), ("adjust", true)],
                &[(SVC2, "ping", false)],
            ),
        ),
        row(SVC2, 33, service(&[("ping", false)], &[])),
    ]
}

fn default_gate() -> Arc<CapabilityGate> {
    gate(&[
        (APP, APP_VERSION, SVC, "get_balance"),
        (APP, APP_VERSION, SVC, "adjust"),
        (SVC, 22, SVC2, "ping"),
    ])
}

fn caps_with(
    rows: Vec<ActiveBundleRow>,
    gate: Arc<CapabilityGate>,
    registry: Arc<ConnectionRegistry>,
    chain: Vec<String>,
    deadline: Option<Instant>,
) -> StageCapabilities {
    let entries = rows
        .into_iter()
        .map(|r| (TENANT_ID, COMMUNITY_ID, r))
        .collect();
    caps_scoped(entries, gate, registry, chain, deadline)
}

/// Like [`caps_with`] but each row is installed in its own `(tenant, community)`.
fn caps_scoped(
    entries: Vec<(i32, i32, ActiveBundleRow)>,
    gate: Arc<CapabilityGate>,
    registry: Arc<ConnectionRegistry>,
    chain: Vec<String>,
    deadline: Option<Instant>,
) -> StageCapabilities {
    let snapshot = ActiveVersionSnapshot::new();
    let rows: Vec<ActiveBundleRow> = entries.iter().map(|(_, _, r)| r.clone()).collect();
    snapshot.update_scoped_all(
        &entries
            .into_iter()
            .map(|(t, c, r)| ((t, c, r.app_id.clone()), r))
            .collect(),
    );
    // The running app's own platform-installed version (grants are pinned to it).
    let version = rows
        .iter()
        .find(|r| Some(&r.app_id) == chain.last())
        .map_or(APP_VERSION, |r| r.version_id);
    StageCapabilities::new(
        "acme".to_string(),
        Some("main".to_string()),
        chain.last().cloned().unwrap_or_else(|| APP.to_string()),
        TENANT_ID,
        COMMUNITY_ID,
        version,
        test_egress(),
        gate,
    )
    .with_tool(ToolContext {
        connections: registry,
        snapshot,
        chain,
        deadline,
        trace: None,
    })
}

fn caps(exec: &FakeExecutor) -> StageCapabilities {
    caps_with(
        world_rows(),
        default_gate(),
        Arc::clone(&exec.registry),
        vec![APP.to_string()],
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

async fn run(c: &StageCapabilities, call: HostCallBody) -> Result<serde_json::Value, String> {
    c.handle(call).await.map_err(|e| e.code)
}

fn recorded(exec: &FakeExecutor) -> Vec<InvokeBody> {
    exec.invokes.lock().unwrap().clone()
}

#[tokio::test]
async fn a_app_to_service_happy_path_returns_target_result_and_pins_the_nested_invoke() {
    let exec = fake_executor(Behavior::Reply(serde_json::json!({"ok": {"balance": 5}}))).await;
    let out = run(&caps(&exec), tool_call(SVC, "get_balance", None))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"ok": {"balance": 5}}));
    let inv = recorded(&exec);
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
    let exec = fake_executor(Behavior::Reply(
        serde_json::json!({"tool_error": "Forbidden"}),
    ))
    .await;
    let out = run(&caps(&exec), tool_call(SVC, "adjust", Some("k-1")))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"tool_error": "Forbidden"}));
    // Principal-scoped (sha256 hex), never the caller's raw key; the same
    // token rides top-level and in the forwarded args.
    let fwd = recorded(&exec)[0].payload["idempotency_key"]
        .as_str()
        .unwrap()
        .to_string();
    assert_ne!(fwd, "k-1");
    assert_eq!(fwd.len(), 64);
    assert!(fwd.bytes().all(|b| b.is_ascii_hexdigit()));
    assert_eq!(
        recorded(&exec)[0].payload["args"]["__idempotency_key"],
        fwd.as_str()
    );
}

#[tokio::test]
async fn b_app_to_app_is_forbidden_even_with_a_grant_and_no_invoke_is_sent() {
    let exec = fake_executor(Behavior::Reply(serde_json::json!({"ok": 1}))).await;
    // SVC is demoted to a plain app bundle that still lists the tool.
    let mut rows = world_rows();
    rows[1].tools.is_tool_service = false;
    let c = caps_with(
        rows,
        default_gate(),
        Arc::clone(&exec.registry),
        vec![APP.to_string()],
        None,
    );
    assert_eq!(
        run(&c, tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "forbidden"
    );
    assert!(recorded(&exec).is_empty());
}

#[tokio::test]
async fn c_a_grant_for_one_tool_does_not_authorize_another_target_or_tool() {
    let exec = fake_executor(Behavior::Reply(serde_json::json!({"ok": 1}))).await;
    // The caller requested (manifest) SVC:adjust but was only granted
    // SVC:get_balance.
    let c = caps_with(
        world_rows(),
        gate(&[(APP, APP_VERSION, SVC, "get_balance")]),
        Arc::clone(&exec.registry),
        vec![APP.to_string()],
        None,
    );
    assert_eq!(
        run(&c, tool_call(SVC, "adjust", Some("k")))
            .await
            .unwrap_err(),
        "not_granted"
    );
    // A grant pinned to another app VERSION is not this caller's grant.
    let c = caps_with(
        world_rows(),
        gate(&[(APP, APP_VERSION + 1, SVC, "get_balance")]),
        Arc::clone(&exec.registry),
        vec![APP.to_string()],
        None,
    );
    assert_eq!(
        run(&c, tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "not_granted"
    );
    assert!(recorded(&exec).is_empty());
}

#[tokio::test]
async fn d_self_call_and_cycle_fail_fast_with_no_executor_traffic() {
    let exec = fake_executor(Behavior::Never).await;
    let started = Instant::now();
    // Self-call.
    let err = run(&caps(&exec), tool_call(APP, "get_balance", None))
        .await
        .unwrap_err();
    assert_eq!(err, "forbidden");
    // Cycle: the call chain already holds SVC.
    let c = caps_with(
        world_rows(),
        default_gate(),
        Arc::clone(&exec.registry),
        vec![SVC.to_string(), APP.to_string()],
        None,
    );
    assert_eq!(
        run(&c, tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "forbidden"
    );
    assert!(recorded(&exec).is_empty(), "no slot/invoke was requested");
    assert!(started.elapsed() < Duration::from_secs(1), "no hang");
}

#[tokio::test]
async fn d_cycle_through_a_real_nested_invoke_is_refused_inside_the_target_scope() {
    // APP -> SVC (real nested invoke); while serving it, SVC's guest calls back
    // into APP. The handler registered for the nested invoke carries the grown
    // chain [APP, SVC], so the cycle is refused host-side with no third invoke.
    let exec = fake_executor(Behavior::NestedCallThenReply(serde_json::json!({
        "target": APP, "tool": "x", "args": {}, "idempotency_key": null,
    })))
    .await;
    let out = run(&caps(&exec), tool_call(SVC, "get_balance", None))
        .await
        .unwrap();
    assert_eq!(
        out,
        serde_json::json!({"ok": {"nested": {"error_code": "forbidden"}}})
    );
    assert_eq!(
        recorded(&exec).len(),
        1,
        "only the first hop reached the executor"
    );
}

#[tokio::test]
async fn e_depth_three_is_refused_and_depth_two_runs() {
    let exec = fake_executor(Behavior::Reply(serde_json::json!({"ok": 1}))).await;
    // Chain [X, Y, APP] is already two hops deep: any further call is depth 3.
    let c = caps_with(
        world_rows(),
        default_gate(),
        Arc::clone(&exec.registry),
        vec![
            "waddles.x.y.z".to_string(),
            SVC2.to_string(),
            APP.to_string(),
        ],
        None,
    );
    assert_eq!(
        run(&c, tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "depth_exceeded"
    );
    assert!(recorded(&exec).is_empty());
    // A service calling a service from chain [APP, SVC] is hop 2: allowed.
    let c = caps_with(
        world_rows(),
        default_gate(),
        Arc::clone(&exec.registry),
        vec![APP.to_string(), SVC.to_string()],
        None,
    );
    let mut call = tool_call(SVC2, "ping", None);
    call.app_id = SVC.to_string();
    assert_eq!(run(&c, call).await.unwrap(), serde_json::json!({"ok": 1}));
    assert_eq!(
        recorded(&exec)[0].payload["call_chain"],
        serde_json::json!([APP, SVC, SVC2])
    );
}

#[tokio::test]
async fn f_unknown_stale_or_tampered_target_is_target_unavailable() {
    let exec = fake_executor(Behavior::Error(ErrorCode::DigestMismatch, None)).await;
    // Tampered / digest not matching what the executor has: typed refusal.
    assert_eq!(
        run(&caps(&exec), tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "target_unavailable"
    );
    // Target no longer active in this scope (deactivated / stale snapshot).
    let rows = world_rows()
        .into_iter()
        .filter(|r| r.app_id != SVC)
        .collect();
    let c = caps_with(
        rows,
        default_gate(),
        Arc::clone(&exec.registry),
        vec![APP.to_string()],
        None,
    );
    let before = recorded(&exec).len();
    assert_eq!(
        run(&c, tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "target_unavailable"
    );
    assert_eq!(recorded(&exec).len(), before, "never reached the executor");
}

#[tokio::test]
async fn f_target_not_loaded_on_this_stage_executor_is_a_typed_cross_stage_refusal() {
    // A target the platform has active but THIS stage's executor never loaded
    // (another stage's bundle): fail loud, bounded, no fallback.
    for code in [ErrorCode::UnknownBundle, ErrorCode::ExportMissing] {
        let exec = fake_executor(Behavior::Error(code, None)).await;
        assert_eq!(
            run(&caps(&exec), tool_call(SVC, "get_balance", None))
                .await
                .unwrap_err(),
            "target_unavailable",
            "{code:?}"
        );
    }
    // No executor connection at all.
    let c = caps_with(
        world_rows(),
        default_gate(),
        Arc::new(ConnectionRegistry::new()),
        vec![APP.to_string()],
        None,
    );
    assert_eq!(
        run(&c, tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "target_unavailable"
    );
}

#[tokio::test]
async fn g_invalid_args_are_refused_before_any_executor_traffic() {
    let exec = fake_executor(Behavior::Reply(serde_json::json!({"ok": 1}))).await;
    let c = caps(&exec);
    // Write tool without an idempotency key.
    assert_eq!(
        run(&c, tool_call(SVC, "adjust", None)).await.unwrap_err(),
        "invalid_args"
    );
    // Oversized args.
    let mut big = tool_call(SVC, "get_balance", None);
    big.args["args"] = serde_json::json!({"k": "x".repeat(70_000)});
    assert_eq!(run(&c, big).await.unwrap_err(), "invalid_args");
    // Malformed shapes (non-object args block, bad target / tool names).
    for args in [
        serde_json::json!("not an object"),
        serde_json::json!({"target": "nope", "tool": "t", "args": {}}),
        serde_json::json!({"target": SVC, "tool": "Bad Tool", "args": {}}),
        serde_json::json!({"target": SVC, "tool": "t"}),
    ] {
        let mut call = tool_call(SVC, "get_balance", None);
        call.args = args;
        assert_eq!(run(&c, call).await.unwrap_err(), "invalid_args");
    }
    assert!(recorded(&exec).is_empty());
}

#[tokio::test]
async fn h_a_slow_target_trips_the_host_timeout_not_the_wasm_trap() {
    let exec = fake_executor(Behavior::Never).await;
    let c = caps_with(
        world_rows(),
        default_gate(),
        Arc::clone(&exec.registry),
        vec![APP.to_string()],
        Some(Instant::now() + Duration::from_millis(250)),
    );
    let started = Instant::now();
    assert_eq!(
        run(&c, tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "timeout"
    );
    let took = started.elapsed();
    assert!(
        took >= Duration::from_millis(200) && took < Duration::from_secs(3),
        "{took:?}"
    );
    // The abandoned invoke did not leak its scope: a later call still works
    // on the same connection.
    assert_eq!(
        run(&c, tool_call(SVC2, "ping", None)).await.unwrap_err(),
        "forbidden",
        "caller never requested SVC2:ping -- the stage still serves calls"
    );
}

#[tokio::test]
async fn h_a_spent_outer_budget_is_timeout_without_touching_the_executor() {
    let exec = fake_executor(Behavior::Reply(serde_json::json!({"ok": 1}))).await;
    let c = caps_with(
        world_rows(),
        default_gate(),
        Arc::clone(&exec.registry),
        vec![APP.to_string()],
        Some(Instant::now() - Duration::from_millis(5)),
    );
    assert_eq!(
        run(&c, tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "timeout"
    );
    assert!(recorded(&exec).is_empty());
}

#[tokio::test]
async fn pool_exhaustion_from_the_executor_is_a_typed_pool_exhausted() {
    let exec = fake_executor(Behavior::Error(
        ErrorCode::ExecutorDeadline,
        Some("pool_exhausted"),
    ))
    .await;
    assert_eq!(
        run(&caps(&exec), tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "pool_exhausted"
    );
}

#[tokio::test]
async fn unwired_tool_context_and_unknown_op_fail_loudly() {
    let bare = StageCapabilities::new(
        "acme".to_string(),
        None,
        APP.to_string(),
        TENANT_ID,
        COMMUNITY_ID,
        APP_VERSION,
        test_egress(),
        default_gate(),
    );
    assert_eq!(
        run(&bare, tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "target_unavailable"
    );
    let exec = fake_executor(Behavior::Never).await;
    let mut call = tool_call(SVC, "get_balance", None);
    call.op = "tool.discover".to_string();
    assert_eq!(run(&caps(&exec), call).await.unwrap_err(), "unknown_op");
}

#[tokio::test]
async fn a_malformed_target_envelope_never_becomes_a_success() {
    let exec = fake_executor(Behavior::Reply(serde_json::json!({"surprise": true}))).await;
    assert_eq!(
        run(&caps(&exec), tool_call(SVC, "get_balance", None))
            .await
            .unwrap_err(),
        "target_unavailable"
    );
}

// regression: tenant-isolation -- caller/target facts resolve per tenant scope.
#[tokio::test]
async fn tenant_isolation_cross_tenant_caller_forbidden_target_unavailable_tenant_wide_ok() {
    let exec = fake_executor(Behavior::Reply(serde_json::json!({"ok": 1}))).await;
    let rows = world_rows();
    let run_with = |entries: Vec<(i32, i32, ActiveBundleRow)>| {
        let c = caps_scoped(
            entries,
            default_gate(),
            Arc::clone(&exec.registry),
            vec![APP.to_string()],
            None,
        );
        async move { run(&c, tool_call(SVC, "get_balance", None)).await }
    };

    // (a) caller installed only in ANOTHER tenant -> forbidden.
    let e = rows
        .iter()
        .cloned()
        .map(|r| (if r.app_id == APP { 8 } else { TENANT_ID }, COMMUNITY_ID, r))
        .collect();
    assert_eq!(run_with(e).await.unwrap_err(), "forbidden");

    // (b) target installed only in ANOTHER tenant -> target_unavailable.
    let e = rows
        .iter()
        .cloned()
        .map(|r| (if r.app_id == SVC { 8 } else { TENANT_ID }, COMMUNITY_ID, r))
        .collect();
    assert_eq!(run_with(e).await.unwrap_err(), "target_unavailable");
    assert!(recorded(&exec).is_empty(), "never reached the executor");

    // (c) same-tenant happy path.
    let e = rows
        .iter()
        .cloned()
        .map(|r| (TENANT_ID, COMMUNITY_ID, r))
        .collect();
    assert!(run_with(e).await.is_ok());

    // (d) tenant-wide (community 0) service reachable from a community call.
    let e = rows
        .iter()
        .cloned()
        .map(|r| (TENANT_ID, if r.app_id == SVC { 0 } else { COMMUNITY_ID }, r))
        .collect();
    assert!(run_with(e).await.is_ok());
}
