//! Flag-ON acceptance of the Discord platform connector SERVICE bundle.
//!
//! One boot composes the production runtime (`assemble`, the settings the
//! production `RuntimeSettings::from_inputs` builds from the env lookup) with
//! Discord ON and `CONNECTOR_HEARTBEAT_ENABLED=true`, the REAL compiled Discord
//! bundle (version 1.0.1, token-first IDENTIFY) pinned in a REAL bundle executor
//! over the host-API wire, a REAL Valkey (spine + the REAL `RedisIdentifyBudget`
//! behind the production `DiscordGateFactory`), a fake Discord gateway
//! (HELLO / IDENTIFY / READY / RESUME / heartbeat ACK / op 7 / op 9 / close 4004),
//! a fake `GET /gateway/bot` and a fake hub-api (bindings, scope authority,
//! identity tokenizer).
//!
//! Proven: a `MESSAGE_CREATE` is ingested, tokenized (the actor decrypts to the
//! hub's pseudonym UUID, no raw username/text in any stream byte or log line)
//! and published under the RESOLVED tenant/community; the bot's own message is
//! dropped; every RESUME skips the budget while every IDENTIFY consumes exactly
//! one slot; two connections of one bot are serialized one IDENTIFY per 5 s
//! bucket; an exhausted budget and a dead Valkey send NO IDENTIFY (fail
//! closed); the legacy Discord receiver is suppressed. Every assertion has a
//! non-zero denominator. Needs `VALKEY_URL` (unset FAILS, never skips).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{BTreeMap, HashSet};
use std::io::Write;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use bundle_executor::config::CliConfig;
use bundle_executor::connector::ConnectorLimits;
use bundle_executor::connector_boot::CONNECTOR_PLATFORMS;
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
use svc_ingest::connector::identify_budget::{BotIdentity, DISCORD_API_BASE, IDENTIFY_WINDOW};
use svc_ingest::connector::secrets::{Destination, ResolvedAuth};
use svc_ingest::connector::webhook::{BoxFut, SecretError};
use svc_ingest::connector::{TransportError, TransportSecretSource};
use svc_ingest::connector_runtime::boot::{
    assemble, AssembleDeps, HostBridge, IdentifyBudgetBackend, LoopbackSeams, RuntimeSettings,
};
use svc_ingest::connector_runtime::feed::{DesiredConnection, DynFeed, FeedError, TransportPlan};
use svc_ingest::connector_runtime::profiles::discord::GATEWAY_URL;
use svc_ingest::connector_runtime::scoped::ScopeLookup;
use svc_ingest::connector_runtime::selector::{
    start_legacy, ConnectorSelector, LegacyPlan, LegacyStarters, IDENTIFY_BUDGET_READY,
};
use svc_ingest::identity_crypto::{
    decrypt_identity_value, DekProvider, JsonEnvelope, LocalDevDekProvider,
};
use svc_ingest::scope_resolver::{ResolvedScope, Unresolved};
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;
use wiremock::matchers::{header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const DISCORD_WASM: &[u8] =
    include_bytes!("../../bundle_executor/tests/fixtures/discord_connector.wasm");
const KEK_ENV: &str = "DISCORD_ACCEPTANCE_KEK";
const GUILD: &str = "123456789012345678";
const GUILD_2: &str = "223456789012345678";
const BOT_ID: &str = "999999999999999999";
const FOREIGN_ID: &str = "444444444444444444";
const FOREIGN_ID_2: &str = "444444444444444445";
/// Heartbeat cadence the fake HELLO announces (the host owns the heartbeat).
const HEARTBEAT_MS: u64 = 200;
/// Tokens whose clear value must never reach a stream byte or a log line.
const FORBIDDEN: [&str; 3] = [
    "acceptdiscorduser",
    "hello discord acceptance",
    "second discord line",
];

/// Collects every tracing event the process emits so no raw username/text can
/// hide in a log line.
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

/// One Discord binding of the fake hub-api.
struct Bind {
    community: i64,
    connection: i64,
    guild: &'static str,
}

/// hub-api's binding list for the Discord provider.
struct FakeHub {
    binds: Vec<Bind>,
}

impl ConnectionResolve for FakeHub {
    async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
        Ok(ResolvedConnection {
            connection_id: r.connection_id,
            community_id: r.community_id,
            provider: "discord".into(),
            kind: ConnectionKind::PlatformOauth,
            endpoint: Endpoint::default(),
            secret: ConnectionSecret::OAuthAccessToken {
                access_token: SecretString::new("unused-the-seam-serves-credentials".to_owned()),
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
        assert_eq!(provider, "discord", "only the discord connector is ON");
        assert_eq!(capability, Some("chat_ingest"));
        let items = if after == 0 {
            self.binds
                .iter()
                .map(|b| BindingSummary {
                    community_id: b.community,
                    connection_id: b.connection,
                    tenant_id: 5,
                    secret_version: 1,
                    provider: provider.to_owned(),
                    kind: ConnectionKind::PlatformOauth,
                    metadata: BTreeMap::from([("guild_id".to_owned(), b.guild.to_owned())]),
                })
                .collect()
        } else {
            vec![]
        };
        Ok(BindingPage {
            next_after: None,
            items,
        })
    }
}

/// Scope authority: each guild resolves to its community under one tenant slug.
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
            ("discord", GUILD) => ResolvedScope::validated(5, 7, &self.slug, Some("main")),
            ("discord", GUILD_2) => ResolvedScope::validated(5, 8, &self.slug, Some("main")),
            _ => return Box::pin(async { Err(Unresolved::NotFound) }),
        };
        Box::pin(async move { r.map_err(|_| Unresolved::Invalid) })
    }
}

/// The hub identity service: deterministic pseudonym per platform user id,
/// recording every (scope, id) it was asked about.
#[derive(Default)]
struct FakeTok {
    seen: Mutex<Vec<(CallScope, String)>>,
}

impl IdentityTokenizer for FakeTok {
    fn tokenize<'a>(
        &'a self,
        scope: &'a CallScope,
        platform_user_id: &'a str,
    ) -> BoxFut<'a, Result<String, TokenizeError>> {
        self.seen
            .lock()
            .unwrap()
            .push((scope.clone(), platform_user_id.to_owned()));
        let uuid = pseudonym_uuid(scope.platform, platform_user_id);
        Box::pin(async move { Ok(uuid) })
    }
}

/// Pass-only bot credential (the real source refuses loopback hosts); serves
/// BOTH the gateway handshake slot and the `GET /gateway/bot` token.
struct BotToken(String);

impl TransportSecretSource for BotToken {
    fn resolve(
        &self,
        r: ConnectionRef,
        _dest: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
        let token = self.0.clone();
        Box::pin(async move {
            if r.connection_id < 1 {
                return Err(SecretError::Missing);
            }
            Ok(ResolvedAuth {
                secret: SecretString::new(token),
                header: None,
            })
        })
    }
}

struct Creds(String);

impl svc_ingest::connector_runtime::SecretsFactory for Creds {
    fn for_connection(
        &self,
        _desired: &DesiredConnection,
    ) -> Result<Arc<dyn TransportSecretSource>, TransportError> {
        Ok(Arc::new(BotToken(self.0.clone())))
    }
}

/// Rewrites the host-constant gateway URL to the loopback fake and lets the
/// captured `resume_gateway_url` point at it (the only test seam; production
/// never constructs this).
struct Rewrite {
    inner: Arc<dyn DynFeed>,
    gateway: SocketAddr,
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
                let TransportPlan::Ws(cfg) = &mut d.plan else {
                    panic!("the discord profile plans a websocket");
                };
                assert_eq!(cfg.url, GATEWAY_URL, "host constant");
                cfg.url = format!("ws://{}/", self.gateway);
                cfg.allow_loopback_insecure = true;
                cfg.session
                    .reconnect_url
                    .as_mut()
                    .expect("discord captures a resume url")
                    .allowed_hosts = vec!["127.0.0.1".into()];
            }
            Ok(out)
        })
    }
}

/// What the fake gateway observed on its wire.
#[derive(Default)]
struct Wire {
    /// Sockets accepted on the main gateway (each got a HELLO).
    gw_sockets: usize,
    /// IDENTIFY texts in arrival order, with their arrival instant.
    identifies: Vec<(Instant, String)>,
    /// RESUME texts in arrival order.
    resumes: Vec<String>,
    /// Heartbeats answered with an ACK.
    heartbeats: usize,
}

/// What the resume host plays after a RESUME, by resume index.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Script {
    /// n0: RESUMED, the bot's own message, a foreign message, op 7.
    /// n1: op 9 d=false (session dead -> a fresh IDENTIFY).
    /// n2: RESUMED, a foreign message, close 4004 (park).
    Lifecycle,
    /// RESUMED, then hold the connection open (heartbeats ACKed).
    Hold,
}

fn dispatch(t: &str, s: u64, d: &serde_json::Value) -> Message {
    Message::text(serde_json::json!({"op": 0, "t": t, "s": s, "d": d}).to_string())
}

fn message_create(id: &str, author: &str, text: &str) -> serde_json::Value {
    serde_json::json!({
        "id": id, "channel_id": "222", "guild_id": GUILD, "content": text,
        "author": {"id": author, "username": "acceptdiscorduser", "bot": false},
        "type": 0
    })
}

fn op_of(text: &str) -> i64 {
    match serde_json::from_str::<serde_json::Value>(text) {
        Ok(v) => v["op"]
            .as_i64()
            .expect("every client frame carries an integer op"),
        Err(e) => panic!("client frame is not JSON: {e}"),
    }
}

async fn hello_and_next(
    ws: &mut tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>,
) -> bool {
    ws.send(Message::text(format!(
        r#"{{"op":10,"d":{{"heartbeat_interval":{HEARTBEAT_MS}}}}}"#
    )))
    .await
    .is_ok()
}

/// The main gateway: HELLO, ACK every heartbeat, and on IDENTIFY answer READY
/// (session `sess{n}`, resume url, the bot's user id) then hold until closed.
async fn gateway(l: TcpListener, resume: SocketAddr, wire: Arc<Mutex<Wire>>) {
    loop {
        let Ok((tcp, _)) = l.accept().await else {
            return;
        };
        let wire = Arc::clone(&wire);
        tokio::spawn(async move {
            let Ok(mut ws) = tokio_tungstenite::accept_async(tcp).await else {
                return;
            };
            wire.lock().unwrap().gw_sockets += 1;
            if !hello_and_next(&mut ws).await {
                return;
            }
            while let Some(Ok(msg)) = ws.next().await {
                let Message::Text(t) = msg else { continue };
                match op_of(&t) {
                    1 => {
                        wire.lock().unwrap().heartbeats += 1;
                        let _ = ws.send(Message::text(r#"{"op":11}"#)).await;
                    }
                    2 => {
                        let n = {
                            let mut w = wire.lock().unwrap();
                            w.identifies.push((Instant::now(), t.to_string()));
                            w.identifies.len()
                        };
                        let ready = serde_json::json!({
                            "session_id": format!("sess{n}"),
                            "resume_gateway_url": format!("ws://{resume}"),
                            "user": {"id": BOT_ID}
                        });
                        if ws.send(dispatch("READY", 1, &ready)).await.is_err() {
                            return;
                        }
                    }
                    _ => {}
                }
            }
        });
    }
}

/// The resume host (every RESUME lands here, never on the main gateway).
async fn resume_host(l: TcpListener, script: Script, wire: Arc<Mutex<Wire>>) {
    loop {
        let Ok((tcp, _)) = l.accept().await else {
            return;
        };
        let wire = Arc::clone(&wire);
        tokio::spawn(async move {
            let Ok(mut ws) = tokio_tungstenite::accept_async(tcp).await else {
                return;
            };
            if !hello_and_next(&mut ws).await {
                return;
            }
            // The final lifecycle step closes with 4004 only after the host has
            // heartbeated (and been ACKed) on this connection.
            let mut close_after_heartbeat = false;
            while let Some(Ok(msg)) = ws.next().await {
                let Message::Text(t) = msg else { continue };
                match op_of(&t) {
                    1 => {
                        wire.lock().unwrap().heartbeats += 1;
                        let _ = ws.send(Message::text(r#"{"op":11}"#)).await;
                        if close_after_heartbeat {
                            let _ = ws
                                .send(Message::Close(Some(CloseFrame {
                                    code: CloseCode::from(4004),
                                    reason: "auth".into(),
                                })))
                                .await;
                        }
                    }
                    6 => {
                        let n = {
                            let mut w = wire.lock().unwrap();
                            w.resumes.push(t.to_string());
                            w.resumes.len() - 1
                        };
                        close_after_heartbeat = play(&mut ws, script, n).await;
                    }
                    _ => {}
                }
            }
        });
    }
}

async fn play(
    ws: &mut tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>,
    script: Script,
    n: usize,
) -> bool {
    let resumed = dispatch("RESUMED", 2, &serde_json::json!({}));
    match (script, n) {
        (Script::Lifecycle, 0) => {
            let _ = ws.send(resumed).await;
            // The bot's OWN message first (must be dropped), then a foreign one.
            let own = message_create("551", BOT_ID, "own message");
            let foreign = message_create("552", FOREIGN_ID, "hello discord acceptance");
            let _ = ws.send(dispatch("MESSAGE_CREATE", 3, &own)).await;
            let _ = ws.send(dispatch("MESSAGE_CREATE", 4, &foreign)).await;
            let _ = ws.send(Message::text(r#"{"op":7,"d":null}"#)).await;
        }
        (Script::Lifecycle, 1) => {
            let _ = ws.send(Message::text(r#"{"op":9,"d":false}"#)).await;
        }
        (Script::Lifecycle, _) => {
            let _ = ws.send(resumed).await;
            let foreign = message_create("553", FOREIGN_ID_2, "second discord line");
            let _ = ws.send(dispatch("MESSAGE_CREATE", 3, &foreign)).await;
            // Close with 4004 once the host's next heartbeat arrives (the read
            // loop keeps ACKing, so the host never sees a zombie connection).
            return true;
        }
        (Script::Hold, _) => {
            let _ = ws.send(resumed).await;
        }
    }
    false
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

fn budget(reg: &prometheus::Registry, outcome: &str) -> f64 {
    counter(
        reg,
        "connector_identify_budget_total",
        &[("outcome", outcome)],
    )
}

fn published(reg: &prometheus::Registry) -> f64 {
    counter(
        reg,
        "svc_ingest_connector_events_total",
        &[("platform", "discord"), ("outcome", "published")],
    )
}

async fn eventually(what: &str, cond: impl Fn() -> bool) {
    for _ in 0..800 {
        if cond() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("timed out waiting for: {what}");
}

type Entry = (String, Vec<(String, Vec<u8>)>);

/// Everything one test run shares: identity, the fakes and the real Valkey.
struct Ctx {
    valkey: String,
    slug: String,
    token: String,
    conn: i64,
    gw: SocketAddr,
    wire: Arc<Mutex<Wire>>,
    /// Fake `GET /gateway/bot`.
    api: MockServer,
}

impl Ctx {
    /// Binds the fakes. `n` keeps parallel tests' slugs/tokens/connection ids
    /// disjoint (the budget keys hash the token, so each test owns its budget).
    /// `remaining` is what `GET /gateway/bot` reports.
    async fn new(n: i64, script: Script, remaining: u32) -> Self {
        logs();
        let valkey = std::env::var("VALKEY_URL")
            .expect("VALKEY_URL must be set: this e2e needs a REAL Valkey (no silent skip)");
        std::env::set_var("SECURITY_TRANSPORT_TLS", "false");
        std::env::set_var("SECURITY_TRANSPORT_AUTH", "false");
        std::env::set_var(KEK_ENV, "0123456789abcdef0123456789abcdef");
        let run = std::process::id();
        let token = format!("bot-token-dc{n}-{run}");
        let gw_l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let resume_l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let gw = gw_l.local_addr().unwrap();
        let wire = Arc::new(Mutex::new(Wire::default()));
        tokio::spawn(gateway(
            gw_l,
            resume_l.local_addr().unwrap(),
            Arc::clone(&wire),
        ));
        tokio::spawn(resume_host(resume_l, script, Arc::clone(&wire)));
        let api = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/gateway/bot"))
            .and(header("authorization", format!("Bot {token}").as_str()))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "url": "wss://gateway.discord.gg",
                "shards": 1,
                "session_start_limit": {
                    "total": 1000, "remaining": remaining,
                    "reset_after": 86_400_000, "max_concurrency": 1
                }
            })))
            .mount(&api)
            .await;
        Self {
            valkey,
            slug: format!("accdc{n}x{run}"),
            token,
            conn: 60_000 + i64::from(run % 20_000) * 40 + n * 4,
            gw,
            wire,
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

    fn limit_key(&self) -> String {
        format!(
            "waddles:connector:identify-budget:discord:{}:limit",
            BotIdentity::from_token(&self.token).as_hex()
        )
    }

    /// The stored `remaining` daily IDENTIFY counter for this test's bot.
    async fn remaining(&self) -> i64 {
        let mut kc = self.kc().await;
        let v: Option<i64> = redis::cmd("HGET")
            .arg(self.limit_key())
            .arg("remaining")
            .query_async(&mut kc)
            .await
            .unwrap();
        v.expect("the budget was seeded")
    }

    async fn keys(&self) -> Vec<String> {
        let mut kc = self.kc().await;
        let all: Vec<String> = redis::cmd("KEYS")
            .arg(format!("*{}*", self.slug))
            .query_async(&mut kc)
            .await
            .unwrap();
        all.into_iter()
            .filter(|k| k.contains(":discord:"))
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
        let hex = BotIdentity::from_token(&self.token);
        let mut keys: Vec<String> = redis::cmd("KEYS")
            .arg(format!("*{}*", self.slug))
            .query_async(&mut kc)
            .await
            .unwrap();
        keys.extend(
            redis::cmd("KEYS")
                .arg(format!(
                    "waddles:connector:identify-budget:discord:{}:*",
                    hex.as_hex()
                ))
                .query_async::<Vec<String>>(&mut kc)
                .await
                .unwrap(),
        );
        if !keys.is_empty() {
            let _: () = redis::cmd("DEL")
                .arg(&keys)
                .query_async(&mut kc)
                .await
                .unwrap();
        }
    }
}

/// One booted runtime + executor.
struct Rig {
    registry: prometheus::Registry,
    tok: Arc<FakeTok>,
    active: Arc<svc_ingest::connector::host_calls::ActiveConnections>,
    stop: Option<oneshot::Sender<()>>,
    task: Option<tokio::task::JoinHandle<()>>,
    exec_conn: tokio::task::JoinHandle<()>,
}

async fn pin(executor: &Executor<NoSource>, cfg: &CliConfig) -> String {
    let digest = digest_of(DISCORD_WASM);
    let p = CONNECTOR_PLATFORMS
        .iter()
        .find(|p| p.platform == "discord")
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
            DISCORD_WASM.to_vec(),
            ConnectorLimits::from_config(cfg),
            None,
        )
        .await
        .expect("the real Discord connector service bundle pins");
    digest
}

/// The production env lookup for Discord ON (heartbeat baseline included).
fn discord_env(digest: &str) -> BTreeMap<&'static str, String> {
    [
        ("CONNECTOR_DISCORD_ENABLED", "true".to_owned()),
        ("CONNECTOR_HEARTBEAT_ENABLED", "true".to_owned()),
        ("SENDER_EXEC_DIGEST_DISCORD", digest.to_owned()),
    ]
    .into_iter()
    .collect()
}

impl Rig {
    /// Boots the production runtime. `budget_conn` is the Valkey connection the
    /// identify budget uses (tests hand in a healthy one or a dead one).
    async fn boot(
        ctx: &Ctx,
        binds: Vec<Bind>,
        budget_conn: redis::aio::MultiplexedConnection,
    ) -> Self {
        let cfg = CliConfig::try_parse_from([
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-ingest:8301",
        ])
        .unwrap();
        let executor = Arc::new(Executor::new(&cfg, NoSource).unwrap());
        let digest = pin(&executor, &cfg).await;

        let env = discord_env(&digest);
        let lookup = |k: &str| env.get(k).cloned();
        let selector = ConnectorSelector::from_env(&lookup)
            .expect("Discord ON is accepted now the identify budget is wired");
        let ingest_cli = svc_ingest::config::CliConfig::parse_from(["svc-ingest"]);
        let mut settings = RuntimeSettings::from_inputs(&ingest_cli, &selector, &lookup)
            .expect("production settings build from the env lookup");
        assert_eq!(settings.platforms, vec!["discord"]);
        assert!(
            settings.heartbeat_enabled,
            "CONNECTOR_HEARTBEAT_ENABLED=true reaches the settings"
        );
        // Loopback-only deltas (production leaves these at config defaults).
        settings.reconcile_interval = Duration::from_secs(1);
        settings.egress_allow = vec![IpAddr::V4(Ipv4Addr::LOCALHOST)];
        settings.discord_pace = Duration::ZERO;

        let tok = Arc::new(FakeTok::default());
        let bridge = HostBridge::new(tok.clone(), &["discord"]).unwrap();
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
        let gateway = ctx.gw;
        let runtime = assemble(
            &settings,
            AssembleDeps {
                resolver: Arc::new(FakeHub { binds }),
                scope: Arc::new(FakeScope {
                    slug: ctx.slug.clone(),
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
                    prometheus::Opts::new("discord_acceptance_identity_total", "test"),
                    &["tenant", "status"],
                )
                .unwrap(),
                lease_conn,
                identify_budget: Some(IdentifyBudgetBackend {
                    conn: budget_conn,
                    // The fake /gateway/bot; production is DISCORD_API_BASE.
                    api_base: ctx.api.uri(),
                    window: IDENTIFY_WINDOW,
                }),
                bridge,
                webhook: None,
                registry: registry.clone(),
                seams: LoopbackSeams {
                    feed_wrap: Some(Arc::new(move |_p, inner| {
                        Arc::new(Rewrite { inner, gateway }) as _
                    })),
                    secrets: Some(Arc::new(Creds(ctx.token.clone()))),
                },
            },
        )
        .expect("the production assembly builds with the budget wired");
        let (stop, rx) = oneshot::channel();
        // `tokio::spawn` proves the assembled runtime future is `Send`.
        let task = tokio::spawn(runtime.run(rx));

        let (stage_io, exec_io) = tokio::io::duplex(4 << 20);
        let hello = executor.hello(false, "runc");
        let handler =
            bundle_executor::connector_boot::ConnectorLinkHandler::new(Arc::clone(&executor));
        let exec_conn = tokio::spawn(async move {
            let _ = run_connection(
                exec_io,
                hello,
                handler,
                "discord-acceptance",
                Heartbeat::disabled(),
            )
            .await;
        });
        link.attach(stage_io).await.expect("host-api handshake");
        Self {
            registry,
            tok,
            active,
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

/// Asserts the acceptance contract for every published entry and returns the
/// decrypted actors (the denominator), so callers can pin exactly who was
/// ingested.
async fn assert_entries(ctx: &Ctx, community: i64) -> Vec<String> {
    let keys = ctx.keys().await;
    assert!(!keys.is_empty(), "no stream under the resolved slug");
    let mut actors = Vec::new();
    for key in &keys {
        assert!(
            key.starts_with(&format!("waddles:t:{}:c:main:src:discord:", ctx.slug)),
            "stream key `{key}` is not under the resolved tenant/community"
        );
        for entry in ctx.entries(key).await {
            let d = Decoded::of(&entry);
            assert_eq!(d.s("/tenant"), ctx.slug);
            assert_eq!(d.s("/community"), "main");
            assert_eq!(d.env["tenant_id"], 5);
            assert_eq!(d.env["community_id"], community);
            assert_eq!(d.s("/event/platform"), "discord");
            assert_eq!(d.s("/event/event_type"), "message");
            let actor = d.decrypt(key, "actor", "/event/actor").await;
            assert!(is_uuid(&actor), "actor is not a UUID: {actor}");
            // Identity-bearing free text is an envelope, never clear.
            let text = d.decrypt(key, "text", "/event/payload/text").await;
            assert!(!text.is_empty(), "text decrypts to something");
            assert_ne!(
                d.s("/event/payload/author_id"),
                BOT_ID,
                "the bot's own message must never be published"
            );
            actors.push(actor);
        }
    }
    actors
}

fn assert_logs_clean() {
    let log = logged();
    assert!(
        log.lines().count() >= 5,
        "log capture is empty: {} lines",
        log.lines().count()
    );
    for bad in FORBIDDEN {
        assert!(!log.contains(bad), "`{bad}` leaked into a log line");
    }
}

fn one_bind(ctx: &Ctx) -> Vec<Bind> {
    vec![Bind {
        community: 7,
        connection: ctx.conn,
        guild: GUILD,
    }]
}

#[derive(Default)]
struct Counting {
    discord_inbound: usize,
    discord_outbound: usize,
    twitch_inbound: usize,
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
        Ok(())
    }
    fn discord_outbound(&mut self) {
        self.discord_outbound += 1;
    }
    async fn twitch_eventsub(&mut self) {}
}

/// Discord ON is accepted (the budget is ready), the legacy Discord receiver is
/// NOT started, and the others stay on their legacy path. No Valkey needed.
#[tokio::test]
async fn discord_on_is_accepted_and_the_legacy_receiver_is_suppressed() {
    assert!(
        std::hint::black_box(IDENTIFY_BUDGET_READY),
        "the acceptance suite runs with the identify budget wired"
    );
    let env = |k: &str| (k == "CONNECTOR_DISCORD_ENABLED").then(|| "true".to_owned());
    let sel = ConnectorSelector::from_env(&env).expect("Discord ON is accepted");
    assert_eq!(sel.enabled_platforms(), vec!["discord"]);
    assert!(sel.connector("discord") && !sel.connector("twitch") && !sel.connector("youtube"));

    let mut starters = Counting::default();
    start_legacy(&LegacyPlan::new(&sel, &[], false), &mut starters)
        .await
        .expect("legacy start");
    assert_eq!(
        starters.discord_inbound, 0,
        "the legacy Discord receiver must be suppressed"
    );
    assert_eq!(
        starters.twitch_inbound, 1,
        "the OFF platforms keep their legacy starters"
    );
    assert_eq!(
        starters.discord_outbound, 1,
        "Discord outbound is governed by CONNECTOR_TOOLS_ENABLED, not the selector"
    );

    let off = ConnectorSelector::from_env(&|_: &str| None).expect("default OFF");
    let mut legacy = Counting::default();
    start_legacy(&LegacyPlan::new(&off, &[], false), &mut legacy)
        .await
        .expect("legacy start");
    assert_eq!(legacy.discord_inbound, 1, "OFF keeps the legacy receiver");
}

/// The whole lifecycle through the real budget: IDENTIFY (token first) -> READY
/// -> RESUME (budget skipped) -> own message dropped, foreign one published ->
/// op 7 -> RESUME -> op 9 invalid -> a FRESH IDENTIFY (second slot) -> RESUME ->
/// second message -> close 4004 parks.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn discord_ingests_tokenizes_publishes_and_meters_every_identify() {
    let ctx = Ctx::new(1, Script::Lifecycle, 1000).await;
    let mut rig = Rig::boot(&ctx, one_bind(&ctx), ctx.kc().await).await;
    eventually("both foreign messages publish", || {
        published(&rig.registry) >= 2.0
    })
    .await;
    eventually("the third RESUME was played", || {
        ctx.wire.lock().unwrap().resumes.len() >= 3
    })
    .await;
    // Close 4004 parks: the connection goes down and is never redialed (no 4th
    // RESUME, no third IDENTIFY) across several reconcile cycles.
    eventually("4004 takes the connection down", || {
        counter(
            &rig.registry,
            "connector_runtime_live",
            &[("platform", "discord")],
        ) == 0.0
    })
    .await;
    tokio::time::sleep(Duration::from_secs(4)).await;
    {
        let w = ctx.wire.lock().unwrap();
        assert_eq!(
            (w.gw_sockets, w.identifies.len(), w.resumes.len()),
            (2, 2, 3),
            "a parked source is never redialed"
        );
    }

    // Ingested -> tokenized -> published under the RESOLVED scope; the bot's
    // own message was dropped (exactly the two foreign actors, never BOT_ID).
    let mut actors = assert_entries(&ctx, 7).await;
    actors.sort();
    let mut want = vec![
        pseudonym_uuid("discord", FOREIGN_ID),
        pseudonym_uuid("discord", FOREIGN_ID_2),
    ];
    want.sort();
    assert_eq!(actors, want, "exactly the two foreign messages published");
    {
        let seen = rig.tok.seen.lock().unwrap();
        assert!(!seen.is_empty(), "identity.lookup was served");
        for (scope, id) in seen.iter() {
            assert_ne!(id, BOT_ID, "the bot's own id never reaches identity");
            assert_eq!(
                (
                    scope.platform,
                    scope.community_id,
                    scope.tenant_id,
                    scope.connection_id
                ),
                ("discord", 7, 5, ctx.conn),
                "host calls run under the connection's own scope"
            );
        }
    }

    // The gateway wire: two IDENTIFYs (initial + after op 9), three RESUMEs.
    let (identifies, resumes, heartbeats) = {
        let w = ctx.wire.lock().unwrap();
        (w.identifies.clone(), w.resumes.clone(), w.heartbeats)
    };
    assert_eq!(identifies.len(), 2, "one IDENTIFY per fresh session");
    for (_, text) in &identifies {
        let v: serde_json::Value = serde_json::from_str(text).unwrap();
        assert_eq!(v["op"], 2);
        assert_eq!(v["d"]["token"], ctx.token.as_str(), "slot substituted");
        assert_eq!(v["d"]["intents"], 33_281);
        assert!(
            text.contains(r#""d":{"token":""#),
            "token-first IDENTIFY (version 1.0.1): {text}"
        );
        assert!(!text.contains("{{"), "no unsubstituted slot");
    }
    assert_eq!(resumes.len(), 3, "READY, op 7 and READY each RESUME");
    let sessions: Vec<String> = resumes
        .iter()
        .map(|t| {
            let v: serde_json::Value = serde_json::from_str(t).unwrap();
            assert_eq!(v["op"], 6, "RESUME, never a second IDENTIFY");
            assert_eq!(v["d"]["token"], ctx.token.as_str());
            v["d"]["session_id"].as_str().unwrap().to_owned()
        })
        .collect();
    assert_eq!(sessions, ["sess1", "sess1", "sess2"]);
    assert!(heartbeats >= 1, "the host heartbeat ran and was ACKed");

    // The budget: 1000 -> 998 (one slot per IDENTIFY, none per RESUME).
    assert_eq!(ctx.remaining().await, 998, "exactly two slots consumed");
    assert_eq!(budget(&rig.registry, "granted"), 2.0);
    assert_eq!(
        budget(&rig.registry, "resume_skipped"),
        3.0,
        "every RESUME skipped the budget"
    );
    assert_eq!(
        counter(
            &rig.registry,
            "connector_runtime_live",
            &[("platform", "discord")]
        ),
        0.0,
        "4004 parked the only connection"
    );
    let gateway_bot = ctx
        .api
        .received_requests()
        .await
        .expect("request recording is on");
    assert_eq!(
        gateway_bot.len(),
        1,
        "GET /gateway/bot seeds the budget once; the second IDENTIFY reuses it"
    );

    assert_logs_clean();
    rig.shutdown().await;
    ctx.cleanup().await;
}

/// One bot, two guild connections, one bucket: the IDENTIFYs are serialized one
/// per 5 s window fleet-wide (never two in the same bucket window).
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn two_connections_of_one_bot_identify_one_per_bucket_window() {
    let ctx = Ctx::new(2, Script::Hold, 1000).await;
    let binds = vec![
        Bind {
            community: 7,
            connection: ctx.conn,
            guild: GUILD,
        },
        Bind {
            community: 8,
            connection: ctx.conn + 1,
            guild: GUILD_2,
        },
    ];
    let mut rig = Rig::boot(&ctx, binds, ctx.kc().await).await;
    eventually("both connections IDENTIFY", || {
        ctx.wire.lock().unwrap().identifies.len() >= 2
    })
    .await;
    eventually("both RESUME after READY", || {
        ctx.wire.lock().unwrap().resumes.len() >= 2
    })
    .await;
    {
        let wire = ctx.wire.lock().unwrap();
        assert_eq!(wire.identifies.len(), 2, "no third IDENTIFY");
        let gap = wire.identifies[1].0.duration_since(wire.identifies[0].0);
        assert!(
            gap >= IDENTIFY_WINDOW - Duration::from_millis(500),
            "two IDENTIFYs of one bot inside one {IDENTIFY_WINDOW:?} bucket window: gap {gap:?}"
        );
    }
    assert_eq!(ctx.remaining().await, 998, "two slots consumed");
    assert_eq!(budget(&rig.registry, "granted"), 2.0);
    assert!(
        budget(&rig.registry, "waited") >= 1.0,
        "the second connection waited for the window"
    );
    rig.shutdown().await;
    ctx.cleanup().await;
}

/// A spent daily budget sends NO IDENTIFY: the connection reaches HELLO, the
/// gate refuses, and the socket is closed without the handshake.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn an_exhausted_budget_sends_no_identify() {
    let ctx = Ctx::new(3, Script::Hold, 0).await;
    let mut rig = Rig::boot(&ctx, one_bind(&ctx), ctx.kc().await).await;
    eventually("the gate refused as exhausted", || {
        budget(&rig.registry, "exhausted") >= 1.0
    })
    .await;
    tokio::time::sleep(Duration::from_secs(3)).await;
    {
        let wire = ctx.wire.lock().unwrap();
        assert!(wire.gw_sockets >= 1, "the connection reached the gateway");
        assert!(wire.identifies.is_empty(), "no IDENTIFY was sent");
        assert!(wire.resumes.is_empty(), "no session to RESUME");
    }
    assert_eq!(budget(&rig.registry, "granted"), 0.0);
    assert_eq!(published(&rig.registry), 0.0, "nothing ingested");
    assert_eq!(ctx.remaining().await, 0, "the counter stays at zero");
    rig.shutdown().await;
    ctx.cleanup().await;
}

/// A dead budget store fails CLOSED: no IDENTIFY, never an unchecked one.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_dead_valkey_fails_closed_and_sends_no_identify() {
    let ctx = Ctx::new(4, Script::Hold, 1000).await;
    // A connection to the real Valkey, named, then killed server-side: every
    // budget command on it fails like an outage.
    let mut budget_conn = ctx.kc().await;
    let name = format!("discord-budget-{}", ctx.slug);
    let _: () = redis::cmd("CLIENT")
        .arg("SETNAME")
        .arg(&name)
        .query_async(&mut budget_conn)
        .await
        .unwrap();
    let mut admin = ctx.kc().await;
    let list: String = redis::cmd("CLIENT")
        .arg("LIST")
        .query_async(&mut admin)
        .await
        .unwrap();
    let id = list
        .lines()
        .find(|l| l.contains(&format!("name={name} ")) || l.ends_with(&format!("name={name}")))
        .and_then(|l| l.split_whitespace().find_map(|f| f.strip_prefix("id=")))
        .expect("the named budget connection is listed")
        .to_owned();
    let killed: i64 = redis::cmd("CLIENT")
        .arg("KILL")
        .arg("ID")
        .arg(&id)
        .query_async(&mut admin)
        .await
        .unwrap();
    assert_eq!(killed, 1, "the budget connection was killed");

    let mut rig = Rig::boot(&ctx, one_bind(&ctx), budget_conn).await;
    eventually("the gate failed closed on the store", || {
        budget(&rig.registry, "store_error") >= 1.0
    })
    .await;
    tokio::time::sleep(Duration::from_secs(3)).await;
    {
        let wire = ctx.wire.lock().unwrap();
        assert!(wire.gw_sockets >= 1, "the connection reached the gateway");
        assert!(wire.identifies.is_empty(), "no unchecked IDENTIFY was sent");
    }
    assert_eq!(budget(&rig.registry, "granted"), 0.0);
    assert_eq!(published(&rig.registry), 0.0, "nothing ingested");
    assert!(
        ctx.api
            .received_requests()
            .await
            .unwrap_or_default()
            .is_empty(),
        "no limit fetch without a store to seed"
    );
    rig.shutdown().await;
    ctx.cleanup().await;
}

/// The production Discord REST base is the host constant the gate uses.
#[test]
fn production_gateway_bot_base_is_the_discord_api() {
    assert_eq!(DISCORD_API_BASE, "https://discord.com/api/v10");
}
