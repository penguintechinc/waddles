//! Proof (numeric-scope contract, `world stage-v2`): the executor serves BOTH
//! WIT families on ONE registry/linker --
//!
//! * a REAL compiled v1 `stage` component (`hostile_fixture.wasm`, built
//!   before `stage-v2` existed) still loads and runs untouched, and its v1
//!   `context.get-context` ignores/keeps ignoring the new numeric fields; and
//! * a REAL compiled `stage-v2` component (`stage_v2_fixture.wasm`) receives
//!   the AUTHORITATIVE numeric scope through `context-v2.get-context` and
//!   `action-stage-v2.dispatch`, and the host FAILS CLOSED when the stage does
//!   not supply a valid numeric scope (`tenant-id == 0` / malformed frame).
//!
//! Only the stage half of the connection is simulated; the executor, wasmtime,
//! the production `Linker` and the frame codec are real.

#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use std::collections::HashMap;
use std::sync::Arc;

use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::{ComponentSource, Executor};
use bundle_executor::wire::run_connection;
use common::{digest_of, test_config};
use penguin_bundle_host::wire::{
    read_frame, write_frame, CapabilityKind, ErrorCode, ExportKind, Frame, HelloBody, HelloLimits,
    HelloOkBody, HostResultBody, InvokeBody, LoadBody, LoadLimits, Message, SandboxInfo,
    ShutdownBody,
};

const V1_WASM: &[u8] = include_bytes!("fixtures/hostile_fixture.wasm");
const V2_WASM: &[u8] = include_bytes!("fixtures/stage_v2_fixture.wasm");
const V1_APP: &str = "waddles.test.hostile-fixture";
const V2_APP: &str = "waddles.test.stage-v2-fixture";

/// Serves each bundle's bytes by its `component_key` (== its `app_id` here).
struct MultiSource(HashMap<String, Vec<u8>>);

impl ComponentSource for MultiSource {
    async fn fetch(
        &self,
        component_key: &str,
        _sidecar_key: &str,
    ) -> Result<(Vec<u8>, Vec<u8>), bundle_executor::error::ExecutorError> {
        Ok((self.0[component_key].clone(), b"{}".to_vec()))
    }
}

/// One invoke the simulated stage drives.
struct Step {
    app_id: &'static str,
    wasm: &'static [u8],
    export: ExportKind,
    payload: serde_json::Value,
    /// The JSON the stage answers the bundle's `get-context` host-call with.
    context: serde_json::Value,
    /// The `(tenant_id, community_id)` the bundle is `Load`ed under.
    load_scope: (i32, i32),
}

/// A step's outcome: the result payload, or the protocol error code+message.
type Outcome = Result<serde_json::Value, (ErrorCode, String)>;

/// Loads every distinct bundle named by `steps` onto one connection, then runs
/// each step in order, answering `get-context` with the step's `context`.
async fn drive(steps: Vec<Step>) -> Vec<Outcome> {
    let mut sources = HashMap::new();
    for s in &steps {
        sources.insert(s.app_id.to_string(), s.wasm.to_vec());
    }
    let (executor_io, stage_io) = tokio::io::duplex(256 * 1024);
    let executor = Arc::new(Executor::new(&test_config(), MultiSource(sources)).expect("executor"));

    let stage = tokio::spawn(async move {
        let mut io = stage_io;
        let hello = read_frame(&mut io).await.expect("hello");
        write_frame(
            &mut io,
            &Frame::new(
                hello.id,
                Message::HelloOk(HelloOkBody {
                    stage: "svc-process".to_string(),
                    protocol_version: 1,
                    limits: HelloLimits {
                        call_timeout_ms: 5000,
                        memory_mb: 64,
                        max_concurrent_calls: 32,
                    },
                }),
            ),
        )
        .await
        .expect("hello-ok");

        let mut loaded: Vec<&'static str> = Vec::new();
        let mut next_id = 1u64;
        for s in &steps {
            if !loaded.contains(&s.app_id) {
                loaded.push(s.app_id);
                write_frame(
                    &mut io,
                    &Frame::new(
                        next_id,
                        Message::Load(LoadBody {
                            tenant_id: s.load_scope.0,
                            community_id: s.load_scope.1,
                            app_id: s.app_id.to_string(),
                            version: "1".to_string(),
                            digest: digest_of(s.wasm),
                            component_key: s.app_id.to_string(),
                            sidecar_key: "s".to_string(),
                            capabilities: vec![],
                            limits: LoadLimits {
                                timeout_ms: 5000,
                                memory_mb: 64,
                            },
                        }),
                    ),
                )
                .await
                .expect("load");
                next_id += 1;
                let reply = read_frame(&mut io).await.expect("loaded reply");
                assert!(
                    matches!(reply.message, Message::Loaded(_)),
                    "{} must load, got {:?}",
                    s.app_id,
                    reply.message
                );
            }
        }

        let mut out = Vec::new();
        for s in steps {
            write_frame(
                &mut io,
                &Frame::new(
                    next_id,
                    Message::Invoke(InvokeBody {
                        app_id: s.app_id.to_string(),
                        digest: digest_of(s.wasm),
                        export: s.export,
                        payload: s.payload.clone(),
                        deadline_ms: 30000,
                        trace: None,
                    }),
                ),
            )
            .await
            .expect("invoke");
            next_id += 1;
            loop {
                let frame = read_frame(&mut io).await.expect("frame");
                match frame.message {
                    Message::Result(body) => {
                        out.push(Ok(body.payload));
                        break;
                    }
                    Message::Error(e) => {
                        out.push(Err((e.code, e.message)));
                        break;
                    }
                    Message::HostCall(call) => {
                        assert_eq!(call.capability, CapabilityKind::Context);
                        write_frame(
                            &mut io,
                            &Frame::new(
                                frame.id,
                                Message::HostResult(HostResultBody {
                                    result: Some(s.context.clone()),
                                    error: None,
                                }),
                            ),
                        )
                        .await
                        .expect("host-result");
                    }
                    other => panic!("unexpected frame {other:?}"),
                }
            }
        }
        write_frame(
            &mut io,
            &Frame::new(9999, Message::Shutdown(ShutdownBody { grace_ms: 100 })),
        )
        .await
        .expect("shutdown");
        out
    });

    let executor_task = tokio::spawn(async move {
        run_connection(
            executor_io,
            HelloBody {
                protocol_version: 1,
                executor_version: "0.1.0".to_string(),
                wasmtime_version: bundle_executor::engine::WASMTIME_VERSION.to_string(),
                wasmtime_abi: bundle_executor::engine::WASMTIME_VERSION.to_string(),
                collector: "drc".to_string(),
                sandbox: SandboxInfo {
                    runtime: "runc".to_string(),
                    verified: false,
                },
            },
            Arc::clone(&executor),
            "test-peer",
            Heartbeat::disabled(),
        )
        .await
    });
    let out = stage.await.expect("stage task");
    executor_task
        .await
        .expect("executor task")
        .expect("clean shutdown");
    out
}

fn event_payload(event_type: &str) -> serde_json::Value {
    serde_json::json!({
        "platform": "test",
        "event_type": event_type,
        "actor": null,
        "payload_json": "{}",
        "occurred_at": "2026-10-14T00:00:00.000Z",
    })
}

fn transform_step(
    app: &'static str,
    wasm: &'static [u8],
    ev: &str,
    ctx: serde_json::Value,
) -> Step {
    Step {
        app_id: app,
        wasm,
        export: ExportKind::Transform,
        payload: event_payload(ev),
        context: ctx,
        load_scope: (10, 7),
    }
}

fn v2_envelope(extra: serde_json::Value) -> serde_json::Value {
    let mut env = serde_json::json!({
        "tenant": "acme",
        "community": "main",
        "app_id": V2_APP,
        "stage": "action",
        "event": event_payload("probe"),
        "ts": "2026-10-14T00:00:00.000Z",
        "target_app_id": null,
        "trace_context": null,
    });
    for (k, v) in extra.as_object().unwrap() {
        env[k] = v.clone();
    }
    serde_json::json!({ "envelope": env, "config": "{}" })
}

fn dispatch_step(payload: serde_json::Value) -> Step {
    Step {
        app_id: V2_APP,
        wasm: V2_WASM,
        export: ExportKind::Dispatch,
        payload,
        context: serde_json::json!({}),
        load_scope: (10, 7),
    }
}

fn ctx_with_ids(extra: serde_json::Value) -> serde_json::Value {
    let mut ctx = serde_json::json!({
        "tenant": "acme", "community": "main", "app_id": V2_APP,
        "feature": "f", "version": "1", "message_id": "m1", "config_json": "{}",
    });
    for (k, v) in extra.as_object().unwrap() {
        ctx[k] = v.clone();
    }
    ctx
}

fn result_tag(outcome: &Outcome) -> String {
    let payload = outcome.as_ref().expect("invoke result");
    let json: serde_json::Value =
        serde_json::from_str(payload["payload_json"].as_str().expect("payload_json")).unwrap();
    json["result"].as_str().expect("result tag").to_string()
}

/// A v1 bundle and a v2 bundle live side by side on one executor; the v1
/// bundle is untouched by the numeric fields even when the stage sends them.
#[tokio::test]
async fn v1_and_v2_bundles_coexist_and_v1_ignores_numeric_scope() {
    let v1_ctx =
        ctx_with_ids(serde_json::json!({ "tenant_id": 10, "community_id": 7, "app_id": V1_APP }));
    let out = drive(vec![
        transform_step(V1_APP, V1_WASM, "get-context", v1_ctx.clone()),
        transform_step(
            V2_APP,
            V2_WASM,
            "scope",
            ctx_with_ids(serde_json::json!({ "tenant_id": 10, "community_id": 7 })),
        ),
        // v1 again AFTER a v2 load: the shared registry/linker still serves it.
        transform_step(V1_APP, V1_WASM, "get-context", v1_ctx),
    ])
    .await;

    for i in [0, 2] {
        let payload = out[i].as_ref().expect("v1 result");
        let json: serde_json::Value =
            serde_json::from_str(payload["payload_json"].as_str().unwrap()).unwrap();
        assert_eq!(json["tenant"], "acme", "v1 get-context still string-scoped");
        assert_eq!(json["app_id"], V1_APP);
        assert!(json.get("tenant_id").is_none(), "v1 never sees numeric ids");
    }
    assert_eq!(result_tag(&out[1]), "10:7:acme:main");
}

/// `context-v2` yields the authoritative ids; tenant-wide is community 0.
#[tokio::test]
async fn context_v2_supplies_the_authoritative_numeric_scope() {
    let out = drive(vec![Step {
        load_scope: (20, 0),
        ..transform_step(
            V2_APP,
            V2_WASM,
            "scope",
            ctx_with_ids(
                serde_json::json!({ "tenant_id": 20, "community_id": 0, "community": null }),
            ),
        )
    }])
    .await;
    assert_eq!(result_tag(&out[0]), "20:0:acme:");
    let out = drive(vec![
        // display strings disagreeing with the ids must NOT influence the ids
        transform_step(
            V2_APP,
            V2_WASM,
            "scope",
            ctx_with_ids(
                serde_json::json!({ "tenant": "evil", "tenant_id": 10, "community_id": 7 }),
            ),
        ),
    ])
    .await;
    assert_eq!(result_tag(&out[0]), "10:7:evil:main");
}

/// Fail closed: a stage that did not supply a valid numeric scope yields
/// `tenant-id == 0` ("no scope"), never an id derived from the strings.
#[tokio::test]
async fn context_v2_fails_closed_without_a_valid_numeric_scope() {
    let cases = [
        serde_json::json!({}),                    // stage predates numeric scope
        serde_json::json!({ "tenant_id": 10 }),   // community_id missing
        serde_json::json!({ "community_id": 7 }), // tenant_id missing
        serde_json::json!({ "tenant_id": 0, "community_id": 7 }), // tenant 0 is invalid
        serde_json::json!({ "tenant_id": -3, "community_id": 7 }), // negative tenant
        serde_json::json!({ "tenant_id": 10, "community_id": -1 }), // negative community
        serde_json::json!({ "tenant_id": "10", "community_id": 7 }), // wrong type
    ];
    let steps = cases
        .iter()
        .map(|c| transform_step(V2_APP, V2_WASM, "scope", ctx_with_ids(c.clone())))
        .collect();
    let out = drive(steps).await;
    for (i, o) in out.iter().enumerate() {
        assert_eq!(result_tag(o), "no-scope", "case {i}: {}", cases[i]);
    }
}

/// `action-stage-v2.dispatch` carries the numeric scope to the guest intact.
#[tokio::test]
async fn dispatch_v2_carries_numeric_scope_to_the_guest() {
    let out = drive(vec![dispatch_step(v2_envelope(
        serde_json::json!({ "tenant_id": 10, "community_id": 7 }),
    ))])
    .await;
    let r0 = out[0].as_ref().expect("dispatch result");
    assert_eq!(r0["provider_message_id"], "10:7:acme:main");
}

/// Confused-deputy guard: a frame whose ids differ from the loaded scope
/// (other tenant, other community, tenant-wide vs community) is
/// MALFORMED_FRAME; the matching frame still runs afterwards.
#[tokio::test]
async fn dispatch_v2_rejects_frame_scope_not_matching_loaded_scope() {
    let out = drive(vec![
        dispatch_step(v2_envelope(
            serde_json::json!({ "tenant_id": 11, "community_id": 7 }),
        )),
        dispatch_step(v2_envelope(
            serde_json::json!({ "tenant_id": 10, "community_id": 8 }),
        )),
        dispatch_step(v2_envelope(
            serde_json::json!({ "tenant_id": 10, "community_id": 0 }),
        )),
        dispatch_step(v2_envelope(
            serde_json::json!({ "tenant_id": 10, "community_id": 7 }),
        )),
    ])
    .await;
    for i in 0..3 {
        match &out[i] {
            Err((code, msg)) => {
                assert_eq!(*code, ErrorCode::MalformedFrame, "case {i}");
                assert!(msg.contains("does not match"), "case {i}: {msg}");
            }
            Ok(p) => panic!("case {i} must be rejected, got {p}"),
        }
    }
    assert_eq!(
        out[3].as_ref().expect("matching frame runs")["provider_message_id"],
        "10:7:acme:main"
    );
}

/// An id above i32::MAX (valid v3 i64) can never match the i32 Load scope:
/// rejected, even when the bundle is loaded for the truncated value.
#[tokio::test]
async fn dispatch_v2_rejects_tenant_id_above_i32_max() {
    let big = i64::from(i32::MAX) + 1;
    let wrapped = vec![
        Step {
            load_scope: (i32::MAX, 0),
            ..dispatch_step(v2_envelope(
                serde_json::json!({ "tenant_id": big, "community_id": 0 }),
            ))
        },
        // exactly i32::MAX is the largest accepted id
        Step {
            load_scope: (i32::MAX, 0),
            ..dispatch_step(v2_envelope(
                serde_json::json!({ "tenant_id": i32::MAX, "community_id": 0 }),
            ))
        },
    ];
    let out = drive(wrapped).await;
    assert!(
        matches!(&out[0], Err((ErrorCode::MalformedFrame, _))),
        "{:?}",
        out[0]
    );
    assert_eq!(
        out[1].as_ref().expect("i32::MAX accepted")["provider_message_id"],
        "2147483647:0:acme:main"
    );
}

/// Unified id source: `get-context-v2` must not hand the guest a scope other
/// than a loaded one -- a host-call reply for another tenant fails closed.
#[tokio::test]
async fn context_v2_rejects_scope_not_loaded() {
    let out = drive(vec![
        transform_step(
            V2_APP,
            V2_WASM,
            "scope",
            ctx_with_ids(serde_json::json!({ "tenant_id": 11, "community_id": 7 })),
        ),
        transform_step(
            V2_APP,
            V2_WASM,
            "scope",
            ctx_with_ids(serde_json::json!({ "tenant_id": 9_000_000_000_i64, "community_id": 7 })),
        ),
    ])
    .await;
    assert_eq!(result_tag(&out[0]), "no-scope");
    assert_eq!(result_tag(&out[1]), "no-scope");
}

/// A v2 dispatch frame without (or with out-of-range) numeric scope is a
/// MALFORMED_FRAME: the guest is never called with a fabricated scope.
#[tokio::test]
async fn dispatch_v2_rejects_missing_or_out_of_range_scope() {
    let out = drive(vec![
        dispatch_step(v2_envelope(serde_json::json!({}))), // a v1-shaped envelope
        dispatch_step(v2_envelope(serde_json::json!({ "tenant_id": 10 }))),
        dispatch_step(v2_envelope(
            serde_json::json!({ "tenant_id": 0, "community_id": 0 }),
        )),
        dispatch_step(v2_envelope(
            serde_json::json!({ "tenant_id": -1, "community_id": 0 }),
        )),
        dispatch_step(v2_envelope(
            serde_json::json!({ "tenant_id": 10, "community_id": -1 }),
        )),
        dispatch_step(v2_envelope(
            serde_json::json!({ "tenant_id": 10, "community_id": 3_000_000_000_i64 }),
        )),
        dispatch_step(v2_envelope(
            serde_json::json!({ "tenant_id": "10", "community_id": 0 }),
        )),
    ])
    .await;
    for (i, o) in out.iter().enumerate() {
        match o {
            Err((code, _)) => assert_eq!(*code, ErrorCode::MalformedFrame, "case {i}"),
            Ok(p) => panic!("case {i} must be rejected, got result {p}"),
        }
    }
}
