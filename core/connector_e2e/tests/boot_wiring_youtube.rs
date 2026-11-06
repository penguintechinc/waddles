//! Real-path proof of the connector runtime boot wiring for YOUTUBE (spec PR-E):
//! a YouTube binding whose community is valid resolves its tenant (hub-api's
//! reserved `community` scope lookup, cross-checked against the server-derived
//! feed tenant -- never metadata), starts, polls a fake YouTube API through the
//! REAL compiled overlay in a REAL executor over the connector link, and
//! publishes onto a REAL Valkey spine. A second binding whose community resolves
//! to a different community is refused (parked, never polled, never active).
//! Needs `VALKEY_URL` (unset FAILS).

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
use svc_ingest::connector::host_calls::{CallScope, IdentityTokenizer, TokenizeError};
use svc_ingest::connector::secrets::{Destination, ResolvedAuth};
use svc_ingest::connector::webhook::{BoxFut, SecretError};
use svc_ingest::connector::{TransportError, TransportSecretSource};
use svc_ingest::connector_runtime::boot::{
    assemble, AssembleDeps, HostBridge, LoopbackSeams, RuntimeSettings,
};
use svc_ingest::connector_runtime::feed::{DesiredConnection, DynFeed, FeedError, TransportPlan};
use svc_ingest::connector_runtime::scoped::ScopeLookup;
use svc_ingest::identity_crypto::LocalDevDekProvider;
use svc_ingest::scope_resolver::{ResolvedScope, Unresolved};
use tokio::sync::oneshot;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const WASM: &[u8] = include_bytes!("../../bundle_executor/tests/fixtures/youtube_overlay.wasm");
const APP: &str = "waddles.core.service.youtube";
const CHAT: &str = "Cg0KC-abc_123";
const OTHER_CHAT: &str = "OtherChat_9";

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
        assert_eq!((provider, capability), ("youtube", Some("chat_ingest")));
        let binding = |community: i64, conn: i64, chat: &str| BindingSummary {
            community_id: community,
            connection_id: conn,
            tenant_id: 5,
            secret_version: 1,
            provider: "youtube".into(),
            kind: ConnectionKind::PlatformOauth,
            // A forged metadata tenant must change nothing.
            metadata: [("live_chat_id", chat), ("tenant_id", "999")]
                .into_iter()
                .map(|(k, v)| (k.to_owned(), v.to_owned()))
                .collect(),
        };
        let items = if after == 0 {
            vec![
                binding(7, self.conn, CHAT),
                binding(8, self.conn + 1, OTHER_CHAT),
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
                secret: SecretString::new("ya29.e2e-token".to_owned()),
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

/// Points the host-constant YouTube API URL at the loopback fake (test seam).
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
                if let TransportPlan::Poll(cfg) = &mut d.plan {
                    let rest = cfg
                        .url
                        .strip_prefix("https://www.googleapis.com")
                        .expect("the profile's host constant");
                    cfg.url = format!("http://127.0.0.1:{}{rest}", self.port);
                    cfg.allow_loopback_insecure = true;
                }
            }
            Ok(out)
        })
    }
}

fn item(id: &str, author: &str, text: &str) -> serde_json::Value {
    serde_json::json!({
        "kind": "youtube#liveChatMessage", "id": id,
        "snippet": {
            "type": "textMessageEvent", "liveChatId": CHAT,
            "publishedAt": "2026-11-05T00:00:00.000Z", "displayMessage": text
        },
        "authorDetails": {
            "channelId": author, "displayName": "Viewer",
            "isChatModerator": false, "isChatOwner": false, "isChatSponsor": false
        }
    })
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
async fn a_youtube_binding_resolves_its_tenant_and_publishes_while_a_mismatch_is_refused() {
    let valkey = std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: this e2e needs a REAL Valkey (no silent skip)");
    std::env::set_var("SECURITY_TRANSPORT_TLS", "false");
    std::env::set_var("SECURITY_TRANSPORT_AUTH", "false");
    std::env::set_var("BOOT_WIRING_YT_KEK", "0123456789abcdef0123456789abcdef");
    let run = std::process::id();
    let slug = format!("ytwire{run}");
    let conn = 20_000 + i64::from(run % 40_000) * 2;

    // Fake YouTube API (two chat messages per poll).
    let api = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/youtube/v3/liveChat/messages"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "kind": "youtube#liveChatMessageListResponse",
            "nextPageToken": "p1", "pollingIntervalMillis": 500,
            "items": [item("m-1", "UCviewer1", "hello"), item("m-2", "UCviewer2", "second")]
        })))
        .mount(&api)
        .await;
    let api_hits = || async { api.received_requests().await.unwrap_or_default().len() };

    // Real executor + REAL youtube overlay pinned as the production boot does.
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
        .find(|p| p.platform == "youtube")
        .unwrap();
    assert_eq!(platform.app_id, APP);
    let manifest = VerifiedManifest::new(
        platform.app_id,
        digest.clone(),
        WitWorld::ConnectorTools,
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
        .expect("the real youtube overlay pins");

    let tok = Arc::new(FakeTok::default());
    let bridge = HostBridge::new(tok.clone(), &["youtube"]).unwrap();
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
        .get_multiplexed_async_connection()
        .await
        .unwrap();
    let settings = RuntimeSettings {
        platforms: vec!["youtube"],
        digests: [("youtube", digest)].into_iter().collect(),
        reconcile_interval: Duration::from_secs(1),
        max_connections: 10,
        start_concurrency: 2,
        discord_pace: Duration::ZERO,
        environment: "test".into(),
        heartbeat_enabled: false,
        egress_allow: vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
    };
    let port = api.address().port();
    let scope = Arc::new(FakeScope {
        slug: slug.clone(),
        asked: Mutex::new(Vec::new()),
    });
    let runtime = assemble(
        &settings,
        AssembleDeps {
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
            dek: Arc::new(LocalDevDekProvider::new("local", "BOOT_WIRING_YT_KEK").unwrap()),
            identity_metrics: prometheus::IntCounterVec::new(
                prometheus::Opts::new("boot_wiring_yt_identity_total", "test"),
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
            "boot-wiring-yt",
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
            &[("platform", "youtube"), ("outcome", "published")],
        )
    };
    eventually("youtube messages published", || published(&registry) >= 2.0).await;
    let mut kc = redis::Client::open(valkey.as_str())
        .unwrap()
        .get_multiplexed_async_connection()
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
    assert!(entries >= 2, "stream entries: {entries}");

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
                ("youtube", 7, 5, conn)
            );
        }
    }
    assert!(api_hits().await >= 1, "the fake YouTube API was polled");

    // 3. Mismatch refused: parked, never active, only one connection live.
    eventually("mismatched youtube connection parked", || {
        counter(
            &registry,
            "connector_runtime_parked_total",
            &[("platform", "youtube"), ("reason", "tenant_mismatch")],
        ) >= 1.0
    })
    .await;
    assert_eq!(active.len(), 1);
    assert_eq!(
        counter(
            &registry,
            "connector_runtime_live",
            &[("platform", "youtube")]
        ),
        1.0
    );

    let _ = stop.send(());
    tokio::time::timeout(Duration::from_secs(15), task)
        .await
        .expect("runtime stops")
        .unwrap();
    assert!(active.is_empty());
    exec_conn.abort();
    let _: () = redis::cmd("DEL")
        .arg(&keys)
        .query_async(&mut kc)
        .await
        .unwrap();
}
