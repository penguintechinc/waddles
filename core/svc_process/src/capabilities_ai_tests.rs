//! `ai.generate` host-capability tests: the REAL [`CapabilityGate`] (grant,
//! 10-calls-per-60s quota), the REAL `handle` dispatch and the REAL prompt /
//! completion guards ([`crate::ai`]) run against a scripted model backend; the
//! real Ollama wire protocol is exercised in `ai.rs`'s own tests (a real HTTP
//! server double) and joined to the handler at the bottom of this file and in
//! `tests/ai_generate_e2e.rs`.
//!
//! The security-critical orderings are pinned here: the gate authorizes BEFORE
//! any content check or model call; PII is rejected before a byte can reach the
//! backend; the model call is deadline-clamped and fails loud.

use super::*;
use crate::ai::{AiBackend, Completion};
use crate::license::test_support::FixedGate;
use bundle_capability_gate::{
    GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use bundle_host_http::egress::{ReqwestTransport, StaticFlag};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

const TENANT_ID: i32 = 7;
const COMMUNITY_ID: i32 = 3;
const APP_ID: &str = "waddles.core.test-ai";
const VERSION: i64 = 1;

/// How the scripted backend answers.
enum Script {
    Reply(Completion),
    Fail(BackendError),
    Hang,
}

/// Counts and records every generation; answers per its [`Script`].
struct ScriptedBackend {
    script: Mutex<Script>,
    requests: Mutex<Vec<GenerateRequest>>,
    calls: AtomicUsize,
}

impl ScriptedBackend {
    fn new(script: Script) -> Arc<Self> {
        Arc::new(Self {
            script: Mutex::new(script),
            requests: Mutex::new(Vec::new()),
            calls: AtomicUsize::new(0),
        })
    }

    fn replying(text: &str) -> Arc<Self> {
        Self::new(Script::Reply(Completion {
            text: text.to_string(),
            prompt_tokens: Some(20),
            completion_tokens: Some(5),
        }))
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }

    fn last(&self) -> GenerateRequest {
        self.requests
            .lock()
            .unwrap()
            .last()
            .cloned()
            .expect("the backend was called")
    }
}

impl AiBackend for ScriptedBackend {
    fn model_for(&self, json: bool) -> &str {
        if json {
            "test-json-model"
        } else {
            "test-model"
        }
    }

    fn generate<'a>(
        &'a self,
        req: &'a GenerateRequest,
    ) -> crate::ai::BoxFuture<'a, Result<Completion, BackendError>> {
        Box::pin(async move {
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.requests.lock().unwrap().push(req.clone());
            let hang = matches!(*self.script.lock().unwrap(), Script::Hang);
            if hang {
                std::future::pending::<()>().await;
            }
            match &*self.script.lock().unwrap() {
                Script::Reply(c) => Ok(c.clone()),
                Script::Fail(e) => Err(e.clone()),
                Script::Hang => Err(BackendError::Backend("unreachable".to_string())),
            }
        })
    }
}

/// A feature gate that records whether it was ever consulted.
struct SpyGate {
    on: bool,
    consulted: AtomicUsize,
}

impl FeatureGate for SpyGate {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move {
            self.consulted.fetch_add(1, Ordering::SeqCst);
            self.on
        })
    }
}

fn grants(ids: &[&str]) -> GrantSet {
    GrantSet {
        permission_snapshot_hash: "test".to_string(),
        grants: ids
            .iter()
            .map(|id| {
                (
                    (*id).to_string(),
                    GrantedPermission {
                        permission_id: (*id).to_string(),
                        params: serde_json::json!({}),
                    },
                )
            })
            .collect(),
    }
}

/// A capability set at `(TENANT_ID, COMMUNITY_ID)` holding exactly `grant_ids`,
/// optionally wired to `wiring` with an invocation deadline `deadline_in` away.
fn caps_with(
    grant_ids: &[&str],
    wiring: Option<AiWiring>,
    deadline_in: Duration,
) -> StageCapabilities {
    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(
        GrantScopeKey {
            tenant_id: TENANT_ID,
            community_id: COMMUNITY_ID,
            app_id: APP_ID.to_string(),
            app_version: VERSION,
        },
        grants(grant_ids),
    );
    let gate = Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ));
    let egress = Arc::new(EgressGuard::new(
        Arc::new(ReqwestTransport::new()),
        bundle_host_http::egress::EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        },
        HttpEgressCatalog::new(),
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("test_ai_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .unwrap(),
        bundle_host_http::egress::boxed(StaticFlag(true)),
    ));
    let caps = StageCapabilities::new(
        "acme".to_string(),
        Some("main".to_string()),
        APP_ID.to_string(),
        TENANT_ID,
        COMMUNITY_ID,
        VERSION,
        egress,
        gate,
    );
    match wiring {
        Some(w) => caps.with_ai(w, Instant::now() + deadline_in),
        None => caps,
    }
}

fn wiring(backend: &Arc<ScriptedBackend>, flag_on: bool) -> AiWiring {
    AiWiring::new(
        Arc::clone(backend) as Arc<dyn AiBackend>,
        Arc::new(FixedGate(flag_on)),
        Duration::from_secs(5),
        4,
    )
}

/// Fully wired, granted, flag ON, a generous deadline.
fn fixture(backend: &Arc<ScriptedBackend>) -> StageCapabilities {
    caps_with(
        &["ai.generate"],
        Some(wiring(backend, true)),
        Duration::from_secs(30),
    )
}

fn ai_call(op: &str, args: serde_json::Value) -> HostCallBody {
    HostCallBody {
        app_id: APP_ID.to_string(),
        capability: CapabilityKind::Db,
        op: op.to_string(),
        args,
        call_id: 1,
    }
}

fn generate(prompt: &str) -> HostCallBody {
    ai_call("ai.generate", serde_json::json!({ "prompt": prompt }))
}

async fn code_of(caps: &StageCapabilities, call: HostCallBody) -> String {
    caps.handle(call).await.expect_err("expected a denial").code
}

#[tokio::test]
async fn a_granted_call_returns_the_sanitized_completion_and_frames_the_prompt() {
    let backend = ScriptedBackend::replying("A fine shoutout! @everyone ![x](https://e.example/a)");
    let caps = fixture(&backend);
    let out = caps
        .handle(generate(
            "write a shoutout for {user:3fa85f64-5717-4562-b3fc-2c963f66afa6}",
        ))
        .await
        .unwrap();
    let text = out["text"].as_str().unwrap();
    assert!(text.starts_with("A fine shoutout!"), "{text}");
    assert!(!text.contains("e.example"), "{text}");
    assert!(!text.contains("@everyone"), "{text}");

    assert_eq!(backend.calls(), 1);
    let req = backend.last();
    assert_eq!(req.messages.len(), 2);
    assert_eq!(req.messages[0].role, "system");
    assert!(req.messages[0].content.starts_with(ai::SYSTEM_PREAMBLE));
    assert_eq!(req.messages[1].role, "user");
    assert!(req.messages[1].content.starts_with("<user_input>\n"));
    assert!(req.messages[1].content.ends_with("\n</user_input>"));
    assert_eq!(req.max_tokens, ai::DEFAULT_COMPLETION_TOKENS);
    assert!(!req.json);
}

#[tokio::test]
async fn options_are_forwarded_and_json_selects_the_structured_model() {
    let backend = ScriptedBackend::replying("{\"a\":1}");
    let caps = fixture(&backend);
    caps.handle(ai_call(
        "ai.generate",
        serde_json::json!({"prompt": "make json", "max_tokens": 64, "temperature": 0.25, "json": true}),
    ))
    .await
    .unwrap();
    let req = backend.last();
    assert!(req.json);
    assert_eq!(req.max_tokens, 64);
    assert_eq!(req.temperature, 0.25);
    assert!(req.messages[0]
        .content
        .contains("exactly one valid JSON object"));
    // Explicit nulls (what the executor sends for an absent option) are defaults.
    caps.handle(ai_call(
        "ai.generate",
        serde_json::json!({"prompt": "x", "max_tokens": null, "temperature": null, "json": false}),
    ))
    .await
    .unwrap();
    assert_eq!(backend.last().max_tokens, ai::DEFAULT_COMPLETION_TOKENS);
    assert_eq!(backend.last().temperature, ai::DEFAULT_TEMPERATURE);
}

// regression: ai-authorize-first -- an ungranted call is refused by the gate
// before ANY content check, wiring/flag lookup or model call.
#[tokio::test]
async fn the_gate_authorizes_first_ungranted_is_not_granted_whatever_else_is_wrong() {
    let backend = ScriptedBackend::replying("never");
    let flag = Arc::new(SpyGate {
        on: true,
        consulted: AtomicUsize::new(0),
    });
    let wired = AiWiring::new(
        Arc::clone(&backend) as Arc<dyn AiBackend>,
        Arc::clone(&flag) as Arc<dyn FeatureGate>,
        Duration::from_secs(5),
        4,
    );
    // No `ai.generate` grant (a different permission only).
    let caps = caps_with(&["platform.clock"], Some(wired), Duration::from_secs(30));
    for prompt in [
        "ok prompt",
        "bob@example.com leaked",
        &"a".repeat(100_000),
        "",
    ] {
        assert_eq!(code_of(&caps, generate(prompt)).await, "not_granted");
    }
    // Even an over-bound option reports not_granted, never too_many_tokens.
    assert_eq!(
        code_of(
            &caps,
            ai_call(
                "ai.generate",
                serde_json::json!({"prompt": "x", "max_tokens": 9999})
            )
        )
        .await,
        "not_granted"
    );
    assert_eq!(backend.calls(), 0, "no model call without a grant");
    assert_eq!(
        flag.consulted.load(Ordering::SeqCst),
        0,
        "the flag must not even be consulted without a grant"
    );
}

#[tokio::test]
async fn ungranted_and_unwired_reports_not_granted_never_not_implemented() {
    let caps = caps_with(&[], None, Duration::from_secs(1));
    assert_eq!(code_of(&caps, generate("hi")).await, "not_granted");
}

#[tokio::test]
async fn granted_but_unwired_is_not_implemented_and_flag_off_is_feature_disabled() {
    let caps = caps_with(&["ai.generate"], None, Duration::from_secs(1));
    assert_eq!(code_of(&caps, generate("hi")).await, "not_implemented");

    let backend = ScriptedBackend::replying("never");
    let caps = caps_with(
        &["ai.generate"],
        Some(wiring(&backend, false)),
        Duration::from_secs(30),
    );
    assert_eq!(code_of(&caps, generate("hi")).await, "feature_disabled");
    assert_eq!(backend.calls(), 0);
}

// regression: ai-rate-limit -- the catalog's 10 calls / 60 s, enforced by the
// real gate quota ledger, never reaching the model once exhausted.
#[tokio::test]
async fn the_rate_quota_is_ten_calls_per_window() {
    let backend = ScriptedBackend::replying("ok");
    let caps = fixture(&backend);
    for i in 0..10 {
        if let Err(e) = caps.handle(generate("hi")).await {
            panic!("call {i} should pass: {e:?}");
        }
    }
    assert_eq!(code_of(&caps, generate("hi")).await, "rate_limited");
    assert_eq!(code_of(&caps, generate("hi")).await, "rate_limited");
    assert_eq!(
        backend.calls(),
        10,
        "the 11th call must never reach the model"
    );
}

// regression: ai-pii-reject -- reserved `Denied::ContainsPii` hook: refused with
// the stable code BEFORE any byte reaches the backend, and the refusal carries
// the category, never the text.
#[tokio::test]
async fn pii_bearing_prompts_are_rejected_before_the_model_is_reached() {
    let backend = ScriptedBackend::replying("never");
    let caps = fixture(&backend);
    for prompt in [
        "email alice@example.com",
        "call 415-555-0199",
        "ssn 123-45-6789",
        "card 4111 1111 1111 1111",
        "token sk-abcdefghijklmnop1234",
        "ping <@123456789012345678>",
        "thanks @coolstreamer",
        "host 10.1.2.3",
        "evade bob\u{200b}@example.com",
    ] {
        let err = caps.handle(generate(prompt)).await.unwrap_err();
        assert_eq!(err.code, "contains_pii", "{prompt:?}");
        assert!(
            !err.message.contains("alice")
                && !err.message.contains("4111")
                && !err.message.contains("coolstreamer"),
            "the refusal must never echo prompt text: {}",
            err.message
        );
    }
    assert_eq!(backend.calls(), 0, "PII must never reach the backend");
}

#[tokio::test]
async fn token_bounds_are_enforced_before_the_model_is_reached() {
    let backend = ScriptedBackend::replying("never");
    let caps = fixture(&backend);

    let err = caps
        .handle(ai_call(
            "ai.generate",
            serde_json::json!({"prompt": "x", "max_tokens": 513}),
        ))
        .await
        .unwrap_err();
    assert_eq!(
        (err.code.as_str(), err.message.as_str()),
        ("too_many_tokens", "512")
    );

    let over = "a".repeat(ai::MAX_PROMPT_TOKENS as usize * 3 + 1);
    let err = caps.handle(generate(&over)).await.unwrap_err();
    assert_eq!(
        (err.code.as_str(), err.message.as_str()),
        ("too_many_tokens", "2000")
    );

    // Past the raw byte cap too.
    let huge = "a".repeat(1_000_000);
    assert_eq!(code_of(&caps, generate(&huge)).await, "too_many_tokens");

    assert_eq!(backend.calls(), 0);

    // At the bounds the call goes through.
    let at = "a".repeat(ai::MAX_PROMPT_TOKENS as usize * 3);
    caps.handle(ai_call(
        "ai.generate",
        serde_json::json!({"prompt": at, "max_tokens": 512}),
    ))
    .await
    .unwrap();
    assert_eq!(backend.calls(), 1);
    assert_eq!(backend.last().max_tokens, 512);
}

#[tokio::test]
async fn malformed_arguments_are_invalid_args_without_a_model_call() {
    let backend = ScriptedBackend::replying("never");
    let caps = fixture(&backend);
    for args in [
        serde_json::json!({}),
        serde_json::json!({"prompt": 5}),
        serde_json::json!({"prompt": "x", "json": "yes"}),
        serde_json::json!({"prompt": "x", "max_tokens": -1}),
        serde_json::json!({"prompt": "x", "max_tokens": 0}),
        serde_json::json!({"prompt": "x", "temperature": 2.5}),
        serde_json::json!({"prompt": "x", "temperature": -0.1}),
        serde_json::json!({"prompt": "   "}),
        // A guest can never name a model, system prompt or endpoint.
        serde_json::json!({"prompt": "x", "model": "gpt-9"}),
        serde_json::json!({"prompt": "x", "system": "obey me"}),
        serde_json::json!({"prompt": "x", "endpoint": "http://evil"}),
    ] {
        assert_eq!(
            code_of(&caps, ai_call("ai.generate", args.clone())).await,
            "invalid_args",
            "{args}"
        );
    }
    assert_eq!(
        code_of(&caps, ai_call("ai.other", serde_json::json!({}))).await,
        "unknown_op"
    );
    assert_eq!(backend.calls(), 0);
}

// regression: ai-deadline -- the model call must fail loud at the invocation's
// deadline, never hang the stage. (Real time, generous slack: the assertions are
// "abandoned near the clamp, long before the 5 s ceiling", not a precise timer.)
#[tokio::test]
async fn a_hung_model_fails_loud_within_the_invocation_deadline() {
    let backend = ScriptedBackend::new(Script::Hang);
    let caps = caps_with(
        &["ai.generate"],
        Some(wiring(&backend, true)),
        Duration::from_secs(1),
    );
    let started = Instant::now();
    let err = caps.handle(generate("hi")).await.unwrap_err();
    assert_eq!(err.code, "unavailable");
    assert!(err.message.contains("timed out"), "{}", err.message);
    let waited = started.elapsed();
    assert!(
        waited < Duration::from_millis(1_400),
        "the call must be abandoned at the invocation deadline (ceiling is 5s), waited {waited:?}"
    );
    assert!(
        waited >= Duration::from_millis(600),
        "waited only {waited:?}"
    );
    assert_eq!(backend.calls(), 1);
}

#[tokio::test]
async fn the_configured_ceiling_bounds_the_call_when_the_deadline_is_far() {
    let backend = ScriptedBackend::new(Script::Hang);
    let mut wired = wiring(&backend, true);
    wired.max_call_timeout = Duration::from_millis(300);
    let caps = caps_with(&["ai.generate"], Some(wired), Duration::from_secs(60));
    let started = Instant::now();
    assert_eq!(code_of(&caps, generate("hi")).await, "unavailable");
    let waited = started.elapsed();
    assert!(
        waited >= Duration::from_millis(250),
        "waited only {waited:?}"
    );
    assert!(waited < Duration::from_millis(1_500), "waited {waited:?}");
}

// regression: ai-concurrency -- many bundles within their own quotas still cannot
// saturate the model host: the (n+1)th concurrent call waits for a permit inside
// its deadline-clamped timeout and fails loud without ever reaching the backend.
#[tokio::test]
async fn the_global_concurrency_cap_fails_loud_without_reaching_the_backend() {
    let backend = ScriptedBackend::new(Script::Hang);
    // Both share ONE single-permit semaphore; the first may hold it for 3 s, the
    // second only ever waits 300 ms.
    let mut long = wiring(&backend, true);
    long.max_call_timeout = Duration::from_secs(3);
    long.concurrency = Arc::new(tokio::sync::Semaphore::new(1));
    let shared = Arc::clone(&long.concurrency);
    let mut short = long.clone();
    short.max_call_timeout = Duration::from_millis(300);
    let first = Arc::new(caps_with(
        &["ai.generate"],
        Some(long),
        Duration::from_secs(60),
    ));
    let second = caps_with(&["ai.generate"], Some(short), Duration::from_secs(60));

    // The first call takes the only permit and hangs in the backend.
    let in_flight = tokio::spawn({
        let first = Arc::clone(&first);
        async move { first.handle(generate("hi")).await }
    });
    while backend.calls() == 0 {
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    // The second waits for the permit, then fails at its own deadline.
    let err = second.handle(generate("hi")).await.unwrap_err();
    assert_eq!(err.code, "unavailable");
    assert_eq!(
        backend.calls(),
        1,
        "the saturated call must never reach the backend"
    );
    // Dropping the hung first call releases its permit: a third call now gets
    // through to the backend instead of being starved forever.
    in_flight.abort();
    let _ = in_flight.await;
    let third = caps_with(
        &["ai.generate"],
        Some({
            let mut w = wiring(&backend, true);
            w.concurrency = Arc::clone(&shared);
            w.max_call_timeout = Duration::from_millis(300);
            w
        }),
        Duration::from_secs(60),
    );
    assert_eq!(code_of(&third, generate("hi")).await, "unavailable");
    assert_eq!(
        backend.calls(),
        2,
        "a freed permit lets the next call reach the backend"
    );
}

#[tokio::test]
async fn an_exhausted_invocation_deadline_refuses_without_calling_the_model() {
    let backend = ScriptedBackend::replying("never");
    let caps = caps_with(
        &["ai.generate"],
        Some(wiring(&backend, true)),
        // Less than the response margin + minimum call budget.
        Duration::from_millis(200),
    );
    let err = caps.handle(generate("hi")).await.unwrap_err();
    assert_eq!(err.code, "unavailable");
    assert!(err.message.contains("deadline"), "{}", err.message);
    assert_eq!(backend.calls(), 0);

    // A deadline already in the past is the same refusal.
    let caps = caps_with(
        &["ai.generate"],
        Some(wiring(&backend, true)),
        Duration::ZERO,
    );
    assert_eq!(code_of(&caps, generate("hi")).await, "unavailable");
    assert_eq!(backend.calls(), 0);
}

#[tokio::test]
async fn backend_failures_map_to_stable_wire_codes() {
    for (script, code) in [
        (
            Script::Fail(BackendError::Unavailable(
                "model backend unreachable".to_string(),
            )),
            "unavailable",
        ),
        (
            Script::Fail(BackendError::Backend(
                "model backend returned HTTP 500".to_string(),
            )),
            "backend",
        ),
        (Script::Fail(BackendError::ToolCall), "content_filtered"),
    ] {
        let backend = ScriptedBackend::new(script);
        let caps = fixture(&backend);
        assert_eq!(code_of(&caps, generate("hi")).await, code);
        assert_eq!(backend.calls(), 1);
    }
}

#[tokio::test]
async fn an_empty_or_fully_stripped_completion_is_content_filtered() {
    for text in ["", "   \n ", "\u{200b}\u{200b}"] {
        let backend = ScriptedBackend::replying(text);
        let caps = fixture(&backend);
        assert_eq!(
            code_of(&caps, generate("hi")).await,
            "content_filtered",
            "{text:?}"
        );
    }
}

#[tokio::test]
async fn an_oversized_completion_is_hard_capped() {
    let backend = ScriptedBackend::replying(&"y".repeat(50_000));
    let caps = fixture(&backend);
    let out = caps.handle(generate("hi")).await.unwrap();
    assert_eq!(
        out["text"].as_str().unwrap().chars().count(),
        ai::MAX_COMPLETION_CHARS
    );
}

#[tokio::test]
async fn a_completion_is_never_a_command_it_is_inert_text() {
    // A hostile completion trying to act through the reply: pings, beacons,
    // active HTML. All neutralized; the result is plain text only.
    let backend = ScriptedBackend::replying(
        "run <@999999999> now <img src=x onerror=1> ![b](http://e.example/p) <@&88888>",
    );
    let caps = fixture(&backend);
    let out = caps.handle(generate("hi")).await.unwrap();
    let text = out["text"].as_str().unwrap();
    for forbidden in ["<@", "<img", "e.example", "onerror=1>"] {
        assert!(!text.contains(forbidden), "{forbidden} survived: {text}");
    }
}

/// The real Ollama backend joined to the real handler and gate, over real HTTP
/// to an in-process server that speaks `/api/chat`.
mod real_backend {
    use super::*;
    use crate::ai::{OllamaBackend, OllamaConfig};
    use axum::extract::State;
    use axum::routing::post;
    use axum::{Json, Router};

    #[derive(Clone, Default)]
    struct Server {
        seen: Arc<Mutex<Vec<serde_json::Value>>>,
        hang: bool,
    }

    async fn chat(
        State(s): State<Server>,
        Json(body): Json<serde_json::Value>,
    ) -> Json<serde_json::Value> {
        s.seen.lock().unwrap().push(body);
        if s.hang {
            std::future::pending::<()>().await;
        }
        Json(serde_json::json!({
            "message": {"role": "assistant", "content": "Welcome aboard, friend!"},
            "prompt_eval_count": 42, "eval_count": 6
        }))
    }

    async fn serve(server: Server) -> String {
        let app = Router::new()
            .route("/api/chat", post(chat))
            .with_state(server);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        format!("http://{addr}")
    }

    fn wired(base: &str, ceiling: Duration) -> AiWiring {
        AiWiring::new(
            Arc::new(
                OllamaBackend::new(&OllamaConfig {
                    base_url: base.to_string(),
                    model: "gemma4:e2b".to_string(),
                    json_model: "gemma4:e4b".to_string(),
                    request_timeout: ceiling + Duration::from_secs(1),
                })
                .unwrap(),
            ),
            Arc::new(FixedGate(true)),
            ceiling,
            4,
        )
    }

    #[tokio::test]
    async fn handler_gate_and_ollama_protocol_join_end_to_end() {
        let server = Server::default();
        let base = serve(server.clone()).await;
        let caps = caps_with(
            &["ai.generate"],
            Some(wired(&base, Duration::from_secs(5))),
            Duration::from_secs(30),
        );
        let out = caps
            .handle(ai_call(
                "ai.generate",
                serde_json::json!({
                    "prompt": "Say hello to {user:3fa85f64-5717-4562-b3fc-2c963f66afa6}",
                    "max_tokens": 32, "json": false
                }),
            ))
            .await
            .unwrap();
        assert_eq!(out["text"], "Welcome aboard, friend!");

        let sent = server.seen.lock().unwrap().clone();
        assert_eq!(sent.len(), 1);
        assert_eq!(sent[0]["model"], "gemma4:e2b");
        assert_eq!(sent[0]["options"]["num_predict"], 32);
        assert!(sent[0]["messages"][1]["content"]
            .as_str()
            .unwrap()
            .contains("{user:3fa85f64-5717-4562-b3fc-2c963f66afa6}"));

        // A PII prompt never produces a request at all.
        let err = caps
            .handle(generate("mail me at carol@example.com"))
            .await
            .unwrap_err();
        assert_eq!(err.code, "contains_pii");
        assert_eq!(server.seen.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn a_hung_ollama_fails_loud_at_the_ceiling_in_real_time() {
        let server = Server {
            hang: true,
            ..Server::default()
        };
        let base = serve(server).await;
        let caps = caps_with(
            &["ai.generate"],
            Some(wired(&base, Duration::from_millis(300))),
            Duration::from_secs(30),
        );
        let started = Instant::now();
        let err = caps.handle(generate("hi")).await.unwrap_err();
        assert_eq!(err.code, "unavailable");
        assert!(err.message.contains("timed out"), "{}", err.message);
        assert!(
            started.elapsed() < Duration::from_secs(3),
            "must fail loud promptly, took {:?}",
            started.elapsed()
        );
    }
}
