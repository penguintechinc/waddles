//! Flag-ON acceptance of the platform connector SERVICE bundles (spec PR-G).
//!
//! One boot composes the production runtime (`assemble`, the settings the
//! production `RuntimeSettings::from_inputs` builds from the env lookup) with
//! Twitch AND YouTube ON, a REAL bundle executor pinning the REAL compiled
//! Twitch and YouTube bundles over the host-API wire, a REAL Valkey spine, a fake
//! IRC server, a fake YouTube API and a fake hub-api (bindings, scope authority,
//! identity tokenizer). Discord stays blocked: the boot refuses it while
//! `IDENTIFY_BUDGET_READY` is false.
//!
//! Per platform it proves: a message is ingested, tokenized (the actor on the
//! stream decrypts to the hub's pseudonym UUID, no raw handle appears in any
//! stream entry byte or any log line), and published to the spine under the
//! RESOLVED tenant/community; the legacy path is suppressed (counting fake);
//! a runtime restart and a dropped upstream both resume; a tenant mismatch
//! parks. Every assertion has a non-zero denominator. Needs `VALKEY_URL`
//! (unset FAILS, never skips).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{BTreeMap, HashSet};
use std::io::Write;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
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
use svc_ingest::connector_runtime::selector::{
    start_legacy, BootError, LegacyPlan, LegacyStarters, OverlaySelector, IDENTIFY_BUDGET_READY,
};
use svc_ingest::connector_runtime::Readiness;
use svc_ingest::identity_crypto::{
    decrypt_identity_value, DekProvider, JsonEnvelope, LocalDevDekProvider,
};
use svc_ingest::scope_resolver::{ResolvedScope, Unresolved};
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tokio_tungstenite::tungstenite::Message;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const TWITCH_WASM: &[u8] = include_bytes!("fixtures/twitch_overlay.wasm");
const YOUTUBE_WASM: &[u8] = include_bytes!("fixtures/youtube_overlay.wasm");
const KEK_ENV: &str = "CONNECTOR_ACCEPTANCE_KEK";
const TW_CHANNEL: &str = "acceptancechan";
const TW_OTHER_CHANNEL: &str = "otheracceptancechan";
const YT_CHAT: &str = "Cg0KC-acceptance";
const YT_OTHER_CHAT: &str = "OtherAcceptanceChat";
const TW_TOKEN: &str = "tok_FAKE_acceptance_0123";

/// Platform user ids sent by the fakes. They ride the event payload in clear
/// today (`user_id`/`author_id`, the platform's opaque id -- NOT a login or
/// display name); the suite pins that exact residual (see [`CLEAR_ID_FIELDS`])
/// and asserts they never reach a log line.
const TW_USER_ID: &str = "7340512";
const TW_USER_ID_2: &str = "7340999";
const YT_AUTHOR: &str = "UCacceptanceviewerone";
const YT_AUTHOR_2: &str = "UCacceptanceviewertwo";

/// Logins, display names and message text: must never appear in clear in any
/// stream entry byte or any log line (lowercased; compared case-blind).
const FORBIDDEN: [&str; 4] = [
    "acceptviewer1",
    "acceptviewer2",
    "hello acceptance chat",
    "second acceptance line",
];

/// The ONLY payload fields allowed to carry a platform id in clear. A new clear
/// identity-bearing field fails the suite until it is deliberately added here.
const CLEAR_ID_FIELDS: [&str; 2] = ["user_id", "author_id"];

fn privmsg(login: &str, user_id: &str, msg_id: &str, text: &str) -> String {
    format!(
        "@display-name={login};id={msg_id};room-id=555;user-id={user_id} \
         :{login}!{login}@{login}.tmi.twitch.tv PRIVMSG #{TW_CHANNEL} :{text}\r\n"
    )
}

/// Collects every tracing event the process emits (host runtime, driver,
/// supervisor, host-call allowlist and the guests' `log` imports) so no raw
/// handle can hide in a log line.
#[derive(Clone, Default)]
struct LogSink(Arc<Mutex<Vec<u8>>>);

impl Write for LogSink {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0
            .lock()
            .map_err(|_| std::io::Error::other("log sink poisoned"))?
            .extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for LogSink {
    type Writer = LogSink;

    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}

fn logs() -> &'static LogSink {
    static SINK: OnceLock<LogSink> = OnceLock::new();
    SINK.get_or_init(|| {
        let sink = LogSink::default();
        let sub = tracing_subscriber::fmt()
            .with_writer(sink.clone())
            .with_max_level(tracing::Level::TRACE)
            .with_ansi(false)
            .finish();
        tracing::subscriber::set_global_default(sub)
            .expect("the acceptance binary installs its log capture exactly once");
        sink
    })
}

fn logged() -> String {
    let bytes = logs().0.lock().expect("log sink lock").clone();
    String::from_utf8_lossy(&bytes).to_lowercase()
}

/// hub-api's binding list for BOTH providers: per platform one honest binding
/// (community 7) and one whose scope resolves to a DIFFERENT community.
struct FakeHub {
    tw_conn: i64,
    yt_conn: i64,
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
                access_token: SecretString::new(TW_TOKEN.to_owned()),
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
        assert_eq!(capability, Some("chat_ingest"));
        let binding = |community: i64, conn: i64, meta: [(&str, &str); 2]| BindingSummary {
            community_id: community,
            connection_id: conn,
            tenant_id: 5,
            secret_version: 1,
            provider: provider.to_owned(),
            kind: ConnectionKind::PlatformOauth,
            metadata: meta
                .into_iter()
                .map(|(k, v)| (k.to_owned(), v.to_owned()))
                .collect(),
        };
        let items = match (provider, after) {
            ("twitch", 0) => vec![
                binding(
                    7,
                    self.tw_conn,
                    [("channel", TW_CHANNEL), ("bot_login", "WaddleBot")],
                ),
                binding(
                    8,
                    self.tw_conn + 1,
                    [("channel", TW_OTHER_CHANNEL), ("bot_login", "WaddleBot")],
                ),
            ],
            ("youtube", 0) => vec![
                binding(
                    7,
                    self.yt_conn,
                    [("live_chat_id", YT_CHAT), ("tenant_id", "999")],
                ),
                binding(
                    8,
                    self.yt_conn + 1,
                    [("live_chat_id", YT_OTHER_CHAT), ("tenant_id", "999")],
                ),
            ],
            (_, 0) => panic!("acceptance boot listed an unexpected provider: {provider}"),
            _ => vec![],
        };
        Ok(BindingPage {
            next_after: None,
            items,
        })
    }
}

/// Scope authority: Twitch looks up the channel; YouTube the reserved
/// `community` source. Each platform resolves to its OWN tenant slug so a
/// cross-wired stream key is caught. Community 8 resolves to 99 (mismatch).
struct FakeScope {
    tw_slug: String,
    yt_slug: String,
}

impl ScopeLookup for FakeScope {
    fn lookup<'a>(
        &'a self,
        platform: &'a str,
        source: &'a str,
    ) -> BoxFut<'a, Result<ResolvedScope, Unresolved>> {
        let r = match (platform, source) {
            ("twitch", TW_CHANNEL) => ResolvedScope::validated(5, 7, &self.tw_slug, Some("main")),
            ("twitch", TW_OTHER_CHANNEL) => {
                ResolvedScope::validated(5, 99, &self.tw_slug, Some("main"))
            }
            ("community", "7") => ResolvedScope::validated(5, 7, &self.yt_slug, Some("main")),
            ("community", "8") => ResolvedScope::validated(5, 99, &self.yt_slug, Some("main")),
            _ => return Box::pin(async { Err(Unresolved::NotFound) }),
        };
        Box::pin(async move { r.map_err(|_| Unresolved::Invalid) })
    }
}

/// The hub identity service: deterministic pseudonym per platform user id,
/// recording the scope each call was made under.
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
                secret: SecretString::new(TW_TOKEN.to_owned()),
                header: None,
            })
        })
    }
}

/// Twitch gets the PRODUCTION wrapper over the pass-only source; YouTube gets
/// the pass-only bearer directly.
struct Creds;

impl svc_ingest::connector_runtime::SecretsFactory for Creds {
    fn for_connection(
        &self,
        desired: &DesiredConnection,
    ) -> Result<Arc<dyn TransportSecretSource>, TransportError> {
        match desired.key.platform {
            "twitch" => (twitch_secrets_wrapper())(Arc::new(PassOnly), desired),
            _ => Ok(Arc::new(PassOnly)),
        }
    }
}

/// Rewrites each platform's host-constant URL to its loopback fake (the only
/// test seam; production never constructs this).
struct Rewrite {
    inner: Arc<dyn DynFeed>,
    irc: SocketAddr,
    yt_port: u16,
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
                match &mut d.plan {
                    TransportPlan::Ws(cfg) => {
                        assert_eq!(cfg.url, "wss://irc-ws.chat.twitch.tv:443", "host constant");
                        cfg.url = format!("ws://{}/", self.irc);
                        cfg.allow_loopback_insecure = true;
                    }
                    TransportPlan::Poll(cfg) => {
                        let rest = cfg
                            .url
                            .strip_prefix("https://www.googleapis.com")
                            .expect("the youtube profile's host constant");
                        cfg.url = format!("http://127.0.0.1:{}{rest}", self.yt_port);
                        cfg.allow_loopback_insecure = true;
                    }
                    _ => panic!("acceptance feed produced an unexpected transport plan"),
                }
            }
            Ok(out)
        })
    }
}

/// Fake IRC. The first accepted socket plays `first` then CLOSES (a dropped
/// upstream); every later socket plays `later` and holds open. Counts sockets.
async fn irc(
    l: TcpListener,
    accepted: Arc<AtomicUsize>,
    drop_first: bool,
    first: Vec<String>,
    later: Vec<String>,
) {
    loop {
        let Ok((tcp, _)) = l.accept().await else {
            return;
        };
        let nth = accepted.fetch_add(1, Ordering::SeqCst);
        let (lines, hold) = if nth == 0 {
            (first.clone(), !drop_first)
        } else {
            (later.clone(), true)
        };
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
            if hold {
                while ws.next().await.is_some() {}
            }
        });
    }
}

fn yt_item(id: &str, author: &str, text: &str) -> serde_json::Value {
    serde_json::json!({
        "kind": "youtube#liveChatMessage", "id": id,
        "snippet": {
            "type": "textMessageEvent", "liveChatId": YT_CHAT,
            "publishedAt": "2026-11-05T00:00:00.000Z", "displayMessage": text
        },
        "authorDetails": {
            "channelId": author, "displayName": "AcceptViewer",
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

fn published(reg: &prometheus::Registry, platform: &str) -> f64 {
    counter(
        reg,
        "svc_ingest_connector_events_total",
        &[("platform", platform), ("outcome", "published")],
    )
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

type Entry = (String, Vec<(String, Vec<u8>)>);

/// Everything one test run shares: identity, fakes and the real Valkey.
struct Ctx {
    valkey: String,
    tw_slug: String,
    yt_slug: String,
    tw_conn: i64,
    yt_conn: i64,
    irc: SocketAddr,
    irc_accepted: Arc<AtomicUsize>,
    api: MockServer,
}

impl Ctx {
    /// Binds the fakes (`n` keeps parallel tests' connection ids and slugs disjoint). `drop_first` makes the first IRC socket close after its
    /// lines (the reconnect scenario); `later` is what every later socket plays.
    async fn new(n: i64, drop_first: bool, first: Vec<String>, later: Vec<String>) -> Self {
        logs();
        let valkey = std::env::var("VALKEY_URL")
            .expect("VALKEY_URL must be set: this e2e needs a REAL Valkey (no silent skip)");
        std::env::set_var("SECURITY_TRANSPORT_TLS", "false");
        std::env::set_var("SECURITY_TRANSPORT_AUTH", "false");
        std::env::set_var(KEK_ENV, "0123456789abcdef0123456789abcdef");
        let run = std::process::id();
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let irc_addr = l.local_addr().unwrap();
        let accepted = Arc::new(AtomicUsize::new(0));
        tokio::spawn(irc(l, Arc::clone(&accepted), drop_first, first, later));
        let api = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/youtube/v3/liveChat/messages"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "kind": "youtube#liveChatMessageListResponse",
                "nextPageToken": "p1", "pollingIntervalMillis": 500,
                "items": [
                    yt_item("yt-m-1", YT_AUTHOR, "hello acceptance chat"),
                    yt_item("yt-m-2", YT_AUTHOR_2, "second acceptance line")
                ]
            })))
            .mount(&api)
            .await;
        Self {
            valkey,
            tw_slug: format!("acctw{n}x{run}"),
            yt_slug: format!("accyt{n}x{run}"),
            tw_conn: 30_000 + i64::from(run % 20_000) * 40 + n * 4,
            yt_conn: 30_002 + i64::from(run % 20_000) * 40 + n * 4,
            irc: irc_addr,
            irc_accepted: accepted,
            api,
        }
    }

    async fn kc(&self) -> redis::aio::MultiplexedConnection {
        redis::Client::open(self.valkey.as_str())
            .unwrap()
            .get_multiplexed_async_connection()
            .await
            .unwrap()
    }

    /// Stream keys written under one platform's RESOLVED tenant slug.
    async fn keys(&self, slug: &str, platform: &str) -> Vec<String> {
        let mut kc = self.kc().await;
        let all: Vec<String> = redis::cmd("KEYS")
            .arg(format!("*{slug}*"))
            .query_async(&mut kc)
            .await
            .unwrap();
        all.into_iter()
            .filter(|k| k.contains(&format!(":{platform}:")))
            .collect()
    }

    async fn entries(&self, key: &str) -> Vec<Entry> {
        let mut kc = self.kc().await;
        redis::cmd("XRANGE")
            .arg(key)
            .arg("-")
            .arg("+")
            .query_async(&mut kc)
            .await
            .unwrap()
    }

    async fn cleanup(&self) {
        let mut kc = self.kc().await;
        for slug in [&self.tw_slug, &self.yt_slug] {
            let keys: Vec<String> = redis::cmd("KEYS")
                .arg(format!("*{slug}*"))
                .query_async(&mut kc)
                .await
                .unwrap();
            if !keys.is_empty() {
                let _: () = redis::cmd("DEL")
                    .arg(&keys)
                    .query_async(&mut kc)
                    .await
                    .unwrap();
            }
        }
    }
}

/// One booted runtime + executor (restart = drop and boot another).
struct Rig {
    registry: prometheus::Registry,
    tok: Arc<FakeTok>,
    active: Arc<svc_ingest::connector::host_calls::ActiveConnections>,
    ready: Arc<dyn svc_ingest::connector_runtime::RuntimeReady>,
    stop: Option<oneshot::Sender<()>>,
    task: Option<tokio::task::JoinHandle<()>>,
    exec_conn: tokio::task::JoinHandle<()>,
}

async fn pin(
    executor: &Executor<NoSource>,
    cfg: &CliConfig,
    platform: &str,
    wasm: &[u8],
) -> String {
    let digest = digest_of(wasm);
    let p = CONNECTOR_PLATFORMS
        .iter()
        .find(|p| p.platform == platform)
        .unwrap();
    let manifest = VerifiedManifest::new(
        p.app_id,
        digest.clone(),
        WitWorld::ConnectorTools,
        p.grants
            .iter()
            .map(|g| (*g).to_owned())
            .collect::<HashSet<_>>(),
    );
    executor
        .pin_connector(
            &manifest,
            wasm.to_vec(),
            ConnectorLimits::from_config(cfg),
            None,
        )
        .await
        .expect("the real connector service bundle pins");
    digest
}

impl Rig {
    async fn boot(ctx: &Ctx, hub: Arc<FakeHub>) -> Self {
        // Executor with BOTH real bundles pinned as the production boot pins them.
        let cfg = CliConfig::try_parse_from([
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-ingest:8301",
        ])
        .unwrap();
        let executor = Arc::new(Executor::new(&cfg, NoSource).unwrap());
        let tw_digest = pin(&executor, &cfg, "twitch", TWITCH_WASM).await;
        let yt_digest = pin(&executor, &cfg, "youtube", YOUTUBE_WASM).await;

        // The production selector + settings builder, fed by an env lookup.
        let env: BTreeMap<&str, String> = [
            ("OVERLAY_TWITCH_ENABLED", "true".to_owned()),
            ("OVERLAY_YOUTUBE_ENABLED", "true".to_owned()),
            ("SENDER_EXEC_DIGEST_TWITCH", tw_digest),
            ("SENDER_EXEC_DIGEST_YOUTUBE", yt_digest),
        ]
        .into_iter()
        .collect();
        let lookup = |k: &str| env.get(k).cloned();
        let selector = OverlaySelector::from_env(&lookup).expect("twitch + youtube ON is accepted");
        let ingest_cli = svc_ingest::config::CliConfig::parse_from(["svc-ingest"]);
        let mut settings = RuntimeSettings::from_inputs(&ingest_cli, &selector, &lookup)
            .expect("production settings build from the env lookup");
        assert_eq!(settings.platforms, vec!["twitch", "youtube"]);
        // Loopback-only deltas (production leaves these at config defaults).
        settings.reconcile_interval = Duration::from_secs(1);
        settings.egress_allow = vec![IpAddr::V4(Ipv4Addr::LOCALHOST)];
        settings.heartbeat_enabled = false;
        settings.discord_pace = Duration::ZERO;

        let tok = Arc::new(FakeTok::default());
        let bridge = HostBridge::new(tok.clone(), &["twitch", "youtube"]).unwrap();
        let link = Arc::clone(&bridge.link);
        let active = Arc::clone(&bridge.active);
        let registry = prometheus::Registry::new();
        let spine_metrics: Arc<dyn penguin_spine::SpineMetrics> =
            Arc::new(penguin_spine::NoopMetrics);
        let appender = Arc::new(
            penguin_spine::SpineClient::connect(
                penguin_spine::SpineConfig::from_env().unwrap(),
                Arc::clone(&spine_metrics),
            )
            .await
            .expect("real spine connects"),
        );
        let lease_conn = ctx.kc().await;
        let (irc, yt_port) = (ctx.irc, ctx.api.address().port());
        let runtime = assemble(
            &settings,
            AssembleDeps {
                resolver: hub,
                scope: Arc::new(FakeScope {
                    tw_slug: ctx.tw_slug.clone(),
                    yt_slug: ctx.yt_slug.clone(),
                }),
                appender,
                spine_metrics,
                keyring: Arc::new(penguin_spine::KeyRing::new(vec![(
                    "k1".into(),
                    vec![7u8; 32],
                )])),
                active_kid: "k1".into(),
                dek: Arc::new(LocalDevDekProvider::new("local", KEK_ENV).unwrap()),
                identity_metrics: prometheus::IntCounterVec::new(
                    prometheus::Opts::new("connector_acceptance_identity_total", "test"),
                    &["tenant", "status"],
                )
                .unwrap(),
                lease_conn,
                bridge,
                webhook: None,
                registry: registry.clone(),
                seams: LoopbackSeams {
                    feed_wrap: Some(Arc::new(move |_p, inner| {
                        Arc::new(Rewrite {
                            inner,
                            irc,
                            yt_port,
                        }) as _
                    })),
                    secrets: Some(Arc::new(Creds)),
                },
            },
        )
        .expect("the production assembly builds");
        let ready = runtime.ready_handle();
        let (stop, rx) = oneshot::channel();
        // `tokio::spawn` proves the assembled runtime future is `Send`.
        let task = tokio::spawn(runtime.run(rx));

        let (stage_io, exec_io) = tokio::io::duplex(4 << 20);
        let hello = executor.hello(false, "runc");
        let handler = ConnectorLinkHandler::new(Arc::clone(&executor));
        let exec_conn = tokio::spawn(async move {
            let _ = run_connection(
                exec_io,
                hello,
                handler,
                "connector-acceptance",
                Heartbeat::disabled(),
            )
            .await;
        });
        link.attach(stage_io).await.expect("host-api handshake");
        Self {
            registry,
            tok,
            active,
            ready,
            stop: Some(stop),
            task: Some(task),
            exec_conn,
        }
    }

    async fn shutdown(&mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        if let Some(task) = self.task.take() {
            tokio::time::timeout(Duration::from_secs(20), task)
                .await
                .expect("runtime stops")
                .unwrap();
        }
        assert!(self.active.is_empty(), "shutdown empties the active table");
        self.exec_conn.abort();
    }
}

fn is_uuid(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_hexdigit(),
        })
}

/// One decoded stream entry (the `env` field's JSON).
struct Decoded {
    env: serde_json::Value,
}

impl Decoded {
    fn of(entry: &Entry) -> Self {
        let raw: Vec<u8> = entry
            .1
            .iter()
            .flat_map(|(f, v)| f.bytes().chain(v.iter().copied()))
            .collect();
        let hay = String::from_utf8_lossy(&raw).to_lowercase();
        for bad in FORBIDDEN {
            assert!(
                !hay.contains(bad),
                "raw value `{bad}` leaked into stream entry {}",
                entry.0
            );
        }
        let (_, body) = entry
            .1
            .iter()
            .find(|(f, _)| f == "env")
            .expect("entry carries the `env` field");
        Self {
            env: serde_json::from_slice(body).expect("entry is JSON"),
        }
    }

    fn s(&self, ptr: &str) -> &str {
        self.env
            .pointer(ptr)
            .and_then(serde_json::Value::as_str)
            .unwrap_or_else(|| panic!("missing string at {ptr}"))
    }

    /// Decrypts an identity envelope field (`/event/actor` or a payload field).
    async fn decrypt(&self, key: &str, field: &str, ptr: &str) -> String {
        let env: JsonEnvelope =
            serde_json::from_str(self.s(ptr)).expect("identity field is an envelope");
        let tenant = self.s("/tenant");
        let dek = LocalDevDekProvider::new("local", KEK_ENV)
            .unwrap()
            .get_dek(tenant)
            .await
            .expect("dek");
        decrypt_identity_value(&env, &dek.0, tenant, key, field, self.s("/event_id"))
            .expect("identity field decrypts")
    }
}

/// Asserts the acceptance contract for every entry of one platform's streams
/// and returns how many entries were examined (the denominator).
async fn assert_entries(
    ctx: &Ctx,
    slug: &str,
    platform: &'static str,
    ids: &[&str],
    min: usize,
) -> usize {
    let keys = ctx.keys(slug, platform).await;
    assert!(
        !keys.is_empty(),
        "{platform}: no stream under the resolved slug"
    );
    let mut examined = 0;
    for key in &keys {
        assert!(
            key.starts_with(&format!("waddles:t:{slug}:c:main:src:{platform}:")),
            "{platform}: stream key `{key}` is not under the resolved tenant/community"
        );
        for entry in ctx.entries(key).await {
            let d = Decoded::of(&entry);
            // Correct scope: resolved tenant slug + numeric ids from hub-api.
            assert_eq!(d.s("/tenant"), slug);
            assert_eq!(d.s("/community"), "main");
            assert_eq!(d.env["tenant_id"], 5);
            assert_eq!(d.env["community_id"], 7);
            assert_eq!(d.s("/event/platform"), platform);
            assert_eq!(d.s("/event/event_type"), "message");
            // Tokenized: the actor decrypts to the hub's pseudonym UUID of one
            // of the sent platform ids, never the id/handle itself.
            let actor = d.decrypt(key, "actor", "/event/actor").await;
            assert!(is_uuid(&actor), "{platform}: actor is not a UUID: {actor}");
            let want: Vec<String> = ids.iter().map(|i| pseudonym_uuid(platform, i)).collect();
            assert!(
                want.contains(&actor),
                "{platform}: actor {actor} not tokenized by the hub"
            );
            // Identity-bearing text/names are envelopes, not clear.
            for f in ["display_name", "text"] {
                let plain = d.decrypt(key, f, &format!("/event/payload/{f}")).await;
                assert!(!plain.is_empty(), "{platform}: {f} decrypts to something");
            }
            // The residual clear ids are exactly the pinned set.
            let payload = d.env["event"]["payload"]
                .as_object()
                .expect("payload object");
            let clear: Vec<&str> = payload
                .iter()
                .filter(|(_, v)| v.as_str().is_some_and(|x| ids.contains(&x)))
                .map(|(k, _)| k.as_str())
                .collect();
            assert!(
                !clear.is_empty(),
                "{platform}: expected the pinned clear id field"
            );
            for f in clear {
                assert!(
                    CLEAR_ID_FIELDS.contains(&f),
                    "{platform}: new clear id field `{f}`"
                );
            }
            examined += 1;
        }
    }
    assert!(
        examined >= min,
        "{platform}: examined {examined} entries, want >= {min}"
    );
    examined
}

/// Every forbidden value is absent from every captured log line, and the
/// capture itself is non-empty (a dead sink would pass vacuously).
fn assert_logs_clean() {
    let log = logged();
    assert!(
        log.lines().count() >= 5,
        "log capture is empty: {} lines",
        log.lines().count()
    );
    for bad in FORBIDDEN
        .iter()
        .copied()
        .chain([TW_USER_ID, TW_USER_ID_2, YT_AUTHOR, YT_AUTHOR_2])
    {
        assert!(
            !log.contains(&bad.to_lowercase()),
            "`{bad}` leaked into a log line"
        );
    }
}

/// The decrypted, volatile-free event the connector path hands downstream for
/// one message id -- the contract `core/svc_process`'s reputation accrual
/// (`test_connector_event_accrual_contract.py`) consumes from the SAME fixture.
async fn contract_event(
    ctx: &Ctx,
    slug: &str,
    platform: &str,
    message_id: &str,
) -> serde_json::Value {
    for key in ctx.keys(slug, platform).await {
        for entry in ctx.entries(&key).await {
            let d = Decoded::of(&entry);
            if d.s("/event/payload/message_id") != message_id {
                continue;
            }
            let mut ev = d.env["event"].clone();
            ev["actor"] = d.decrypt(&key, "actor", "/event/actor").await.into();
            for f in ["display_name", "text", "author"] {
                let ptr = format!("/event/payload/{f}");
                if d.env.pointer(&ptr).is_some() {
                    ev["payload"][f] = d.decrypt(&key, f, &ptr).await.into();
                }
            }
            let o = ev.as_object_mut().expect("event object");
            o.remove("occurred_at");
            o.remove("source");
            ev["payload"]
                .as_object_mut()
                .expect("payload")
                .remove("published_at");
            return ev;
        }
    }
    panic!("{platform}: no entry for message {message_id}");
}

fn hub_for(ctx: &Ctx) -> Arc<FakeHub> {
    Arc::new(FakeHub {
        tw_conn: ctx.tw_conn,
        yt_conn: ctx.yt_conn,
        list_calls: AtomicUsize::new(0),
    })
}

fn two_viewers() -> Vec<String> {
    vec![
        privmsg("acceptviewer1", TW_USER_ID, "m-1", "hello acceptance chat"),
        privmsg(
            "acceptviewer2",
            TW_USER_ID_2,
            "m-2",
            "second acceptance line",
        ),
    ]
}

async fn both_published(rig: &Rig, min: f64) {
    eventually("both platforms publish", || {
        published(&rig.registry, "twitch") >= min && published(&rig.registry, "youtube") >= min
    })
    .await;
}

#[derive(Default)]
struct Counting {
    twitch_inbound: usize,
    discord_inbound: usize,
    twitch_outbound: usize,
    discord_outbound: usize,
    twitch_eventsub: usize,
}

impl LegacyStarters for Counting {
    fn twitch_inbound(&mut self) -> anyhow::Result<()> {
        self.twitch_inbound += 1;
        Ok(())
    }
    fn discord_inbound(&mut self) {
        self.discord_inbound += 1;
    }
    fn twitch_outbound(&mut self) -> anyhow::Result<()> {
        self.twitch_outbound += 1;
        Ok(())
    }
    fn discord_outbound(&mut self) {
        self.discord_outbound += 1;
    }
    async fn twitch_eventsub(&mut self) {
        self.twitch_eventsub += 1;
    }
}

/// Discord ON is refused at boot while the identify budget is unported; the
/// legacy starters of the ON platforms are NOT called, the others still are.
#[tokio::test]
async fn discord_is_refused_and_legacy_is_suppressed_for_the_on_platforms() {
    let env = |pairs: &'static [(&'static str, &'static str)]| {
        move |k: &str| {
            pairs
                .iter()
                .find(|(n, _)| *n == k)
                .map(|(_, v)| (*v).to_owned())
        }
    };
    let discord = OverlaySelector::from_env(&env(&[
        ("OVERLAY_TWITCH_ENABLED", "true"),
        ("OVERLAY_YOUTUBE_ENABLED", "true"),
        ("OVERLAY_DISCORD_ENABLED", "true"),
    ]));
    assert_eq!(
        discord.is_ok(),
        IDENTIFY_BUDGET_READY,
        "Discord ON must be refused exactly while IDENTIFY_BUDGET_READY is false"
    );
    if !IDENTIFY_BUDGET_READY {
        assert!(
            matches!(discord, Err(BootError::Blocked(m)) if m.contains("identify budget")),
            "refusal must name the identify budget: {discord:?}"
        );
    }
    let malformed = OverlaySelector::from_env(&env(&[("OVERLAY_TWITCH_ENABLED", "yes")]));
    assert_eq!(
        malformed,
        Err(BootError::Malformed("OVERLAY_TWITCH_ENABLED"))
    );

    let sel = OverlaySelector::from_env(&env(&[
        ("OVERLAY_TWITCH_ENABLED", "true"),
        ("OVERLAY_YOUTUBE_ENABLED", "true"),
    ]))
    .expect("twitch + youtube ON is accepted");
    assert_eq!(sel.enabled_platforms(), vec!["twitch", "youtube"]);
    assert!(sel.overlay("twitch") && sel.overlay("youtube") && !sel.overlay("discord"));
    let mut starters = Counting::default();
    start_legacy(&LegacyPlan::new(&sel, &[], false), &mut starters)
        .await
        .expect("legacy start");
    assert_eq!(
        starters.twitch_inbound, 0,
        "legacy Twitch inbound must be suppressed"
    );
    assert_eq!(
        starters.discord_inbound, 1,
        "Discord stays on the legacy path"
    );
    assert_eq!(starters.discord_outbound, 1);
    assert_eq!(
        starters.twitch_outbound, 1,
        "outbound is governed by CONNECTOR_TOOLS_ENABLED"
    );
    assert_eq!(
        starters.twitch_eventsub, 1,
        "EventSub stays legacy without a runtime webhook"
    );

    let off = OverlaySelector::from_env(&env(&[])).expect("default OFF");
    let mut legacy = Counting::default();
    start_legacy(&LegacyPlan::new(&off, &[], false), &mut legacy)
        .await
        .expect("legacy start");
    assert_eq!(
        (legacy.twitch_inbound, legacy.discord_inbound),
        (1, 1),
        "all OFF keeps every legacy starter"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn both_platforms_ingest_tokenize_publish_and_park_a_tenant_mismatch() {
    let ctx = Ctx::new(1, false, two_viewers(), vec![]).await;
    let hub = hub_for(&ctx);
    let mut rig = Rig::boot(&ctx, Arc::clone(&hub)).await;
    both_published(&rig, 2.0).await;

    // Ingested -> tokenized -> published under the RESOLVED scope, per platform.
    let tw = assert_entries(&ctx, &ctx.tw_slug, "twitch", &[TW_USER_ID, TW_USER_ID_2], 2).await;
    let yt = assert_entries(&ctx, &ctx.yt_slug, "youtube", &[YT_AUTHOR, YT_AUTHOR_2], 2).await;
    assert!(tw + yt >= 4, "entries examined: {}", tw + yt);

    // Contract fixture shared with the svc_process reputation-accrual test: the
    // event the downstream consumes is exactly this, per platform.
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/accrual_contract.json")).expect("fixture");
    for (platform, slug, msg) in [
        ("twitch", &ctx.tw_slug, "m-1"),
        ("youtube", &ctx.yt_slug, "yt-m-1"),
    ] {
        let got = contract_event(&ctx, slug, platform, msg).await;
        assert_eq!(
            got, fixture[platform],
            "{platform}: connector event drifted from tests/fixtures/accrual_contract.json; actual:\n{got:#}"
        );
    }

    // Host-calls ran under each connection's OWN scope; allowlist served them.
    {
        let seen = rig.tok.seen.lock().unwrap();
        for (platform, conn) in [("twitch", ctx.tw_conn), ("youtube", ctx.yt_conn)] {
            let mine: Vec<_> = seen.iter().filter(|s| s.platform == platform).collect();
            assert!(!mine.is_empty(), "{platform}: identity.lookup was served");
            for s in mine {
                assert_eq!((s.community_id, s.tenant_id, s.connection_id), (7, 5, conn));
            }
        }
    }
    assert!(svc_ingest::connector::host_calls::call_count("db", "ok") >= 1);
    assert!(
        hub.list_calls.load(Ordering::SeqCst) >= 2,
        "both providers listed"
    );

    // Tenant mismatch: parked, never dialed/polled, never active, never published.
    for p in ["twitch", "youtube"] {
        eventually("mismatched connection parked", || {
            counter(
                &rig.registry,
                "connector_runtime_parked_total",
                &[("platform", p), ("reason", "tenant_mismatch")],
            ) >= 1.0
        })
        .await;
        assert_eq!(
            counter(&rig.registry, "connector_runtime_live", &[("platform", p)]),
            1.0
        );
        assert_eq!(
            rig.ready.ready(p),
            Readiness::Ready {
                live: 1,
                desired: 2
            },
            "{p} readiness"
        );
    }
    assert_eq!(
        ctx.irc_accepted.load(Ordering::SeqCst),
        1,
        "only the honest IRC dialed"
    );
    assert_eq!(
        rig.active.len(),
        2,
        "one honest connection per platform is active"
    );
    let reqs = ctx.api.received_requests().await.unwrap_or_default();
    assert!(!reqs.is_empty(), "the fake YouTube API was polled");
    for r in &reqs {
        let q = r.url.query().unwrap_or_default();
        assert!(q.contains(YT_CHAT), "polled the honest chat: {q}");
        assert!(
            !q.contains(YT_OTHER_CHAT),
            "the mismatched chat was polled: {q}"
        );
    }

    assert_logs_clean();
    rig.shutdown().await;
    ctx.cleanup().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_runtime_restart_resumes_both_platforms() {
    let later = vec![privmsg(
        "acceptviewer1",
        TW_USER_ID,
        "m-after-restart",
        "hello acceptance chat",
    )];
    let ctx = Ctx::new(2, false, two_viewers(), later).await;
    let hub = hub_for(&ctx);

    let mut first = Rig::boot(&ctx, Arc::clone(&hub)).await;
    both_published(&first, 2.0).await;
    let before_tw = ctx.keys(&ctx.tw_slug, "twitch").await;
    let mut tw_before = 0;
    for k in &before_tw {
        tw_before += ctx.entries(k).await.len();
    }
    assert!(tw_before >= 2, "twitch entries before restart: {tw_before}");
    let polls_before = ctx.api.received_requests().await.unwrap_or_default().len();
    first.shutdown().await;

    // A fresh runtime + executor over the SAME spine/Valkey: it must re-acquire
    // the leases the first one released and ingest again.
    let mut second = Rig::boot(&ctx, hub).await;
    both_published(&second, 1.0).await;
    let mut tw_after = 0;
    for k in &ctx.keys(&ctx.tw_slug, "twitch").await {
        tw_after += ctx.entries(k).await.len();
    }
    assert!(
        tw_after > tw_before,
        "twitch resumed: {tw_before} -> {tw_after}"
    );
    assert!(
        ctx.irc_accepted.load(Ordering::SeqCst) >= 2,
        "twitch re-dialed after restart"
    );
    assert!(
        ctx.api.received_requests().await.unwrap_or_default().len() > polls_before,
        "youtube polling resumed after restart"
    );
    assert_entries(&ctx, &ctx.tw_slug, "twitch", &[TW_USER_ID, TW_USER_ID_2], 3).await;
    assert_entries(&ctx, &ctx.yt_slug, "youtube", &[YT_AUTHOR, YT_AUTHOR_2], 2).await;
    assert_logs_clean();
    second.shutdown().await;
    ctx.cleanup().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_dropped_twitch_socket_reconnects_and_keeps_ingesting() {
    let first = vec![privmsg(
        "acceptviewer1",
        TW_USER_ID,
        "m-1",
        "hello acceptance chat",
    )];
    let later = vec![privmsg(
        "acceptviewer2",
        TW_USER_ID_2,
        "m-9",
        "second acceptance line",
    )];
    let ctx = Ctx::new(3, true, first, later).await;
    let mut rig = Rig::boot(&ctx, hub_for(&ctx)).await;
    eventually("twitch reconnects and publishes both messages", || {
        ctx.irc_accepted.load(Ordering::SeqCst) >= 2 && published(&rig.registry, "twitch") >= 2.0
    })
    .await;
    eventually("connection live again", || {
        counter(
            &rig.registry,
            "connector_runtime_live",
            &[("platform", "twitch")],
        ) == 1.0
    })
    .await;
    assert_entries(&ctx, &ctx.tw_slug, "twitch", &[TW_USER_ID, TW_USER_ID_2], 2).await;
    assert_logs_clean();
    rig.shutdown().await;
    ctx.cleanup().await;
}
