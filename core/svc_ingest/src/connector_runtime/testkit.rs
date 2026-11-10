//! Shared test fixtures for the connector runtime (cfg(test) only): desired
//! connection builders, a scripted binding resolver, a test profile, a
//! loopback WebSocket server and a tracing capture subscriber.

use std::collections::{BTreeMap, HashMap};
use std::fmt;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use connection_resolver::{
    BindingPage, BindingSummary, ConnectionKind, ConnectionRef, ConnectionResolve, ResolveError,
    ResolvedConnection,
};
use futures_util::{SinkExt, StreamExt};
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::Message;

use super::feed::{ConnKey, DesiredConnection, TransportPlan, WebhookPlan};
use super::plan::{ConnectorProfile, PlanError, RuntimeEnv};
use crate::connector::webhook::{RateLimit, WebhookRoute};
use crate::connector::webhook_verify::Scheme;
use crate::connector::{SessionRules, WsConfig};

/// A WebSocket config pointing at a loopback test server.
pub(crate) fn ws_config(platform: &str, port: u16, source_key: &str) -> WsConfig {
    WsConfig {
        platform: platform.into(),
        url: format!("ws://127.0.0.1:{port}/"),
        source_key: source_key.into(),
        handles: HashMap::new(),
        resume_token: None,
        connect_timeout: Duration::from_secs(1),
        idle_timeout: Duration::from_secs(5),
        max_lifetime: Duration::from_secs(60),
        egress_allow: vec!["127.0.0.1".parse().expect("ip literal")],
        max_frame_bytes: 4096,
        dedup_window: 0,
        allow_loopback_insecure: true,
        slot_prefixes: HashMap::new(),
        auto_replies: vec![],
        self_echo: None,
        heartbeat_interval: None,
        session: SessionRules::default(),
    }
}

/// A desired WebSocket connection.
pub(crate) fn desired_ws(
    platform: &'static str,
    community: i64,
    conn: i64,
    port: u16,
    source_key: &str,
    fingerprint: u64,
) -> DesiredConnection {
    DesiredConnection {
        key: ConnKey {
            platform,
            community_id: community,
            connection_id: conn,
            lane: Default::default(),
        },
        tenant_id: 1,
        plan: TransportPlan::Ws(Box::new(ws_config(platform, port, source_key))),
        fingerprint,
    }
}

/// A valid webhook route owned by `community`.
pub(crate) fn webhook_route(route_key: &str, community: i64, conn: i64) -> WebhookRoute {
    WebhookRoute {
        route_key: route_key.into(),
        owner_bundle: "twitch".into(),
        scheme: Scheme::twitch_eventsub(),
        secret_ref: ConnectionRef {
            community_id: community,
            connection_id: conn,
        },
        replay_window: Duration::from_secs(300),
        rate_limit: RateLimit {
            max: 100,
            per: Duration::from_secs(60),
        },
        body_limit: 1024,
    }
}

/// A desired webhook connection.
pub(crate) fn desired_webhook(
    community: i64,
    conn: i64,
    route_key: &str,
    fingerprint: u64,
) -> DesiredConnection {
    DesiredConnection {
        key: ConnKey {
            platform: "twitch",
            community_id: community,
            connection_id: conn,
            lane: Default::default(),
        },
        tenant_id: 1,
        plan: TransportPlan::Webhook(Box::new(WebhookPlan {
            route: webhook_route(route_key, community, conn),
            authorized_community: community,
        })),
        fingerprint,
    }
}

/// A binding as hub-api lists it.
pub(crate) fn binding(community: i64, conn: i64, meta: &[(&str, &str)]) -> BindingSummary {
    BindingSummary {
        tenant_id: 1,
        secret_version: 1,
        community_id: community,
        connection_id: conn,
        provider: "twitchprov".to_owned(),
        kind: ConnectionKind::PlatformOauth,
        metadata: meta
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect::<BTreeMap<_, _>>(),
    }
}

/// Profile whose behavior is chosen by the binding's `mode` metadata:
/// absent -> one ws connection keyed by `channel`; `bad` -> `PlanError`;
/// `mismatch` -> a connection under a different connection id; `two` -> a ws
/// and a webhook connection; `webhook` -> a webhook route keyed by `route`.
pub(crate) struct TestProfile;

impl ConnectorProfile for TestProfile {
    fn platform(&self) -> &'static str {
        "twitch"
    }
    fn app_id(&self) -> &'static str {
        "waddles.core.service.twitch"
    }
    fn provider(&self) -> &'static str {
        "twitchprov"
    }
    fn capability(&self) -> &'static str {
        "chat_ingest"
    }
    fn plan(
        &self,
        b: &BindingSummary,
        _env: &RuntimeEnv,
    ) -> Result<Vec<DesiredConnection>, PlanError> {
        let channel = b
            .metadata
            .get("channel")
            .map(String::as_str)
            .unwrap_or("chan");
        let ws = |conn| desired_ws("twitch", b.community_id, conn, 1, channel, 7);
        match b.metadata.get("mode").map(String::as_str) {
            Some("bad") => Err(PlanError::MissingKey("channel")),
            Some("budget") => Err(PlanError::IdentifyBudgetNotReady),
            Some("mismatch") => Ok(vec![ws(b.connection_id + 1)]),
            Some("two") => Ok(vec![
                ws(b.connection_id),
                desired_webhook(
                    b.community_id,
                    b.connection_id,
                    &format!("route-{}-{}", b.community_id, b.connection_id),
                    7,
                ),
            ]),
            Some("webhook") => Ok(vec![desired_webhook(
                b.community_id,
                b.connection_id,
                b.metadata
                    .get("route")
                    .map(String::as_str)
                    .unwrap_or("route-default-1"),
                7,
            )]),
            _ => Ok(vec![ws(b.connection_id)]),
        }
    }
}

/// Resolver answering `list_bindings` from a script (one entry per call).
pub(crate) struct ScriptedResolver {
    pub script: Mutex<Vec<Result<BindingPage, ResolveError>>>,
    pub afters: Mutex<Vec<i64>>,
    pub hang: bool,
    pub endless: bool,
}

impl ScriptedResolver {
    pub fn new(script: Vec<Result<BindingPage, ResolveError>>) -> Self {
        Self {
            script: Mutex::new(script),
            afters: Mutex::new(vec![]),
            hang: false,
            endless: false,
        }
    }
}

impl ConnectionResolve for ScriptedResolver {
    async fn resolve(&self, _r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
        Err(ResolveError::NotFound)
    }

    async fn list_bindings(
        &self,
        _p: &str,
        _c: Option<&str>,
        after: i64,
        _l: u32,
    ) -> Result<BindingPage, ResolveError> {
        self.afters.lock().expect("lock").push(after);
        if self.hang {
            std::future::pending::<()>().await;
        }
        if self.endless {
            return Ok(BindingPage {
                items: vec![],
                next_after: Some(after + 1),
            });
        }
        self.script.lock().expect("lock").remove(0)
    }
}

/// A page of bindings.
pub(crate) fn page(
    items: Vec<BindingSummary>,
    next: Option<i64>,
) -> Result<BindingPage, ResolveError> {
    Ok(BindingPage {
        items,
        next_after: next,
    })
}

/// A loopback WebSocket server: every accepted client gets a text frame every
/// 15 ms until it goes away. `accepts` counts completed WS handshakes.
pub(crate) struct WsServer {
    pub port: u16,
    pub accepts: Arc<AtomicUsize>,
    task: tokio::task::JoinHandle<()>,
}

impl WsServer {
    /// Binds an ephemeral port and starts serving on the current runtime.
    pub async fn start() -> Self {
        let l = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let port = l.local_addr().expect("addr").port();
        let accepts = Arc::new(AtomicUsize::new(0));
        let a = accepts.clone();
        let task = tokio::spawn(async move {
            loop {
                let Ok((tcp, _)) = l.accept().await else {
                    return;
                };
                let a = a.clone();
                tokio::spawn(async move {
                    let Ok(mut ws) = tokio_tungstenite::accept_async(tcp).await else {
                        return;
                    };
                    a.fetch_add(1, Ordering::SeqCst);
                    loop {
                        tokio::select! {
                            incoming = ws.next() => match incoming {
                                Some(Ok(_)) => {}
                                _ => return,
                            },
                            () = tokio::time::sleep(Duration::from_millis(15)) => {
                                if ws.send(Message::text("frame")).await.is_err() {
                                    return;
                                }
                            }
                        }
                    }
                });
            }
        });
        Self {
            port,
            accepts,
            task,
        }
    }

    /// Completed handshakes so far.
    pub fn accepted(&self) -> usize {
        self.accepts.load(Ordering::SeqCst)
    }
}

impl Drop for WsServer {
    fn drop(&mut self) {
        self.task.abort();
    }
}

/// A TCP listener that accepts connections but never answers the WebSocket
/// upgrade, so a client's connect hangs until its own timeout.
pub(crate) struct BlackholeServer {
    pub port: u16,
    pub accepts: Arc<AtomicUsize>,
    task: tokio::task::JoinHandle<()>,
}

impl BlackholeServer {
    /// Starts listening on an ephemeral port.
    pub async fn start() -> Self {
        let l = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let port = l.local_addr().expect("addr").port();
        let accepts = Arc::new(AtomicUsize::new(0));
        let a = accepts.clone();
        let task = tokio::spawn(async move {
            let mut held = Vec::new();
            while let Ok((tcp, _)) = l.accept().await {
                a.fetch_add(1, Ordering::SeqCst);
                held.push(tcp);
            }
        });
        Self {
            port,
            accepts,
            task,
        }
    }

    /// TCP connections accepted so far.
    pub fn accepted(&self) -> usize {
        self.accepts.load(Ordering::SeqCst)
    }
}

impl Drop for BlackholeServer {
    fn drop(&mut self) {
        self.task.abort();
    }
}

/// Process-wide tracing subscriber recording every event into a buffer keyed
/// by the emitting thread. A scoped (thread-local) subscriber is not used:
/// its callsite-interest handling races with parallel tests and silently
/// drops events, which would make a "no PII logged" assertion vacuous.
struct GlobalCapture {
    buffers: Mutex<HashMap<std::thread::ThreadId, Vec<String>>>,
}

struct FieldVisitor<'a>(&'a mut Vec<String>);

impl tracing::field::Visit for FieldVisitor<'_> {
    fn record_debug(&mut self, f: &tracing::field::Field, v: &dyn fmt::Debug) {
        self.0.push(format!("{}={v:?}", f.name()));
    }
}

impl tracing::Subscriber for &'static GlobalCapture {
    fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        tracing::span::Id::from_u64(1)
    }
    fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
    fn event(&self, e: &tracing::Event<'_>) {
        let mut g = self.buffers.lock().expect("lock");
        let buf = g.entry(std::thread::current().id()).or_default();
        e.record(&mut FieldVisitor(buf));
    }
    fn enter(&self, _: &tracing::span::Id) {}
    fn exit(&self, _: &tracing::span::Id) {}
}

static CAPTURE: std::sync::OnceLock<&'static GlobalCapture> = std::sync::OnceLock::new();

/// What the current thread has logged since [`capture`] was called.
pub(crate) struct Capture(&'static GlobalCapture);

impl Capture {
    /// Everything this thread logged so far, joined.
    pub fn text(&self) -> String {
        let g = self.0.buffers.lock().expect("lock");
        match g.get(&std::thread::current().id()) {
            Some(v) => v.join(" | "),
            None => String::new(),
        }
    }
}

/// Starts capturing the current thread's tracing events (tests that assert on
/// logs must run on a current-thread runtime so every event stays on it).
/// Panics if another global subscriber was installed first: a silent no-op
/// capture would make the assertions vacuous.
pub(crate) fn capture() -> Capture {
    let cap = CAPTURE.get_or_init(|| {
        let leaked: &'static GlobalCapture = Box::leak(Box::new(GlobalCapture {
            buffers: Mutex::new(HashMap::new()),
        }));
        tracing::subscriber::set_global_default(leaked)
            .expect("no other global tracing subscriber may be installed in lib tests");
        leaked
    });
    // Registering a dispatcher rebuilds tracing's process-wide callsite
    // interest cache. A callsite first hit by a parallel test before (or while)
    // the global subscriber was installed can otherwise stay cached as
    // "never" and silently drop this test's events.
    drop(tracing::Dispatch::new(*cap));
    cap.buffers
        .lock()
        .expect("lock")
        .insert(std::thread::current().id(), Vec::new());
    Capture(cap)
}
