//! Real-path tests for per-community Twitch sessions: the REAL `RealRunner`
//! (real `ResolvingConnector`, real `penguin-connector-twitch` IRC client,
//! real TCP sockets) against an in-process IRC server and a REAL Valkey for
//! `ValkeyLease`. Only the hub boundary (`ConnectionResolve`) and the spine
//! appender are in-memory. Every test asserts how many connections/events it
//! examined -- a zero denominator fails (Verification Integrity).
//!
//! Needs `VALKEY_URL` (a real Valkey); an unset var FAILS, never skips.
//! Capture of tracing output is process-global (installed once) so no
//! callsite-interest race can hide a log line.

#[path = "support/irc_server.rs"]
mod irc_server;

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock, PoisonError};
use std::time::Duration;

use connection_resolver::{
    BindingPage, BindingSummary, ConnectionKind, ConnectionRef, ConnectionResolve,
    ConnectionSecret, Endpoint, ResolveError, ResolvedConnection, SecretString,
};
use irc_server::IrcTestServer;
use penguin_spine::{KeyRing, Scope, SpineError, SpineMetrics, StageEnvelope};
use svc_ingest::publish::EventAppender;
use svc_ingest::twitch_sessions::{
    DiscoveryOutcome, LeaseOutcome, RealRunner, ReconcileReport, SessionDeps, SessionEnd,
    SessionManager, SessionMetrics, SessionRunner, ValkeyLease,
};

type Key = (i64, i64);

/// Substrings that must never reach any log/span: every token, channel login
/// and bot login in this file embeds one of them.
const SECRET_MARKERS: [&str; 3] = ["SECRETTOKEN", "chanlogin", "botlogin"];

// ---------------------------------------------------------------- capture

/// Concatenated rendered fields of every event/span seen process-wide.
static CAPTURED: Mutex<Vec<String>> = Mutex::new(Vec::new());
static SPAN_IDS: AtomicU64 = AtomicU64::new(1);

struct FieldText(String);
impl tracing::field::Visit for FieldText {
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        self.0.push_str(&format!("{}={:?} ", field.name(), value));
    }
}

/// Process-global capturing subscriber: every event message/fields and every
/// span's name + attributes land in [`CAPTURED`].
struct Capture;
impl Capture {
    fn push(s: String) {
        CAPTURED
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(s);
    }
}
impl tracing::Subscriber for Capture {
    fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, attrs: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        let mut v = FieldText(format!("span:{} ", attrs.metadata().name()));
        attrs.record(&mut v);
        Self::push(v.0);
        tracing::span::Id::from_u64(SPAN_IDS.fetch_add(1, Ordering::Relaxed))
    }
    fn record(&self, _: &tracing::span::Id, values: &tracing::span::Record<'_>) {
        let mut v = FieldText("span-record ".to_string());
        values.record(&mut v);
        Self::push(v.0);
    }
    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
    fn event(&self, event: &tracing::Event<'_>) {
        let mut v = FieldText(format!("event:{} ", event.metadata().target()));
        event.record(&mut v);
        Self::push(v.0);
    }
    fn enter(&self, _: &tracing::span::Id) {}
    fn exit(&self, _: &tracing::span::Id) {}
}

/// Installs the global capture once; every test calls it first so no
/// callsite is ever registered without it.
fn init_capture() {
    static ONCE: OnceLock<()> = OnceLock::new();
    ONCE.get_or_init(|| {
        tracing::subscriber::set_global_default(Capture).expect("install global capture");
    });
}

// ------------------------------------------------------------------ doubles

/// One binding row in the fake hub.
#[derive(Clone)]
struct Binding {
    channel: String,
    bot: String,
    token: String,
    expires_at: Option<i64>,
}

/// In-memory hub (the `ConnectionResolve` boundary) over a mutable table.
#[derive(Default)]
struct Hub {
    table: Mutex<BTreeMap<Key, Binding>>,
    list_fail_ticks: AtomicU32,
    resolve_down: AtomicBool,
    resolve_failures: AtomicU32,
    resolves: Mutex<Vec<(Key, String)>>,
}

impl Hub {
    fn put(&self, key: Key, channel: &str, bot: &str, token: &str, expires_at: Option<i64>) {
        self.table
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .insert(
                key,
                Binding {
                    channel: channel.to_string(),
                    bot: bot.to_string(),
                    token: token.to_string(),
                    expires_at,
                },
            );
    }

    fn remove(&self, key: Key) {
        self.table
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .remove(&key);
    }

    fn set_token(&self, key: Key, token: &str, expires_at: Option<i64>) {
        let mut t = self.table.lock().unwrap_or_else(PoisonError::into_inner);
        let b = t.get_mut(&key).expect("binding exists");
        b.token = token.to_string();
        b.expires_at = expires_at;
    }

    /// Tokens handed out for `key`, in resolve order.
    fn resolved_tokens(&self, key: Key) -> Vec<String> {
        self.resolves
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .filter(|(k, _)| *k == key)
            .map(|(_, t)| t.clone())
            .collect()
    }
}

impl ConnectionResolve for Hub {
    async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
        if self.resolve_down.load(Ordering::SeqCst) {
            self.resolve_failures.fetch_add(1, Ordering::SeqCst);
            return Err(ResolveError::Transient("hub down"));
        }
        let key = (r.community_id, r.connection_id);
        let b = self
            .table
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .get(&key)
            .cloned()
            .ok_or(ResolveError::NotFound)?;
        self.resolves
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push((key, b.token.clone()));
        Ok(ResolvedConnection {
            connection_id: r.connection_id,
            community_id: r.community_id,
            provider: "twitch".into(),
            kind: ConnectionKind::PlatformOauth,
            endpoint: Endpoint::default(),
            secret: ConnectionSecret::OAuthAccessToken {
                access_token: SecretString::new(b.token),
            },
            expires_at: b.expires_at,
        })
    }

    async fn list_bindings(
        &self,
        provider: &str,
        _capability: Option<&str>,
        after: i64,
        _limit: u32,
    ) -> Result<BindingPage, ResolveError> {
        if self
            .list_fail_ticks
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
            .is_ok()
        {
            return Err(ResolveError::Transient("hub outage"));
        }
        assert_eq!(provider, "twitch");
        let items = self
            .table
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .filter(|((_, conn), _)| *conn > after)
            .map(|((community, conn), b)| BindingSummary {
                community_id: *community,
                connection_id: *conn,
                provider: "twitch".into(),
                kind: ConnectionKind::PlatformOauth,
                metadata: BTreeMap::from([
                    ("channel".to_string(), b.channel.clone()),
                    ("bot_login".to_string(), b.bot.clone()),
                ]),
            })
            .collect();
        Ok(BindingPage {
            items,
            next_after: None,
        })
    }
}

/// Records every appended `(stream, envelope)`.
#[derive(Default)]
struct Recorder {
    calls: Mutex<Vec<(String, StageEnvelope)>>,
}

impl Recorder {
    fn streams(&self) -> Vec<String> {
        self.calls
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .map(|(s, _)| s.clone())
            .collect()
    }
    fn count(&self) -> usize {
        self.streams().len()
    }
    fn count_for(&self, channel: &str) -> usize {
        let needle = format!(":tw-{channel}:");
        self.streams()
            .iter()
            .filter(|s| s.contains(&needle))
            .count()
    }
}

impl EventAppender for Recorder {
    async fn append(&self, stream: &str, env: &StageEnvelope) -> Result<String, SpineError> {
        self.calls
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push((stream.to_string(), env.clone()));
        Ok("1-0".to_string())
    }
}

struct NoopSpine;
impl SpineMetrics for NoopSpine {}

/// Session-manager metrics that remember what they were told.
#[derive(Default)]
struct Metrics {
    ended: Mutex<Vec<SessionEnd>>,
    resolve_failed: Mutex<Vec<&'static str>>,
}

impl SessionMetrics for Metrics {
    fn session_ended(&self, reason: SessionEnd) {
        self.ended
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(reason);
    }
    fn lease_event(&self, _: LeaseOutcome) {}
    fn discovery(&self, _: DiscoveryOutcome) {}
    fn reconcile_done(&self, _: &ReconcileReport, _: Duration, _: usize) {}
    fn set_live_sessions(&self, _: usize) {}
    fn resolve_failed(&self, class: &'static str) {
        self.resolve_failed
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(class);
    }
    fn discovery_skipped(&self, _: &'static str, _: u32) {}
}

// ------------------------------------------------------------------ harness

/// A random positive numeric id: unique per run so leases left in the shared
/// Valkey by an earlier run/test can never collide.
fn fresh_id() -> i64 {
    (uuid::Uuid::new_v4().as_u128() as i64 & 0x3FFF_FFFF_FFFF) + 1
}

fn fresh_key() -> Key {
    (fresh_id(), fresh_id())
}

/// Everything one test needs.
struct Env {
    server: IrcTestServer,
    hub: Arc<Hub>,
    appender: Arc<Recorder>,
    metrics: Arc<Metrics>,
}

impl Env {
    async fn new() -> Self {
        init_capture();
        Self {
            server: IrcTestServer::start().await,
            hub: Arc::new(Hub::default()),
            appender: Arc::new(Recorder::default()),
            metrics: Arc::new(Metrics::default()),
        }
    }

    /// A manager over the REAL runner and a REAL Valkey lease.
    async fn manager(&self, replica: &str) -> SessionManager<Hub, ValkeyLease> {
        let url = std::env::var("VALKEY_URL")
            .expect("VALKEY_URL must be set: these tests need a REAL Valkey (no silent skip)");
        let client = redis::Client::open(url).expect("valkey client");
        let cfg =
            redis::AsyncConnectionConfig::new().set_response_timeout(Some(Duration::from_secs(2)));
        let conn = client
            .get_multiplexed_async_connection_with_config(&cfg)
            .await
            .expect("real valkey must be reachable");
        let lease = Arc::new(ValkeyLease::with_replica_id(conn, replica.to_string()));
        let runner: Arc<dyn SessionRunner> = Arc::new(RealRunner::new(
            self.hub.clone(),
            self.appender.clone(),
            Arc::new(NoopSpine),
            Arc::new(KeyRing::new(vec![("k1".to_string(), vec![9u8; 32])])),
            "k1".to_string(),
            Arc::new(Scope::new("acme", None)),
            "127.0.0.1".to_string(),
            self.server.port(),
            false,
        ));
        SessionManager::new(
            self.hub.clone(),
            lease,
            SessionDeps {
                runner,
                metrics: self.metrics.clone(),
            },
        )
    }

    /// Waits until connection `idx` has registered AND joined.
    async fn joined(&self, idx: usize) {
        eventually(&format!("conn {idx} registered+joined"), || {
            self.server
                .connections()
                .get(idx)
                .is_some_and(|c| c.registered && !c.joined.is_empty())
        })
        .await;
    }

    /// Waits for exactly `n` appended events, then confirms none more arrive.
    async fn events_settle_at(&self, n: usize) {
        eventually(&format!("{n} events appended"), || {
            self.appender.count() >= n
        })
        .await;
        // Bounded re-poll: a late duplicate must not slip past a short sleep.
        stays("event count", n, || self.appender.count()).await;
    }
}

/// Polls `f` (real time) until true; panics naming `what` after 15s.
async fn eventually(what: &str, mut f: impl FnMut() -> bool) {
    for _ in 0..750 {
        if f() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("timed out waiting for: {what}");
}

/// Re-polls `f` every 20ms for 2s and asserts it never leaves `expected`.
async fn stays(what: &str, expected: usize, f: impl Fn() -> usize) {
    for _ in 0..100 {
        assert_eq!(
            f(),
            expected,
            "{what} changed from {expected} (late duplicate?)"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

/// Jumps the clock `by` without letting auto-advance distort real-socket
/// waits: freeze, advance, resume immediately.
async fn jump_clock(by: Duration) {
    tokio::time::pause();
    tokio::time::advance(by).await;
    tokio::time::resume();
}

// -------------------------------------------------------------------- tests

#[tokio::test]
async fn session_lifecycle_connect_receive_publish_drop_reconnect() {
    let env = Env::new().await;
    let key = fresh_key();
    env.hub.put(
        key,
        "chanlogin_life",
        "botlogin_life",
        "SECRETTOKEN_life_1",
        None,
    );
    let mut mgr = env.manager("replica-life").await;

    let rep = mgr.reconcile().await;
    assert_eq!((rep.desired, rep.started), (1, 1), "{rep:?}");
    env.joined(0).await;
    let conns = env.server.connections();
    assert_eq!(conns.len(), 1, "connections observed");
    assert_eq!(conns[0].token.as_deref(), Some("SECRETTOKEN_life_1"));
    assert_eq!(conns[0].nick.as_deref(), Some("botlogin_life"));
    assert_eq!(conns[0].joined, "#chanlogin_life");

    env.server.send_privmsg(0, "viewer1", "hello");
    env.events_settle_at(1).await;
    assert_eq!(env.appender.count_for("chanlogin_life"), 1);

    env.server.drop_conn(0);
    eventually("reconnect (2nd server connection)", || {
        env.server.connections().len() == 2
    })
    .await;
    env.joined(1).await;
    assert_eq!(
        env.hub.resolved_tokens(key).len(),
        2,
        "a 2nd resolve() must happen on reconnect"
    );
    assert!(!env.server.connections()[0].live);
    env.server.send_privmsg(1, "viewer1", "again");
    env.events_settle_at(2).await;
}

#[tokio::test]
async fn reconcile_adds_session_for_new_binding_and_server_sees_the_join() {
    let env = Env::new().await;
    let mut mgr = env.manager("replica-add").await;

    let rep = mgr.reconcile().await;
    assert_eq!((rep.desired, rep.started), (0, 0), "{rep:?}");
    assert!(!rep.discovery_failed);
    assert_eq!(env.server.connections().len(), 0, "no binding, no socket");

    let key = fresh_key();
    env.hub.put(
        key,
        "chanlogin_add",
        "botlogin_add",
        "SECRETTOKEN_add_1",
        None,
    );
    let rep = mgr.reconcile().await;
    assert_eq!((rep.desired, rep.started), (1, 1), "{rep:?}");
    env.joined(0).await;
    let conns = env.server.connections();
    assert_eq!(conns.len(), 1, "connections observed");
    assert_eq!(conns[0].joined, "#chanlogin_add");
    assert_eq!(mgr.live_session_count(), 1);
    env.server.send_privmsg(0, "viewer1", "hi");
    env.events_settle_at(1).await;
    assert_eq!(env.appender.count_for("chanlogin_add"), 1);
}

#[tokio::test]
async fn reconcile_removing_binding_closes_the_socket() {
    let env = Env::new().await;
    let key = fresh_key();
    env.hub
        .put(key, "chanlogin_rm", "botlogin_rm", "SECRETTOKEN_rm_1", None);
    let mut mgr = env.manager("replica-rm").await;
    mgr.reconcile().await;
    env.joined(0).await;
    assert_eq!(env.server.live_conn_count(), 1, "1 live before the removal");

    env.hub.remove(key);
    let rep = mgr.reconcile().await;
    assert_eq!(rep.stopped, 1, "{rep:?}");
    eventually("socket closed after removal", || {
        env.server.live_conn_count() == 0
    })
    .await;
    assert_eq!(env.server.connections().len(), 1, "no reconnect after stop");
    assert_eq!(mgr.live_session_count(), 0);
}

#[tokio::test]
async fn two_communities_two_independent_sessions_with_their_own_tokens() {
    let env = Env::new().await;
    let (ka, kb) = (fresh_key(), fresh_key());
    env.hub
        .put(ka, "chanlogin_a", "botlogin_a", "SECRETTOKEN_a", None);
    env.hub
        .put(kb, "chanlogin_b", "botlogin_b", "SECRETTOKEN_b", None);
    env.server.map_token("SECRETTOKEN_a", ka.0);
    env.server.map_token("SECRETTOKEN_b", kb.0);
    let mut mgr = env.manager("replica-two").await;

    let rep = mgr.reconcile().await;
    assert_eq!((rep.desired, rep.started), (2, 2), "{rep:?}");
    eventually("two connections", || env.server.connections().len() == 2).await;
    eventually("both joined", || {
        env.server
            .connections()
            .iter()
            .all(|c| c.registered && !c.joined.is_empty())
    })
    .await;

    let conns = env.server.connections();
    let idx_a = conns
        .iter()
        .position(|c| c.community == Some(ka.0))
        .expect("conn with A's token");
    let idx_b = conns
        .iter()
        .position(|c| c.community == Some(kb.0))
        .expect("conn with B's token");
    assert_ne!(idx_a, idx_b);
    assert_eq!(
        conns[idx_a].joined, "#chanlogin_a",
        "A joins only its channel"
    );
    assert_eq!(
        conns[idx_b].joined, "#chanlogin_b",
        "B joins only its channel"
    );

    env.server.send_privmsg(idx_a, "viewer1", "to a");
    env.events_settle_at(1).await;
    assert_eq!(env.appender.count_for("chanlogin_a"), 1);
    assert_eq!(
        env.appender.count_for("chanlogin_b"),
        0,
        "a message to A is never published under B's source"
    );
    env.server.send_privmsg(idx_b, "viewer2", "to b");
    env.events_settle_at(2).await;
    assert_eq!(env.appender.count_for("chanlogin_a"), 1);
    assert_eq!(env.appender.count_for("chanlogin_b"), 1);
}

#[tokio::test]
async fn hub_outage_mid_run_keeps_sockets_open_and_recovers() {
    let env = Env::new().await;
    let key = fresh_key();
    env.hub.put(
        key,
        "chanlogin_out",
        "botlogin_out",
        "SECRETTOKEN_out",
        None,
    );
    let mut mgr = env.manager("replica-out").await;
    mgr.reconcile().await;
    env.joined(0).await;
    assert_eq!(env.server.live_conn_count(), 1);

    env.hub.list_fail_ticks.store(3, Ordering::SeqCst);
    env.hub.resolve_down.store(true, Ordering::SeqCst);
    for tick in 1..=3 {
        let rep = mgr.reconcile().await;
        assert!(rep.discovery_failed, "tick {tick}: {rep:?}");
        assert_eq!(
            (rep.kept, rep.stopped, rep.started),
            (1, 0, 0),
            "tick {tick}"
        );
        assert_eq!(env.server.live_conn_count(), 1, "tick {tick}: socket stays");
        assert_eq!(mgr.live_session_count(), 1);
    }
    assert_eq!(
        env.metrics
            .resolve_failed
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .filter(|c| **c == "transient")
            .count(),
        3,
        "one transient discovery failure per outage tick"
    );
    env.server.send_privmsg(0, "viewer1", "during outage");
    env.events_settle_at(1).await;

    // Mid-outage the socket dies: the session must retry resolve() (which
    // fails Transient) and stay alive in the manager, not be torn down.
    env.server.drop_conn(0);
    eventually("socket gone", || env.server.live_conn_count() == 0).await;
    eventually("session retried resolve() and it failed", || {
        env.hub.resolve_failures.load(Ordering::SeqCst) >= 1
    })
    .await;
    env.hub.list_fail_ticks.store(1, Ordering::SeqCst);
    let rep = mgr.reconcile().await;
    assert!(rep.discovery_failed, "{rep:?}");
    assert_eq!((rep.kept, rep.stopped, rep.started), (1, 0, 0), "{rep:?}");
    assert_eq!(
        mgr.live_session_count(),
        1,
        "not torn down by resolve Transient"
    );
    assert_eq!(env.server.live_conn_count(), 0);
    assert_eq!(
        env.server.connections().len(),
        1,
        "no connect while hub down"
    );

    env.hub.resolve_down.store(false, Ordering::SeqCst);
    let rep = mgr.reconcile().await;
    assert!(!rep.discovery_failed, "{rep:?}");
    assert_eq!((rep.kept, rep.started, rep.restarted), (1, 0, 0), "{rep:?}");
    eventually("session reconnects after recovery", || {
        env.server.connections().len() == 2
    })
    .await;
    env.joined(1).await;
    assert_eq!(
        env.hub.resolved_tokens(key).len(),
        2,
        "initial + post-recovery"
    );
    assert!(env
        .metrics
        .ended
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .is_empty());
    env.server.send_privmsg(1, "viewer1", "after recovery");
    env.events_settle_at(2).await;
}

#[tokio::test]
async fn revoked_token_session_ends_then_self_heals_after_cooldown() {
    let env = Env::new().await;
    let key = fresh_key();
    env.hub.put(
        key,
        "chanlogin_rev",
        "botlogin_rev",
        "SECRETTOKEN_rev_old",
        None,
    );
    env.server.reject_token("SECRETTOKEN_rev_old");
    let mut mgr = env.manager("replica-rev").await;

    let rep = mgr.reconcile().await;
    assert_eq!(rep.started, 1, "{rep:?}");
    eventually("server saw + rejected the login", || {
        let c = env.server.connections();
        c.len() == 1 && !c[0].live
    })
    .await;
    assert!(!env.server.connections()[0].registered);
    eventually("session ended on its own", || mgr.live_session_count() == 0).await;

    // Inside the cooldown: reaped, but NOT restarted, no new resolve/socket.
    let rep = mgr.reconcile().await;
    assert_eq!((rep.started, rep.restarted), (0, 0), "{rep:?}");
    assert_eq!(env.server.connections().len(), 1);
    assert_eq!(env.hub.resolved_tokens(key).len(), 1);
    assert_eq!(
        *env.metrics
            .ended
            .lock()
            .unwrap_or_else(PoisonError::into_inner),
        vec![SessionEnd::NonRetryable]
    );

    // Hub swaps in a fresh token; after the cooldown the session heals.
    env.hub.set_token(key, "SECRETTOKEN_rev_new", None);
    jump_clock(Duration::from_secs(121)).await;
    let rep = mgr.reconcile().await;
    assert_eq!(rep.restarted, 1, "{rep:?}");
    env.joined(1).await;
    let conns = env.server.connections();
    assert_eq!(conns.len(), 2, "connections observed");
    assert_eq!(conns[1].token.as_deref(), Some("SECRETTOKEN_rev_new"));
    assert!(conns[1].registered && conns[1].live);
}

#[tokio::test]
async fn two_managers_one_server_one_connection() {
    let env = Env::new().await;
    let key = fresh_key();
    env.hub.put(
        key,
        "chanlogin_lease",
        "botlogin_lease",
        "SECRETTOKEN_lease",
        None,
    );
    let mut m1 = env.manager("replica-1").await;
    let mut m2 = env.manager("replica-2").await;

    let r1 = m1.reconcile().await;
    let r2 = m2.reconcile().await;
    assert_eq!(r1.started, 1, "{r1:?}");
    assert_eq!((r2.started, r2.lease_denied), (0, 1), "{r2:?}");
    env.joined(0).await;
    let r2b = m2.reconcile().await;
    assert_eq!((r2b.started, r2b.lease_denied), (0, 1), "{r2b:?}");
    // Give a (wrongly started) second session ample time to connect.
    stays("server connections across both managers", 1, || {
        env.server.connections().len()
    })
    .await;
    assert_eq!(m1.live_session_count() + m2.live_session_count(), 1);

    env.server.send_privmsg(0, "viewer1", "once");
    env.events_settle_at(1).await;
}

#[tokio::test]
async fn token_never_appears_in_captured_tracing_output() {
    let env = Env::new().await;
    let key = fresh_key();
    env.hub.put(
        key,
        "chanlogin_trace",
        "botlogin_trace",
        "SECRETTOKEN_trace",
        None,
    );
    let mut mgr = env.manager("replica-trace").await;

    mgr.reconcile().await;
    env.joined(0).await;
    env.server.send_privmsg(0, "viewer1", "hello");
    env.events_settle_at(1).await;
    env.server.drop_conn(0);
    eventually("reconnect", || env.server.connections().len() == 2).await;
    env.joined(1).await;
    env.hub.remove(key);
    mgr.reconcile().await;
    eventually("closed", || env.server.live_conn_count() == 0).await;

    let captured = CAPTURED
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .clone();
    assert!(
        captured
            .iter()
            .any(|l| l.contains("twitch session started")),
        "capture must have seen the lifecycle ({} lines)",
        captured.len()
    );
    assert!(
        captured
            .iter()
            .any(|l| l.contains("span:twitch.session.connect")),
        "capture must have seen the connect span"
    );
    for line in &captured {
        for marker in SECRET_MARKERS {
            assert!(
                !line.contains(marker),
                "secret marker {marker:?} leaked into tracing output: {line}"
            );
        }
    }
}

#[tokio::test]
async fn session_recycle_floor_applies_to_near_expiry_token() {
    let env = Env::new().await;
    let key = fresh_key();
    let now = i64::try_from(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock")
            .as_secs(),
    )
    .expect("epoch fits i64");
    env.hub.put(
        key,
        "chanlogin_cyc",
        "botlogin_cyc",
        "SECRETTOKEN_cyc_1",
        Some(now + 301),
    );
    let mut mgr = env.manager("replica-cyc").await;
    mgr.reconcile().await;
    env.joined(0).await;

    // The hub refreshes the token; the session lifetime is MIN_SESSION (30s).
    env.hub.set_token(key, "SECRETTOKEN_cyc_2", None);
    jump_clock(Duration::from_secs(31)).await;

    eventually("recycle reconnect", || env.server.connections().len() == 2).await;
    env.joined(1).await;
    let conns = env.server.connections();
    assert_eq!(conns[1].token.as_deref(), Some("SECRETTOKEN_cyc_2"));
    eventually("old socket closed", || !env.server.connections()[0].live).await;
    assert_eq!(
        env.hub.resolved_tokens(key),
        vec![
            "SECRETTOKEN_cyc_1".to_string(),
            "SECRETTOKEN_cyc_2".to_string()
        ]
    );
    assert_eq!(mgr.live_session_count(), 1, "session survived the recycle");

    // The recycled channel is the live one: a message on it is published once.
    env.server.send_privmsg(1, "viewer1", "post-recycle");
    env.events_settle_at(1).await;
}

#[tokio::test]
async fn session_recycles_before_token_expiry_with_fresh_resolve() {
    let env = Env::new().await;
    let key = fresh_key();
    let now = i64::try_from(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock")
            .as_secs(),
    )
    .expect("epoch fits i64");
    // expiry = now+400s -> recycle_at = 400 - RECYCLE_MARGIN(300) = ~100s,
    // well above the 30s MIN_SESSION floor: only expires_at can drive it.
    env.hub.put(
        key,
        "chanlogin_exp",
        "botlogin_exp",
        "SECRETTOKEN_exp_1",
        Some(now + 400),
    );
    let mut mgr = env.manager("replica-exp").await;
    mgr.reconcile().await;
    env.joined(0).await;
    env.hub.set_token(key, "SECRETTOKEN_exp_2", None);

    // Past the floor but before recycle_at: the session must NOT recycle.
    jump_clock(Duration::from_secs(40)).await;
    stays("server connections before recycle_at", 1, || {
        env.server.connections().len()
    })
    .await;
    assert!(
        env.server.connections()[0].live,
        "still on the original socket"
    );

    // Past recycle_at (~100s): reconnect with the refreshed token.
    jump_clock(Duration::from_secs(65)).await;
    eventually("expiry-driven recycle reconnect", || {
        env.server.connections().len() == 2
    })
    .await;
    env.joined(1).await;
    assert_eq!(
        env.server.connections()[1].token.as_deref(),
        Some("SECRETTOKEN_exp_2")
    );
    eventually("old socket closed", || !env.server.connections()[0].live).await;
    assert_eq!(
        env.hub.resolved_tokens(key),
        vec![
            "SECRETTOKEN_exp_1".to_string(),
            "SECRETTOKEN_exp_2".to_string()
        ]
    );
    assert_eq!(mgr.live_session_count(), 1);
    env.server.send_privmsg(1, "viewer1", "post-recycle");
    env.events_settle_at(1).await;
}
