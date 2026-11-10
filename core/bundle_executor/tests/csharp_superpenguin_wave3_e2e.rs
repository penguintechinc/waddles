//! Real-path proof for the ten wave-3 SuperPenguinTV utility ports (lastseen, deathcounter, dailycounter, quotes, raidtracker,
//! top, alias, timers, weather, viewers): each REAL `componentize-dotnet`-built component is loaded by the REAL `Executor`
//! (wasmtime, production `Linker`, frame codec) and driven through `process-stage.transform`. Only the STAGE half is simulated: a
//! small model answers every host call (flags, context, kv, identity, tool.invoke, http) and records them, so the test asserts the
//! bundle's observable behavior AND the wire contract (flag key/default, kv key charset, secret-ref-only http).
//!
//! `#[ignore]` so a plain `cargo test` needs no 7 MB artifacts; the test FAILS (never skips) when `SUPERPENGUIN_WAVE3_WASM_DIR` is
//! missing or any component is absent. Built and run by `make test-superpenguin-wave3-e2e`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;
use std::sync::Arc;

use base64::Engine;
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

const ACTOR_UUID: &str = "6f1c0a52-9d3e-4b7a-8c10-2e5d7f9a1b34";
const MENTION_UUID: &str = "22222222-2222-4222-8222-222222222222";
const WEATHER_BODY: &str = r#"{"location":{"name":"Seattle","country":"USA"},"current":{"condition":{"text":"Rain"},"temp_f":50.0,"temp_c":10.0,"humidity":90},"forecast":{"forecastday":[{"day":{"condition":{"text":"Cloudy"},"maxtemp_f":55.0,"maxtemp_c":12.8,"mintemp_f":44.0,"mintemp_c":6.7}}]}}"#;

struct WasmSource(Vec<u8>);

impl ComponentSource for WasmSource {
    async fn fetch(
        &self,
        _component_key: &str,
        _sidecar_key: &str,
    ) -> Result<(Vec<u8>, Vec<u8>), bundle_executor::error::ExecutorError> {
        Ok((self.0.clone(), b"{}".to_vec()))
    }
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

/// The simulated stage: kv carried across invokes plus the knobs each step turns.
#[derive(Default)]
struct StageModel {
    app_id: String,
    flag_key: String,
    flag_on: bool,
    message_id: String,
    now_ms: u64,
    kv: HashMap<String, Vec<u8>>,
    calls: Vec<(String, String, serde_json::Value)>,
    unexpected_calls: usize,
}

impl StageModel {
    fn answer(
        &mut self,
        capability: CapabilityKind,
        op: &str,
        args: serde_json::Value,
    ) -> HostResultBody {
        self.calls
            .push((format!("{capability:?}"), op.to_string(), args.clone()));
        let ok = |v: serde_json::Value| HostResultBody {
            result: Some(v),
            error: None,
        };
        let err = |code: &str, message: &str| HostResultBody {
            result: None,
            error: Some(HostResultError {
                code: code.to_string(),
                message: message.to_string(),
            }),
        };
        let key_of = |args: &serde_json::Value| {
            let key = args["key"].as_str().unwrap().to_string();
            assert!(
                !key.contains(':'),
                "kv key {key:?} contains ':' (host rejects it)"
            );
            key
        };
        match (capability, op) {
            (CapabilityKind::Flags, "enabled") => {
                assert_eq!(
                    args["key"], self.flag_key,
                    "the app flag must be checked under its documented key"
                );
                assert_eq!(args["default_value"], false, "the flag must default OFF");
                ok(serde_json::json!({ "enabled": self.flag_on }))
            }
            (CapabilityKind::Context, _) => ok(serde_json::json!({
                "tenant": "t1", "community": "main", "app_id": self.app_id, "feature": "f",
                "version": "1", "message_id": self.message_id, "config_json": "{}",
            })),
            (CapabilityKind::Log, _) => ok(serde_json::json!({})),
            (CapabilityKind::Clock, "now-millis") => ok(serde_json::json!(self.now_ms)),
            (CapabilityKind::Clock, "now-rfc3339") => ok(serde_json::json!("2026-11-07T00:00:00Z")),
            (CapabilityKind::Clock, "monotonic-nanos") => ok(serde_json::json!(1_u64)),
            (CapabilityKind::Kv, "get") => {
                let key = key_of(&args);
                ok(serde_json::json!({ "value": self.kv.get(&key) }))
            }
            (CapabilityKind::Kv, "set") => {
                let key = key_of(&args);
                let bytes: Vec<u8> = serde_json::from_value(args["value"].clone()).unwrap();
                self.kv.insert(key, bytes);
                ok(serde_json::json!({}))
            }
            (CapabilityKind::Kv, "delete") => {
                let key = key_of(&args);
                self.kv.remove(&key);
                ok(serde_json::json!({}))
            }
            (CapabilityKind::Kv, "increment") => {
                let key = key_of(&args);
                let current = self
                    .kv
                    .get(&key)
                    .map_or(0, |b| i64::from_le_bytes(b.clone().try_into().unwrap()));
                let next = current + args["delta"].as_i64().unwrap();
                self.kv.insert(key, next.to_le_bytes().to_vec());
                ok(serde_json::json!({ "value": next }))
            }
            (CapabilityKind::Db, "identity.resolve_actor") => {
                ok(serde_json::json!({ "user": ACTOR_UUID }))
            }
            (CapabilityKind::Db, "identity.resolve_mention") => {
                ok(serde_json::json!({ "user": MENTION_UUID }))
            }
            (CapabilityKind::Db, "tool.invoke") => match (
                args["target"].as_str().unwrap(),
                args["tool"].as_str().unwrap(),
            ) {
                ("waddles.core.service.economy", "points_leaders") => {
                    ok(serde_json::json!({ "ok": {
                    "entries": [{ "user": ACTOR_UUID, "balance": 4200 }], "alias": "stars" } }))
                }
                (target, tool) => err(
                    "fixture_unsupported",
                    &format!("unmodelled tool {target}/{tool}"),
                ),
            },
            (CapabilityKind::Http, "send") => {
                let url = args["url"].as_str().unwrap();
                assert_eq!(args["method"], "GET");
                assert!(
                    url.starts_with("https://api.weatherapi.com/v1/forecast.json?q="),
                    "declared FQDN only: {url}"
                );
                assert!(
                    !url.contains("key="),
                    "the API key must never be in the URL"
                );
                assert_eq!(
                    args["secret_refs"],
                    serde_json::json!([["key", "WEATHER_API_KEY"]]),
                    "key travels as a secret reference"
                );
                ok(serde_json::json!({
                    "status": 200, "headers": [],
                    "body_base64": base64::engine::general_purpose::STANDARD.encode(WEATHER_BODY),
                    "truncated": false,
                }))
            }
            _ => {
                self.unexpected_calls += 1;
                err("fixture_unsupported", "unexpected test-host operation")
            }
        }
    }
}

/// One invoke: the event, the model knobs for it, and the expected outcome.
#[derive(Clone, Copy)]
struct Step {
    event_type: &'static str,
    actor: &'static str,
    text: &'static str,
    /// Overrides the chat payload entirely (non-chat events).
    payload: Option<&'static str>,
    moderator: bool,
    flag_on: bool,
    message_id: &'static str,
    advance_ms: u64,
    /// `None` = must be silent; `Some(s)` = reply must contain `s`.
    expect: Option<&'static str>,
}

impl Step {
    fn chat(text: &'static str, message_id: &'static str, expect: Option<&'static str>) -> Self {
        Step {
            event_type: "chat.message",
            actor: "viewer-1",
            text,
            payload: None,
            moderator: false,
            flag_on: true,
            message_id,
            advance_ms: 0,
            expect,
        }
    }
    fn moderator(mut self) -> Self {
        self.moderator = true;
        self
    }
    fn advance(mut self, ms: u64) -> Self {
        self.advance_ms = ms;
        self
    }
}

struct Run {
    trapped: bool,
    reply: Option<serde_json::Value>,
    calls: Vec<(String, String, serde_json::Value)>,
}

async fn run_steps(app: &str, wasm: Vec<u8>, steps: &[Step]) -> Vec<Run> {
    let app_id = format!("waddles.integrations.superpenguin.{app}");
    let flag_key = format!("waddles.command-superpenguin-{app}");
    let (executor_io, stage_io) = tokio::io::duplex(256 * 1024);
    let digest = format!("sha256:{:x}", Sha256::digest(&wasm));
    let executor =
        Arc::new(Executor::new(&test_config(), WasmSource(wasm)).expect("executor builds"));
    let owned: Vec<(Step, String)> = steps
        .iter()
        .map(|s| {
            let payload = s.payload.map(str::to_string).unwrap_or_else(|| {
                serde_json::json!({ "text": s.text, "channel_id": "chan-1", "is_mod": s.moderator, "is_broadcaster": false }).to_string()
            });
            (*s, payload)
        })
        .collect();

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
        .unwrap();
        write_frame(
            &mut io,
            &Frame::new(
                1,
                Message::Load(LoadBody {
                    tenant_id: 1,
                    community_id: 7,
                    app_id: app_id.clone(),
                    version: "1".to_string(),
                    digest: digest.clone(),
                    component_key: "k".to_string(),
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
        .unwrap();
        let loaded = read_frame(&mut io).await.expect("loaded reply");
        assert!(
            matches!(loaded.message, Message::Loaded(_)),
            "component must load, got {:?}",
            loaded.message
        );

        let mut model = StageModel {
            app_id: app_id.clone(),
            flag_key,
            now_ms: 1_700_000_000_000,
            ..Default::default()
        };
        let mut runs = Vec::new();
        for (i, (step, payload_json)) in owned.into_iter().enumerate() {
            model.calls.clear();
            model.flag_on = step.flag_on;
            model.message_id = step.message_id.to_string();
            model.now_ms += step.advance_ms;
            write_frame(&mut io, &Frame::new(10 + i as u64, Message::Invoke(InvokeBody {
                app_id: app_id.clone(), digest: digest.clone(), export: ExportKind::Transform,
                payload: serde_json::json!({
                    "platform": "twitch", "event_type": step.event_type, "actor": step.actor,
                    "payload_json": payload_json, "occurred_at": "2026-11-07T00:00:00.000Z",
                }),
                deadline_ms: 30000, trace: None,
            }))).await.unwrap();
            let result = loop {
                let frame = read_frame(&mut io)
                    .await
                    .expect("host-call or result frame");
                match frame.message {
                    Message::Result(body) => break Some(body),
                    Message::Error(body) => {
                        assert_eq!(format!("{:?}", body.code), "WasmTrap");
                        break None;
                    }
                    Message::HostCall(call) => {
                        let body = model.answer(call.capability, &call.op, call.args);
                        write_frame(&mut io, &Frame::new(frame.id, Message::HostResult(body)))
                            .await
                            .unwrap();
                    }
                    other => panic!("expected a host-call or result, got {other:?}"),
                }
            };
            let trapped = result.is_none();
            let reply = match result {
                Some(r) if !r.payload.is_null() => {
                    let inner = r.payload["payload_json"]
                        .as_str()
                        .expect("payload_json string");
                    Some(serde_json::from_str(inner).expect("reply json"))
                }
                _ => None,
            };
            runs.push(Run {
                reply,
                calls: model.calls.clone(),
                trapped,
            });
        }
        write_frame(
            &mut io,
            &Frame::new(9999, Message::Shutdown(ShutdownBody { grace_ms: 100 })),
        )
        .await
        .unwrap();
        (runs, model.unexpected_calls)
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

    let (runs, unexpected) = stage.await.expect("stage task");
    executor_task
        .await
        .expect("executor task")
        .expect("connection ran to a clean shutdown");
    assert_eq!(
        unexpected, 0,
        "test host must model every capability; caught refusals cannot hide missing coverage"
    );
    runs
}

fn text_of(run: &Run) -> String {
    run.reply
        .as_ref()
        .expect("a reply")
        .get("text")
        .and_then(|t| t.as_str())
        .expect("reply text")
        .to_string()
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "build all wave-3 C# artifacts and set SUPERPENGUIN_WAVE3_WASM_DIR (make test-superpenguin-wave3-e2e)"]
async fn wave3_ports_run_flag_gated_replay_safe_and_fail_loud_through_the_real_executor() {
    let directory = std::env::var("SUPERPENGUIN_WAVE3_WASM_DIR")
        .expect("SUPERPENGUIN_WAVE3_WASM_DIR is required");
    let raid = Step {
        event_type: "channel.raid",
        actor: "{user:raider-1}",
        text: "",
        moderator: false,
        flag_on: true,
        message_id: "raid-1",
        payload: Some(r#"{"metadata":{"viewers":12}}"#),
        advance_ms: 0,
        expect: None,
    };
    let cases: Vec<(&str, Vec<Step>)> = vec![
        (
            "lastseen",
            vec![
                Step::chat("!lastseen", "a", Some("Have never seen")),
                Step::chat("!lastseen", "b", Some("was last seen")).advance(125_000),
            ],
        ),
        (
            "deathcounter",
            vec![
                Step::chat("!death +", "a", Some("has died 1 times")).moderator(),
                Step::chat("!death +", "a", Some("has died 1 times")).moderator(),
                Step::chat("!death +", "b", Some("has died 1 times")),
            ],
        ),
        (
            "dailycounter",
            vec![Step::chat(
                "!dailycounter",
                "a",
                Some("Daily count: 0 (next goal: 10)"),
            )],
        ),
        (
            "quotes",
            vec![
                Step::chat("!addquote hello wasm", "a", Some("Quote #1 added.")).moderator(),
                Step::chat("!addquote hello wasm", "a", Some("Quote #1 added.")).moderator(),
                Step::chat("!quote 1", "b", Some("hello wasm")),
                Step::chat("!quote 2", "c", Some("Quote #2 not found.")),
            ],
        ),
        (
            "raidtracker",
            vec![
                raid,
                Step::chat("!raids", "b", Some("{user:raider-1} 1 raids (12 viewers)")),
            ],
        ),
        (
            "top",
            vec![Step::chat(
                "!top5",
                "a",
                Some("Top 5 in stars: 1. {user:6f1c0a52-9d3e-4b7a-8c10-2e5d7f9a1b34} 4,200"),
            )],
        ),
        (
            "alias",
            vec![
                Step::chat(
                    "!alias set hi !hello",
                    "a",
                    Some("Alias !hi now runs !hello."),
                )
                .moderator(),
                Step::chat("!hi there", "b", Some("!hello there")),
                Step::chat("!alias set bye !alias", "c", Some("cannot point at")).moderator(),
            ],
        ),
        (
            "timers",
            vec![
                Step::chat(
                    "!timer create news 10 10 0",
                    "a",
                    Some("Timer news created"),
                )
                .moderator(),
                Step::chat(
                    "!timer addline news Follow the stream",
                    "b",
                    Some("Line 1 added"),
                )
                .moderator(),
                Step::chat("just chatting", "c", None),
                Step::chat("just chatting", "d", Some("Follow the stream")).advance(11_000),
            ],
        ),
        (
            "weather",
            vec![Step::chat(
                "!weather Seattle",
                "a",
                Some("The weather in Seattle, USA is Rain 50F/10C. Humidity: 90%."),
            )],
        ),
        (
            "viewers",
            vec![
                Step::chat("!lurk", "a", None),
                Step::chat("!viewers", "b", Some("Active viewers: 1 (1 lurking)")),
            ],
        ),
    ];

    let mut examined = 0;
    for (app, steps) in cases {
        let path = std::path::Path::new(&directory).join(format!("superpenguin_{app}.wasm"));
        let wasm = std::fs::read(&path).unwrap_or_else(|e| {
            panic!("{app}: a real built component is required ({path:?}): {e}")
        });
        assert!(wasm.len() > 1_000_000, "{app}: implausibly small component");

        // Flag OFF first: same first event, nothing but the flag check may happen, and no reply.
        let probe = Step {
            flag_on: false,
            expect: None,
            ..steps[0]
        };
        let off = run_steps(app, wasm.clone(), &[probe]).await;
        assert!(
            !off[0].trapped && off[0].reply.is_none(),
            "{app}: flag-off must be silent"
        );
        assert!(
            off[0].calls.iter().all(|(c, op, _)| c == "Context"
                || c == "Log"
                || (c == "Flags" && op == "enabled")),
            "{app}: flag-off must stop after the flag check, saw {:?}",
            off[0].calls
        );

        let runs = run_steps(app, wasm, &steps).await;
        for (i, (run, step)) in runs.iter().zip(steps.iter()).enumerate() {
            assert!(!run.trapped, "{app} step {i}: must not trap");
            match step.expect {
                Some(needle) => {
                    let text = text_of(run);
                    assert!(
                        text.contains(needle),
                        "{app} step {i}: expected {needle:?} in {text:?}"
                    );
                    assert_eq!(
                        run.reply.as_ref().unwrap()["channel_id"],
                        "chan-1",
                        "{app}: reply keeps the origin channel"
                    );
                }
                None => assert!(
                    run.reply.is_none(),
                    "{app} step {i}: must be silent, got {:?}",
                    run.reply
                ),
            }
        }
        examined += 1;
        println!(
            "C# wave-3 real-executor matrix: {app} PASS ({} steps)",
            steps.len()
        );
    }
    assert_eq!(examined, 10, "zero or partial port coverage must fail");
}
