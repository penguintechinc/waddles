//! Real-path proof of the connector runtime BOOT wiring (spec PR-E): the code
//! `svc_ingest::connector_runtime::boot::assemble` composes in production --
//! feed, per-platform profile, secrets wrapper, scope tenant refusal, scoped
//! sink, active-connection table, host-call allowlist -- driving the REAL
//! compiled Twitch connector in a REAL bundle executor over the host-API wire
//! (`ConnectorLinkHandler`, the connector link's production handler), against a
//! fake IRC server, a fake hub-api resolver and a REAL Valkey (lease + spine).
//!
//! Only the loopback test seams (`LoopbackSeams`) differ from production: the
//! host-constant IRC URL is rewritten to the fake server and the destination-
//! allowlisted credential source is replaced by a pass-only stand-in wrapped by
//! the PRODUCTION `twitch_secrets_wrapper`. Needs `VALKEY_URL` (unset FAILS).
//!
//! Every assertion has a non-zero denominator: connections live, host-calls
//! served, events published, stream entries written.

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
    BindingPage, BindingSummary, ConnectionKind, ConnectionRef, ConnectionResolve,
    ConnectionSecret, Endpoint, ResolveError, ResolvedConnection, SecretString,
};
use connector_e2e::{digest_of, NoSource};
use futures_util::{SinkExt, StreamExt};
use svc_ingest::connector::host_calls::{CallScope, IdentityTokenizer, TokenizeError};
use svc_ingest::connector::secrets::{Destination, ResolvedAuth};
use svc_ingest::connector::webhook::{BoxFut, SecretError};
use svc_ingest::connector::{TransportError, TransportSecretSource};
use svc_ingest::connector_runtime::boot::{
    assemble, twitch_secrets_wrapper, AssembleDeps, HostBridge, LoopbackSeams, RuntimeSettings,
};
use svc_ingest::connector_runtime::feed::{DesiredConnection, DynFeed, FeedError, TransportPlan};
use svc_ingest::connector_runtime::scoped::ScopeLookup;
use svc_ingest::connector_runtime::Readiness;
use svc_ingest::identity_crypto::LocalDevDekProvider;
use svc_ingest::scope_resolver::{ResolvedScope, Unresolved};
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tokio_tungstenite::tungstenite::Message;

const WASM: &[u8] = include_bytes!("fixtures/twitch_connector.wasm");
const TWITCH_APP: &str = "waddles.core.service.twitch";
const CHANNEL: &str = "somechannel";
const OTHER_CHANNEL: &str = "otherchannel";
const TOKEN: &str = "tok_FAKE_0123456789";

/// A fake hub-api: lists two Twitch bindings (one honest, one whose scope
/// resolves to a DIFFERENT community) and resolves nothing else.
struct FakeHub {
    conn: i64,
    list_calls: AtomicUsize,
}

impl ConnectionResolve for FakeHub {
    async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
        Ok(ResolvedConnection {
            connection_id: r.connection_id,
            community_id: r.community_id,
            provider: "twitch".into(),
            kind: ConnectionKind::PlatformOauth,
            endpoint: Endpoint::default(),
            secret: ConnectionSecret::OAuthAccessToken {
                access_token: SecretString::new(TOKEN.to_owned()),
            },
            expires_at: None,
            metadata: Default::default(),
        })
    }

    async fn list_bindings(
        &self,
        provider: &str,
        capability: Option<&str>,
        after: i64,
        _limit: u32,
    ) -> Result<BindingPage, ResolveError> {
        self.list_calls.fetch_add(1, Ordering::SeqCst);
        assert_eq!((provider, capability), ("twitch", Some("chat_ingest")));
        let binding = |community: i64, conn: i64, channel: &str| BindingSummary {
            community_id: community,
            connection_id: conn,
            tenant_id: 5,
            secret_version: 1,
            provider: "twitch".into(),
            kind: ConnectionKind::PlatformOauth,
            metadata: [("channel", channel), ("bot_login", "WaddleBot")]
                .into_iter()
                .map(|(k, v)| (k.to_owned(), v.to_owned()))
                .collect(),
        };
        let items = if after == 0 {
            vec![
                binding(7, self.conn, CHANNEL),
                binding(8, self.conn + 1, OTHER_CHANNEL),
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

/// hub-api's scope resolution: `somechannel` -> community 7, but
/// `otherchannel` (bound to community 8 in the feed) resolves to community 99.
struct FakeScope {
    slug: String,
}

impl ScopeLookup for FakeScope {
    fn lookup<'a>(
        &'a self,
        platform: &'a str,
        source: &'a str,
    ) -> BoxFut<'a, Result<ResolvedScope, Unresolved>> {
        let r = match (platform, source) {
            ("twitch", CHANNEL) => ResolvedScope::validated(5, 7, &self.slug, Some("main")),
            ("twitch", OTHER_CHANNEL) => ResolvedScope::validated(5, 99, &self.slug, Some("main")),
            _ => return Box::pin(async { Err(Unresolved::NotFound) }),
        };
        Box::pin(async move { r.map_err(|_| Unresolved::Invalid) })
    }
}

/// The hub identity service: deterministic pseudonym per platform user id;
/// records the scope each call was made under.
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

/// Pass-only credential stand-in (the real source refuses loopback hosts).
struct PassOnly;

impl TransportSecretSource for PassOnly {
    fn resolve(
        &self,
        r: ConnectionRef,
        _dest: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
        Box::pin(async move {
            if r.connection_id < 1 {
                return Err(SecretError::Missing);
            }
            Ok(ResolvedAuth {
                secret: SecretString::new(TOKEN.to_owned()),
                header: None,
            })
        })
    }
}

/// The production Twitch wrapper over the pass-only inner source.
struct WrappedPass;

impl svc_ingest::connector_runtime::SecretsFactory for WrappedPass {
    fn for_connection(
        &self,
        desired: &DesiredConnection,
    ) -> Result<Arc<dyn TransportSecretSource>, TransportError> {
        (twitch_secrets_wrapper())(Arc::new(PassOnly), desired)
    }
}

/// Rewrites the host-constant IRC URL to the loopback fake (test seam only).
struct Rewrite {
    inner: Arc<dyn DynFeed>,
    addr: std::net::SocketAddr,
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
                if let TransportPlan::Ws(cfg) = &mut d.plan {
                    assert_eq!(cfg.url, "wss://irc-ws.chat.twitch.tv:443", "host constant");
                    cfg.url = format!("ws://{}/", self.addr);
                    cfg.allow_loopback_insecure = true;
                }
            }
            Ok(out)
        })
    }
}

const VIEWER: &str = "@display-name=Viewer1;id=m-1;room-id=555;user-id=12345 :viewer1!viewer1@viewer1.tmi.twitch.tv PRIVMSG #somechannel :hello chat\r\n";
const VIEWER2: &str = "@display-name=Viewer2;id=m-3;room-id=555;user-id=999 :viewer2!viewer2@viewer2.tmi.twitch.tv PRIVMSG #somechannel :second\r\n";

/// Fake IRC: accepts connections, reads the handshake, plays `lines`, and counts
/// accepted sockets (the refused connection must never dial).
async fn irc(l: TcpListener, accepted: Arc<AtomicUsize>, lines: Vec<String>) {
    loop {
        let Ok((tcp, _)) = l.accept().await else {
            return;
        };
        accepted.fetch_add(1, Ordering::SeqCst);
        let lines = lines.clone();
        tokio::spawn(async move {
            let Ok(mut ws) = tokio_tungstenite::accept_async(tcp).await else {
                return;
            };
            let Some(Ok(Message::Text(_hs))) = ws.next().await else {
                return;
            };
            for line in lines {
                if ws.send(Message::text(line)).await.is_err() {
                    return;
                }
            }
            while ws.next().await.is_some() {}
        });
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
async fn boot_wiring_publishes_scoped_events_and_refuses_the_mismatched_tenant() {
    let valkey = std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: this e2e needs a REAL Valkey (no silent skip)");
    std::env::set_var("SECURITY_TRANSPORT_TLS", "false");
    std::env::set_var("SECURITY_TRANSPORT_AUTH", "false");
    std::env::set_var("BOOT_WIRING_KEK", "0123456789abcdef0123456789abcdef");
    let run = std::process::id();
    let slug = format!("bootwire{run}");
    let conn = 10_000 + i64::from(run % 50_000) * 2;

    // Real executor with the REAL twitch connector pinned the way the production
    // boot pins it (host-constant app_id, the platform's exact grants, no bridge).
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
        .find(|p| p.platform == "twitch")
        .unwrap();
    assert_eq!(platform.app_id, TWITCH_APP);
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
        .expect("the real twitch connector pins");

    // Fake IRC.
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    let accepted = Arc::new(AtomicUsize::new(0));
    tokio::spawn(irc(
        l,
        Arc::clone(&accepted),
        vec![VIEWER.into(), VIEWER2.into()],
    ));

    // The boot wiring under test.
    let tok = Arc::new(FakeTok::default());
    let bridge = HostBridge::new(tok.clone(), &["twitch"]).unwrap();
    let link = Arc::clone(&bridge.link);
    let active = Arc::clone(&bridge.active);
    let registry = prometheus::Registry::new();
    let spine_cfg = penguin_spine::SpineConfig::from_env().unwrap();
    let spine_metrics: Arc<dyn penguin_spine::SpineMetrics> = Arc::new(penguin_spine::NoopMetrics);
    let appender = Arc::new(
        penguin_spine::SpineClient::connect(spine_cfg, Arc::clone(&spine_metrics))
            .await
            .expect("real spine connects"),
    );
    let lease_conn = redis::Client::open(valkey.as_str())
        .unwrap()
        .get_multiplexed_async_connection()
        .await
        .unwrap();
    let settings = RuntimeSettings {
        platforms: vec!["twitch"],
        digests: [("twitch", digest.clone())].into_iter().collect(),
        reconcile_interval: Duration::from_secs(1),
        max_connections: 10,
        start_concurrency: 2,
        discord_pace: Duration::ZERO,
        environment: "test".into(),
        heartbeat_enabled: true,
        egress_allow: vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
    };
    let seams = LoopbackSeams {
        feed_wrap: Some(Arc::new(move |_p, inner| {
            Arc::new(Rewrite { inner, addr }) as _
        })),
        secrets: Some(Arc::new(WrappedPass)),
    };
    let runtime = assemble(
        &settings,
        AssembleDeps {
            resolver: Arc::new(FakeHub {
                conn,
                list_calls: AtomicUsize::new(0),
            }),
            scope: Arc::new(FakeScope { slug: slug.clone() }),
            appender,
            spine_metrics,
            keyring: Arc::new(penguin_spine::KeyRing::new(vec![(
                "k1".into(),
                vec![7u8; 32],
            )])),
            active_kid: "k1".into(),
            dek: Arc::new(LocalDevDekProvider::new("local", "BOOT_WIRING_KEK").unwrap()),
            identity_metrics: prometheus::IntCounterVec::new(
                prometheus::Opts::new("boot_wiring_identity_total", "test"),
                &["tenant", "status"],
            )
            .unwrap(),
            lease_conn,
            bridge,
            webhook: None,
            registry: registry.clone(),
            seams,
        },
    )
    .expect("the production assembly builds");
    let ready = runtime.ready_handle();
    let (stop, rx) = oneshot::channel();
    // `tokio::spawn` proves the assembled runtime future is `Send`.
    let task = tokio::spawn(runtime.run(rx));

    // The executor dials the shared link (what its connector dial loop does).
    let (stage_io, exec_io) = tokio::io::duplex(4 << 20);
    let hello = executor.hello(false, "runc");
    let handler = ConnectorLinkHandler::new(Arc::clone(&executor));
    let exec_conn = tokio::spawn(async move {
        let _ = run_connection(
            exec_io,
            hello,
            handler,
            "boot-wiring",
            Heartbeat::disabled(),
        )
        .await;
    });
    link.attach(stage_io).await.expect("host-api handshake");

    // 1. Messages in -> events published (real Valkey), non-zero.
    eventually("both viewer messages published", || {
        counter(
            &registry,
            "svc_ingest_connector_events_total",
            &[("platform", "twitch"), ("outcome", "published")],
        ) >= 2.0
    })
    .await;
    let events = counter(
        &registry,
        "svc_ingest_connector_events_total",
        &[("platform", "twitch"), ("outcome", "published")],
    );
    assert!(events >= 2.0, "published events: {events}");
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
        "the stream key carries the RESOLVED scope slug"
    );
    let mut entries = 0i64;
    for k in &keys {
        let n: i64 = redis::cmd("XLEN")
            .arg(k)
            .query_async(&mut kc)
            .await
            .unwrap();
        entries += n;
    }
    assert!(entries >= 2, "stream entries written: {entries}");

    // 2. The host-call path ran scoped: identity lookups were made under the
    //    connection's OWN scope (tenant from the resolver, community from the feed).
    {
        let seen = tok.seen.lock().unwrap();
        assert!(!seen.is_empty(), "identity.lookup host-calls were served");
        for s in seen.iter() {
            assert_eq!(
                (s.platform, s.community_id, s.tenant_id, s.connection_id),
                ("twitch", 7, 5, conn)
            );
        }
    }
    assert!(
        svc_ingest::connector::host_calls::call_count("db", "ok") >= 1,
        "the allowlist served identity.lookup"
    );

    // 3. Tenant refusal: the second binding resolves to another community, so it
    //    is parked, never dialed, never active, never published.
    eventually("mismatched connection parked", || {
        counter(
            &registry,
            "connector_runtime_parked_total",
            &[("platform", "twitch"), ("reason", "tenant_mismatch")],
        ) >= 1.0
    })
    .await;
    assert_eq!(
        accepted.load(Ordering::SeqCst),
        1,
        "only the honest connection dialed"
    );
    assert_eq!(
        active.len(),
        1,
        "only the honest connection is in the host-call table"
    );
    assert_eq!(
        counter(
            &registry,
            "connector_runtime_live",
            &[("platform", "twitch")]
        ),
        1.0
    );
    assert_eq!(
        ready.ready("twitch"),
        Readiness::Ready {
            live: 1,
            desired: 2
        }
    );

    // 4. Shutdown deactivates every connection.
    let _ = stop.send(());
    tokio::time::timeout(Duration::from_secs(15), task)
        .await
        .expect("runtime stops")
        .unwrap();
    assert!(active.is_empty(), "shutdown empties the active table");
    exec_conn.abort();
    let _: () = redis::cmd("DEL")
        .arg(&keys)
        .query_async(&mut kc)
        .await
        .unwrap();
}
