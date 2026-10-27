//! Python proof for `world stage-v2` -- the seam `componentize-py-eager-wizen`
//! says mocked tests can never see. A Python bundle
//! (`tests/fixtures/stage-v2-py-bundle/src`) is **freshly compiled on every
//! run** with `componentize-py -w stage-v2` (pinned `python:3.13-slim-bookworm`
//! image, `waddle_sdk._component_entry`'s `ActionStageV2` + eager
//! `context_v2` import), loaded by the REAL `Executor`, and:
//!
//! * `transform` reads the authoritative numeric ids through
//!   `context-v2.get-context` -> `get_bundle_context()`;
//! * `dispatch` receives them on the v2 envelope AND in its bundle context;
//! * a dispatch frame whose ids do NOT match the loaded scope is refused with
//!   `MALFORMED_FRAME` (confused-deputy guard) and the guest is not run.
//!
//! Requires `docker` on PATH; a missing docker or a failed build FAILS the
//! test outright, never skips. Only the stage half of the connection is
//! simulated.

#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use std::sync::Arc;

use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::Executor;
use bundle_executor::wire::run_connection;
use common::{digest_of, test_config, WasmSource};
use penguin_bundle_host::wire::{
    read_frame, write_frame, CapabilityKind, ErrorCode, ExportKind, Frame, HelloBody, HelloLimits,
    HelloOkBody, HostResultBody, InvokeBody, LoadBody, LoadLimits, Message, SandboxInfo,
    ShutdownBody,
};

const APP_ID: &str = "waddles.test.stage-v2-py";

/// Builds the Python test bundle for `world stage-v2` inside the same pinned
/// image/componentize-py version production's builder uses.
fn build_fresh_wasm() -> Vec<u8> {
    const PYTHON_IMAGE: &str =
        "python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26";
    const COMPONENTIZE_PY_VERSION: &str = "0.25.1";

    let repo_root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("core/bundle_executor has a parent (core/)")
        .parent()
        .expect("core/ has a parent (repo root)")
        .to_path_buf();
    let out_dir = std::env::temp_dir().join(format!(
        "stage-v2-py-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&out_dir).expect("create temp wasm output dir");

    let build_script = format!(
        "set -eu; export PATH=/tmp/.local/bin:$PATH; \
         pip install --no-cache-dir --quiet componentize-py=={COMPONENTIZE_PY_VERSION}; \
         cd /repo; \
         PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 componentize-py \
            -d wit/waddle-bundle -w stage-v2 \
            componentize -p sdk/waddle-sdk/src \
            -p core/bundle_executor/tests/fixtures/stage-v2-py-bundle/src \
            waddle_sdk._component_entry \
            -o /out/stage-v2-py.wasm"
    );
    let status = std::process::Command::new("docker")
        .args([
            "run",
            "--rm",
            "--user",
            &format!("{}:{}", id_of("-u"), id_of("-g")),
            "-e",
            "HOME=/tmp",
            "-v",
            &format!("{}:/repo:ro", repo_root.display()),
            "-v",
            &format!("{}:/out", out_dir.display()),
            PYTHON_IMAGE,
            "sh",
            "-c",
            &build_script,
        ])
        .status()
        .unwrap_or_else(|e| {
            panic!(
                "docker must be on PATH to build the real stage-v2 Python bundle -- no \
                 mock/stub substitute is permitted at this boundary (error: {e})"
            )
        });
    assert!(
        status.success(),
        "fresh `componentize-py -w stage-v2 componentize` of the Python bundle FAILED (exit {:?})",
        status.code()
    );
    let bytes = std::fs::read(out_dir.join("stage-v2-py.wasm"))
        .expect("componentize-py reported success but the wasm is unreadable");
    assert!(!bytes.is_empty(), "freshly built wasm is empty");
    let _ = std::fs::remove_dir_all(&out_dir);
    bytes
}

/// `id <flag>` for the bind-mounted `docker run` (root-owned output in a
/// worktree must never be left behind).
fn id_of(flag: &str) -> String {
    let out = std::process::Command::new("id")
        .arg(flag)
        .output()
        .expect("`id` is available");
    String::from_utf8(out.stdout)
        .expect("utf8")
        .trim()
        .to_string()
}

fn event(event_type: &str) -> serde_json::Value {
    serde_json::json!({
        "platform": "test", "event_type": event_type, "actor": null,
        "payload_json": "{}", "occurred_at": "2026-10-14T00:00:00.000Z",
    })
}

fn v2_dispatch_payload(tenant_id: i64, community_id: i32) -> serde_json::Value {
    serde_json::json!({
        "envelope": {
            "tenant_id": tenant_id, "community_id": community_id,
            "tenant": "acme", "community": "main", "app_id": APP_ID,
            "stage": "action", "event": event("probe"),
            "ts": "2026-10-14T00:00:00.000Z", "target_app_id": null, "trace_context": null,
        },
        "config": "{}",
    })
}

type Outcome = Result<serde_json::Value, (ErrorCode, String)>;

/// Loads the bundle under scope `(10, 7)` and runs each `(export, payload)`,
/// answering the bundle's `context` host-call with ids `(10, 7)`.
async fn drive(wasm: Vec<u8>, steps: Vec<(ExportKind, serde_json::Value)>) -> Vec<Outcome> {
    let (executor_io, stage_io) = tokio::io::duplex(256 * 1024);
    let digest = digest_of(&wasm);
    let executor = Arc::new(Executor::new(&test_config(), WasmSource(wasm)).expect("executor"));

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
                        call_timeout_ms: 60000,
                        memory_mb: 256,
                        max_concurrent_calls: 32,
                    },
                }),
            ),
        )
        .await
        .expect("hello-ok");
        write_frame(
            &mut io,
            &Frame::new(
                1,
                Message::Load(LoadBody {
                    tenant_id: 10,
                    community_id: 7,
                    app_id: APP_ID.to_string(),
                    version: "1".to_string(),
                    digest: digest.clone(),
                    component_key: "k".to_string(),
                    sidecar_key: "s".to_string(),
                    capabilities: vec![],
                    limits: LoadLimits {
                        timeout_ms: 60000,
                        memory_mb: 256,
                    },
                }),
            ),
        )
        .await
        .expect("load");
        let loaded = read_frame(&mut io).await.expect("loaded reply");
        assert!(
            matches!(loaded.message, Message::Loaded(_)),
            "stage-v2 Python component must load, got {:?}",
            loaded.message
        );

        let mut out = Vec::new();
        for (i, (export, payload)) in steps.into_iter().enumerate() {
            write_frame(
                &mut io,
                &Frame::new(
                    2 + i as u64,
                    Message::Invoke(InvokeBody {
                        app_id: APP_ID.to_string(),
                        digest: digest.clone(),
                        export,
                        payload,
                        deadline_ms: 60000,
                        trace: None,
                    }),
                ),
            )
            .await
            .expect("invoke");
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
                        let result = match call.capability {
                            CapabilityKind::Context => serde_json::json!({
                                "tenant_id": 10, "community_id": 7,
                                "tenant": "acme", "community": "main", "app_id": APP_ID,
                                "feature": "f", "version": "1", "message_id": "m1",
                                "config_json": "{}",
                            }),
                            CapabilityKind::Log => serde_json::json!({}),
                            other => panic!("unexpected host-call: {other:?}"),
                        };
                        write_frame(
                            &mut io,
                            &Frame::new(
                                frame.id,
                                Message::HostResult(HostResultBody {
                                    result: Some(result),
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

/// ONE fresh build + ONE compile, then every scenario on that connection.
#[tokio::test(flavor = "multi_thread")]
async fn python_stage_v2_bundle_receives_numeric_ids_and_the_guard_holds() {
    let wasm = tokio::task::spawn_blocking(build_fresh_wasm)
        .await
        .expect("build task");
    let out = drive(
        wasm,
        vec![
            (ExportKind::Transform, event("scope")),
            (ExportKind::Dispatch, v2_dispatch_payload(10, 7)),
            // confused deputy: ids differ from the loaded (10, 7) scope
            (ExportKind::Dispatch, v2_dispatch_payload(11, 7)),
            // above i32::MAX
            (
                ExportKind::Dispatch,
                v2_dispatch_payload(i64::from(i32::MAX) + 1, 7),
            ),
        ],
    )
    .await;

    // transform: ids from context-v2 reached get_bundle_context()
    let t = out[0].as_ref().expect("transform result");
    let payload: serde_json::Value =
        serde_json::from_str(t["payload_json"].as_str().expect("payload_json")).unwrap();
    assert_eq!(payload["result"], "10:7:acme");

    // dispatch: envelope ids == bundle-context ids == (10, 7)
    let d = out[1].as_ref().expect("dispatch result");
    assert_eq!(d["provider_message_id"], "10:7");
    assert_eq!(d["detail"], "10:7|10:7");

    // guard: mismatched / oversized ids -> MALFORMED_FRAME, guest not run
    for (i, o) in out.iter().enumerate().skip(2) {
        match o {
            Err((code, _)) => assert_eq!(*code, ErrorCode::MalformedFrame, "case {i}"),
            Ok(p) => panic!("case {i} must be rejected, got {p}"),
        }
    }
}
