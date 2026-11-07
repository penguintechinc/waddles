#![allow(clippy::unwrap_used, clippy::expect_used)]
//! Real-path tests for the Discord MODERATION duty (`mod.kick|ban|unban|timeout`): a local fake
//! Discord (wiremock) behind the guard's exact-ip test allowance, with the production verifier,
//! substituter, `GuardedClient`, `DiscordSlots` and `DiscordDuties` running unmodified. Only the
//! component loader (a template double that mirrors the connector's output byte for byte; the
//! real WASM is exercised in `core/connector_e2e`), the credential seam and the tokenization seam
//! are doubles.

use std::collections::{BTreeMap, HashMap};
use std::net::{IpAddr, Ipv4Addr};
use std::sync::Arc;
use std::time::Duration;

use bundle_host_http::guarded::{GuardedClient, GuardedLimits};
use chrono::{TimeZone, Utc};
use connection_resolver::SecretString;
use serde_json::{json, Value};
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use super::{
    check_hierarchy, mod_payload, moderation_count, refusal_count, DiscordDuties, GuildFacts,
    NoUserResolver, PlatformUserResolver, UserResolveError, MAX_TIMEOUT_SECONDS,
};
use crate::connector::sender_exec::discord_slots::DiscordSlots;
use crate::connector::sender_exec::tests::Flag;
use crate::connector::sender_exec::*;
use crate::outbound_creds::CredError;
use crate::outbound_creds_discord::{DiscordBinding, DiscordCredentialSource};
use crate::outbound_ops::{OutboundAction, PolicyOp};

const DIGEST: &str = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const TOKEN: &str = "BOT-SECRET-ONE";
const GUILD: &str = "111";
const OWNER: &str = "900000000000000001";
const BOT: &str = "600000000000000001";
const UUID_A: &str = "123e4567-e89b-12d3-a456-426614174000";
const UUID_B: &str = "223e4567-e89b-12d3-a456-426614174000";
const UUID_C: &str = "323e4567-e89b-12d3-a456-426614174000";
const UUID_BOT: &str = "423e4567-e89b-12d3-a456-426614174000";
const UUID_OWNER: &str = "523e4567-e89b-12d3-a456-426614174000";
const UUID_D: &str = "623e4567-e89b-12d3-a456-426614174000";
const RAW_D: &str = "700000000000000004";
const RAW_A: &str = "700000000000000001";
const RAW_B: &str = "700000000000000002";
const RAW_C: &str = "700000000000000003";
const KICK_BAN_MOD: u64 = (1 << 1) | (1 << 2) | (1 << 40);

#[derive(Clone)]
struct Creds;

impl DiscordCredentialSource for Creds {
    async fn binding(&self, community_id: Option<i64>) -> Result<DiscordBinding, CredError> {
        match community_id {
            Some(1) => Ok(DiscordBinding {
                bot_token: SecretString::new(TOKEN.to_string()),
                guild_id: GUILD.to_string(),
                managed_roles: BTreeMap::new(),
            }),
            Some(c) if c > 0 => Err(CredError::NoBinding),
            _ => Err(CredError::NoCommunityScope),
        }
    }
}

/// Tokenization double: community 1 owns A, B, C, the bot and the owner.
#[derive(Clone)]
struct Users(Arc<HashMap<String, String>>);

impl Users {
    fn new() -> Self {
        Self(Arc::new(
            [
                (UUID_A, RAW_A),
                (UUID_B, RAW_B),
                (UUID_C, RAW_C),
                (UUID_BOT, BOT),
                (UUID_OWNER, OWNER),
                (UUID_D, RAW_D),
            ]
            .iter()
            .map(|(u, r)| ((*u).to_string(), (*r).to_string()))
            .collect(),
        ))
    }
}

impl PlatformUserResolver for Users {
    async fn resolve(
        &self,
        _tenant: i64,
        community: i64,
        uuid: &str,
    ) -> Result<SlotValue, UserResolveError> {
        if community != 1 {
            return Err(UserResolveError::NotFound);
        }
        self.0
            .get(uuid)
            .map(|r| SlotValue::new(r.clone()))
            .ok_or(UserResolveError::NotFound)
    }
}

/// Mirrors the connector's `mod.*` builders (`bundles/rust/discord/src/moderation.rs`).
#[derive(Clone)]
struct Tpl {
    base: String,
}

impl TemplateBuilder for Tpl {
    async fn build_request(
        &self,
        _platform: &str,
        action: &SenderAction,
    ) -> Result<BuiltTemplate, BuildError> {
        let p: Value = serde_json::from_str(&action.payload_json).unwrap();
        let user = p["user_id"].as_str().unwrap();
        let g = format!("{}/api/v10/guilds/{{target.guild_id}}", self.base);
        let json_hdr = vec![("Content-Type".to_string(), "application/json".to_string())];
        let (method, url, headers, body) = match action.kind.as_str() {
            "mod.kick" => ("DELETE", format!("{g}/members/{user}"), vec![], None),
            "mod.unban" => ("DELETE", format!("{g}/bans/{user}"), vec![], None),
            "mod.ban" => (
                "PUT",
                format!("{g}/bans/{user}"),
                json_hdr,
                Some(
                    json!({"delete_message_seconds": p.get("delete_message_seconds").cloned().unwrap_or(json!(0))}),
                ),
            ),
            "mod.timeout" => (
                "PATCH",
                format!("{g}/members/{user}"),
                json_hdr,
                Some(
                    json!({"communication_disabled_until": p.get("until").cloned().unwrap_or(Value::Null)}),
                ),
            ),
            other => panic!("unexpected kind {other}"),
        };
        Ok(BuiltTemplate {
            tpl: RequestTpl {
                method: method.to_string(),
                url,
                headers,
                body: body.map(|b| serde_json::to_vec(&b).unwrap()),
                secret_refs: vec![("Authorization".to_string(), "discord-bot".to_string())],
            },
            component_digest: DIGEST.to_string(),
        })
    }
}

type Exec = SenderExecutor<Tpl, DiscordSlots<Creds>, Flag, DiscordDuties<Creds, Users>>;

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

async fn rig() -> (MockServer, Exec) {
    let server = MockServer::start().await;
    let base = server.uri();
    let mut policy = PlatformPolicy::discord();
    policy.hosts = vec![server.address().to_string()];
    policy.allow_http = true;
    let mut digests = HashMap::new();
    digests.insert("discord".to_string(), DIGEST.to_string());
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
    let api = format!("{base}/api/v10");
    let exec = SenderExecutor::with_duties(
        Tpl { base },
        DiscordSlots::with_endpoint(Creds, guard(), api.clone()),
        Flag(true),
        DiscordDuties::with_endpoint(Creds, Users::new(), api, Duration::from_secs(30)),
        cfg,
    );
    (server, exec)
}

/// The fake guild: roles everyone(0), bot(10, kick|ban|moderate), mod(5), high(20), admin(3).
struct Guild {
    bot_perms: u64,
    bot_roles: Vec<&'static str>,
    members: Vec<(&'static str, Vec<&'static str>)>,
}

impl Guild {
    fn standard() -> Self {
        Self {
            bot_perms: KICK_BAN_MOD,
            bot_roles: vec!["1001"],
            members: vec![
                (RAW_A, vec!["2001"]),
                (RAW_B, vec!["3001"]),
                (RAW_C, vec!["4001"]),
            ],
        }
    }

    async fn mount(&self, s: &MockServer) {
        let get = |p: String, body: Value| {
            Mock::given(method("GET"))
                .and(path(p))
                .respond_with(ResponseTemplate::new(200).set_body_json(body))
        };
        get(
            format!("/api/v10/guilds/{GUILD}"),
            json!({"id": GUILD, "owner_id": OWNER}),
        )
        .mount(s)
        .await;
        get("/api/v10/users/@me".into(), json!({"id": BOT}))
            .mount(s)
            .await;
        get(
            format!("/api/v10/guilds/{GUILD}/roles"),
            json!([
                {"id": GUILD, "position": 0, "permissions": "0"},
                {"id": "1001", "position": 10, "permissions": self.bot_perms.to_string()},
                {"id": "2001", "position": 5, "permissions": "0"},
                {"id": "3001", "position": 20, "permissions": "0"},
                {"id": "4001", "position": 3, "permissions": "8"},
            ]),
        )
        .mount(s)
        .await;
        get(
            format!("/api/v10/guilds/{GUILD}/members/{BOT}"),
            json!({"roles": self.bot_roles, "user": {"id": BOT}}),
        )
        .mount(s)
        .await;
        for (raw, roles) in &self.members {
            get(
                format!("/api/v10/guilds/{GUILD}/members/{raw}"),
                json!({"roles": roles, "user": {"id": raw}}),
            )
            .mount(s)
            .await;
        }
        // any other member: Discord's "Unknown Member"
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(404).set_body_json(json!({"code": 10007})))
            .with_priority(10)
            .mount(s)
            .await;
    }
}

async fn mount_writes(s: &MockServer) {
    for m in ["DELETE", "PUT", "PATCH"] {
        Mock::given(method(m))
            .respond_with(ResponseTemplate::new(204))
            .mount(s)
            .await;
    }
}

fn op(kind: &'static str, user: &str, text: Option<&str>, origin: &str) -> OutboundAction {
    OutboundAction::PolicyOp(PolicyOp {
        op: kind,
        channel: None,
        text: text.map(str::to_string),
        message_id: None,
        user_id: Some(format!("{{user:{user}}}")),
        origin_channel: Some(origin.to_string()),
        tenant_id: Some(9),
    })
}

fn rq(a: &OutboundAction, community: i64) -> ExecRequest<'_> {
    ExecRequest {
        platform: "discord",
        action: a,
        community_id: Some(community),
    }
}

async fn writes(s: &MockServer) -> Vec<(String, String, Vec<u8>)> {
    s.received_requests()
        .await
        .unwrap()
        .iter()
        .filter(|r| r.method.as_str() != "GET")
        .map(|r| {
            (
                r.method.to_string(),
                r.url.path().to_string(),
                r.body.clone(),
            )
        })
        .collect()
}

#[tokio::test]
async fn kick_ban_unban_timeout_reach_discord_pinned_to_the_binding_guild() {
    let (s, exec) = rig().await;
    Guild::standard().mount(&s).await;
    mount_writes(&s).await;
    let before = moderation_count("mod.kick", "authorized");
    for (kind, text, verb, tail) in [
        ("mod.kick", None, "DELETE", format!("members/{RAW_A}")),
        (
            "mod.ban",
            Some(r#"{"delete_message_seconds":3600}"#),
            "PUT",
            format!("bans/{RAW_A}"),
        ),
        (
            "mod.timeout",
            Some(r#"{"duration_seconds":600}"#),
            "PATCH",
            format!("members/{RAW_A}"),
        ),
        (
            "mod.timeout",
            Some(r#"{"clear":true}"#),
            "PATCH",
            format!("members/{RAW_A}"),
        ),
    ] {
        let a = op(kind, UUID_A, text, "guild:111");
        exec.execute(&rq(&a, 1))
            .await
            .unwrap_or_else(|e| panic!("{kind}: {e}"));
        let w = writes(&s).await;
        let (m, p, body) = w.last().unwrap();
        assert_eq!(m, verb);
        assert_eq!(p, &format!("/api/v10/guilds/{GUILD}/{tail}"), "{kind}");
        if kind == "mod.ban" {
            assert_eq!(
                serde_json::from_slice::<Value>(body).unwrap(),
                json!({"delete_message_seconds": 3600})
            );
        }
    }
    // unban needs no hierarchy facts (the target is not a member)
    let a = op("mod.unban", UUID_B, None, "guild:111");
    exec.execute(&rq(&a, 1)).await.unwrap();
    assert_eq!(
        writes(&s).await.last().unwrap().1,
        format!("/api/v10/guilds/{GUILD}/bans/{RAW_B}")
    );
    // the last timeout was `clear`: null; the earlier one carried an absolute future instant
    let w = writes(&s).await;
    let timeouts: Vec<Value> = w
        .iter()
        .filter(|(m, ..)| m == "PATCH")
        .map(|(.., b)| serde_json::from_slice(b).unwrap())
        .collect();
    assert!(timeouts[0]["communication_disabled_until"]
        .as_str()
        .unwrap()
        .ends_with('Z'));
    assert!(timeouts[1]["communication_disabled_until"].is_null());
    assert!(moderation_count("mod.kick", "authorized") > before);
    // credential is the per-community bot token, composed host-side
    let reqs = s.received_requests().await.unwrap();
    assert!(reqs.iter().filter(|r| r.method.as_str() != "GET").all(|r| r
        .headers
        .get("authorization")
        .unwrap()
        == &format!("Bot {TOKEN}")));
}

#[tokio::test]
async fn a_channel_pinned_origin_in_the_binding_guild_is_accepted_and_a_foreign_one_refused() {
    let (s, exec) = rig().await;
    Guild::standard().mount(&s).await;
    mount_writes(&s).await;
    for (ch, guild) in [("5550", GUILD), ("5551", "999")] {
        Mock::given(method("GET"))
            .and(path(format!("/api/v10/channels/{ch}")))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(json!({"id": ch, "guild_id": guild})),
            )
            .mount(&s)
            .await;
    }
    let ok = op("mod.kick", UUID_A, None, "5550");
    exec.execute(&rq(&ok, 1)).await.unwrap();
    let n = writes(&s).await.len();
    let foreign = op("mod.kick", UUID_A, None, "5551");
    assert!(exec.execute(&rq(&foreign, 1)).await.is_err());
    assert_eq!(
        writes(&s).await.len(),
        n,
        "a foreign-guild channel sends nothing"
    );
}

#[tokio::test]
async fn refusals_are_explicit_and_send_nothing() {
    let (s, exec) = rig().await;
    Guild::standard().mount(&s).await;
    mount_writes(&s).await;
    let cases: Vec<(OutboundAction, &str)> = vec![
        // target outranks the bot
        (op("mod.kick", UUID_B, None, "guild:111"), "role_hierarchy"),
        (op("mod.ban", UUID_B, None, "guild:111"), "role_hierarchy"),
        // the owner and the bot itself
        (
            op("mod.ban", UUID_OWNER, None, "guild:111"),
            "target_is_owner",
        ),
        (
            op("mod.kick", UUID_BOT, None, "guild:111"),
            "target_is_self",
        ),
        // administrators cannot be timed out
        (
            op(
                "mod.timeout",
                UUID_C,
                Some(r#"{"duration_seconds":60}"#),
                "guild:111",
            ),
            "target_is_admin",
        ),
        // kick of a non-member
        (
            op(
                "mod.kick",
                "623e4567-e89b-12d3-a456-426614174000",
                None,
                "guild:111",
            ),
            "user_not_in_community",
        ),
        // asserted guild differs from the binding
        (op("mod.kick", UUID_A, None, "guild:222"), "guild_mismatch"),
        // no origin proof at all
        (
            OutboundAction::PolicyOp(PolicyOp {
                op: "mod.kick",
                channel: None,
                text: None,
                message_id: None,
                user_id: Some(format!("{{user:{UUID_A}}}")),
                origin_channel: None,
                tenant_id: Some(9),
            }),
            "missing_origin",
        ),
    ];
    for (a, _why) in &cases {
        let before = writes(&s).await.len();
        assert!(exec.execute(&rq(a, 1)).await.is_err(), "{a:?}");
        assert_eq!(writes(&s).await.len(), before, "{a:?} must send nothing");
    }
    // explicit reason codes for the hierarchy family
    for (a, why) in &cases[..5] {
        let e = exec.execute(&rq(a, 1)).await.unwrap_err();
        assert_eq!(e.code(), *why, "{a:?}");
        assert!(refusal_count(a.op(), why) >= 1);
    }
    // a raw snowflake / foreign community / bad arguments / over-cap timeout
    let raw = OutboundAction::PolicyOp(PolicyOp {
        op: "mod.kick",
        channel: None,
        text: None,
        message_id: None,
        user_id: Some(RAW_A.to_string()),
        origin_channel: Some("guild:111".into()),
        tenant_id: Some(9),
    });
    assert_eq!(
        exec.execute(&rq(&raw, 1)).await.unwrap_err().code(),
        "user_not_tokenized"
    );
    let a = op("mod.kick", UUID_A, None, "guild:-");
    assert!(
        exec.execute(&rq(&a, 2)).await.is_err(),
        "community 2 has no binding"
    );
    for text in [
        r#"{"duration_seconds":2419201}"#,
        r#"{"duration_seconds":0}"#,
        r#"{"duration_seconds":5,"clear":true}"#,
        r#"{"roles":["1"]}"#,
        "{}",
        "not json",
    ] {
        let a = op("mod.timeout", UUID_A, Some(text), "guild:111");
        assert!(exec.execute(&rq(&a, 1)).await.is_err(), "{text}");
    }
    let a = op(
        "mod.ban",
        UUID_A,
        Some(r#"{"delete_message_seconds":604801}"#),
        "guild:111",
    );
    assert!(exec.execute(&rq(&a, 1)).await.is_err());
    let a = op("mod.kick", UUID_A, Some(r#"{"x":1}"#), "guild:111");
    assert!(exec.execute(&rq(&a, 1)).await.is_err());
    assert!(
        writes(&s).await.is_empty(),
        "no refused op ever sent a write"
    );
}

#[tokio::test]
async fn a_bot_without_the_permission_is_refused_explicitly_and_a_preemptive_ban_is_allowed() {
    let (s, exec) = rig().await;
    let mut g = Guild::standard();
    g.bot_perms = 1 << 1; // kick only
    g.mount(&s).await;
    mount_writes(&s).await;
    let ban = op("mod.ban", UUID_A, None, "guild:111");
    assert_eq!(
        exec.execute(&rq(&ban, 1)).await.unwrap_err().code(),
        "bot_lacks_permission"
    );
    let kick = op("mod.kick", UUID_A, None, "guild:111");
    exec.execute(&rq(&kick, 1)).await.unwrap();
    // an unknown member can still be banned pre-emptively (bot may ban)
    let (s2, exec2) = rig().await;
    Guild::standard().mount(&s2).await;
    mount_writes(&s2).await;
    let pre = op("mod.ban", UUID_D, None, "guild:111");
    exec2.execute(&rq(&pre, 1)).await.unwrap();
    assert_eq!(
        writes(&s2).await.last().unwrap().1,
        format!("/api/v10/guilds/{GUILD}/bans/{RAW_D}")
    );
    // ...but a kick of a non-member is refused explicitly
    let kick = op("mod.kick", UUID_D, None, "guild:111");
    assert_eq!(
        exec2.execute(&rq(&kick, 1)).await.unwrap_err().code(),
        "not_in_community"
    );
}

#[tokio::test]
async fn discord_failures_while_reading_guild_state_fail_closed() {
    let (s, exec) = rig().await;
    // nothing mounted: every GET is a 404 -> lookup failure, never a send
    mount_writes(&s).await;
    let a = op("mod.kick", UUID_A, None, "guild:111");
    assert!(exec.execute(&rq(&a, 1)).await.is_err());
    assert!(writes(&s).await.is_empty());
}

fn facts(bot_roles: &[&str], target: Option<&[&str]>) -> GuildFacts {
    GuildFacts {
        guild_id: GUILD.into(),
        owner_id: OWNER.into(),
        bot_id: BOT.into(),
        target_id: RAW_A.into(),
        roles: HashMap::from([
            (GUILD.to_string(), (0, 0)),
            ("1".to_string(), (10, KICK_BAN_MOD)),
            ("2".to_string(), (10, 0)),
            ("3".to_string(), (11, 0)),
            ("9".to_string(), (1, 8)),
        ]),
        bot_roles: bot_roles.iter().map(|s| (*s).to_string()).collect(),
        target_roles: target.map(|t| t.iter().map(|s| (*s).to_string()).collect()),
    }
}

#[test]
fn hierarchy_requires_a_strictly_higher_bot_role() {
    // equal position is NOT enough (Discord's own rule)
    assert_eq!(
        check_hierarchy("mod.kick", &facts(&["1"], Some(&["2"]))),
        Err("role_hierarchy")
    );
    assert_eq!(
        check_hierarchy("mod.kick", &facts(&["1"], Some(&["3"]))),
        Err("role_hierarchy")
    );
    assert_eq!(
        check_hierarchy("mod.kick", &facts(&["1"], Some(&[]))),
        Ok(())
    );
    // a role missing from the guild list fails closed
    assert_eq!(
        check_hierarchy("mod.kick", &facts(&["1"], Some(&["404"]))),
        Err("role_lookup_failed")
    );
    // the guild owner outranks every role
    let mut owner = facts(&["1"], Some(&["3"]));
    owner.bot_id = OWNER.into();
    assert_eq!(check_hierarchy("mod.kick", &owner), Ok(()));
    // admin targets cannot be timed out but can be kicked by a higher bot
    assert_eq!(
        check_hierarchy("mod.timeout", &facts(&["1"], Some(&["9"]))),
        Err("target_is_admin")
    );
    assert_eq!(
        check_hierarchy("mod.kick", &facts(&["1"], Some(&["9"]))),
        Ok(())
    );
    // non-members: ban ok, kick/timeout refused
    assert_eq!(check_hierarchy("mod.ban", &facts(&["1"], None)), Ok(()));
    assert_eq!(
        check_hierarchy("mod.kick", &facts(&["1"], None)),
        Err("not_in_community")
    );
    assert_eq!(
        check_hierarchy("mod.timeout", &facts(&["1"], None)),
        Err("not_in_community")
    );
    // missing bot permission
    assert_eq!(
        check_hierarchy("mod.ban", &facts(&["2"], Some(&[]))),
        Err("bot_lacks_permission")
    );
    assert_eq!(
        check_hierarchy("mod.unban", &facts(&["1"], Some(&[]))),
        Err("not_a_moderation_op")
    );
}

#[test]
fn timeout_payload_is_the_host_clock_plus_the_duration() {
    let now = Utc.with_ymd_and_hms(2026, 11, 7, 12, 0, 0).unwrap();
    let mk = |text: &str| PolicyOp {
        op: "mod.timeout",
        channel: None,
        text: Some(text.to_string()),
        message_id: None,
        user_id: Some(format!("{{user:{UUID_A}}}")),
        origin_channel: Some("guild:111".into()),
        tenant_id: Some(9),
    };
    let v: Value =
        serde_json::from_str(&mod_payload(&mk(r#"{"duration_seconds":3600}"#), now).unwrap())
            .unwrap();
    assert_eq!(v["until"], "2026-11-07T13:00:00Z");
    assert_eq!(v["user_id"], format!("{{user:{UUID_A}}}"));
    let max = format!(r#"{{"duration_seconds":{MAX_TIMEOUT_SECONDS}}}"#);
    let v: Value = serde_json::from_str(&mod_payload(&mk(&max), now).unwrap()).unwrap();
    assert_eq!(v["until"], "2026-12-05T12:00:00Z");
    assert!(mod_payload(
        &mk(&format!(
            r#"{{"duration_seconds":{}}}"#,
            MAX_TIMEOUT_SECONDS + 1
        )),
        now
    )
    .is_err());
    // bare uuid targets are normalized to the placeholder; garbage is refused
    let mut bare = mk(r#"{"clear":true}"#);
    bare.user_id = Some(UUID_A.to_string());
    assert!(mod_payload(&bare, now)
        .unwrap()
        .contains(&format!("{{user:{UUID_A}}}")));
    bare.user_id = Some(RAW_A.to_string());
    assert!(mod_payload(&bare, now).is_err());
    bare.user_id = None;
    assert!(mod_payload(&bare, now).is_err());
    let _ = NoUserResolver;
}
