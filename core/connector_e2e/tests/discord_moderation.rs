//! Real-path e2e of Discord moderation + message ops: the REAL compiled Discord connector
//! (`discord_connector.wasm`) builds the request template over the REAL executor wire
//! (`WireTemplateBuilder` -> `ExecutorLink` -> `run_connection` -> `ConnectorHost` -> wasm); the
//! production verifier / substituter / `GuardedClient` / `DiscordSlots` / `DiscordDuties`
//! (hierarchy gate, tokenization, guild pin) run unmodified; the request is asserted on the wire
//! at a local fake Discord. Doubles (only): the credential seam, the tokenization seam, and a
//! decorator that swaps ONLY scheme+host of the connector's template for the local fake after
//! asserting the connector named the real host. No Valkey: this drives the executor directly (the
//! queue/producer path is covered by `outbound_exec.rs` and the svc_action tests).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{BTreeMap, HashMap};
use std::net::{IpAddr, Ipv4Addr};
use std::sync::Arc;
use std::time::Duration;

use bundle_executor::manifest::WitWorld;
use bundle_host_http::guarded::{GuardedClient, GuardedLimits};
use connection_resolver::SecretString;
use connector_e2e::{digest_of, RigConnector, WireRig};
use serde_json::{json, Value};
use svc_ingest::connector::sender_exec::discord_duties::{
    DiscordDuties, PlatformUserResolver, UserResolveError,
};
use svc_ingest::connector::sender_exec::discord_slots::DiscordSlots;
use svc_ingest::connector::sender_exec::{
    BuildError, BuiltTemplate, ExecRequest, ExecutorConfig, FlagGate, PlatformPolicy, SenderAction,
    SenderExecutor, SlotValue, TemplateBuilder, WireTemplateBuilder,
};
use svc_ingest::outbound_creds::CredError;
use svc_ingest::outbound_creds_discord::{DiscordBinding, DiscordCredentialSource};
use svc_ingest::outbound_ops::{parse_outbound_entry, OutboundAction};
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const DISCORD_WASM: &[u8] =
    include_bytes!("../../bundle_executor/tests/fixtures/discord_connector.wasm");
const COMMUNITY: i64 = 7;
const TENANT: i64 = 3;
const TOKEN: &str = "DC-BOT-SECRET";
const GUILD: &str = "111";
const OWNER: &str = "900000000000000001";
const BOT: &str = "600000000000000001";
const CHANNEL: &str = "123456789";
const UUID_LOW: &str = "123e4567-e89b-12d3-a456-426614174000";
const UUID_HIGH: &str = "223e4567-e89b-12d3-a456-426614174000";
const RAW_LOW: &str = "700000000000000001";
const RAW_HIGH: &str = "700000000000000002";

#[derive(Clone)]
struct Creds;
impl DiscordCredentialSource for Creds {
    async fn binding(&self, community_id: Option<i64>) -> Result<DiscordBinding, CredError> {
        if community_id != Some(COMMUNITY) {
            return Err(CredError::NoBinding);
        }
        Ok(DiscordBinding {
            bot_token: SecretString::new(TOKEN.to_string()),
            guild_id: GUILD.to_string(),
            managed_roles: BTreeMap::new(),
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
        if tenant != TENANT || community != COMMUNITY {
            return Err(UserResolveError::NotFound);
        }
        match uuid {
            UUID_LOW => Ok(SlotValue::new(RAW_LOW.to_string())),
            UUID_HIGH => Ok(SlotValue::new(RAW_HIGH.to_string())),
            _ => Err(UserResolveError::NotFound),
        }
    }
}

struct On;
impl FlagGate for On {
    async fn enabled(&self, _flag: &str) -> bool {
        true
    }
}

struct Rewrite {
    inner: WireTemplateBuilder,
    base: String,
}

impl TemplateBuilder for Rewrite {
    async fn build_request(
        &self,
        platform: &str,
        action: &SenderAction,
    ) -> Result<BuiltTemplate, BuildError> {
        let mut built = self.inner.build_request(platform, action).await?;
        let rest = built
            .tpl
            .url
            .strip_prefix("https://discord.com")
            .unwrap_or_else(|| panic!("connector did not name discord.com: {}", built.tpl.url));
        built.tpl.url = format!("{}{rest}", self.base);
        Ok(built)
    }
}

type Exec = SenderExecutor<Rewrite, DiscordSlots<Creds>, On, DiscordDuties<Creds, Users>>;

struct Rig {
    _wire: WireRig,
    exec: Exec,
    dc: MockServer,
}

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

async fn rig() -> Rig {
    std::env::set_var("SECURITY_TRANSPORT_TLS", "false");
    std::env::set_var("SECURITY_TRANSPORT_AUTH", "false");
    let wire = WireRig::start_with_permissions(
        vec![RigConnector {
            platform: "discord".into(),
            app_id: "waddles.core.service.discord".into(),
            wasm: DISCORD_WASM,
            world: WitWorld::ConnectorTools,
        }],
        &["connector.pii.read"],
        &svc_ingest::outbound_boot::CONNECTOR_FLAGS,
        64 * 1024,
        |_| {},
    )
    .await;
    let dc = MockServer::start().await;
    let mut policy = PlatformPolicy::discord();
    policy.hosts = vec![dc.address().to_string()];
    policy.allow_http = true;
    let digests = HashMap::from([("discord".to_string(), digest_of(DISCORD_WASM))]);
    let cfg = ExecutorConfig::new(vec![policy], digests)
        .unwrap()
        .with_client(
            GuardedLimits {
                max_redirects: 0,
                timeout: Duration::from_secs(5),
                max_response_bytes: 64 * 1024,
            },
            vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        );
    let api = format!("{}/api/v10", dc.uri());
    let exec = SenderExecutor::with_duties(
        Rewrite {
            inner: WireTemplateBuilder::new(Arc::clone(&wire.link), Arc::clone(&wire.registry)),
            base: dc.uri(),
        },
        DiscordSlots::with_endpoint(Creds, guard(), api.clone()),
        On,
        DiscordDuties::with_endpoint(Creds, Users, api, Duration::from_secs(30)),
        cfg,
    );
    // The fake guild: bot role position 10 (kick/ban/moderate), low member 5, high member 20.
    let get = |p: String, body: Value| {
        Mock::given(method("GET"))
            .and(path(p))
            .respond_with(ResponseTemplate::new(200).set_body_json(body))
    };
    get(
        format!("/api/v10/guilds/{GUILD}"),
        json!({"owner_id": OWNER}),
    )
    .mount(&dc)
    .await;
    get("/api/v10/users/@me".into(), json!({"id": BOT}))
        .mount(&dc)
        .await;
    get(
        format!("/api/v10/guilds/{GUILD}/roles"),
        json!([
            {"id": GUILD, "position": 0, "permissions": "0"},
            {"id": "1001", "position": 10, "permissions": ((1u64 << 1) | (1 << 2) | (1 << 40)).to_string()},
            {"id": "2001", "position": 5, "permissions": "0"},
            {"id": "3001", "position": 20, "permissions": "0"},
        ]),
    )
    .mount(&dc)
    .await;
    for (id, roles) in [(BOT, "1001"), (RAW_LOW, "2001"), (RAW_HIGH, "3001")] {
        get(
            format!("/api/v10/guilds/{GUILD}/members/{id}"),
            json!({"roles": [roles], "user": {"id": id}}),
        )
        .mount(&dc)
        .await;
    }
    Mock::given(method("GET"))
        .and(path(format!("/api/v10/channels/{CHANNEL}")))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({"id": CHANNEL, "guild_id": GUILD})),
        )
        .mount(&dc)
        .await;
    for m in ["DELETE", "PUT", "PATCH"] {
        Mock::given(method(m))
            .respond_with(ResponseTemplate::new(204))
            .mount(&dc)
            .await;
    }
    Rig {
        _wire: wire,
        exec,
        dc,
    }
}

/// The wire entry exactly as the producer builds it (`message.edit` etc. carry the channel).
fn entry(op: &str, fields: Value) -> OutboundAction {
    let mut m = json!({"v": 1, "op": op, "platform": "discord", "tenant_id": TENANT});
    for (k, v) in fields.as_object().unwrap() {
        m[k] = v.clone();
    }
    parse_outbound_entry(&m.to_string(), "discord")
        .unwrap()
        .action
}

fn rq(a: &OutboundAction) -> ExecRequest<'_> {
    ExecRequest {
        platform: "discord",
        action: a,
        community_id: Some(COMMUNITY),
    }
}

async fn last_write(s: &MockServer) -> (String, String, Vec<u8>, String) {
    let reqs = s.received_requests().await.unwrap();
    let r = reqs
        .iter()
        .rfind(|r| r.method.as_str() != "GET")
        .expect("a write reached the fake");
    (
        r.method.to_string(),
        r.url.path().to_string(),
        r.body.clone(),
        r.headers
            .get("authorization")
            .map(|v| v.to_str().unwrap().to_string())
            .unwrap_or_default(),
    )
}

async fn write_count(s: &MockServer) -> usize {
    s.received_requests()
        .await
        .unwrap()
        .iter()
        .filter(|r| r.method.as_str() != "GET")
        .count()
}

#[tokio::test]
async fn moderation_flows_through_the_real_connector_pinned_to_the_binding_guild() {
    let r = rig().await;
    for (op, text, verb, tail) in [
        ("mod.kick", None, "DELETE", format!("members/{RAW_LOW}")),
        (
            "mod.ban",
            Some(r#"{"delete_message_seconds":7200}"#),
            "PUT",
            format!("bans/{RAW_LOW}"),
        ),
        ("mod.unban", None, "DELETE", format!("bans/{RAW_LOW}")),
        (
            "mod.timeout",
            Some(r#"{"duration_seconds":900}"#),
            "PATCH",
            format!("members/{RAW_LOW}"),
        ),
        (
            "mod.timeout",
            Some(r#"{"clear":true}"#),
            "PATCH",
            format!("members/{RAW_LOW}"),
        ),
    ] {
        let mut f = json!({"user_id": UUID_LOW, "origin_channel": "guild:-"});
        if let Some(t) = text {
            f["text"] = json!(t);
        }
        let a = entry(op, f);
        r.exec
            .execute(&rq(&a))
            .await
            .unwrap_or_else(|e| panic!("{op}: {e}"));
        let (m, p, body, auth) = last_write(&r.dc).await;
        assert_eq!(m, verb, "{op}");
        assert_eq!(p, format!("/api/v10/guilds/{GUILD}/{tail}"), "{op}");
        assert_eq!(auth, format!("Bot {TOKEN}"));
        let b: Option<Value> = (!body.is_empty()).then(|| serde_json::from_slice(&body).unwrap());
        match (op, text) {
            ("mod.ban", _) => assert_eq!(b.unwrap(), json!({"delete_message_seconds": 7200})),
            ("mod.timeout", Some(t)) if t.contains("clear") => {
                assert!(b.unwrap()["communication_disabled_until"].is_null());
            }
            ("mod.timeout", _) => {
                let until = b.unwrap()["communication_disabled_until"]
                    .as_str()
                    .unwrap()
                    .to_string();
                assert!(
                    until.len() == 20 && until.ends_with('Z') && until.as_bytes()[10] == b'T',
                    "until is an absolute UTC instant: {until}"
                );
            }
            _ => assert!(b.is_none(), "{op} carries no body"),
        }
    }
}

#[tokio::test]
async fn hierarchy_and_argument_refusals_send_nothing() {
    let r = rig().await;
    let cases = [
        // the high-ranked member outranks the bot
        (
            "mod.kick",
            json!({"user_id": UUID_HIGH, "origin_channel": "guild:-"}),
            "role_hierarchy",
        ),
        // asserted guild differs from the binding
        (
            "mod.kick",
            json!({"user_id": UUID_LOW, "origin_channel": "guild:222"}),
            "guild_mismatch",
        ),
        // a raw snowflake never enters the executor
        (
            "mod.kick",
            json!({"user_id": RAW_LOW, "origin_channel": "guild:-"}),
            "user_not_tokenized",
        ),
    ];
    for (op, f, why) in cases {
        let a = entry(op, f);
        let e = r.exec.execute(&rq(&a)).await.unwrap_err();
        assert_eq!(e.code(), why, "{op}");
    }
    // connector-side refusals: a payload naming roles / a guild never reaches Discord
    for (op, text) in [
        ("mod.timeout", r#"{"duration_seconds":2419201}"#),
        ("mod.timeout", r#"{"roles":["1"]}"#),
        ("mod.ban", r#"{"delete_message_seconds":604801}"#),
        ("mod.kick", r#"{"guild_id":"9"}"#),
    ] {
        let a = entry(
            op,
            json!({"user_id": UUID_LOW, "origin_channel": "guild:-", "text": text}),
        );
        assert!(r.exec.execute(&rq(&a)).await.is_err(), "{op} {text}");
    }
    assert_eq!(write_count(&r.dc).await, 0);
}

#[tokio::test]
async fn message_edit_react_and_delete_act_only_in_the_pinned_channel() {
    let r = rig().await;
    let a = entry(
        "message.edit",
        json!({"channel": CHANNEL, "origin_channel": CHANNEL, "message_id": "42", "text": "edited"}),
    );
    r.exec.execute(&rq(&a)).await.unwrap();
    let (m, p, body, auth) = last_write(&r.dc).await;
    assert_eq!(
        (m.as_str(), p.as_str()),
        (
            "PATCH",
            format!("/api/v10/channels/{CHANNEL}/messages/42").as_str()
        )
    );
    assert_eq!(auth, format!("Bot {TOKEN}"));
    let b: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(b["content"], "edited");
    assert_eq!(b["allowed_mentions"], json!({"parse": ["users"]}));

    let a = entry(
        "reaction.add",
        json!({"channel": CHANNEL, "origin_channel": CHANNEL, "message_id": "42", "text": "\u{1F44D}"}),
    );
    r.exec.execute(&rq(&a)).await.unwrap();
    let (m, p, ..) = last_write(&r.dc).await;
    assert_eq!(m, "PUT");
    assert_eq!(
        p,
        format!("/api/v10/channels/{CHANNEL}/messages/42/reactions/%F0%9F%91%8D/@me")
    );

    // delete = the existing channel-scoped chat.delete kind
    let a = entry(
        "chat.delete",
        json!({"channel": CHANNEL, "origin_channel": CHANNEL, "message_id": "42"}),
    );
    r.exec.execute(&rq(&a)).await.unwrap();
    let (m, p, ..) = last_write(&r.dc).await;
    assert_eq!(
        (m.as_str(), p.as_str()),
        (
            "DELETE",
            format!("/api/v10/channels/{CHANNEL}/messages/42").as_str()
        )
    );

    // a different channel than the stamped origin is refused before anything is sent
    let n = write_count(&r.dc).await;
    let a = entry(
        "message.edit",
        json!({"channel": "999", "origin_channel": CHANNEL, "message_id": "42", "text": "x"}),
    );
    assert!(r.exec.execute(&rq(&a)).await.is_err());
    // an unsafe emoji is refused by the connector
    let a = entry(
        "reaction.add",
        json!({"channel": CHANNEL, "origin_channel": CHANNEL, "message_id": "42", "text": "../x"}),
    );
    assert!(r.exec.execute(&rq(&a)).await.is_err());
    assert_eq!(write_count(&r.dc).await, n);
}
