//! Real-path e2e of the generic outbound executor's BOOT path
//! (`waddles.connector-tools` ON): a signed queue entry on a REAL Valkey list
//! is drained by the production `outbound::run_exec` / `run_discord_exec` /
//! `run_youtube_exec`, the REAL compiled platform connector builds the request
//! template over the REAL executor wire (`WireTemplateBuilder` ->
//! `ExecutorLink` -> `run_connection` -> `ConnectorHost` -> wasm), the
//! production verifier / substituter / `GuardedClient` / per-community slot
//! sources / `PlatformSlotRouter` / Discord duties run unmodified, and the
//! request is asserted on the wire at a local fake platform endpoint.
//!
//! Doubles (and only these): the hub-api credential seam, the hub-api
//! tokenization seam, and a `TemplateBuilder` decorator that rewrites ONLY the
//! scheme+host of the connector's template to the local fake after asserting the
//! connector named the real platform host (https cannot be terminated by a
//! local plain-http fake). Needs a real Valkey: `VALKEY_URL` unset FAILS (no
//! silent skip).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{BTreeMap, HashMap};
use std::net::{IpAddr, Ipv4Addr};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use bundle_executor::manifest::WitWorld;
use bundle_host_http::guarded::{GuardedClient, GuardedLimits};
use connection_resolver::SecretString;
use connector_e2e::{digest_of, RigConnector, WireRig};
use serde_json::json;
use svc_ingest::connector::sender_exec::discord_duties::{
    DiscordDuties, PlatformUserResolver, UserResolveError,
};
use svc_ingest::connector::sender_exec::discord_slots::DiscordSlots;
use svc_ingest::connector::sender_exec::twitch_slots::TwitchSlots;
use svc_ingest::connector::sender_exec::youtube_slots::YoutubeSlots;
use svc_ingest::connector::sender_exec::{
    exec_count, BuildError, BuiltTemplate, ExecutorConfig, PlatformPolicy, PlatformSlotRouter,
    SenderAction, SenderExecutor, SlotValue, TemplateBuilder, WireTemplateBuilder,
};
use svc_ingest::outbound::{run_discord_exec, run_exec, run_youtube_exec};
use svc_ingest::outbound::{
    DISCORD_OUTBOUND_QUEUE_KEY, TWITCH_OUTBOUND_QUEUE_KEY, YOUTUBE_OUTBOUND_QUEUE_KEY,
};
use svc_ingest::outbound_boot::CachedFlagGate;
use svc_ingest::outbound_creds::{CredError, TwitchCredential, TwitchCredentialSource};
use svc_ingest::outbound_creds_discord::{DiscordBinding, DiscordCredentialSource};
use svc_ingest::outbound_creds_youtube::{YoutubeBinding, YoutubeCredentialSource};
use svc_ingest::outbound_pin::{refusal_count, replay_count, PinKeys};
use svc_ingest::pin_sig::PinFields;
use tokio::sync::oneshot;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const TWITCH_WASM: &[u8] = include_bytes!("fixtures/twitch_connector.wasm");
const YOUTUBE_WASM: &[u8] = include_bytes!("fixtures/youtube_connector.wasm");
const DISCORD_WASM: &[u8] =
    include_bytes!("../../bundle_executor/tests/fixtures/discord_connector.wasm");

const KEYS: &str = "k1:00112233445566778899aabbccddeeff";
const COMMUNITY: i64 = 7;
const TENANT: i64 = 3;
const TW_TOKEN: &str = "TW-OAUTH-SECRET";
const DC_TOKEN: &str = "DC-BOT-SECRET";
const YT_TOKEN: &str = "YT-OAUTH-SECRET";
const GUILD: &str = "111";
const DISCORD_CHANNEL: &str = "123456789";
const USER_UUID: &str = "123e4567-e89b-12d3-a456-426614174000";
const USER_RAW: &str = "700000000000000001";
const DM_CHANNEL: &str = "555000111";

/// Serializes the tests: they share the three global queue keys.
static LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());
static SEQ: AtomicU64 = AtomicU64::new(0);

// ---- doubles -------------------------------------------------------------

struct TwCreds;
impl TwitchCredentialSource for TwCreds {
    async fn credential(
        &self,
        community_id: Option<i64>,
        channel: &str,
    ) -> Result<TwitchCredential, CredError> {
        if community_id != Some(COMMUNITY) {
            return Err(CredError::NoBinding);
        }
        if channel != "#chan" {
            return Err(CredError::ChannelNotOwned);
        }
        Ok(TwitchCredential {
            nick: "botlogin".to_string(),
            oauth_token: SecretString::new(TW_TOKEN.to_string()),
        })
    }
}

#[derive(Clone)]
struct DcCreds;
impl DiscordCredentialSource for DcCreds {
    async fn binding(&self, community_id: Option<i64>) -> Result<DiscordBinding, CredError> {
        if community_id != Some(COMMUNITY) {
            return Err(CredError::NoBinding);
        }
        Ok(DiscordBinding {
            bot_token: SecretString::new(DC_TOKEN.to_string()),
            guild_id: GUILD.to_string(),
            application_id: None,
            managed_roles: BTreeMap::from([("vip".to_string(), "222".to_string())]),
        })
    }
}

struct YtCreds;
impl YoutubeCredentialSource for YtCreds {
    async fn binding(
        &self,
        community_id: Option<i64>,
        live_chat_id: &str,
    ) -> Result<YoutubeBinding, CredError> {
        if community_id != Some(COMMUNITY) {
            return Err(CredError::NoBinding);
        }
        if live_chat_id != "lc1" {
            return Err(CredError::ChannelNotOwned);
        }
        Ok(YoutubeBinding {
            oauth_token: SecretString::new(YT_TOKEN.to_string()),
            live_chat_id: live_chat_id.to_string(),
        })
    }
}

struct Users;
impl PlatformUserResolver for Users {
    async fn resolve(
        &self,
        tenant: i64,
        community: i64,
        uuid: &str,
    ) -> Result<SlotValue, UserResolveError> {
        if tenant == TENANT && community == COMMUNITY && uuid == USER_UUID {
            Ok(SlotValue::new(USER_RAW.to_string()))
        } else {
            Err(UserResolveError::NotFound)
        }
    }
}

/// Real connector output, with only scheme+host swapped for the local fake after
/// asserting the connector named the real platform host.
struct Rewrite {
    inner: WireTemplateBuilder,
    bases: HashMap<&'static str, String>,
}

impl TemplateBuilder for Rewrite {
    async fn build_request(
        &self,
        platform: &str,
        action: &SenderAction,
    ) -> Result<BuiltTemplate, BuildError> {
        let mut built = self.inner.build_request(platform, action).await?;
        let (prod, local) = match platform {
            "twitch" => ("https://api.twitch.tv", &self.bases["twitch"]),
            "discord" => ("https://discord.com", &self.bases["discord"]),
            "youtube" => ("https://www.googleapis.com", &self.bases["youtube"]),
            other => panic!("unexpected platform {other}"),
        };
        let rest = built
            .tpl
            .url
            .strip_prefix(prod)
            .unwrap_or_else(|| panic!("connector did not name {prod}: {}", built.tpl.url));
        built.tpl.url = format!("{local}{rest}");
        Ok(built)
    }
}

type Exec = SenderExecutor<
    Rewrite,
    PlatformSlotRouter<TwitchSlots<TwCreds>, DiscordSlots<DcCreds>, YoutubeSlots<YtCreds>>,
    CachedFlagGate,
    DiscordDuties<DcCreds, Users>,
>;

fn guard() -> GuardedClient {
    GuardedClient::new(
        GuardedLimits {
            max_redirects: 0,
            timeout: Duration::from_secs(5),
            max_response_bytes: 64 * 1024,
        },
        vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
    )
}

struct Rig {
    _wire: WireRig,
    exec: Arc<Exec>,
    tw: MockServer,
    dc: MockServer,
    yt: MockServer,
    _guard: tokio::sync::MutexGuard<'static, ()>,
}

async fn rig(flag: bool) -> Rig {
    let guard_lock = LOCK.lock().await;
    let url = std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: this e2e needs a REAL Valkey (no silent skip)");
    std::env::set_var("VALKEY_URL", url);
    std::env::set_var("SECURITY_TRANSPORT_TLS", "false");
    std::env::set_var("SECURITY_TRANSPORT_AUTH", "false");
    let client = redis::Client::open(std::env::var("VALKEY_URL").unwrap()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    for key in [
        TWITCH_OUTBOUND_QUEUE_KEY,
        DISCORD_OUTBOUND_QUEUE_KEY,
        YOUTUBE_OUTBOUND_QUEUE_KEY,
    ] {
        let _: () = redis::cmd("DEL")
            .arg(key)
            .query_async(&mut conn)
            .await
            .unwrap();
    }

    let wire = WireRig::start_with_permissions(
        vec![
            RigConnector {
                platform: "twitch".into(),
                app_id: "waddles.core.service.twitch".into(),
                wasm: TWITCH_WASM,
                world: WitWorld::ConnectorTools,
            },
            RigConnector {
                platform: "discord".into(),
                app_id: "waddles.core.service.discord".into(),
                wasm: DISCORD_WASM,
                world: WitWorld::ConnectorTools,
            },
            RigConnector {
                platform: "youtube".into(),
                app_id: "waddles.core.service.youtube".into(),
                wasm: YOUTUBE_WASM,
                world: WitWorld::ConnectorTools,
            },
        ],
        &["connector.pii.read"],
        &svc_ingest::outbound_boot::CONNECTOR_FLAGS,
        64 * 1024,
        |_| {},
    )
    .await;
    let (tw, dc, yt) = (
        MockServer::start().await,
        MockServer::start().await,
        MockServer::start().await,
    );
    let mut policies = Vec::new();
    for (mut p, s) in [
        (PlatformPolicy::twitch(), &tw),
        (PlatformPolicy::discord(), &dc),
        (PlatformPolicy::youtube(), &yt),
    ] {
        p.hosts = vec![s.address().to_string()];
        p.allow_http = true;
        policies.push(p);
    }
    let digests = HashMap::from([
        ("twitch".to_string(), digest_of(TWITCH_WASM)),
        ("discord".to_string(), digest_of(DISCORD_WASM)),
        ("youtube".to_string(), digest_of(YOUTUBE_WASM)),
    ]);
    let cfg = ExecutorConfig::new(policies, digests).unwrap().with_client(
        GuardedLimits {
            max_redirects: 0,
            timeout: Duration::from_secs(5),
            max_response_bytes: 64 * 1024,
        },
        vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
    );
    let bases = HashMap::from([
        ("twitch", tw.uri()),
        ("discord", dc.uri()),
        ("youtube", yt.uri()),
    ]);
    let builder = Rewrite {
        inner: WireTemplateBuilder::new(Arc::clone(&wire.link), Arc::clone(&wire.registry)),
        bases,
    };
    let slots = PlatformSlotRouter::new(
        TwitchSlots::with_endpoint(
            TwCreds,
            "cid123".to_string(),
            guard(),
            format!("{}/helix/users", tw.uri()),
        ),
        DiscordSlots::with_endpoint(DcCreds, guard(), format!("{}/api/v10", dc.uri())),
        YoutubeSlots::new(YtCreds),
    );
    let duties = DiscordDuties::with_endpoint(
        DcCreds,
        Users,
        format!("{}/api/v10", dc.uri()),
        Duration::from_secs(30),
    );
    let exec = Arc::new(SenderExecutor::with_duties(
        builder,
        slots,
        CachedFlagGate(flag),
        duties,
        cfg,
    ));
    Rig {
        _wire: wire,
        exec,
        tw,
        dc,
        yt,
        _guard: guard_lock,
    }
}

// ---- helpers -------------------------------------------------------------

fn pin() -> PinKeys {
    PinKeys::parse(KEYS, Some("k1")).unwrap()
}

fn now_ms() -> u64 {
    u64::try_from(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis(),
    )
    .unwrap()
}

/// A producer-stamped queue entry exactly as `svc_action` writes it.
#[allow(clippy::too_many_arguments)]
fn entry(
    platform: &str,
    op: &str,
    channel: &str,
    origin: &str,
    text: &str,
    user_id: &str,
    tenant: i64,
) -> String {
    let op_id = format!(
        "e2e-{}-{}-{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::SeqCst),
        now_ms()
    );
    let exp_ms = now_ms() + 60_000;
    let sig = pin()
        .sign(&PinFields {
            platform,
            op,
            channel,
            origin_channel: origin,
            message_id: "",
            text,
            user_id,
            op_id: &op_id,
            exp_ms,
            community_id: COMMUNITY,
            tenant_id: tenant,
        })
        .unwrap();
    let mut v = json!({
        "v": 1, "op": op, "platform": platform, "origin_channel": origin,
        "text": text, "op_id": op_id, "exp_ms": exp_ms,
        "community_id": COMMUNITY, "pin_sig": sig,
    });
    if !channel.is_empty() {
        v["channel"] = json!(channel);
    }
    if !user_id.is_empty() {
        v["user_id"] = json!(user_id);
    }
    if tenant != 0 {
        v["tenant_id"] = json!(tenant);
    }
    v.to_string()
}

async fn push(queue: &str, raw: &str) {
    let client = redis::Client::open(std::env::var("VALKEY_URL").unwrap()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let _: i64 = redis::cmd("LPUSH")
        .arg(queue)
        .arg(raw)
        .query_async(&mut conn)
        .await
        .unwrap();
}

/// Starts the production drain for `platform`; returns the shutdown trigger
/// and the task handle.
fn drain(rig: &Rig, platform: &'static str) -> (oneshot::Sender<()>, tokio::task::JoinHandle<()>) {
    let (tx, rx) = oneshot::channel();
    let exec = Arc::clone(&rig.exec);
    let h = tokio::spawn(async move {
        let spine = penguin_spine::SpineConfig::from_env().unwrap();
        let res = match platform {
            "twitch" => run_exec(&spine, &*exec, Some(pin()), rx).await,
            "discord" => run_discord_exec(&spine, &*exec, Some(pin()), rx).await,
            _ => run_youtube_exec(&spine, &*exec, Some(pin()), rx).await,
        };
        res.unwrap();
    });
    (tx, h)
}

async fn stop(tx: oneshot::Sender<()>, h: tokio::task::JoinHandle<()>) {
    let _ = tx.send(());
    // Graceful-shutdown parity: the drain returns within a BRPOP timeout.
    tokio::time::timeout(Duration::from_secs(15), h)
        .await
        .expect("drain did not stop on shutdown")
        .unwrap();
}

async fn wait_for(server: &MockServer, method_name: &str, want: usize) -> Vec<wiremock::Request> {
    for _ in 0..200 {
        let got: Vec<_> = server
            .received_requests()
            .await
            .unwrap()
            .into_iter()
            .filter(|r| r.method.as_str() == method_name)
            .collect();
        if got.len() >= want {
            return got;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let outcomes: Vec<_> = [
        "ok",
        "template_refused",
        "slot_failed",
        "build_failed",
        "pin_refused",
        "subst_failed",
        "egress_failed",
        "upstream_error",
        "flag_off",
        "digest_refused",
        "bad_action",
    ]
    .iter()
    .map(|o| {
        format!(
            "{o}={}",
            ["twitch", "discord", "youtube"]
                .iter()
                .map(|p| exec_count(p, "chat.send", o) + exec_count(p, "event.create", o))
                .sum::<u64>()
        )
    })
    .collect();
    panic!(
        "timed out waiting for {want} {method_name} request(s); chat.send/event.create outcomes: {outcomes:?}"
    );
}

async fn mock_twitch(s: &MockServer) {
    Mock::given(method("GET"))
        .and(path("/helix/users"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"data": [
            {"id": "111", "login": "chan"}, {"id": "222", "login": "botlogin"}
        ]})))
        .mount(s)
        .await;
    Mock::given(method("POST"))
        .and(path("/helix/chat/messages"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({"data": [{"is_sent": true}]})),
        )
        .mount(s)
        .await;
}

fn header(r: &wiremock::Request, name: &str) -> String {
    r.headers
        .get(name)
        .map(|v| v.to_str().unwrap().to_string())
        .unwrap_or_default()
}

// ---- tests ---------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn twitch_chat_send_flows_queue_to_wire_with_replay_and_pin_guards() {
    let rig = rig(true).await;
    mock_twitch(&rig.tw).await;
    let (tx, h) = drain(&rig, "twitch");

    let good = entry(
        "twitch",
        "chat.send",
        "#chan",
        "#chan",
        "hello world",
        "",
        0,
    );
    push(TWITCH_OUTBOUND_QUEUE_KEY, &good).await;
    let sent = wait_for(&rig.tw, "POST", 1).await;
    let req = &sent[0];
    assert_eq!(req.url.path(), "/helix/chat/messages");
    assert_eq!(header(req, "authorization"), format!("Bearer {TW_TOKEN}"));
    assert_eq!(header(req, "client-id"), "cid123");
    let body: serde_json::Value = serde_json::from_slice(&req.body).unwrap();
    assert_eq!(body["broadcaster_id"], "111");
    assert_eq!(body["sender_id"], "222");
    assert_eq!(body["message"], "hello world");

    // Replay guard: the identical entry again is dropped, nothing new is sent.
    let replays0 = replay_count("twitch", "chat.send");
    push(TWITCH_OUTBOUND_QUEUE_KEY, &good).await;
    for _ in 0..100 {
        if replay_count("twitch", "chat.send") > replays0 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(
        replay_count("twitch", "chat.send"),
        replays0 + 1,
        "pin_replayed"
    );

    // Wrong channel: channel != the stamped origin -> refused, nothing sent.
    let refused0 = refusal_count("twitch", "chat.send", "channel_not_pinned");
    let wrong = entry("twitch", "chat.send", "#victim", "#chan", "pwned", "", 0);
    push(TWITCH_OUTBOUND_QUEUE_KEY, &wrong).await;
    for _ in 0..100 {
        if refusal_count("twitch", "chat.send", "channel_not_pinned") > refused0 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(
        refusal_count("twitch", "chat.send", "channel_not_pinned"),
        refused0 + 1
    );
    tokio::time::sleep(Duration::from_millis(300)).await;
    let posts = rig
        .tw
        .received_requests()
        .await
        .unwrap()
        .into_iter()
        .filter(|r| r.method.as_str() == "POST")
        .count();
    assert_eq!(posts, 1, "exactly the one good send reached the wire");
    stop(tx, h).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn discord_chat_dm_and_role_flow_through_the_real_connector() {
    let rig = rig(true).await;
    let s = &rig.dc;
    Mock::given(method("GET"))
        .and(path(format!("/api/v10/channels/{DISCORD_CHANNEL}")))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"id": DISCORD_CHANNEL, "guild_id": GUILD})),
        )
        .mount(s)
        .await;
    Mock::given(method("POST"))
        .and(path(format!(
            "/api/v10/channels/{DISCORD_CHANNEL}/messages"
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": "1"})))
        .mount(s)
        .await;
    Mock::given(method("GET"))
        .and(path(format!("/api/v10/guilds/{GUILD}/members/{USER_RAW}")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"user": {"id": USER_RAW}})))
        .mount(s)
        .await;
    Mock::given(method("POST"))
        .and(path("/api/v10/users/@me/channels"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": DM_CHANNEL})))
        .mount(s)
        .await;
    Mock::given(method("POST"))
        .and(path(format!("/api/v10/channels/{DM_CHANNEL}/messages")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": "2"})))
        .mount(s)
        .await;
    Mock::given(method("GET"))
        .and(path(format!("/api/v10/guilds/{GUILD}/roles")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([
            {"id": "222", "permissions": "1024"}
        ])))
        .mount(s)
        .await;
    Mock::given(method("PUT"))
        .and(path(format!(
            "/api/v10/guilds/{GUILD}/members/{USER_RAW}/roles/222"
        )))
        .respond_with(ResponseTemplate::new(204))
        .mount(s)
        .await;
    let (tx, h) = drain(&rig, "discord");

    // chat.send
    push(
        DISCORD_OUTBOUND_QUEUE_KEY,
        &entry(
            "discord",
            "chat.send",
            DISCORD_CHANNEL,
            DISCORD_CHANNEL,
            "hi there",
            "",
            0,
        ),
    )
    .await;
    let posts = wait_for(s, "POST", 1).await;
    let chat = posts
        .iter()
        .find(|r| r.url.path() == format!("/api/v10/channels/{DISCORD_CHANNEL}/messages"))
        .expect("chat message on the wire");
    assert_eq!(header(chat, "authorization"), format!("Bot {DC_TOKEN}"));
    let body: serde_json::Value = serde_json::from_slice(&chat.body).unwrap();
    assert_eq!(body["content"], "hi there");

    // dm.send: tokenized recipient resolved by the (fake) hub resolver only.
    push(
        DISCORD_OUTBOUND_QUEUE_KEY,
        &entry(
            "discord",
            "dm.send",
            "",
            DISCORD_CHANNEL,
            "secret dm",
            USER_UUID,
            TENANT,
        ),
    )
    .await;
    let dm = loop {
        let posts = wait_for(s, "POST", 3).await;
        if let Some(r) = posts
            .iter()
            .find(|r| r.url.path() == format!("/api/v10/channels/{DM_CHANNEL}/messages"))
        {
            break r.clone();
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    };
    assert_eq!(header(&dm, "authorization"), format!("Bot {DC_TOKEN}"));
    let body: serde_json::Value = serde_json::from_slice(&dm.body).unwrap();
    assert_eq!(body["content"], "secret dm");

    // role.add: key mapped through the binding allowlist, guild pinned by host.
    push(
        DISCORD_OUTBOUND_QUEUE_KEY,
        &entry(
            "discord",
            "role.add",
            "",
            DISCORD_CHANNEL,
            "vip",
            USER_UUID,
            TENANT,
        ),
    )
    .await;
    let put = wait_for(s, "PUT", 1).await;
    assert_eq!(
        put[0].url.path(),
        format!("/api/v10/guilds/{GUILD}/members/{USER_RAW}/roles/222")
    );
    assert_eq!(header(&put[0], "authorization"), format!("Bot {DC_TOKEN}"));
    stop(tx, h).await;
}

/// A producer-stamped guild scheduled-event entry exactly as `svc_action`
/// writes it: no channel, the expected guild in `origin_channel`, the event id
/// in `message_id`, the event JSON in `text`. Returns `(raw, op_id)`.
fn event_entry(op: &str, expect_guild: &str, event_id: &str, text: &str) -> (String, String) {
    let op_id = format!(
        "e2e-ev-{}-{}-{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::SeqCst),
        now_ms()
    );
    let exp_ms = now_ms() + 60_000;
    let sig = pin()
        .sign(&PinFields {
            platform: "discord",
            op,
            channel: "",
            origin_channel: expect_guild,
            message_id: event_id,
            text,
            user_id: "",
            op_id: &op_id,
            exp_ms,
            community_id: COMMUNITY,
            tenant_id: TENANT,
        })
        .unwrap();
    let mut v = json!({
        "v": 1, "op": op, "platform": "discord", "origin_channel": expect_guild,
        "op_id": op_id, "exp_ms": exp_ms, "community_id": COMMUNITY,
        "tenant_id": TENANT, "pin_sig": sig,
    });
    if !text.is_empty() {
        v["text"] = json!(text);
    }
    if !event_id.is_empty() {
        v["message_id"] = json!(event_id);
    }
    (v.to_string(), op_id)
}

/// Polls the drain's result key for `op_id` (what svc-action's producer reads).
async fn ack_for(op_id: &str) -> serde_json::Value {
    let client = redis::Client::open(std::env::var("VALKEY_URL").unwrap()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    for _ in 0..200 {
        let raw: Option<String> = redis::cmd("GET")
            .arg(format!("waddles:transport:discord:ack:{op_id}"))
            .query_async(&mut conn)
            .await
            .unwrap();
        if let Some(raw) = raw {
            return serde_json::from_str(&raw).unwrap();
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("no ack for {op_id}");
}

const EVENT_ID: &str = "900000000000000001";
const EVENT_FIELDS: &str = r#"{"name":"Movie night","description":"snacks","scheduled_start_time":"2026-12-01T19:00:00+00:00","scheduled_end_time":"2026-12-01T21:00:00+00:00","location":"Online"}"#;

/// Guild scheduled events: create / update / cancel / delete flow queue ->
/// real connector -> real executor -> wire, the guild comes ONLY from the
/// binding, the create reply's id reaches the producer through the ack, a
/// guild assertion mismatch and a payload-supplied guild never reach the wire.
#[tokio::test(flavor = "multi_thread")]
async fn discord_scheduled_events_flow_through_the_real_connector_pinned_to_the_binding_guild() {
    let rig = rig(true).await;
    let s = &rig.dc;
    let events = format!("/api/v10/guilds/{GUILD}/scheduled-events");
    Mock::given(method("POST"))
        .and(path(events.clone()))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": EVENT_ID})))
        .mount(s)
        .await;
    Mock::given(method("PATCH"))
        .and(path(format!("{events}/{EVENT_ID}")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": EVENT_ID})))
        .mount(s)
        .await;
    Mock::given(method("DELETE"))
        .and(path(format!("{events}/{EVENT_ID}")))
        .respond_with(ResponseTemplate::new(204))
        .mount(s)
        .await;
    let (tx, h) = drain(&rig, "discord");

    // create: exact request on the wire + the new id returned in the ack.
    let (raw, op_id) = event_entry("event.create", GUILD, "", EVENT_FIELDS);
    push(DISCORD_OUTBOUND_QUEUE_KEY, &raw).await;
    let posts = wait_for(s, "POST", 1).await;
    assert_eq!(posts[0].url.path(), events);
    assert_eq!(
        header(&posts[0], "authorization"),
        format!("Bot {DC_TOKEN}")
    );
    let body: serde_json::Value = serde_json::from_slice(&posts[0].body).unwrap();
    assert_eq!(body["name"], "Movie night");
    assert_eq!(body["entity_type"], 3);
    assert_eq!(body["privacy_level"], 2);
    assert_eq!(body["entity_metadata"]["location"], "Online");
    assert_eq!(body["scheduled_start_time"], "2026-12-01T19:00:00+00:00");
    assert_eq!(ack_for(&op_id).await, json!({"ok": true, "id": EVENT_ID}));

    // update (full field set) and cancel (status=4) are PATCHes of that event.
    let (raw, op_id) = event_entry("event.update", GUILD, EVENT_ID, EVENT_FIELDS);
    push(DISCORD_OUTBOUND_QUEUE_KEY, &raw).await;
    assert_eq!(ack_for(&op_id).await, json!({"ok": true}));
    let (raw, op_id) = event_entry("event.cancel", GUILD, EVENT_ID, "");
    push(DISCORD_OUTBOUND_QUEUE_KEY, &raw).await;
    assert_eq!(ack_for(&op_id).await, json!({"ok": true}));
    let patches = wait_for(s, "PATCH", 2).await;
    assert!(patches
        .iter()
        .all(|r| r.url.path() == format!("{events}/{EVENT_ID}")));
    let bodies: Vec<serde_json::Value> = patches
        .iter()
        .map(|r| serde_json::from_slice(&r.body).unwrap())
        .collect();
    assert!(bodies.iter().any(|b| b["name"] == "Movie night"));
    assert!(bodies.iter().any(|b| b == &json!({"status": 4})));

    // delete
    let (raw, op_id) = event_entry("event.delete", GUILD, EVENT_ID, "");
    push(DISCORD_OUTBOUND_QUEUE_KEY, &raw).await;
    assert_eq!(ack_for(&op_id).await, json!({"ok": true}));
    wait_for(s, "DELETE", 1).await;

    // guild pinning: an assertion for another guild is refused before the wire.
    let before = s.received_requests().await.unwrap().len();
    let (raw, op_id) = event_entry("event.create", "222", "", EVENT_FIELDS);
    push(DISCORD_OUTBOUND_QUEUE_KEY, &raw).await;
    assert_eq!(
        ack_for(&op_id).await,
        json!({"ok": false, "code": "guild_mismatch"})
    );
    // ...and a guild smuggled inside the (validly signed) event JSON is refused
    // by the connector: nothing is sent, nothing names guild 999.
    let smuggled = EVENT_FIELDS.replace('{', r#"{"guild_id":"999","#);
    let (raw, op_id) = event_entry("event.create", GUILD, "", &smuggled);
    push(DISCORD_OUTBOUND_QUEUE_KEY, &raw).await;
    assert_eq!(
        ack_for(&op_id).await,
        json!({"ok": false, "code": "build_refused"})
    );
    // replay of an already-executed stamp is dropped (no second create).
    let (raw, op_id) = event_entry("event.create", GUILD, "", EVENT_FIELDS);
    push(DISCORD_OUTBOUND_QUEUE_KEY, &raw).await;
    let _ = ack_for(&op_id).await;
    let after_first = s.received_requests().await.unwrap().len();
    push(DISCORD_OUTBOUND_QUEUE_KEY, &raw).await;
    tokio::time::sleep(Duration::from_millis(800)).await;
    let all = s.received_requests().await.unwrap();
    assert_eq!(
        all.len(),
        after_first,
        "replayed stamp must not reach the wire"
    );
    assert_eq!(
        after_first,
        before + 1,
        "only the one good create after the refusals"
    );
    assert!(all.iter().all(|r| !r.url.path().contains("999")));
    assert!(replay_count("discord", "event.create") >= 1);
    stop(tx, h).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn youtube_chat_send_flows_queue_to_wire() {
    let rig = rig(true).await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": "x"})))
        .mount(&rig.yt)
        .await;
    let (tx, h) = drain(&rig, "youtube");
    push(
        YOUTUBE_OUTBOUND_QUEUE_KEY,
        &entry("youtube", "chat.send", "lc1", "lc1", "yt hello", "", 0),
    )
    .await;
    let sent = wait_for(&rig.yt, "POST", 1).await;
    let req = &sent[0];
    assert!(req.url.path().contains("liveChat/messages"), "{}", req.url);
    assert_eq!(header(req, "authorization"), format!("Bearer {YT_TOKEN}"));
    let body = String::from_utf8(req.body.clone()).unwrap();
    assert!(body.contains("lc1") && body.contains("yt hello"), "{body}");

    // A live chat the community does not own is refused: nothing more is sent.
    push(
        YOUTUBE_OUTBOUND_QUEUE_KEY,
        &entry("youtube", "chat.send", "other", "other", "nope", "", 0),
    )
    .await;
    tokio::time::sleep(Duration::from_millis(800)).await;
    assert_eq!(
        rig.yt.received_requests().await.unwrap().len(),
        1,
        "foreign live chat must not reach the wire"
    );
    stop(tx, h).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn flag_off_executor_refuses_everything_and_sends_nothing() {
    let rig = rig(false).await;
    mock_twitch(&rig.tw).await;
    let off0 = exec_count("twitch", "chat.send", "flag_off");
    let (tx, h) = drain(&rig, "twitch");
    push(
        TWITCH_OUTBOUND_QUEUE_KEY,
        &entry("twitch", "chat.send", "#chan", "#chan", "off", "", 0),
    )
    .await;
    for _ in 0..100 {
        if exec_count("twitch", "chat.send", "flag_off") > off0 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(exec_count("twitch", "chat.send", "flag_off"), off0 + 1);
    assert!(rig.tw.received_requests().await.unwrap().is_empty());
    stop(tx, h).await;
}

/// The op id of a queue entry built by [`entry`].
fn op_id_of(raw: &str) -> String {
    serde_json::from_str::<serde_json::Value>(raw).unwrap()["op_id"]
        .as_str()
        .unwrap()
        .to_string()
}

/// Table test of the table-driven op seam, end to end through the production drains: an op
/// REGISTERED in the policy table reaches the connector's `build-request` (the executor ran, so
/// the producer gets an ack that is anything but `unsupported`), while an op that is NOT in the
/// table for that platform is rejected at parse time: dropped, never executed, never acked.
#[tokio::test(flavor = "multi_thread")]
async fn registered_ops_reach_build_request_and_unregistered_ops_are_rejected_at_parse() {
    let rig = rig(true).await;
    mock_twitch(&rig.tw).await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": "x"})))
        .mount(&rig.yt)
        .await;
    let (tw_tx, tw_h) = drain(&rig, "twitch");
    let (yt_tx, yt_h) = drain(&rig, "youtube");

    // (platform, queue, op, channel/origin, registered?)
    let rows: [(&str, &str, &str, &str, bool); 7] = [
        (
            "twitch",
            TWITCH_OUTBOUND_QUEUE_KEY,
            "mod.ban",
            "#chan",
            true,
        ),
        (
            "twitch",
            TWITCH_OUTBOUND_QUEUE_KEY,
            "mod.timeout",
            "#chan",
            true,
        ),
        (
            "twitch",
            TWITCH_OUTBOUND_QUEUE_KEY,
            "eventsub.list",
            "#chan",
            true,
        ),
        (
            "youtube",
            YOUTUBE_OUTBOUND_QUEUE_KEY,
            "mod.add",
            "lc1",
            true,
        ),
        // Not in the table for that platform (or at all): rejected at parse.
        (
            "twitch",
            TWITCH_OUTBOUND_QUEUE_KEY,
            "mod.kick",
            "#chan",
            false,
        ),
        (
            "twitch",
            TWITCH_OUTBOUND_QUEUE_KEY,
            "totally.fake",
            "#chan",
            false,
        ),
        (
            "youtube",
            YOUTUBE_OUTBOUND_QUEUE_KEY,
            "eventsub.create",
            "lc1",
            false,
        ),
    ];
    let mut reached = 0;
    let mut rejected = 0;
    for (platform, queue, op, chan, registered) in rows {
        let raw = entry(platform, op, chan, chan, "", "999", TENANT);
        let op_id = op_id_of(&raw);
        push(queue, &raw).await;
        if registered {
            let ack = ack_for(&op_id).await;
            let code = ack["code"].as_str().unwrap_or("ok");
            assert_ne!(
                code, "unsupported",
                "{platform}/{op}: never reached the executor"
            );
            reached += 1;
        } else {
            // Rejected at parse: no ack is ever written for it.
            tokio::time::sleep(Duration::from_millis(600)).await;
            let client = redis::Client::open(std::env::var("VALKEY_URL").unwrap()).unwrap();
            let mut conn = client.get_multiplexed_async_connection().await.unwrap();
            let raw_ack: Option<String> = redis::cmd("GET")
                .arg(format!("waddles:transport:discord:ack:{op_id}"))
                .query_async(&mut conn)
                .await
                .unwrap();
            assert!(
                raw_ack.is_none(),
                "{platform}/{op}: unregistered op was acked"
            );
            rejected += 1;
        }
    }
    assert_eq!((reached, rejected), (4, 3), "denominator: rows exercised");
    stop(tw_tx, tw_h).await;
    stop(yt_tx, yt_h).await;
}
