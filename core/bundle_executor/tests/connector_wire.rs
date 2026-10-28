//! Real-path proof that the executor serves connector-world exports over the
//! host-API wire (`penguin-bundle-host` 0.3.0 `ExportKind::Connector*`).
//!
//! The executor side is entirely real (`Executor`, `ConnectorRouter`,
//! `ConnectorHost`, wasmtime, the production `Linker`, `run_connection`'s read
//! loop and the frame codec) against two real compiled `waddle:connector`
//! components (Echo and the fault injector). Only the STAGE half is scripted:
//! it speaks raw `invoke` frames, so every wire-contract field name and every
//! typed error code is pinned from the outside.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine as _;
use bundle_executor::config::CliConfig;
use bundle_executor::connector::ConnectorLimits;
use bundle_executor::error::ExecutorError;
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::{ComponentSource, Executor};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use bundle_executor::wire::run_connection;
use penguin_bundle_host::wire::{
    read_frame, write_frame, ErrorCode, ExportKind, Frame, HelloLimits, HelloOkBody, InvokeBody,
    Message, ResultBody,
};
use sha2::{Digest, Sha256};
use tokio::io::{duplex, DuplexStream};

const ECHO_WASM: &[u8] = include_bytes!("fixtures/echo_connector.wasm");
const FAULTY_WASM: &[u8] = include_bytes!("fixtures/faulty_connector.wasm");
const ECHO_APP: &str = "waddles.core.example.echo";
const FAULTY_APP: &str = "waddles.core.test.faulty";

struct NoSource;

impl ComponentSource for NoSource {
    async fn fetch(&self, _: &str, _: &str) -> Result<(Vec<u8>, Vec<u8>), ExecutorError> {
        Err(ExecutorError::Config("no stage components here".into()))
    }
}

fn digest_of(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("sha256:{:x}", hasher.finalize())
}

fn cfg() -> CliConfig {
    use clap::Parser;
    CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-ingest:8301",
    ])
    .unwrap()
}

fn limits(cfg: &CliConfig) -> ConnectorLimits {
    ConnectorLimits {
        pool_wait: Duration::from_millis(200),
        ..ConnectorLimits::from_config(cfg)
    }
}

async fn pin(exec: &Executor<NoSource>, app: &str, wasm: &'static [u8], l: ConnectorLimits) {
    let m = VerifiedManifest::new(app, digest_of(wasm), WitWorld::Connector, HashSet::new());
    exec.pin_connector(&m, wasm.to_vec(), l, None)
        .await
        .unwrap();
}

/// The scripted stage: owns the stage end of the pipe, post-handshake.
struct Stage {
    io: DuplexStream,
    next_id: u64,
}

impl Stage {
    async fn invoke(
        &mut self,
        app: &str,
        digest: &str,
        export: ExportKind,
        payload: serde_json::Value,
    ) -> Result<ResultBody, penguin_bundle_host::wire::ErrorBody> {
        let id = self.next_id;
        self.next_id += 1;
        write_frame(
            &mut self.io,
            &Frame::new(
                id,
                Message::Invoke(InvokeBody {
                    app_id: app.to_string(),
                    digest: digest.to_string(),
                    export,
                    payload,
                    deadline_ms: 2_000,
                    trace: None,
                }),
            ),
        )
        .await
        .unwrap();
        let reply = read_frame(&mut self.io).await.unwrap();
        assert_eq!(reply.id, id, "a reply reuses the request's correlation id");
        match reply.message {
            Message::Result(r) => Ok(r),
            Message::Error(e) => Err(e),
            other => panic!("expected result/error, got {other:?}"),
        }
    }
}

async fn connect(exec: Arc<Executor<NoSource>>) -> (Stage, tokio::task::JoinHandle<()>) {
    let (mut stage_io, exec_io) = duplex(1 << 20);
    let hello = exec.hello(false, "runc");
    let task = tokio::spawn(async move {
        let _ = run_connection(exec_io, hello, exec, "test-stage", Heartbeat::disabled()).await;
    });
    let hello = read_frame(&mut stage_io).await.unwrap();
    assert!(matches!(hello.message, Message::Hello(_)));
    write_frame(
        &mut stage_io,
        &Frame::new(
            hello.id,
            Message::HelloOk(HelloOkBody {
                stage: "test".into(),
                protocol_version: 1,
                limits: HelloLimits {
                    call_timeout_ms: 2_000,
                    memory_mb: 64,
                    max_concurrent_calls: 8,
                },
            }),
        ),
    )
    .await
    .unwrap();
    (
        Stage {
            io: stage_io,
            next_id: 100,
        },
        task,
    )
}

fn ctx(platform: &str) -> serde_json::Value {
    serde_json::json!({
        "platform": platform,
        "source_id": "src-1",
        "connection_id": "conn-1",
        "session_token": null,
    })
}

async fn rig(tune: impl FnOnce(&mut ConnectorLimits)) -> (Stage, Arc<Executor<NoSource>>) {
    let cfg = cfg();
    let exec = Arc::new(Executor::new(&cfg, NoSource).unwrap());
    let mut l = limits(&cfg);
    tune(&mut l);
    pin(&exec, ECHO_APP, ECHO_WASM, l).await;
    pin(&exec, FAULTY_APP, FAULTY_WASM, l).await;
    let (stage, _task) = connect(Arc::clone(&exec)).await;
    (stage, exec)
}

#[tokio::test]
async fn all_four_exports_run_over_the_wire_with_the_documented_payloads() {
    let (mut stage, exec) = rig(|_| {}).await;
    assert_eq!(exec.pinned_connectors().await, 2);
    let echo = digest_of(ECHO_WASM);

    let hs = stage
        .invoke(
            ECHO_APP,
            &echo,
            ExportKind::ConnectorOnConnect,
            serde_json::json!({"ctx": ctx("echo")}),
        )
        .await
        .unwrap();
    assert_eq!(hs.payload["bytes"], BASE64.encode(b"echo-hello"));
    assert_eq!(hs.payload["secret_refs"], serde_json::json!([]));
    assert!(hs.fuel_used > 0, "fuel metering is live on the wire path");

    let frame = br#"{"source":"echo","text":"  hi  ","actor":"alice"}"#;
    let ev = stage
        .invoke(
            ECHO_APP,
            &echo,
            ExportKind::ConnectorOnFrame,
            serde_json::json!({
                "ctx": ctx("echo"),
                "bytes": BASE64.encode(frame),
                "received_at": "2026-10-13T00:00:00.000Z",
            }),
        )
        .await
        .unwrap();
    let e = &ev.payload["events"][0];
    assert_eq!(e["platform"], "echo");
    assert_eq!(e["event_type"], "message");
    assert_eq!(e["actor"], "alice");
    assert_eq!(e["payload_json"], r#"{"text":"hi"}"#);
    assert_eq!(e["occurred_at"], "2026-10-13T00:00:00.000Z");

    let hb = stage
        .invoke(
            ECHO_APP,
            &echo,
            ExportKind::ConnectorOnHeartbeatDue,
            serde_json::json!({"ctx": ctx("echo")}),
        )
        .await
        .unwrap();
    assert!(hb.payload["bytes"].is_null());

    let faulty = digest_of(FAULTY_WASM);
    let hb = stage
        .invoke(
            FAULTY_APP,
            &faulty,
            ExportKind::ConnectorOnHeartbeatDue,
            serde_json::json!({"ctx": ctx("faulty")}),
        )
        .await
        .unwrap();
    assert_eq!(hb.payload["bytes"], BASE64.encode(b"faulty-ping"));

    let bye = stage
        .invoke(
            ECHO_APP,
            &echo,
            ExportKind::ConnectorOnDisconnect,
            serde_json::json!({"ctx": ctx("echo"), "reason": "done"}),
        )
        .await
        .unwrap();
    assert_eq!(bye.payload, serde_json::json!({}));
}

#[tokio::test]
async fn a_guest_frame_error_is_a_result_payload_with_its_kind() {
    let (mut stage, _exec) = rig(|_| {}).await;
    let echo = digest_of(ECHO_WASM);
    for (bytes, kind) in [
        (b"not json".as_slice(), "malformed"),
        (br#"{"text":"no source"}"#.as_slice(), "malformed"),
    ] {
        let r = stage
            .invoke(
                ECHO_APP,
                &echo,
                ExportKind::ConnectorOnFrame,
                serde_json::json!({
                    "ctx": ctx("echo"),
                    "bytes": BASE64.encode(bytes),
                    "received_at": "2026-10-13T00:00:00.000Z",
                }),
            )
            .await
            .unwrap();
        assert_eq!(r.payload["frame_error"]["kind"], kind);
    }
    let faulty = digest_of(FAULTY_WASM);
    for (bytes, kind) in [
        (b"???".as_slice(), "unsupported"),
        (b"malformed".as_slice(), "malformed"),
    ] {
        let r = stage
            .invoke(
                FAULTY_APP,
                &faulty,
                ExportKind::ConnectorOnFrame,
                serde_json::json!({
                    "ctx": ctx("faulty"),
                    "bytes": BASE64.encode(bytes),
                    "received_at": "2026-10-13T00:00:00.000Z",
                }),
            )
            .await
            .unwrap();
        assert_eq!(r.payload["frame_error"]["kind"], kind, "{bytes:?}");
    }
}

async fn faulty_frame(
    stage: &mut Stage,
    bytes: &[u8],
) -> Result<ResultBody, penguin_bundle_host::wire::ErrorBody> {
    stage
        .invoke(
            FAULTY_APP,
            &digest_of(FAULTY_WASM),
            ExportKind::ConnectorOnFrame,
            serde_json::json!({
                "ctx": ctx("faulty"),
                "bytes": BASE64.encode(bytes),
                "received_at": "2026-10-13T00:00:00.000Z",
            }),
        )
        .await
}

#[tokio::test]
async fn sandbox_containment_surfaces_as_typed_error_codes_and_the_executor_keeps_serving() {
    let (mut stage, _exec) = rig(|_| {}).await;
    let trap = faulty_frame(&mut stage, b"trap").await.unwrap_err();
    assert_eq!(trap.code, ErrorCode::WasmTrap);
    // The same pinned connector serves the next, unrelated call.
    assert!(faulty_frame(&mut stage, b"ok").await.is_ok());

    let (mut stage, _exec) = rig(|l| {
        l.fuel = 2_000_000;
        l.deadline_ms = 30_000;
    })
    .await;
    let fuel = faulty_frame(&mut stage, b"spin").await.unwrap_err();
    assert_eq!(fuel.code, ErrorCode::ExecutorDeadline);
    assert_eq!(fuel.detail.as_deref(), Some("fuel_exhausted"));

    let (mut stage, _exec) = rig(|l| {
        l.fuel = u64::MAX;
        l.deadline_ms = 150;
    })
    .await;
    let deadline = faulty_frame(&mut stage, b"spin").await.unwrap_err();
    assert_eq!(deadline.code, ErrorCode::ExecutorDeadline);
    assert_eq!(deadline.detail, None);

    let (mut stage, _exec) = rig(|l| l.memory_limit_mb = 16).await;
    let mem = faulty_frame(&mut stage, b"grow").await.unwrap_err();
    assert_eq!(mem.code, ErrorCode::MemoryLimit);
}

#[tokio::test]
async fn a_full_connector_pool_is_typed_pool_exhausted() {
    let (mut stage, _exec) = rig(|l| {
        l.pool_size = 1;
        l.pool_wait = Duration::from_millis(50);
        l.fuel = u64::MAX;
        l.deadline_ms = 1_200;
    })
    .await;
    // Occupy the single slot with a spinning call, then race a second.
    let faulty = digest_of(FAULTY_WASM);
    let spin = |id: u64, bytes: &[u8]| {
        Frame::new(
            id,
            Message::Invoke(InvokeBody {
                app_id: FAULTY_APP.to_string(),
                digest: faulty.clone(),
                export: ExportKind::ConnectorOnFrame,
                payload: serde_json::json!({
                    "ctx": ctx("faulty"),
                    "bytes": BASE64.encode(bytes),
                    "received_at": "2026-10-13T00:00:00.000Z",
                }),
                deadline_ms: 2_000,
                trace: None,
            }),
        )
    };
    write_frame(&mut stage.io, &spin(1, b"spin")).await.unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;
    write_frame(&mut stage.io, &spin(2, b"ok")).await.unwrap();
    let mut saw_pool_exhausted = false;
    for _ in 0..2 {
        let f = read_frame(&mut stage.io).await.unwrap();
        if let Message::Error(e) = f.message {
            if f.id == 2 {
                assert_eq!(e.code, ErrorCode::ExecutorDeadline);
                assert_eq!(e.detail.as_deref(), Some("pool_exhausted"));
                saw_pool_exhausted = true;
            }
        }
    }
    assert!(saw_pool_exhausted, "the second call must be back-pressured");
}

#[tokio::test]
async fn the_wire_fails_closed_on_unknown_digests_mismatched_ids_and_bad_payloads() {
    let (mut stage, _exec) = rig(|_| {}).await;
    let echo = digest_of(ECHO_WASM);
    let good = serde_json::json!({"ctx": ctx("echo")});

    // Unpinned digest.
    let e = stage
        .invoke(
            ECHO_APP,
            "sha256:00",
            ExportKind::ConnectorOnConnect,
            good.clone(),
        )
        .await
        .unwrap_err();
    assert_eq!(e.code, ErrorCode::UnknownBundle);

    // Pinned digest, wrong app_id: refused, never silently accepted.
    let e = stage
        .invoke(
            FAULTY_APP,
            &echo,
            ExportKind::ConnectorOnConnect,
            good.clone(),
        )
        .await
        .unwrap_err();
    assert_eq!(e.code, ErrorCode::UnknownBundle);

    // Stage exports never reach the connector registry (and a stage digest is
    // not pinned here anyway): fail closed either way.
    let e = stage
        .invoke(ECHO_APP, &echo, ExportKind::Transform, good.clone())
        .await
        .unwrap_err();
    assert_eq!(e.code, ErrorCode::UnknownBundle);

    // Malformed payloads: unknown field, bad base64, missing field, wrong type.
    for payload in [
        serde_json::json!({"ctx": ctx("echo"), "extra": 1}),
        serde_json::json!({"nope": true}),
    ] {
        let e = stage
            .invoke(ECHO_APP, &echo, ExportKind::ConnectorOnConnect, payload)
            .await
            .unwrap_err();
        assert_eq!(e.code, ErrorCode::MalformedFrame);
    }
    let e = stage
        .invoke(
            ECHO_APP,
            &echo,
            ExportKind::ConnectorOnFrame,
            serde_json::json!({"ctx": ctx("echo"), "bytes": "!!!", "received_at": "t"}),
        )
        .await
        .unwrap_err();
    assert_eq!(e.code, ErrorCode::MalformedFrame);
}

#[tokio::test]
async fn pinning_is_core_only_digest_checked_and_never_replaces() {
    let cfg = cfg();
    let exec = Executor::new(&cfg, NoSource).unwrap();
    let l = limits(&cfg);
    pin(&exec, ECHO_APP, ECHO_WASM, l).await;

    // Re-pinning the same digest is refused.
    let m = VerifiedManifest::new(
        ECHO_APP,
        digest_of(ECHO_WASM),
        WitWorld::Connector,
        HashSet::new(),
    );
    let err = exec
        .pin_connector(&m, ECHO_WASM.to_vec(), l, None)
        .await
        .unwrap_err();
    assert!(matches!(err, ExecutorError::ConnectorLoad(ref m) if m.contains("already pinned")));
    assert_eq!(exec.pinned_connectors().await, 1);

    // A vendor namespace never pins.
    let vendor = VerifiedManifest::new(
        "vendor.acme.echo",
        digest_of(FAULTY_WASM),
        WitWorld::Connector,
        HashSet::new(),
    );
    let err = exec
        .pin_connector(&vendor, FAULTY_WASM.to_vec(), l, None)
        .await
        .unwrap_err();
    assert!(matches!(err, ExecutorError::ConnectorLoad(_)));
    assert_eq!(exec.pinned_connectors().await, 1);
}

#[tokio::test]
async fn a_draining_executor_refuses_connector_invokes() {
    // Connector invokes are routed before the stage registry; a draining
    // executor refuses them like any invoke (SHUTTING_DOWN), proving the
    // routing sits behind the drain gate.
    let (mut stage, exec) = rig(|_| {}).await;
    let _ = exec.drain(Duration::from_millis(50)).await;
    let e = stage
        .invoke(
            ECHO_APP,
            &digest_of(ECHO_WASM),
            ExportKind::ConnectorOnConnect,
            serde_json::json!({"ctx": ctx("echo")}),
        )
        .await
        .unwrap_err();
    assert_eq!(e.code, ErrorCode::ShuttingDown);
}
