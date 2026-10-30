//! `world stage-tools` (P3b Task 1): the new world type-checks, its linker arm
//! registers the `tool` host import, and a minimal real component (imports
//! `tool.invoke`, exports `tool-provider.call`) pre-links against it. Also
//! pins the isolation direction: a `stage-v2` linker never registers `tool`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use bundle_executor::config::CliConfig;
use bundle_executor::engine::{build_engine, build_linker_for, stage_tools_world};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use common::test_config;
use wasmtime::component::Component;

const FIXTURE: &[u8] = include_bytes!("fixtures/stage_tools_fixture.wasm");

fn manifest(world: WitWorld) -> VerifiedManifest {
    VerifiedManifest::new(
        "waddles.test.stage-tools-fixture",
        "sha256:aa",
        world,
        Default::default(),
    )
}

fn cfg() -> CliConfig {
    test_config()
}

#[test]
fn stage_tools_fixture_pre_links_against_the_stage_tools_linker() {
    let engine = build_engine(&cfg()).unwrap();
    let linker = build_linker_for(&engine, &manifest(WitWorld::StageTools)).unwrap();
    let component = Component::new(&engine, FIXTURE).unwrap();
    let pre = linker.instantiate_pre(&component).unwrap();
    // Typed pre-link: proves the component exports exactly `tool-provider`.
    stage_tools_world::StageToolsPre::new(pre).unwrap();
}

#[test]
fn stage_v2_linker_does_not_register_tool_import() {
    let engine = build_engine(&cfg()).unwrap();
    let linker = build_linker_for(&engine, &manifest(WitWorld::StageV2)).unwrap();
    let component = Component::new(&engine, FIXTURE).unwrap();
    assert!(
        linker.instantiate_pre(&component).is_err(),
        "a stage-v2 linker must not satisfy the `tool` import"
    );
}

// ---- P3b Task 2: `ExportKind::ToolCall` runs the provider's exported tool ----

use std::sync::Arc;

use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::Executor;
use bundle_executor::wire::run_connection;
use common::{digest_of, WasmSource};
use penguin_bundle_host::wire::{
    read_frame, write_frame, ErrorCode, ExportKind, Frame, HelloBody, HelloLimits, HelloOkBody,
    InvokeBody, LoadBody, LoadLimits, Message, SandboxInfo, ShutdownBody,
};

const APP: &str = "waddles.test.stage-tools-fixture";

/// Loads the fixture then runs one `ToolCall` per payload; returns each outcome.
async fn tool_calls(
    payloads: Vec<serde_json::Value>,
) -> Vec<Result<penguin_bundle_host::wire::ResultBody, (ErrorCode, String)>> {
    let (executor_io, stage_io) = tokio::io::duplex(256 * 1024);
    let executor =
        Arc::new(Executor::new(&test_config(), WasmSource(FIXTURE.to_vec())).expect("executor"));
    let stage = tokio::spawn(async move {
        let mut io = stage_io;
        let hello = read_frame(&mut io).await.unwrap();
        let ok = HelloOkBody {
            stage: "svc-process".into(),
            protocol_version: 1,
            limits: HelloLimits {
                call_timeout_ms: 5000,
                memory_mb: 64,
                max_concurrent_calls: 32,
            },
        };
        write_frame(&mut io, &Frame::new(hello.id, Message::HelloOk(ok)))
            .await
            .unwrap();
        let load = LoadBody {
            tenant_id: 1,
            community_id: 0,
            app_id: APP.into(),
            version: "1".into(),
            digest: digest_of(FIXTURE),
            component_key: APP.into(),
            sidecar_key: "s".into(),
            capabilities: vec![],
            limits: LoadLimits {
                timeout_ms: 5000,
                memory_mb: 64,
            },
        };
        write_frame(&mut io, &Frame::new(1, Message::Load(load)))
            .await
            .unwrap();
        let reply = read_frame(&mut io).await.unwrap();
        assert!(
            matches!(reply.message, Message::Loaded(_)),
            "{:?}",
            reply.message
        );
        let mut out = Vec::new();
        for (i, payload) in payloads.into_iter().enumerate() {
            let body = InvokeBody {
                app_id: APP.into(),
                digest: digest_of(FIXTURE),
                export: ExportKind::ToolCall,
                payload,
                deadline_ms: 30000,
                trace: None,
            };
            write_frame(&mut io, &Frame::new(10 + i as u64, Message::Invoke(body)))
                .await
                .unwrap();
            match read_frame(&mut io).await.unwrap().message {
                Message::Result(r) => out.push(Ok(r)),
                Message::Error(e) => out.push(Err((e.code, e.message))),
                other => panic!("unexpected frame {other:?}"),
            }
        }
        write_frame(
            &mut io,
            &Frame::new(9999, Message::Shutdown(ShutdownBody { grace_ms: 100 })),
        )
        .await
        .unwrap();
        out
    });
    let hello = HelloBody {
        protocol_version: 1,
        executor_version: "0.1.0".into(),
        wasmtime_version: bundle_executor::engine::WASMTIME_VERSION.into(),
        wasmtime_abi: bundle_executor::engine::WASMTIME_VERSION.into(),
        collector: "drc".into(),
        sandbox: SandboxInfo {
            runtime: "runc".into(),
            verified: false,
        },
    };
    let exec = tokio::spawn(run_connection(
        executor_io,
        hello,
        executor,
        "test-peer",
        Heartbeat::disabled(),
    ));
    let out = stage.await.unwrap();
    exec.await.unwrap().unwrap();
    out
}

#[tokio::test]
async fn tool_call_runs_the_providers_exported_tool_and_returns_its_result() {
    let out = tool_calls(vec![serde_json::json!({"tool": "echo", "args": "hi"})]).await;
    let r = out.into_iter().next().unwrap().expect("tool call result");
    assert_eq!(r.payload, serde_json::json!({"ok": "hi"}));
    assert!(r.fuel_used > 0, "fuel must be metered for a tool call");
}

#[tokio::test]
async fn tool_call_with_a_malformed_payload_is_refused_before_the_guest_runs() {
    let out = tool_calls(vec![serde_json::json!({"args": "no tool name"})]).await;
    let (code, _) = out.into_iter().next().unwrap().unwrap_err();
    assert_eq!(code, ErrorCode::MalformedFrame);
}
