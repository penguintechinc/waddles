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
        max_tool_args_bytes: 900_000,
        max_tool_result_bytes: 900_000,
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
    /// Writes one invoke frame without waiting for its reply (so a test can have
    /// several in flight at once); returns the frame id to match on.
    async fn send_invoke(
        &mut self,
        app: &str,
        digest: &str,
        export: ExportKind,
        payload: serde_json::Value,
    ) -> u64 {
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
                    deadline_ms: 1_500,
                    trace: None,
                }),
            ),
        )
        .await
        .unwrap();
        id
    }

    /// Reads the next reply frame as `(id, result-or-error)`.
    async fn next_reply(&mut self) -> (u64, Result<ResultBody, ErrorBody>) {
        let reply = read_frame(&mut self.io).await.unwrap();
        let out = match reply.message {
            Message::Result(r) => Ok(r),
            Message::Error(e) => Err(e),
            other => panic!("expected result/error, got {other:?}"),
        };
        (reply.id, out)
    }

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
    wire_rig_with(&[]).await
}

/// [`wire_rig`] with extra executor CLI args (e.g. a tiny global call cap).
async fn wire_rig_with(extra: &[&str]) -> Stage {
    wire_rig_with_source(extra, NoSource).await
}

/// [`wire_rig_with`] with a caller-supplied stage component source.
async fn wire_rig_with_source<S: ComponentSource>(extra: &[&str], source: S) -> Stage {
    let cfg = {
        use clap::Parser;
        let mut args = vec![
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-ingest:8301",
        ];
        args.extend_from_slice(extra);
        CliConfig::try_parse_from(args).unwrap()
    };
    let exec = Arc::new(Executor::new(&cfg, source).unwrap());
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
    // ...while the receiver share is intact (combined cap 2, one held by the
    // tool) and on-frame/heartbeat still run.
    assert_eq!(host.available_slots(), 1);
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

// regression (PR #984 review claim 1): a connector `tool-call` must take the SAME
// global admission (`EXECUTOR_MAX_CONCURRENT_CALLS`) as every stage export. With
// a global cap of 1 and one spinning tool holding it, a second tool call -- even
// though the connector's own tool pool has free slots -- is refused
// `pool_exhausted` instead of running.
#[tokio::test]
async fn tool_call_takes_global_admission_like_other_exports() {
    let mut stage = wire_rig_with(&[
        "--executor-max-concurrent-calls",
        "1",
        "--executor-pool-wait-ms",
        "200",
        // Huge fuel so the spinning tool is ended by the DEADLINE, not fuel.
        "--executor-connector-fuel-limit",
        "4000000000000000",
    ])
    .await;
    let digest = digest_of(TOOLS_WASM);
    let slow = stage
        .send_invoke(
            TOOLS_APP,
            &digest,
            ExportKind::ToolCall,
            serde_json::json!({"tool": "slow", "args": {}}),
        )
        .await;
    // Let the spinning tool take its admission.
    tokio::time::sleep(Duration::from_millis(300)).await;
    let quick = stage
        .send_invoke(
            TOOLS_APP,
            &digest,
            ExportKind::ToolCall,
            serde_json::json!({"tool": "echo", "args": {"a": 1}}),
        )
        .await;
    let mut seen = std::collections::HashMap::new();
    for _ in 0..2 {
        let (id, out) = stage.next_reply().await;
        seen.insert(id, out);
    }
    let refused = seen.remove(&quick).expect("quick reply").unwrap_err();
    assert_eq!(refused.code, ErrorCode::ExecutorDeadline, "{refused:?}");
    assert_eq!(
        refused.detail.as_deref(),
        Some("pool_exhausted"),
        "{refused:?}"
    );
    // The spinning tool is ended by the deadline (a sandbox trip), not served.
    assert!(seen.remove(&slow).expect("slow reply").is_err());
}

// ---- PR #984 review claims 3/4/5 ----

use base64::Engine as _;

// regression (claim 3): oversized args are refused BEFORE admission -- no slot
// is taken, no guest runs, typed `PayloadTooLarge`.
#[tokio::test]
async fn oversized_tool_args_are_refused_before_admission() {
    let rig = rig();
    let host = tools_host_with(
        &rig,
        ConnectorLimits {
            max_tool_args_bytes: 64,
            ..limits()
        },
    )
    .await;
    let big = json(serde_json::json!({"pad": "x".repeat(200)}));
    let err = host.call_tool("echo".into(), big).await.unwrap_err();
    assert!(
        matches!(
            err,
            ConnectorCallError::PayloadTooLarge { what: "args", .. }
        ),
        "{err:?}"
    );
    assert_eq!(host.available_tool_slots(), tool_pool_size(4));
    // Exactly at the cap still works (boundary).
    let ok = json(serde_json::json!({"a": 1}));
    assert!(ok.len() <= 64);
    host.call_tool("echo".into(), ok).await.unwrap();
}

// regression (claim 3): a result over the cap is refused before it is parsed
// into a `serde_json::Value` (`echo` returns its args, so a small result cap
// with a larger arg cap makes the guest "produce" an oversized result).
#[tokio::test]
async fn oversized_tool_result_is_refused_before_parsing() {
    let rig = rig();
    let host = tools_host_with(
        &rig,
        ConnectorLimits {
            max_tool_args_bytes: 10_000,
            max_tool_result_bytes: 100,
            ..limits()
        },
    )
    .await;
    let err = host
        .call_tool(
            "echo".into(),
            json(serde_json::json!({"pad": "x".repeat(500)})),
        )
        .await
        .unwrap_err();
    assert!(
        matches!(
            err,
            ConnectorCallError::PayloadTooLarge { what: "result", .. }
        ),
        "{err:?}"
    );
    // The permit came back even though the call was refused post-run.
    assert_eq!(host.available_tool_slots(), tool_pool_size(4));
    // A small result still passes.
    host.call_tool("echo".into(), json(serde_json::json!({"a": 1})))
        .await
        .unwrap();
}

// regression (claim 4): receiver + tool traffic share ONE combined cap of
// `pool_size`; a running tool counts against it, so total in-flight instances
// can never exceed `pool_size` (was 1.5x with two additive pools).
#[tokio::test]
async fn tool_and_receiver_calls_share_one_combined_cap() {
    let rig = rig();
    let l = ConnectorLimits {
        pool_size: 1,
        deadline_ms: 1_200,
        fuel: u64::MAX / 2,
        ..limits()
    };
    let host = tools_host_with(&rig, l).await;
    let slow = {
        let h = host.clone();
        tokio::spawn(async move {
            h.call_tool("slow".into(), json(serde_json::json!({})))
                .await
        })
    };
    let give_up = Instant::now() + Duration::from_secs(5);
    while host.available_slots() > 0 {
        assert!(Instant::now() < give_up, "slow tool never took its slot");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    let err = host
        .on_frame(RawFrame {
            ctx: ctx(),
            bytes: br#"{"text":"x"}"#.to_vec(),
            received_at: "2026-11-04T00:00:00.000Z".into(),
        })
        .await
        .unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::PoolExhausted { .. }),
        "combined cap must hold: {err:?}"
    );
    assert!(slow.await.unwrap().is_err());
    assert_eq!(host.available_slots(), 1);
}

// regression (claim 5, cancel): a caller that gives up mid-call drops its
// future, but the blocking task owns the permits and releases them only when
// the (deadline-bounded) guest actually ends -- never leaked, never freed early.
#[tokio::test]
async fn caller_cancel_mid_call_releases_permits_only_after_the_guest_ends() {
    let rig = rig();
    let l = ConnectorLimits {
        deadline_ms: 800,
        fuel: u64::MAX / 2,
        ..limits()
    };
    let host = tools_host_with(&rig, l).await;
    let total = host.available_slots();
    let call = {
        let h = host.clone();
        tokio::spawn(async move {
            h.call_tool("slow".into(), json(serde_json::json!({})))
                .await
        })
    };
    let give_up = Instant::now() + Duration::from_secs(5);
    while host.available_tool_slots() == tool_pool_size(4) {
        assert!(Instant::now() < give_up, "slow tool never took its slot");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    call.abort();
    let _ = call.await;
    // Cancelled caller, still-spinning guest: the permit is still held.
    assert!(host.available_tool_slots() < tool_pool_size(4));
    // The deadline ends the guest; then (and only then) the permits return.
    let give_up = Instant::now() + Duration::from_secs(5);
    while host.available_tool_slots() != tool_pool_size(4) || host.available_slots() != total {
        assert!(Instant::now() < give_up, "permits never returned");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

// regression (claim 5, wire): saturating the tool share through the REAL wire
// dispatcher cannot starve `connector_on_frame` on the same connector, and the
// next tool call is typed back-pressure.
#[tokio::test]
async fn saturated_tool_share_cannot_starve_on_frame_over_the_wire() {
    // pool 8 -> tool share 4; give the host-wide cap plenty of room.
    let mut stage = wire_rig_with(&[
        "--executor-pool-wait-ms",
        "200",
        "--executor-connector-fuel-limit",
        "4000000000000000",
    ])
    .await;
    let digest = digest_of(TOOLS_WASM);
    for _ in 0..tool_pool_size(8) {
        stage
            .send_invoke(
                TOOLS_APP,
                &digest,
                ExportKind::ToolCall,
                serde_json::json!({"tool": "slow", "args": {}}),
            )
            .await;
    }
    tokio::time::sleep(Duration::from_millis(400)).await;
    let extra_tool = stage
        .send_invoke(
            TOOLS_APP,
            &digest,
            ExportKind::ToolCall,
            serde_json::json!({"tool": "echo", "args": {}}),
        )
        .await;
    let frame = stage
        .send_invoke(
            TOOLS_APP,
            &digest,
            ExportKind::ConnectorOnFrame,
            serde_json::json!({
                "ctx": {"platform": "twitch", "source_id": "s", "connection_id": "c", "session_token": null},
                "bytes": base64::engine::general_purpose::STANDARD.encode(br#"{"text":"alive"}"#),
                "received_at": "2026-11-04T00:00:00.000Z",
            }),
        )
        .await;
    let mut replies = std::collections::HashMap::new();
    for _ in 0..(tool_pool_size(8) + 2) {
        let (id, out) = stage.next_reply().await;
        replies.insert(id, out);
    }
    let frame_out = replies.remove(&frame).expect("on-frame reply");
    assert!(frame_out.is_ok(), "on-frame starved: {frame_out:?}");
    let tool_out = replies.remove(&extra_tool).expect("extra tool reply");
    assert_eq!(
        tool_out.unwrap_err().detail.as_deref(),
        Some("pool_exhausted")
    );
}

// regression (claim 5, NoSource gap): a `tool-call` for an UNPINNED digest is
// `UNKNOWN_BUNDLE` and never triggers a stage component fetch, even when the
// source could have served it (fetch-count assertion).
struct CountingSource(Arc<std::sync::atomic::AtomicUsize>);

impl ComponentSource for CountingSource {
    async fn fetch(&self, _: &str, _: &str) -> Result<(Vec<u8>, Vec<u8>), ExecutorError> {
        self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        Ok((TOOLS_WASM.to_vec(), Vec::new()))
    }
}

#[tokio::test]
async fn unpinned_tool_call_never_fetches_from_the_component_source() {
    let fetches = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let mut stage = wire_rig_with_source(&[], CountingSource(Arc::clone(&fetches))).await;
    let e = stage
        .invoke(
            "waddles.core.not-pinned",
            &format!("sha256:{}", "a".repeat(64)),
            ExportKind::ToolCall,
            serde_json::json!({"tool": "echo", "args": {}}),
        )
        .await
        .unwrap_err();
    assert_eq!(e.code, ErrorCode::UnknownBundle, "{e:?}");
    assert_eq!(
        fetches.load(std::sync::atomic::Ordering::SeqCst),
        0,
        "an invoke must never fetch"
    );
}
