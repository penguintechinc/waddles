//! Live end-to-end for per-community Twitch sessions: the REAL `SessionManager` + `RealRunner`
//! and REAL `ConnectionResolver` against a REAL hub-api over a REAL Postgres, a REAL Valkey lease,
//! and a real-socket IRC test server. Only Twitch's own HTTP API is faked (inside the hub-api
//! harness); the chart-rendered machine-JWT identities, the admin API, the vault and the internal
//! list/resolve routes are the production code.
//!
//! `#[ignore]` by default -- run via `scripts/ci/twitch-sessions-live.sh`
//! (`make test-svc-ingest-twitch-live`), which starts the harness
//! (`hub_api/tests/live/live_hub.py`) and Valkey and exports `LIVE_HUB_MANIFEST` + `VALKEY_URL`.
//! A missing manifest/Valkey is a hard failure, never a skip.
//!
//! Each test prints `LIVE_TELEMETRY test=<name> logs=N metrics=N histograms=N sessions_live_series=N`
//! (tracing events, Prometheus series with samples, histograms with observations, and whether
//! `svc_ingest_twitch_sessions_live` was exported); the script sums them and fails on zero.

#[path = "support/irc_server.rs"]
mod irc_server;

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use connection_resolver::{
    ConnectionKind, ConnectionResolver, ResolverConfig, ResolverMetrics as ConnResolverMetrics,
};
use irc_server::IrcTestServer;
use penguin_spine::{KeyRing, Scope, SpineError, StageEnvelope};
use serde_json::{json, Value};
use svc_ingest::publish::EventAppender;
use svc_ingest::telemetry::{register_ingest_metrics, IngestMetrics};
use svc_ingest::twitch_sessions::{
    RealRunner, SessionDeps, SessionManager, SessionMetrics, SessionRunner, ValkeyLease,
};
use tokio::sync::oneshot;
use tracing::span::{Attributes, Id, Record};
use tracing::{Event, Metadata, Subscriber};

/// Reconcile cadence for these tests (the manager accepts any interval; the 10s config floor
/// applies only to the env-driven production path).
const INTERVAL: Duration = Duration::from_secs(2);
/// Slack on top of one interval (+10% jitter) for network + IRC handshake on a busy CI box.
const SLACK: Duration = Duration::from_secs(6);

static LOGS: AtomicU64 = AtomicU64::new(0);
static SPAN_IDS: AtomicU64 = AtomicU64::new(1);

/// Process-global subscriber counting tracing events (log records).
struct CountingSubscriber;
impl Subscriber for CountingSubscriber {
    fn enabled(&self, _: &Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, _: &Attributes<'_>) -> Id {
        Id::from_u64(SPAN_IDS.fetch_add(1, Ordering::Relaxed))
    }
    fn record(&self, _: &Id, _: &Record<'_>) {}
    fn record_follows_from(&self, _: &Id, _: &Id) {}
    fn event(&self, _: &Event<'_>) {
        LOGS.fetch_add(1, Ordering::SeqCst);
    }
    fn enter(&self, _: &Id) {}
    fn exit(&self, _: &Id) {}
}

fn init_logs() {
    static ONCE: OnceLock<()> = OnceLock::new();
    ONCE.get_or_init(|| {
        tracing::subscriber::set_global_default(CountingSubscriber).expect("install subscriber");
    });
}

/// The connection resolver's own metrics seam is not under test here.
struct NoopResolverMetrics;
impl ConnResolverMetrics for NoopResolverMetrics {
    fn resolve(&self, _: ConnectionKind, _: &'static str, _: Duration) {}
    fn resolve_failed(&self, _: &'static str, _: Duration) {}
    fn list(&self, _: &'static str) {}
}

/// Discards appended events: these tests assert the IRC-side session lifecycle, not publishing.
struct NullAppender;
impl EventAppender for NullAppender {
    async fn append(&self, _: &str, _: &StageEnvelope) -> Result<String, SpineError> {
        Ok("1-0".to_string())
    }
}

/// Everything one test needs: manifest data, IRC server, metrics registry, HTTP client.
struct Live {
    hub_url: String,
    admin_token: String,
    community_a: i64,
    sa_path: PathBuf,
    bot_tokens: Vec<(String, String)>,
    channels: Vec<String>,
    server: IrcTestServer,
    registry: prometheus::Registry,
    metrics: Arc<IngestMetrics>,
    http: reqwest::Client,
}

impl Live {
    /// Loads the manifest and writes svc-ingest's ServiceAccount token file.
    async fn load(test: &str) -> Self {
        init_logs();
        let path = std::env::var("LIVE_HUB_MANIFEST")
            .expect("LIVE_HUB_MANIFEST not set -- run via scripts/ci/twitch-sessions-live.sh");
        let m: Value = serde_json::from_str(&std::fs::read_to_string(path).expect("manifest"))
            .expect("manifest json");
        let sa_token = m["sa_tokens"]
            .as_object()
            .expect("sa_tokens")
            .iter()
            .find(|(_, v)| v.as_str() == Some("svc-ingest"))
            .map(|(k, _)| k.clone())
            .expect("svc-ingest SA token");
        let sa_path =
            std::env::temp_dir().join(format!("twitch-live-sa-{}-{test}", std::process::id()));
        std::fs::write(&sa_path, sa_token).expect("write SA token");
        let bot_tokens = m["twitch_bot_tokens"]
            .as_array()
            .expect("twitch_bot_tokens")
            .iter()
            .map(|t| {
                (
                    t["access_token"].as_str().expect("access").to_string(),
                    t["refresh_token"].as_str().expect("refresh").to_string(),
                )
            })
            .collect();
        let channels = m["twitch_channels"]
            .as_array()
            .expect("twitch_channels")
            .iter()
            .map(|c| c.as_str().expect("channel").to_string())
            .collect();
        let registry = prometheus::Registry::new();
        let metrics = Arc::new(register_ingest_metrics(&registry));
        Self {
            hub_url: m["hub_url"].as_str().expect("hub_url").to_string(),
            admin_token: m["admin_token"].as_str().expect("admin_token").to_string(),
            community_a: m["community_a"].as_i64().expect("community_a"),
            sa_path,
            bot_tokens,
            channels,
            server: IrcTestServer::start().await,
            registry,
            metrics,
            http: reqwest::Client::new(),
        }
    }

    /// Creates a Twitch binding through the REAL admin API; returns the connection id.
    async fn create_binding(&self, which: usize) -> i64 {
        let (access, refresh) = &self.bot_tokens[which];
        let resp = self
            .http
            .post(format!(
                "{}/api/v1/communities/{}/provider-connections",
                self.hub_url, self.community_a
            ))
            .bearer_auth(&self.admin_token)
            .json(&json!({
                "provider": "twitch",
                "credentials": {"access_token": access, "refresh_token": refresh},
                "metadata": {"channel_login": self.channels[which]},
            }))
            .send()
            .await
            .expect("create request");
        let status = resp.status().as_u16();
        let body: Value = resp.json().await.expect("create body");
        assert_eq!(status, 201, "admin create twitch binding failed: {body}");
        body["connection"]["id"].as_i64().expect("connection id")
    }

    /// Deletes a binding through the REAL admin API.
    async fn delete_binding(&self, connection: i64) {
        let resp = self
            .http
            .delete(format!(
                "{}/api/v1/communities/{}/provider-connections/{connection}",
                self.hub_url, self.community_a
            ))
            .bearer_auth(&self.admin_token)
            .send()
            .await
            .expect("delete request");
        assert!(
            resp.status().is_success(),
            "admin delete failed: {}",
            resp.status()
        );
    }

    /// Spawns the REAL manager loop (real resolver, real Valkey lease, real IRC client).
    async fn start_manager(
        &self,
        replica: &str,
    ) -> (oneshot::Sender<()>, tokio::task::JoinHandle<()>) {
        let valkey = std::env::var("VALKEY_URL")
            .expect("VALKEY_URL must be set: these tests need a REAL Valkey (no silent skip)");
        let client = redis::Client::open(valkey).expect("valkey client");
        let cfg =
            redis::AsyncConnectionConfig::new().set_response_timeout(Some(Duration::from_secs(2)));
        let conn = client
            .get_multiplexed_async_connection_with_config(&cfg)
            .await
            .expect("real valkey must be reachable");
        let resolver = Arc::new(
            ConnectionResolver::new(
                ResolverConfig::new(
                    self.hub_url.clone(),
                    "alpha",
                    format!("{}/internal/service-token", self.hub_url),
                    self.sa_path.to_string_lossy().to_string(),
                ),
                Arc::new(NoopResolverMetrics),
            )
            .expect("resolver"),
        );
        let session_metrics: Arc<dyn SessionMetrics> = self.metrics.clone();
        let runner: Arc<dyn SessionRunner> = Arc::new(
            RealRunner::new(
                resolver.clone(),
                Arc::new(NullAppender),
                self.metrics.clone(),
                Arc::new(KeyRing::new(vec![("k1".to_string(), vec![9u8; 32])])),
                "k1".to_string(),
                Arc::new(Scope::new("acme", None)),
                "127.0.0.1".to_string(),
                self.server.port(),
                false,
            )
            .with_session_metrics(session_metrics.clone())
            .with_resolve_timeout(Duration::from_secs(10)),
        );
        let manager = SessionManager::new(
            resolver,
            Arc::new(ValkeyLease::with_replica_id(conn, replica.to_string())),
            SessionDeps {
                runner,
                metrics: session_metrics,
            },
        )
        .with_timing(INTERVAL, Duration::from_secs(10));
        let (tx, rx) = oneshot::channel();
        (tx, tokio::spawn(manager.run(rx)))
    }

    /// Prints this test's telemetry counts and fails if any signal was silent.
    fn report(&self, test: &str) {
        let families = self.registry.gather();
        let mut metrics = 0u64;
        let mut histograms = 0u64;
        let mut live_series = 0u64;
        for f in &families {
            for m in f.get_metric() {
                let has_sample = match f.get_field_type() {
                    prometheus::proto::MetricType::HISTOGRAM => {
                        let n = m.get_histogram().get_sample_count();
                        if n > 0 {
                            histograms += 1;
                        }
                        n > 0
                    }
                    _ => true,
                };
                if has_sample {
                    metrics += 1;
                }
            }
            if f.name() == "svc_ingest_twitch_sessions_live" {
                live_series += f.get_metric().len() as u64;
            }
        }
        let logs = LOGS.load(Ordering::SeqCst);
        println!(
            "LIVE_TELEMETRY test={test} logs={logs} metrics={metrics} histograms={histograms} sessions_live_series={live_series}"
        );
        assert!(logs >= 1, "no log records in {test}");
        assert!(metrics >= 1, "no metric data points in {test}");
        assert!(histograms >= 1, "no histogram observations in {test}");
        assert!(
            live_series >= 1,
            "svc_ingest_twitch_sessions_live not exported in {test}"
        );
    }
}

/// Polls `f` until true or `limit` elapses; returns the elapsed time on success.
async fn within(what: &str, limit: Duration, mut f: impl FnMut() -> bool) -> Duration {
    let began = Instant::now();
    while began.elapsed() < limit {
        if f() {
            return began.elapsed();
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("timed out after {limit:?} waiting for: {what}");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs the live harness (scripts/ci/twitch-sessions-live.sh)"]
async fn live_hub_binding_created_via_api_starts_a_session_within_one_interval() {
    let live = Live::load("create").await;
    let (stop, task) = live.start_manager("live-replica-create").await;

    // The first reconcile runs immediately: with no binding yet, no socket may exist.
    tokio::time::sleep(Duration::from_millis(500)).await;
    let before = live.server.connections().len();
    assert_eq!(before, 0, "no binding, no IRC connection (saw {before})");

    let conn = live.create_binding(0).await;
    let limit = INTERVAL + INTERVAL / 10 + SLACK;
    let elapsed = within("IRC server saw registration + JOIN", limit, || {
        live.server
            .connections()
            .iter()
            .any(|c| c.registered && !c.joined.is_empty())
    })
    .await;
    let conns = live.server.connections();
    println!(
        "LIVE_CREATE connection_id={conn} irc_connections_examined={} join_after_ms={}",
        conns.len(),
        elapsed.as_millis()
    );
    assert_eq!(conns.len(), 1, "exactly one session for one binding");
    assert_eq!(conns[0].joined, format!("#{}", live.channels[0]));
    assert_eq!(
        conns[0].nick.as_deref(),
        Some("waddlebots"),
        "bot login from the binding"
    );
    assert_eq!(
        conns[0].token.as_deref(),
        Some(live.bot_tokens[0].0.as_str()),
        "token resolved from the real hub"
    );
    live.report("create");
    // The hub DB is shared by every test in this run: remove this binding so the next test
    // starts from an empty desired set.
    live.delete_binding(conn).await;
    stop.send(()).expect("stop manager");
    task.await.expect("manager task");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs the live harness (scripts/ci/twitch-sessions-live.sh)"]
async fn live_hub_binding_deleted_via_api_stops_the_session() {
    let live = Live::load("delete").await;
    let conn = live.create_binding(1).await;
    let (stop, task) = live.start_manager("live-replica-delete").await;
    let limit = INTERVAL + INTERVAL / 10 + SLACK;
    within("session joined", limit, || {
        live.server
            .connections()
            .iter()
            .any(|c| c.registered && !c.joined.is_empty())
    })
    .await;
    assert_eq!(live.server.live_conn_count(), 1);

    live.delete_binding(conn).await;
    let elapsed = within("socket closed after API delete", limit, || {
        live.server.live_conn_count() == 0
    })
    .await;
    let total = live.server.connections().len();
    println!(
        "LIVE_DELETE connection_id={conn} irc_connections_examined={total} stop_after_ms={}",
        elapsed.as_millis()
    );
    assert_eq!(total, 1, "no reconnect after the binding was deleted");
    live.report("delete");
    stop.send(()).expect("stop manager");
    task.await.expect("manager task");
}
