//! Real-path proof of the connector runtime boot wiring for SPECTRUM (gh #486):
//! `CONNECTOR_SPECTRUM_ENABLED=true` through the production selector -> settings
//! -> `assemble` (profile, resolver-backed feed, REAL Valkey `SpectrumStore`)
//! polls a fake RSI API through the REAL compiled connector in a REAL executor,
//! primes the roster silently, then publishes the diff (UUID actors) onto a REAL
//! Valkey spine. A second binding whose community resolves elsewhere is parked.
//! Flag OFF: the selector leaves Spectrum on the legacy path and nothing is
//! assembled or polled. Needs `VALKEY_URL` (unset FAILS).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;
use std::net::{IpAddr, Ipv4Addr};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_executor::config::CliConfig;
use bundle_executor::connector::ConnectorLimits;
use bundle_executor::connector_boot::{ConnectorLinkHandler, CONNECTOR_PLATFORMS};
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::Executor;
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use bundle_executor::parity_support::pseudonym_uuid;
use bundle_executor::wire::run_connection;
use clap::Parser;
use connection_resolver::{
    BindingPage, BindingSummary, ConnectionKind, ConnectionRef, ConnectionResolve, ResolveError,
    ResolvedConnection, SecretString,
};
use connector_e2e::{digest_of, NoSource};
use svc_ingest::config::CliConfig as IngestCli;
use svc_ingest::connector::host_calls::{CallScope, IdentityTokenizer, TokenizeError};
use svc_ingest::connector::secrets::{Destination, ResolvedAuth};
use svc_ingest::connector::webhook::{BoxFut, SecretError};
use svc_ingest::connector::{TransportError, TransportSecretSource};
use svc_ingest::connector_runtime::boot::{
    assemble, AssembleDeps, HostBridge, LoopbackSeams, RuntimeSettings,
};
use svc_ingest::connector_runtime::feed::{DesiredConnection, DynFeed, FeedError, TransportPlan};
use svc_ingest::connector_runtime::scoped::ScopeLookup;
use svc_ingest::connector_runtime::selector::ConnectorSelector;
use svc_ingest::identity_crypto::LocalDevDekProvider;
use svc_ingest::scope_resolver::{ResolvedScope, Unresolved};
use tokio::sync::oneshot;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const WASM: &[u8] = include_bytes!("fixtures/spectrum_connector.wasm");
const APP: &str = "waddles.core.service.spectrum";
const COMMUNITY: &str = "555";
const OTHER_COMMUNITY: &str = "556";

/// hub-api binding feed: one honest YouTube binding (community 7, tenant 5) and
/// one whose community (8) the scope authority resolves to community 99.
struct FakeHub {
    conn: i64,
    list_calls: AtomicUsize,
}

impl ConnectionResolve for FakeHub {
    async fn resolve(&self, _r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
        Err(ResolveError::Malformed(
            "not used: credentials come from the seam",
        ))
    }

    async fn list_bindings(
        &self,
        provider: &str,
        capability: Option<&str>,
        after: i64,
        _limit: u32,
    ) -> Result<BindingPage, ResolveError> {
        self.list_calls.fetch_add(1, Ordering::SeqCst);
        assert_eq!((provider, capability), ("spectrum", Some("chat_ingest")));
        let binding = |community: i64, conn: i64, spectrum: &str| BindingSummary {
            community_id: community,
            connection_id: conn,
            tenant_id: 5,
            secret_version: 1,
            provider: "spectrum".into(),
            kind: ConnectionKind::PlatformOauth,
            // The hub-api contract (`community_id`, optional `kind`); a forged
            // metadata tenant must change nothing.
            metadata: [
                ("community_id", spectrum),
                ("kind", "roster"),
                ("tenant_id", "999"),
            ]
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v.to_owned()))
            .collect(),
        };
        let items = if after == 0 {
            vec![
                binding(7, self.conn, COMMUNITY),
                binding(8, self.conn + 1, OTHER_COMMUNITY),
            ]
        } else {
            vec![]
        };
        Ok(BindingPage {
            next_after: None,
            items,
        })
    }
}

/// Scope authority: the reserved `community` lookup (what hub-api answers).
struct FakeScope {
    slug: String,
    asked: Mutex<Vec<(String, String)>>,
}

impl ScopeLookup for FakeScope {
    fn lookup<'a>(
        &'a self,
        platform: &'a str,
        source: &'a str,
    ) -> BoxFut<'a, Result<ResolvedScope, Unresolved>> {
        self.asked
            .lock()
            .unwrap()
            .push((platform.to_owned(), source.to_owned()));
        let r = match (platform, source) {
            ("community", "7") => ResolvedScope::validated(5, 7, &self.slug, Some("main")),
            ("community", "8") => ResolvedScope::validated(5, 99, &self.slug, Some("main")),
            _ => return Box::pin(async { Err(Unresolved::NotFound) }),
        };
        Box::pin(async move { r.map_err(|_| Unresolved::Invalid) })
    }
}

#[derive(Default)]
struct FakeTok {
    seen: Mutex<Vec<CallScope>>,
}

impl IdentityTokenizer for FakeTok {
    fn tokenize<'a>(
        &'a self,
        scope: &'a CallScope,
        platform_user_id: &'a str,
    ) -> BoxFut<'a, Result<String, TokenizeError>> {
        self.seen.lock().unwrap().push(scope.clone());
        let uuid = pseudonym_uuid(scope.platform, platform_user_id);
        Box::pin(async move { Ok(uuid) })
    }
}

struct Bearer;

impl TransportSecretSource for Bearer {
    fn resolve(
        &self,
        _r: ConnectionRef,
        _d: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
        Box::pin(async {
            Ok(ResolvedAuth {
                secret: SecretString::new("rsi-session-e2e".to_owned()),
                header: None,
            })
        })
    }
}

struct Creds;

impl svc_ingest::connector_runtime::SecretsFactory for Creds {
    fn for_connection(
        &self,
        _d: &DesiredConnection,
    ) -> Result<Arc<dyn TransportSecretSource>, TransportError> {
        Ok(Arc::new(Bearer))
    }
}

/// Points the host-constant RSI API URL at the loopback fake (test seam).
struct Rewrite {
    inner: Arc<dyn DynFeed>,
    port: u16,
}

impl DynFeed for Rewrite {
    fn desired_boxed(
        &self,
        platform: &'static str,
    ) -> std::pin::Pin<
        Box<
            dyn std::future::Future<Output = Result<Vec<DesiredConnection>, FeedError>> + Send + '_,
        >,
    > {
        Box::pin(async move {
            let mut out = self.inner.desired_boxed(platform).await?;
            for d in &mut out {
                if let TransportPlan::Spectrum(p) = &mut d.plan {
                    let rest = p
                        .poll
                        .url
                        .strip_prefix("https://robertsspaceindustries.com")
                        .expect("the profile's host constant");
                    p.poll.url = format!("http://127.0.0.1:{}{rest}", self.port);
                    p.poll.allow_loopback_insecure = true;
                    p.poll.interval = Duration::from_millis(30);
                    p.spec.page_delay = Duration::from_millis(5);
                }
            }
            Ok(out)
        })
    }
}

fn member(id: &str, roles: &[&str]) -> serde_json::Value {
    serde_json::json!({"id": id, "displayname": "Pilot", "roles": roles})
}

fn page(list: Vec<serde_json::Value>) -> serde_json::Value {
    serde_json::json!({"success": 1, "code": "OK", "data": {"members": list, "total": 3}})
}

/// Stateful fake RSI roster: the first cycle (two pages) is the primed roster,
/// every later cycle the changed one.
struct FakeRoster(Mutex<usize>);

impl wiremock::Respond for FakeRoster {
    fn respond(&self, req: &wiremock::Request) -> ResponseTemplate {
        let body: serde_json::Value = serde_json::from_slice(&req.body).unwrap();
        assert_eq!(
            req.headers.get("x-rsi-token").unwrap().to_str().unwrap(),
            "rsi-session-e2e"
        );
        assert_eq!(
            req.headers.get("cookie").unwrap().to_str().unwrap(),
            "Rsi-Token=rsi-session-e2e"
        );
        let mut n = self.0.lock().unwrap();
        *n += 1;
        let first = *n <= 2;
        ResponseTemplate::new(200).set_body_json(match (first, body["page"].as_u64().unwrap()) {
            (true, 1) => page(vec![member("RSI-1", &["r1"]), member("RSI-2", &[])]),
            (true, _) => page(vec![member("RSI-3", &[])]),
            (false, 1) => page(vec![member("RSI-1", &["r2"]), member("RSI-2", &[])]),
            (false, _) => page(vec![member("RSI-4", &["r9"])]),
        })
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

async fn eventually(what: &str, cond: impl Fn() -> bool) {
    for _ in 0..400 {
        if cond() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("timed out waiting for: {what}");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn flag_on_runs_spectrum_through_the_production_boot_and_refuses_a_mismatch() {
    let valkey = std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: this e2e needs a REAL Valkey (no silent skip)");
    std::env::set_var("SECURITY_TRANSPORT_TLS", "false");
    std::env::set_var("SECURITY_TRANSPORT_AUTH", "false");
    std::env::set_var("BOOT_WIRING_SP_KEK", "0123456789abcdef0123456789abcdef");
    let run = std::process::id();
    let slug = format!("spwire{run}");
    let conn = 20_000 + i64::from(run % 40_000) * 2;

    // Fake RSI API (POST roster pages; stateful: primed cycle, then the diff).
    let api = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/api/spectrum/community/member/list"))
        .respond_with(FakeRoster(Mutex::new(0)))
        .mount(&api)
        .await;
    let api_hits = || async { api.received_requests().await.unwrap_or_default().len() };

    // Real executor + REAL spectrum connector pinned as the production boot does.
    let cfg = CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-ingest:8301",
    ])
    .unwrap();
    let executor = Arc::new(Executor::new(&cfg, NoSource).unwrap());
    let digest = digest_of(WASM);
    let platform = CONNECTOR_PLATFORMS
        .iter()
        .find(|p| p.platform == "spectrum")
        .unwrap();
    assert_eq!(platform.app_id, APP);
    let manifest = VerifiedManifest::new(
        platform.app_id,
        digest.clone(),
        WitWorld::Connector,
        platform
            .grants
            .iter()
            .map(|g| (*g).to_owned())
            .collect::<HashSet<_>>(),
    );
    executor
        .pin_connector(
            &manifest,
            WASM.to_vec(),
            ConnectorLimits::from_config(&cfg),
            None,
        )
        .await
        .expect("the real spectrum connector pins");

    let tok = Arc::new(FakeTok::default());
    let bridge = HostBridge::new(tok.clone(), &["spectrum"]).unwrap();
    let link = Arc::clone(&bridge.link);
    let active = Arc::clone(&bridge.active);
    let registry = prometheus::Registry::new();
    let spine_metrics: Arc<dyn penguin_spine::SpineMetrics> = Arc::new(penguin_spine::NoopMetrics);
    let appender = Arc::new(
        penguin_spine::SpineClient::connect(
            penguin_spine::SpineConfig::from_env().unwrap(),
            Arc::clone(&spine_metrics),
        )
        .await
        .expect("real spine connects"),
    );
    let lease_conn = redis::Client::open(valkey.as_str())
        .unwrap()
        .get_connection_manager()
        .await
        .unwrap();
    // The production path: selector (flag ON) -> settings (digest from env).
    let lookup = {
        let digest = digest.clone();
        move |k: &str| match k {
            "CONNECTOR_SPECTRUM_ENABLED" => Some("true".to_owned()),
            "SENDER_EXEC_DIGEST_SPECTRUM" => Some(digest.clone()),
            _ => None,
        }
    };
    let selector = ConnectorSelector::from_env(&lookup).expect("selector");
    let mut settings = RuntimeSettings::from_inputs(
        &IngestCli::parse_from(["svc-ingest", "--environment", "test"]),
        &selector,
        &lookup,
    )
    .expect("settings");
    assert_eq!(settings.platforms, vec!["spectrum"]);
    settings.reconcile_interval = Duration::from_secs(1);
    settings.egress_allow = vec![IpAddr::V4(Ipv4Addr::LOCALHOST)];
    let port = api.address().port();
    let scope = Arc::new(FakeScope {
        slug: slug.clone(),
        asked: Mutex::new(Vec::new()),
    });
    let runtime = assemble(
        &settings,
        AssembleDeps {
            interaction_routing: None,
            identify_budget: None,
            resolver: Arc::new(FakeHub {
                conn,
                list_calls: AtomicUsize::new(0),
            }),
            scope: scope.clone(),
            appender,
            spine_metrics,
            keyring: Arc::new(penguin_spine::KeyRing::new(vec![(
                "k1".into(),
                vec![7u8; 32],
            )])),
            active_kid: "k1".into(),
            dek: Arc::new(LocalDevDekProvider::new("local", "BOOT_WIRING_SP_KEK").unwrap()),
            identity_metrics: prometheus::IntCounterVec::new(
                prometheus::Opts::new("boot_wiring_sp_identity_total", "test"),
                &["tenant", "status"],
            )
            .unwrap(),
            lease_conn,
            bridge,
            webhook: None,
            registry: registry.clone(),
            seams: LoopbackSeams {
                feed_wrap: Some(Arc::new(move |_p, inner| {
                    Arc::new(Rewrite { inner, port }) as _
                })),
                secrets: Some(Arc::new(Creds)),
            },
        },
    )
    .expect("the production assembly builds");
    let (stop, rx) = oneshot::channel();
    let task = tokio::spawn(runtime.run(rx));

    let (stage_io, exec_io) = tokio::io::duplex(4 << 20);
    let hello = executor.hello(false, "runc");
    let handler = ConnectorLinkHandler::new(Arc::clone(&executor));
    let exec_conn = tokio::spawn(async move {
        let _ = run_connection(
            exec_io,
            hello,
            handler,
            "boot-wiring-sp",
            Heartbeat::disabled(),
        )
        .await;
    });
    link.attach(stage_io).await.expect("host-api handshake");

    // 1. Published: non-zero events and stream entries under the RESOLVED scope.
    let published = |reg: &prometheus::Registry| {
        counter(
            reg,
            "svc_ingest_connector_events_total",
            &[("platform", "spectrum"), ("outcome", "published")],
        )
    };
    eventually("spectrum roster diff published", || {
        published(&registry) >= 3.0
    })
    .await;
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(
        published(&registry),
        3.0,
        "primed cycle silent; exactly the three deltas"
    );
    let mut kc = redis::Client::open(valkey.as_str())
        .unwrap()
        .get_connection_manager()
        .await
        .unwrap();
    let keys: Vec<String> = redis::cmd("KEYS")
        .arg(format!("*{slug}*"))
        .query_async(&mut kc)
        .await
        .unwrap();
    assert!(
        !keys.is_empty(),
        "stream key carries the resolved scope slug"
    );
    let mut entries = 0i64;
    for k in &keys {
        entries += redis::cmd("XLEN")
            .arg(k)
            .query_async::<i64>(&mut kc)
            .await
            .unwrap();
    }
    assert_eq!(entries, 3, "stream entries: {entries}");

    // 2. Tenant came from the community lookup (not metadata 999), host-calls ran
    //    under the connection's own scope.
    assert!(scope
        .asked
        .lock()
        .unwrap()
        .contains(&("community".to_owned(), "7".to_owned())));
    {
        let seen = tok.seen.lock().unwrap();
        assert!(!seen.is_empty(), "identity.lookup served");
        for s in seen.iter() {
            assert_eq!(
                (s.platform, s.community_id, s.tenant_id, s.connection_id),
                ("spectrum", 7, 5, conn)
            );
        }
    }
    assert!(api_hits().await >= 1, "the fake RSI API was polled");

    // 3. Mismatch refused: parked, never active, only one connection live.
    eventually("mismatched spectrum connection parked", || {
        counter(
            &registry,
            "connector_runtime_parked_total",
            &[("platform", "spectrum"), ("reason", "tenant_mismatch")],
        ) >= 1.0
    })
    .await;
    assert_eq!(active.len(), 1);
    assert_eq!(
        counter(
            &registry,
            "connector_runtime_live",
            &[("platform", "spectrum")]
        ),
        1.0
    );

    // 4. Dedupe/snapshot state lives in the REAL Valkey, community-scoped
    //    (`{tenant}:{community}`), numeric -- not the RSI id.
    let state: Vec<String> = redis::cmd("KEYS")
        .arg("waddles:ingest:spectrum:5:7:roster:*")
        .query_async(&mut kc)
        .await
        .unwrap();
    assert!(
        state.iter().any(|k| k.ends_with(":snap")),
        "snapshot persisted in Valkey: {state:?}"
    );
    assert!(state.iter().all(|k| !k.contains(COMMUNITY)));

    let _ = stop.send(());
    tokio::time::timeout(Duration::from_secs(15), task)
        .await
        .expect("runtime stops")
        .unwrap();
    assert!(active.is_empty());
    exec_conn.abort();
    let _: () = redis::cmd("DEL")
        .arg(&keys)
        .arg(&state)
        .query_async(&mut kc)
        .await
        .unwrap();
}

/// Flag OFF (the default): the selector leaves Spectrum on the legacy receivers,
/// no connector platform is enabled, so nothing is assembled, pinned or polled.
/// Flag ON without its pinned digest is a named boot failure, never a silent run.
#[tokio::test]
async fn flag_off_keeps_spectrum_legacy_and_flag_on_requires_its_digest() {
    let cli = IngestCli::parse_from(["svc-ingest", "--environment", "test"]);
    let off = |_: &str| None::<String>;
    let sel = ConnectorSelector::from_env(&off).expect("default OFF");
    assert!(!sel.connector("spectrum") && sel.legacy("spectrum"));
    assert!(!sel.any_connector());
    let s = RuntimeSettings::from_inputs(&cli, &sel, &off).expect("settings");
    assert!(s.platforms.is_empty(), "no platform enabled: no runtime");

    // Explicit false is the same.
    let explicit = |k: &str| (k == "CONNECTOR_SPECTRUM_ENABLED").then(|| "false".to_owned());
    let sel = ConnectorSelector::from_env(&explicit).expect("explicit OFF");
    assert!(sel.legacy("spectrum") && sel.enabled_platforms().is_empty());

    // ON but no digest: fatal and named.
    let on = |k: &str| (k == "CONNECTOR_SPECTRUM_ENABLED").then(|| "true".to_owned());
    let sel = ConnectorSelector::from_env(&on).expect("ON parses");
    assert!(sel.connector("spectrum") && !sel.legacy("spectrum"));
    let err = RuntimeSettings::from_inputs(&cli, &sel, &on).unwrap_err();
    assert!(
        err.to_string().contains("SENDER_EXEC_DIGEST_SPECTRUM"),
        "{err}"
    );
}
