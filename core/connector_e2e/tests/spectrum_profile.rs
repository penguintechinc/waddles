//! Real-path proof of the Spectrum `ConnectorProfile` (gh #486): the profile
//! derives the stateful poll; svc-ingest's real `PollTransport` +
//! `SpectrumTransport` talk to a local fake RSI API (POST JSON bodies,
//! `Cookie`/`X-Rsi-Token` headers, host-written page numbers); the real
//! `ConnectorDriver` carries each frame through the production wire into the
//! REAL compiled Spectrum connector (`fixtures/spectrum_connector.wasm`), whose
//! `identity.lookup` goes through the executor's real host-call path. Only the
//! upstream, the state store (in-memory) and a recording sink are synthetic.
//!
//! The supervisor test drives the real `ConnectorRuntime` and proves a tenant
//! the resolver disagrees with parks the connection before any request is sent.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{BTreeMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};
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
use svc_ingest::connector::spectrum::{MemorySpectrumStore, SpectrumStore, SpectrumTransport};
use svc_ingest::connector::webhook::{BoxFut, SecretError};
use svc_ingest::connector::{
    register_connector_metrics, ConnectionCtx, ConnectorDriver, ConnectorEvent, ConnectorRegistry,
    EventSink, ExecutorLink, PinnedConnector, PollConfig, PollTransport, RunEnd, SinkError,
    TransportError, WireConnectorInvoker,
};
use svc_ingest::connector_runtime::feed::{DesiredConnection, FeedError, TransportPlan};
use svc_ingest::connector_runtime::metrics::RuntimeMetrics;
use svc_ingest::connector_runtime::plan::{ConnectorProfile, RuntimeEnv};
use svc_ingest::connector_runtime::profiles::spectrum::SpectrumProfile;
use svc_ingest::connector_runtime::ready::ExecutorStatus;
use svc_ingest::connector_runtime::supervisor::{SinkBuildError, SinkFactory};
use svc_ingest::connector_runtime::tenant::{TenantError, TenantResolve};
use svc_ingest::connector_runtime::{
    BindingFeed, ConnectorRuntime, RuntimeConfig, RuntimeDeps, SecretsFactory,
};
use svc_ingest::twitch_sessions::lease::{LeaseError, SessionLease};
use tokio::sync::oneshot;
use tokio::time::Instant;
use wiremock::{Mock, MockServer, Request, Respond, ResponseTemplate};

const WASM: &[u8] = include_bytes!("fixtures/spectrum_connector.wasm");
const TOKEN: &str = "rsi-session-e2e";
const LOBBY: &str = "L-42";
const COMMUNITY: &str = "C-9";

/// Records every published event.
#[derive(Clone, Default)]
struct Recorder(Arc<Mutex<Vec<(ConnectionCtx, ConnectorEvent)>>>);

impl EventSink for Recorder {
    async fn publish(&self, ctx: &ConnectionCtx, event: ConnectorEvent) -> Result<(), SinkError> {
        self.0.lock().unwrap().push((ctx.clone(), event));
        Ok(())
    }
}

impl Recorder {
    fn len(&self) -> usize {
        self.0.lock().unwrap().len()
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

/// One request the fake RSI API observed.
struct Seen {
    path: String,
    body: serde_json::Value,
    cookie: Option<String>,
    token: Option<String>,
    method: String,
}

/// Fake RSI: answers per path from an ordered script (last answer repeats)
/// and records each request.
type Dynamic = Box<dyn FnMut(&serde_json::Value) -> serde_json::Value + Send>;

#[derive(Clone, Default)]
struct Rsi {
    scripts: Arc<Mutex<BTreeMap<String, VecDeque<serde_json::Value>>>>,
    seen: Arc<Mutex<Vec<Seen>>>,
    /// When set, answers every request from the request body (stateful fake).
    dynamic: Arc<Mutex<Option<Dynamic>>>,
}

impl Respond for Rsi {
    fn respond(&self, req: &Request) -> ResponseTemplate {
        let header = |n: &str| req.headers.get(n).map(|v| v.to_str().unwrap().to_owned());
        self.seen.lock().unwrap().push(Seen {
            path: req.url.path().to_owned(),
            body: serde_json::from_slice(&req.body).unwrap(),
            cookie: header("cookie"),
            token: header("x-rsi-token"),
            method: req.method.to_string(),
        });
        if let Some(f) = self.dynamic.lock().unwrap().as_mut() {
            let body: serde_json::Value = serde_json::from_slice(&req.body).unwrap();
            return ResponseTemplate::new(200).set_body_json(f(&body));
        }
        let mut s = self.scripts.lock().unwrap();
        let q = s.get_mut(req.url.path()).expect("scripted path");
        let body = if q.len() > 1 {
            q.pop_front().unwrap()
        } else {
            q.front().cloned().unwrap()
        };
        ResponseTemplate::new(200).set_body_json(body)
    }
}

impl Rsi {
    async fn start(scripts: Vec<(&str, Vec<serde_json::Value>)>) -> (MockServer, Self) {
        let rsi = Self::default();
        for (p, v) in scripts {
            rsi.scripts
                .lock()
                .unwrap()
                .insert(p.to_owned(), VecDeque::from(v));
        }
        let server = MockServer::start().await;
        Mock::given(wiremock::matchers::any())
            .respond_with(rsi.clone())
            .mount(&server)
            .await;
        (server, rsi)
    }
}

fn ok(data: serde_json::Value) -> serde_json::Value {
    serde_json::json!({"success": 1, "code": "OK", "data": data})
}

fn lobby_msgs(items: &[(&str, &str, &str)]) -> serde_json::Value {
    let items: Vec<_> = items
        .iter()
        .map(|(id, author, text)| {
            serde_json::json!({
                "id": id, "plaintext": text, "time_created": 1_730_000_000,
                "member": {"id": author, "displayname": "Spectator"}
            })
        })
        .collect();
    ok(serde_json::json!({"messages": items}))
}

fn member(id: &str, roles: &[&str]) -> serde_json::Value {
    serde_json::json!({"id": id, "displayname": "Pilot", "roles": roles})
}

fn members(list: Vec<serde_json::Value>, total: u64) -> serde_json::Value {
    ok(serde_json::json!({"members": list, "total": total}))
}

fn binding(kind: &str, source: &str) -> BindingSummary {
    BindingSummary {
        tenant_id: 4,
        secret_version: 1,
        community_id: 7,
        connection_id: 12,
        provider: "spectrum".to_owned(),
        kind: ConnectionKind::ExternalHttp,
        metadata: [("kind", kind), ("source_id", source), ("tenant_id", "99")]
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect(),
    }
}

/// The profile's desired connection plus ONLY the test-permitted overrides
/// (loopback http to the fake API, fast intervals); every request shape is
/// the profile's host constant.
fn derived(server: &MockServer, kind: &str, source: &str) -> DesiredConnection {
    let env = RuntimeEnv {
        environment: "test".to_owned(),
        max_frame_bytes: 262_144,
        egress_allow: vec!["127.0.0.1".parse().unwrap()],
        heartbeat_enabled: false,
    };
    let mut plans = SpectrumProfile::new()
        .plan(&binding(kind, source), &env)
        .unwrap();
    assert_eq!(plans.len(), 1);
    let mut d = plans.remove(0);
    let TransportPlan::Spectrum(p) = &mut d.plan else {
        panic!("spectrum plan expected");
    };
    let rest = p
        .poll
        .url
        .strip_prefix("https://robertsspaceindustries.com")
        .expect("the profile's host constant");
    p.poll.url = format!("{}{rest}", server.uri());
    p.poll.allow_loopback_insecure = true;
    p.poll.interval = Duration::from_millis(20);
    p.spec.page_delay = Duration::from_millis(5);
    d
}

fn poll_of(d: &DesiredConnection) -> PollConfig {
    match &d.plan {
        TransportPlan::Spectrum(p) => p.poll.clone(),
        _ => panic!("spectrum plan expected"),
    }
}

struct Rig {
    registry: Arc<ConnectorRegistry>,
    invoker: Arc<WireConnectorInvoker>,
    calls: CallLog,
    _executor: Arc<Executor<NoSource>>,
    _conn: tokio::task::JoinHandle<()>,
    _link: Arc<ExecutorLink>,
}

/// Pins the REAL spectrum connector (inbound-only `connector` world, with the
/// pii grant and the scripted identity bridge) and attaches it over the wire.
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
        "waddles.core.service.spectrum",
        digest.clone(),
        WitWorld::Connector,
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
            "spectrum".to_owned(),
            "waddles.core.service.spectrum".to_owned(),
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
    let (stage_io, exec_io) = tokio::io::duplex(4 << 20);
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

fn is_uuid(s: &str) -> bool {
    s.len() == 36
        && s.char_indices().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => c == '-',
            _ => c.is_ascii_hexdigit(),
        })
}

async fn wait_for(sink: &Recorder, n: usize) {
    let deadline = Instant::now() + Duration::from_secs(30);
    while sink.len() < n {
        assert!(
            Instant::now() < deadline,
            "only {} of {n} events arrived",
            sink.len()
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

/// Runs a driver over `transport` until `n` events were published, then stops it.
async fn run_until(
    rig: &Rig,
    sink: &Recorder,
    transport: SpectrumTransport<Token>,
    n: usize,
) -> RunEnd {
    let (stop, shutdown) = oneshot::channel::<()>();
    let d = driver(rig, sink);
    let run = tokio::spawn(async move { d.run(transport, shutdown).await });
    wait_for(sink, n).await;
    // let any extra (wrong) event surface before asserting exact counts
    tokio::time::sleep(Duration::from_millis(300)).await;
    stop.send(()).unwrap();
    run.await.unwrap().unwrap()
}

fn assert_clean_event(ev: &ConnectorEvent, author: &str) {
    assert_eq!(ev.platform, "spectrum");
    let actor = ev.actor.as_deref().expect("actor");
    assert!(is_uuid(actor), "actor is a UUID token: {actor}");
    assert_eq!(actor, pseudonym_uuid("spectrum", author));
    assert!(
        !ev.payload_json.contains(author),
        "no raw member id in the payload: {}",
        ev.payload_json
    );
    assert!(!ev.payload_json.contains("Spectator") && !ev.payload_json.contains("Pilot"));
}

#[tokio::test(flavor = "multi_thread")]
async fn lobby_primes_dedupes_and_publishes_uuid_actors_over_post() {
    let path = "/api/spectrum/lobby/messages";
    let (server, rsi) = Rsi::start(vec![(
        path,
        vec![
            lobby_msgs(&[("m1", "RSI-A", "old one"), ("m2", "RSI-B", "old two")]),
            lobby_msgs(&[
                ("m1", "RSI-A", "old one"),
                ("m2", "RSI-B", "old two"),
                ("m3", "RSI-A", "fresh"),
            ]),
            lobby_msgs(&[
                ("m1", "RSI-A", "old one"),
                ("m2", "RSI-B", "old two"),
                ("m3", "RSI-A", "fresh"),
            ]),
            lobby_msgs(&[
                ("m2", "RSI-B", "old two"),
                ("m3", "RSI-A", "fresh"),
                ("m4", "RSI-B", "later"),
            ]),
        ],
    )])
    .await;
    let rig = rig().await;
    let sink = Recorder::default();
    let store = Arc::new(MemorySpectrumStore::new());
    let d = derived(&server, "lobby", LOBBY);
    let TransportPlan::Spectrum(plan) = &d.plan else {
        unreachable!()
    };
    let st: Arc<dyn SpectrumStore> = store.clone();
    let transport = SpectrumTransport::new(
        PollTransport::new(poll_of(&d), Token).unwrap(),
        st,
        plan.spec.clone(),
    );
    let end = run_until(&rig, &sink, transport, 2).await;
    assert_eq!(end, RunEnd::Shutdown);

    // Priming suppressed m1/m2; dedupe let m3 and m4 through exactly once.
    let events = sink.0.lock().unwrap();
    assert_eq!(events.len(), 2, "history suppressed, duplicates dropped");
    for ((ctx, ev), (author, text)) in events.iter().zip([("RSI-A", "fresh"), ("RSI-B", "later")]) {
        assert_eq!(ctx.platform, "spectrum");
        assert_eq!(ev.event_type, "message");
        assert_clean_event(ev, author);
        let p: serde_json::Value = serde_json::from_str(&ev.payload_json).unwrap();
        assert_eq!(p["text"], text);
    }
    assert!(rig
        .calls
        .lock()
        .unwrap()
        .iter()
        .any(|o| o == "identity.lookup"));

    // The request shape is the profile's host constant, every poll.
    let seen = rsi.seen.lock().unwrap();
    assert!(seen.len() >= 4);
    for s in seen.iter() {
        assert_eq!(s.method, "POST");
        assert_eq!(s.path, path);
        assert_eq!(s.body, serde_json::json!({"lobby_id": LOBBY, "limit": 50}));
        assert_eq!(s.cookie.as_deref(), Some(&*format!("Rsi-Token={TOKEN}")));
        assert_eq!(s.token.as_deref(), Some(TOKEN));
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn roster_pages_host_cursor_then_emits_member_diff_events() {
    let path = "/api/spectrum/community/member/list";
    let (server, rsi) = Rsi::start(vec![(path, vec![members(vec![], 0)])]).await;
    // Stateful fake: cycle 1 (the first two pages) is the primed roster;
    // every later cycle is the changed roster (RSI-3 left, RSI-4 joined,
    // RSI-1 roles changed) and stays that way.
    let mut calls = 0usize;
    *rsi.dynamic.lock().unwrap() = Some(Box::new(move |body| {
        calls += 1;
        let page = body["page"].as_u64().unwrap();
        match (calls <= 2, page) {
            (true, 1) => members(vec![member("RSI-1", &["r1"]), member("RSI-2", &[])], 3),
            (true, _) => members(vec![member("RSI-3", &[])], 3),
            (false, 1) => members(vec![member("RSI-1", &["r2"]), member("RSI-2", &[])], 3),
            (false, _) => members(vec![member("RSI-4", &["r9"])], 3),
        }
    }));
    let rig = rig().await;
    let sink = Recorder::default();
    let store = Arc::new(MemorySpectrumStore::new());
    let d = derived(&server, "roster", COMMUNITY);
    let TransportPlan::Spectrum(plan) = &d.plan else {
        unreachable!()
    };
    let st: Arc<dyn SpectrumStore> = store.clone();
    let transport = SpectrumTransport::new(
        PollTransport::new(poll_of(&d), Token).unwrap(),
        st,
        plan.spec.clone(),
    );
    // The snapshot cycle repeats; its script's last answers repeat, so only
    // the first diff produces events (later cycles are no-change).
    let end = run_until(&rig, &sink, transport, 3).await;
    assert_eq!(end, RunEnd::Shutdown);

    let events = sink.0.lock().unwrap();
    let mut got: Vec<(String, String)> = events
        .iter()
        .map(|(_, e)| {
            let author = ["RSI-1", "RSI-3", "RSI-4"]
                .into_iter()
                .find(|a| e.actor.as_deref() == Some(pseudonym_uuid("spectrum", a).as_str()))
                .expect("actor is one of the roster members' UUIDs");
            assert_clean_event(e, author);
            (e.event_type.clone(), author.to_owned())
        })
        .collect();
    got.sort();
    assert_eq!(
        got,
        [
            ("member_joined".to_owned(), "RSI-4".to_owned()),
            ("member_left".to_owned(), "RSI-3".to_owned()),
            ("member_roles_changed".to_owned(), "RSI-1".to_owned()),
        ],
        "primed cycle emitted nothing; the diff emitted exactly the deltas"
    );

    // Host-written page numbers, host-constant body otherwise, same headers.
    let seen = rsi.seen.lock().unwrap();
    let pages: Vec<u64> = seen
        .iter()
        .take(4)
        .map(|s| s.body["page"].as_u64().unwrap())
        .collect();
    assert_eq!(pages, [1, 2, 1, 2]);
    for s in seen.iter() {
        assert_eq!(s.path, path);
        assert_eq!(s.body["community_id"], COMMUNITY);
        assert_eq!(s.body["pagesize"], 100);
        assert_eq!(s.cookie.as_deref(), Some(&*format!("Rsi-Token={TOKEN}")));
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn failure_envelope_parks_the_transport() {
    let path = "/api/spectrum/lobby/messages";
    let (server, _) = Rsi::start(vec![(
        path,
        vec![serde_json::json!({"success": 0, "code": "ErrApiLoginRequired"})],
    )])
    .await;
    let rig = rig().await;
    let sink = Recorder::default();
    let d = derived(&server, "lobby", LOBBY);
    let TransportPlan::Spectrum(plan) = &d.plan else {
        unreachable!()
    };
    let st: Arc<dyn SpectrumStore> = Arc::new(MemorySpectrumStore::new());
    let transport = SpectrumTransport::new(
        PollTransport::new(poll_of(&d), Token).unwrap(),
        st,
        plan.spec.clone(),
    );
    let (_keep, shutdown) = oneshot::channel::<()>();
    let end = tokio::time::timeout(
        Duration::from_secs(30),
        driver(&rig, &sink).run(transport, shutdown),
    )
    .await
    .expect("parks in time")
    .unwrap();
    assert_eq!(end, RunEnd::Parked, "a rejected session is terminal");
    assert_eq!(sink.len(), 0);
}

// ---- supervisor: tenant disagreement parks, agreement runs -------------------

struct OneFeed(Mutex<Option<DesiredConnection>>);

impl BindingFeed for OneFeed {
    async fn desired(&self, _p: &'static str) -> Result<Vec<DesiredConnection>, FeedError> {
        // The DesiredConnection is not Clone; rebuild it from the stored plan.
        let g = self.0.lock().unwrap();
        let d = g.as_ref().unwrap();
        let TransportPlan::Spectrum(p) = &d.plan else {
            unreachable!()
        };
        Ok(vec![DesiredConnection {
            key: d.key.clone(),
            tenant_id: d.tenant_id,
            plan: TransportPlan::Spectrum(Box::new(svc_ingest::connector_runtime::SpectrumPlan {
                poll: p.poll.clone(),
                spec: p.spec.clone(),
            })),
            fingerprint: d.fingerprint,
        }])
    }
}

struct Tenant(i64);

impl TenantResolve for Tenant {
    fn resolve<'a>(&'a self, _d: &'a DesiredConnection) -> BoxFut<'a, Result<i64, TenantError>> {
        let t = self.0;
        Box::pin(async move { Ok(t) })
    }
}

struct Secrets;

impl SecretsFactory for Secrets {
    fn for_connection(
        &self,
        _d: &DesiredConnection,
    ) -> Result<Arc<dyn TransportSecretSource>, TransportError> {
        Ok(Arc::new(Token))
    }
}

struct Sinks(Recorder, Arc<Mutex<Vec<i64>>>);

impl SinkFactory<Recorder> for Sinks {
    fn build<'a>(
        &'a self,
        _d: &'a DesiredConnection,
        tenant_id: i64,
    ) -> BoxFut<'a, Result<Recorder, SinkBuildError>> {
        self.1.lock().unwrap().push(tenant_id);
        let r = self.0.clone();
        Box::pin(async move { Ok(r) })
    }
}

struct Attached;

impl ExecutorStatus for Attached {
    fn attached(&self) -> bool {
        true
    }
}

#[derive(Default)]
struct Lease(Mutex<HashSet<(i64, i64)>>, AtomicBool);

impl SessionLease for Lease {
    async fn acquire(&self, key: (i64, i64), _t: Duration) -> Result<bool, LeaseError> {
        Ok(self.0.lock().unwrap().insert(key))
    }
    async fn renew(&self, key: (i64, i64), _t: Duration) -> Result<bool, LeaseError> {
        Ok(self.0.lock().unwrap().contains(&key))
    }
    async fn release(&self, key: (i64, i64)) -> Result<(), LeaseError> {
        self.1.store(true, Ordering::SeqCst);
        self.0.lock().unwrap().remove(&key);
        Ok(())
    }
}

fn counter(reg: &prometheus::Registry, name: &str, want: &[(&str, &str)]) -> f64 {
    reg.gather()
        .iter()
        .filter(|f| f.name() == name)
        .flat_map(|f| f.get_metric().iter())
        .filter(|m| {
            want.iter().all(|(k, v)| {
                m.get_label()
                    .iter()
                    .any(|l| l.name() == *k && l.value() == *v)
            })
        })
        .map(|m| m.get_counter().value() + m.get_gauge().value())
        .sum()
}

/// Boots the real runtime over one Spectrum desired connection whose tenant
/// the resolver answers as `resolved_tenant`; returns (events, tenants the
/// sink was built for, registry, stop handle).
async fn run_runtime(
    rig: &Rig,
    server: &MockServer,
    resolved_tenant: i64,
) -> (
    Recorder,
    Arc<Mutex<Vec<i64>>>,
    prometheus::Registry,
    oneshot::Sender<()>,
    tokio::task::JoinHandle<()>,
) {
    let reg = prometheus::Registry::new();
    let connector_metrics = register_connector_metrics(&reg).unwrap();
    let metrics = RuntimeMetrics::register(&reg).unwrap();
    let sink = Recorder::default();
    let built = Arc::new(Mutex::new(Vec::new()));
    let store: Arc<dyn SpectrumStore> = Arc::new(MemorySpectrumStore::new());
    let mut cfg = RuntimeConfig::new(vec!["spectrum"]);
    cfg.reconcile_interval = Duration::from_millis(100);
    cfg.restart_cooldown = Duration::from_millis(200);
    cfg.lease_ttl = Duration::from_secs(5);
    cfg.lease_renew_every = Duration::from_millis(500);
    cfg.backoff_cap = Duration::from_millis(50);
    let rt = ConnectorRuntime::new(
        RuntimeDeps {
            feed: OneFeed(Mutex::new(Some(derived(server, "lobby", LOBBY)))),
            invoker: Arc::clone(&rig.invoker),
            registry: Arc::clone(&rig.registry),
            sinks: Arc::new(Sinks(sink.clone(), Arc::clone(&built))),
            lease: Arc::new(Lease::default()),
            secrets: Arc::new(Secrets),
            gates: None,
            tenants: Arc::new(Tenant(resolved_tenant)),
            executor: Arc::new(Attached),
            connector_metrics,
            metrics,
            frame_metrics: None,
            webhook: None,
            activator: None,
            spectrum: Some(store),
        },
        cfg,
    )
    .unwrap();
    let (stop, shutdown) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { rt.run(shutdown).await });
    (sink, built, reg, stop, h)
}

#[tokio::test(flavor = "multi_thread")]
async fn runtime_parks_a_tenant_mismatch_and_runs_an_agreeing_tenant() {
    let path = "/api/spectrum/lobby/messages";
    let script = || {
        vec![(
            path,
            vec![
                lobby_msgs(&[("m1", "RSI-A", "history")]),
                lobby_msgs(&[("m1", "RSI-A", "history"), ("m2", "RSI-A", "live")]),
            ],
        )]
    };
    let rig = rig().await;

    // Resolver says tenant 5, the feed said 4 -> parked, nothing polled.
    let (server, rsi) = Rsi::start(script()).await;
    let (sink, built, reg, stop, h) = run_runtime(&rig, &server, 5).await;
    let deadline = Instant::now() + Duration::from_secs(20);
    while counter(
        &reg,
        "connector_runtime_parked_total",
        &[("platform", "spectrum"), ("reason", "tenant_mismatch")],
    ) < 1.0
    {
        assert!(Instant::now() < deadline, "never parked on the mismatch");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(rsi.seen.lock().unwrap().is_empty(), "no request was sent");
    assert!(built.lock().unwrap().is_empty(), "no sink was built");
    assert_eq!(sink.len(), 0);
    stop.send(()).unwrap();
    h.await.unwrap();

    // Agreeing tenant: runs, publishes under the RESOLVED tenant.
    let (server, rsi) = Rsi::start(script()).await;
    let (sink, built, reg, stop, h) = run_runtime(&rig, &server, 4).await;
    wait_for(&sink, 1).await;
    {
        let events = sink.0.lock().unwrap();
        assert_eq!(
            events.len(),
            1,
            "history primed away; the live message flows"
        );
        assert_clean_event(&events[0].1, "RSI-A");
    }
    assert_eq!(built.lock().unwrap().as_slice(), &[4]);
    assert_eq!(
        counter(
            &reg,
            "connector_runtime_parked_total",
            &[("platform", "spectrum"), ("reason", "tenant_mismatch")],
        ),
        0.0
    );
    assert!(!rsi.seen.lock().unwrap().is_empty());
    stop.send(()).unwrap();
    h.await.unwrap();
}
