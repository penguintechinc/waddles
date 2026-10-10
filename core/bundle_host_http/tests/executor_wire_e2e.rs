//! End-to-end proof of the `http.send` JSON wire between the bundle executor
//! and this crate's egress guard.
//!
//! A REAL compiled WASI 0.2 guest (`tests/fixtures/http_fixture.wasm`, source
//! in `tests/fixtures/http-fixture-src`) calls the WIT `http.send` import
//! inside the REAL `bundle-executor` (wasmtime, `host::imports`'s
//! `http::Host::send`, the real host-API frame codec over an in-memory
//! duplex). The stage half of that connection is a thin double that hands
//! every `http`/`send` host-call to the REAL [`EgressGuard::send`] and writes
//! its real result back -- only the network transport behind the guard is
//! scripted, so no request leaves the process.
//!
//! Why this exists: the two halves were written independently and disagreed
//! on the JSON (the executor sent the request body as a `body` byte array
//! the guard never read, so every body was silently dropped; the guard
//! returned `headers: [{name, value}]` + `body_base64` while the executor
//! decoded `[(name, value)]` + `body`, so any response with headers failed
//! "malformed host-result"). Each side's own unit tests passed against a
//! wire shape the other side never produced -- nothing ran them together.
//! This file does, so a change to either half that breaks the contract fails
//! here. It lives in this crate (not `bundle-executor`) because the executor
//! may not link the guard's `reqwest`
//! (`core/bundle_executor/tests/dependency_policy.rs`).

// Integration-test-only: assertions read naturally as `expect`/`unwrap`.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{HashMap, VecDeque};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_executor::config::CliConfig;
use bundle_executor::error::ExecutorError;
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::{ComponentSource, Executor};
use bundle_executor::wire::run_connection;
use bundle_host_http::egress::{
    boxed, EgressGuard, EgressLimits, EgressRuleRow, EgressRuleSource, HttpTransport, StaticFlag,
    TransportRequest, TransportResponse,
};
use penguin_bundle_host::wire::{
    read_frame, write_frame, CapabilityKind, ExportKind, Frame, HelloBody, HelloLimits,
    HelloOkBody, HostResultBody, HostResultError, InvokeBody, LoadBody, LoadLimits, Message,
    SandboxInfo, ShutdownBody,
};
use sha2::{Digest, Sha256};

const FIXTURE_WASM: &[u8] = include_bytes!("fixtures/http_fixture.wasm");
const APP_ID: &str = "waddles.test.http-fixture";

/// A public IPv4 literal: needs no DNS (the guard's resolver returns a
/// literal as-is), is not in any forbidden range, and is granted below via a
/// `net.http.public-ip` entry.
const HOST: &str = "93.184.216.34";

/// Process-environment variable the bundle's granted `bot-token` secret ref
/// resolves through (`EnvCredentialBroker`). Unique to this file so no other
/// test touches it.
const SECRET_ENV: &str = "BUNDLE_HTTP_WIRE_E2E_TOKEN";
const SECRET_VALUE: &str = "tok_live_e2e_9f8e7d6c5b4a";

/// Hands back the committed fixture bytes regardless of key -- exactly one
/// bundle in this file.
struct FixtureSource;

impl ComponentSource for FixtureSource {
    async fn fetch(
        &self,
        _component_key: &str,
        _sidecar_key: &str,
    ) -> Result<(Vec<u8>, Vec<u8>), ExecutorError> {
        // No `bundle_signing_public_keys` configured, so the `{}` sidecar is
        // never parsed (same as the executor's own integration tests).
        Ok((FIXTURE_WASM.to_vec(), b"{}".to_vec()))
    }
}

fn fixture_digest() -> String {
    let mut hasher = Sha256::new();
    hasher.update(FIXTURE_WASM);
    format!("sha256:{:x}", hasher.finalize())
}

fn executor_config() -> CliConfig {
    use clap::Parser;
    CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-process:8301",
    ])
    .expect("static test args always parse")
}

/// The scripted network behind the guard: records every request the guard
/// finally sends and answers from a queue. An exhausted queue is a loud
/// transport error, never a default success.
#[derive(Default)]
struct ScriptedTransport {
    responses: Mutex<VecDeque<Result<TransportResponse, HostResultError>>>,
    requests: Mutex<Vec<TransportRequest>>,
}

impl ScriptedTransport {
    fn with(responses: Vec<Result<TransportResponse, HostResultError>>) -> Arc<Self> {
        Arc::new(Self {
            responses: Mutex::new(responses.into()),
            requests: Mutex::new(Vec::new()),
        })
    }
}

impl HttpTransport for ScriptedTransport {
    fn send<'a>(
        &'a self,
        req: TransportRequest,
        _timeout: Duration,
        _max_response_bytes: usize,
    ) -> Pin<Box<dyn Future<Output = Result<TransportResponse, HostResultError>> + Send + 'a>> {
        self.requests.lock().unwrap().push(req);
        let next = self
            .responses
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_else(|| {
                Err(HostResultError {
                    code: "transport".to_string(),
                    message: "ScriptedTransport: no scripted response left".to_string(),
                })
            });
        Box::pin(async move { next })
    }
}

/// One app's egress row: `GET`/`POST` on [`HOST`], plus the `bot-token`
/// symbolic secret ref mapped to [`SECRET_ENV`].
struct Catalog;

impl EgressRuleSource for Catalog {
    fn resolve(&self, app_id: &str) -> Option<EgressRuleRow> {
        (app_id == APP_ID).then(|| {
            EgressRuleRow::from_legacy_patterns(
                vec![(
                    HOST.to_string(),
                    vec!["GET".to_string(), "POST".to_string()],
                )],
                None,
                HashMap::from([("bot-token".to_string(), SECRET_ENV.to_string())]),
            )
        })
    }
}

fn guard_over(transport: &Arc<ScriptedTransport>) -> Arc<EgressGuard> {
    Arc::new(EgressGuard::new(
        Arc::clone(transport) as Arc<dyn HttpTransport>,
        EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 100,
            rate_limit_burst: 100,
            timeout: Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        },
        Arc::new(Catalog),
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("e2e_egress_denied_total", "e2e"),
            &["app_id", "reason"],
        )
        .expect("static metric opts"),
        boxed(StaticFlag(true)),
    ))
}

/// What one session observed.
struct Session {
    /// Each invoke's echoed `payload-json` (the guest's rendering of the
    /// `http.send` outcome, see the fixture's module doc).
    outputs: Vec<String>,
    /// The raw `args` JSON of every `http`/`send` host-call, exactly as the
    /// executor put it on the wire.
    wire_args: Vec<serde_json::Value>,
}

/// Runs the real executor against the real guard: handshake, load the
/// fixture, then one `transform` invoke per `specs` entry (each a guest
/// `http.send`), answering every host-call with `guard.send`.
async fn run_session(guard: Arc<EgressGuard>, specs: &[&str]) -> Session {
    let (executor_io, stage_io) = tokio::io::duplex(1024 * 1024);
    let executor = Arc::new(Executor::new(&executor_config(), FixtureSource).expect("executor"));

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
            executor,
            "test-peer",
            Heartbeat::disabled(),
        )
        .await
    });

    let specs: Vec<String> = specs.iter().map(|s| (*s).to_string()).collect();
    let stage = tokio::spawn(stage_side(stage_io, guard, specs, fixture_digest()));

    let session = tokio::time::timeout(Duration::from_secs(120), stage)
        .await
        .expect("stage side finished within the deadline")
        .expect("stage task");
    executor_task
        .await
        .expect("executor task")
        .expect("connection ran to a clean shutdown");
    session
}

async fn stage_side<S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin>(
    mut io: S,
    guard: Arc<EgressGuard>,
    specs: Vec<String>,
    digest: String,
) -> Session {
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
    .expect("write hello-ok");

    write_frame(
        &mut io,
        &Frame::new(
            1000,
            Message::Load(LoadBody {
                tenant_id: 1,
                community_id: 0,
                app_id: APP_ID.to_string(),
                version: "1".to_string(),
                digest: digest.clone(),
                component_key: "bundles/http-fixture/1/x.wasm".to_string(),
                sidecar_key: "bundles/http-fixture/1/x.json".to_string(),
                capabilities: vec![],
                limits: LoadLimits {
                    timeout_ms: 5000,
                    memory_mb: 64,
                },
            }),
        ),
    )
    .await
    .expect("write load");
    let loaded = read_frame(&mut io).await.expect("loaded reply");
    let Message::Loaded(loaded_body) = loaded.message else {
        panic!("expected loaded, got {:?}", loaded.message);
    };
    assert_eq!(loaded_body.digest, digest);

    let mut outputs = Vec::new();
    let mut wire_args = Vec::new();
    for (i, spec) in specs.iter().enumerate() {
        write_frame(
            &mut io,
            &Frame::new(
                1001 + i as u64,
                Message::Invoke(InvokeBody {
                    app_id: APP_ID.to_string(),
                    digest: digest.clone(),
                    export: ExportKind::Transform,
                    payload: serde_json::json!({
                        "platform": "test",
                        "event_type": "http-send",
                        "actor": null,
                        "payload_json": spec,
                        "occurred_at": "2026-09-22T00:00:00.000Z",
                    }),
                    deadline_ms: 30_000,
                    trace: None,
                }),
            ),
        )
        .await
        .expect("write invoke");

        loop {
            let frame = read_frame(&mut io).await.expect("frame from the executor");
            match frame.message {
                Message::HostCall(call) => {
                    assert_eq!(
                        (call.capability, call.op.as_str()),
                        (CapabilityKind::Http, "send"),
                        "the fixture only issues http.send"
                    );
                    wire_args.push(call.args.clone());
                    // The REAL guard answers, exactly as a stage's
                    // `handle_http` does after its permission gate.
                    let body = match guard.send(&call.app_id, &call.args).await {
                        Ok(value) => HostResultBody {
                            result: Some(value),
                            error: None,
                        },
                        Err(err) => HostResultBody {
                            result: None,
                            error: Some(err),
                        },
                    };
                    write_frame(&mut io, &Frame::new(frame.id, Message::HostResult(body)))
                        .await
                        .expect("write host-result");
                }
                Message::Result(body) => {
                    let out = body.payload["payload_json"]
                        .as_str()
                        .unwrap_or_else(|| panic!("no payload_json in {:?}", body.payload))
                        .to_string();
                    outputs.push(out);
                    break;
                }
                other => panic!("unexpected frame from the executor: {other:?}"),
            }
        }
    }

    write_frame(
        &mut io,
        &Frame::new(9999, Message::Shutdown(ShutdownBody { grace_ms: 100 })),
    )
    .await
    .expect("write shutdown");

    Session { outputs, wire_args }
}

fn response(
    status: u16,
    headers: &[(&str, &str)],
    body: &[u8],
    truncated: bool,
) -> Result<TransportResponse, HostResultError> {
    Ok(TransportResponse {
        status,
        headers: headers
            .iter()
            .map(|(n, v)| ((*n).to_string(), (*v).to_string()))
            .collect(),
        body: body.to_vec(),
        truncated,
    })
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// The headline regression: a POST with a non-empty (non-UTF-8) body must
/// actually send that body, and a response carrying headers (including a
/// repeated one) and a binary body must decode -- not fail "malformed
/// host-result".
#[tokio::test(flavor = "multi_thread")]
async fn post_body_is_sent_and_a_response_with_headers_and_body_decodes() {
    let request_body = [0x00_u8, 0xff, 0x10, b'h', b'i'];
    let response_body = [0xde_u8, 0xad, 0xbe, 0xef, 0x00];
    let transport = ScriptedTransport::with(vec![response(
        201,
        &[
            ("content-type", "application/json"),
            ("set-cookie", "a=1"),
            ("set-cookie", "b=2"),
        ],
        &response_body,
        false,
    )]);
    let spec = format!("POST https://{HOST}/hook {} -", hex(&request_body));

    let session = run_session(guard_over(&transport), &[&spec]).await;

    // Guest-visible result: status, every header in order (duplicates
    // kept), the exact response bytes.
    assert_eq!(
        session.outputs,
        vec![format!(
            "ok;status=201;truncated=false;headers=content-type:application/json|set-cookie:a=1|set-cookie:b=2;body_hex={}",
            hex(&response_body)
        )]
    );

    // Network-visible request: the body arrived byte-exact, with the
    // guest's method/URL/headers.
    let requests = transport.requests.lock().unwrap();
    assert_eq!(requests.len(), 1, "exactly one request left the guard");
    assert_eq!(requests[0].method, "POST");
    assert_eq!(requests[0].url, format!("https://{HOST}/hook"));
    assert_eq!(requests[0].body.as_deref(), Some(&request_body[..]));
    for (name, value) in [
        ("content-type", "application/octet-stream"),
        ("x-fixture", "1"),
    ] {
        assert!(
            requests[0]
                .headers
                .iter()
                .any(|(n, v)| n.eq_ignore_ascii_case(name) && v == value),
            "request header {name}: {value} missing from {:?}",
            requests[0].headers
        );
    }

    // And the executor put the body on the wire as `body_base64`, never the
    // legacy `body` array.
    assert_eq!(session.wire_args.len(), 1);
    assert_eq!(session.wire_args[0]["body_base64"], "AP8QaGk=");
    assert!(session.wire_args[0].get("body").is_none());
}

/// No body stays no body, an empty body stays an empty body, and the
/// response's `truncated` flag and empty header/body sets decode.
#[tokio::test(flavor = "multi_thread")]
async fn absent_empty_and_truncated_cases_survive_the_round_trip() {
    let transport = ScriptedTransport::with(vec![
        response(200, &[], b"", true),
        response(204, &[], b"", false),
    ]);
    let get = format!("GET https://{HOST}/ - -");
    let post_empty = format!("POST https://{HOST}/ {} -", "");

    let session = run_session(guard_over(&transport), &[&get, &post_empty]).await;

    assert_eq!(
        session.outputs,
        vec![
            "ok;status=200;truncated=true;headers=;body_hex=".to_string(),
            "ok;status=204;truncated=false;headers=;body_hex=".to_string(),
        ]
    );
    let requests = transport.requests.lock().unwrap();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0].body, None, "GET without a body sends none");
    assert_eq!(
        requests[1].body.as_deref(),
        Some(&[][..]),
        "an empty POST body is an empty body, not no body"
    );
    assert!(session.wire_args[0].get("body_base64").is_none());
    assert_eq!(session.wire_args[1]["body_base64"], "");
}

/// A `secret_refs` list (the executor's `[slot, ref]` pair-list shape) is
/// resolved host-side into the request header, the secret never appears in
/// the guest's arguments, and a response that echoes it back is scrubbed
/// before the guest sees it -- #749's behavior, through the real wire.
#[tokio::test(flavor = "multi_thread")]
async fn header_secret_ref_is_injected_host_side_and_scrubbed_from_the_response() {
    // Single test in this file touching the process environment, under a
    // name unique to it.
    std::env::set_var(SECRET_ENV, SECRET_VALUE);
    let echo = format!("echo {SECRET_VALUE}");
    let transport = ScriptedTransport::with(vec![response(
        200,
        &[("x-echo", echo.as_str())],
        format!("token={SECRET_VALUE}").as_bytes(),
        false,
    )]);
    let spec = format!(
        "POST https://{HOST}/auth {} Authorization=bot-token",
        hex(b"{}")
    );

    let session = run_session(guard_over(&transport), &[&spec]).await;

    // The wire args name only the symbolic ref, as a pair list.
    assert_eq!(
        session.wire_args[0]["secret_refs"],
        serde_json::json!([["Authorization", "bot-token"]])
    );
    assert!(!session.wire_args[0].to_string().contains(SECRET_VALUE));

    // The host injected the real value into the outgoing request...
    let requests = transport.requests.lock().unwrap();
    assert!(
        requests[0]
            .headers
            .iter()
            .any(|(n, v)| n == "Authorization" && v == SECRET_VALUE),
        "Authorization not injected: {:?}",
        requests[0].headers
    );
    assert_eq!(requests[0].body.as_deref(), Some(&b"{}"[..]));

    // ...and the guest never sees it, even when the server reflects it.
    assert_eq!(session.outputs.len(), 1);
    assert!(
        !session.outputs[0].contains(SECRET_VALUE),
        "secret leaked to the guest: {}",
        session.outputs[0]
    );
    assert!(
        session.outputs[0].starts_with("ok;status=200;"),
        "{}",
        session.outputs[0]
    );
    assert!(session.outputs[0].contains(&hex(b"token=[REDACTED]")));
}

/// A request the guard refuses (undeclared host) reaches the guest as an
/// error, and nothing is sent -- a denial is never a silent success.
#[tokio::test(flavor = "multi_thread")]
async fn a_guard_denial_reaches_the_guest_as_an_error_and_sends_nothing() {
    let transport = ScriptedTransport::with(vec![]);
    let session = run_session(guard_over(&transport), &["GET https://8.8.4.4/ - -"]).await;

    assert_eq!(session.outputs.len(), 1);
    assert!(
        session.outputs[0].starts_with("err;"),
        "expected a guest-visible error, got {}",
        session.outputs[0]
    );
    assert!(
        transport.requests.lock().unwrap().is_empty(),
        "a denied request must never reach the transport"
    );
}
