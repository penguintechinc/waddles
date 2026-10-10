//! Shared harness for the `stage-next` `reputation`, `economy` and `identity`
//! integration tests (`stage_next_reputation.rs` -- Rust fixture;
//! `stage_next_python_reputation_e2e.rs` -- freshly built Python bundle). The executor side is entirely real
//! (`Executor`, wasmtime, the production `Linker`, the frame codec); only the
//! STAGE half of the connection is simulated -- it answers the single
//! `reputation.*` host-call a bundle makes per invoke with a canned
//! host-result or a gate-style denial, and reports back exactly what it
//! observed on the wire.

#![allow(dead_code, clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bundle_executor::config::CliConfig;
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::{ComponentSource, Executor};
use bundle_executor::wire::run_connection;
use penguin_bundle_host::wire::{
    read_frame, write_frame, CapabilityKind, ExportKind, Frame, HelloBody, HelloLimits,
    HelloOkBody, HostResultBody, HostResultError, InvokeBody, LoadBody, LoadLimits, Message,
    SandboxInfo, ShutdownBody,
};
use sha2::{Digest, Sha256};

pub const TARGET_USER: &str = "11111111-2222-4333-8444-555555555555";

/// Hands back one component's bytes regardless of key.
pub struct WasmSource(pub Vec<u8>);

impl ComponentSource for WasmSource {
    async fn fetch(
        &self,
        _component_key: &str,
        _sidecar_key: &str,
    ) -> Result<(Vec<u8>, Vec<u8>), bundle_executor::error::ExecutorError> {
        // No `bundle_signing_public_keys` configured below, so the `{}` stub
        // sidecar is never parsed (same convention as host_bridge_integration).
        Ok((self.0.clone(), b"{}".to_vec()))
    }
}

pub fn digest_of(wasm: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(wasm);
    format!("sha256:{:x}", hasher.finalize())
}

pub fn test_config() -> CliConfig {
    use clap::Parser;
    CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-process:8301",
    ])
    .expect("static test args always parse")
}

/// One scripted stage answer: a host-result value, or a `(code, message)` refusal.
pub type ScriptedAnswer = Result<serde_json::Value, (&'static str, &'static str)>;

/// What the simulated stage answers to a `reputation.*` / `economy.*` /
/// `identity.*` host-call.
#[derive(Clone)]
pub enum StageAnswer {
    Ok(serde_json::Value),
    Err(&'static str, &'static str),
    /// For a bundle that makes SEVERAL host-calls in one invoke (e.g. a game
    /// resolving an actor, then a mention, then moving currency): answers each
    /// call by its EXACT op. A call whose op has no entry fails the test loudly
    /// (a bundle making a call the scenario did not script is a finding).
    PerOp(Vec<(&'static str, ScriptedAnswer)>),
}

impl StageAnswer {
    /// The `HostResultBody` the simulated stage replies with for `op`.
    fn body_for(&self, op: &str) -> HostResultBody {
        let one = |answer: &ScriptedAnswer| match answer {
            Ok(v) => HostResultBody {
                result: Some(v.clone()),
                error: None,
            },
            Err((code, message)) => HostResultBody {
                result: None,
                error: Some(HostResultError {
                    code: (*code).to_string(),
                    message: (*message).to_string(),
                }),
            },
        };
        match self {
            Self::Ok(v) => one(&Ok(v.clone())),
            Self::Err(code, message) => one(&Err((*code, *message))),
            Self::PerOp(table) => {
                let answer = table
                    .iter()
                    .find(|(scripted, _)| *scripted == op)
                    .unwrap_or_else(|| panic!("no scripted stage answer for host-call {op:?}"));
                one(&answer.1)
            }
        }
    }
}

/// One `transform` invoke: the event the bundle receives and how the stage
/// answers the `reputation.*` call it makes.
#[derive(Clone)]
pub struct Scenario {
    pub event_type: &'static str,
    pub payload_json: String,
    pub answer: StageAnswer,
}

/// What the stage observed for one [`Scenario`]:
/// `(host-call op, host-call args, guest result tag)` -- the LAST capability
/// call the bundle made.
pub type Observed = (String, serde_json::Value, String);

/// Every capability host-call one invoke made, in order (`(op, args)`), plus
/// the guest's result tag.
pub type ObservedAll = (Vec<(String, serde_json::Value)>, String);

/// Runs load + one `transform` invoke against `wasm` (a single scenario).
pub async fn run_one(
    wasm: &[u8],
    app_id: &'static str,
    memory_mb: u32,
    event_type: &'static str,
    payload_json: String,
    answer: StageAnswer,
) -> Observed {
    run_many(
        wasm,
        app_id,
        memory_mb,
        vec![Scenario {
            event_type,
            payload_json,
            answer,
        }],
    )
    .await
    .remove(0)
}

/// Runs ONE load (one compile) and then one `transform` invoke per scenario,
/// in order, on the same connection -- so an expensive component (CPython on
/// WASI) is compiled once per test, not once per scenario.
pub async fn run_many(
    wasm: &[u8],
    app_id: &'static str,
    memory_mb: u32,
    scenarios: Vec<Scenario>,
) -> Vec<Observed> {
    run_many_detailed(wasm, app_id, memory_mb, scenarios)
        .await
        .into_iter()
        .map(|(mut calls, result)| {
            let (op, args) = calls
                .pop()
                .expect("the bundle must have made a reputation.*/economy.*/identity.* call");
            (op, args, result)
        })
        .collect()
}

/// [`run_many`], but reporting EVERY capability host-call each invoke made (in
/// order) instead of only the last -- for bundles that compose several imports.
pub async fn run_many_detailed(
    wasm: &[u8],
    app_id: &'static str,
    memory_mb: u32,
    scenarios: Vec<Scenario>,
) -> Vec<ObservedAll> {
    let (executor_io, stage_io) = tokio::io::duplex(256 * 1024);
    let executor = Arc::new(
        Executor::new(&test_config(), WasmSource(wasm.to_vec())).expect("executor builds"),
    );
    let digest = digest_of(wasm);

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
                        memory_mb,
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
                    app_id: app_id.to_string(),
                    version: "1".to_string(),
                    digest: digest.clone(),
                    component_key: "k".to_string(),
                    sidecar_key: "s".to_string(),
                    capabilities: vec![],
                    limits: LoadLimits {
                        timeout_ms: 5000,
                        memory_mb,
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

        let mut observed = Vec::new();
        for (i, scenario) in scenarios.into_iter().enumerate() {
            write_frame(
                &mut io,
                &Frame::new(
                    2 + i as u64,
                    Message::Invoke(InvokeBody {
                        app_id: app_id.to_string(),
                        digest: digest.clone(),
                        export: ExportKind::Transform,
                        payload: serde_json::json!({
                            "platform": "test",
                            "event_type": scenario.event_type,
                            "actor": null,
                            "payload_json": scenario.payload_json,
                            "occurred_at": "2026-10-09T00:00:00.000Z",
                        }),
                        deadline_ms: 60000,
                        trace: None,
                    }),
                ),
            )
            .await
            .expect("write invoke");

            let mut cap_calls: Vec<(String, serde_json::Value)> = Vec::new();
            let result = loop {
                let frame = read_frame(&mut io)
                    .await
                    .expect("host-call or result frame");
                match frame.message {
                    Message::Result(body) => break body,
                    Message::HostCall(call)
                        if call.op.starts_with("reputation.")
                            || call.op.starts_with("economy.")
                            || call.op.starts_with("identity.") =>
                    {
                        assert_eq!(
                            call.capability,
                            CapabilityKind::Db,
                            "reputation/economy/identity ride the db wire kind"
                        );
                        let body = scenario.answer.body_for(&call.op);
                        write_frame(&mut io, &Frame::new(frame.id, Message::HostResult(body)))
                            .await
                            .expect("write host-result");
                        cap_calls.push((call.op, call.args));
                    }
                    Message::HostCall(call) => {
                        // Always-granted housekeeping calls (the Python entry's
                        // `context.get-context`, `log.write`).
                        let result = match (call.capability, call.op.as_str()) {
                            (CapabilityKind::Context, _) => serde_json::json!({
                                "tenant": "t1", "community": "main", "app_id": app_id,
                                "feature": "f", "version": "1", "message_id": "m1",
                                "config_json": "{}",
                            }),
                            (CapabilityKind::Log, _) => serde_json::json!({}),
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
                        .expect("write housekeeping host-result");
                    }
                    other => panic!("expected a host-call or result, got {other:?}"),
                }
            };
            assert!(
                !cap_calls.is_empty(),
                "the bundle must have made a reputation.*/economy.*/identity.* call"
            );
            let payload_json = result.payload["payload_json"]
                .as_str()
                .expect("payload_json is a string");
            let parsed: serde_json::Value =
                serde_json::from_str(payload_json).expect("guest result json");
            observed.push((
                cap_calls,
                parsed["result"].as_str().expect("result tag").to_string(),
            ));
        }

        write_frame(
            &mut io,
            &Frame::new(9999, Message::Shutdown(ShutdownBody { grace_ms: 100 })),
        )
        .await
        .expect("write shutdown");
        observed
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

    let observed = stage.await.expect("stage task");
    executor_task
        .await
        .expect("executor task")
        .expect("connection ran to a clean shutdown");
    observed
}
