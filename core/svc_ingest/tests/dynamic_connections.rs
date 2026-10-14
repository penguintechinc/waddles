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
    Ok { token: String, bot: String },
    Fail { status: u16, body: Value },
    FailThenOk { fails_left: u32, status: u16, token: String, bot: String },
}

#[derive(Default)]
struct HubState {
    bindings: Vec<Value>,
    behaviors: HashMap<(i64, i64), Behavior>,
    list_status: Option<u16>,
    resolve_calls: Vec<(i64, i64)>,
    list_queries: Vec<HashMap<String, String>>,
    bad_auth: u32,
}

#[derive(Clone)]
struct MockHub {
    state: Arc<Mutex<HubState>>,
    addr: SocketAddr,
}

fn auth_ok(headers: &HeaderMap) -> bool {
    headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v == format!("Bearer {BEARER}"))
}

async fn hub_list(
    State(state): State<Arc<Mutex<HubState>>>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
) -> (StatusCode, Json<Value>) {
    let mut st = state.lock().unwrap();
    if !auth_ok(&headers) {
        st.bad_auth += 1;
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": "unauthorized"})));
    }
    st.list_queries.push(query.clone());
    if let Some(status) = st.list_status {
        return (
            StatusCode::from_u16(status).unwrap(),
            Json(json!({"success": false, "error": {"code": "CONNECTIONS_UNAVAILABLE", "message": "x"}})),
        );
    }
    let after: i64 = query.get("after").and_then(|v| v.parse().ok()).unwrap_or(0);
    let limit: usize = query.get("limit").and_then(|v| v.parse().ok()).unwrap_or(500);
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
    if !auth_ok(&headers) {
        st.bad_auth += 1;
        return (StatusCode::UNAUTHORIZED, Json(json!({"error": "unauthorized"})));
    }
    st.resolve_calls.push((community, connection));
    let key = (community, connection);
    let behavior = st.behaviors.get(&key).cloned();
    let ok = |token: &str, bot: &str| {
        (
            StatusCode::OK,
            Json(json!({
                "success": true, "connectionId": connection, "communityId": community,
                "provider": "twitch", "accessToken": token, "expiresAt": null,
                "metadata": {"bot_login": bot, "channel_login": "ignored"}
            })),
        )
    };
    match behavior {
        Some(Behavior::Ok { token, bot }) => ok(&token, &bot),
        Some(Behavior::Fail { status, body }) => (StatusCode::from_u16(status).unwrap(), Json(body)),
        Some(Behavior::FailThenOk { fails_left, status, token, bot }) => {
            if fails_left > 0 {
                st.behaviors.insert(
                    key,
                    Behavior::FailThenOk { fails_left: fails_left - 1, status, token, bot },
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
            Behavior::Ok { token: format!("tok-{bot}-secret"), bot: bot.to_string() },
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
        self.state.lock().unwrap().behaviors.insert((id, id), behavior);
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
}

impl FakeIrc {
    async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let state = Arc::new(Mutex::new(IrcState::default()));
        let (push, _) = broadcast::channel::<String>(64);
        let (kill, _) = watch::channel(0u64);
        let this = Self { port, state: state.clone(), push: push.clone(), kill: kill.clone() };
        tokio::spawn(async move {
            loop {
                let Ok((socket, _)) = listener.accept().await else { return };
                let idx = {
                    let mut st = state.lock().unwrap();
                    st.conns.push(Vec::new());
                    st.open += 1;
                    st.conns.len() - 1
                };
                let state = state.clone();
                let mut push_rx = push.subscribe();
                let mut kill_rx = kill.subscribe();
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
        IrcSettings { host: "127.0.0.1".into(), port: self.port, use_tls: false }
    }

    fn lines(&self, conn: usize) -> Vec<String> {
        self.state.lock().unwrap().conns.get(conn).cloned().unwrap_or_default()
    }

    fn conn_count(&self) -> usize {
        self.state.lock().unwrap().conns.len()
    }

    fn open_count(&self) -> usize {
        self.state.lock().unwrap().open
    }

    /// The JOIN line connection `conn` sent, if any.
    fn join_line(&self, conn: usize) -> Option<String> {
        self.lines(conn).into_iter().find(|l| l.starts_with("JOIN "))
    }

    fn pass_line(&self, conn: usize) -> Option<String> {
        self.lines(conn).into_iter().find(|l| l.starts_with("PASS "))
    }

    /// Finds the connection whose NICK is `nick`.
    fn conn_for_nick(&self, nick: &str) -> Option<usize> {
        let st = self.state.lock().unwrap();
        (0..st.conns.len()).rev().find(|i| st.conns[*i].iter().any(|l| l == &format!("NICK {nick}")))
    }

    fn send_privmsg(&self, channel: &str, sender: &str, text: &str) {
        let _ = self
            .push
            .send(format!(":{sender}!{sender}@{sender}.tmi.twitch.tv PRIVMSG #{channel} :{text}"));
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
}

impl EventAppender for RecordingAppender {
    async fn append(&self, stream: &str, env: &StageEnvelope) -> Result<String, SpineError> {
        self.calls.lock().unwrap().push((stream.to_string(), env.clone()));
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

type TestSupervisor = Supervisor<HubConnectionsClient<StaticBearer>, RecordingAppender, NoopMetrics>;

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
    Harness { hub, irc, appender: publish, metrics, supervisor }
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
        h.irc.conn_for_nick("waddlebots").and_then(|c| h.irc.join_line(c)).is_some()
            && h.irc.conn_for_nick("otherbot").and_then(|c| h.irc.join_line(c)).is_some()
    })
    .await;

    assert_eq!(h.irc.conn_count(), 2, "ONE socket per bot account, never one per channel");
    let wb = h.irc.conn_for_nick("waddlebots").unwrap();
    assert_eq!(
        h.irc.join_line(wb).as_deref(),
        Some("JOIN #chan_a,#chan_b"),
        "all of one bot's channels in a single JOIN"
    );
    assert_eq!(h.irc.pass_line(wb).as_deref(), Some("PASS oauth:tok-waddlebots-secret"));
    let ob = h.irc.conn_for_nick("otherbot").unwrap();
    assert_eq!(h.irc.join_line(ob).as_deref(), Some("JOIN #chan_c"));

    // list used the contract's query; every call carried the machine bearer.
    {
        let st = h.hub.state.lock().unwrap();
        assert_eq!(st.list_queries[0]["provider"], "twitch");
        assert_eq!(st.list_queries[0]["capability"], "chat_ingest");
        assert_eq!(st.list_queries[0]["after"], "0");
        assert_eq!(st.bad_auth, 0, "every request must carry the machine JWT bearer");
        assert_eq!(st.resolve_calls.len(), 3, "one resolve per binding on first connect");
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
        assert!(streams.contains(&expected), "missing stream {expected}: {streams:?}");
    }

    assert_eq!(h.metrics.bots_running.get(), 2);
    assert_eq!(h.metrics.channels_joined.get(), 3);
    assert_eq!(
        h.metrics.events_routed_total.with_label_values(&["published"]).get(),
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
        h.irc.conn_for_nick("waddlebots").and_then(|c| h.irc.join_line(c)).as_deref()
            == Some("JOIN #chan_a")
    })
    .await;
    assert_eq!(h.hub.resolves_for(2), 1);
    assert_eq!(
        h.metrics.resolve_not_usable_total.with_label_values(&["revoked_grant"]).get(),
        1
    );

    // The refusal is sticky while the same binding version stays listed: more
    // reconcile passes neither re-resolve it nor reconnect the surviving bot.
    for _ in 0..3 {
        h.supervisor.reconcile_once().await.unwrap();
    }
    tokio::time::sleep(Duration::from_millis(150)).await;
    assert_eq!(h.hub.resolves_for(2), 1, "a 409 must never be retried");
    assert_eq!(h.irc.conn_count(), 1, "surviving channel's socket must not churn");

    // Editing the binding (new updatedAt) makes it eligible again.
    {
        let mut st = h.hub.state.lock().unwrap();
        for b in &mut st.bindings {
            if b["grantId"] == 2 {
                b["updatedAt"] = json!("v2");
            }
        }
    }
    h.hub.set_behavior(2, Behavior::Ok { token: "tok-waddlebots-secret".into(), bot: "waddlebots".into() });
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
    assert_eq!(h.irc.conn_count(), 0, "a refused-only bot never opens an IRC socket");
    assert_eq!(h.hub.resolves_for(1), 1);
}

#[tokio::test]
async fn a_503_on_resolve_is_retried_with_backoff_not_dropped() {
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
        h.irc.conn_for_nick("waddlebots").and_then(|c| h.irc.join_line(c)).is_some()
    })
    .await;
    assert!(h.hub.resolves_for(1) >= 3, "2 transient failures then success");
    assert_eq!(
        h.metrics.resolve_not_usable_total.with_label_values(&["other"]).get(),
        0,
        "a 503 is retryable, never counted as a drop"
    );
    h.supervisor.stop_all().await;
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
    assert!(matches!(err, ReconcileError::List(ClientError::Transient(_))));
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(h.supervisor.running_bots(), 1, "an outage must not tear bots down");
    assert_eq!(h.irc.open_count(), 1);
    assert_eq!(h.irc.conn_count(), 1, "and must not churn the socket");
    assert_eq!(h.metrics.reconcile_total.with_label_values(&["list_failed"]).get(), 1);

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
    assert_eq!(h.irc.pass_line(0).as_deref(), Some("PASS oauth:tok-waddlebots-secret"));
    assert_eq!(h.hub.resolves_for(1), 1);

    // hub-api rotates the token (refresh); the socket then drops.
    h.hub.set_behavior(
        1,
        Behavior::Ok { token: "tok-rotated-secret".into(), bot: "waddlebots".into() },
    );
    h.irc.drop_all_connections();
    eventually("reconnected with the rotated token", || {
        h.irc.conn_count() >= 2 && h.irc.pass_line(1).is_some()
    })
    .await;
    assert_eq!(h.irc.pass_line(1).as_deref(), Some("PASS oauth:tok-rotated-secret"));
    assert_eq!(h.hub.resolves_for(1), 2, "token re-resolved on reconnect, never cached");
    h.supervisor.stop_all().await;
}

#[tokio::test]
async fn twitch_auth_rejection_re_resolves_and_keeps_retrying_instead_of_stopping() {
    let mut h = harness().await;
    h.irc.set_reject_auth(true);
    h.hub.add(1, "waddlebots", "chan_a");
    h.supervisor.reconcile_once().await.unwrap();
    eventually("at least two resolve attempts while auth is rejected", || {
        h.hub.resolves_for(1) >= 2
    })
    .await;
    assert_eq!(h.supervisor.running_bots(), 1, "auth rejection must not stop the bot for good");

    // Once Twitch accepts the (refreshed) token the bot recovers on its own.
    h.irc.set_reject_auth(false);
    eventually("recovered", || {
        h.irc.conn_for_nick("waddlebots").is_some_and(|c| h.irc.join_line(c).is_some())
    })
    .await;
    h.supervisor.stop_all().await;
}

#[tokio::test]
async fn a_token_for_a_different_bot_login_is_refused_not_used() {
    let mut h = harness().await;
    h.hub.add(1, "waddlebots", "chan_a");
    // hub-api claims this connection authenticates as someone else.
    h.hub.set_behavior(1, Behavior::Ok { token: "tok-impostor-secret".into(), bot: "someoneelse".into() });
    h.supervisor.reconcile_once().await.unwrap();
    eventually("refused", || {
        h.metrics.resolve_not_usable_total.with_label_values(&["identity_mismatch"]).get() == 1
    })
    .await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(h.irc.conn_count(), 0, "a mismatched identity must never reach Twitch");
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
    assert_eq!(h.metrics.bindings_skipped_total.with_label_values(&["no_scope"]).get(), 1);
    eventually("joined only the scoped channel", || {
        h.irc.join_line(0).as_deref() == Some("JOIN #chan_scoped")
    })
    .await;
    assert_eq!(h.hub.resolves_for(1), 0, "an unscoped binding is never even resolved");

    h.irc.send_privmsg("chan_scoped", "viewer", "scoped hello");
    eventually("published", || !published(&h).is_empty()).await;
    let calls = published(&h);
    assert_eq!(calls[0].0, "waddles:t:acme:c:main:src:twitch:tw-chan_scoped:events");
    assert_eq!(calls[0].1.tenant, "acme");
    assert_eq!(calls[0].1.community.as_deref(), Some("main"));
    assert!(!logs.text().contains("tok-waddlebots-secret"));
    h.supervisor.stop_all().await;
}

#[tokio::test]
async fn run_loop_picks_up_new_bindings_and_stops_every_bot_on_shutdown() {
    let h = harness().await;
    let Harness { hub, irc, metrics, supervisor, .. } = h;
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
