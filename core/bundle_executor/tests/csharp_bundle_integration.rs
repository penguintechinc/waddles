//! C#-toolchain feasibility spike (spec
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` S18 R13 /
//! D35): end-to-end proof that a REAL `componentize-dotnet`-built WASI 0.2
//! component -- `tests/fixtures/csping.wasm`, the published output of
//! `bundles/csharp/csping`, not a hand-rolled stand-in (task instruction
//! "never fake a host call") -- loads and runs through this executor's
//! genuine wasmtime `Engine`/`Linker` (`crate::engine::build_linker`,
//! the SAME linker every Rust- and Python-built bundle instantiates
//! against) exactly like `tests/host_bridge_integration.rs` proves for a
//! Rust-built component.
//!
//! Drives one `load` + one `process-stage.transform` invoke (`!csping` ->
//! a `pong (c#)` reply, zero host calls -- the C# impl only touches the
//! JSON payload, mirroring `bundles/rust/ping`'s zero-host-call
//! `transform`) + two `action-stage.dispatch` invokes, one per platform,
//! each expecting exactly one host call (`relay.push`) -- proving the
//! relay provider always tracks `envelope.event.platform` rather than a
//! constant baked into the C# build, the same regression
//! `bundles/rust/ping`'s own unit test
//! (`dispatch_relays_to_the_events_own_origin_platform_not_a_hardcoded_one`)
//! guards at the Rust-source level, exercised here at the compiled-
//! component level instead.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bundle_executor::config::CliConfig;
use bundle_executor::invoke::{ComponentSource, Executor};
use bundle_executor::wire::run_connection;
use penguin_bundle_host::wire::{
    read_frame, write_frame, CapabilityKind, ExportKind, Frame, HelloBody, HelloLimits,
    HelloOkBody, HostResultBody, InvokeBody, LoadBody, LoadLimits, Message, SandboxInfo,
    ShutdownBody,
};
use sha2::{Digest, Sha256};

const FIXTURE_WASM: &[u8] = include_bytes!("fixtures/csping.wasm");
const APP_ID: &str = "waddles.core.example.csping";

/// Hands back the committed `csping.wasm` bytes regardless of key -- there
/// is exactly one bundle in this test, same pattern as
/// `host_bridge_integration.rs`'s `FixtureSource`.
struct FixtureSource;

impl ComponentSource for FixtureSource {
    async fn fetch(
        &self,
        _component_key: &str,
        _sidecar_key: &str,
    ) -> Result<Vec<u8>, bundle_executor::error::ExecutorError> {
        Ok(FIXTURE_WASM.to_vec())
    }
}

fn fixture_digest() -> String {
    let mut hasher = Sha256::new();
    hasher.update(FIXTURE_WASM);
    format!("sha256:{:x}", hasher.finalize())
}

fn test_config() -> CliConfig {
    use clap::Parser;
    #[allow(clippy::expect_used)]
    CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-process:8301",
    ])
    .expect("static test args always parse")
}

fn hello_body() -> HelloBody {
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
    }
}

/// Answers exactly one `host-call` frame with the canned `relay.push`
/// result -- the only WIT host import `bundles/csharp/csping`'s
/// `ActionStageExportsImpl.Dispatch` calls (mirrors
/// `host_bridge_integration.rs::answer_one_host_call`, narrowed to the
/// one capability this bundle actually uses).
async fn answer_relay_push<S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin>(io: &mut S) {
    let frame = read_frame(io).await.expect("expected a host-call frame");
    let Message::HostCall(call) = frame.message else {
        panic!("expected host-call, got {:?}", frame.message);
    };
    assert_eq!(call.capability, CapabilityKind::Relay);
    assert_eq!(call.op.as_str(), "push");
    write_frame(
        io,
        &Frame::new(
            frame.id,
            Message::HostResult(HostResultBody {
                result: Some(serde_json::json!({})),
                error: None,
            }),
        ),
    )
    .await
    .expect("write host-result");
}

#[tokio::test]
async fn csharp_component_loads_and_runs_transform_and_dispatch_through_the_real_executor() {
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
                        call_timeout_ms: 5000,
                        memory_mb: 64,
                        max_concurrent_calls: 32,
                    },
                }),
            ),
        )
        .await
        .expect("write hello-ok");

        let load_start = std::time::Instant::now();
        write_frame(
            &mut io,
            &Frame::new(
                1000,
                Message::Load(LoadBody {
                    app_id: APP_ID.to_string(),
                    version: "1".to_string(),
                    digest: digest.clone(),
                    component_key: "bundles/csping/1/csping.wasm".to_string(),
                    sidecar_key: "bundles/csping/1/csping.json".to_string(),
                    capabilities: vec![],
                    limits: LoadLimits {
                        timeout_ms: 5000,
                        memory_mb: 64,
                    },
                }),
            ),
        )
        .await
        .expect("write load");
        let loaded = read_frame(&mut io).await.expect("loaded reply");
        let Message::Loaded(loaded_body) = loaded.message else {
            panic!("expected loaded, got {:?}", loaded.message);
        };
        assert_eq!(loaded_body.digest, digest);
        let instantiation_ms = load_start.elapsed().as_millis();
        eprintln!("csping.wasm: load (compile) took {instantiation_ms}ms");

        // process-stage.transform("!csping") -> zero host calls, a
        // `pong (c#)` reply on the same platform/channel.
        let payload_json = serde_json::to_string(&serde_json::json!({
            "text": "!csping",
            "channel_id": "12345",
        }))
        .unwrap();
        let invoke_start = std::time::Instant::now();
        write_frame(
            &mut io,
            &Frame::new(
                1001,
                Message::Invoke(InvokeBody {
                    app_id: APP_ID.to_string(),
                    digest: digest.clone(),
                    export: ExportKind::Transform,
                    payload: serde_json::json!({
                        "platform": "twitch",
                        "event_type": "chat.message",
                        "actor": "viewer-1",
                        "payload_json": payload_json,
                        "occurred_at": "2026-09-27T00:00:00.000Z",
                    }),
                    deadline_ms: 5000,
                    trace: None,
                }),
            ),
        )
        .await
        .expect("write invoke transform");
        let transform_result = read_frame(&mut io).await.expect("transform result");
        let first_invoke_ms = invoke_start.elapsed().as_millis();
        eprintln!(
            "csping.wasm: first transform invoke took {first_invoke_ms}ms (includes instantiation)"
        );
        let Message::Result(transform_body) = transform_result.message else {
            panic!("expected result, got {:?}", transform_result.message);
        };

        // action-stage.dispatch(...) once per platform -> exactly one
        // host call each (relay.push), proving the provider always
        // tracks the envelope's own platform.
        let mut dispatch_payloads = Vec::new();
        for (invoke_id, platform) in [(1002u64, "twitch"), (1003u64, "discord")] {
            let reply_payload_json = serde_json::to_string(&serde_json::json!({
                "text": "pong (c#)",
                "channel_id": "12345",
            }))
            .unwrap();
            write_frame(
                &mut io,
                &Frame::new(
                    invoke_id,
                    Message::Invoke(InvokeBody {
                        app_id: APP_ID.to_string(),
                        digest: digest.clone(),
                        export: ExportKind::Dispatch,
                        payload: serde_json::json!({
                            "envelope": {
                                "tenant": "t1",
                                "community": null,
                                "app_id": APP_ID,
                                "stage": "action",
                                "event": {
                                    "platform": platform,
                                    "event_type": "chat.message",
                                    "actor": "viewer-1",
                                    "payload_json": reply_payload_json,
                                    "occurred_at": "2026-09-27T00:00:00.000Z",
                                },
                                "ts": "2026-09-27T00:00:00.000Z",
                                "target_app_id": null,
                                "trace_context": null,
                            },
                            "config": "{}",
                        }),
                        deadline_ms: 5000,
                        trace: None,
                    }),
                ),
            )
            .await
            .expect("write invoke dispatch");
            answer_relay_push(&mut io).await;
            let dispatch_result = read_frame(&mut io).await.expect("dispatch result");
            let Message::Result(dispatch_body) = dispatch_result.message else {
                panic!("expected result, got {:?}", dispatch_result.message);
            };
            dispatch_payloads.push((platform, dispatch_body.payload));
        }

        write_frame(
            &mut io,
            &Frame::new(1004, Message::Shutdown(ShutdownBody { grace_ms: 100 })),
        )
        .await
        .expect("write shutdown");

        (transform_body.payload, dispatch_payloads)
    });

    let executor_task = tokio::spawn(async move {
        run_connection(executor_io, hello_body(), Arc::clone(&executor)).await
    });

    let (transform_payload, dispatch_payloads) = stage.await.expect("stage task");
    executor_task
        .await
        .expect("executor task")
        .expect("connection ran to a clean shutdown");

    // `transform` matched `!csping` and produced a `pong (c#)` reply on
    // the SAME platform/channel the inbound event carried -- proving the
    // C#-built component's `process-stage.transform` export really ran
    // (not a stub/no-op) and its JSON-payload handling round-tripped
    // correctly across the wasmtime component boundary.
    assert_eq!(transform_payload["platform"], serde_json::json!("twitch"));
    assert_eq!(
        transform_payload["event_type"],
        serde_json::json!("chat.message")
    );
    let reply_payload_json = transform_payload["payload_json"]
        .as_str()
        .expect("payload_json is a string");
    let reply: serde_json::Value = serde_json::from_str(reply_payload_json).unwrap();
    assert_eq!(reply["text"], serde_json::json!("pong (c#)"));
    assert_eq!(reply["channel_id"], serde_json::json!("12345"));

    // `dispatch` relayed to each event's OWN origin platform, never a
    // hardcoded one -- the same non-negotiable `bundles/rust/ping`'s own
    // regression test guards, proven here against the compiled C#
    // component via a REAL `relay.push` host call each time (not
    // skipped/short-circuited).
    for (platform, payload) in dispatch_payloads {
        assert_eq!(
            payload["ok"],
            serde_json::json!(true),
            "dispatch for platform {platform} did not report ok=true"
        );
        assert_eq!(
            payload["detail"],
            serde_json::json!(platform),
            "dispatch's transport-result.detail must echo the relay provider \
             (this bundle's own `envelope.event.platform`), proving the C# \
             component relayed to platform {platform} and not a hardcoded one"
        );
    }
}
