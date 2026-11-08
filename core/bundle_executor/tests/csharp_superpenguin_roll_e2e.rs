//! Real-path proof for the C# `superpenguin-roll` bundle on `world stage-next-tools`: the REAL
//! `componentize-dotnet`-built component (`bundles/csharp/superpenguin-roll`, NOT a stand-in) is loaded by the
//! REAL `Executor` (wasmtime, the production `Linker`, the frame codec) and driven through `process-stage.transform`.
//! Only the STAGE half of the connection is simulated: it answers each host call the bundle makes (flags, context,
//! identity, kv, economy, tool) from an in-test model and records every call, so the test asserts both the wire
//! contract (op names, args) and the bundle's behavior (wager outcome vs reply, cooldown, replay idempotency).
//!
//! The wasm is built by `make test-superpenguin-roll-e2e` (`scripts/test-superpenguin-roll-e2e.sh`) from the bundle's
//! own pinned Dockerfile and passed in `SUPERPENGUIN_ROLL_WASM`. The test is `#[ignore]` so a plain `cargo test` does not
//! need a 7 MB artifact, and FAILS (never skips) when that variable is missing or unreadable.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;
use std::sync::Arc;

use bundle_executor::config::CliConfig;
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::{ComponentSource, Executor};
use bundle_executor::wire::run_connection;
use penguin_bundle_host::wire::{
    CapabilityKind, ExportKind, Frame, HelloBody, HelloLimits, HelloOkBody, HostResultBody,
    HostResultError, InvokeBody, LoadBody, LoadLimits, Message, SandboxInfo, ShutdownBody,
    read_frame, write_frame,
};
use sha2::{Digest, Sha256};

const APP_ID: &str = "waddles.integrations.superpenguin.roll";
const ACTOR_UUID: &str = "6f1c0a52-9d3e-4b7a-8c10-2e5d7f9a1b34";
const FLAG_KEY: &str = "waddles.command-superpenguin-roll";

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

fn load_wasm() -> Vec<u8> {
    let path = std::env::var("SUPERPENGUIN_ROLL_WASM").expect(
        "SUPERPENGUIN_ROLL_WASM must point at the built superpenguin_roll.wasm \
         (run `make test-superpenguin-roll-e2e`) -- a missing artifact FAILS, it never skips",
    );
    let bytes = std::fs::read(&path).unwrap_or_else(|e| panic!("cannot read {path}: {e}"));
    assert!(
        bytes.len() > 1_000_000,
        "{path} is implausibly small ({} bytes)",
        bytes.len()
    );
    bytes
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

/// The simulated stage: a tiny model of kv + the knobs each scenario turns.
#[derive(Default)]
struct StageModel {
    app_id: String,
    flag_key: String,
    flag_on: bool,
    message_id: String,
    kv: HashMap<String, Vec<u8>>,
    /// `Some((code, message))` makes `economy.wager` refuse like the host gate would.
    wager_refusal: Option<(&'static str, &'static str)>,
    max_bet: i64,
    balance_after_wager: i64,
    alias: &'static str,
    /// Every capability host call made, in order: `(capability, op, args)`.
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
            (CapabilityKind::Clock, "now-millis") => ok(serde_json::json!(1_700_000_000_000_u64)),
            (CapabilityKind::Clock, "now-rfc3339") => ok(serde_json::json!("2026-11-07T00:00:00Z")),
            (CapabilityKind::Clock, "monotonic-nanos") => ok(serde_json::json!(1_u64)),
            (CapabilityKind::Kv, "get") => {
                let key = args["key"].as_str().unwrap().to_string();
                assert!(
                    !key.contains(':'),
                    "kv key {key:?} contains ':' (host rejects it)"
                );
                ok(serde_json::json!({ "value": self.kv.get(&key) }))
            }
            (CapabilityKind::Kv, "set") => {
                let key = args["key"].as_str().unwrap().to_string();
                assert!(
                    !key.contains(':'),
                    "kv key {key:?} contains ':' (host rejects it)"
                );
                let bytes: Vec<u8> = serde_json::from_value(args["value"].clone()).unwrap();
                self.kv.insert(key, bytes);
                ok(serde_json::json!({}))
            }
            (CapabilityKind::Kv, "delete") => {
                self.kv.remove(args["key"].as_str().unwrap());
                ok(serde_json::json!({}))
            }
            (CapabilityKind::Kv, "increment") => {
                let key = args["key"].as_str().unwrap().to_string();
                assert!(
                    !key.contains(':'),
                    "kv key {key:?} contains ':' (host rejects it)"
                );
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
                ok(serde_json::json!({ "user": "22222222-2222-4222-8222-222222222222" }))
            }
            (CapabilityKind::Db, "economy.max_bet") => {
                ok(serde_json::json!({ "max_bet": self.max_bet }))
            }
            (CapabilityKind::Db, "economy.wager") => match self.wager_refusal {
                Some((code, message)) => err(code, message),
                None => ok(serde_json::json!({ "balance": self.balance_after_wager })),
            },
            (CapabilityKind::Db, "tool.invoke") => {
                assert_eq!(args["target"], "waddles.core.service.economy");
                let tool = args["tool"].as_str().unwrap();
                assert!(matches!(
                    tool,
                    "points_view" | "get_balance" | "adjust_balance"
                ));
                let user = args["args"]["user"].as_str().unwrap();
                assert!(matches!(
                    user,
                    ACTOR_UUID | "22222222-2222-4222-8222-222222222222"
                ));
                if tool == "adjust_balance" {
                    assert!(args["args"]["delta"].as_i64().is_some());
                    let key = args["idempotency_key"]
                        .as_str()
                        .expect("service writes require a retry key");
                    assert!(!key.is_empty() && key.len() <= 128);
                }
                ok(
                    serde_json::json!({ "ok": { "user": user, "balance": 1234, "alias": self.alias } }),
                )
            }
            _ => {
                // Return a real refusal so the guest can finish and the wire can shut down.
                // The harness fails below even if a guest catches this refusal.
                self.unexpected_calls += 1;
                err("fixture_unsupported", "unexpected test-host operation")
            }
        }
    }
}

struct Run {
    trapped: bool,
    reply: Option<serde_json::Value>,
    calls: Vec<(String, String, serde_json::Value)>,
}

/// One load + a sequence of `transform` invokes on one connection, the stage model carrying kv between them.
async fn run(
    wasm: Vec<u8>,
    steps: Vec<(&'static str, Box<dyn FnOnce(&mut StageModel) + Send>)>,
) -> Vec<Run> {
    run_for(APP_ID.into(), FLAG_KEY.into(), wasm, steps).await
}

async fn run_for(
    app_id: String,
    flag_key: String,
    wasm: Vec<u8>,
    steps: Vec<(&'static str, Box<dyn FnOnce(&mut StageModel) + Send>)>,
) -> Vec<Run> {
    let (executor_io, stage_io) = tokio::io::duplex(256 * 1024);
    let digest = format!("sha256:{:x}", Sha256::digest(&wasm));
    let executor =
        Arc::new(Executor::new(&test_config(), WasmSource(wasm)).expect("executor builds"));

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
            max_bet: 500,
            balance_after_wager: 1234,
            alias: "stars",
            ..Default::default()
        };
        let mut runs = Vec::new();
        for (i, (text, prepare)) in steps.into_iter().enumerate() {
            model.calls.clear();
            prepare(&mut model);
            let payload_json =
                serde_json::json!({ "text": text, "channel_id": "chan-1" }).to_string();
            write_frame(
                &mut io,
                &Frame::new(
                    10 + i as u64,
                    Message::Invoke(InvokeBody {
                        app_id: app_id.clone(),
                        digest: digest.clone(),
                        export: ExportKind::Transform,
                        payload: serde_json::json!({
                            "platform": "twitch", "event_type": "chat.message", "actor": "viewer-1",
                            "payload_json": payload_json, "occurred_at": "2026-11-07T00:00:00.000Z",
                        }),
                        deadline_ms: 30000,
                        trace: None,
                    }),
                ),
            )
            .await
            .unwrap();
            let result = loop {
                let frame = read_frame(&mut io)
                    .await
                    .expect("host-call or result frame");
                match frame.message {
                    Message::Result(body) => break Some(body),
                    // An unhandled guest exception surfaces as a WasmTrap error frame: the loud-failure path.
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

    let (runs, unexpected_calls) = stage.await.expect("stage task");
    executor_task
        .await
        .expect("executor task")
        .expect("connection ran to a clean shutdown");
    assert_eq!(
        unexpected_calls, 0,
        "test host must model every capability; caught refusals cannot hide missing coverage"
    );
    runs
}

fn ops(run: &Run) -> Vec<&str> {
    run.calls.iter().map(|(_, op, _)| op.as_str()).collect()
}

fn reply_text(run: &Run) -> String {
    run.reply
        .as_ref()
        .expect("a reply")
        .get("text")
        .and_then(|t| t.as_str())
        .expect("reply text")
        .to_string()
}

/// Pulls the two dice out of `... rolls a [d1] and [d2]. ...`.
fn dice(text: &str) -> (u64, u64) {
    let rest = text.split("rolls a [").nth(1).expect("dice in reply");
    let d1: u64 = rest[..1].parse().unwrap();
    let d2: u64 = rest.split("and [").nth(1).unwrap()[..1].parse().unwrap();
    (d1, d2)
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "needs the built C# wasm: run `make test-superpenguin-roll-e2e`"]
async fn csharp_roll_bundle_wagers_replays_and_fails_loud_through_the_real_executor() {
    let wasm = load_wasm();
    type Step = (&'static str, Box<dyn FnOnce(&mut StageModel) + Send>);
    let steps: Vec<Step> = vec![
        // 0: flag OFF -> no reply, no economy/identity/kv traffic, and the flag key/default asserted in the model.
        (
            "!roll",
            Box::new(|m| {
                m.flag_on = false;
                m.message_id = "evt-0".into();
            }),
        ),
        // 1: fresh roll, stake 10.
        (
            "!roll 10",
            Box::new(|m| {
                m.flag_on = true;
                m.message_id = "evt-1".into();
            }),
        ),
        // 2: a second roll by the same actor inside the cooldown -> silently dropped.
        (
            "!roll 10",
            Box::new(|m| {
                m.message_id = "evt-2".into();
            }),
        ),
        // 3: redelivery of event 1 -> same outcome, no cooldown drop, no fresh roll.
        (
            "!roll 10",
            Box::new(|m| {
                m.message_id = "evt-1".into();
            }),
        ),
        // 4: !dice has its own cooldown; the host refuses the wager for insufficient funds -> user-facing reply.
        (
            "!dice 10",
            Box::new(|m| {
                m.message_id = "evt-4".into();
                m.wager_refusal = Some(("insufficient_funds", "3"));
            }),
        ),
        // 5: the host denies the wager (not granted) -> the invoke FAILS loudly, no unpaid reply.
        (
            "!roll 10",
            Box::new(|m| {
                m.kv.clear();
                m.message_id = "evt-5".into();
                m.wager_refusal = Some(("not_granted", "no"));
            }),
        ),
    ];
    let runs = run(wasm, steps).await;

    // 0: flag off.
    assert_eq!(
        ops(&runs[0]),
        vec!["enabled"],
        "flag-off must stop after the flag check"
    );
    assert!(runs[0].reply.is_none());

    // 1: fresh roll: exactly one wager, payout consistent with the printed dice, alias + placeholder + balance in text.
    let r1 = &runs[1];
    assert_eq!(
        r1.calls
            .iter()
            .filter(|(_, op, _)| op == "economy.wager")
            .count(),
        1,
        "exactly one atomic wager per roll"
    );
    let wager = r1
        .calls
        .iter()
        .find(|(_, op, _)| op == "economy.wager")
        .unwrap();
    assert_eq!(wager.2["user"], ACTOR_UUID);
    assert_eq!(wager.2["stake"], 10);
    let text1 = reply_text(r1);
    let (d1, d2) = dice(&text1);
    let multipliers = [2u64, 3, 4, 6, 8, 10];
    let expected_payout = if d1 == d2 {
        10 * multipliers[(d1 - 1) as usize]
    } else {
        0
    };
    assert_eq!(
        wager.2["payout"], expected_payout,
        "payout must match the dice the reply printed"
    );
    assert!(
        text1.contains(&format!("{{user:{ACTOR_UUID}}}")),
        "reply must use the display-name placeholder: {text1}"
    );
    assert!(
        text1.contains("Balance: 1234 stars"),
        "balance + economy-service alias in reply: {text1}"
    );
    assert_eq!(
        r1.reply.as_ref().unwrap()["channel_id"],
        "chan-1",
        "reply keeps the origin channel"
    );

    // 2: cooldown -> silent drop, and crucially NO wager.
    assert!(
        runs[2].reply.is_none(),
        "a roll inside the cooldown is silently dropped"
    );
    assert!(!ops(&runs[2]).contains(&"economy.wager"));

    // 3: redelivery -> identical text, the same wager args, and no cooldown claim attempt.
    let r3 = &runs[3];
    assert_eq!(
        reply_text(r3),
        text1,
        "a redelivered event reprints the original outcome"
    );
    let wager3 = r3
        .calls
        .iter()
        .find(|(_, op, _)| op == "economy.wager")
        .unwrap();
    assert_eq!(
        wager3.2, wager.2,
        "the replayed wager is byte-identical so the host can de-duplicate it"
    );
    assert!(
        !ops(r3).contains(&"increment"),
        "a replay must not re-take the cooldown"
    );
    assert!(
        !ops(r3).contains(&"economy.max_bet"),
        "a replay skips fresh validation"
    );

    // 4: insufficient funds -> user-facing reply and the cooldown handed back (kv delete of the claim + outcome).
    let r4 = &runs[4];
    assert!(
        reply_text(r4).contains("you don't have 10 stars to roll"),
        "{}",
        reply_text(r4)
    );
    assert!(
        ops(r4).iter().filter(|o| **o == "delete").count() >= 2,
        "outcome record and cooldown released: {:?}",
        ops(r4)
    );

    // 5: not_granted is loud: no reply was produced.
    assert!(
        runs[5].trapped,
        "a host denial must fail the invoke loudly (trap), not return normally"
    );
    assert!(
        runs[5].reply.is_none(),
        "a host denial must never become an unpaid chat reply"
    );
    assert!(
        runs[..5].iter().all(|r| !r.trapped),
        "only the denial scenario may trap"
    );
    assert!(ops(&runs[5]).contains(&"economy.wager"));
}

/// Every resumed C# port must execute its own flag-on command through the production linker.
#[tokio::test(flavor = "multi_thread")]
#[ignore = "build all C# artifacts and set SUPERPENGUIN_WASM_DIR"]
async fn csharp_port_matrix_executes_commands_and_replays_through_real_executor() {
    let directory =
        std::env::var("SUPERPENGUIN_WASM_DIR").expect("SUPERPENGUIN_WASM_DIR is required");
    let cases = [
        ("slots", "!slots 25"),
        ("gamble", "!gamble 10"),
        ("defuse", "!defuse red"),
        ("first", "!first"),
        ("roulette", "!roulette 10"),
        ("duel", "!duel 22222222-2222-4222-8222-222222222222 10"),
    ];
    let mut examined = 0;
    for (game, command) in cases {
        let path = std::path::Path::new(&directory).join(format!("superpenguin_{game}.wasm"));
        let wasm = std::fs::read(path).expect("a real built component is required for EVERY port");
        assert!(wasm.len() > 1_000_000);
        let runs = run_for(
            format!("waddles.integrations.superpenguin.{game}"),
            format!("waddles.command-superpenguin-{game}"),
            wasm,
            vec![
                (
                    command,
                    Box::new(|m| {
                        m.flag_on = false;
                        m.message_id = "off".into();
                    }),
                ),
                (
                    command,
                    Box::new(|m| {
                        m.flag_on = true;
                        m.message_id = "on".into();
                    }),
                ),
                (
                    command,
                    Box::new(|m| {
                        m.flag_on = true;
                        m.message_id = "on".into();
                    }),
                ),
            ],
        )
        .await;
        assert!(
            !runs[0].trapped && runs[0].reply.is_none(),
            "{game}: flag-off contract"
        );
        assert!(!runs[1].trapped, "{game}: flag-on command must run");
        assert!(
            runs[1].reply.is_some(),
            "{game}: command must produce a reply"
        );
        assert!(
            !runs[2].trapped && runs[2].reply == runs[1].reply,
            "{game}: replay must preserve the response"
        );
        assert!(
            runs[1]
                .calls
                .iter()
                .any(|(_, op, _)| op == "economy.wager" || op == "tool.invoke"),
            "{game}: must cross its real economy/service binding"
        );
        examined += 1;
        println!("C# real-executor matrix: {game} PASS");
    }
    assert_eq!(examined, 6, "zero or partial port coverage must fail");
}
