//! Supervisor tests: real driver + real `WsTransport` against loopback
//! servers, plus deterministic `Core` unit tests for the diff/end branches.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use tokio::task::JoinHandle;

use super::*;
use crate::connector::webhook::{
    BoxFut as WhBoxFut, DedupToken, SecretError, WebhookDedup, WebhookIngress, WebhookSecretSource,
};
use crate::connector::{
    register_connector_metrics, ConnectionCtx, ConnectorEvent, ConnectorInvokeError, Handshake,
    PinnedConnector, RawFrame, SinkError,
};
use crate::connector_runtime::feed::FeedError;
use crate::connector_runtime::metrics::RuntimeMetrics;
use crate::connector_runtime::ready::Readiness;
use crate::connector_runtime::tenant::{TenantError, TenantResolve};
use crate::connector_runtime::testkit::{
    capture, desired_webhook, desired_ws, BlackholeServer, WsServer,
};
use crate::twitch_sessions::lease::LeaseError;

const PLATFORM: &str = "twitch";

// ---- fakes ---------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Ws,
    BadWs,
    Webhook,
}

#[derive(Clone)]
struct Spec {
    community: i64,
    conn: i64,
    port: u16,
    source: String,
    fp: u64,
    kind: Kind,
}

impl Spec {
    fn ws(community: i64, conn: i64, port: u16) -> Self {
        Self {
            community,
            conn,
            port,
            source: format!("source-{community}"),
            fp: 1,
            kind: Kind::Ws,
        }
    }
}

struct FeedState {
    resp: Result<Vec<Spec>, ()>,
    calls: usize,
    hang: bool,
}

struct FeedCtl(Mutex<FeedState>);

impl FeedCtl {
    fn new(specs: Vec<Spec>) -> Arc<Self> {
        Arc::new(Self(Mutex::new(FeedState {
            resp: Ok(specs),
            calls: 0,
            hang: false,
        })))
    }
    fn set(&self, specs: Vec<Spec>) {
        self.0.lock().expect("lock").resp = Ok(specs);
    }
    fn fail(&self) {
        self.0.lock().expect("lock").resp = Err(());
    }
    fn hang(&self, on: bool) {
        self.0.lock().expect("lock").hang = on;
    }
    fn calls(&self) -> usize {
        self.0.lock().expect("lock").calls
    }
}

struct FakeFeed(Arc<FeedCtl>);

impl BindingFeed for FakeFeed {
    async fn desired(&self, p: &'static str) -> Result<Vec<DesiredConnection>, FeedError> {
        let (resp, hang) = {
            let mut g = self.0 .0.lock().expect("lock");
            g.calls += 1;
            (g.resp.clone(), g.hang)
        };
        if hang {
            std::future::pending::<()>().await;
        }
        let specs = resp.map_err(|()| FeedError::Timeout)?;
        Ok(specs
            .iter()
            .map(|s| match s.kind {
                Kind::Ws => desired_ws(p, s.community, s.conn, s.port, &s.source, s.fp),
                Kind::BadWs => {
                    let mut d = desired_ws(p, s.community, s.conn, s.port, &s.source, s.fp);
                    if let TransportPlan::Ws(c) = &mut d.plan {
                        c.max_frame_bytes = 0;
                    }
                    d
                }
                Kind::Webhook => desired_webhook(
                    s.community,
                    s.conn,
                    &format!("route-key-{}-{}", s.community, s.conn),
                    s.fp,
                ),
            })
            .collect())
    }
}

#[derive(Default)]
struct Events {
    per_community: Mutex<HashMap<i64, usize>>,
    panic_for: Mutex<HashSet<i64>>,
    fail_build: Mutex<HashSet<i64>>,
    hang_build: AtomicBool,
    built: AtomicUsize,
    tenants_seen: Mutex<Vec<i64>>,
}

impl Events {
    fn count(&self, community: i64) -> usize {
        *self
            .per_community
            .lock()
            .expect("lock")
            .get(&community)
            .unwrap_or(&0)
    }
}

struct RecSink {
    community: i64,
    ev: Arc<Events>,
}

impl EventSink for RecSink {
    async fn publish(&self, _ctx: &ConnectionCtx, _e: ConnectorEvent) -> Result<(), SinkError> {
        if self
            .ev
            .panic_for
            .lock()
            .expect("lock")
            .contains(&self.community)
        {
            panic!("injected sink panic");
        }
        *self
            .ev
            .per_community
            .lock()
            .expect("lock")
            .entry(self.community)
            .or_default() += 1;
        Ok(())
    }
}

struct RecFactory(Arc<Events>);

impl SinkFactory<RecSink> for RecFactory {
    fn build<'a>(
        &'a self,
        d: &'a DesiredConnection,
        tenant_id: i64,
    ) -> BoxFut<'a, Result<RecSink, SinkBuildError>> {
        Box::pin(async move {
            if self.0.hang_build.load(Ordering::SeqCst) {
                std::future::pending::<()>().await;
            }
            if self
                .0
                .fail_build
                .lock()
                .expect("lock")
                .contains(&d.key.community_id)
            {
                return Err(SinkBuildError("scope unresolved"));
            }
            self.0.built.fetch_add(1, Ordering::SeqCst);
            self.0.tenants_seen.lock().expect("lock").push(tenant_id);
            Ok(RecSink {
                community: d.key.community_id,
                ev: self.0.clone(),
            })
        })
    }
}

struct FakeInvoker;

/// Every `ConnectionCtx::connection_id` an `on_connect` was invoked with.
static CONNECT_CTX_IDS: Mutex<Vec<String>> = Mutex::new(Vec::new());

impl ConnectorInvoker for FakeInvoker {
    async fn on_connect(
        &self,
        _p: &str,
        c: &ConnectionCtx,
    ) -> Result<Handshake, ConnectorInvokeError> {
        CONNECT_CTX_IDS
            .lock()
            .expect("lock")
            .push(c.connection_id.clone());
        Ok(Handshake {
            bytes: b"hi".to_vec(),
            secret_refs: vec![],
        })
    }
    async fn on_frame(
        &self,
        platform: &str,
        _f: RawFrame,
    ) -> Result<Vec<ConnectorEvent>, ConnectorInvokeError> {
        Ok(vec![ConnectorEvent {
            platform: platform.to_owned(),
            event_type: "chat.message".into(),
            actor: None,
            payload_json: "{}".into(),
            occurred_at: "2026-01-01T00:00:00.000Z".into(),
        }])
    }
    async fn on_heartbeat_due(
        &self,
        _p: &str,
        _c: &ConnectionCtx,
    ) -> Result<Option<Vec<u8>>, ConnectorInvokeError> {
        Ok(None)
    }
    async fn on_disconnect(
        &self,
        _p: &str,
        _c: &ConnectionCtx,
        _r: &str,
    ) -> Result<(), ConnectorInvokeError> {
        Ok(())
    }
}

#[derive(Default)]
struct LeaseStore {
    held: Mutex<HashMap<(i64, i64), String>>,
}

impl LeaseStore {
    fn len(&self) -> usize {
        self.held.lock().expect("lock").len()
    }
    fn steal_all(&self) {
        self.held.lock().expect("lock").clear();
    }
    fn owners(&self) -> Vec<String> {
        self.held.lock().expect("lock").values().cloned().collect()
    }
}

struct MemLease {
    store: Arc<LeaseStore>,
    owner: String,
    down: AtomicBool,
}

impl SessionLease for MemLease {
    async fn acquire(&self, key: (i64, i64), _ttl: Duration) -> Result<bool, LeaseError> {
        if self.down.load(Ordering::SeqCst) {
            return Err(LeaseError("store down".into()));
        }
        let mut g = self.store.held.lock().expect("lock");
        if g.contains_key(&key) {
            return Ok(false);
        }
        g.insert(key, self.owner.clone());
        Ok(true)
    }
    async fn renew(&self, key: (i64, i64), _ttl: Duration) -> Result<bool, LeaseError> {
        if self.down.load(Ordering::SeqCst) {
            return Err(LeaseError("store down".into()));
        }
        let g = self.store.held.lock().expect("lock");
        Ok(g.get(&key).is_some_and(|o| *o == self.owner))
    }
    async fn release(&self, key: (i64, i64)) -> Result<(), LeaseError> {
        let mut g = self.store.held.lock().expect("lock");
        if g.get(&key).is_some_and(|o| *o == self.owner) {
            g.remove(&key);
        }
        Ok(())
    }
}

struct NoAuth;
impl TransportSecretSource for NoAuth {
    fn resolve(
        &self,
        _r: connection_resolver::ConnectionRef,
        _d: crate::connector::secrets::Destination,
    ) -> WhBoxFut<'_, Result<crate::connector::secrets::ResolvedAuth, SecretError>> {
        Box::pin(async { Err(SecretError::Missing) })
    }
}

struct FakeSecrets {
    fail: AtomicBool,
}
impl SecretsFactory for FakeSecrets {
    fn for_connection(
        &self,
        _d: &DesiredConnection,
    ) -> Result<Arc<dyn TransportSecretSource>, TransportError> {
        if self.fail.load(Ordering::SeqCst) {
            return Err(TransportError::Connect("no secrets".into()));
        }
        Ok(Arc::new(NoAuth))
    }
}

/// Tenant resolver answering from a switchable mode.
struct FakeTenants {
    mode: Mutex<std::result::Result<i64, TenantError>>,
}

impl FakeTenants {
    fn ok(tenant: i64) -> Arc<Self> {
        Arc::new(Self {
            mode: Mutex::new(Ok(tenant)),
        })
    }
}

impl TenantResolve for FakeTenants {
    fn resolve<'a>(&'a self, _d: &'a DesiredConnection) -> BoxFut<'a, Result<i64, TenantError>> {
        let r = *self.mode.lock().expect("lock");
        Box::pin(async move { r })
    }
}

struct Attached(AtomicBool);
impl ExecutorStatus for Attached {
    fn attached(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }
}

struct NoSecret;
impl WebhookSecretSource for NoSecret {
    fn resolve(
        &self,
        _r: connection_resolver::ConnectionRef,
    ) -> WhBoxFut<'_, Result<connection_resolver::SecretString, SecretError>> {
        Box::pin(async { Err(SecretError::Missing) })
    }
}

struct AlwaysFirst;
impl WebhookDedup for AlwaysFirst {
    fn first_seen<'a>(&'a self, key: &'a str) -> WhBoxFut<'a, Result<Option<DedupToken>, ()>> {
        Box::pin(async move { Ok(Some(DedupToken(key.to_owned()))) })
    }
    fn release<'a>(&'a self, _k: &'a str, _t: &'a DedupToken) -> WhBoxFut<'a, Result<(), ()>> {
        Box::pin(async { Ok(()) })
    }
}

/// Real `WebhookIngress` register/bind, recording attach/detach calls.
struct IngressBinder {
    ingress: WebhookIngress,
    attached: AtomicUsize,
    detached: AtomicUsize,
    fail: AtomicBool,
}

impl IngressBinder {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            ingress: WebhookIngress::new(Arc::new(NoSecret), Arc::new(AlwaysFirst), None),
            attached: AtomicUsize::new(0),
            detached: AtomicUsize::new(0),
            fail: AtomicBool::new(false),
        })
    }
}

impl WebhookAttach for IngressBinder {
    fn attach(
        &self,
        plan: &WebhookPlan,
        _key: &ConnKey,
    ) -> Result<WebhookTransport, TransportError> {
        if self.fail.load(Ordering::SeqCst) {
            return Err(TransportError::Connect("refused".into()));
        }
        self.ingress
            .register(plan.route.clone(), plan.authorized_community)
            .map_err(|_| TransportError::Connect("register".into()))?;
        let t = self
            .ingress
            .bind_transport(&plan.route.route_key, &plan.route.owner_bundle)
            .map_err(|_| TransportError::Connect("bind".into()))?;
        self.attached.fetch_add(1, Ordering::SeqCst);
        Ok(t)
    }
    fn detach(&self, plan: &WebhookPlan) {
        let _ = self
            .ingress
            .unregister(&plan.route.route_key, plan.authorized_community);
        self.detached.fetch_add(1, Ordering::SeqCst);
    }
}

// ---- rig -----------------------------------------------------------------

fn fast_config() -> RuntimeConfig {
    let mut c = RuntimeConfig::new(vec![PLATFORM]);
    c.reconcile_interval = Duration::from_millis(40);
    c.feed_timeout = Duration::from_secs(1);
    c.restart_cooldown = Duration::from_millis(150);
    c.lease_ttl = Duration::from_secs(2);
    c.lease_renew_every = Duration::from_millis(400);
    c.stop_timeout = Duration::from_secs(2);
    c.backoff_cap = Duration::from_millis(50);
    c
}

struct Shared3 {
    feed: Arc<FeedCtl>,
    ev: Arc<Events>,
    store: Arc<LeaseStore>,
}

impl Shared3 {
    fn new(specs: Vec<Spec>) -> Self {
        Self {
            feed: FeedCtl::new(specs),
            ev: Arc::new(Events::default()),
            store: Arc::new(LeaseStore::default()),
        }
    }
}

struct Opts {
    registry_platforms: Vec<&'static str>,
    webhook: Option<Arc<dyn WebhookAttach>>,
    executor_attached: bool,
    secrets_fail: bool,
    tenants: Arc<FakeTenants>,
    activator: Option<Arc<dyn ConnectionActivator>>,
}

impl Default for Opts {
    fn default() -> Self {
        Self {
            registry_platforms: vec![PLATFORM],
            webhook: None,
            executor_attached: true,
            secrets_fail: false,
            tenants: FakeTenants::ok(1),
            activator: None,
        }
    }
}

struct Running {
    stop: Option<oneshot::Sender<()>>,
    join: Option<JoinHandle<()>>,
    status: Arc<RuntimeStatus>,
    ready: Arc<dyn RuntimeReady>,
    metrics: RuntimeMetrics,
    reg: prometheus::Registry,
    lease: Arc<MemLease>,
    executor: Arc<Attached>,
}

impl Running {
    fn counter(&self, name: &str, label: &str) -> u64 {
        match name {
            "restart" => self
                .metrics
                .restarts_total
                .with_label_values(&[PLATFORM, label])
                .get(),
            "reconcile" => self
                .metrics
                .reconcile_total
                .with_label_values(&[PLATFORM, label])
                .get(),
            "lease" => self
                .metrics
                .lease_total
                .with_label_values(&[PLATFORM, label])
                .get(),
            "parked" => self
                .metrics
                .parked_total
                .with_label_values(&[PLATFORM, label])
                .get(),
            other => panic!("unknown counter {other}"),
        }
    }

    async fn stop(&mut self) {
        if let Some(tx) = self.stop.take() {
            let _ = tx.send(());
        }
        if let Some(j) = self.join.take() {
            tokio::time::timeout(Duration::from_secs(5), j)
                .await
                .expect("runtime stops in time")
                .expect("runtime task did not panic");
        }
    }
}

fn start(sh: &Shared3, cfg: RuntimeConfig, owner: &str, opts: Opts) -> Running {
    let reg = prometheus::Registry::new();
    let connector_metrics = register_connector_metrics(&reg).expect("connector metrics");
    let metrics = RuntimeMetrics::register(&reg).expect("runtime metrics");
    let mut registry = ConnectorRegistry::new();
    for p in &opts.registry_platforms {
        registry
            .register(PinnedConnector::new(*p, "app", "sha256:x", 4096))
            .expect("register");
    }
    let lease = Arc::new(MemLease {
        store: sh.store.clone(),
        owner: owner.to_owned(),
        down: AtomicBool::new(false),
    });
    let executor = Arc::new(Attached(AtomicBool::new(opts.executor_attached)));
    let rt = ConnectorRuntime::new(
        RuntimeDeps {
            gates: None,
            activator: opts.activator.clone(),
            feed: FakeFeed(sh.feed.clone()),
            invoker: Arc::new(FakeInvoker),
            registry: Arc::new(registry),
            sinks: Arc::new(RecFactory(sh.ev.clone())),
            lease: lease.clone(),
            secrets: Arc::new(FakeSecrets {
                fail: AtomicBool::new(opts.secrets_fail),
            }),
            tenants: opts.tenants.clone(),
            executor: executor.clone(),
            connector_metrics,
            metrics: metrics.clone(),
            frame_metrics: None,
            webhook: opts.webhook,
        },
        cfg,
    )
    .expect("runtime");
    let status = rt.status_handle();
    let ready = rt.ready_handle();
    let (tx, rx) = oneshot::channel();
    // `tokio::spawn` proves the `run` future is `Send`.
    let join = tokio::spawn(rt.run(rx));
    Running {
        stop: Some(tx),
        join: Some(join),
        status,
        ready,
        metrics,
        reg,
        lease,
        executor,
    }
}

async fn eventually(what: &str, cond: impl Fn() -> bool) {
    for _ in 0..500 {
        if cond() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("timed out waiting for: {what}");
}

async fn settle(ms: u64) {
    tokio::time::sleep(Duration::from_millis(ms)).await;
}

// ---- end-to-end through the real driver and transport --------------------

#[tokio::test]
async fn starts_publishes_per_community_and_reports_ready() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port), Spec::ws(2, 20, srv.port)]);
    let mut r = start(&sh, fast_config(), "a", Opts::default());
    eventually("both communities publish", || {
        sh.ev.count(1) > 2 && sh.ev.count(2) > 2
    })
    .await;
    assert_eq!(srv.accepted(), 2);
    assert_eq!(sh.store.len(), 2, "one lease per connection");
    eventually("live == desired == 2", || {
        r.ready.ready(PLATFORM)
            == Readiness::Ready {
                live: 2,
                desired: 2,
            }
    })
    .await;
    assert_eq!(r.metrics.desired.with_label_values(&[PLATFORM]).get(), 2);
    assert_eq!(r.metrics.live.with_label_values(&[PLATFORM]).get(), 2);
    assert_eq!(r.counter("lease", "acquired"), 2);
    let snap = r.status.snapshot(PLATFORM).expect("snapshot");
    assert!(snap.executor_attached && snap.pinned_ok && !snap.discovery_failed);
    r.stop().await;
    assert_eq!(sh.store.len(), 0, "shutdown releases every lease");
    assert_eq!(r.metrics.live.with_label_values(&[PLATFORM]).get(), 0);
}

#[tokio::test]
async fn a_panicking_community_is_isolated_and_restarted_after_the_cooldown() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port), Spec::ws(2, 20, srv.port)]);
    let mut r = start(&sh, fast_config(), "a", Opts::default());
    eventually("both flow", || sh.ev.count(1) > 2 && sh.ev.count(2) > 2).await;

    sh.ev.panic_for.lock().expect("lock").insert(2);
    let c1_before = sh.ev.count(1);
    let c2_before = sh.ev.count(2);
    settle(300).await;
    assert!(
        sh.ev.count(1) > c1_before + 3,
        "the healthy community keeps running"
    );
    assert_eq!(
        sh.ev.count(2),
        c2_before,
        "the panicking one publishes nothing"
    );

    sh.ev.panic_for.lock().expect("lock").clear();
    eventually("panicked community restarted and flows again", || {
        sh.ev.count(2) > c2_before + 2
    })
    .await;
    assert!(r.counter("restart", "panic") >= 1);
    r.stop().await;
}

#[tokio::test]
async fn reconcile_adds_removes_and_restarts_on_a_fingerprint_change() {
    let a = WsServer::start().await;
    let b = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, a.port)]);
    let mut r = start(&sh, fast_config(), "a", Opts::default());
    eventually("c1 connected", || a.accepted() == 1).await;

    // Add c2.
    sh.feed
        .set(vec![Spec::ws(1, 10, a.port), Spec::ws(2, 20, a.port)]);
    eventually("c2 flows", || sh.ev.count(2) > 2).await;
    assert_eq!(a.accepted(), 2, "adding c2 must not restart c1");

    // Remove c1.
    sh.feed.set(vec![Spec::ws(2, 20, a.port)]);
    eventually("c1 lease released", || sh.store.len() == 1).await;
    let c1 = sh.ev.count(1);
    settle(150).await;
    assert_eq!(sh.ev.count(1), c1, "a removed connection stops publishing");
    assert_eq!(r.metrics.desired.with_label_values(&[PLATFORM]).get(), 1);

    // Change c2's fingerprint (and target).
    let mut moved = Spec::ws(2, 20, b.port);
    moved.fp = 2;
    sh.feed.set(vec![moved]);
    eventually("c2 reconnected to the new endpoint", || b.accepted() == 1).await;
    assert_eq!(r.counter("restart", "fingerprint"), 1);
    assert_eq!(
        sh.store.len(),
        1,
        "the lease is held again after the restart"
    );
    r.stop().await;
}

#[tokio::test]
async fn a_feed_error_keeps_the_current_set_and_flags_discovery_failed() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    let mut r = start(&sh, fast_config(), "a", Opts::default());
    eventually("flowing", || sh.ev.count(1) > 2).await;

    sh.feed.fail();
    eventually("two failed passes", || {
        r.counter("reconcile", "feed_error") >= 2
    })
    .await;
    let before = sh.ev.count(1);
    settle(200).await;
    assert!(sh.ev.count(1) > before + 3, "the set must keep running");
    assert_eq!(srv.accepted(), 1, "no churn on a feed error");
    assert!(r.status.snapshot(PLATFORM).expect("snap").discovery_failed);

    sh.feed.set(vec![Spec::ws(1, 10, srv.port)]);
    eventually("recovered", || {
        !r.status.snapshot(PLATFORM).expect("snap").discovery_failed
    })
    .await;
    r.stop().await;
}

#[tokio::test]
async fn a_hung_feed_times_out_and_keeps_the_set() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    let mut cfg = fast_config();
    cfg.feed_timeout = Duration::from_millis(100);
    let mut r = start(&sh, cfg, "a", Opts::default());
    eventually("flowing", || sh.ev.count(1) > 2).await;
    sh.feed.hang(true);
    eventually("timeout counted", || {
        r.counter("reconcile", "feed_timeout") >= 1
    })
    .await;
    let before = sh.ev.count(1);
    settle(200).await;
    assert!(
        sh.ev.count(1) > before,
        "connections run while the feed hangs"
    );
    assert_eq!(srv.accepted(), 1);
    r.stop().await;
}

#[tokio::test]
async fn the_connection_cap_skips_the_excess_and_counts_it() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![
        Spec::ws(1, 10, srv.port),
        Spec::ws(2, 20, srv.port),
        Spec::ws(3, 30, srv.port),
    ]);
    let mut cfg = fast_config();
    cfg.max_connections = 2;
    let mut r = start(&sh, cfg, "a", Opts::default());
    eventually("capped counted", || {
        r.metrics.capped_total.with_label_values(&[PLATFORM]).get() >= 1
    })
    .await;
    settle(200).await;
    assert_eq!(srv.accepted(), 2, "only the cap's worth connect");
    assert_eq!(sh.ev.count(3), 0, "highest key is the one skipped");
    assert_eq!(r.metrics.desired.with_label_values(&[PLATFORM]).get(), 3);
    r.stop().await;
}

#[tokio::test]
async fn start_concurrency_bounds_simultaneous_connects() {
    let hole = BlackholeServer::start().await;
    let sh = Shared3::new((1..=4).map(|i| Spec::ws(i, i * 10, hole.port)).collect());
    let mut cfg = fast_config();
    cfg.start_concurrency = 2;
    let mut r = start(&sh, cfg, "a", Opts::default());
    settle(400).await;
    assert_eq!(
        hole.accepted(),
        2,
        "only start_concurrency connects may be in flight"
    );
    // The hung connects time out (1s), freeing permits for the rest.
    eventually("remaining connections attempted", || hole.accepted() >= 4).await;
    r.stop().await;
}

#[tokio::test]
async fn start_pace_spaces_connect_attempts() {
    let srv = WsServer::start().await;
    let sh = Shared3::new((1..=3).map(|i| Spec::ws(i, i * 10, srv.port)).collect());
    let cfg = fast_config().with_start_pace(PLATFORM, Duration::from_millis(300));
    let began = std::time::Instant::now();
    let mut r = start(&sh, cfg, "a", Opts::default());
    eventually("first connected", || srv.accepted() >= 1).await;
    assert!(srv.accepted() < 3, "connects are not simultaneous");
    eventually("all connected", || srv.accepted() == 3).await;
    assert!(
        began.elapsed() >= Duration::from_millis(550),
        "three connects at a 300ms gap take at least ~600ms, took {:?}",
        began.elapsed()
    );
    r.stop().await;
}

#[tokio::test]
async fn shutdown_while_a_connection_is_still_preparing_is_prompt() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    sh.ev.hang_build.store(true, Ordering::SeqCst);
    let mut r = start(&sh, fast_config(), "a", Opts::default());
    eventually("lease taken while the sink build hangs", || {
        sh.store.len() == 1
    })
    .await;
    let began = std::time::Instant::now();
    r.stop().await;
    assert!(began.elapsed() < Duration::from_secs(1));
    assert_eq!(sh.store.len(), 0, "the lease is released");
    assert_eq!(srv.accepted(), 0);
}

#[tokio::test]
async fn two_runtimes_never_run_the_same_connection() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    let mut a = start(&sh, fast_config(), "replica-a", Opts::default());
    let mut b = start(&sh, fast_config(), "replica-b", Opts::default());
    eventually("one connection up", || srv.accepted() == 1).await;
    settle(400).await;
    assert_eq!(
        srv.accepted(),
        1,
        "exactly one replica holds the connection"
    );
    assert!(
        a.counter("lease", "denied") + b.counter("lease", "denied") >= 1,
        "the other replica was denied"
    );
    let owner = sh.store.owners().pop().expect("an owner");
    // Stop the owner; the other takes over on its next reconcile.
    if owner == "replica-a" {
        a.stop().await;
    } else {
        b.stop().await;
    }
    eventually("the survivor takes over", || srv.accepted() == 2).await;
    a.stop().await;
    b.stop().await;
    assert_eq!(sh.store.len(), 0);
}

#[tokio::test]
async fn a_lost_lease_stops_the_connection_and_it_is_reacquired() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    let mut cfg = fast_config();
    cfg.lease_ttl = Duration::from_millis(800);
    cfg.lease_renew_every = Duration::from_millis(100);
    let mut r = start(&sh, cfg, "a", Opts::default());
    eventually("connected", || srv.accepted() == 1).await;
    sh.store.steal_all();
    eventually("loss counted", || r.counter("lease", "lost") == 1).await;
    eventually("reacquired and reconnected", || srv.accepted() == 2).await;
    r.stop().await;
}

#[tokio::test]
async fn an_unreachable_lease_store_fails_closed() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    let mut cfg = fast_config();
    cfg.lease_ttl = Duration::from_millis(800);
    cfg.lease_renew_every = Duration::from_millis(100);
    let mut r = start(&sh, cfg, "a", Opts::default());
    eventually("connected", || srv.accepted() == 1).await;
    r.lease.down.store(true, Ordering::SeqCst);
    eventually("stopped after renew failures", || {
        r.counter("lease", "lost") == 1
    })
    .await;
    eventually("acquire errors counted", || {
        r.counter("lease", "error") >= 1
    })
    .await;
    assert_eq!(srv.accepted(), 1, "no reconnect while the store is down");
    sh.store.steal_all(); // the real lease would have expired by now
    r.lease.down.store(false, Ordering::SeqCst);
    eventually("reconnected after recovery", || srv.accepted() == 2).await;
    r.stop().await;
}

#[tokio::test]
async fn a_driver_refusal_parks_until_the_fingerprint_changes() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    // The registry has no pinned connector for the platform -> DriverError.
    let opts = Opts {
        registry_platforms: vec!["youtube"],
        ..Opts::default()
    };
    let mut r = start(&sh, fast_config(), "a", opts);
    eventually("parked", || r.counter("parked", "driver_error") == 1).await;
    settle(300).await;
    assert_eq!(
        r.counter("parked", "driver_error"),
        1,
        "parked stays parked"
    );
    assert_eq!(srv.accepted(), 0);
    assert_eq!(sh.store.len(), 0, "a parked connection holds no lease");

    let mut changed = Spec::ws(1, 10, srv.port);
    changed.fp = 9;
    sh.feed.set(vec![changed]);
    eventually("retried on the new fingerprint", || {
        r.counter("restart", "fingerprint") == 1
    })
    .await;
    eventually("parked again", || r.counter("parked", "driver_error") == 2).await;
    r.stop().await;
}

#[tokio::test]
async fn an_invalid_transport_config_parks_as_transport_build() {
    let sh = Shared3::new(vec![Spec {
        kind: Kind::BadWs,
        ..Spec::ws(1, 10, 9)
    }]);
    let mut r = start(&sh, fast_config(), "a", Opts::default());
    eventually("parked", || r.counter("parked", "transport_build") == 1).await;
    settle(200).await;
    assert_eq!(r.counter("parked", "transport_build"), 1);
    r.stop().await;
}

#[tokio::test]
async fn a_secrets_failure_parks_as_transport_build() {
    let sh = Shared3::new(vec![Spec::ws(1, 10, 9)]);
    let opts = Opts {
        secrets_fail: true,
        ..Opts::default()
    };
    let mut r = start(&sh, fast_config(), "a", opts);
    eventually("parked", || r.counter("parked", "transport_build") == 1).await;
    r.stop().await;
}

#[tokio::test]
async fn a_sink_build_failure_cools_down_then_restarts() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    sh.ev.fail_build.lock().expect("lock").insert(1);
    let mut r = start(&sh, fast_config(), "a", Opts::default());
    eventually("retried after cooldown", || {
        r.counter("restart", "sink_build") >= 1
    })
    .await;
    assert_eq!(srv.accepted(), 0);
    sh.ev.fail_build.lock().expect("lock").clear();
    eventually("flows once the sink builds", || sh.ev.count(1) > 2).await;
    r.stop().await;
}

#[tokio::test]
async fn a_fingerprint_change_during_cooldown_restarts_immediately() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    sh.ev.fail_build.lock().expect("lock").insert(1);
    let mut cfg = fast_config();
    cfg.restart_cooldown = Duration::from_secs(60);
    let mut r = start(&sh, cfg, "a", Opts::default());
    eventually("in cooldown", || {
        sh.ev.built.load(Ordering::SeqCst) == 0 && sh.feed.calls() > 3
    })
    .await;
    sh.ev.fail_build.lock().expect("lock").clear();
    let mut changed = Spec::ws(1, 10, srv.port);
    changed.fp = 5;
    sh.feed.set(vec![changed]);
    eventually("restarted despite the 60s cooldown", || sh.ev.count(1) > 2).await;
    assert_eq!(r.counter("restart", "fingerprint"), 1);
    r.stop().await;
}

#[tokio::test]
async fn a_webhook_plan_without_a_binder_parks_loudly() {
    let sh = Shared3::new(vec![Spec {
        kind: Kind::Webhook,
        ..Spec::ws(1, 10, 9)
    }]);
    let mut r = start(&sh, fast_config(), "a", Opts::default());
    eventually("parked", || r.counter("parked", "webhook_unavailable") == 1).await;
    r.stop().await;
}

#[tokio::test]
async fn webhook_routes_are_attached_and_detached_with_the_connection() {
    let binder = IngressBinder::new();
    let sh = Shared3::new(vec![Spec {
        kind: Kind::Webhook,
        ..Spec::ws(1, 10, 9)
    }]);
    let opts = Opts {
        webhook: Some(binder.clone()),
        ..Opts::default()
    };
    let mut r = start(&sh, fast_config(), "a", opts);
    eventually("attached", || binder.attached.load(Ordering::SeqCst) == 1).await;
    assert_eq!(binder.detached.load(Ordering::SeqCst), 0);
    eventually("lease held", || sh.store.len() == 1).await;

    sh.feed.set(vec![]);
    eventually("detached after removal", || {
        binder.detached.load(Ordering::SeqCst) == 1
    })
    .await;
    eventually("lease released", || sh.store.len() == 0).await;
    r.stop().await;
}

#[tokio::test]
async fn a_webhook_binder_refusal_parks_as_transport_build() {
    let binder = IngressBinder::new();
    binder.fail.store(true, Ordering::SeqCst);
    let sh = Shared3::new(vec![Spec {
        kind: Kind::Webhook,
        ..Spec::ws(1, 10, 9)
    }]);
    let opts = Opts {
        webhook: Some(binder),
        ..Opts::default()
    };
    let mut r = start(&sh, fast_config(), "a", opts);
    eventually("parked", || r.counter("parked", "transport_build") == 1).await;
    r.stop().await;
}

#[tokio::test]
async fn readiness_waits_for_the_executor_then_reports_live_over_desired() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    let opts = Opts {
        executor_attached: false,
        ..Opts::default()
    };
    let mut r = start(&sh, fast_config(), "a", opts);
    eventually("detached reported", || {
        r.ready.ready(PLATFORM) == Readiness::NotReady("executor_detached")
    })
    .await;
    assert_eq!(r.ready.ready("discord"), Readiness::Disabled);
    r.executor.0.store(true, Ordering::SeqCst);
    eventually("ready", || {
        r.ready.ready(PLATFORM)
            == Readiness::Ready {
                live: 1,
                desired: 1,
            }
    })
    .await;
    r.stop().await;
}

#[tokio::test]
async fn logs_and_metrics_never_carry_handles_or_ids_as_labels() {
    let cap = capture();
    let srv = WsServer::start().await;
    let handle = "Secret-Handle-XYZ";
    let mut s1 = Spec::ws(1, 10, srv.port);
    s1.source = handle.into();
    let mut s2 = Spec::ws(2, 20, srv.port);
    s2.source = handle.to_lowercase();
    let mut s3 = Spec::ws(3, 30, srv.port);
    s3.source = "another-handle".into();
    let sh = Shared3::new(vec![s1, s2, s3.clone()]);
    sh.ev.panic_for.lock().expect("lock").insert(3);
    let mut r = start(&sh, fast_config(), "a", Opts::default());
    eventually("flows and the panic is contained", || {
        sh.ev.count(3) == 0 && r.counter("lease", "acquired") >= 3
    })
    .await;
    settle(300).await;
    r.stop().await;

    let logged = cap.text();
    assert!(!logged.is_empty(), "capture must have seen events");
    assert!(
        !logged.to_lowercase().contains("secret-handle-xyz")
            && !logged.to_lowercase().contains("another-handle"),
        "handle leaked into logs: {logged}"
    );
    assert!(
        logged.contains("community_id=3"),
        "ids are logged: {logged}"
    );
    assert!(logged.contains("panicked"), "{logged}");

    let allowed_names = ["platform", "outcome", "reason", "kind"];
    let mut series = 0;
    for fam in r.reg.gather() {
        for m in fam.get_metric() {
            for l in m.get_label() {
                series += 1;
                assert!(allowed_names.contains(&l.name()), "label {}", l.name());
                let v = l.value();
                assert!(
                    !v.chars().all(|c| c.is_ascii_digit()),
                    "numeric (id-like) label value {v} on {}",
                    fam.name()
                );
                assert!(!v.to_lowercase().contains("handle"), "{v}");
            }
        }
    }
    assert!(series > 0, "denominator: label series were examined");
}

// ---- construction --------------------------------------------------------

#[tokio::test]
async fn construction_rejects_an_invalid_config() {
    let sh = Shared3::new(vec![]);
    let reg = prometheus::Registry::new();
    let connector_metrics = register_connector_metrics(&reg).expect("cm");
    let metrics = RuntimeMetrics::register(&reg).expect("m");
    let res = ConnectorRuntime::new(
        RuntimeDeps {
            gates: None,
            activator: None,
            feed: FakeFeed(sh.feed.clone()),
            invoker: Arc::new(FakeInvoker),
            registry: Arc::new(ConnectorRegistry::new()),
            sinks: Arc::new(RecFactory(sh.ev.clone())),
            lease: Arc::new(MemLease {
                store: sh.store.clone(),
                owner: "a".into(),
                down: AtomicBool::new(false),
            }),
            secrets: Arc::new(FakeSecrets {
                fail: AtomicBool::new(false),
            }),
            tenants: FakeTenants::ok(1),
            executor: Arc::new(Attached(AtomicBool::new(true))),
            connector_metrics,
            metrics,
            frame_metrics: None,
            webhook: None,
        },
        RuntimeConfig::new(vec![]),
    );
    assert!(matches!(res, Err(RuntimeError::Config(_))));
}

#[tokio::test]
async fn the_feed_is_asked_once_per_platform_per_pass() {
    let sh = Shared3::new(vec![]);
    let mut r = start(&sh, fast_config(), "a", Opts::default());
    eventually("several passes", || sh.feed.calls() >= 3).await;
    assert_eq!(
        r.ready.ready(PLATFORM),
        Readiness::NotReady("digest_unverified")
    );
    r.stop().await;
}

// ---- deterministic Core tests (synthetic connection futures) ------------

type TestFut = std::pin::Pin<Box<dyn Future<Output = (ConnKey, TaskEnd)>>>;
type TestMake = Box<dyn Fn(Arc<DesiredConnection>, oneshot::Receiver<()>) -> TestFut>;
type TestCore = Core<TestFut, TestMake, FakeInvoker, RecSink, MemLease>;

fn core_rig(cfg: RuntimeConfig) -> (TestCore, Arc<LeaseStore>, RuntimeMetrics) {
    let sh3 = Shared3::new(vec![]);
    let reg = prometheus::Registry::new();
    let connector_metrics = register_connector_metrics(&reg).expect("cm");
    let metrics = RuntimeMetrics::register(&reg).expect("m");
    let rt = ConnectorRuntime::new(
        RuntimeDeps {
            gates: None,
            activator: None,
            feed: FakeFeed(sh3.feed.clone()),
            invoker: Arc::new(FakeInvoker),
            registry: Arc::new(ConnectorRegistry::new()),
            sinks: Arc::new(RecFactory(sh3.ev.clone())),
            lease: Arc::new(MemLease {
                store: sh3.store.clone(),
                owner: "core".into(),
                down: AtomicBool::new(false),
            }),
            secrets: Arc::new(FakeSecrets {
                fail: AtomicBool::new(false),
            }),
            tenants: FakeTenants::ok(1),
            executor: Arc::new(Attached(AtomicBool::new(true))),
            connector_metrics,
            metrics: metrics.clone(),
            frame_metrics: None,
            webhook: None,
        },
        cfg.clone(),
    )
    .expect("rt");
    // A synthetic connection ends (as Stopped) only when asked to stop.
    let make: Box<dyn Fn(Arc<DesiredConnection>, oneshot::Receiver<()>) -> TestFut> =
        Box::new(|d, rx| {
            Box::pin(async move {
                let _ = rx.await;
                (d.key.clone(), TaskEnd::Stopped)
            })
        });
    let core = Core {
        make,
        tasks: FuturesUnordered::new(),
        slots: HashMap::new(),
        sh: rt.sh.clone(),
        status: rt.status.clone(),
        config: cfg,
    };
    (core, sh3.store.clone(), metrics)
}

fn d(community: i64, conn: i64, fp: u64) -> DesiredConnection {
    desired_ws(PLATFORM, community, conn, 1, "src", fp)
}

fn key(community: i64, conn: i64) -> ConnKey {
    ConnKey {
        platform: PLATFORM,
        community_id: community,
        connection_id: conn,
    }
}

fn is_active(core: &impl SlotView, k: &ConnKey) -> bool {
    core.state_of(k) == Some("active")
}

trait SlotView {
    fn state_of(&self, k: &ConnKey) -> Option<&'static str>;
}

impl<Fut, M, I, S, L> SlotView for Core<Fut, M, I, S, L>
where
    Fut: Future<Output = (ConnKey, TaskEnd)>,
    M: Fn(Arc<DesiredConnection>, oneshot::Receiver<()>) -> Fut,
    I: ConnectorInvoker + 'static,
    S: EventSink + 'static,
    L: SessionLease + 'static,
{
    fn state_of(&self, k: &ConnKey) -> Option<&'static str> {
        self.slots.get(k).map(|s| match s.state {
            SlotState::Active => "active",
            SlotState::Cooldown { .. } => "cooldown",
            SlotState::Parked => "parked",
        })
    }
}

#[tokio::test]
async fn core_apply_starts_new_keeps_unchanged_and_ignores_foreign_platforms() {
    let (mut core, _store, _m) = core_rig(fast_config());
    let mut foreign = d(9, 99, 1);
    foreign.key.platform = "discord";
    core.apply(PLATFORM, vec![d(1, 10, 1), d(1, 10, 1), foreign]);
    assert_eq!(
        core.slots.len(),
        1,
        "duplicate key and foreign platform dropped"
    );
    assert_eq!(core.tasks.len(), 1);
    core.apply(PLATFORM, vec![d(1, 10, 1)]);
    assert_eq!(core.tasks.len(), 1, "unchanged -> nothing new");
    assert!(is_active(&core, &key(1, 10)));
}

#[tokio::test]
async fn core_changed_fingerprint_stops_then_starts_the_new_one() {
    let (mut core, _store, m) = core_rig(fast_config());
    core.apply(PLATFORM, vec![d(1, 10, 1)]);
    core.apply(PLATFORM, vec![d(1, 10, 2)]);
    assert!(core.slots[&key(1, 10)].stopping);
    assert!(core.slots[&key(1, 10)].pending.is_some());
    // Drive the stop to completion.
    let (k, end) = core.tasks.next().await.expect("ended");
    assert_eq!(end, TaskEnd::Stopped);
    core.on_end(k, end).await;
    assert_eq!(core.tasks.len(), 1, "the replacement started");
    assert_eq!(core.slots[&key(1, 10)].desired.fingerprint, 2);
    assert!(!core.slots[&key(1, 10)].stopping);
    assert_eq!(
        m.restarts_total
            .with_label_values(&[PLATFORM, "fingerprint"])
            .get(),
        1
    );
}

#[tokio::test]
async fn core_removal_then_readd_before_the_end_restarts_it() {
    let (mut core, _store, _m) = core_rig(fast_config());
    core.apply(PLATFORM, vec![d(1, 10, 1)]);
    core.apply(PLATFORM, vec![]);
    assert!(core.slots[&key(1, 10)].stopping && core.slots[&key(1, 10)].pending.is_none());
    core.apply(PLATFORM, vec![d(1, 10, 1)]);
    assert!(
        core.slots[&key(1, 10)].pending.is_some(),
        "re-added while stopping: restart after the stop"
    );
    let (k, end) = core.tasks.next().await.expect("ended");
    core.on_end(k, end).await;
    assert_eq!(core.tasks.len(), 1);
}

#[tokio::test]
async fn core_removal_without_readd_drops_the_slot_on_end() {
    let (mut core, _store, _m) = core_rig(fast_config());
    core.apply(PLATFORM, vec![d(1, 10, 1)]);
    core.apply(PLATFORM, vec![]);
    let (k, end) = core.tasks.next().await.expect("ended");
    core.on_end(k, end).await;
    assert!(core.slots.is_empty() && core.tasks.is_empty());
}

#[tokio::test]
async fn core_cooldown_and_parked_slots_are_removed_immediately_when_undesired() {
    let (mut core, _store, _m) = core_rig(fast_config());
    for (c, state) in [
        (1, SlotState::Parked),
        (
            2,
            SlotState::Cooldown {
                until: Instant::now() + Duration::from_secs(60),
                reason: RestartReason::Ended,
            },
        ),
    ] {
        core.slots.insert(
            key(c, c * 10),
            Slot {
                desired: Arc::new(d(c, c * 10, 1)),
                state,
                stopping: false,
                pending: None,
                stop: None,
            },
        );
    }
    core.apply(PLATFORM, vec![]);
    assert!(core.slots.is_empty());
    assert!(core.tasks.is_empty());
}

#[tokio::test]
async fn core_cooldown_waits_until_elapsed_and_parked_ignores_same_fingerprint() {
    let (mut core, _store, m) = core_rig(fast_config());
    core.slots.insert(
        key(1, 10),
        Slot {
            desired: Arc::new(d(1, 10, 1)),
            state: SlotState::Cooldown {
                until: Instant::now() + Duration::from_secs(60),
                reason: RestartReason::Panic,
            },
            stopping: false,
            pending: None,
            stop: None,
        },
    );
    core.slots.insert(
        key(2, 20),
        Slot {
            desired: Arc::new(d(2, 20, 1)),
            state: SlotState::Parked,
            stopping: false,
            pending: None,
            stop: None,
        },
    );
    core.apply(PLATFORM, vec![d(1, 10, 1), d(2, 20, 1)]);
    assert!(core.tasks.is_empty(), "neither restarts yet");

    // Elapsed cooldown restarts with its recorded reason.
    core.slots.get_mut(&key(1, 10)).expect("slot").state = SlotState::Cooldown {
        until: Instant::now(),
        reason: RestartReason::Panic,
    };
    core.apply(PLATFORM, vec![d(1, 10, 1), d(2, 20, 1)]);
    assert_eq!(core.tasks.len(), 1);
    assert_eq!(
        m.restarts_total
            .with_label_values(&[PLATFORM, "panic"])
            .get(),
        1
    );
    // A changed fingerprint un-parks.
    core.apply(PLATFORM, vec![d(1, 10, 1), d(2, 20, 2)]);
    assert_eq!(core.tasks.len(), 2);
}

#[tokio::test]
async fn core_cap_counts_only_new_connections_against_the_limit() {
    let mut cfg = fast_config();
    cfg.max_connections = 2;
    let (mut core, _store, m) = core_rig(cfg);
    core.apply(PLATFORM, vec![d(1, 10, 1), d(2, 20, 1), d(3, 30, 1)]);
    assert_eq!(core.slots.len(), 2);
    assert!(core.slots.contains_key(&key(1, 10)) && core.slots.contains_key(&key(2, 20)));
    assert_eq!(m.capped_total.with_label_values(&[PLATFORM]).get(), 1);
    // A restart of an existing connection is not blocked by the cap.
    core.apply(PLATFORM, vec![d(1, 10, 2), d(2, 20, 1), d(3, 30, 1)]);
    assert!(core.slots[&key(1, 10)].pending.is_some());
}

#[tokio::test]
async fn core_on_end_classifies_every_end() {
    let (mut core, store, m) = core_rig(fast_config());
    let put = |core: &mut Core<_, _, _, _, _>, c: i64| {
        core.slots.insert(
            key(c, c * 10),
            Slot {
                desired: Arc::new(d(c, c * 10, 1)),
                state: SlotState::Active,
                stopping: false,
                pending: None,
                stop: None,
            },
        );
    };
    for c in 1..=6 {
        put(&mut core, c);
    }
    store
        .held
        .lock()
        .expect("lock")
        .insert((1, 10), "core".into());
    core.on_end(key(1, 10), TaskEnd::Parked(ParkReason::Fatal))
        .await;
    assert_eq!(core.state_of(&key(1, 10)), Some("parked"));
    assert!(
        store.held.lock().expect("lock").is_empty(),
        "lease released on park"
    );
    assert_eq!(
        m.parked_total.with_label_values(&[PLATFORM, "fatal"]).get(),
        1
    );

    core.on_end(key(2, 20), TaskEnd::Failed(RestartReason::Ended))
        .await;
    assert_eq!(core.state_of(&key(2, 20)), Some("cooldown"));
    core.on_end(key(3, 30), TaskEnd::Stopped).await;
    assert_eq!(
        core.state_of(&key(3, 30)),
        Some("cooldown"),
        "an unrequested stop is an unexpected end"
    );
    core.on_end(key(4, 40), TaskEnd::LeaseDenied).await;
    core.on_end(key(5, 50), TaskEnd::LeaseLost).await;
    assert!(core.state_of(&key(4, 40)).is_none() && core.state_of(&key(5, 50)).is_none());
    // An end for an unknown key is ignored.
    core.on_end(key(77, 770), TaskEnd::Stopped).await;
    assert_eq!(core.slots.len(), 4);
    // A pending replacement is NOT started when the lease was lost.
    core.slots.get_mut(&key(6, 60)).expect("slot").pending = Some(Arc::new(d(6, 60, 2)));
    core.on_end(key(6, 60), TaskEnd::LeaseLost).await;
    assert!(core.tasks.is_empty());
}

#[tokio::test]
async fn core_shutdown_aborts_connections_that_ignore_the_stop_and_releases_leases() {
    let mut cfg = fast_config();
    cfg.stop_timeout = Duration::from_millis(50);
    let (mut core, store, _m) = core_rig(cfg);
    // This connection ignores its stop signal entirely.
    core.make = Box::new(|d, _rx| {
        Box::pin(async move {
            std::future::pending::<()>().await;
            (d.key.clone(), TaskEnd::Stopped)
        })
    });
    core.apply(PLATFORM, vec![d(1, 10, 1)]);
    store
        .held
        .lock()
        .expect("lock")
        .insert((1, 10), "core".into());
    let began = std::time::Instant::now();
    core.shutdown().await;
    assert!(began.elapsed() < Duration::from_secs(2));
    assert!(core.tasks.is_empty() && core.slots.is_empty());
    assert!(
        store.held.lock().expect("lock").is_empty(),
        "lease released after abort"
    );
}

#[tokio::test]
async fn core_reconcile_applies_ok_and_records_feed_failures() {
    let (mut core, _store, m) = core_rig(fast_config());
    let ctl = FeedCtl::new(vec![]);
    let feed = FakeFeed(ctl.clone());
    ctl.0.lock().expect("lock").resp = Ok(vec![]);
    core.reconcile(&feed, PLATFORM).await;
    assert_eq!(
        m.reconcile_total.with_label_values(&[PLATFORM, "ok"]).get(),
        1
    );
    ctl.fail();
    core.reconcile(&feed, PLATFORM).await;
    assert_eq!(
        m.reconcile_total
            .with_label_values(&[PLATFORM, "feed_error"])
            .get(),
        1
    );
    assert!(
        core.status
            .snapshot(PLATFORM)
            .expect("snap")
            .discovery_failed
    );
}

#[tokio::test]
async fn resolver_secrets_bind_the_community_and_require_a_provider() {
    use crate::connector_runtime::testkit::ScriptedResolver;
    let r = Arc::new(ScriptedResolver::new(vec![]));
    let providers = BTreeMap::from([("twitch", "twitchprov")]);
    let f = ResolverSecrets::new(r, providers);
    assert!(f.for_connection(&d(1, 10, 1)).is_ok());
    let mut other = d(1, 10, 1);
    other.key.platform = "discord";
    assert!(f.for_connection(&other).is_err(), "no provider configured");
    assert!(
        f.for_connection(&d(0, 10, 1)).is_err(),
        "community must be positive"
    );
}

#[tokio::test]
async fn a_platform_wrapper_is_applied_only_to_its_platform_and_can_refuse() {
    use crate::connector_runtime::testkit::ScriptedResolver;
    let calls = Arc::new(AtomicUsize::new(0));
    let c = calls.clone();
    let wrapper: SecretsWrapper = Arc::new(move |inner, d| {
        c.fetch_add(1, Ordering::SeqCst);
        // The wrapper sees the whole plan (Twitch needs the ws config).
        assert!(matches!(d.plan, TransportPlan::Ws(_)));
        if d.key.connection_id == 666 {
            return Err(TransportError::Connect("refused".into()));
        }
        Ok(inner)
    });
    let r = Arc::new(ScriptedResolver::new(vec![]));
    let providers = BTreeMap::from([("twitch", "twitchprov"), ("discord", "discordprov")]);
    let f = ResolverSecrets::new(r, providers).with_wrapper("twitch", wrapper);
    assert!(f.for_connection(&d(1, 10, 1)).is_ok());
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    let mut disc = d(1, 10, 1);
    disc.key.platform = "discord";
    assert!(f.for_connection(&disc).is_ok());
    assert_eq!(calls.load(Ordering::SeqCst), 1, "not applied to discord");
    assert!(f.for_connection(&d(1, 666, 1)).is_err(), "wrapper refusal");
}

#[tokio::test]
async fn the_sink_is_scoped_to_the_resolved_tenant_not_the_profiles() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    let opts = Opts {
        tenants: FakeTenants::ok(1),
        ..Opts::default()
    };
    // desired_ws sets tenant_id 1; a resolver answering 1 agrees.
    let mut r = start(&sh, fast_config(), "a", opts);
    eventually("flows", || sh.ev.count(1) > 2).await;
    assert_eq!(sh.ev.tenants_seen.lock().expect("lock").first(), Some(&1));
    r.stop().await;
}

#[tokio::test]
async fn a_profile_tenant_that_disagrees_with_the_resolved_one_parks_with_a_metric() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    let opts = Opts {
        tenants: FakeTenants::ok(2), // profile (testkit) says tenant 1
        ..Opts::default()
    };
    let mut r = start(&sh, fast_config(), "a", opts);
    eventually("parked", || r.counter("parked", "tenant_mismatch") == 1).await;
    settle(200).await;
    assert_eq!(r.counter("parked", "tenant_mismatch"), 1, "stays parked");
    assert_eq!(sh.ev.built.load(Ordering::SeqCst), 0, "no sink was built");
    assert_eq!(srv.accepted(), 0);
    assert_eq!(sh.store.len(), 0, "lease released");
    r.stop().await;
}

#[tokio::test]
async fn an_unresolved_tenant_parks_and_the_unresolved_sentinel_is_filled_by_the_resolver() {
    let srv = WsServer::start().await;
    // Resolver answers "unresolved".
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    let tenants = FakeTenants::ok(1);
    *tenants.mode.lock().expect("lock") = Err(TenantError::Unresolved);
    let opts = Opts {
        tenants: tenants.clone(),
        ..Opts::default()
    };
    let mut r = start(&sh, fast_config(), "a", opts);
    eventually("parked unresolved", || {
        r.counter("parked", "tenant_unresolved") == 1
    })
    .await;
    assert_eq!(srv.accepted(), 0);
    r.stop().await;

    // A resolver answering 0 is also "unresolved" (never a default tenant).
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    let opts = Opts {
        tenants: FakeTenants::ok(0),
        ..Opts::default()
    };
    let mut r = start(&sh, fast_config(), "a", opts);
    eventually("parked on tenant 0", || {
        r.counter("parked", "tenant_unresolved") == 1
    })
    .await;
    r.stop().await;
}

#[tokio::test]
async fn a_resolver_mismatch_error_parks_and_a_transient_one_retries() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    let tenants = FakeTenants::ok(1);
    *tenants.mode.lock().expect("lock") = Err(TenantError::Mismatch);
    let mut r = start(
        &sh,
        fast_config(),
        "a",
        Opts {
            tenants: tenants.clone(),
            ..Opts::default()
        },
    );
    eventually("parked mismatch", || {
        r.counter("parked", "tenant_mismatch") == 1
    })
    .await;
    r.stop().await;

    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    let tenants = FakeTenants::ok(1);
    *tenants.mode.lock().expect("lock") = Err(TenantError::Transient);
    let mut r = start(
        &sh,
        fast_config(),
        "a",
        Opts {
            tenants: tenants.clone(),
            ..Opts::default()
        },
    );
    eventually("retried", || r.counter("restart", "tenant_lookup") >= 1).await;
    *tenants.mode.lock().expect("lock") = Ok(1);
    eventually("flows after recovery", || sh.ev.count(1) > 2).await;
    r.stop().await;
}

// ---- host-call activation -------------------------------------------------

/// Records the active-connection table's life: `(connection_id, tenant)` while
/// active, every activate/deactivate in order, and an optional refusal.
#[derive(Default)]
struct RecActivator {
    active: Mutex<HashMap<i64, i64>>,
    log: Mutex<Vec<String>>,
    refuse: AtomicBool,
}

impl ConnectionActivator for RecActivator {
    fn activate(&self, key: &ConnKey, tenant_id: i64) -> Result<(), &'static str> {
        if self.refuse.load(Ordering::SeqCst) {
            return Err("refused");
        }
        self.active
            .lock()
            .expect("lock")
            .insert(key.connection_id, tenant_id);
        self.log
            .lock()
            .expect("lock")
            .push(format!("on:{}:{tenant_id}", key.connection_id));
        Ok(())
    }
    fn deactivate(&self, key: &ConnKey, _tenant_id: i64) {
        self.active.lock().expect("lock").remove(&key.connection_id);
        self.log
            .lock()
            .expect("lock")
            .push(format!("off:{}", key.connection_id));
    }
}

impl RecActivator {
    fn active(&self) -> HashMap<i64, i64> {
        self.active.lock().expect("lock").clone()
    }
    fn log(&self) -> Vec<String> {
        self.log.lock().expect("lock").clone()
    }
}

#[tokio::test]
async fn a_running_connection_is_active_with_the_resolved_tenant_until_it_stops() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port), Spec::ws(2, 20, srv.port)]);
    let act = Arc::new(RecActivator::default());
    let opts = Opts {
        activator: Some(act.clone()),
        ..Opts::default()
    };
    let mut r = start(&sh, fast_config(), "a", opts);
    eventually("both publish", || sh.ev.count(1) > 2 && sh.ev.count(2) > 2).await;
    assert_eq!(
        act.active(),
        HashMap::from([(10, 1), (20, 1)]),
        "activated with the RESOLVED tenant, before frames flowed"
    );
    // Activation precedes the first published event of the connection.
    assert!(act.log()[0].starts_with("on:"));
    r.stop().await;
    assert!(
        act.active().is_empty(),
        "shutdown deactivates every connection"
    );
    assert_eq!(
        act.log().iter().filter(|l| l.starts_with("off:")).count(),
        2
    );
}

#[tokio::test]
async fn a_refused_activation_parks_the_connection_and_it_never_runs() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(1, 10, srv.port)]);
    let act = Arc::new(RecActivator::default());
    act.refuse.store(true, Ordering::SeqCst);
    let opts = Opts {
        activator: Some(act.clone()),
        ..Opts::default()
    };
    let mut r = start(&sh, fast_config(), "a", opts);
    eventually("parked as activation_failed", || {
        r.counter("parked", "activation_failed") >= 1
    })
    .await;
    settle(200).await;
    assert_eq!(
        sh.ev.count(1),
        0,
        "an unscoped connection publishes nothing"
    );
    assert_eq!(srv.accepted(), 0, "and never even dials upstream");
    assert!(act.active().is_empty());
    r.stop().await;
}

#[tokio::test]
async fn a_panicking_connection_is_deactivated_and_reactivated_on_restart() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(2, 20, srv.port)]);
    let act = Arc::new(RecActivator::default());
    let opts = Opts {
        activator: Some(act.clone()),
        ..Opts::default()
    };
    let mut r = start(&sh, fast_config(), "a", opts);
    eventually("flows", || sh.ev.count(2) > 2).await;
    assert!(act.active().contains_key(&20));
    sh.ev.panic_for.lock().expect("lock").insert(2);
    eventually("deactivated after the panic", || {
        !act.active().contains_key(&20)
    })
    .await;
    sh.ev.panic_for.lock().expect("lock").clear();
    eventually("reactivated by the restart", || {
        act.active().contains_key(&20)
    })
    .await;
    r.stop().await;
}

/// The ctx id the executor stamps into host-calls must be the hub connection
/// row id (what the active-connection table is keyed by), not a transport-minted
/// per-session UUID -- otherwise every scoped host-call is denied.
#[tokio::test]
async fn the_connection_ctx_carries_the_hub_connection_id() {
    let srv = WsServer::start().await;
    let sh = Shared3::new(vec![Spec::ws(3, 4242, srv.port)]);
    let mut r = start(&sh, fast_config(), "a", Opts::default());
    eventually("connected", || sh.ev.count(3) > 0).await;
    assert!(CONNECT_CTX_IDS
        .lock()
        .expect("lock")
        .iter()
        .any(|id| id == "4242"));
    r.stop().await;
}
