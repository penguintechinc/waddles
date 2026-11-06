//! Real-path proof of the YouTube `OverlayProfile` (PR-F3): the profile derives
//! the `PollConfig`; svc-ingest's real `PollTransport` polls a local fake
//! YouTube API; the real `ConnectorDriver` carries each response through the
//! production wire (`WireConnectorInvoker` -> `ExecutorLink` -> real
//! `run_connection`/`ConnectorHost`) into the REAL compiled YouTube overlay
//! (`fixtures/youtube_overlay.wasm`), whose `identity.lookup` goes through the
//! executor's real host-call path (`scripted_bridge`). Only the two ends are
//! synthetic: the fake upstream and a recording sink.
//!
//! Timeline (server-scripted, so every transport rule is observed in order):
//! page 1 (+hint 100 ms, clamped to 2 s) -> page 2 -> 403 quotaExceeded with
//! Retry-After 2 (back off, keep the cursor) -> page 3 -> 403 liveChatEnded
//! (park). A second connection for a CHANGED `live_chat_id` then starts clean.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{HashSet, VecDeque};
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_executor::config::CliConfig;
use bundle_executor::connector::ConnectorLimits;
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::Executor;
use bundle_executor::manifest::{VerifiedManifest, WitWorld, PERM_CONNECTOR_PII_READ};
use bundle_executor::parity_support::{pseudonym_uuid, scripted_bridge, CallLog};
use bundle_executor::wire::run_connection;
use clap::Parser;
use connection_resolver::{BindingSummary, ConnectionKind, ConnectionRef, SecretString};
use connector_e2e::{digest_of, NoSource};
use svc_ingest::connector::secrets::{Destination, ResolvedAuth, TransportSecretSource};
use svc_ingest::connector::webhook::{BoxFut, SecretError};
use svc_ingest::connector::{
    register_connector_metrics, ConnectionCtx, ConnectorDriver, ConnectorEvent, ConnectorRegistry,
    EventSink, ExecutorLink, PinnedConnector, PollConfig, PollTransport, RunEnd, SinkError,
    WireConnectorInvoker,
};
use svc_ingest::connector_runtime::feed::TransportPlan;
use svc_ingest::connector_runtime::plan::{OverlayProfile, RuntimeEnv};
use svc_ingest::connector_runtime::profiles::youtube::YoutubeProfile;
use tokio::io::{duplex, AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tokio::time::Instant;

const WASM: &[u8] = include_bytes!("../../bundle_executor/tests/fixtures/youtube_overlay.wasm");
const CHAT: &str = "Cg0KC-abc_123";
const CHAT2: &str = "NewChatId_9";
const TOKEN: &str = "ya29.e2e-token";

/// One scripted upstream answer.
struct Scripted {
    status: u16,
    retry_after: Option<&'static str>,
    body: serde_json::Value,
}

/// One request the fake API observed.
struct Seen {
    path: String,
    query: Vec<(String, String)>,
    auth: Option<String>,
    at: Instant,
}

/// A minimal local HTTP/1.1 API answering from a script, in order.
struct FakeApi {
    addr: SocketAddr,
    script: Arc<Mutex<VecDeque<Scripted>>>,
    seen: Arc<Mutex<Vec<Seen>>>,
}

impl FakeApi {
    async fn start(script: Vec<Scripted>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let script = Arc::new(Mutex::new(VecDeque::from(script)));
        let seen = Arc::new(Mutex::new(Vec::new()));
        let (s2, n2) = (Arc::clone(&script), Arc::clone(&seen));
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    break;
                };
                let (s3, n3) = (Arc::clone(&s2), Arc::clone(&n2));
                tokio::spawn(async move {
                    let mut buf = Vec::new();
                    let mut chunk = [0u8; 2048];
                    while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
                        match sock.read(&mut chunk).await {
                            Ok(0) | Err(_) => return,
                            Ok(n) => buf.extend_from_slice(&chunk[..n]),
                        }
                    }
                    let head = String::from_utf8_lossy(&buf).into_owned();
                    let target = head
                        .lines()
                        .next()
                        .and_then(|l| l.split(' ').nth(1))
                        .expect("request line")
                        .to_owned();
                    let auth = head
                        .lines()
                        .find_map(|l| l.strip_prefix("authorization: "))
                        .map(str::to_owned);
                    let (path, query) = split_target(&target);
                    n3.lock().unwrap().push(Seen {
                        path,
                        query,
                        auth,
                        at: Instant::now(),
                    });
                    let next = s3.lock().unwrap().pop_front();
                    let (status, retry_after, body) = match next {
                        Some(s) => (s.status, s.retry_after, s.body.to_string()),
                        None => (500, None, "{}".to_owned()),
                    };
                    let mut resp = format!(
                        "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n",
                        body.len()
                    );
                    if let Some(r) = retry_after {
                        resp.push_str(&format!("retry-after: {r}\r\n"));
                    }
                    resp.push_str("\r\n");
                    resp.push_str(&body);
                    let _ = sock.write_all(resp.as_bytes()).await;
                    let _ = sock.shutdown().await;
                });
            }
        });
        Self { addr, script, seen }
    }
}

fn item(chat: &str, id: &str, author: &str, text: &str) -> serde_json::Value {
    serde_json::json!({
        "kind": "youtube#liveChatMessage", "id": id,
        "snippet": {
            "type": "textMessageEvent", "liveChatId": chat,
            "publishedAt": "2026-11-05T00:00:00.000Z", "displayMessage": text
        },
        "authorDetails": {
            "channelId": author, "displayName": "Viewer",
            "isChatModerator": false, "isChatOwner": false, "isChatSponsor": false
        }
    })
}

fn page(next: &str, hint_ms: u64, items: Vec<serde_json::Value>) -> Scripted {
    Scripted {
        status: 200,
        retry_after: None,
        body: serde_json::json!({
            "kind": "youtube#liveChatMessageListResponse",
            "nextPageToken": next, "pollingIntervalMillis": hint_ms, "items": items
        }),
    }
}

fn forbidden(reason: &str, retry_after: Option<&'static str>) -> Scripted {
    Scripted {
        status: 403,
        retry_after,
        body: serde_json::json!({"error":{"code":403,"errors":[{"reason":reason}]}}),
    }
}

struct Token;

impl TransportSecretSource for Token {
    fn resolve(
        &self,
        _requested: ConnectionRef,
        _dest: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
        Box::pin(async {
            Ok(ResolvedAuth {
                secret: SecretString::new(TOKEN.to_owned()),
                header: None,
            })
        })
    }
}

/// Records every published event.
#[derive(Clone, Default)]
struct Recorder(Arc<Mutex<Vec<(ConnectionCtx, ConnectorEvent)>>>);

impl EventSink for Recorder {
    async fn publish(&self, ctx: &ConnectionCtx, event: ConnectorEvent) -> Result<(), SinkError> {
        self.0.lock().unwrap().push((ctx.clone(), event));
        Ok(())
    }
}

fn binding(chat: &str) -> BindingSummary {
    BindingSummary {
        tenant_id: 1,
        secret_version: 1,
        community_id: 7,
        connection_id: 12,
        provider: "youtube".to_owned(),
        kind: ConnectionKind::PlatformOauth,
        metadata: [("live_chat_id", chat), ("tenant_id", "3")]
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect(),
    }
}

/// The profile's config plus ONLY the test-permitted loopback overrides.
fn derived(api: &FakeApi, chat: &str) -> (PollConfig, u64) {
    let env = RuntimeEnv {
        environment: "test".to_owned(),
        max_frame_bytes: 262_144,
        egress_allow: vec!["127.0.0.1".parse().unwrap()],
        heartbeat_enabled: false,
    };
    let mut plans = YoutubeProfile::new().plan(&binding(chat), &env).unwrap();
    assert_eq!(plans.len(), 1);
    let d = plans.remove(0);
    let fp = d.fingerprint;
    let TransportPlan::Poll(mut cfg) = d.plan else {
        panic!("poll plan expected");
    };
    let rest = cfg
        .url
        .strip_prefix("https://www.googleapis.com")
        .expect("the profile's host constant");
    cfg.url = format!("http://127.0.0.1:{}{rest}", api.addr.port());
    cfg.allow_loopback_insecure = true;
    (*cfg, fp)
}

struct Rig {
    registry: Arc<ConnectorRegistry>,
    invoker: Arc<WireConnectorInvoker>,
    calls: CallLog,
    _executor: Arc<Executor<NoSource>>,
    _conn: tokio::task::JoinHandle<()>,
    _link: Arc<ExecutorLink>,
}

/// Pins the REAL youtube overlay (with the pii grant and the scripted
/// identity bridge) in a real executor and attaches it over the wire.
async fn rig() -> Rig {
    let cfg = CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-ingest:8301",
    ])
    .unwrap();
    let executor = Arc::new(Executor::new(&cfg, NoSource).unwrap());
    let digest = digest_of(WASM);
    let manifest = VerifiedManifest::new(
        "waddles.core.service.youtube",
        digest.clone(),
        WitWorld::ConnectorTools,
        HashSet::from([PERM_CONNECTOR_PII_READ.to_string()]),
    );
    let (bridge, calls) = scripted_bridge();
    executor
        .pin_connector(
            &manifest,
            WASM.to_vec(),
            ConnectorLimits::from_config(&cfg),
            Some(bridge),
        )
        .await
        .unwrap();
    let mut registry = ConnectorRegistry::new();
    registry
        .register(PinnedConnector::new(
            "youtube".to_owned(),
            "waddles.core.service.youtube".to_owned(),
            digest,
            262_144,
        ))
        .unwrap();
    let registry = Arc::new(registry);
    let link = Arc::new(ExecutorLink::new());
    let invoker = Arc::new(WireConnectorInvoker::new(
        Arc::clone(&link),
        Arc::clone(&registry),
    ));
    let (stage_io, exec_io) = duplex(4 << 20);
    let ex = Arc::clone(&executor);
    let hello = ex.hello(false, "runc");
    let conn = tokio::spawn(async move {
        let _ = run_connection(exec_io, hello, ex, "connector-e2e", Heartbeat::disabled()).await;
    });
    link.attach(stage_io).await.unwrap();
    Rig {
        registry,
        invoker,
        calls,
        _executor: executor,
        _conn: conn,
        _link: link,
    }
}

fn is_uuid(s: &str) -> bool {
    s.len() == 36
        && s.char_indices().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => c == '-',
            _ => c.is_ascii_hexdigit(),
        })
}

fn param(q: &[(String, String)], k: &str) -> Option<String> {
    q.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone())
}

/// Percent-decodes a query component (test-local; the server is a fake).
fn pct_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            let h = std::str::from_utf8(&b[i + 1..i + 3]).unwrap();
            out.push(u8::from_str_radix(h, 16).unwrap());
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    String::from_utf8(out).unwrap()
}

/// Splits a request target into its path and decoded query pairs.
fn split_target(t: &str) -> (String, Vec<(String, String)>) {
    let (path, q) = t.split_once('?').unwrap_or((t, ""));
    let pairs = q
        .split('&')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p.split_once('=').expect("k=v");
            (pct_decode(k), pct_decode(v))
        })
        .collect();
    (path.to_owned(), pairs)
}

fn driver(rig: &Rig, sink: &Recorder) -> ConnectorDriver<WireConnectorInvoker, Recorder> {
    let prom = prometheus::Registry::new();
    let metrics = register_connector_metrics(&prom).unwrap();
    ConnectorDriver::new(
        Arc::clone(&rig.registry),
        Arc::clone(&rig.invoker),
        Arc::new(sink.clone()),
        metrics,
    )
    .with_backoff_cap(Duration::from_millis(50))
}

#[tokio::test(flavor = "multi_thread")]
async fn youtube_profile_polls_normalizes_backs_off_parks_and_rediscovers() {
    let api = FakeApi::start(vec![
        page(
            "NP1",
            100,
            vec![
                item(CHAT, "m1", "UCauthorA", "hello"),
                item(CHAT, "m2", "UCauthorB", "world"),
            ],
        ),
        page("NP2", 2000, vec![item(CHAT, "m3", "UCauthorA", "again")]),
        forbidden("quotaExceeded", Some("2")),
        page("NP3", 3000, vec![item(CHAT, "m4", "UCauthorB", "back")]),
        forbidden("liveChatEnded", None),
    ])
    .await;
    let rig = rig().await;
    let sink = Recorder::default();

    // ---- phase 1: runs until liveChatEnded parks it ----
    let (cfg, fp1) = derived(&api, CHAT);
    let transport = PollTransport::new(cfg, Token).unwrap();
    let (_keep, shutdown) = oneshot::channel::<()>();
    let end = tokio::time::timeout(
        Duration::from_secs(60),
        driver(&rig, &sink).run(transport, shutdown),
    )
    .await
    .expect("driver must finish (park) in time")
    .expect("driver run");
    assert_eq!(end, RunEnd::Parked, "liveChatEnded must park, not retry");

    {
        let seen = api.seen.lock().unwrap();
        assert_eq!(seen.len(), 5, "five polls, then parked (no hammering)");
        for s in seen.iter() {
            assert_eq!(s.path.as_str(), "/youtube/v3/liveChat/messages");
            assert_eq!(s.auth.as_deref(), Some(&*format!("Bearer {TOKEN}")));
            assert_eq!(param(&s.query, "liveChatId").as_deref(), Some(CHAT));
            assert_eq!(
                param(&s.query, "part").as_deref(),
                Some("id,snippet,authorDetails")
            );
            assert_eq!(param(&s.query, "maxResults").as_deref(), Some("200"));
        }
        let tokens: Vec<_> = seen.iter().map(|s| param(&s.query, "pageToken")).collect();
        assert_eq!(
            tokens,
            vec![
                None,
                Some("NP1".to_owned()),
                Some("NP2".to_owned()),
                Some("NP2".to_owned()),
                Some("NP3".to_owned())
            ],
            "cursor advances on Published and is HELD across the 403 back-off"
        );
        let gap = |a: usize, b: usize| seen[b].at.duration_since(seen[a].at);
        assert!(
            gap(0, 1) >= Duration::from_millis(1900),
            "100 ms server hint is clamped up to the 2 s floor: {:?}",
            gap(0, 1)
        );
        assert!(
            gap(2, 3) >= Duration::from_millis(1900) && gap(2, 3) < Duration::from_secs(20),
            "quota 403 waited for Retry-After (2 s), not the 30 s base: {:?}",
            gap(2, 3)
        );
        assert!(
            gap(3, 4) >= Duration::from_millis(2900),
            "page 3's server interval (3 s) applied: {:?}",
            gap(3, 4)
        );
    }

    // Normalized events: non-zero denominator, UUID actors, no raw handles.
    {
        let events = sink.0.lock().unwrap();
        assert_eq!(events.len(), 4, "m1..m4 published");
        let want = [
            ("UCauthorA", "hello"),
            ("UCauthorB", "world"),
            ("UCauthorA", "again"),
            ("UCauthorB", "back"),
        ];
        for ((ctx, ev), (author, text)) in events.iter().zip(want) {
            assert_eq!(ev.platform, "youtube");
            assert_eq!(ev.event_type, "message");
            assert_eq!(ctx.platform, "youtube");
            let actor = ev.actor.as_deref().expect("actor");
            assert!(is_uuid(actor), "actor is a UUID token: {actor}");
            assert_eq!(actor, pseudonym_uuid("youtube", author));
            assert!(!ev.payload_json.contains(author) || ev.payload_json.contains("author_id"));
            let p: serde_json::Value = serde_json::from_str(&ev.payload_json).unwrap();
            assert_eq!(p["text"], text);
            assert_eq!(p["live_chat_id"], CHAT);
        }
    }
    let ops = rig.calls.lock().unwrap().clone();
    assert!(
        ops.iter().any(|o| o == "identity.lookup"),
        "the real identity host-call path ran: {ops:?}"
    );

    // ---- phase 2: the binding now carries a NEW live_chat_id ----
    let (cfg2, fp2) = derived(&api, CHAT2);
    assert_ne!(fp1, fp2, "a changed live_chat_id restarts the connection");
    api.script.lock().unwrap().push_back(page(
        "X1",
        2000,
        vec![item(CHAT2, "n1", "UCauthorA", "new")],
    ));
    let before = sink.0.lock().unwrap().len();
    let transport = PollTransport::new(cfg2, Token).unwrap();
    let (stop, shutdown) = oneshot::channel::<()>();
    let d2 = driver(&rig, &sink);
    let run = tokio::spawn(async move { d2.run(transport, shutdown).await });
    let deadline = Instant::now() + Duration::from_secs(30);
    while sink.0.lock().unwrap().len() <= before {
        assert!(
            Instant::now() < deadline,
            "new chat never produced an event"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    stop.send(()).unwrap();
    assert_eq!(run.await.unwrap().unwrap(), RunEnd::Shutdown);
    let seen = api.seen.lock().unwrap();
    let first_new = &seen[5];
    assert_eq!(
        param(&first_new.query, "liveChatId").as_deref(),
        Some(CHAT2)
    );
    assert_eq!(
        param(&first_new.query, "pageToken"),
        None,
        "a new chat starts from no cursor"
    );
    let events = sink.0.lock().unwrap();
    assert_eq!(events[before].1.event_type, "message");
    let p: serde_json::Value = serde_json::from_str(&events[before].1.payload_json).unwrap();
    assert_eq!(p["live_chat_id"], CHAT2);
}
