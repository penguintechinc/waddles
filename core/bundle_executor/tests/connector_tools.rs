//! Real-path proof of P6a Task 4: a connector overlay on `world connector-tools`
//! can EXPOSE tools (`tool-provider.call`) and link the P3b `tool` import,
//! while the frozen `connector` world is untouched and every misuse fails loud.
//!
//! Real compiled components throughout, never a mock:
//! - `fixtures/connector_tools_fixture.wasm` -- `connector-tools` world, tools
//!   served through the real `waddle-sdk-rs` `ToolRegistry`.
//! - `fixtures/echo_connector.wasm` -- the plain `connector` world.
//!
//! Everything goes through the production `build_engine` / `LinkerCache` /
//! `ConnectorHost::load` / `Executor::pin_connector` / `run_connection` path.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use bundle_executor::config::CliConfig;
use bundle_executor::connector::{
    ConnectionCtx, ConnectorCallError, ConnectorHost, ConnectorLimits, FrameError, RawFrame,
    ToolError,
};
use bundle_executor::engine::{build_engine, spawn_epoch_ticker, LinkerCache};
use bundle_executor::error::ExecutorError;
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::{ComponentSource, Executor};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use bundle_executor::wire::run_connection;
use penguin_bundle_host::wire::{
    read_frame, write_frame, ErrorBody, ErrorCode, ExportKind, Frame, HelloLimits, HelloOkBody,
    InvokeBody, Message, ResultBody,
};
use sha2::{Digest, Sha256};
use tokio::io::{duplex, DuplexStream};
use wasmtime::Engine;

const TOOLS_WASM: &[u8] = include_bytes!("fixtures/connector_tools_fixture.wasm");
const ECHO_WASM: &[u8] = include_bytes!("fixtures/echo_connector.wasm");
const TOOLS_APP: &str = "waddles.core.test.connector-tools";
const ECHO_APP: &str = "waddles.core.example.echo";

fn cfg() -> CliConfig {
    use clap::Parser;
    CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-ingest:8301",
    ])
    .unwrap()
}

fn digest_of(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("sha256:{:x}", hasher.finalize())
}

fn limits() -> ConnectorLimits {
    ConnectorLimits {
        fuel: 50_000_000,
        deadline_ms: 2_000,
        memory_limit_mb: 64,
        pool_size: 4,
        pool_wait: Duration::from_millis(200),
    }
}

fn manifest(app_id: &str, bytes: &[u8], world: WitWorld) -> VerifiedManifest {
    VerifiedManifest::new(app_id, digest_of(bytes), world, HashSet::new())
}

struct Rig {
    engine: Engine,
    linkers: LinkerCache,
    _ticker: tokio::task::JoinHandle<()>,
}

fn rig() -> Rig {
    let engine = build_engine(&cfg()).unwrap();
    let ticker = spawn_epoch_ticker(&engine);
    Rig {
        linkers: LinkerCache::new(engine.clone()),
        engine,
        _ticker: ticker,
    }
}

async fn try_load(
    rig: &Rig,
    app: &str,
    bytes: &[u8],
    world: WitWorld,
) -> Result<ConnectorHost, ExecutorError> {
    ConnectorHost::load(
        &rig.engine,
        &rig.linkers,
        &manifest(app, bytes, world),
        bytes.to_vec(),
        limits(),
        None,
    )
    .await
}

async fn tools_host(rig: &Rig) -> ConnectorHost {
    try_load(rig, TOOLS_APP, TOOLS_WASM, WitWorld::ConnectorTools)
        .await
        .unwrap()
}

fn json(v: serde_json::Value) -> Vec<u8> {
    serde_json::to_vec(&v).unwrap()
}

fn ctx() -> ConnectionCtx {
    ConnectionCtx {
        platform: "twitch".to_string(),
        source_id: "src-1".to_string(),
        connection_id: "conn-1".to_string(),
        session_token: None,
    }
}

#[tokio::test]
async fn provider_tool_returns_canonical_json_with_fuel_metered() {
    let rig = rig();
    let host = tools_host(&rig).await;
    let reply = host
        .call_tool(
            "get_chatters".into(),
            json(serde_json::json!({"source_id": "src-1"})),
        )
        .await
        .unwrap();
    let v: serde_json::Value = serde_json::from_slice(&reply.value).unwrap();
    assert_eq!(v["source_id"], "src-1");
    assert_eq!(v["chatters"], serde_json::json!(["chatter-a", "chatter-b"]));
    assert!(
        reply.fuel_used > 0,
        "fuel metering must be live on tool calls"
    );
}

#[tokio::test]
async fn echo_tool_round_trips_args_unchanged() {
    let rig = rig();
    let host = tools_host(&rig).await;
    let reply = host
        .call_tool("echo".into(), json(serde_json::json!({"k": [1, 2, 3]})))
        .await
        .unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&reply.value).unwrap(),
        serde_json::json!({"k": [1, 2, 3]})
    );
}

#[tokio::test]
async fn provider_refusal_is_a_typed_tool_error_not_a_sandbox_trip() {
    let rig = rig();
    let host = tools_host(&rig).await;
    let err = host
        .call_tool("denied".into(), json(serde_json::json!({})))
        .await
        .unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::Tool(ToolError::NotGranted)),
        "{err:?}"
    );
    assert!(!err.is_sandbox_trip());
    assert_eq!(err.kind(), "tool_error");
}

#[tokio::test]
async fn bad_args_and_unknown_tool_are_typed_tool_errors() {
    let rig = rig();
    let host = tools_host(&rig).await;
    // Missing required `source_id` -> invalid-args from the real SDK registry.
    let err = host
        .call_tool("get_chatters".into(), json(serde_json::json!({})))
        .await
        .unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::Tool(ToolError::InvalidArgs(_))),
        "{err:?}"
    );
    // Non-JSON args bytes.
    let err = host
        .call_tool("echo".into(), b"\xff not json".to_vec())
        .await
        .unwrap_err();
    assert!(matches!(err, ConnectorCallError::Tool(_)), "{err:?}");
    // Unknown tool.
    let err = host
        .call_tool("no_such_tool".into(), json(serde_json::json!({})))
        .await
        .unwrap_err();
    assert!(matches!(err, ConnectorCallError::Tool(_)), "{err:?}");
}

#[tokio::test]
async fn tool_import_is_linked_and_fails_typed_without_a_stage_bridge() {
    // `relay` makes a real `tool.invoke` host call. With no stage bridge the
    // host answers a typed `target-unavailable` -- proving the import is linked
    // (a missing import would fail at load) and never fabricates a success.
    let rig = rig();
    let host = tools_host(&rig).await;
    let err = host
        .call_tool(
            "relay".into(),
            json(serde_json::json!({"target": "waddles.core.other", "tool": "x", "args": {}})),
        )
        .await
        .unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::Tool(ToolError::TargetUnavailable)),
        "{err:?}"
    );
}

#[tokio::test]
async fn receiver_exports_still_work_in_the_tools_world() {
    let rig = rig();
    let host = tools_host(&rig).await;
    let hs = host.on_connect(ctx()).await.unwrap();
    assert_eq!(hs.value.bytes, b"tools-hello");
    let reply = host
        .on_frame(RawFrame {
            ctx: ctx(),
            bytes: br#"{"text":"hi"}"#.to_vec(),
            received_at: "2026-11-04T00:00:00.000Z".into(),
        })
        .await
        .unwrap();
    assert_eq!(reply.value.len(), 1);
    assert_eq!(reply.value[0].payload_json, r#"{"text":"hi"}"#);
    let err = host
        .on_frame(RawFrame {
            ctx: ctx(),
            bytes: b"nope".to_vec(),
            received_at: "2026-11-04T00:00:00.000Z".into(),
        })
        .await
        .unwrap_err();
    assert!(matches!(
        err,
        ConnectorCallError::Guest(FrameError::Malformed(_))
    ));
    assert_eq!(host.on_heartbeat_due(ctx()).await.unwrap().value, None);
    host.on_disconnect(ctx(), "bye".into()).await.unwrap();
}

#[tokio::test]
async fn plain_connector_world_has_no_tool_provider_and_fails_loud() {
    let rig = rig();
    let host = try_load(&rig, ECHO_APP, ECHO_WASM, WitWorld::Connector)
        .await
        .unwrap();
    let err = host
        .call_tool("echo".into(), json(serde_json::json!({})))
        .await
        .unwrap_err();
    assert!(matches!(err, ConnectorCallError::NoToolProvider), "{err:?}");
    assert!(!err.is_sandbox_trip());
}

#[tokio::test]
async fn tools_component_refuses_to_load_under_the_frozen_connector_world() {
    // Isolation: the plain `connector` linker never registers `tool`, so a
    // component importing it cannot sneak in without declaring connector-tools.
    let rig = rig();
    let err = try_load(&rig, TOOLS_APP, TOOLS_WASM, WitWorld::Connector)
        .await
        .err()
        .expect("a tool-importing component must not link under `connector`");
    assert!(matches!(err, ExecutorError::ConnectorLoad(_)), "{err:?}");
}

#[tokio::test]
async fn connector_without_tool_provider_refuses_to_load_as_connector_tools() {
    let rig = rig();
    let err = try_load(&rig, ECHO_APP, ECHO_WASM, WitWorld::ConnectorTools)
        .await
        .err()
        .expect("echo exports no tool-provider");
    assert!(matches!(err, ExecutorError::ConnectorLoad(_)), "{err:?}");
}

#[tokio::test]
async fn connector_tools_world_stays_core_only() {
    let rig = rig();
    let err = try_load(
        &rig,
        "vendor.acme.connector-tools",
        TOOLS_WASM,
        WitWorld::ConnectorTools,
    )
    .await
    .err()
    .expect("vendor namespace must be refused");
    assert!(matches!(err, ExecutorError::ConnectorLoad(_)), "{err:?}");
}

#[tokio::test]
async fn a_digest_mismatch_is_refused() {
    let rig = rig();
    let bad = VerifiedManifest::new(
        TOOLS_APP,
        format!("sha256:{}", "0".repeat(64)),
        WitWorld::ConnectorTools,
        HashSet::new(),
    );
    let err = ConnectorHost::load(
        &rig.engine,
        &rig.linkers,
        &bad,
        TOOLS_WASM.to_vec(),
        limits(),
        None,
    )
    .await
    .err()
    .expect("digest mismatch");
    assert!(
        matches!(err, ExecutorError::DigestMismatch { .. }),
        "{err:?}"
    );
}

// ---- over the host-API wire: `ExportKind::ToolCall` reaches a pinned overlay ----

struct NoSource;

impl ComponentSource for NoSource {
    async fn fetch(&self, _: &str, _: &str) -> Result<(Vec<u8>, Vec<u8>), ExecutorError> {
        Err(ExecutorError::Config("no stage components here".into()))
    }
}

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
    ) -> Result<ResultBody, ErrorBody> {
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
        assert_eq!(reply.id, id);
        match reply.message {
            Message::Result(r) => Ok(r),
            Message::Error(e) => Err(e),
            other => panic!("expected result/error, got {other:?}"),
        }
    }
}

async fn wire_rig() -> Stage {
    let cfg = cfg();
    let exec = Arc::new(Executor::new(&cfg, NoSource).unwrap());
    let l = ConnectorLimits {
        pool_wait: Duration::from_millis(200),
        ..ConnectorLimits::from_config(&cfg)
    };
    exec.pin_connector(
        &manifest(TOOLS_APP, TOOLS_WASM, WitWorld::ConnectorTools),
        TOOLS_WASM.to_vec(),
        l,
        None,
    )
    .await
    .unwrap();
    exec.pin_connector(
        &manifest(ECHO_APP, ECHO_WASM, WitWorld::Connector),
        ECHO_WASM.to_vec(),
        l,
        None,
    )
    .await
    .unwrap();
    let (mut stage_io, exec_io) = duplex(1 << 20);
    let hello = exec.hello(false, "runc");
    tokio::spawn(async move {
        let _ = run_connection(exec_io, hello, exec, "test-stage", Heartbeat::disabled()).await;
    });
    let hello = read_frame(&mut stage_io).await.unwrap();
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
    Stage {
        io: stage_io,
        next_id: 100,
    }
}

#[tokio::test]
async fn tool_call_over_the_wire_runs_the_connector_providers_tool() {
    let mut stage = wire_rig().await;
    let digest = digest_of(TOOLS_WASM);
    let ok = stage
        .invoke(
            TOOLS_APP,
            &digest,
            ExportKind::ToolCall,
            serde_json::json!({"tool": "get_chatters", "args": {"source_id": "s9"}}),
        )
        .await
        .unwrap();
    assert_eq!(ok.payload["ok"]["source_id"], "s9");
    assert_eq!(
        ok.payload["ok"]["chatters"],
        serde_json::json!(["chatter-a", "chatter-b"])
    );
    assert!(ok.fuel_used > 0);

    // A provider refusal is a normal result payload, same shape as the stage path.
    let denied = stage
        .invoke(
            TOOLS_APP,
            &digest,
            ExportKind::ToolCall,
            serde_json::json!({"tool": "denied", "args": {}}),
        )
        .await
        .unwrap();
    assert!(
        denied.payload.get("tool_error").is_some(),
        "{:?}",
        denied.payload
    );
}

#[tokio::test]
async fn tool_call_wire_failures_are_typed() {
    let mut stage = wire_rig().await;
    // Plain connector has no tool-provider -> EXPORT_MISSING.
    let e = stage
        .invoke(
            ECHO_APP,
            &digest_of(ECHO_WASM),
            ExportKind::ToolCall,
            serde_json::json!({"tool": "echo", "args": {}}),
        )
        .await
        .unwrap_err();
    assert_eq!(e.code, ErrorCode::ExportMissing, "{e:?}");

    // Malformed payload (unknown field) -> MALFORMED_FRAME.
    let e = stage
        .invoke(
            TOOLS_APP,
            &digest_of(TOOLS_WASM),
            ExportKind::ToolCall,
            serde_json::json!({"tool": "echo", "args": {}, "extra": 1}),
        )
        .await
        .unwrap_err();
    assert_eq!(e.code, ErrorCode::MalformedFrame, "{e:?}");

    // Wrong app_id for a pinned digest -> UNKNOWN_BUNDLE (fail closed).
    let e = stage
        .invoke(
            "waddles.core.someone-else",
            &digest_of(TOOLS_WASM),
            ExportKind::ToolCall,
            serde_json::json!({"tool": "echo", "args": {}}),
        )
        .await
        .unwrap_err();
    assert_eq!(e.code, ErrorCode::UnknownBundle, "{e:?}");

    // Unpinned digest still falls through to the stage registry -> UNKNOWN_BUNDLE.
    let e = stage
        .invoke(
            TOOLS_APP,
            "sha256:deadbeef",
            ExportKind::ToolCall,
            serde_json::json!({"tool": "echo", "args": {}}),
        )
        .await
        .unwrap_err();
    assert_eq!(e.code, ErrorCode::UnknownBundle, "{e:?}");
}

// ---- security review: tool traffic must not defeat the receiver pool / deadline ----

use bundle_executor::connector::{clamp_deadline_ms, tool_pool_size};
use std::time::Instant;

async fn tools_host_with(rig: &Rig, l: ConnectorLimits) -> ConnectorHost {
    ConnectorHost::load(
        &rig.engine,
        &rig.linkers,
        &manifest(TOOLS_APP, TOOLS_WASM, WitWorld::ConnectorTools),
        TOOLS_WASM.to_vec(),
        l,
        None,
    )
    .await
    .unwrap()
}

#[test]
fn tool_pool_is_a_fixed_separate_share_and_deadline_only_tightens() {
    assert_eq!(tool_pool_size(1), 1);
    assert_eq!(tool_pool_size(4), 2);
    assert_eq!(tool_pool_size(9), 4);
    assert_eq!(clamp_deadline_ms(None, 2_000), 2_000);
    assert_eq!(clamp_deadline_ms(Some(500), 2_000), 500);
    assert_eq!(clamp_deadline_ms(Some(60_000), 2_000), 2_000);
    assert_eq!(clamp_deadline_ms(Some(0), 2_000), 1);
}

// regression: security-review resource-cap defeat -- saturating tool-provider
// calls must never consume a slot on-frame / heartbeat need.
#[tokio::test]
async fn saturated_tool_pool_cannot_starve_on_frame_or_heartbeat() {
    let rig = rig();
    let l = ConnectorLimits {
        pool_size: 2,
        deadline_ms: 1_500,
        // Huge fuel so the spinning tool is ended by the DEADLINE, holding its
        // slot long enough to prove saturation.
        fuel: u64::MAX / 2,
        ..limits()
    };
    let host = tools_host_with(&rig, l).await;
    assert_eq!(host.available_slots(), 2);
    assert_eq!(host.available_tool_slots(), tool_pool_size(2));

    // Occupy the entire tool pool with a spinning tool.
    let slow = {
        let h = host.clone();
        tokio::spawn(async move {
            h.call_tool("slow".into(), json(serde_json::json!({})))
                .await
        })
    };
    let deadline = Instant::now() + Duration::from_secs(5);
    while host.available_tool_slots() > 0 {
        assert!(Instant::now() < deadline, "slow tool never took its slot");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }

    // Further tool calls are typed back-pressure from the TOOL pool...
    let err = host
        .call_tool("echo".into(), json(serde_json::json!({})))
        .await
        .unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::PoolExhausted { .. }),
        "{err:?}"
    );
    // ...while the receiver pool is untouched and on-frame/heartbeat still run.
    assert_eq!(host.available_slots(), 2);
    host.on_frame(RawFrame {
        ctx: ctx(),
        bytes: br#"{"text":"still alive"}"#.to_vec(),
        received_at: "2026-11-04T00:00:00.000Z".into(),
    })
    .await
    .unwrap();
    host.on_heartbeat_due(ctx()).await.unwrap();

    // The spinning tool is ended by the host deadline, freeing the tool slot.
    let done = slow.await.unwrap().unwrap_err();
    assert!(done.is_sandbox_trip(), "{done:?}");
    assert_eq!(host.available_tool_slots(), tool_pool_size(2));
}

// regression: a wire/body deadline ABOVE the connector's limit is clamped to it.
#[tokio::test]
async fn a_requested_deadline_above_the_limit_is_clamped_to_the_limit() {
    let rig = rig();
    let l = ConnectorLimits {
        deadline_ms: 400,
        fuel: u64::MAX / 2,
        ..limits()
    };
    let host = tools_host_with(&rig, l).await;
    let started = Instant::now();
    let err = host
        .call_tool_with_deadline("slow".into(), json(serde_json::json!({})), Some(600_000))
        .await
        .unwrap_err();
    let took = started.elapsed();
    assert!(
        matches!(err, ConnectorCallError::DeadlineExceeded(_)),
        "{err:?}"
    );
    assert!(
        took < Duration::from_secs(5),
        "a 600s body deadline must be clamped to the 400ms limit, took {took:?}"
    );
}

#[tokio::test]
async fn a_requested_deadline_below_the_limit_tightens_it() {
    let rig = rig();
    let l = ConnectorLimits {
        deadline_ms: 20_000,
        fuel: u64::MAX / 2,
        ..limits()
    };
    let host = tools_host_with(&rig, l).await;
    let started = Instant::now();
    let err = host
        .call_tool_with_deadline("slow".into(), json(serde_json::json!({})), Some(300))
        .await
        .unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::DeadlineExceeded(_)),
        "{err:?}"
    );
    assert!(
        started.elapsed() < Duration::from_secs(8),
        "a 300ms body deadline must beat the 20s limit, took {:?}",
        started.elapsed()
    );
}
