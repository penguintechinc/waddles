//! Issue #726 foundation proof: a REAL compiled `world stage-next` component
//! (`tests/fixtures/reputation_fixture.wasm`) loads into the executor's
//! production `Linker`, CALLS the new `reputation` host import, and the call
//! crosses the real wire bridge to the stage as `capability = db`,
//! `op = "reputation.*"` with the exact args shape `core/svc_process`
//! decodes. Only the stage half of the connection is simulated (it answers
//! with a canned host-result or a gate-style denial); every executor-side
//! piece -- component instantiation against the stage-next imports, the
//! `reputation::Host` impl, the frame codec -- is the real code.
//!
//! The same fixture is also loaded against a linker built for
//! `WitWorld::Stage` to prove the isolation direction: a `stage` linker
//! (`build_linker_for` + `WitWorld::Stage`) refuses a component importing
//! `reputation` ("unknown import") before any guest code runs.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bundle_executor::config::CliConfig;
use bundle_executor::engine::{build_engine, build_linker_for};
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::{ComponentSource, Executor};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use bundle_executor::wire::run_connection;
use penguin_bundle_host::wire::{
    read_frame, write_frame, CapabilityKind, ExportKind, Frame, HelloBody, HelloLimits,
    HelloOkBody, HostResultBody, HostResultError, InvokeBody, LoadBody, LoadLimits, Message,
    SandboxInfo, ShutdownBody,
};
use sha2::{Digest, Sha256};

const FIXTURE_WASM: &[u8] = include_bytes!("fixtures/reputation_fixture.wasm");
const APP_ID: &str = "waddles.test.reputation-fixture";
const TARGET_USER: &str = "11111111-2222-4333-8444-555555555555";

struct FixtureSource;

impl ComponentSource for FixtureSource {
    async fn fetch(
        &self,
        _component_key: &str,
        _sidecar_key: &str,
    ) -> Result<(Vec<u8>, Vec<u8>), bundle_executor::error::ExecutorError> {
        Ok((FIXTURE_WASM.to_vec(), b"{}".to_vec()))
    }
}

fn fixture_digest() -> String {
    let mut hasher = Sha256::new();
    hasher.update(FIXTURE_WASM);
    format!("sha256:{:x}", hasher.finalize())
}

fn test_config() -> CliConfig {
    use clap::Parser;
    CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-process:8301",
    ])
    .expect("static test args always parse")
}

/// What the simulated stage answers to the one `reputation.*` host-call.
enum StageAnswer {
    Ok(serde_json::Value),
    Err(&'static str, &'static str),
}

/// Runs load + one `transform` invoke of `event_type`/`payload_json` against
/// the fixture, answering the single expected host-call with `answer`.
/// Returns `(observed host-call op, observed host-call args, guest result)`.
async fn run_one(
    event_type: &'static str,
    payload_json: String,
    answer: StageAnswer,
) -> (String, serde_json::Value, String) {
    let (executor_io, stage_io) = tokio::io::duplex(256 * 1024);
    let executor = Arc::new(Executor::new(&test_config(), FixtureSource).expect("executor builds"));
    let digest = fixture_digest();

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
                        call_timeout_ms: 2000,
                        memory_mb: 64,
                        max_concurrent_calls: 32,
                    },
                }),
            ),
        )
        .await
        .expect("write hello-ok");

        write_frame(
            &mut io,
            &Frame::new(
                1,
                Message::Load(LoadBody {
                    tenant_id: 1,
                    community_id: 7,
                    app_id: APP_ID.to_string(),
                    version: "1".to_string(),
                    digest: digest.clone(),
                    component_key: "k".to_string(),
                    sidecar_key: "s".to_string(),
                    capabilities: vec![],
                    limits: LoadLimits {
                        timeout_ms: 2000,
                        memory_mb: 64,
                    },
                }),
            ),
        )
        .await
        .expect("write load");
        let loaded = read_frame(&mut io).await.expect("loaded reply");
        assert!(
            matches!(loaded.message, Message::Loaded(_)),
            "stage-next component must load, got {:?}",
            loaded.message
        );

        write_frame(
            &mut io,
            &Frame::new(
                2,
                Message::Invoke(InvokeBody {
                    app_id: APP_ID.to_string(),
                    digest,
                    export: ExportKind::Transform,
                    payload: serde_json::json!({
                        "platform": "test",
                        "event_type": event_type,
                        "actor": null,
                        "payload_json": payload_json,
                        "occurred_at": "2026-10-09T00:00:00.000Z",
                    }),
                    deadline_ms: 5000,
                    trace: None,
                }),
            ),
        )
        .await
        .expect("write invoke");

        let call_frame = read_frame(&mut io).await.expect("host-call frame");
        let Message::HostCall(call) = call_frame.message else {
            panic!("expected host-call, got {:?}", call_frame.message);
        };
        assert_eq!(
            call.capability,
            CapabilityKind::Db,
            "reputation rides the db wire kind"
        );
        let body = match answer {
            StageAnswer::Ok(v) => HostResultBody {
                result: Some(v),
                error: None,
            },
            StageAnswer::Err(code, message) => HostResultBody {
                result: None,
                error: Some(HostResultError {
                    code: code.to_string(),
                    message: message.to_string(),
                }),
            },
        };
        write_frame(
            &mut io,
            &Frame::new(call_frame.id, Message::HostResult(body)),
        )
        .await
        .expect("write host-result");

        let result = read_frame(&mut io).await.expect("transform result");
        let Message::Result(result_body) = result.message else {
            panic!("expected result, got {:?}", result.message);
        };

        write_frame(
            &mut io,
            &Frame::new(3, Message::Shutdown(ShutdownBody { grace_ms: 100 })),
        )
        .await
        .expect("write shutdown");
        (call.op, call.args, result_body.payload)
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

    let (op, args, payload) = stage.await.expect("stage task");
    executor_task
        .await
        .expect("executor task")
        .expect("connection ran to a clean shutdown");
    let payload_json = payload["payload_json"]
        .as_str()
        .expect("payload_json is a string");
    let parsed: serde_json::Value = serde_json::from_str(payload_json).expect("guest result json");
    (
        op,
        args,
        parsed["result"].as_str().expect("result tag").to_string(),
    )
}

#[tokio::test]
async fn get_crosses_the_bridge_and_returns_the_balance() {
    let (op, args, result) = run_one(
        "rep-get",
        format!(r#"{{"user":"{TARGET_USER}"}}"#),
        StageAnswer::Ok(serde_json::json!({ "balance": 42 })),
    )
    .await;
    assert_eq!(op, "reputation.get");
    assert_eq!(args, serde_json::json!({ "user": TARGET_USER }));
    assert_eq!(result, "ok:42");
}

#[tokio::test]
async fn adjust_crosses_the_bridge_with_user_delta_reason_and_returns_new_balance() {
    let (op, args, result) = run_one(
        "rep-adjust",
        format!(r#"{{"user":"{TARGET_USER}","delta":-5,"reason":"game.loss"}}"#),
        StageAnswer::Ok(serde_json::json!({ "balance": 37 })),
    )
    .await;
    assert_eq!(op, "reputation.adjust");
    assert_eq!(
        args,
        serde_json::json!({ "user": TARGET_USER, "delta": -5, "reason": "game.loss" })
    );
    assert_eq!(result, "ok:37");
}

/// Fail-loud on gate denial: the stage's gate code reaches the guest as a
/// typed `denied(<code>)`, never a swallowed default balance.
#[tokio::test]
async fn gate_denial_surfaces_as_a_typed_denied_error() {
    let (_, _, result) = run_one(
        "rep-adjust",
        format!(r#"{{"user":"{TARGET_USER}","delta":999,"reason":"x"}}"#),
        StageAnswer::Err("delta_out_of_bounds", "delta outside declared bounds"),
    )
    .await;
    assert_eq!(result, "denied:delta_out_of_bounds");

    let (_, _, result) = run_one(
        "rep-get",
        format!(r#"{{"user":"{TARGET_USER}"}}"#),
        StageAnswer::Err("not_granted", "reputation.read not granted"),
    )
    .await;
    assert_eq!(result, "denied:not_granted");
}

#[tokio::test]
async fn membership_and_cap_and_unavailable_errors_map_to_their_variants() {
    let cases: [(&'static str, &'static str, &str); 4] = [
        ("user_not_in_scope", "gate", "not-a-member"),
        ("not_a_member", "store", "not-a-member"),
        ("daily_cap_exceeded", "store", "daily-cap-exceeded"),
        ("not_implemented", "wiring", "unavailable:wiring"),
    ];
    for (code, message, expected) in cases {
        let (_, _, result) = run_one(
            "rep-adjust",
            format!(r#"{{"user":"{TARGET_USER}","delta":1,"reason":"r"}}"#),
            StageAnswer::Err(code, message),
        )
        .await;
        assert_eq!(result, expected, "stage code {code}");
    }
}

/// Isolation direction: a linker built for `WitWorld::Stage` does not
/// register `reputation`, so the stage-next fixture fails to instantiate
/// ("unknown import") before any guest code runs; the `StageNext` linker
/// accepts it.
#[tokio::test]
async fn stage_world_linker_refuses_a_component_importing_reputation() {
    let engine = build_engine(&test_config()).expect("engine");
    let component = wasmtime::component::Component::new(&engine, FIXTURE_WASM).expect("component");

    let stage_manifest =
        VerifiedManifest::new(APP_ID, "sha256:aa", WitWorld::Stage, Default::default());
    let stage_linker = build_linker_for(&engine, &stage_manifest).expect("stage linker");
    assert!(
        stage_linker.instantiate_pre(&component).is_err(),
        "a stage-1.0.0 linker must not satisfy the reputation import"
    );

    let next_manifest =
        VerifiedManifest::new(APP_ID, "sha256:bb", WitWorld::StageNext, Default::default());
    let next_linker = build_linker_for(&engine, &next_manifest).expect("stage-next linker");
    assert!(
        next_linker.instantiate_pre(&component).is_ok(),
        "a stage-next linker must satisfy the reputation import"
    );
}
