//! Integration test (own process): drives `ai.generate` through the REAL
//! capability gate, the REAL host-call handler and the REAL Ollama backend
//! (rustls `reqwest` over HTTP to an in-process server speaking `/api/chat`),
//! then reads the telemetry back off the REAL Prometheus scrape surface.
//!
//! Proves the three things a unit test cannot: the OTel instruments for the
//! capability actually EMIT under the names dashboards query (counts printed,
//! zero is a failure); no prompt or completion text ever reaches a label; and
//! the guards refuse hostile input before any request is made to the model.
//!
//! Own file because `telemetry::init` installs process-global state that only
//! one call per process may claim.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::State;
use axum::routing::post;
use axum::{Json, Router};
use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody};
use svc_process::ai::{AiWiring, OllamaBackend, OllamaConfig};
use svc_process::capabilities::{CapabilityHandler, HttpEgressCatalog, StageCapabilities};
use svc_process::license::FeatureGate;

const APP_ID: &str = "waddles.ai.e2e";
/// A prompt secret that must appear in NO label, metric line or log-bound string.
const SECRET_PROMPT_WORD: &str = "zebrafish-tango-7731";

/// Always-ON feature gate (the production gate is a license-client read).
struct On;

impl FeatureGate for On {
    fn enabled<'a>(
        &'a self,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = bool> + Send + 'a>> {
        Box::pin(async { true })
    }
}

#[derive(Clone, Default)]
struct Ollama {
    seen: Arc<Mutex<Vec<serde_json::Value>>>,
}

async fn chat(
    State(s): State<Ollama>,
    Json(body): Json<serde_json::Value>,
) -> Json<serde_json::Value> {
    s.seen.lock().expect("lock").push(body);
    Json(serde_json::json!({
        "message": {"role": "assistant", "content": "Hello from the model!"},
        "prompt_eval_count": 33, "eval_count": 5
    }))
}

/// The value of the first scrape line starting with `prefix` containing every
/// `needle`.
fn sample(rendered: &str, prefix: &str, needles: &[&str]) -> Option<f64> {
    rendered
        .lines()
        .filter(|l| l.starts_with(prefix) && needles.iter().all(|n| l.contains(n)))
        .find_map(|l| l.rsplit(' ').next()?.parse::<f64>().ok())
}

fn caps(base: &str) -> StageCapabilities {
    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(
        GrantScopeKey {
            tenant_id: 1,
            community_id: 1,
            app_id: APP_ID.to_string(),
            app_version: 1,
        },
        GrantSet {
            permission_snapshot_hash: "test".to_string(),
            grants: std::collections::HashMap::from([(
                "ai.generate".to_string(),
                GrantedPermission {
                    permission_id: "ai.generate".to_string(),
                    params: serde_json::json!({}),
                },
            )]),
        },
    );
    let gate = Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ));
    let egress = Arc::new(bundle_host_http::egress::EgressGuard::new(
        Arc::new(bundle_host_http::egress::ReqwestTransport::new()),
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
            prometheus::Opts::new("ai_e2e_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition"),
        bundle_host_http::egress::boxed(bundle_host_http::egress::StaticFlag(true)),
    ));
    let backend = OllamaBackend::new(&OllamaConfig {
        base_url: base.to_string(),
        model: "gemma4:e2b".to_string(),
        json_model: "gemma4:e4b".to_string(),
        request_timeout: Duration::from_secs(6),
    })
    .expect("loopback is a valid self-hosted endpoint");
    StageCapabilities::<redis::aio::MultiplexedConnection>::new(
        "acme".to_string(),
        Some("main".to_string()),
        APP_ID.to_string(),
        1,
        1,
        1,
        egress,
        gate,
    )
    .with_ai(
        AiWiring::new(Arc::new(backend), Arc::new(On), Duration::from_secs(5), 4),
        std::time::Instant::now() + Duration::from_secs(30),
    )
}

fn call(args: serde_json::Value) -> HostCallBody {
    HostCallBody {
        app_id: APP_ID.to_string(),
        capability: CapabilityKind::Db,
        op: "ai.generate".to_string(),
        args,
        call_id: 1,
    }
}

#[tokio::test]
async fn ai_generate_emits_telemetry_and_guards_hold_end_to_end() {
    // SAFETY: the only test in this binary/process; no OTLP endpoint so the
    // pipeline is Prometheus-only (the reader `penguin-logging` always attaches).
    unsafe { std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT") };
    let (mut guard, registry) = svc_process::telemetry::init("svc-process-ai-e2e");

    let ollama = Ollama::default();
    let app = Router::new()
        .route("/api/chat", post(chat))
        .with_state(ollama.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let addr = listener.local_addr().expect("addr");
    tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    let caps = caps(&format!("http://{addr}"));

    // Three successful generations (one structured) and one PII refusal.
    for prompt in [
        format!("say something about {SECRET_PROMPT_WORD}"),
        "another one".to_string(),
    ] {
        let out = caps
            .handle(call(serde_json::json!({ "prompt": prompt })))
            .await
            .expect("granted call succeeds");
        assert_eq!(out["text"], "Hello from the model!");
    }
    caps.handle(call(serde_json::json!({"prompt": "as json", "json": true})))
        .await
        .expect("structured call succeeds");
    let refused = caps
        .handle(call(serde_json::json!({"prompt": "mail dave@example.com"})))
        .await
        .expect_err("PII is refused");
    assert_eq!(refused.code, "contains_pii");

    // The model saw exactly the three clean prompts, framed; the PII one never left.
    let seen = ollama.seen.lock().expect("lock").clone();
    assert_eq!(seen.len(), 3, "PII must not produce a model request");
    assert_eq!(seen[2]["model"], "gemma4:e4b");
    assert_eq!(seen[2]["format"], "json");
    for req in &seen {
        assert!(req["messages"][1]["content"]
            .as_str()
            .expect("user turn")
            .starts_with("<user_input>\n"));
        assert!(!req.to_string().contains("dave@example.com"));
    }

    let text = svc_process::telemetry::render_metrics(&registry).expect("registry must encode");
    for doubled in ["_total_total", "_seconds_seconds"] {
        assert!(!text.contains(doubled), "double-suffixed name in:\n{text}");
    }

    let ok_calls = sample(
        &text,
        "waddles_bundle_ai_generate_total{",
        &["model=\"gemma4:e2b\"", "outcome=\"ok\""],
    );
    let json_calls = sample(
        &text,
        "waddles_bundle_ai_generate_total{",
        &["model=\"gemma4:e4b\"", "outcome=\"ok\""],
    );
    let pii_calls = sample(
        &text,
        "waddles_bundle_ai_generate_total{",
        &["model=\"gemma4:e2b\"", "outcome=\"pii_rejected\""],
    );
    println!(
        "ai telemetry counts: ok(e2b)={ok_calls:?} ok(e4b)={json_calls:?} pii_rejected={pii_calls:?}"
    );
    assert_eq!(ok_calls, Some(2.0), "scrape:\n{text}");
    assert_eq!(json_calls, Some(1.0), "scrape:\n{text}");
    assert_eq!(pii_calls, Some(1.0), "scrape:\n{text}");

    // The latency histogram (the most-often-missing signal) emitted.
    assert_eq!(
        sample(
            &text,
            "waddles_bundle_ai_generate_duration_seconds_count{",
            &["model=\"gemma4:e2b\"", "outcome=\"ok\""]
        ),
        Some(2.0),
        "latency histogram missing:\n{text}"
    );
    assert!(text.contains("waddles_bundle_ai_generate_duration_seconds_bucket{"));

    // Token counters carry the model-reported counts: 2 x (33 + 5) on e2b.
    assert_eq!(
        sample(
            &text,
            "waddles_bundle_ai_tokens_total{",
            &["model=\"gemma4:e2b\"", "kind=\"prompt\""]
        ),
        Some(66.0)
    );
    assert_eq!(
        sample(
            &text,
            "waddles_bundle_ai_tokens_total{",
            &["model=\"gemma4:e2b\"", "kind=\"completion\""]
        ),
        Some(10.0)
    );

    // The stage's own host-call timing attributes the call to the `ai` capability.
    assert_eq!(
        sample(
            &text,
            "waddles_host_call_latency_seconds_count{",
            &["capability=\"ai\"", "op=\"generate\"", "result=\"ok\""]
        ),
        Some(3.0),
        "host-call timing missing:\n{text}"
    );
    assert_eq!(
        sample(
            &text,
            "waddles_host_call_latency_seconds_count{",
            &["capability=\"ai\"", "op=\"generate\"", "result=\"denied\""]
        ),
        Some(1.0),
        "the PII refusal must be a denied host call:\n{text}"
    );

    // PII / cardinality guard on the whole scrape: no prompt or completion text.
    for forbidden in [
        SECRET_PROMPT_WORD,
        "dave@example.com",
        "Hello from the model",
        "another one",
    ] {
        assert!(
            !text.contains(forbidden),
            "{forbidden:?} leaked into the scrape"
        );
    }
    guard.shutdown();
}
