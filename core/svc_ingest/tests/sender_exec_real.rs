#![allow(clippy::unwrap_used, clippy::expect_used)]
//! Public-API, own-process proof of the executor's observability contract:
//! what it LOGS and what it exports as metrics, on the real production path
//! (verifier -> substituter -> `GuardedClient`) against a local fake Twitch.
//! Lives in its own test binary so a process-global subscriber can capture
//! every event deterministically (a scoped subscriber races other tests'
//! tracing callsite caches inside the lib test binary).

use std::collections::{BTreeSet, HashMap};
use std::net::{IpAddr, Ipv4Addr};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use bundle_host_http::guarded::GuardedLimits;
use svc_ingest::connector::sender_exec::{
    exec_count, register_metrics, BuildError, BuiltTemplate, ExecError, ExecRequest,
    ExecutorConfig, FlagGate, PlatformPolicy, RequestTpl, SenderAction, SenderExecutor, SlotError,
    SlotRequest, SlotSource, SlotValue, TemplateBuilder,
};
use svc_ingest::outbound_ops::OutboundAction;
use wiremock::matchers::any;
use wiremock::{Mock, MockServer, ResponseTemplate};

const DIGEST: &str = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
const TOKEN: &str = "SECRET-TOKEN-XYZ";

static LOGS: OnceLock<Arc<Mutex<String>>> = OnceLock::new();

struct Capture(Arc<Mutex<String>>);
struct Text<'a>(&'a mut String);

impl tracing::field::Visit for Text<'_> {
    fn record_debug(&mut self, f: &tracing::field::Field, v: &dyn std::fmt::Debug) {
        use std::fmt::Write;
        let _ = write!(self.0, "{}={:?} ", f.name(), v);
    }
}

impl tracing::Subscriber for Capture {
    fn enabled(&self, m: &tracing::Metadata<'_>) -> bool {
        // Our code and the guarded client. Third-party transport debug logs
        // (hyper) are not the executor's and do name socket addresses.
        m.target().starts_with("svc_ingest") || m.target().starts_with("bundle_host_http")
    }
    fn new_span(&self, a: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        a.record(&mut Text(&mut self.0.lock().unwrap()));
        tracing::span::Id::from_u64(1)
    }
    fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
    fn event(&self, e: &tracing::Event<'_>) {
        e.record(&mut Text(&mut self.0.lock().unwrap()));
    }
    fn enter(&self, _: &tracing::span::Id) {}
    fn exit(&self, _: &tracing::span::Id) {}
}

struct Builder(RequestTpl);

impl TemplateBuilder for Builder {
    async fn build_request(
        &self,
        _p: &str,
        _a: &SenderAction,
    ) -> Result<BuiltTemplate, BuildError> {
        Ok(BuiltTemplate {
            tpl: self.0.clone(),
            component_digest: DIGEST.to_string(),
        })
    }
}

struct Slots(Option<SlotError>);

impl SlotSource for Slots {
    async fn resolve(&self, _r: &SlotRequest<'_>) -> Result<HashMap<String, SlotValue>, SlotError> {
        if let Some(e) = self.0 {
            return Err(e);
        }
        Ok([
            ("token", TOKEN),
            ("client_id", "cid123"),
            ("broadcaster_id", "111"),
            ("sender_id", "222"),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_string(), SlotValue::new(v.to_string())))
        .collect())
    }
}

struct Flag(bool);

impl FlagGate for Flag {
    async fn enabled(&self, _f: &str) -> bool {
        self.0
    }
}

fn twitch_send_tpl(base: &str) -> RequestTpl {
    let s = |a: &str, b: &str| (a.to_string(), b.to_string());
    RequestTpl {
        method: "POST".into(),
        url: format!("{base}/helix/chat/messages"),
        headers: vec![
            s("Authorization", "Bearer {{TWITCH_TOKEN}}"),
            s("Client-Id", "{{TWITCH_CLIENT_ID}}"),
            s("Content-Type", "application/json"),
        ],
        body: Some(
            br#"{"broadcaster_id":"{{TWITCH_BROADCASTER_ID}}","sender_id":"{{TWITCH_SENDER_ID}}","message":"private message body"}"#
                .to_vec(),
        ),
        secret_refs: vec![
            s("{{TWITCH_TOKEN}}", "token"),
            s("{{TWITCH_CLIENT_ID}}", "client_id"),
            s("{{TWITCH_BROADCASTER_ID}}", "broadcaster_id"),
            s("{{TWITCH_SENDER_ID}}", "sender_id"),
        ],
    }
}

fn executor(server: &MockServer, slots: Slots, flag: bool) -> SenderExecutor<Builder, Slots, Flag> {
    let mut policy = PlatformPolicy::twitch();
    policy.hosts = vec![server.address().to_string()];
    policy.allow_http = true;
    let mut digests = HashMap::new();
    digests.insert("twitch".to_string(), DIGEST.to_string());
    let cfg = ExecutorConfig::new(vec![policy], digests)
        .unwrap()
        .with_client(
            GuardedLimits {
                max_redirects: 0,
                timeout: Duration::from_secs(5),
                max_response_bytes: 4096,
            },
            vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        );
    SenderExecutor::new(
        Builder(twitch_send_tpl(&server.uri())),
        slots,
        Flag(flag),
        cfg,
    )
}

#[tokio::test]
async fn logs_and_metrics_are_pii_free_secret_free_and_bounded() {
    let buf = Arc::new(Mutex::new(String::new()));
    LOGS.set(buf.clone()).unwrap();
    tracing::subscriber::set_global_default(Capture(buf.clone())).unwrap();

    let action = OutboundAction::ChatSend {
        channel: "#chan".into(),
        text: "private message body".into(),
        origin_channel: Some("#chan".into()),
        tenant_id: None,
    };
    let req = ExecRequest {
        platform: "twitch",
        action: &action,
        community_id: Some(7),
    };
    let ok0 = exec_count("twitch", "chat.send", "ok");
    let up0 = exec_count("twitch", "chat.send", "upstream_error");
    let rl0 = exec_count("twitch", "chat.send", "rate_limited");
    let sl0 = exec_count("twitch", "chat.send", "slot_failed");
    let fo0 = exec_count("twitch", "chat.send", "flag_off");

    for status in [200u16, 401, 429] {
        let server = MockServer::start().await;
        Mock::given(any())
            .respond_with(ResponseTemplate::new(status))
            .mount(&server)
            .await;
        let res = executor(&server, Slots(None), true).execute(&req).await;
        let shown = format!("{res:?}");
        for needle in [TOKEN, "helix", "private message", "cid123"] {
            assert!(!shown.contains(needle), "result leaked {needle}");
        }
    }
    let server = MockServer::start().await;
    let refused = executor(
        &server,
        Slots(Some(SlotError::Refused("no_connection"))),
        true,
    )
    .execute(&req)
    .await
    .unwrap_err();
    assert!(matches!(refused, ExecError::Slot(_)));
    assert_eq!(
        executor(&server, Slots(None), false)
            .execute(&req)
            .await
            .unwrap_err(),
        ExecError::FlagOff
    );
    assert!(server.received_requests().await.unwrap().is_empty());

    assert_eq!(exec_count("twitch", "chat.send", "ok"), ok0 + 1);
    assert_eq!(exec_count("twitch", "chat.send", "upstream_error"), up0 + 1);
    assert_eq!(exec_count("twitch", "chat.send", "rate_limited"), rl0 + 1);
    assert_eq!(exec_count("twitch", "chat.send", "slot_failed"), sl0 + 1);
    assert_eq!(exec_count("twitch", "chat.send", "flag_off"), fo0 + 1);

    let logs = buf.lock().unwrap().clone();
    assert!(
        logs.contains("outbound executor refused or failed"),
        "{logs}"
    );
    assert!(
        logs.contains("code=\"no_connection\"") || logs.contains("code=no_connection"),
        "{logs}"
    );
    for needle in [
        TOKEN,
        "private message",
        "helix",
        "111",
        "222",
        "cid123",
        "#chan",
        "127.0.0.1",
        "Bearer",
    ] {
        assert!(!logs.contains(needle), "log leaked {needle:?}: {logs}");
    }

    // Exported series: only the bounded label vocabularies.
    let registry = prometheus::Registry::new();
    register_metrics(&registry).unwrap();
    let mut platforms = BTreeSet::new();
    let mut kinds = BTreeSet::new();
    for fam in registry.gather() {
        for m in fam.get_metric() {
            for l in m.get_label() {
                match l.name() {
                    "platform" => platforms.insert(l.value().to_string()),
                    "kind" => kinds.insert(l.value().to_string()),
                    _ => false,
                };
            }
        }
    }
    assert!(platforms.is_subset(
        &["twitch", "discord", "youtube", "other"]
            .map(String::from)
            .into()
    ));
    assert!(kinds.is_subset(
        &["chat.send", "chat.delete", "dm.send", "other"]
            .map(String::from)
            .into()
    ));
    assert!(!platforms.is_empty());
}
