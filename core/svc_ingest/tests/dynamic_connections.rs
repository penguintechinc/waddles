//! Real-path integration tests for the dynamic Twitch connections supervisor
//! (`svc_ingest::connections`): the production `HubConnectionsClient` talks
//! real HTTP to a stateful mock hub-api (axum), and the production bot task
//! opens real TCP sockets to a fake Twitch IRC server -- nothing between the
//! supervisor and the wire is stubbed. Only the spine `XADD` is recorded in
//! memory (a live Valkey is not available to `cargo test`).
//!
//! Each test builds its own mock hub, fake IRC server, metrics registry and log
//! capture, so tests are independent and run in parallel.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::routing::{get, post};
use axum::{Json, Router};
use penguin_spine::{KeyRing, Scope, SpineError, SpineMetrics, StageEnvelope};
use serde_json::{json, Value};
use svc_ingest::connections::bot::{IrcSettings, PublishCtx};
use svc_ingest::connections::client::{
    BearerSource, ClientError, HubClientConfig, HubConnectionsClient,
};
use svc_ingest::connections::metrics::ConnectionsMetrics;
use svc_ingest::connections::supervisor::{
    ReconcileError, ScopeMode, Supervisor, SupervisorSettings,
};
use svc_ingest::publish::EventAppender;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpListener;
use tokio::sync::{broadcast, oneshot, watch};

const BEARER: &str = "test-machine-jwt";

// --------------------------------------------------------------------------
// Mock hub-api
// --------------------------------------------------------------------------

/// What the mock answers for one `(community, connection)` resolve.
#[derive(Clone)]
enum Behavior {
    Ok {
        token: String,
        bot: String,
    },
    Fail {
        status: u16,
        body: Value,
    },
    FailThenOk {
        fails_left: u32,
        status: u16,
        token: String,
        bot: String,
    },
}

#[derive(Default)]
struct HubState {
    bindings: Vec<Value>,
    behaviors: HashMap<(i64, i64), Behavior>,
    list_status: Option<u16>,
    /// Replaces the list answer wholesale (status, body) -- malformed/oversized
    /// body tests.
    list_override: Option<(u16, Value)>,
    resolve_calls: Vec<(i64, i64)>,
    list_queries: Vec<HashMap<String, String>>,
    bad_auth: u32,
    /// The bearer the mock accepts (`None` = the static test bearer).
    expected_bearer: Option<String>,
    /// `POST /internal/service-token` calls: (SA bearer token, JSON body).
    token_requests: Vec<(String, Value)>,
    /// Status the token endpoint answers with (0 = 200 + a minted token).
    token_status: u16,
    /// Tokens the token endpoint mints in order (then `MINTED_JWT`) -- lets a
    /// test simulate hub-api rotating its signing key between two mints.
    mint_queue: Vec<String>,
}

#[derive(Clone)]
struct MockHub {
    state: Arc<Mutex<HubState>>,
    addr: SocketAddr,
}

fn auth_ok(st: &HubState, headers: &HeaderMap) -> bool {
    let want = st.expected_bearer.as_deref().unwrap_or(BEARER);
    headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v == format!("Bearer {want}"))
}

/// The machine JWT the mock token endpoint mints.
const MINTED_JWT: &str = "minted-machine-jwt";

/// hub-api's machine-JWT bootstrap (`POST /internal/service-token`).
async fn hub_service_token(
    State(state): State<Arc<Mutex<HubState>>>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> (StatusCode, Json<Value>) {
    let mut st = state.lock().unwrap();
    let sa = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or_default()
        .to_string();
    st.token_requests.push((sa, body));
    if st.token_status != 0 {
        return (
            StatusCode::from_u16(st.token_status).unwrap(),
            Json(json!({"error": "denied"})),
        );
    }
    let token = if st.mint_queue.is_empty() {
        MINTED_JWT.to_string()
    } else {
        st.mint_queue.remove(0)
    };
    (
        StatusCode::OK,
        Json(json!({"token": token, "expires_in": 600})),
    )
}

async fn hub_list(
    State(state): State<Arc<Mutex<HubState>>>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
) -> (StatusCode, Json<Value>) {
    let mut st = state.lock().unwrap();
    if !auth_ok(&st, &headers) {
        st.bad_auth += 1;
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "unauthorized"})),
        );
    }
    st.list_queries.push(query.clone());
    if let Some((status, body)) = st.list_override.clone() {
        return (StatusCode::from_u16(status).unwrap(), Json(body));
    }
    if let Some(status) = st.list_status {
        return (
            StatusCode::from_u16(status).unwrap(),
            Json(
                json!({"success": false, "error": {"code": "CONNECTIONS_UNAVAILABLE", "message": "x"}}),
            ),
        );
    }
    let after: i64 = query.get("after").and_then(|v| v.parse().ok()).unwrap_or(0);
    let limit: usize = query
        .get("limit")
        .and_then(|v| v.parse().ok())
        .unwrap_or(500);
    let page: Vec<Value> = st
        .bindings
        .iter()
        .filter(|b| b["grantId"].as_i64().unwrap_or(0) > after)
        .take(limit)
        .cloned()
        .collect();
    let next_after = if page.len() == limit {
        page.last().and_then(|b| b["grantId"].as_i64())
    } else {
        None
    };
    (
        StatusCode::OK,
        Json(json!({"success": true, "items": page, "nextAfter": next_after})),
    )
}

async fn hub_resolve(
    State(state): State<Arc<Mutex<HubState>>>,
    headers: HeaderMap,
    Path((community, connection)): Path<(i64, i64)>,
) -> (StatusCode, Json<Value>) {
    let mut st = state.lock().unwrap();
    if !auth_ok(&st, &headers) {
        st.bad_auth += 1;
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "unauthorized"})),
        );
    }
    st.resolve_calls.push((community, connection));
    let key = (community, connection);
    let behavior = st.behaviors.get(&key).cloned();
    let ok = |token: &str, bot: &str| {
        // An empty `bot` means "hub-api sent no bot_login at all" (fail-closed test).
        let metadata = if bot.is_empty() {
            json!({"channel_login": "ignored"})
        } else {
            json!({"bot_login": bot, "channel_login": "ignored"})
        };
        (
            StatusCode::OK,
            Json(json!({
                "success": true, "connectionId": connection, "communityId": community,
                "provider": "twitch", "accessToken": token, "expiresAt": null,
                "metadata": metadata
            })),
        )
    };
    match behavior {
        Some(Behavior::Ok { token, bot }) => ok(&token, &bot),
        Some(Behavior::Fail { status, body }) => {
            (StatusCode::from_u16(status).unwrap(), Json(body))
        }
        Some(Behavior::FailThenOk {
            fails_left,
            status,
            token,
            bot,
        }) => {
            if fails_left > 0 {
                st.behaviors.insert(
                    key,
                    Behavior::FailThenOk {
                        fails_left: fails_left - 1,
                        status,
                        token,
                        bot,
                    },
                );
                (
                    StatusCode::from_u16(status).unwrap(),
                    Json(json!({"success": false, "error": {"code": "CONNECTIONS_UNAVAILABLE"}})),
                )
            } else {
                ok(&token, &bot)
            }
        }
        None => (
            StatusCode::NOT_FOUND,
            Json(json!({"success": false, "error": {"code": "NOT_FOUND"}})),
        ),
    }
}

impl MockHub {
    async fn start() -> Self {
        let state = Arc::new(Mutex::new(HubState::default()));
        let app = Router::new()
            .route("/internal/service-token", post(hub_service_token))
            .route("/api/v1/internal/provider-connections", get(hub_list))
            .route(
                "/api/v1/internal/communities/{community}/provider-connections/{connection}/resolve",
                post(hub_resolve),
            )
            .with_state(state.clone());
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        Self { state, addr }
    }

    /// Adds one binding (and its OK resolve behavior). `grant == connection ==
    /// community` ids keep the tests readable.
    fn add(&self, id: i64, bot: &str, channel: &str) {
        self.add_with(id, bot, channel, json!({}));
    }

    fn add_with(&self, id: i64, bot: &str, channel: &str, extra: Value) {
        let mut binding = json!({
            "grantId": id, "connectionId": id, "communityId": id, "tenantId": 1,
            "provider": "twitch", "resourceType": "channel", "resourceId": format!("rid{id}"),
            "capabilities": ["chat_ingest"], "expiresAt": null, "updatedAt": "v1",
            "metadata": {"bot_login": bot, "channel_login": channel}
        });
        for (k, v) in extra.as_object().unwrap() {
            binding[k] = v.clone();
        }
        let mut st = self.state.lock().unwrap();
        st.bindings.push(binding);
        st.behaviors.insert(
            (id, id),
            Behavior::Ok {
                token: format!("tok-{bot}-secret"),
                bot: bot.to_string(),
            },
        );
    }

    fn remove(&self, id: i64) {
        self.state
            .lock()
            .unwrap()
            .bindings
            .retain(|b| b["grantId"].as_i64() != Some(id));
    }

    fn set_behavior(&self, id: i64, behavior: Behavior) {
        self.state
            .lock()
            .unwrap()
            .behaviors
            .insert((id, id), behavior);
    }

    fn resolves_for(&self, id: i64) -> usize {
        self.state
            .lock()
            .unwrap()
            .resolve_calls
            .iter()
            .filter(|c| **c == (id, id))
            .count()
    }

    fn total_resolves(&self) -> usize {
        self.state.lock().unwrap().resolve_calls.len()
    }
}

// --------------------------------------------------------------------------
// Fake Twitch IRC server
// --------------------------------------------------------------------------

#[derive(Default)]
struct IrcState {
    /// Every line each accepted connection sent, in order.
    conns: Vec<Vec<String>>,
    open: usize,
    reject_auth: bool,
}

#[derive(Clone)]
struct FakeIrc {
    port: u16,
    state: Arc<Mutex<IrcState>>,
    push: broadcast::Sender<String>,
    kill: watch::Sender<u64>,
    /// While `true` the server withholds its `001` registration reply.
    stall: watch::Sender<bool>,
}

impl FakeIrc {
    async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let state = Arc::new(Mutex::new(IrcState::default()));
        let (push, _) = broadcast::channel::<String>(64);
        let (kill, _) = watch::channel(0u64);
        let (stall, _) = watch::channel(false);
        let this = Self {
            port,
            state: state.clone(),
            push: push.clone(),
            kill: kill.clone(),
            stall: stall.clone(),
        };
        tokio::spawn(async move {
            loop {
                let Ok((socket, _)) = listener.accept().await else {
                    return;
                };
                let idx = {
                    let mut st = state.lock().unwrap();
                    st.conns.push(Vec::new());
                    st.open += 1;
                    st.conns.len() - 1
                };
                let state = state.clone();
                let mut push_rx = push.subscribe();
                let mut kill_rx = kill.subscribe();
                let mut stall_rx = stall.subscribe();
                tokio::spawn(async move {
                    let (read, mut write) = socket.into_split();
                    let mut reader = BufReader::new(read);
                    // Channels this connection JOINed (lowercase, with `#`) --
                    // like real Twitch, only their chat is delivered to it.
                    let mut joined_channels: Vec<String> = Vec::new();
                    let mut joined = false;
                    loop {
                        let mut line = String::new();
                        tokio::select! {
                            n = reader.read_line(&mut line) => {
                                match n {
                                    Ok(0) | Err(_) => break,
                                    Ok(_) => {
                                        let text = line.trim_end().to_string();
                                        let reject = state.lock().unwrap().reject_auth;
                                        state.lock().unwrap().conns[idx].push(text.clone());
                                        if text.starts_with("NICK ") {
                                            while *stall_rx.borrow() {
                                                if stall_rx.changed().await.is_err() { break; }
                                            }
                                            let reply = if reject {
                                                ":tmi.twitch.tv 464 * :Password incorrect\r\n"
                                            } else {
                                                ":tmi.twitch.tv 001 bot :Welcome\r\n"
                                            };
                                            if write.write_all(reply.as_bytes()).await.is_err() { break; }
                                        }
                                        if let Some(list) = text.strip_prefix("JOIN ") {
                                            joined = true;
                                            joined_channels.extend(list.split(',').map(str::to_string));
                                        }
                                    }
                                }
                            }
                            pushed = push_rx.recv(), if joined => {
                                if let Ok(raw) = pushed {
                                    // `\u{2}`: deliver invalid UTF-8 to every joined
                                    // connection (a corrupted line).
                                    if raw.starts_with('\u{2}') {
                                        if write.write_all(b"\xff\xfe\r\n").await.is_err() { break; }
                                        continue;
                                    }
                                    // `\u{1}`: deliver regardless of the JOINed set.
                                    if let Some(forced) = raw.strip_prefix('\u{1}') {
                                        if write.write_all(format!("{forced}\r\n").as_bytes()).await.is_err() { break; }
                                        continue;
                                    }
                                    let channel = raw
                                        .split(" PRIVMSG ")
                                        .nth(1)
                                        .and_then(|rest| rest.split(' ').next())
                                        .unwrap_or_default();
                                    if joined_channels.iter().any(|c| c == channel)
                                        && write.write_all(format!("{raw}\r\n").as_bytes()).await.is_err()
                                    {
                                        break;
                                    }
                                }
                            }
                            _ = kill_rx.changed() => break,
                        }
                    }
                    state.lock().unwrap().open -= 1;
                });
            }
        });
        this
    }

    fn settings(&self) -> IrcSettings {
        IrcSettings {
            host: "127.0.0.1".into(),
            port: self.port,
            use_tls: false,
        }
    }

    fn lines(&self, conn: usize) -> Vec<String> {
        self.state
            .lock()
            .unwrap()
            .conns
            .get(conn)
            .cloned()
            .unwrap_or_default()
    }

    fn conn_count(&self) -> usize {
        self.state.lock().unwrap().conns.len()
    }

    fn open_count(&self) -> usize {
        self.state.lock().unwrap().open
    }

    /// The JOIN line connection `conn` sent, if any.
    fn join_line(&self, conn: usize) -> Option<String> {
        self.lines(conn)
            .into_iter()
            .find(|l| l.starts_with("JOIN "))
    }

    fn pass_line(&self, conn: usize) -> Option<String> {
        self.lines(conn)
            .into_iter()
            .find(|l| l.starts_with("PASS "))
    }

    /// Finds the connection whose NICK is `nick`.
    fn conn_for_nick(&self, nick: &str) -> Option<usize> {
        let st = self.state.lock().unwrap();
        (0..st.conns.len())
            .rev()
            .find(|i| st.conns[*i].iter().any(|l| l == &format!("NICK {nick}")))
    }

    fn send_privmsg(&self, channel: &str, sender: &str, text: &str) {
        let _ = self.push.send(format!(
            ":{sender}!{sender}@{sender}.tmi.twitch.tv PRIVMSG #{channel} :{text}"
        ));
    }

    /// Delivers a PRIVMSG for `channel` to every joined connection even if it
    /// never JOINed that channel (a stray/late line).
    fn send_privmsg_unfiltered(&self, channel: &str, sender: &str, text: &str) {
        let _ = self.push.send(format!(
            "\u{1}:{sender}!{sender}@{sender}.tmi.twitch.tv PRIVMSG #{channel} :{text}"
        ));
    }

    /// Sends an invalid-UTF-8 line to every joined connection.
    fn send_garbage(&self) {
        let _ = self.push.send("\u{2}".to_string());
    }

    fn set_stall(&self, stall: bool) {
        self.stall.send_replace(stall);
    }

    fn drop_all_connections(&self) {
        self.kill.send_modify(|v| *v += 1);
    }

    fn set_reject_auth(&self, reject: bool) {
        self.state.lock().unwrap().reject_auth = reject;
    }
}

// --------------------------------------------------------------------------
// Spine + log capture + harness
// --------------------------------------------------------------------------

#[derive(Default)]
struct RecordingAppender {
    calls: Mutex<Vec<(String, StageEnvelope)>>,
    /// When set, every `append` fails (a Valkey outage).
    fail: std::sync::atomic::AtomicBool,
}

impl EventAppender for RecordingAppender {
    async fn append(&self, stream: &str, env: &StageEnvelope) -> Result<String, SpineError> {
        if self.fail.load(std::sync::atomic::Ordering::SeqCst) {
            return Err(SpineError::Config("simulated valkey outage".to_string()));
        }
        self.calls
            .lock()
            .unwrap()
            .push((stream.to_string(), env.clone()));
        Ok("1-0".to_string())
    }
}

#[derive(Default)]
struct NoopMetrics;
impl SpineMetrics for NoopMetrics {}

struct StaticBearer;
impl BearerSource for StaticBearer {
    async fn bearer(&self) -> Result<Arc<str>, ClientError> {
        Ok(Arc::from(BEARER))
    }
}

/// Captures every rendered `tracing` event (TRACE and up) in memory.
#[derive(Clone, Default)]
struct LogCapture(Arc<Mutex<Vec<u8>>>);

impl std::io::Write for LogCapture {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for LogCapture {
    type Writer = LogCapture;
    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}

impl LogCapture {
    fn text(&self) -> String {
        String::from_utf8_lossy(&self.0.lock().unwrap()).to_string()
    }
}

fn install_log_capture() -> (LogCapture, tracing::subscriber::DefaultGuard) {
    let capture = LogCapture::default();
    let subscriber = tracing_subscriber::fmt()
        .with_max_level(tracing::Level::TRACE)
        .with_ansi(false)
        .with_writer(capture.clone())
        .finish();
    let guard = tracing::subscriber::set_default(subscriber);
    (capture, guard)
}

/// Every secret value the tests feed through the pipeline (resolved OAuth
/// tokens, the IRC `PASS` prefix, machine JWTs). None may ever reach a log line.
const TOKEN_SECRETS: &[&str] = &[
    "tok-waddlebots-secret",
    "tok-otherbot-secret",
    "tok-impostor-secret",
    "tok-rotated-secret",
    "tok-late-secret",
    "oauth:",
    MINTED_JWT,
    BEARER,
    "old-jwt-secret",
    "new-jwt-secret",
];

/// F6: asserts the captured TRACE-level logs saw the run (non-empty) and carry
/// no token / machine-JWT value -- applied on every failure path, not just the
/// happy path.
fn assert_logs_clean(logs: &LogCapture, path: &str) {
    let captured = logs.text();
    assert!(
        !captured.is_empty(),
        "{path}: log capture must have seen the run (a silent capture proves nothing)"
    );
    for secret in TOKEN_SECRETS {
        assert!(
            !captured.contains(secret),
            "{path}: log output leaked {secret:?}"
        );
    }
}

type TestSupervisor =
    Supervisor<HubConnectionsClient<StaticBearer>, RecordingAppender, NoopMetrics>;

struct Harness {
    hub: MockHub,
    irc: FakeIrc,
    appender: Arc<PublishCtx<RecordingAppender, NoopMetrics>>,
    metrics: Arc<ConnectionsMetrics>,
    supervisor: TestSupervisor,
}

fn runner_scope() -> Scope {
    Scope::new("global", None)
}

async fn harness_with(mode: ScopeMode, page_size: u32) -> Harness {
    svc_ingest::crypto::ensure_installed();
    let hub = MockHub::start().await;
    let irc = FakeIrc::start().await;
    let metrics = Arc::new(ConnectionsMetrics::register(&prometheus::Registry::new()).unwrap());
    let client = HubConnectionsClient::new(
        &HubClientConfig {
            base_url: format!("http://{}", hub.addr),
            ca_file: None,
            request_timeout: Duration::from_secs(5),
            connect_timeout: Duration::from_secs(2),
        },
        StaticBearer,
        metrics.clone(),
    )
    .unwrap();
    let publish = Arc::new(PublishCtx {
        appender: RecordingAppender::default(),
        metrics: NoopMetrics,
        keyring: KeyRing::new(vec![("k1".to_string(), vec![9u8; 32])]),
        active_kid: "k1".to_string(),
    });
    let supervisor = Supervisor::new(
        Arc::new(client),
        publish.clone(),
        metrics.clone(),
        mode,
        SupervisorSettings {
            poll_interval: Duration::from_millis(150),
            page_size,
            irc: irc.settings(),
            reconnect_backoff_cap: Duration::from_millis(200),
        },
    );
    Harness {
        hub,
        irc,
        appender: publish,
        metrics,
        supervisor,
    }
}

async fn harness() -> Harness {
    harness_with(ScopeMode::Runner(runner_scope()), 500).await
}

async fn eventually(what: &str, mut cond: impl FnMut() -> bool) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    while tokio::time::Instant::now() < deadline {
        if cond() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("timed out waiting for: {what}");
}

fn published(h: &Harness) -> Vec<(String, StageEnvelope)> {
    h.appender.appender.calls.lock().unwrap().clone()
}

// --------------------------------------------------------------------------
// Tests
// --------------------------------------------------------------------------

#[tokio::test]
async fn one_socket_per_bot_joins_all_channels_and_publishes_under_scope() {
    let (logs, _guard) = install_log_capture();
    let mut h = harness().await;
    // Two channels for one bot account, one for another -- 3 bindings, 2 sockets.
    h.hub.add(1, "waddlebots", "chan_a");
    h.hub.add(2, "waddlebots", "chan_b");
    h.hub.add(3, "otherbot", "chan_c");

    let report = h.supervisor.reconcile_once().await.unwrap();
    assert_eq!((report.listed, report.spawned), (3, 2));

    eventually("both bots joined", || {
        h.irc
            .conn_for_nick("waddlebots")
            .and_then(|c| h.irc.join_line(c))
            .is_some()
            && h.irc
                .conn_for_nick("otherbot")
                .and_then(|c| h.irc.join_line(c))
                .is_some()
    })
    .await;

    assert_eq!(
        h.irc.conn_count(),
        2,
        "ONE socket per bot account, never one per channel"
    );
    let wb = h.irc.conn_for_nick("waddlebots").unwrap();
    assert_eq!(
        h.irc.join_line(wb).as_deref(),
        Some("JOIN #chan_a,#chan_b"),
        "all of one bot's channels in a single JOIN"
    );
    assert_eq!(
        h.irc.pass_line(wb).as_deref(),
        Some("PASS oauth:tok-waddlebots-secret")
    );
    let ob = h.irc.conn_for_nick("otherbot").unwrap();
    assert_eq!(h.irc.join_line(ob).as_deref(), Some("JOIN #chan_c"));

    // list used the contract's query; every call carried the machine bearer.
    {
        let st = h.hub.state.lock().unwrap();
        assert_eq!(st.list_queries[0]["provider"], "twitch");
        assert_eq!(st.list_queries[0]["capability"], "chat_ingest");
        assert_eq!(st.list_queries[0]["after"], "0");
        assert_eq!(
            st.bad_auth, 0,
            "every request must carry the machine JWT bearer"
        );
        assert_eq!(
            st.resolve_calls.len(),
            3,
            "one resolve per binding on first connect"
        );
    }

    // Inbound chat routes to the spine; the bot's own echo does not.
    h.irc.send_privmsg("chan_a", "viewer1", "hello a");
    h.irc.send_privmsg("chan_b", "viewer2", "hello b");
    h.irc.send_privmsg("chan_c", "viewer3", "hello c");
    h.irc.send_privmsg("chan_a", "waddlebots", "my own echo");
    eventually("3 events published", || published(&h).len() >= 3).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    let calls = published(&h);
    assert_eq!(calls.len(), 3, "self-echo must not publish");
    let streams: Vec<&str> = calls.iter().map(|(s, _)| s.as_str()).collect();
    for expected in [
        "waddles:t:global:c:_tenant:src:twitch:tw-chan_a:events",
        "waddles:t:global:c:_tenant:src:twitch:tw-chan_b:events",
        "waddles:t:global:c:_tenant:src:twitch:tw-chan_c:events",
    ] {
        assert!(
            streams.contains(&expected),
            "missing stream {expected}: {streams:?}"
        );
    }

    assert_eq!(h.metrics.bots_running.get(), 2);
    assert_eq!(h.metrics.channels_joined.get(), 3);
    assert_eq!(
        h.metrics
            .events_routed_total
            .with_label_values(&["published"])
            .get(),
        3
    );

    // Token hygiene, asserted on every captured log line at TRACE.
    let captured = logs.text();
    assert!(!captured.is_empty(), "log capture must have seen the run");
    for secret in ["tok-waddlebots-secret", "tok-otherbot-secret", "oauth:"] {
        assert!(!captured.contains(secret), "log output leaked {secret:?}");
    }
    h.supervisor.stop_all().await;
}

#[tokio::test]
async fn a_409_drops_that_channel_for_good_without_retry_and_serves_the_rest() {
    let (logs, _guard) = install_log_capture();
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.hub.add(2, "waddlebots", "chan_dead");
    h.hub.set_behavior(
        2,
        Behavior::Fail {
            status: 409,
            body: json!({"success": false, "error": {
                "code": "CONNECTION_NOT_USABLE", "message": "no", "details": {"reason": "revoked_grant"}}}),
        },
    );

    h.supervisor.reconcile_once().await.unwrap();
    eventually("bot joined the surviving channel only", || {
        h.irc
            .conn_for_nick("waddlebots")
            .and_then(|c| h.irc.join_line(c))
            .as_deref()
            == Some("JOIN #chan_a")
    })
    .await;
    assert_eq!(h.hub.resolves_for(2), 1);
    assert_eq!(
        h.metrics
            .resolve_not_usable_total
            .with_label_values(&["revoked_grant"])
            .get(),
        1
    );

    // The refusal is sticky while the same binding version stays listed: more
    // reconcile passes neither re-resolve it nor reconnect the surviving bot.
    for _ in 0..3 {
        h.supervisor.reconcile_once().await.unwrap();
    }
    tokio::time::sleep(Duration::from_millis(150)).await;
    assert_eq!(h.hub.resolves_for(2), 1, "a 409 must never be retried");
    assert_eq!(
        h.irc.conn_count(),
        1,
        "surviving channel's socket must not churn"
    );

    // Editing the binding (new updatedAt) makes it eligible again.
    {
        let mut st = h.hub.state.lock().unwrap();
        for b in &mut st.bindings {
            if b["grantId"] == 2 {
                b["updatedAt"] = json!("v2");
            }
        }
    }
    h.hub.set_behavior(
        2,
        Behavior::Ok {
            token: "tok-waddlebots-secret".into(),
            bot: "waddlebots".into(),
        },
    );
    h.supervisor.reconcile_once().await.unwrap();
    eventually("edited binding rejoined", || {
        h.irc
            .conn_for_nick("waddlebots")
            .and_then(|c| h.irc.join_line(c))
            .as_deref()
            == Some("JOIN #chan_a,#chan_dead")
    })
    .await;
    h.supervisor.stop_all().await;
    assert_logs_clean(&logs, "409 not-usable");
}

#[tokio::test]
async fn a_bot_whose_every_channel_is_refused_exits_and_is_not_respawned() {
    let mut h = harness().await;
    h.hub.add(1, "doomed", "chan_a");
    h.hub.set_behavior(
        1,
        Behavior::Fail { status: 409, body: json!({"error": {"code": "CONNECTION_NOT_USABLE", "details": {"reason": "token_expired"}}}) },
    );
    h.supervisor.reconcile_once().await.unwrap();
    eventually("bot task ended", || h.hub.resolves_for(1) >= 1).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    // Next passes reap the finished bot and do not respawn it (still rejected).
    let report = h.supervisor.reconcile_once().await.unwrap();
    assert_eq!(report.spawned, 0);
    assert_eq!(h.supervisor.running_bots(), 0);
    assert_eq!(
        h.irc.conn_count(),
        0,
        "a refused-only bot never opens an IRC socket"
    );
    assert_eq!(h.hub.resolves_for(1), 1);
}

#[tokio::test]
async fn a_503_on_resolve_is_retried_with_backoff_not_dropped() {
    let (logs, _guard) = install_log_capture();
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.hub.set_behavior(
        1,
        Behavior::FailThenOk {
            fails_left: 2,
            status: 503,
            token: "tok-waddlebots-secret".into(),
            bot: "waddlebots".into(),
        },
    );
    h.supervisor.reconcile_once().await.unwrap();
    eventually("bot eventually connects after transient failures", || {
        h.irc
            .conn_for_nick("waddlebots")
            .and_then(|c| h.irc.join_line(c))
            .is_some()
    })
    .await;
    assert!(
        h.hub.resolves_for(1) >= 3,
        "2 transient failures then success"
    );
    assert_eq!(
        h.metrics
            .resolve_not_usable_total
            .with_label_values(&["other"])
            .get(),
        0,
        "a 503 is retryable, never counted as a drop"
    );
    h.supervisor.stop_all().await;
    assert_logs_clean(&logs, "503 transient");
}

#[tokio::test]
async fn membership_changes_reconnect_one_socket_and_vanished_bindings_stop_the_bot() {
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.supervisor.reconcile_once().await.unwrap();
    eventually("first connect", || h.irc.join_line(0).is_some()).await;
    assert_eq!(h.irc.join_line(0).as_deref(), Some("JOIN #chan_a"));

    // A second channel for the same bot: ONE new socket joining both, the old
    // one closed -- never two sockets for one bot.
    h.hub.add(2, "waddlebots", "chan_b");
    let report = h.supervisor.reconcile_once().await.unwrap();
    assert_eq!((report.spawned, report.updated), (0, 1));
    eventually("reconnect with both channels", || {
        h.irc.conn_count() >= 2 && h.irc.join_line(1).is_some()
    })
    .await;
    assert_eq!(h.irc.join_line(1).as_deref(), Some("JOIN #chan_a,#chan_b"));
    eventually("old socket closed", || h.irc.open_count() == 1).await;
    assert_eq!(h.metrics.channels_joined.get(), 2);

    // Both bindings vanish: the bot is stopped and its socket closed.
    h.hub.remove(1);
    h.hub.remove(2);
    let report = h.supervisor.reconcile_once().await.unwrap();
    assert_eq!(report.stopped, 1);
    eventually("socket closed after stop", || h.irc.open_count() == 0).await;
    assert_eq!(h.supervisor.running_bots(), 0);
    assert_eq!(h.metrics.bots_running.get(), 0);
    assert_eq!(h.metrics.channels_joined.get(), 0);
}

#[tokio::test]
async fn a_failed_list_leaves_running_bots_untouched() {
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.supervisor.reconcile_once().await.unwrap();
    eventually("connected", || h.irc.join_line(0).is_some()).await;

    h.hub.state.lock().unwrap().list_status = Some(503);
    let err = h.supervisor.reconcile_once().await.unwrap_err();
    assert!(matches!(
        err,
        ReconcileError::List(ClientError::Transient(_))
    ));
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(
        h.supervisor.running_bots(),
        1,
        "an outage must not tear bots down"
    );
    assert_eq!(h.irc.open_count(), 1);
    assert_eq!(h.irc.conn_count(), 1, "and must not churn the socket");
    assert_eq!(
        h.metrics
            .reconcile_total
            .with_label_values(&["list_failed"])
            .get(),
        1
    );

    h.hub.state.lock().unwrap().list_status = None;
    h.supervisor.reconcile_once().await.unwrap();
    assert_eq!(h.irc.conn_count(), 1);
    h.supervisor.stop_all().await;
}

#[tokio::test]
async fn list_paginates_by_cursor() {
    let mut h = harness_with(ScopeMode::Runner(runner_scope()), 2).await;
    for id in 1..=5 {
        h.hub.add(id, "waddlebots", &format!("chan_{id}"));
    }
    let report = h.supervisor.reconcile_once().await.unwrap();
    assert_eq!(report.listed, 5);
    let afters: Vec<String> = h
        .hub
        .state
        .lock()
        .unwrap()
        .list_queries
        .iter()
        .map(|q| q["after"].clone())
        .collect();
    assert_eq!(afters, ["0", "2", "4"], "cursor must advance page by page");
    eventually("one socket joined all five", || {
        h.irc.join_line(0).as_deref() == Some("JOIN #chan_1,#chan_2,#chan_3,#chan_4,#chan_5")
    })
    .await;
    h.supervisor.stop_all().await;
}

#[tokio::test]
async fn a_dropped_irc_connection_re_resolves_a_fresh_token_on_reconnect() {
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.supervisor.reconcile_once().await.unwrap();
    eventually("connected", || h.irc.join_line(0).is_some()).await;
    assert_eq!(
        h.irc.pass_line(0).as_deref(),
        Some("PASS oauth:tok-waddlebots-secret")
    );
    assert_eq!(h.hub.resolves_for(1), 1);

    // hub-api rotates the token (refresh); the socket then drops.
    h.hub.set_behavior(
        1,
        Behavior::Ok {
            token: "tok-rotated-secret".into(),
            bot: "waddlebots".into(),
        },
    );
    h.irc.drop_all_connections();
    eventually("reconnected with the rotated token", || {
        h.irc.conn_count() >= 2 && h.irc.pass_line(1).is_some()
    })
    .await;
    assert_eq!(
        h.irc.pass_line(1).as_deref(),
        Some("PASS oauth:tok-rotated-secret")
    );
    assert_eq!(
        h.hub.resolves_for(1),
        2,
        "token re-resolved on reconnect, never cached"
    );
    h.supervisor.stop_all().await;
}

#[tokio::test]
async fn twitch_auth_rejection_re_resolves_and_keeps_retrying_instead_of_stopping() {
    let (logs, _guard) = install_log_capture();
    let mut h = harness().await;
    h.irc.set_reject_auth(true);
    h.hub.add(1, "waddlebots", "chan_a");
    h.supervisor.reconcile_once().await.unwrap();
    eventually(
        "at least two resolve attempts while auth is rejected",
        || h.hub.resolves_for(1) >= 2,
    )
    .await;
    assert_eq!(
        h.supervisor.running_bots(),
        1,
        "auth rejection must not stop the bot for good"
    );

    // Once Twitch accepts the (refreshed) token the bot recovers on its own.
    h.irc.set_reject_auth(false);
    eventually("recovered", || {
        h.irc
            .conn_for_nick("waddlebots")
            .is_some_and(|c| h.irc.join_line(c).is_some())
    })
    .await;
    h.supervisor.stop_all().await;
    assert_logs_clean(&logs, "twitch auth rejection");
}

#[tokio::test]
async fn a_token_for_a_different_bot_login_is_refused_not_used() {
    let (logs, _guard) = install_log_capture();
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    // hub-api claims this connection authenticates as someone else.
    h.hub.set_behavior(
        1,
        Behavior::Ok {
            token: "tok-impostor-secret".into(),
            bot: "someoneelse".into(),
        },
    );
    h.supervisor.reconcile_once().await.unwrap();
    eventually("refused", || {
        h.metrics
            .resolve_not_usable_total
            .with_label_values(&["identity_mismatch"])
            .get()
            == 1
    })
    .await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(
        h.irc.conn_count(),
        0,
        "a mismatched identity must never reach Twitch"
    );
    assert_logs_clean(&logs, "login mismatch");
}

// regression: F5 -- a resolve answer with NO bot_login must fail closed, never be
// accepted as "no claim to contradict".
#[tokio::test]
async fn a_resolve_without_a_bot_login_is_refused_fail_closed() {
    let (logs, _guard) = install_log_capture();
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.hub.set_behavior(
        1,
        Behavior::Ok {
            token: "tok-impostor-secret".into(),
            bot: String::new(), // metadata carries no bot_login at all
        },
    );
    h.supervisor.reconcile_once().await.unwrap();
    eventually("refused as identity_mismatch", || {
        h.metrics
            .resolve_not_usable_total
            .with_label_values(&["identity_mismatch"])
            .get()
            == 1
    })
    .await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(
        h.irc.conn_count(),
        0,
        "an unverifiable identity must never reach Twitch"
    );
    assert_logs_clean(&logs, "missing bot_login");
}

#[tokio::test]
async fn binding_scope_mode_fails_closed_without_a_slug_and_scopes_events_with_one() {
    let (logs, _guard) = install_log_capture();
    let mut h = harness_with(ScopeMode::Binding, 500).await;
    // No tenantSlug: skipped and counted, never published under a guessed scope.
    h.hub.add(1, "waddlebots", "chan_noslug");
    // Fully scoped.
    h.hub.add_with(
        2,
        "waddlebots",
        "chan_scoped",
        json!({"tenantSlug": "acme", "communityName": "main"}),
    );
    h.supervisor.reconcile_once().await.unwrap();
    assert_eq!(
        h.metrics
            .bindings_skipped_total
            .with_label_values(&["no_scope"])
            .get(),
        1
    );
    eventually("joined only the scoped channel", || {
        h.irc.join_line(0).as_deref() == Some("JOIN #chan_scoped")
    })
    .await;
    assert_eq!(
        h.hub.resolves_for(1),
        0,
        "an unscoped binding is never even resolved"
    );

    h.irc.send_privmsg("chan_scoped", "viewer", "scoped hello");
    eventually("published", || !published(&h).is_empty()).await;
    let calls = published(&h);
    assert_eq!(
        calls[0].0,
        "waddles:t:acme:c:main:src:twitch:tw-chan_scoped:events"
    );
    assert_eq!(calls[0].1.tenant, "acme");
    assert_eq!(calls[0].1.community.as_deref(), Some("main"));
    assert!(!logs.text().contains("tok-waddlebots-secret"));
    h.supervisor.stop_all().await;
}

#[tokio::test]
async fn run_loop_picks_up_new_bindings_and_stops_every_bot_on_shutdown() {
    let h = harness().await;
    let Harness {
        hub,
        irc,
        metrics,
        supervisor,
        ..
    } = h;
    let (tx, rx) = oneshot::channel();
    let task = tokio::spawn(supervisor.run(rx));

    // Nothing bound yet: the supervisor idles. A binding added later is picked
    // up by a subsequent poll with no restart.
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(irc.conn_count(), 0);
    hub.add(1, "waddlebots", "chan_a");
    eventually("polled binding connected", || irc.join_line(0).is_some()).await;
    assert!(metrics.reconcile_total.with_label_values(&["ok"]).get() >= 2);
    assert!(hub.total_resolves() >= 1);

    tx.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(10), task)
        .await
        .expect("run() must return after shutdown")
        .unwrap();
    eventually("socket closed on shutdown", || irc.open_count() == 0).await;
    assert_eq!(metrics.bots_running.get(), 0);
}

#[tokio::test]
async fn a_publish_failure_is_counted_and_the_bot_keeps_serving() {
    let (logs, _guard) = install_log_capture();
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.supervisor.reconcile_once().await.unwrap();
    eventually("connected", || h.irc.join_line(0).is_some()).await;

    // Valkey outage: the XADD fails, the failure is counted, the socket stays up.
    h.appender
        .appender
        .fail
        .store(true, std::sync::atomic::Ordering::SeqCst);
    h.irc.send_privmsg("chan_a", "viewer", "lost");
    eventually("publish failure counted", || {
        h.metrics
            .events_routed_total
            .with_label_values(&["publish_failed"])
            .get()
            == 1
    })
    .await;
    assert_eq!(
        h.irc.conn_count(),
        1,
        "a spine outage must not drop the IRC socket"
    );

    // Recovery: the very next message publishes normally.
    h.appender
        .appender
        .fail
        .store(false, std::sync::atomic::Ordering::SeqCst);
    h.irc.send_privmsg("chan_a", "viewer", "delivered");
    eventually("published after recovery", || !published(&h).is_empty()).await;
    h.supervisor.stop_all().await;
    assert_logs_clean(&logs, "publish failure");
}

// regression: F1 -- only a TERMINAL refusal is a sticky drop. A bare 400 (or any
// non-terminal 4xx) is retried, never counted as a drop, and its channel is
// skipped (not joined under another member's token) until it resolves.
#[tokio::test]
async fn a_non_terminal_4xx_on_resolve_skips_the_channel_and_retries_instead_of_dropping() {
    let (logs, _guard) = install_log_capture();
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.hub.add(2, "waddlebots", "chan_bad");
    h.hub.set_behavior(
        2,
        Behavior::Fail {
            status: 400,
            body: json!({"error": {"code": "BAD_REQUEST"}}),
        },
    );
    h.supervisor.reconcile_once().await.unwrap();
    eventually("joined only the resolvable channel", || {
        h.irc.join_line(0).as_deref() == Some("JOIN #chan_a")
    })
    .await;
    eventually("the failing member is re-probed", || {
        h.hub.resolves_for(2) >= 2
    })
    .await;
    assert_eq!(
        h.metrics
            .resolve_not_usable_total
            .with_label_values(&["other"])
            .get(),
        0,
        "a non-terminal 4xx is never counted as a drop"
    );
    h.supervisor.stop_all().await;
    assert_logs_clean(&logs, "non-terminal 4xx");
}

// regression: F1 -- hub-api answers 409 REFRESH_NOT_CONFIGURED while an operator has
// not yet set the refresh secret. Fixing the secret must recover with NO restart
// and NO binding edit.
#[tokio::test]
async fn a_409_refresh_not_configured_recovers_once_the_operator_fixes_it() {
    let (logs, _guard) = install_log_capture();
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.hub.set_behavior(
        1,
        Behavior::Fail {
            status: 409,
            body: json!({"success": false, "error": {
                "code": "REFRESH_NOT_CONFIGURED", "message": "set the secret"}}),
        },
    );
    h.supervisor.reconcile_once().await.unwrap();
    eventually("the bot keeps retrying the resolve", || {
        h.hub.resolves_for(1) >= 3
    })
    .await;
    assert_eq!(h.irc.conn_count(), 0, "nothing to join yet");
    assert_eq!(
        h.supervisor.running_bots(),
        1,
        "an operator-fixable 409 must not end the bot"
    );
    assert_eq!(
        h.metrics
            .resolve_not_usable_total
            .with_label_values(&["other"])
            .get(),
        0
    );

    // Operator sets the secret: the very same binding (same updatedAt) recovers.
    h.hub.set_behavior(
        1,
        Behavior::Ok {
            token: "tok-waddlebots-secret".into(),
            bot: "waddlebots".into(),
        },
    );
    eventually("recovered without restart", || {
        h.irc.join_line(0).as_deref() == Some("JOIN #chan_a")
    })
    .await;
    h.supervisor.stop_all().await;
    assert_logs_clean(&logs, "409 REFRESH_NOT_CONFIGURED");
}

// regression: F1 -- a route-level 404 (no JSON error envelope) is a hub-api replica
// that lacks the routes mid-rollout. It must recover when the rollout finishes.
#[tokio::test]
async fn a_mid_rollout_route_404_recovers_when_the_rollout_finishes() {
    let (logs, _guard) = install_log_capture();
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.hub.set_behavior(
        1,
        Behavior::Fail {
            status: 404,
            body: json!("<html>404 Not Found</html>"),
        },
    );
    h.supervisor.reconcile_once().await.unwrap();
    eventually("retrying through the 404s", || h.hub.resolves_for(1) >= 3).await;
    assert_eq!(h.supervisor.running_bots(), 1, "a bare 404 is not terminal");
    h.hub.set_behavior(
        1,
        Behavior::Ok {
            token: "tok-waddlebots-secret".into(),
            bot: "waddlebots".into(),
        },
    );
    eventually("recovered after the rollout", || {
        h.irc.join_line(0).as_deref() == Some("JOIN #chan_a")
    })
    .await;
    h.supervisor.stop_all().await;
    assert_logs_clean(&logs, "mid-rollout 404");
}

// A GENUINE terminal refusal stays dropped: an enveloped 404 NOT_FOUND and a 409
// REFRESH_REJECTED are never retried.
#[tokio::test]
async fn enveloped_not_found_and_refresh_rejected_stay_dropped_for_good() {
    let mut h = harness().await;
    h.hub.add(1, "gone", "chan_gone");
    h.hub.add(2, "rejected", "chan_rejected");
    h.hub.set_behavior(
        1,
        Behavior::Fail {
            status: 404,
            body: json!({"success": false, "error": {"code": "NOT_FOUND"}}),
        },
    );
    h.hub.set_behavior(
        2,
        Behavior::Fail {
            status: 409,
            body: json!({"success": false, "error": {
                "code": "REFRESH_REJECTED", "details": {"reason": "revoked_grant"}}}),
        },
    );
    h.supervisor.reconcile_once().await.unwrap();
    eventually("both refused", || {
        h.hub.resolves_for(1) >= 1 && h.hub.resolves_for(2) >= 1
    })
    .await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    for _ in 0..3 {
        h.supervisor.reconcile_once().await.unwrap();
    }
    tokio::time::sleep(Duration::from_millis(250)).await;
    assert_eq!(h.hub.resolves_for(1), 1, "NOT_FOUND must never be retried");
    assert_eq!(
        h.hub.resolves_for(2),
        1,
        "REFRESH_REJECTED must never be retried"
    );
    assert_eq!(h.irc.conn_count(), 0);
}

// regression: F5 -- a member whose resolve fails transiently is NOT joined under
// another member's token; a background probe joins it as soon as it resolves,
// without churning the healthy member's socket while it keeps failing.
#[tokio::test]
async fn a_transiently_failing_member_is_not_joined_under_another_members_token() {
    let (logs, _guard) = install_log_capture();
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.hub.add(2, "waddlebots", "chan_b");
    h.hub.set_behavior(
        2,
        Behavior::Fail {
            status: 503,
            body: json!({"error": {"code": "CONNECTIONS_UNAVAILABLE"}}),
        },
    );
    h.supervisor.reconcile_once().await.unwrap();
    eventually("healthy member joined alone", || {
        h.irc.join_line(0).as_deref() == Some("JOIN #chan_a")
    })
    .await;
    eventually("the failing member is probed in the background", || {
        h.hub.resolves_for(2) >= 3
    })
    .await;
    assert_eq!(
        h.irc.conn_count(),
        1,
        "probing a failing member must not churn the live socket"
    );
    assert_eq!(h.irc.join_line(0).as_deref(), Some("JOIN #chan_a"));

    // Member 2 recovers (with its own token): ONE reconnect joins both channels.
    h.hub.set_behavior(
        2,
        Behavior::Ok {
            token: "tok-late-secret".into(),
            bot: "waddlebots".into(),
        },
    );
    eventually("reconnected with both channels", || {
        (0..h.irc.conn_count())
            .any(|c| h.irc.join_line(c).as_deref() == Some("JOIN #chan_a,#chan_b"))
    })
    .await;
    assert!(
        h.metrics
            .bot_reconnects_total
            .with_label_values(&["member_recovered"])
            .get()
            >= 1
    );
    h.supervisor.stop_all().await;
    assert_logs_clean(&logs, "member recovery");
}

#[tokio::test]
async fn a_stray_message_for_an_unjoined_channel_is_counted_unrouted_not_published() {
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.supervisor.reconcile_once().await.unwrap();
    eventually("connected", || h.irc.join_line(0).is_some()).await;
    h.irc
        .send_privmsg_unfiltered("chan_zzz", "viewer", "wrong room");
    eventually("unrouted counted", || {
        h.metrics
            .events_routed_total
            .with_label_values(&["unrouted"])
            .get()
            == 1
    })
    .await;
    assert!(published(&h).is_empty());
    h.supervisor.stop_all().await;
}

#[tokio::test]
async fn a_corrupted_line_reconnects_and_a_proven_session_resets_backoff() {
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.supervisor.reconcile_once().await.unwrap();
    eventually("connected", || h.irc.join_line(0).is_some()).await;

    // A delivered line proves the session; the corrupted line then breaks it.
    h.irc.send_privmsg("chan_a", "viewer", "proof of life");
    eventually("proved", || !published(&h).is_empty()).await;
    h.irc.send_garbage();
    eventually("reconnected after the recv error", || {
        h.irc.conn_count() >= 2 && h.irc.join_line(1).is_some()
    })
    .await;
    assert!(
        h.metrics
            .bot_reconnects_total
            .with_label_values(&["error"])
            .get()
            >= 1
    );
    h.supervisor.stop_all().await;
}

#[tokio::test]
async fn an_unreachable_irc_server_backs_off_and_keeps_retrying() {
    let mut h = harness().await;
    // Nothing listens on port 1: every connect is refused.
    h.supervisor = {
        let metrics = h.metrics.clone();
        let client = HubConnectionsClient::new(
            &HubClientConfig {
                base_url: format!("http://{}", h.hub.addr),
                ca_file: None,
                request_timeout: Duration::from_secs(5),
                connect_timeout: Duration::from_secs(2),
            },
            StaticBearer,
            metrics.clone(),
        )
        .unwrap();
        Supervisor::new(
            Arc::new(client),
            h.appender.clone(),
            metrics,
            ScopeMode::Runner(runner_scope()),
            SupervisorSettings {
                poll_interval: Duration::from_millis(150),
                page_size: 500,
                irc: IrcSettings {
                    host: "127.0.0.1".into(),
                    port: 1,
                    use_tls: false,
                },
                reconnect_backoff_cap: Duration::from_millis(100),
            },
        )
    };
    h.hub.add(1, "waddlebots", "chan_a");
    h.supervisor.reconcile_once().await.unwrap();
    eventually("connect failures retried", || {
        h.metrics
            .bot_reconnects_total
            .with_label_values(&["error"])
            .get()
            >= 2
    })
    .await;
    assert_eq!(
        h.supervisor.running_bots(),
        1,
        "a connect failure never stops the bot"
    );
    assert_eq!(h.metrics.channels_joined.get(), 0);
    h.supervisor.stop_all().await;
}

#[tokio::test]
async fn a_membership_change_during_a_stalled_connect_restarts_it_with_the_new_set() {
    let mut h = harness().await;
    h.irc.set_stall(true);
    h.hub.add(1, "waddlebots", "chan_a");
    h.supervisor.reconcile_once().await.unwrap();
    eventually("first connection is mid-registration", || {
        h.irc.conn_for_nick("waddlebots").is_some()
    })
    .await;

    h.hub.add(2, "waddlebots", "chan_b");
    h.irc.set_stall(false);
    let report = h.supervisor.reconcile_once().await.unwrap();
    assert_eq!(report.updated, 1);
    eventually("joined both channels on a fresh connection", || {
        (0..h.irc.conn_count())
            .any(|c| h.irc.join_line(c).as_deref() == Some("JOIN #chan_a,#chan_b"))
    })
    .await;
    h.supervisor.stop_all().await;
}

#[tokio::test]
async fn shutdown_while_backing_off_returns_promptly() {
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    h.hub.set_behavior(
        1,
        Behavior::Fail {
            status: 503,
            body: json!({"error": {"code": "CONNECTIONS_UNAVAILABLE"}}),
        },
    );
    h.supervisor.reconcile_once().await.unwrap();
    eventually("retrying resolve", || h.hub.resolves_for(1) >= 2).await;
    let started = std::time::Instant::now();
    h.supervisor.stop_all().await;
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "a bot in backoff must stop immediately, not wait out the delay"
    );
    assert_eq!(h.metrics.bots_running.get(), 0);
    assert_eq!(h.irc.conn_count(), 0);
}

// -- client-level behavior over real HTTP ------------------------------------

fn bare_client<B: BearerSource>(hub: &MockHub, bearer: B) -> HubConnectionsClient<B> {
    svc_ingest::crypto::ensure_installed();
    HubConnectionsClient::new(
        &HubClientConfig {
            base_url: format!("http://{}", hub.addr),
            ca_file: None,
            request_timeout: Duration::from_secs(5),
            connect_timeout: Duration::from_secs(2),
        },
        bearer,
        Arc::new(ConnectionsMetrics::register(&prometheus::Registry::new()).unwrap()),
    )
    .unwrap()
}

#[tokio::test]
async fn client_classifies_every_hub_answer_over_real_http() {
    use svc_ingest::connections::client::ConnectionsApi;
    let hub = MockHub::start().await;
    hub.add(1, "waddlebots", "chan_a");
    let client = bare_client(&hub, StaticBearer);

    // list: 503 retryable; 200 with an undecodable body retryable; an oversize
    // body is refused rather than buffered.
    hub.state.lock().unwrap().list_override =
        Some((503, json!({"error": {"code": "CONNECTIONS_UNAVAILABLE"}})));
    let err = client
        .list_page("twitch", "chat_ingest", 0, 10)
        .await
        .unwrap_err();
    assert!(matches!(err, ClientError::Transient(_)), "{err:?}");
    hub.state.lock().unwrap().list_override = Some((200, json!("not a page")));
    let err = client
        .list_page("twitch", "chat_ingest", 0, 10)
        .await
        .unwrap_err();
    assert!(
        matches!(&err, ClientError::Transient(m) if m.contains("undecodable")),
        "{err:?}"
    );
    hub.state.lock().unwrap().list_override = Some((200, json!("x".repeat(9 * 1024 * 1024))));
    let err = client
        .list_page("twitch", "chat_ingest", 0, 10)
        .await
        .unwrap_err();
    assert!(
        matches!(&err, ClientError::Transient(m) if m.contains("exceeds")),
        "{err:?}"
    );
    hub.state.lock().unwrap().list_override = None;
    let page = client
        .list_page("twitch", "chat_ingest", 0, 10)
        .await
        .unwrap();
    assert_eq!(page.items.len(), 1);

    // resolve: unknown connection -> NotUsable(not_found); 400 -> Permanent;
    // garbage / empty token -> Transient; the happy path carries the token.
    let err = client.resolve(9, 9).await.unwrap_err();
    assert!(
        matches!(&err, ClientError::NotUsable { status: 404, reason, .. } if reason == "not_found"),
        "{err:?}"
    );
    hub.set_behavior(
        1,
        Behavior::Fail {
            status: 400,
            body: json!({"error": {"code": "BAD_REQUEST"}}),
        },
    );
    assert!(matches!(
        client.resolve(1, 1).await.unwrap_err(),
        ClientError::Permanent { status: 400, .. }
    ));
    hub.set_behavior(
        1,
        Behavior::Fail {
            status: 200,
            body: json!("garbage"),
        },
    );
    assert!(matches!(
        client.resolve(1, 1).await.unwrap_err(),
        ClientError::Transient(_)
    ));
    // A token of the wrong JSON type is a decode failure -- and serde's own error
    // text would quote it. The reported error must carry no trace of the value.
    hub.set_behavior(
        1,
        Behavior::Fail {
            status: 200,
            body: json!({
                "success": true, "connectionId": 1, "communityId": 1, "provider": "twitch",
                "accessToken": 424242424242_u64, "metadata": {}
            }),
        },
    );
    let err = client.resolve(1, 1).await.unwrap_err();
    assert!(
        matches!(&err, ClientError::Transient(m) if m.contains("undecodable resolve response")),
        "{err:?}"
    );
    assert!(
        !format!("{err:?}{err}").contains("424242424242"),
        "decode error leaked the token value: {err}"
    );
    hub.set_behavior(
        1,
        Behavior::Ok {
            token: String::new(),
            bot: "waddlebots".into(),
        },
    );
    let err = client.resolve(1, 1).await.unwrap_err();
    assert!(
        matches!(&err, ClientError::Transient(m) if m.contains("empty access token")),
        "{err:?}"
    );
    hub.set_behavior(
        1,
        Behavior::Ok {
            token: "tok-happy".into(),
            bot: "waddlebots".into(),
        },
    );
    let creds = client.resolve(1, 1).await.unwrap();
    assert_eq!(creds.access_token.expose(), "tok-happy");
    assert_eq!(creds.bot_login().as_deref(), Some("waddlebots"));

    // A wrong bearer is rejected by hub-api (401 -> retryable, re-mint later).
    struct WrongBearer;
    impl BearerSource for WrongBearer {
        async fn bearer(&self) -> Result<Arc<str>, ClientError> {
            Ok(Arc::from("not-the-jwt"))
        }
    }
    let wrong = bare_client(&hub, WrongBearer);
    let err = wrong.resolve(1, 1).await.unwrap_err();
    assert!(matches!(err, ClientError::Transient(_)), "{err:?}");
    assert!(hub.state.lock().unwrap().bad_auth >= 1);
}

#[tokio::test]
async fn client_reports_a_refused_connection_as_transient() {
    use svc_ingest::connections::client::ConnectionsApi;
    svc_ingest::crypto::ensure_installed();
    let metrics = Arc::new(ConnectionsMetrics::register(&prometheus::Registry::new()).unwrap());
    let cfg = HubClientConfig {
        base_url: "http://127.0.0.1:1".to_string(),
        ca_file: None,
        request_timeout: Duration::from_secs(2),
        connect_timeout: Duration::from_secs(1),
    };
    let client = HubConnectionsClient::new(&cfg, StaticBearer, metrics.clone()).unwrap();
    let err = client.resolve(1, 1).await.unwrap_err();
    assert!(
        matches!(&err, ClientError::Transient(m) if m.contains("request failed")),
        "{err:?}"
    );
    assert_eq!(
        metrics
            .requests_total
            .with_label_values(&["resolve", "transient"])
            .get(),
        1
    );
}

#[tokio::test]
async fn machine_jwt_bootstrap_mints_the_bearer_once_and_never_leaks_the_sa_token() {
    use svc_ingest::connections::client::{list_all, ConnectionsApi, SCOPE_CONNECTIONS_RESOLVE};
    let (logs, _guard) = install_log_capture();
    let hub = MockHub::start().await;
    hub.add(1, "waddlebots", "chan_a");
    hub.state.lock().unwrap().expected_bearer = Some(MINTED_JWT.to_string());

    let dir = std::env::temp_dir().join(format!("svc-ingest-sa-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sa_path = dir.join("token");
    std::fs::write(&sa_path, "projected-sa-token-secret\n").unwrap();
    let mint = |addr: SocketAddr, path: &str| {
        service_auth::MachineJwtClient::with_timeouts(
            format!("http://{addr}/internal/service-token"),
            path.to_string(),
            SCOPE_CONNECTIONS_RESOLVE,
            service_auth::HttpTimeouts::from_env(),
        )
        .unwrap()
    };
    let client = bare_client(&hub, mint(hub.addr, &sa_path.to_string_lossy()));

    let all = list_all(&client, "twitch", "chat_ingest", 100)
        .await
        .unwrap();
    assert_eq!(all.len(), 1);
    let creds = client.resolve(1, 1).await.unwrap();
    assert_eq!(creds.access_token.expose(), "tok-waddlebots-secret");

    {
        let st = hub.state.lock().unwrap();
        assert_eq!(
            st.bad_auth, 0,
            "both calls must carry the freshly minted machine JWT"
        );
        assert_eq!(
            st.token_requests.len(),
            1,
            "the machine JWT is cached across calls"
        );
        assert_eq!(st.token_requests[0].0, "projected-sa-token-secret");
        assert_eq!(st.token_requests[0].1["scope"], SCOPE_CONNECTIONS_RESOLVE);
    }

    // A rejected bootstrap surfaces as a retryable transient failure -- and the
    // SA token never reaches the error text or any log line.
    let hub2 = MockHub::start().await;
    hub2.state.lock().unwrap().token_status = 403;
    let client2 = bare_client(&hub2, mint(hub2.addr, &sa_path.to_string_lossy()));
    let err = client2.resolve(1, 1).await.unwrap_err();
    assert!(
        matches!(&err, ClientError::Transient(m) if m.contains("machine JWT mint failed")),
        "{err:?}"
    );
    assert!(!format!("{err:?}").contains("projected-sa-token-secret"));

    // An unreadable SA token file is the same class of failure.
    let err = bare_client(&hub, mint(hub.addr, "/nonexistent/sa-token"))
        .resolve(1, 1)
        .await
        .unwrap_err();
    assert!(matches!(err, ClientError::Transient(_)));

    let captured = logs.text();
    for secret in [
        "projected-sa-token-secret",
        MINTED_JWT,
        "tok-waddlebots-secret",
    ] {
        assert!(!captured.contains(secret), "log output leaked {secret:?}");
    }
    let _ = std::fs::remove_dir_all(dir);
}

// regression: F2 -- hub-api rotates its signing key: the cached machine JWT is now
// rejected (401). The client must invalidate it, re-mint, and replay ONCE, instead
// of failing until the cached token's own expiry.
#[tokio::test]
async fn a_401_invalidates_the_cached_machine_jwt_and_replays_once_with_a_fresh_one() {
    use svc_ingest::connections::client::{ConnectionsApi, SCOPE_CONNECTIONS_RESOLVE};
    let (logs, _guard) = install_log_capture();
    let hub = MockHub::start().await;
    hub.add(1, "waddlebots", "chan_a");
    {
        let mut st = hub.state.lock().unwrap();
        // Mint #1 is the pre-rotation token hub-api no longer accepts; mint #2 is valid.
        st.mint_queue = vec!["old-jwt-secret".to_string(), "new-jwt-secret".to_string()];
        st.expected_bearer = Some("new-jwt-secret".to_string());
    }
    let dir = std::env::temp_dir().join(format!("svc-ingest-sa-remint-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sa_path = dir.join("token");
    std::fs::write(&sa_path, "projected-sa-token-secret\n").unwrap();
    let minter = service_auth::MachineJwtClient::with_timeouts(
        format!("http://{}/internal/service-token", hub.addr),
        sa_path.to_string_lossy().to_string(),
        SCOPE_CONNECTIONS_RESOLVE,
        service_auth::HttpTimeouts::from_env(),
    )
    .unwrap();
    let client = bare_client(&hub, minter);

    let creds = client
        .resolve(1, 1)
        .await
        .expect("a 401 must be healed by one re-mint + replay");
    assert_eq!(creds.access_token.expose(), "tok-waddlebots-secret");
    {
        let st = hub.state.lock().unwrap();
        assert_eq!(st.bad_auth, 1, "exactly the one rejected attempt");
        assert_eq!(
            st.token_requests.len(),
            2,
            "the 401 triggered exactly one re-mint"
        );
    }
    // The fresh token is cached: the next call needs no further mint.
    client.resolve(1, 1).await.unwrap();
    assert_eq!(hub.state.lock().unwrap().token_requests.len(), 2);
    assert_logs_clean(&logs, "401 re-mint");
    let _ = std::fs::remove_dir_all(dir);
}

// F2: the replay is bounded -- a hub-api that keeps rejecting costs exactly two
// attempts and two mints per call, never a loop, and surfaces as retryable.
#[tokio::test]
async fn a_persistent_401_is_replayed_exactly_once_and_stays_retryable() {
    use svc_ingest::connections::client::{ConnectionsApi, SCOPE_CONNECTIONS_RESOLVE};
    let hub = MockHub::start().await;
    hub.add(1, "waddlebots", "chan_a");
    hub.state.lock().unwrap().expected_bearer = Some("never-minted".to_string());
    let dir = std::env::temp_dir().join(format!("svc-ingest-sa-401loop-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sa_path = dir.join("token");
    std::fs::write(&sa_path, "sa\n").unwrap();
    let minter = service_auth::MachineJwtClient::with_timeouts(
        format!("http://{}/internal/service-token", hub.addr),
        sa_path.to_string_lossy().to_string(),
        SCOPE_CONNECTIONS_RESOLVE,
        service_auth::HttpTimeouts::from_env(),
    )
    .unwrap();
    let client = bare_client(&hub, minter);
    let err = client.resolve(1, 1).await.unwrap_err();
    assert!(matches!(err, ClientError::Transient(_)), "{err:?}");
    let st = hub.state.lock().unwrap();
    assert_eq!(st.bad_auth, 2, "original attempt + exactly one replay");
    assert_eq!(
        st.token_requests.len(),
        2,
        "initial mint + exactly one re-mint"
    );
    drop(st);
    let _ = std::fs::remove_dir_all(dir);
}

/// A public, self-signed test CA certificate (no private key retained) --
/// exercises the explicit-CA https path of `HubConnectionsClient::new`.
const TEST_CA_PEM: &str = "-----BEGIN CERTIFICATE-----
MIIBkTCCATegAwIBAgIUYy1MuFb0jwuOkA69e4KDgezEk28wCgYIKoZIzj0EAwIw
HTEbMBkGA1UEAwwSc3ZjLWluZ2VzdC10ZXN0LWNhMCAXDTI2MTAxNDEzNTgzOVoY
DzIxMjYwOTIwMTM1ODM5WjAdMRswGQYDVQQDDBJzdmMtaW5nZXN0LXRlc3QtY2Ew
WTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAARaPt5lGVDmlpk5TnS/3RRLe/CjKPl6
oJfqxi22A2x3ZVc8/zJd7OWkGcF693dLaTwmkVKT260PA3BDnqDPbSgVo1MwUTAd
BgNVHQ4EFgQUdvS/GR3MkPGz3efzBKoGUR6nE2AwHwYDVR0jBBgwFoAUdvS/GR3M
kPGz3efzBKoGUR6nE2AwDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQDAgNIADBF
AiBrkS6cHJyD0oe8tr/1LK445c/UaAfHwtkK9YPiWMed3gIhALKHeSCitnFC++nt
yZBAUeSQHAH5Zn2Ky4FSSnRXLOZZ
-----END CERTIFICATE-----
";

#[tokio::test]
async fn an_https_base_with_an_explicit_ca_builds_and_is_not_plaintext() {
    svc_ingest::crypto::ensure_installed();
    let dir = std::env::temp_dir().join(format!("svc-ingest-ca-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let ca = dir.join("ca.pem");
    std::fs::write(&ca, TEST_CA_PEM).unwrap();
    let metrics = Arc::new(ConnectionsMetrics::register(&prometheus::Registry::new()).unwrap());
    let cfg = HubClientConfig {
        base_url: "https://hub-api.waddlebot.svc:8204".to_string(),
        ca_file: Some(ca),
        request_timeout: Duration::from_secs(2),
        connect_timeout: Duration::from_secs(1),
    };
    let client = HubConnectionsClient::new(&cfg, StaticBearer, metrics.clone()).unwrap();
    assert!(!client.is_plaintext());
    let plain = HubConnectionsClient::new(
        &HubClientConfig {
            base_url: "http://hub-api:8204".into(),
            ca_file: None,
            ..cfg
        },
        StaticBearer,
        metrics,
    )
    .unwrap();
    assert!(plain.is_plaintext());
    let _ = std::fs::remove_dir_all(dir);
}
