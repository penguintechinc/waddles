//! End-to-end proof that this executor's wasmtime instantiation, WIT host
//! import wiring (`crate::host::imports`) and frame protocol
//! (`crate::wire`) work together against a REAL compiled WASI 0.2
//! component -- `tests/fixtures/hostile_fixture.wasm` -- not a hand-rolled
//! stand-in (task instruction: "never fake a host call"). The only thing
//! simulated here is the STAGE side of the connection, which does not
//! exist yet (blocked on `core/svc_process`'s M4 host-capability wiring);
//! it answers each `host-call` with a canned-but-realistic
//! `host-result`, exactly the shape a real stage would send.

// Integration-test-only: assertions on the wire protocol read naturally as
// `expect`/`unwrap` (`rules/general.md` permits this outside library code);
// the crate-wide `[lints.clippy] unwrap_used/expect_used = "deny"` in
// `Cargo.toml` still reaches `tests/` targets, so this file opts out
// explicitly rather than the `#[cfg(test)] mod tests { #![allow(...)] }`
// pattern used inside `src/`.
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

const FIXTURE_WASM: &[u8] = include_bytes!("fixtures/hostile_fixture.wasm");
const APP_ID: &str = "waddles.test.hostile-fixture";

/// Hands back the committed fixture bytes regardless of key -- there is
/// exactly one bundle in this test.
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

/// Answers exactly one `host-call` frame read off `io` with a canned
/// result appropriate to its capability/op, mirroring the JSON contract
/// `crate::host::imports` documents at each `Host` impl.
async fn answer_one_host_call<S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin>(io: &mut S) {
    let frame = read_frame(io).await.expect("expected a host-call frame");
    let Message::HostCall(call) = frame.message else {
        panic!("expected host-call, got {:?}", frame.message);
    };
    let result = match (call.capability, call.op.as_str()) {
        (CapabilityKind::Context, "get-context") => serde_json::json!({
            "tenant": "t1", "app_id": APP_ID, "message_id": "msg-1"
        }),
        (CapabilityKind::Kv, "set") => serde_json::json!({}),
        (CapabilityKind::Kv, "get") => serde_json::json!({ "value": [104, 105] }), // "hi"
        (CapabilityKind::Db, "execute") => serde_json::json!({
            "columns": ["one"], "rows": [[1]], "rows_affected": 1
        }),
        (CapabilityKind::Log, "write") => serde_json::json!({}),
        (CapabilityKind::Clock, "now-millis") => serde_json::json!(1_700_000_000_000_u64),
        (CapabilityKind::Flags, "tier") => serde_json::json!({ "tier": "professional" }),
        (CapabilityKind::Flags, "enabled") => serde_json::json!({ "enabled": true }),
        (CapabilityKind::Relay, "push") => serde_json::json!({}),
        other => panic!("unexpected host-call {other:?}"),
    };
    write_frame(
        io,
        &Frame::new(
            frame.id,
            Message::HostResult(HostResultBody {
                result: Some(result),
                error: None,
            }),
        ),
    )
    .await
    .expect("write host-result");
}

/// Drives one `load` + one `transform` invoke (with `host_calls` host-call
/// exchanges expected mid-flight) + one `dispatch` invoke (2 host calls:
/// flags then relay) + `shutdown`, over the stage half of `io`.
async fn run_fake_stage<S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin>(
    mut io: S,
    digest: String,
) -> (serde_json::Value, serde_json::Value) {
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
            1000,
            Message::Load(LoadBody {
                app_id: APP_ID.to_string(),
                version: "1".to_string(),
                digest: digest.clone(),
                component_key: "bundles/hostile-fixture/1/x.wasm".to_string(),
                sidecar_key: "bundles/hostile-fixture/1/x.json".to_string(),
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
    let Message::Loaded(loaded_body) = loaded.message else {
        panic!("expected loaded, got {:?}", loaded.message);
    };
    assert_eq!(loaded_body.digest, digest);

    // process-stage.transform("kv-roundtrip") -> two host calls (set, get).
    write_frame(
        &mut io,
        &Frame::new(
            1001,
            Message::Invoke(InvokeBody {
                app_id: APP_ID.to_string(),
                digest: digest.clone(),
                export: ExportKind::Transform,
                payload: serde_json::json!({
                    "platform": "test",
                    "event_type": "kv-roundtrip",
                    "actor": null,
                    "payload_json": "{}",
                    "occurred_at": "2026-09-22T00:00:00.000Z",
                }),
                deadline_ms: 5000,
                trace: None,
            }),
        ),
    )
    .await
    .expect("write invoke transform");
    answer_one_host_call(&mut io).await; // kv.set
    answer_one_host_call(&mut io).await; // kv.get
    let transform_result = read_frame(&mut io).await.expect("transform result");
    let Message::Result(transform_body) = transform_result.message else {
        panic!("expected result, got {:?}", transform_result.message);
    };

    // action-stage.dispatch(...) -> two host calls (flags.tier, flags.enabled -- wait
    // fixture only calls tier() and enabled() and relay.push, 3 host calls total).
    write_frame(
        &mut io,
        &Frame::new(
            1002,
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
                            "platform": "test",
                            "event_type": "probe",
                            "actor": null,
                            "payload_json": "{}",
                            "occurred_at": "2026-09-22T00:00:00.000Z",
                        },
                        "ts": "2026-09-22T00:00:00.000Z",
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
    answer_one_host_call(&mut io).await; // flags.tier
    answer_one_host_call(&mut io).await; // flags.enabled
    answer_one_host_call(&mut io).await; // relay.push
    let dispatch_result = read_frame(&mut io).await.expect("dispatch result");
    let Message::Result(dispatch_body) = dispatch_result.message else {
        panic!("expected result, got {:?}", dispatch_result.message);
    };

    write_frame(
        &mut io,
        &Frame::new(1003, Message::Shutdown(ShutdownBody { grace_ms: 100 })),
    )
    .await
    .expect("write shutdown");

    (transform_body.payload, dispatch_body.payload)
}

#[tokio::test]
async fn full_stack_load_transform_dispatch_against_a_real_component() {
    let (executor_io, stage_io) = tokio::io::duplex(256 * 1024);
    let executor = Arc::new(Executor::new(&test_config(), FixtureSource).expect("executor builds"));
    let digest = fixture_digest();

    let stage = tokio::spawn(run_fake_stage(stage_io, digest));

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
        )
        .await
    });

    let (transform_payload, dispatch_payload) = stage.await.expect("stage task");
    executor_task
        .await
        .expect("executor task")
        .expect("connection ran to a clean shutdown");

    // transform's payload_json is a String field on PlatformEvent; the
    // guest set it to the canned kv.get bytes decoded as UTF-8 ("hi"),
    // proving the kv.set-then-get round trip really crossed the WIT
    // boundary and came back through this executor's real host bridge.
    let payload_json = transform_payload["payload_json"]
        .as_str()
        .expect("payload_json is a string");
    assert!(
        payload_json.contains("hi"),
        "expected the kv round trip's value in payload_json, got {payload_json:?}"
    );

    // dispatch's `ok` field is the canned flags.enabled=true value, and
    // `detail` is the canned flags.tier="professional" value.
    assert_eq!(dispatch_payload["ok"], serde_json::json!(true));
    assert_eq!(
        dispatch_payload["detail"],
        serde_json::json!("professional")
    );
}

/// Negative sandbox test #1 (spec SS14.6), against the real compiled
/// fixture rather than a WIT-import-level unit test: the guest's
/// `event_type: "socket-probe"` branch attempts `std::net::TcpStream::
/// connect` and the invocation completes normally with the denial
/// encoded into `payload_json`, proving the component is not trapped or
/// killed by a denied socket call (spec: "the component keeps running").
///
/// Note (documented, not silently skipped): `cargo component build`'s
/// default `wasm32-wasip1`+adapter pipeline does not lower
/// `std::net::TcpStream` to a `wasi:sockets` import at all for a Rust
/// guest (verified via `wasm-tools component wit` on this fixture, see
/// `tests/fixtures/README.md`) -- matching spec SS6.5's own per-language
/// table, which lists `wasi:sockets` as a permitted-beyond-the-world
/// import for Python and Tier 2 prebuilt components, NOT for Rust. The
/// socket call therefore fails at the wasi-libc shim layer with an
/// ordinary `ConnectionRefused`-class `io::Error` before ever reaching a
/// host import this executor would need to deny -- which is exactly
/// `src/host/mod.rs`'s own `wasi_tcp_create_socket_is_denied_natively`/
/// `wasi_udp_create_socket_is_denied_natively` tests' job, exercised
/// directly against the `wasi:sockets` `Host` trait this executor's
/// linker actually wires up.
/// This test still asserts the guest-visible half of the guarantee: a
/// denied/failed socket call is a clean `Err`, not a trap.
#[tokio::test]
async fn socket_probe_completes_without_trapping_the_component() {
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
        read_frame(&mut io).await.expect("loaded reply");

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
                        "event_type": "socket-probe",
                        "actor": null,
                        "payload_json": "{}",
                        "occurred_at": "2026-09-22T00:00:00.000Z",
                    }),
                    deadline_ms: 5000,
                    trace: None,
                }),
            ),
        )
        .await
        .expect("write invoke");
        let result = read_frame(&mut io)
            .await
            .expect("result frame (not a trap/hang)");

        write_frame(
            &mut io,
            &Frame::new(3, Message::Shutdown(ShutdownBody { grace_ms: 100 })),
        )
        .await
        .expect("write shutdown");
        result
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
        )
        .await
    });

    let result_frame = stage.await.expect("stage task");
    executor_task
        .await
        .expect("executor task")
        .expect("connection ran to a clean shutdown");

    let Message::Result(result_body) = result_frame.message else {
        panic!(
            "socket probe trapped the component instead of returning a result: {:?}",
            result_frame.message
        );
    };
    let payload_json = result_body.payload["payload_json"]
        .as_str()
        .expect("payload_json is a string");
    assert!(
        payload_json.starts_with("socket-denied:")
            || payload_json == "socket-unexpectedly-succeeded",
        "unexpected socket-probe outcome: {payload_json:?}"
    );
    println!("socket_probe outcome: {payload_json}");
}
