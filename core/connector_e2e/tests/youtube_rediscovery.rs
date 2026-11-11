//! Real-path proof of YouTube live-chat REDISCOVERY through the production
//! runtime assembly: the stream's chat A ends (`403 liveChatEnded`) -> the host
//! asks `liveBroadcasts` with the connection's own credential -> no live
//! broadcast parks the connection as `waiting_for_live` (re-checked every
//! reconcile) -> once chat B goes live the plan's fingerprint changes, the
//! connection restarts cleanly and B's messages are published through the REAL
//! compiled connector, a REAL executor and a REAL Valkey spine. A decoy
//! broadcast from ANOTHER channel is offered throughout and must never be
//! polled. Needs `VALKEY_URL` (unset FAILS).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;
use std::net::{IpAddr, Ipv4Addr};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
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
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, Request, ResponseTemplate};

const WASM: &[u8] = include_bytes!("../../bundle_executor/tests/fixtures/youtube_connector.wasm");
const APP: &str = "waddles.core.service.youtube";
const CHAT_A: &str = "ChatA_ended-1";
const CHAT_B: &str = "ChatB_live-2";
const CHAT_DECOY: &str = "Decoy_other-3";
const MY_CHANNEL: &str = "UCmychannel";
const OTHER_CHANNEL: &str = "UCotherchannel";

/// hub-api binding feed: one binding (community 7, tenant 5) pinned to chat A
/// and to its own channel.
struct FakeHub {
    conn: i64,
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
        assert_eq!((provider, capability), ("youtube", Some("chat_ingest")));
        let items = if after == 0 {
            vec![BindingSummary {
                community_id: 7,
                connection_id: self.conn,
                tenant_id: 5,
                secret_version: 1,
                provider: "youtube".into(),
                kind: ConnectionKind::PlatformOauth,
                metadata: [("live_chat_id", CHAT_A), ("channel_id", MY_CHANNEL)]
                    .into_iter()
                    .map(|(k, v)| (k.to_owned(), v.to_owned()))
                    .collect(),
            }]
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

/// Points the host-constant YouTube API URLs (poll AND rediscovery) at the
/// loopback fake (test seam).
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
                    let local = |u: &str| {
                        let rest = u
                            .strip_prefix("https://www.googleapis.com")
                            .expect("the profile's host constant");
                        format!("http://127.0.0.1:{}{rest}", self.port)
                    };
                    cfg.url = local(&cfg.url);
                    cfg.allow_loopback_insecure = true;
                    let rd = cfg.rediscover.as_mut().expect("youtube plans rediscover");
                    rd.url = local(&rd.url);
                }
            }
            Ok(out)
        })
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

fn broadcast(chat: &str, channel: &str) -> serde_json::Value {
    serde_json::json!({"snippet": {"liveChatId": chat, "channelId": channel}})
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
    for _ in 0..600 {
        if cond() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("timed out waiting for: {what}");
}

/// The fake YouTube API's mutable world.
#[derive(Default)]
struct World {
    a_ended: AtomicBool,
    b_live: AtomicBool,
    discovery_calls: AtomicUsize,
    polled: Mutex<Vec<String>>,
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn an_ended_live_chat_waits_then_rediscovers_the_new_stream_of_the_same_channel() {
    let valkey = std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: this e2e needs a REAL Valkey (no silent skip)");
    std::env::set_var("SECURITY_TRANSPORT_TLS", "false");
    std::env::set_var("SECURITY_TRANSPORT_AUTH", "false");
    std::env::set_var("YT_REDISC_KEK", "0123456789abcdef0123456789abcdef");
    let run = std::process::id();
    let slug = format!("ytrediscover{run}");
    let conn = 30_000 + i64::from(run % 30_000) * 2;

    let world = Arc::new(World::default());
    let api = MockServer::start().await;
    let w = Arc::clone(&world);
    Mock::given(method("GET"))
        .respond_with(move |req: &Request| {
            let q: std::collections::HashMap<String, String> = req
                .url
                .query_pairs()
                .map(|(k, v)| (k.into_owned(), v.into_owned()))
                .collect();
            if req.url.path() == "/youtube/v3/liveBroadcasts" {
                w.discovery_calls.fetch_add(1, Ordering::SeqCst);
                assert_eq!(q.get("broadcastStatus").map(String::as_str), Some("active"));
                // The decoy (another channel's live stream) is listed FIRST.
                let mut items = vec![broadcast(CHAT_DECOY, OTHER_CHANNEL)];
                if w.b_live.load(Ordering::SeqCst) {
                    items.push(broadcast(CHAT_B, MY_CHANNEL));
                }
                return ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"items": items}));
            }
            assert_eq!(req.url.path(), "/youtube/v3/liveChat/messages");
            let chat = q.get("liveChatId").cloned().unwrap_or_default();
            w.polled.lock().unwrap().push(chat.clone());
            let ended = chat == CHAT_A && w.a_ended.load(Ordering::SeqCst);
            if ended {
                return ResponseTemplate::new(403).set_body_json(serde_json::json!(
                    {"error": {"code": 403, "errors": [{"reason": "liveChatEnded"}]}}
                ));
            }
            let (id1, id2) = if chat == CHAT_B {
                ("b-1", "b-2")
            } else {
                ("a-1", "a-2")
            };
            ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "kind": "youtube#liveChatMessageListResponse",
                "nextPageToken": format!("p-{id1}"), "pollingIntervalMillis": 500,
                "items": [item(&chat, id1, "UCviewer1", "hello"), item(&chat, id2, "UCviewer2", "second")]
            }))
        })
        .mount(&api)
        .await;

    // Real executor + REAL youtube connector pinned as the production boot does.
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
        .expect("the real youtube connector pins");

    let tok = Arc::new(FakeTok::default());
    let bridge = HostBridge::new(tok.clone(), &["youtube"]).unwrap();
    let link = Arc::clone(&bridge.link);
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
            interaction_routing: None,
            identify_budget: None,
            resolver: Arc::new(FakeHub { conn }),
            scope: scope.clone(),
            appender,
            spine_metrics,
            keyring: Arc::new(penguin_spine::KeyRing::new(vec![(
                "k1".into(),
                vec![7u8; 32],
            )])),
            active_kid: "k1".into(),
            dek: Arc::new(LocalDevDekProvider::new("local", "YT_REDISC_KEK").unwrap()),
            identity_metrics: prometheus::IntCounterVec::new(
                prometheus::Opts::new("yt_rediscovery_identity_total", "test"),
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
            "yt-rediscovery",
            Heartbeat::disabled(),
        )
        .await;
    });
    link.attach(stage_io).await.expect("host-api handshake");

    let published = || {
        counter(
            &registry,
            "svc_ingest_connector_events_total",
            &[("platform", "youtube"), ("outcome", "published")],
        )
    };
    let metric = |name: &str, reason: &str| {
        counter(
            &registry,
            name,
            &[("platform", "youtube"), ("reason", reason)],
        )
    };
    let polled = |chat: &str| {
        world
            .polled
            .lock()
            .unwrap()
            .iter()
            .filter(|c| *c == chat)
            .count()
    };

    // 1. Chat A is live: its messages are published.
    eventually("chat A messages published", || published() >= 2.0).await;
    assert!(polled(CHAT_A) >= 1);
    assert_eq!(world.discovery_calls.load(Ordering::SeqCst), 0);

    // 2. Chat A ends, nothing else is live for THIS channel: the connection
    //    waits (distinct reason, not terminal) and re-checks every reconcile.
    let before_wait = published();
    world.a_ended.store(true, Ordering::SeqCst);
    eventually("waiting_for_live parked twice (re-checked)", || {
        metric("connector_runtime_parked_total", "waiting_for_live") >= 2.0
            && world.discovery_calls.load(Ordering::SeqCst) >= 2
    })
    .await;
    assert_eq!(metric("connector_runtime_parked_total", "fatal"), 0.0);
    // Quota burn is visible: one counted discovery call per outcome.
    let discovery = |outcome: &str| {
        counter(
            &registry,
            "svc_ingest_transport_frame_rules_total",
            &[
                ("platform", "youtube"),
                ("rule", "poll_rediscover"),
                ("outcome", outcome),
            ],
        )
    };
    assert!(discovery("waiting_for_live") >= 2.0);
    assert_eq!(discovery("rediscovered"), 0.0);
    assert_eq!(polled(CHAT_B), 0, "B is not live yet");
    assert_eq!(
        polled(CHAT_DECOY),
        0,
        "another channel's chat is never polled"
    );

    // 3. Chat B goes live: the runtime retargets (fingerprint restart) and
    //    publishes B's messages.
    world.b_live.store(true, Ordering::SeqCst);
    eventually("chat B messages published", || {
        polled(CHAT_B) >= 1 && published() >= before_wait + 2.0
    })
    .await;
    assert!(metric("connector_runtime_restarts_total", "fingerprint") >= 1.0);
    assert_eq!(discovery("rediscovered"), 1.0);
    assert_eq!(
        polled(CHAT_DECOY),
        0,
        "another channel's chat is never polled"
    );
    assert_eq!(metric("connector_runtime_parked_total", "fatal"), 0.0);

    // The connection is the same community/connection throughout.
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

    let _ = stop.send(());
    tokio::time::timeout(Duration::from_secs(15), task)
        .await
        .expect("runtime stops")
        .unwrap();
    exec_conn.abort();
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
    let _: () = redis::cmd("DEL")
        .arg(&keys)
        .query_async(&mut kc)
        .await
        .unwrap();
}
