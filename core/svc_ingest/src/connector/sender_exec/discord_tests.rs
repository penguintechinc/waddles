#![allow(clippy::unwrap_used, clippy::expect_used)]
//! Real-path tests for the Discord host duties (T6d-2): a local fake Discord
//! (wiremock) behind the guard's exact-ip test allowance, the production
//! verifier / substituter / `GuardedClient` / `DiscordSlots` / `DiscordDuties`
//! running unmodified. Only the component loader ([`TemplateBuilder`]), the
//! hub-api credential seam and the tokenization seam are doubles.

use std::collections::{BTreeMap, HashMap};
use std::net::{IpAddr, Ipv4Addr};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_host_http::guarded::{GuardedClient, GuardedLimits};
use connection_resolver::SecretString;
use serde_json::json;
use wiremock::matchers::{body_partial_json, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use super::discord_duties::{
    refusal_count, DiscordDuties, NoUserResolver, PlatformUserResolver, UserResolveError,
    FORBIDDEN_ROLE_PERMS,
};
use super::discord_slots::DiscordSlots;
use super::tests::Flag;
use super::*;
use crate::outbound_creds::CredError;
use crate::outbound_creds_discord::{DiscordBinding, DiscordCredentialSource};
use crate::outbound_ops::{parse_outbound_entry, EventKind};

const DIGEST: &str = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const TOKEN: &str = "BOT-SECRET-ONE";
const TOKEN2: &str = "BOT-SECRET-TWO";
const CHANNEL: &str = "123456789";
const GUILD: &str = "111";
const UUID_A: &str = "123e4567-e89b-12d3-a456-426614174000";
const UUID_B: &str = "223e4567-e89b-12d3-a456-426614174000";
const RAW_A: &str = "700000000000000001";
const RAW_B: &str = "700000000000000002";

// ---- doubles -------------------------------------------------------------

/// token, guild, managed roles.
type CommunityRow = (String, String, BTreeMap<String, String>);
/// Template factory keyed by the fake server's base url.
type TplFn = Box<dyn Fn(&str) -> RequestTpl>;

#[derive(Clone)]
struct Creds {
    map: Arc<HashMap<i64, CommunityRow>>,
    calls: Arc<AtomicUsize>,
    err: Option<CredError>,
}

fn roles() -> BTreeMap<String, String> {
    [
        ("vip", "222"),
        ("adm", "333"),
        ("mgr", "334"),
        ("guildmgr", "335"),
        ("mixed", "336"),
        ("ghost", "999999"),
    ]
    .iter()
    .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
    .collect()
}

impl Creds {
    fn new() -> Self {
        let mut m = HashMap::new();
        m.insert(1, (TOKEN.to_string(), GUILD.to_string(), roles()));
        m.insert(2, (TOKEN2.to_string(), "222000".to_string(), roles()));
        Self {
            map: Arc::new(m),
            calls: Arc::new(AtomicUsize::new(0)),
            err: None,
        }
    }
}

impl DiscordCredentialSource for Creds {
    async fn binding(&self, community_id: Option<i64>) -> Result<DiscordBinding, CredError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if let Some(e) = self.err {
            return Err(e);
        }
        let c = community_id
            .filter(|c| *c > 0)
            .ok_or(CredError::NoCommunityScope)?;
        let (t, g, r) = self.map.get(&c).ok_or(CredError::NoBinding)?;
        Ok(DiscordBinding {
            bot_token: SecretString::new(t.clone()),
            guild_id: g.clone(),
            managed_roles: r.clone(),
        })
    }
}

/// Tokenization double: `(community, uuid) -> raw`; anything else NotFound.
#[derive(Clone)]
struct Users {
    map: Arc<HashMap<(i64, String), String>>,
    calls: Arc<AtomicUsize>,
    bad_answer: bool,
    /// Tenants the hook passed, in call order.
    tenants: Arc<Mutex<Vec<i64>>>,
    fail: Option<UserResolveError>,
}

impl Users {
    fn new() -> Self {
        let mut m = HashMap::new();
        m.insert((1, UUID_A.to_string()), RAW_A.to_string());
        m.insert((1, UUID_B.to_string()), RAW_B.to_string());
        // Community 2 owns UUID_A too, but as a DIFFERENT person.
        m.insert((2, UUID_A.to_string()), "800000000000000009".to_string());
        Self {
            map: Arc::new(m),
            calls: Arc::new(AtomicUsize::new(0)),
            bad_answer: false,
            tenants: Arc::new(Mutex::new(Vec::new())),
            fail: None,
        }
    }
}

impl PlatformUserResolver for Users {
    async fn resolve(
        &self,
        tenant: i64,
        community: i64,
        uuid: &str,
    ) -> Result<SlotValue, UserResolveError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.tenants.lock().unwrap().push(tenant);
        if let Some(e) = self.fail {
            return Err(e);
        }
        if self.bad_answer {
            return Ok(SlotValue::new("not-a-snowflake".to_string()));
        }
        self.map
            .get(&(community, uuid.to_string()))
            .map(|r| SlotValue::new(r.clone()))
            .ok_or(UserResolveError::NotFound)
    }
}

#[derive(Clone)]
struct Tpl {
    tpl: Arc<Mutex<RequestTpl>>,
    calls: Arc<Mutex<Vec<SenderAction>>>,
}

impl Tpl {
    fn new(tpl: RequestTpl) -> Self {
        Self {
            tpl: Arc::new(Mutex::new(tpl)),
            calls: Arc::new(Mutex::new(Vec::new())),
        }
    }
    fn calls(&self) -> usize {
        self.calls.lock().unwrap().len()
    }
}

impl TemplateBuilder for Tpl {
    async fn build_request(
        &self,
        _platform: &str,
        action: &SenderAction,
    ) -> Result<BuiltTemplate, BuildError> {
        self.calls.lock().unwrap().push(action.clone());
        Ok(BuiltTemplate {
            tpl: self.tpl.lock().unwrap().clone(),
            component_digest: DIGEST.to_string(),
        })
    }
}

type Exec = SenderExecutor<Tpl, DiscordSlots<Creds>, Flag, DiscordDuties<Creds, Users>>;

struct Rig {
    server: MockServer,
    exec: Exec,
    tpl: Tpl,
    creds: Creds,
    users: Users,
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

fn dm_tpl(base: &str, recipient: &str) -> RequestTpl {
    RequestTpl {
        method: "POST".into(),
        url: format!("{base}/api/v10/channels/{{dm.channel_id}}/messages"),
        headers: vec![
            ("Content-Type".into(), "application/json".into()),
            ("X-Waddle-Host-Dm-Recipient".into(), recipient.into()),
        ],
        body: Some(
            serde_json::to_vec(&json!({
                "content": "dm",
                "allowed_mentions": {"parse": ["users"]}
            }))
            .unwrap(),
        ),
        secret_refs: vec![("Authorization".into(), "discord-bot".into())],
    }
}

fn role_tpl(base: &str, method: &str, guild: &str, user: &str, role: &str) -> RequestTpl {
    RequestTpl {
        method: method.into(),
        url: format!("{base}/api/v10/guilds/{guild}/members/{user}/roles/{role}"),
        headers: vec![],
        body: None,
        secret_refs: vec![("Authorization".into(), "discord-bot".into())],
    }
}

async fn rig_with(tpl: impl FnOnce(&str) -> RequestTpl, ttl: Duration) -> Rig {
    rig_full(tpl, ttl, None).await
}

async fn rig_full(
    tpl: impl FnOnce(&str) -> RequestTpl,
    ttl: Duration,
    err: Option<CredError>,
) -> Rig {
    let server = MockServer::start().await;
    let base = server.uri();
    let tpl = Tpl::new(tpl(&base));
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
    let mut creds = Creds::new();
    creds.err = err;
    let users = Users::new();
    let exec = SenderExecutor::with_duties(
        tpl.clone(),
        DiscordSlots::with_endpoint(creds.clone(), guard(), api.clone()),
        Flag(true),
        DiscordDuties::with_endpoint(creds.clone(), users.clone(), api, ttl),
        cfg,
    );
    Rig {
        server,
        exec,
        tpl,
        creds,
        users,
    }
}

async fn rig(tpl: impl FnOnce(&str) -> RequestTpl) -> Rig {
    rig_with(tpl, Duration::from_secs(30)).await
}

async fn mock_channel(s: &MockServer, ch: &str, guild: Option<&str>) {
    let body = guild.map_or_else(|| json!({"id": ch}), |g| json!({"id": ch, "guild_id": g}));
    Mock::given(method("GET"))
        .and(path(format!("/api/v10/channels/{ch}")))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .mount(s)
        .await;
}

async fn mock_member(s: &MockServer, raw: &str, status: u16, code: Option<u64>) {
    let mut r = ResponseTemplate::new(status);
    if let Some(c) = code {
        r = r.set_body_json(json!({"code": c, "message": "x"}));
    } else if status == 200 {
        r = r.set_body_json(json!({"user": {"id": raw}}));
    }
    Mock::given(method("GET"))
        .and(path(format!("/api/v10/guilds/{GUILD}/members/{raw}")))
        .respond_with(r)
        .mount(s)
        .await;
}

async fn mock_open_dm(s: &MockServer, raw: &str, channel: &str) {
    Mock::given(method("POST"))
        .and(path("/api/v10/users/@me/channels"))
        .and(body_partial_json(json!({"recipient_id": raw})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": channel})))
        .mount(s)
        .await;
}

async fn mock_message(s: &MockServer, channel: &str) {
    Mock::given(method("POST"))
        .and(path(format!("/api/v10/channels/{channel}/messages")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": "1"})))
        .mount(s)
        .await;
}

async fn mock_roles(s: &MockServer) {
    Mock::given(method("GET"))
        .and(path(format!("/api/v10/guilds/{GUILD}/roles")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([
            {"id": "111", "permissions": "0"},
            {"id": "222", "permissions": "1024"},
            {"id": "333", "permissions": "8"},
            {"id": "334", "permissions": "268435456"},
            {"id": "335", "permissions": "32"},
            {"id": "336", "permissions": "2147483656"},
        ])))
        .mount(s)
        .await;
}

async fn mock_role_put(s: &MockServer, verb: &str, raw: &str, role: &str) {
    Mock::given(method(verb))
        .and(path(format!(
            "/api/v10/guilds/{GUILD}/members/{raw}/roles/{role}"
        )))
        .respond_with(ResponseTemplate::new(204))
        .mount(s)
        .await;
}

fn dm(user: &str) -> OutboundAction {
    OutboundAction::DmSend {
        user_id: user.to_string(),
        text: "hello there".to_string(),
        origin_channel: CHANNEL.to_string(),
        tenant_id: Some(9),
    }
}

fn role(add: bool, user: &str, key: &str) -> OutboundAction {
    OutboundAction::RoleOp {
        add,
        user_id: user.to_string(),
        role_key: key.to_string(),
        origin_channel: CHANNEL.to_string(),
        tenant_id: Some(9),
    }
}

fn rq<'a>(a: &'a OutboundAction, community: i64) -> ExecRequest<'a> {
    ExecRequest {
        platform: "discord",
        action: a,
        community_id: Some(community),
    }
}

async fn seen(s: &MockServer) -> Vec<(String, String)> {
    s.received_requests()
        .await
        .unwrap()
        .iter()
        .map(|r| (r.method.to_string(), r.url.path().to_string()))
        .collect()
}

async fn writes(s: &MockServer) -> usize {
    seen(s)
        .await
        .iter()
        .filter(|(m, p)| {
            (m == "POST" && p.ends_with("/messages"))
                || m == "PUT"
                || m == "DELETE"
                || (m == "POST" && p.ends_with("/users/@me/channels"))
        })
        .count()
}

fn code(e: &ExecError) -> &'static str {
    e.code()
}

// ---- dm.send -------------------------------------------------------------

#[tokio::test]
async fn dm_send_opens_the_dm_itself_and_fills_the_slot_from_the_reply() {
    let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    mock_member(&r.server, RAW_A, 200, None).await;
    mock_open_dm(&r.server, RAW_A, "555").await;
    mock_message(&r.server, "555").await;

    let a = dm(UUID_A);
    r.exec.execute(&rq(&a, 1)).await.unwrap();

    let reqs = r.server.received_requests().await.unwrap();
    let msg = reqs
        .iter()
        .find(|q| q.url.path() == "/api/v10/channels/555/messages")
        .expect("message went to the channel Discord returned");
    // host-composed credential; pseudo-header never forwarded
    assert_eq!(
        msg.headers.get("authorization").unwrap(),
        &format!("Bot {TOKEN}")
    );
    assert!(!msg.headers.iter().any(|(k, _)| k
        .as_str()
        .eq_ignore_ascii_case("x-waddle-host-dm-recipient")));
    let body: serde_json::Value = serde_json::from_slice(&msg.body).unwrap();
    assert_eq!(body["content"], "dm");
    // the raw recipient id appears ONLY in the open-DM call
    for q in &reqs {
        let has_raw = String::from_utf8_lossy(&q.body).contains(RAW_A);
        let open = q.url.path() == "/api/v10/users/@me/channels";
        assert!(!has_raw || open, "raw id leaked into {}", q.url.path());
    }
    // the connector saw only the tokenized placeholder
    let built = r.tpl.calls.lock().unwrap()[0].payload_json.clone();
    assert!(built.contains(&format!("{{user:{UUID_A}}}")) && !built.contains(RAW_A));
    // order: gate (channel, membership) precedes the DM open and the send
    let order = seen(&r.server).await;
    let pos = |needle: &str| order.iter().position(|(_, p)| p.contains(needle)).unwrap();
    assert!(pos("/members/") < pos("/users/@me/channels"));
    assert!(pos("/users/@me/channels") < pos("/channels/555/messages"));
}

#[tokio::test]
async fn a_dm_channel_from_another_dm_send_is_never_reused() {
    let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    for (raw, _) in [(RAW_A, 0), (RAW_B, 0)] {
        mock_member(&r.server, raw, 200, None).await;
    }
    mock_open_dm(&r.server, RAW_A, "555").await;
    mock_open_dm(&r.server, RAW_B, "666").await;
    mock_message(&r.server, "555").await;
    mock_message(&r.server, "666").await;

    r.exec.execute(&rq(&dm(UUID_A), 1)).await.unwrap();
    // second operation, other recipient: must open ITS OWN channel
    *r.tpl.tpl.lock().unwrap() = dm_tpl(&r.server.uri(), &format!("{{user:{UUID_B}}}"));
    r.exec.execute(&rq(&dm(UUID_B), 1)).await.unwrap();

    let s = seen(&r.server).await;
    let opens = s
        .iter()
        .filter(|(_, p)| p.ends_with("/users/@me/channels"))
        .count();
    assert_eq!(opens, 2, "every dm.send opens its own DM");
    let to = |ch: &str| {
        s.iter()
            .filter(|(m, p)| m == "POST" && p == &format!("/api/v10/channels/{ch}/messages"))
            .count()
    };
    assert_eq!((to("555"), to("666")), (1, 1));
}

#[tokio::test]
async fn the_gate_refuses_a_non_member_a_foreign_channel_and_a_failed_lookup() {
    // non-member (Discord "Unknown Member")
    let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    mock_member(&r.server, RAW_A, 404, Some(10007)).await;
    let before = refusal_count("dm.send", "not_in_community");
    let e = r.exec.execute(&rq(&dm(UUID_A), 1)).await.unwrap_err();
    assert_eq!(e, ExecError::Slot(SlotError::Refused("not_in_community")));
    assert!(e.is_policy_refusal());
    assert_eq!(writes(&r.server).await, 0);
    assert_eq!(r.tpl.calls(), 0, "refused before any template was built");
    assert!(refusal_count("dm.send", "not_in_community") > before);

    // the pinned channel belongs to ANOTHER guild
    let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some("999")).await;
    mock_member(&r.server, RAW_A, 200, None).await;
    let e = r.exec.execute(&rq(&dm(UUID_A), 1)).await.unwrap_err();
    assert_eq!(code(&e), "not_in_community");
    assert_eq!(writes(&r.server).await, 0);

    // a channel with no guild
    let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, None).await;
    let e = r.exec.execute(&rq(&dm(UUID_A), 1)).await.unwrap_err();
    assert_eq!(code(&e), "not_in_community");

    // membership lookup failure fails closed (nothing sent)
    let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    mock_member(&r.server, RAW_A, 500, None).await;
    let e = r.exec.execute(&rq(&dm(UUID_A), 1)).await.unwrap_err();
    assert_eq!(code(&e), "membership_lookup_failed");
    assert_eq!(writes(&r.server).await, 0);

    // an ambiguous 404 (not an Unknown Member code) is never read as "no" OR "yes"
    let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    mock_member(&r.server, RAW_A, 404, Some(10004)).await;
    let e = r.exec.execute(&rq(&dm(UUID_A), 1)).await.unwrap_err();
    assert_eq!(code(&e), "membership_lookup_failed");
}

#[tokio::test]
async fn user_references_are_community_scoped_and_never_raw() {
    // UUID_A exists in community 2 as a different person; in community 1 it is
    // RAW_A. Community 2's guild/token are used, so RAW_A can never be reached.
    let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some("222000")).await;
    let e = r.exec.execute(&rq(&dm(UUID_A), 2)).await.unwrap_err();
    // community 2's resolver answered a DIFFERENT raw id and that user is not in the guild mock
    assert!(matches!(
        code(&e),
        "membership_lookup_failed" | "not_in_community"
    ));
    assert!(!seen(&r.server).await.iter().any(|(_, p)| p.contains(RAW_A)));

    // a UUID that does not belong to the community at all
    let unknown = "323e4567-e89b-12d3-a456-426614174000";
    let r = rig(|b| dm_tpl(b, &format!("{{user:{unknown}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    let e = r.exec.execute(&rq(&dm(unknown), 1)).await.unwrap_err();
    assert_eq!(code(&e), "user_not_in_community");
    assert!(e.is_policy_refusal());
    assert_eq!(writes(&r.server).await, 0);

    // a raw snowflake is refused outright (PII rule) and never resolved
    let r = rig(|b| dm_tpl(b, "{user:x}")).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    let e = r.exec.execute(&rq(&dm(RAW_A), 1)).await.unwrap_err();
    assert_eq!(code(&e), "user_not_tokenized");
    assert_eq!(r.users.calls.load(Ordering::SeqCst), 0);

    // wrapper + upper-case UUID are accepted and canonicalized
    let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    mock_member(&r.server, RAW_A, 200, None).await;
    mock_open_dm(&r.server, RAW_A, "555").await;
    mock_message(&r.server, "555").await;
    let wrapped = dm(&format!("{{user:{}}}", UUID_A.to_uppercase()));
    r.exec.execute(&rq(&wrapped, 1)).await.unwrap();
}

#[tokio::test]
async fn tokenization_failures_fail_closed_and_loud() {
    // resolver answers a non-snowflake: never used
    let mut r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    let users = Users {
        bad_answer: true,
        ..Users::new()
    };
    let api = format!("{}/api/v10", r.server.uri());
    r.exec = SenderExecutor::with_duties(
        r.tpl.clone(),
        DiscordSlots::with_endpoint(r.creds.clone(), guard(), api.clone()),
        Flag(true),
        DiscordDuties::with_endpoint(r.creds.clone(), users, api.clone(), Duration::from_secs(30)),
        ExecutorConfig::new(
            vec![{
                let mut p = PlatformPolicy::discord();
                p.hosts = vec![r.server.address().to_string()];
                p.allow_http = true;
                p
            }],
            HashMap::from([("discord".to_string(), DIGEST.to_string())]),
        )
        .unwrap()
        .with_client(
            GuardedLimits::default(),
            vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        ),
    );
    let e = r.exec.execute(&rq(&dm(UUID_A), 1)).await.unwrap_err();
    assert_eq!(code(&e), "tokenization_bad_answer");

    // no tokenization service wired: Unavailable, retryable, never a guess
    let exec = SenderExecutor::with_duties(
        r.tpl.clone(),
        DiscordSlots::with_endpoint(r.creds.clone(), guard(), api.clone()),
        Flag(true),
        DiscordDuties::with_endpoint(
            r.creds.clone(),
            NoUserResolver,
            api,
            Duration::from_secs(30),
        ),
        ExecutorConfig::new(
            vec![{
                let mut p = PlatformPolicy::discord();
                p.hosts = vec![r.server.address().to_string()];
                p.allow_http = true;
                p
            }],
            HashMap::from([("discord".to_string(), DIGEST.to_string())]),
        )
        .unwrap()
        .with_client(
            GuardedLimits::default(),
            vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        ),
    );
    let e = exec.execute(&rq(&dm(UUID_A), 1)).await.unwrap_err();
    assert_eq!(code(&e), "tokenization_unavailable");
    assert!(
        !e.is_policy_refusal(),
        "unavailable is retryable, not a refusal"
    );
}

#[tokio::test]
async fn hostile_dm_templates_are_refused_and_send_nothing() {
    let cases: Vec<(&str, TplFn)> = vec![
        (
            "literal channel (a dm.post-style payload channel)",
            Box::new(|b: &str| {
                let mut t = dm_tpl(b, &format!("{{user:{UUID_A}}}"));
                t.url = format!("{b}/api/v10/channels/999/messages");
                t
            }),
        ),
        (
            "recipient header names someone else",
            Box::new(|b: &str| dm_tpl(b, &format!("{{user:{UUID_B}}}"))),
        ),
        (
            "recipient header carries a raw snowflake",
            Box::new(|b: &str| dm_tpl(b, RAW_A)),
        ),
        (
            "another host-only pseudo-header",
            Box::new(|b: &str| {
                let mut t = dm_tpl(b, &format!("{{user:{UUID_A}}}"));
                t.headers.push(("X-Waddle-Host-Other".into(), "x".into()));
                t
            }),
        ),
        (
            "the template carries its own Authorization",
            Box::new(|b: &str| {
                let mut t = dm_tpl(b, &format!("{{user:{UUID_A}}}"));
                t.headers
                    .push(("Authorization".into(), "Bot stolen".into()));
                t
            }),
        ),
        (
            "channel slot from the stamp instead of the opened DM",
            Box::new(|b: &str| {
                let mut t = dm_tpl(b, &format!("{{user:{UUID_A}}}"));
                t.url = format!("{b}/api/v10/channels/{{target.channel_id}}/messages");
                t
            }),
        ),
    ];
    for (name, mk) in cases {
        let r = rig(|b| mk(b)).await;
        mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
        mock_member(&r.server, RAW_A, 200, None).await;
        mock_open_dm(&r.server, RAW_A, "555").await;
        mock_message(&r.server, "555").await;
        let res = r.exec.execute(&rq(&dm(UUID_A), 1)).await;
        assert!(res.is_err(), "{name} must be refused");
        assert_eq!(writes(&r.server).await, 0, "{name}: nothing may be sent");
    }
}

#[tokio::test]
async fn the_pseudo_header_is_still_forbidden_for_every_other_kind() {
    // chat.send through the same executor: the strip is DM-only.
    let r = rig(|b| {
        let mut t = dm_tpl(b, "plain");
        t.url = format!("{b}/api/v10/channels/{{target.channel_id}}/messages");
        t
    })
    .await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    let a = OutboundAction::ChatSend {
        channel: CHANNEL.into(),
        text: "x".into(),
        origin_channel: Some(CHANNEL.into()),
    };
    let e = r.exec.execute(&rq(&a, 1)).await.unwrap_err();
    assert_eq!(code(&e), "header_not_allowed");
    assert_eq!(writes(&r.server).await, 0);
}

// ---- role ops ------------------------------------------------------------

fn good_role(b: &str, verb: &str, key: &str) -> RequestTpl {
    role_tpl(
        b,
        verb,
        "{target.guild_id}",
        &format!("{{user:{UUID_A}}}"),
        &format!("{{target.role.{key}}}"),
    )
}

#[tokio::test]
async fn role_add_and_remove_map_the_key_pin_the_guild_and_build_the_url() {
    for (add, verb) in [(true, "PUT"), (false, "DELETE")] {
        let r = rig(|b| good_role(b, verb, "vip")).await;
        mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
        mock_roles(&r.server).await;
        mock_role_put(&r.server, verb, RAW_A, "222").await;
        r.exec
            .execute(&rq(&role(add, UUID_A, "vip"), 1))
            .await
            .unwrap();
        let reqs = r.server.received_requests().await.unwrap();
        let q = reqs.iter().find(|q| q.method.as_str() == verb).unwrap();
        assert_eq!(
            q.url.path(),
            format!("/api/v10/guilds/{GUILD}/members/{RAW_A}/roles/222")
        );
        assert_eq!(
            q.headers.get("authorization").unwrap(),
            &format!("Bot {TOKEN}")
        );
    }
}

fn guild_role(add: bool, scope: &str) -> OutboundAction {
    OutboundAction::RoleOp {
        add,
        user_id: UUID_A.to_string(),
        role_key: "vip".to_string(),
        origin_channel: scope.to_string(),
        tenant_id: Some(9),
    }
}

/// Guild-scoped role ops (gateway / external events carry no channel): the op lands ONLY in the
/// community's own binding guild; no channel lookup happens; an agreeing gateway-guild assertion
/// is allowed, a foreign one is refused before anything is sent.
#[tokio::test]
async fn guild_scoped_role_ops_pin_the_binding_guild_and_refuse_a_foreign_asserted_guild() {
    for (add, verb) in [(true, "PUT"), (false, "DELETE")] {
        for scope in ["guild:-".to_string(), format!("guild:{GUILD}")] {
            let r = rig(|b| good_role(b, verb, "vip")).await;
            mock_roles(&r.server).await;
            mock_role_put(&r.server, verb, RAW_A, "222").await;
            r.exec
                .execute(&rq(&guild_role(add, &scope), 1))
                .await
                .unwrap();
            let reqs = seen(&r.server).await;
            assert!(
                reqs.contains(&(
                    verb.to_string(),
                    format!("/api/v10/guilds/{GUILD}/members/{RAW_A}/roles/222")
                )),
                "{scope}: {reqs:?}"
            );
            assert!(
                reqs.iter()
                    .all(|(_, p)| !p.starts_with("/api/v10/channels/")),
                "no channel lookup for a guild-scoped op"
            );
        }
    }
    // A gateway event from ANOTHER guild than the community's paired one is refused.
    let r = rig(|b| good_role(b, "PUT", "vip")).await;
    mock_roles(&r.server).await;
    mock_role_put(&r.server, "PUT", RAW_A, "222").await;
    let before = refusal_count("role.add", "guild_mismatch");
    let e = r
        .exec
        .execute(&rq(&guild_role(true, "guild:999000999"), 1))
        .await
        .unwrap_err();
    assert_eq!(e, ExecError::Slot(SlotError::Refused("guild_mismatch")));
    assert_eq!(writes(&r.server).await, 0, "nothing was sent");
    assert!(refusal_count("role.add", "guild_mismatch") > before);
}

/// A community's op is pinned to ITS binding guild even when another community is bound to a
/// different guild (Wix-style external event: scope `guild:-`).
#[tokio::test]
async fn an_external_event_op_can_only_touch_the_communitys_own_paired_guild() {
    let r = rig(|b| good_role(b, "PUT", "vip")).await;
    mock_roles(&r.server).await;
    mock_role_put(&r.server, "PUT", RAW_A, "222").await;
    // community 2 is bound to guild 222000 with ITS OWN token: the request goes there only.
    Mock::given(method("GET"))
        .and(path("/api/v10/guilds/222000/roles"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!([
            {"id": "222", "permissions": "0"}
        ])))
        .mount(&r.server)
        .await;
    Mock::given(method("PUT"))
        .and(path(
            "/api/v10/guilds/222000/members/800000000000000009/roles/222",
        ))
        .respond_with(ResponseTemplate::new(204))
        .mount(&r.server)
        .await;
    r.exec
        .execute(&rq(&guild_role(true, "guild:-"), 2))
        .await
        .unwrap();
    let reqs = r.server.received_requests().await.unwrap();
    let put = reqs.iter().find(|q| q.method.as_str() == "PUT").unwrap();
    assert_eq!(
        put.url.path(),
        "/api/v10/guilds/222000/members/800000000000000009/roles/222"
    );
    assert_eq!(
        put.headers.get("authorization").unwrap(),
        &format!("Bot {TOKEN2}")
    );
}

/// A community with several usable Discord bindings is refused with the typed
/// `ambiguous_paired_guild` BEFORE anything is built or sent -- for channel-pinned and
/// guild-scoped ops alike; the guild is never guessed.
#[tokio::test]
async fn several_paired_guilds_are_refused_explicitly() {
    for scope in ["guild:-".to_string(), format!("guild:{GUILD}")] {
        let r = rig_full(
            |b| good_role(b, "PUT", "vip"),
            Duration::from_secs(30),
            Some(CredError::AmbiguousBinding),
        )
        .await;
        mock_roles(&r.server).await;
        mock_role_put(&r.server, "PUT", RAW_A, "222").await;
        let e = r
            .exec
            .execute(&rq(&guild_role(true, &scope), 1))
            .await
            .unwrap_err();
        assert_eq!(
            e,
            ExecError::Slot(SlotError::Refused("ambiguous_paired_guild")),
            "{scope}"
        );
        assert_eq!(writes(&r.server).await, 0, "nothing was sent");
    }
}

#[tokio::test]
async fn malformed_guild_scopes_are_refused() {
    for scope in [
        "guild:",
        "guild:abc",
        "guild:12345678901234567890123",
        "guild:-x",
    ] {
        let r = rig(|b| good_role(b, "PUT", "vip")).await;
        mock_roles(&r.server).await;
        mock_role_put(&r.server, "PUT", RAW_A, "222").await;
        let e = r
            .exec
            .execute(&rq(&guild_role(true, scope), 1))
            .await
            .unwrap_err();
        assert!(matches!(e, ExecError::Pin(_)), "{scope}: {e:?}");
        assert_eq!(writes(&r.server).await, 0, "{scope}");
    }
}

#[test]
fn guild_scope_wire_form() {
    use super::role_guild_scope as g;
    assert_eq!(g("guild:-"), Some(None));
    assert_eq!(g("guild:123"), Some(Some("123")));
    for bad in [
        "123",
        "guild:",
        "guild:x",
        "Guild:1",
        "guild:1 ",
        &format!("guild:{}", "1".repeat(21)),
    ] {
        assert_eq!(g(bad), None, "{bad}");
    }
}

#[tokio::test]
async fn a_privileged_role_is_refused_even_when_allowlisted() {
    // ADMINISTRATOR, MANAGE_ROLES, MANAGE_GUILD, and a mixed-bit set carrying ADMINISTRATOR
    for key in ["adm", "mgr", "guildmgr", "mixed"] {
        let r = rig(|b| good_role(b, "PUT", key)).await;
        mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
        mock_roles(&r.server).await;
        // a PUT mock exists for every role: if the gate leaked, it would be hit
        for id in ["333", "334", "335", "336"] {
            mock_role_put(&r.server, "PUT", RAW_A, id).await;
        }
        let before = refusal_count("role.add", "role_privileged");
        let e = r
            .exec
            .execute(&rq(&role(true, UUID_A, key), 1))
            .await
            .unwrap_err();
        assert_eq!(
            e,
            ExecError::Slot(SlotError::Refused("role_privileged")),
            "{key}"
        );
        assert!(e.is_policy_refusal());
        assert_eq!(writes(&r.server).await, 0, "{key}: nothing sent");
        assert!(refusal_count("role.add", "role_privileged") > before);
    }
    assert_eq!(FORBIDDEN_ROLE_PERMS, (1 << 3) | (1 << 5) | (1 << 28));
}

#[tokio::test]
async fn role_keys_outside_the_allowlist_or_shaped_like_snowflakes_are_refused() {
    for key in ["nope", "222", "VIP", "../x", "vip/../adm"] {
        let r = rig(|b| good_role(b, "PUT", "vip")).await;
        mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
        mock_roles(&r.server).await;
        mock_role_put(&r.server, "PUT", RAW_A, "222").await;
        let e = r
            .exec
            .execute(&rq(&role(true, UUID_A, key), 1))
            .await
            .unwrap_err();
        assert_eq!(code(&e), "role_not_managed", "{key}");
        assert_eq!(writes(&r.server).await, 0);
    }
}

#[tokio::test]
async fn role_lookup_failures_fail_closed() {
    // roles endpoint 500
    let r = rig(|b| good_role(b, "PUT", "vip")).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    Mock::given(method("GET"))
        .and(path(format!("/api/v10/guilds/{GUILD}/roles")))
        .respond_with(ResponseTemplate::new(500))
        .mount(&r.server)
        .await;
    mock_role_put(&r.server, "PUT", RAW_A, "222").await;
    let e = r
        .exec
        .execute(&rq(&role(true, UUID_A, "vip"), 1))
        .await
        .unwrap_err();
    assert_eq!(code(&e), "role_lookup_failed");
    assert_eq!(writes(&r.server).await, 0);

    // malformed permissions poison the whole answer
    let r = rig(|b| good_role(b, "PUT", "vip")).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    Mock::given(method("GET"))
        .and(path(format!("/api/v10/guilds/{GUILD}/roles")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([
            {"id": "222", "permissions": "0"}, {"id": "333", "permissions": "lots"}
        ])))
        .mount(&r.server)
        .await;
    mock_role_put(&r.server, "PUT", RAW_A, "222").await;
    let e = r
        .exec
        .execute(&rq(&role(true, UUID_A, "vip"), 1))
        .await
        .unwrap_err();
    assert_eq!(code(&e), "role_lookup_failed");
    assert_eq!(writes(&r.server).await, 0);

    // a role id absent from the guild's list (deleted role) is refused
    let r = rig(|b| good_role(b, "PUT", "ghost")).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    mock_roles(&r.server).await;
    let e = r
        .exec
        .execute(&rq(&role(true, UUID_A, "ghost"), 1))
        .await
        .unwrap_err();
    assert_eq!(code(&e), "role_not_found");
}

#[tokio::test]
async fn the_guild_is_pinned_from_the_binding_never_the_payload_or_template() {
    // a queue entry that tries to smuggle a guild is parsed with the field ignored
    let raw = format!(
        r#"{{"v":1,"op":"role.add","platform":"discord","user_id":"{UUID_A}","text":"vip","origin_channel":"{CHANNEL}","guild_id":"999","community_id":1,"tenant_id":9}}"#
    );
    let entry = parse_outbound_entry(&raw, "discord").unwrap();
    assert_eq!(entry.action, role(true, UUID_A, "vip"));
    let r = rig(|b| good_role(b, "PUT", "vip")).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    mock_roles(&r.server).await;
    mock_role_put(&r.server, "PUT", RAW_A, "222").await;
    r.exec.execute(&rq(&entry.action, 1)).await.unwrap();
    assert!(seen(&r.server)
        .await
        .iter()
        .all(|(_, p)| !p.contains("999/")));

    // a hostile template that hard-codes another guild instead of the slot
    let r = rig(|b| {
        role_tpl(
            b,
            "PUT",
            "999",
            &format!("{{user:{UUID_A}}}"),
            "{target.role.vip}",
        )
    })
    .await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    mock_roles(&r.server).await;
    let e = r
        .exec
        .execute(&rq(&role(true, UUID_A, "vip"), 1))
        .await
        .unwrap_err();
    assert_eq!(code(&e), "path_mismatch");
    assert_eq!(writes(&r.server).await, 0);
}

#[tokio::test]
async fn a_template_naming_a_different_role_or_user_than_the_entry_is_refused() {
    for (name, mk) in [
        (
            "other role key",
            Box::new(|b: &str| good_role(b, "PUT", "adm")) as Box<dyn Fn(&str) -> RequestTpl>,
        ),
        (
            "other user",
            Box::new(|b: &str| {
                role_tpl(
                    b,
                    "PUT",
                    "{target.guild_id}",
                    &format!("{{user:{UUID_B}}}"),
                    "{target.role.vip}",
                )
            }),
        ),
        (
            "raw role snowflake",
            Box::new(|b: &str| {
                role_tpl(
                    b,
                    "PUT",
                    "{target.guild_id}",
                    &format!("{{user:{UUID_A}}}"),
                    "333",
                )
            }),
        ),
        (
            "wrong method",
            Box::new(|b: &str| good_role(b, "POST", "vip")),
        ),
    ] {
        let r = rig(|b| mk(b)).await;
        mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
        mock_roles(&r.server).await;
        for id in ["222", "333"] {
            mock_role_put(&r.server, "PUT", RAW_A, id).await;
        }
        let res = r.exec.execute(&rq(&role(true, UUID_A, "vip"), 1)).await;
        assert!(res.is_err(), "{name}");
        assert_eq!(writes(&r.server).await, 0, "{name}");
    }
}

#[tokio::test]
async fn a_role_for_another_communitys_user_or_channel_is_refused() {
    let r = rig(|b| good_role(b, "PUT", "vip")).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    mock_roles(&r.server).await;
    mock_role_put(&r.server, "PUT", RAW_A, "222").await;
    let unknown = "323e4567-e89b-12d3-a456-426614174000";
    let e = r
        .exec
        .execute(&rq(&role(true, unknown, "vip"), 1))
        .await
        .unwrap_err();
    assert_eq!(code(&e), "user_not_in_community");

    // pinned channel not in the binding's guild
    let r = rig(|b| good_role(b, "PUT", "vip")).await;
    mock_channel(&r.server, CHANNEL, Some("999")).await;
    mock_roles(&r.server).await;
    let e = r
        .exec
        .execute(&rq(&role(true, UUID_A, "vip"), 1))
        .await
        .unwrap_err();
    assert_eq!(code(&e), "channel_not_owned");
    assert_eq!(writes(&r.server).await, 0);
    // the gate (not just the later slot source) refused: no template was built
    assert_eq!(r.tpl.calls(), 0);
}

#[tokio::test]
async fn the_role_cache_is_short_lived_and_bounded() {
    // within TTL: one roles GET for two ops; with TTL 0 every lookup re-fetches (gate + slot, x2 ops)
    for (ttl, expect) in [(Duration::from_secs(60), 1usize), (Duration::ZERO, 4usize)] {
        let r = rig_with(|b| good_role(b, "PUT", "vip"), ttl).await;
        mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
        mock_roles(&r.server).await;
        mock_role_put(&r.server, "PUT", RAW_A, "222").await;
        for _ in 0..2 {
            r.exec
                .execute(&rq(&role(true, UUID_A, "vip"), 1))
                .await
                .unwrap();
        }
        let gets = seen(&r.server)
            .await
            .iter()
            .filter(|(m, p)| m == "GET" && p.ends_with("/roles"))
            .count();
        // each op consults the role set twice (gate + slot); only the fetch count matters
        assert_eq!(gets, expect, "ttl {ttl:?}");
    }
    // a role that LOSES nothing but gains ADMIN after the TTL is caught on re-fetch
    let r = rig_with(|b| good_role(b, "PUT", "vip"), Duration::ZERO).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    mock_role_put(&r.server, "PUT", RAW_A, "222").await;
    Mock::given(method("GET"))
        .and(path(format!("/api/v10/guilds/{GUILD}/roles")))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!([{"id":"222","permissions":"8"}])),
        )
        .mount(&r.server)
        .await;
    let e = r
        .exec
        .execute(&rq(&role(true, UUID_A, "vip"), 1))
        .await
        .unwrap_err();
    assert_eq!(code(&e), "role_privileged");
}

// ---- credential slot + misc ----------------------------------------------

#[tokio::test]
async fn the_bot_token_is_per_community_per_call_and_never_leaks() {
    let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    mock_member(&r.server, RAW_A, 200, None).await;
    mock_open_dm(&r.server, RAW_A, "555").await;
    mock_message(&r.server, "555").await;
    let before = r.creds.calls.load(Ordering::SeqCst);
    r.exec.execute(&rq(&dm(UUID_A), 1)).await.unwrap();
    let per_op = r.creds.calls.load(Ordering::SeqCst) - before;
    r.exec.execute(&rq(&dm(UUID_A), 1)).await.unwrap();
    assert_eq!(
        r.creds.calls.load(Ordering::SeqCst) - before,
        per_op * 2,
        "never cached"
    );
    // every request carried community 1's token, none carried community 2's
    for q in r.server.received_requests().await.unwrap() {
        assert_eq!(
            q.headers.get("authorization").unwrap(),
            &format!("Bot {TOKEN}")
        );
        assert!(!String::from_utf8_lossy(&q.body).contains(TOKEN));
    }
}

#[tokio::test]
async fn a_community_without_a_binding_or_scope_never_borrows_another() {
    let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    for c in [3i64, 0] {
        let a = dm(UUID_A);
        let res = r
            .exec
            .execute(&ExecRequest {
                platform: "discord",
                action: &a,
                community_id: Some(c),
            })
            .await
            .unwrap_err();
        assert!(matches!(code(&res), "no_connection" | "no_community_scope"));
    }
    let a = dm(UUID_A);
    let e = r
        .exec
        .execute(&ExecRequest {
            platform: "discord",
            action: &a,
            community_id: None,
        })
        .await
        .unwrap_err();
    assert_eq!(code(&e), "no_community_scope");
    assert_eq!(writes(&r.server).await, 0);
}

#[tokio::test]
async fn chat_send_proves_channel_ownership_through_the_slot_source() {
    let a = OutboundAction::ChatSend {
        channel: CHANNEL.into(),
        text: "hi".into(),
        origin_channel: Some(CHANNEL.into()),
    };
    let chat = |b: &str| RequestTpl {
        method: "POST".into(),
        url: format!("{b}/api/v10/channels/{{target.channel_id}}/messages"),
        headers: vec![("Content-Type".into(), "application/json".into())],
        body: Some(serde_json::to_vec(&json!({"content": "hi"})).unwrap()),
        secret_refs: vec![("Authorization".into(), "discord-bot".into())],
    };
    // owned: sent with the community's token
    let r = rig(chat).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    mock_message(&r.server, CHANNEL).await;
    r.exec.execute(&rq(&a, 1)).await.unwrap();
    // channel in another guild: refused, no message
    let r = rig(chat).await;
    mock_channel(&r.server, CHANNEL, Some("999")).await;
    mock_message(&r.server, CHANNEL).await;
    let e = r.exec.execute(&rq(&a, 1)).await.unwrap_err();
    assert_eq!(code(&e), "channel_not_owned");
    assert_eq!(writes(&r.server).await, 0);
    // channel lookup 401 = unusable connection; 500 = retryable
    for (status, expect, refusal) in [
        (401, "connection_not_usable", true),
        (500, "discord_lookup_failed", false),
    ] {
        let r = rig(chat).await;
        Mock::given(method("GET"))
            .and(path(format!("/api/v10/channels/{CHANNEL}")))
            .respond_with(ResponseTemplate::new(status))
            .mount(&r.server)
            .await;
        let e = r.exec.execute(&rq(&a, 1)).await.unwrap_err();
        assert_eq!(code(&e), expect);
        assert_eq!(e.is_policy_refusal(), refusal);
    }
}

#[tokio::test]
async fn the_slot_source_refuses_foreign_platforms_unknown_handles_and_missing_pins() {
    let src =
        DiscordSlots::with_endpoint(Creds::new(), guard(), "http://127.0.0.1:1/api/v10".into());
    let base = SlotRequest {
        guild_scoped: false,
        platform: "discord",
        community_id: Some(1),
        pinned_channel: Some(CHANNEL),
        handles: &["discord-bot"],
    };
    let mut r = SlotRequest {
        guild_scoped: false,
        platform: "twitch",
        ..clone_req(&base)
    };
    assert_eq!(
        src.resolve(&r).await.unwrap_err(),
        SlotError::Refused("unsupported_platform")
    );
    r = SlotRequest {
        guild_scoped: false,
        handles: &["token"],
        ..clone_req(&base)
    };
    assert_eq!(
        src.resolve(&r).await.unwrap_err(),
        SlotError::Refused("unknown_handle")
    );
    r = SlotRequest {
        guild_scoped: false,
        pinned_channel: None,
        ..clone_req(&base)
    };
    assert_eq!(
        src.resolve(&r).await.unwrap_err(),
        SlotError::Refused("no_origin_pin")
    );
    // unreachable Discord: typed + retryable, never a credential
    assert_eq!(
        src.resolve(&clone_req(&base)).await.unwrap_err(),
        SlotError::Unavailable("discord_lookup_failed")
    );
    // a non-snowflake channel never reaches the wire
    r = SlotRequest {
        guild_scoped: false,
        pinned_channel: Some("../x"),
        ..clone_req(&base)
    };
    assert_eq!(
        src.resolve(&r).await.unwrap_err(),
        SlotError::Refused("channel_not_owned")
    );
}

fn clone_req<'a>(r: &SlotRequest<'a>) -> SlotRequest<'a> {
    SlotRequest {
        guild_scoped: false,
        platform: r.platform,
        community_id: r.community_id,
        pinned_channel: r.pinned_channel,
        handles: r.handles,
    }
}

#[tokio::test]
async fn the_hook_claims_only_discord_duty_kinds_and_shapes_payloads() {
    let d = DiscordDuties::new(Creds::new(), Users::new());
    for k in ["dm.send", "role.add", "role.remove"] {
        assert!(d.claims("discord", k));
        assert!(!d.claims("twitch", k));
    }
    assert!(!d.claims("discord", "dm.post") && !d.claims("discord", "chat.send"));
    let client = guard();
    let a = dm(UUID_A);
    let cx = DutyCx {
        platform: "discord",
        action: &a,
        community_id: Some(1),
        pinned_channel: Some(CHANNEL),
        client: &client,
    };
    let p: serde_json::Value = serde_json::from_str(&d.payload(&cx).await.unwrap()).unwrap();
    assert_eq!(
        p,
        json!({"recipient_id": format!("{{user:{UUID_A}}}"), "content": "hello there"})
    );
    let a = role(false, UUID_A, "vip");
    let cx = DutyCx {
        platform: "discord",
        action: &a,
        community_id: Some(1),
        pinned_channel: Some(CHANNEL),
        client: &client,
    };
    let p: serde_json::Value = serde_json::from_str(&d.payload(&cx).await.unwrap()).unwrap();
    assert_eq!(
        p,
        json!({"user_id": format!("{{user:{UUID_A}}}"), "role": "vip"})
    );
    // a chat action is not a duty; an unknown duty slot is refused
    let a = OutboundAction::ChatSend {
        channel: "1".into(),
        text: "x".into(),
        origin_channel: None,
    };
    let cx = DutyCx {
        platform: "discord",
        action: &a,
        community_id: Some(1),
        pinned_channel: None,
        client: &client,
    };
    assert_eq!(d.gate(&cx).await.unwrap_err(), ExecError::BadAction);
    assert_eq!(d.payload(&cx).await.unwrap_err(), ExecError::BadAction);
    let mut t = dm_tpl("http://x", "y");
    assert_eq!(
        d.bind_template(&cx, &mut t).await.unwrap_err(),
        ExecError::BadAction
    );
    let a = dm(UUID_A);
    let cx = DutyCx {
        platform: "discord",
        action: &a,
        community_id: Some(1),
        pinned_channel: Some(CHANNEL),
        client: &client,
    };
    assert_eq!(
        d.dynamic_slot(&cx, "target.channel_id", &client)
            .await
            .unwrap_err(),
        ExecError::Slot(SlotError::Refused("unknown_duty_slot"))
    );
}

#[tokio::test]
async fn dm_open_failures_are_refused_and_nothing_is_sent() {
    for (status, body) in [
        (403, json!({"code": 50007})),
        (200, json!({"id": "not-a-snowflake"})),
        (200, json!({})),
    ] {
        let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
        mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
        mock_member(&r.server, RAW_A, 200, None).await;
        Mock::given(method("POST"))
            .and(path("/api/v10/users/@me/channels"))
            .respond_with(ResponseTemplate::new(status).set_body_json(body))
            .mount(&r.server)
            .await;
        mock_message(&r.server, "555").await;
        let e = r.exec.execute(&rq(&dm(UUID_A), 1)).await.unwrap_err();
        assert_eq!(code(&e), "dm_open_failed");
        let s = seen(&r.server).await;
        assert!(
            !s.iter().any(|(_, p)| p.ends_with("/messages")),
            "no message may follow a failed open"
        );
    }
}

#[tokio::test]
async fn policy_exposes_the_connector_contract_kinds() {
    let p = PlatformPolicy::discord();
    for k in ["dm.send", "role.add", "role.remove"] {
        assert!(p.kind(k).unwrap().needs_duty, "{k}");
    }
    assert_eq!(p.kind("role.add").unwrap().method, "PUT");
    assert_eq!(p.kind("role.remove").unwrap().method, "DELETE");
    assert!(p.kind("dm.post").is_none());
    // every duty slot is Duty-origin: the guest can never supply one
    for s in [
        "dm.channel_id",
        "target.guild_id",
        "target.user",
        "target.role",
    ] {
        let d = &p.slots[p.slot_index(s).unwrap()];
        assert_eq!(d.origin, SlotOrigin::Duty);
    }
    for k in &p.kinds {
        assert!(k.slots.iter().all(|n| p.slot_index(n).is_some()));
    }
}

#[test]
fn role_ops_parse_from_the_wire_and_never_reach_a_platform_sender() {
    let ok = parse_outbound_entry(
        &format!(r#"{{"op":"role.remove","user_id":"{UUID_A}","text":"vip","origin_channel":"{CHANNEL}"}}"#),
        "discord",
    )
    .unwrap();
    assert_eq!(ok.action.op(), "role.remove");
    for missing in [
        r#"{"op":"role.add","text":"vip","origin_channel":"1"}"#,
        r#"{"op":"role.add","user_id":"u","origin_channel":"1"}"#,
        r#"{"op":"role.add","user_id":"u","text":"vip"}"#,
    ] {
        assert!(
            parse_outbound_entry(missing, "discord").is_err(),
            "{missing}"
        );
    }
}

#[tokio::test]
async fn the_stamps_tenant_reaches_the_resolver_and_is_required() {
    let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    mock_member(&r.server, RAW_A, 200, None).await;
    mock_open_dm(&r.server, RAW_A, "555").await;
    mock_message(&r.server, "555").await;
    r.exec.execute(&rq(&dm(UUID_A), 1)).await.unwrap();
    assert!(r.users.tenants.lock().unwrap().iter().all(|t| *t == 9));

    // an entry whose stamp carries no tenant is refused before any lookup
    let r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
    mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
    let a = OutboundAction::DmSend {
        user_id: UUID_A.to_string(),
        text: "x".to_string(),
        origin_channel: CHANNEL.to_string(),
        tenant_id: None,
    };
    let e = r.exec.execute(&rq(&a, 1)).await.unwrap_err();
    assert_eq!(code(&e), "no_tenant_scope");
    assert_eq!(r.users.calls.load(Ordering::SeqCst), 0);
    assert_eq!(writes(&r.server).await, 0);
}

#[tokio::test]
async fn resolver_failures_map_to_distinct_typed_outcomes() {
    for (fail, expect, retryable) in [
        (UserResolveError::NotFound, "user_not_in_community", false),
        (UserResolveError::Denied, "tokenization_denied", false),
        (
            UserResolveError::RateLimited,
            "tokenization_rate_limited",
            true,
        ),
        (
            UserResolveError::Unavailable,
            "tokenization_unavailable",
            true,
        ),
    ] {
        let mut r = rig(|b| dm_tpl(b, &format!("{{user:{UUID_A}}}"))).await;
        mock_channel(&r.server, CHANNEL, Some(GUILD)).await;
        let users = Users {
            fail: Some(fail),
            ..Users::new()
        };
        let api = format!("{}/api/v10", r.server.uri());
        let mut policy = PlatformPolicy::discord();
        policy.hosts = vec![r.server.address().to_string()];
        policy.allow_http = true;
        r.exec = SenderExecutor::with_duties(
            r.tpl.clone(),
            DiscordSlots::with_endpoint(r.creds.clone(), guard(), api.clone()),
            Flag(true),
            DiscordDuties::with_endpoint(r.creds.clone(), users, api, Duration::from_secs(30)),
            ExecutorConfig::new(
                vec![policy],
                HashMap::from([("discord".to_string(), DIGEST.to_string())]),
            )
            .unwrap()
            .with_client(
                GuardedLimits::default(),
                vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
            ),
        );
        let e = r.exec.execute(&rq(&dm(UUID_A), 1)).await.unwrap_err();
        assert_eq!(code(&e), expect);
        assert_eq!(!e.is_policy_refusal(), retryable, "{expect}");
        assert_eq!(writes(&r.server).await, 0);
    }
}

// ---- guild scheduled events ---------------------------------------------

const EVENT_ID: &str = "900000000000000001";
const EVENT_JSON: &str = r#"{"name":"n","scheduled_start_time":"2026-12-01T19:00:00Z","scheduled_end_time":"2026-12-01T20:00:00Z","location":"l"}"#;

fn event(kind: EventKind, guild: &str, tenant: Option<i64>) -> OutboundAction {
    let (event_id, event_json) = match kind {
        EventKind::Create => (None, Some(EVENT_JSON.to_string())),
        EventKind::Update => (Some(EVENT_ID.to_string()), Some(EVENT_JSON.to_string())),
        EventKind::Cancel | EventKind::Delete => (Some(EVENT_ID.to_string()), None),
    };
    OutboundAction::EventOp {
        kind,
        event_id,
        event_json,
        expect_guild: (guild != "-").then(|| guild.to_string()),
        tenant_id: tenant,
    }
}

fn event_tpl(base: &str, method: &str, id: Option<&str>) -> RequestTpl {
    let tail = id.map_or_else(String::new, |i| format!("/{i}"));
    RequestTpl {
        method: method.into(),
        url: format!("{base}/api/v10/guilds/{{target.guild_id}}/scheduled-events{tail}"),
        headers: if method == "DELETE" {
            vec![]
        } else {
            vec![("Content-Type".into(), "application/json".into())]
        },
        body: (method != "DELETE").then(|| br#"{"name":"n"}"#.to_vec()),
        secret_refs: vec![("Authorization".into(), "discord-bot".into())],
    }
}

async fn mock_events(s: &MockServer, verb: &str, p: &str, status: u16, body: serde_json::Value) {
    Mock::given(method(verb))
        .and(path(p.to_string()))
        .respond_with(ResponseTemplate::new(status).set_body_json(body))
        .mount(s)
        .await;
}

#[tokio::test]
async fn event_create_pins_the_binding_guild_and_returns_the_new_event_id() {
    let r = rig(|b| event_tpl(b, "POST", None)).await;
    mock_events(
        &r.server,
        "POST",
        &format!("/api/v10/guilds/{GUILD}/scheduled-events"),
        200,
        json!({"id": EVENT_ID}),
    )
    .await;
    let out = r
        .exec
        .execute(&rq(&event(EventKind::Create, GUILD, Some(9)), 1))
        .await
        .unwrap();
    assert_eq!(out.result_id.as_deref(), Some(EVENT_ID));
    let reqs = r.server.received_requests().await.unwrap();
    assert_eq!(
        reqs.len(),
        1,
        "no channel/role/membership lookups for events"
    );
    assert_eq!(
        reqs[0].headers.get("authorization").unwrap(),
        &format!("Bot {TOKEN}")
    );
    // The connector payload carries the producer's fields and NO guild/channel.
    let calls = r.tpl.calls.lock().unwrap().clone();
    assert_eq!(calls[0].kind, "event.create");
    let p: serde_json::Value = serde_json::from_str(&calls[0].payload_json).unwrap();
    assert_eq!(p["name"], "n");
    assert!(p.get("guild_id").is_none() && p.get("channel_id").is_none());
    assert!(p.get("event_id").is_none());
}

#[tokio::test]
async fn event_update_cancel_delete_address_the_event_in_the_binding_guild() {
    for (kind, verb, status) in [
        (EventKind::Update, "PATCH", 200),
        (EventKind::Cancel, "PATCH", 200),
        (EventKind::Delete, "DELETE", 204),
    ] {
        let r = rig(|b| event_tpl(b, verb, Some(EVENT_ID))).await;
        let p = format!("/api/v10/guilds/{GUILD}/scheduled-events/{EVENT_ID}");
        mock_events(&r.server, verb, &p, status, json!({})).await;
        let out = r
            .exec
            .execute(&rq(&event(kind, GUILD, Some(9)), 1))
            .await
            .unwrap();
        assert_eq!(out.result_id, None, "{kind:?}");
        let calls = r.tpl.calls.lock().unwrap().clone();
        let payload: serde_json::Value = serde_json::from_str(&calls[0].payload_json).unwrap();
        assert_eq!(payload["event_id"], EVENT_ID);
        assert_eq!(seen(&r.server).await, vec![(verb.to_string(), p)]);
    }
}

#[tokio::test]
async fn an_event_guild_assertion_that_differs_from_the_binding_is_refused_before_the_connector() {
    let r = rig(|b| event_tpl(b, "POST", None)).await;
    mock_events(
        &r.server,
        "POST",
        &format!("/api/v10/guilds/{GUILD}/scheduled-events"),
        200,
        json!({"id": EVENT_ID}),
    )
    .await;
    let before = refusal_count("event.create", "guild_mismatch");
    // regression: guild pinning -- the producer's guild is an assertion only.
    let e = r
        .exec
        .execute(&rq(&event(EventKind::Create, "222000", Some(9)), 1))
        .await
        .unwrap_err();
    assert_eq!(e, ExecError::Slot(SlotError::Refused("guild_mismatch")));
    assert!(e.is_policy_refusal());
    assert_eq!(r.tpl.calls(), 0, "refused before the connector ran");
    assert!(seen(&r.server).await.is_empty());
    assert!(refusal_count("event.create", "guild_mismatch") > before);
    // Community 2's binding is another guild: its own assertion passes the gate
    // only for ITS guild, never community 1's.
    let e = r
        .exec
        .execute(&rq(&event(EventKind::Create, GUILD, Some(9)), 2))
        .await
        .unwrap_err();
    assert_eq!(code(&e), "guild_mismatch");
    assert!(seen(&r.server).await.is_empty());
}

#[tokio::test]
async fn event_ops_without_tenant_or_community_scope_are_refused() {
    let r = rig(|b| event_tpl(b, "POST", None)).await;
    let e = r
        .exec
        .execute(&rq(&event(EventKind::Create, GUILD, None), 1))
        .await
        .unwrap_err();
    assert_eq!(code(&e), "no_tenant_scope");
    let e = r
        .exec
        .execute(&rq(&event(EventKind::Create, GUILD, Some(9)), 99))
        .await
        .unwrap_err();
    assert!(matches!(e, ExecError::Slot(_)), "{e:?}");
    assert!(seen(&r.server).await.is_empty());
}

#[tokio::test]
async fn a_create_reply_without_a_snowflake_id_fails_loudly() {
    for body in [json!({}), json!({"id": "abc"}), json!({"id": 5})] {
        let r = rig(|b| event_tpl(b, "POST", None)).await;
        mock_events(
            &r.server,
            "POST",
            &format!("/api/v10/guilds/{GUILD}/scheduled-events"),
            200,
            body,
        )
        .await;
        let e = r
            .exec
            .execute(&rq(&event(EventKind::Create, GUILD, Some(9)), 1))
            .await
            .unwrap_err();
        assert_eq!(e, ExecError::Egress("bad_response"));
        assert!(!e.is_policy_refusal());
    }
}

#[tokio::test]
async fn a_template_naming_another_guild_or_path_for_an_event_is_refused_by_the_verifier() {
    for url_tail in [
        "/api/v10/guilds/999/scheduled-events",
        "/api/v10/guilds/{target.guild_id}/scheduled-events/../roles",
        "/api/v10/guilds/{target.guild_id}/roles",
        "/api/v10/channels/{target.channel_id}/messages",
    ] {
        let r = rig(|b| {
            let mut t = event_tpl(b, "POST", None);
            t.url = format!("{b}{url_tail}");
            t
        })
        .await;
        let res = r
            .exec
            .execute(&rq(&event(EventKind::Create, GUILD, Some(9)), 1))
            .await;
        assert!(res.is_err(), "{url_tail}");
        assert!(
            r.server.received_requests().await.unwrap().is_empty(),
            "{url_tail}"
        );
    }
}

#[test]
fn event_payload_shapes_per_kind_and_malformed_inputs() {
    use super::discord_duties::event_payload as ep;
    let v = |s: String| serde_json::from_str::<serde_json::Value>(&s).unwrap();
    assert_eq!(
        v(ep(EventKind::Create, None, Some(EVENT_JSON)).unwrap())["name"],
        "n"
    );
    let u = v(ep(EventKind::Update, Some("7"), Some(EVENT_JSON)).unwrap());
    assert_eq!(
        (u["name"].as_str(), u["event_id"].as_str()),
        (Some("n"), Some("7"))
    );
    assert_eq!(
        v(ep(EventKind::Cancel, Some("7"), None).unwrap()),
        json!({"event_id": "7"})
    );
    assert_eq!(
        v(ep(EventKind::Delete, Some("7"), None).unwrap()),
        json!({"event_id": "7"})
    );
    for bad in [
        ep(EventKind::Create, Some("7"), Some(EVENT_JSON)),
        ep(EventKind::Create, None, None),
        ep(EventKind::Create, None, Some("[]")),
        ep(EventKind::Create, None, Some("nope")),
        ep(EventKind::Update, None, Some(EVENT_JSON)),
        ep(EventKind::Cancel, None, None),
        ep(EventKind::Cancel, Some("7"), Some(EVENT_JSON)),
        ep(EventKind::Delete, Some("7"), Some("{}")),
    ] {
        assert_eq!(bad, Err(ExecError::BadAction));
    }
}

#[test]
fn event_wire_entries_parse_with_per_kind_field_rules() {
    let base = |op: &str, extra: &str| {
        format!(
            r#"{{"v":1,"op":"{op}","platform":"discord","origin_channel":"111","tenant_id":9{extra}}}"#
        )
    };
    let ok = |op: &str, extra: &str| parse_outbound_entry(&base(op, extra), "discord");
    let t = r#","text":"{\"name\":\"n\"}""#;
    let m = r#","message_id":"42""#;
    assert!(matches!(
        ok("event.create", t).unwrap().action,
        OutboundAction::EventOp {
            kind: EventKind::Create,
            event_id: None,
            ..
        }
    ));
    let u = ok("event.update", &format!("{t}{m}")).unwrap().action;
    assert!(matches!(
        u,
        OutboundAction::EventOp { kind: EventKind::Update, ref event_id, ref expect_guild, tenant_id: Some(9), .. }
            if event_id.as_deref() == Some("42") && expect_guild.as_deref() == Some("111")
    ));
    for op in ["event.cancel", "event.delete"] {
        assert_eq!(ok(op, m).unwrap().action.op(), op);
        assert!(
            ok(op, &format!("{m}{t}")).is_err(),
            "{op} must not carry text"
        );
        assert!(ok(op, "").is_err(), "{op} needs an id");
    }
    assert!(ok("event.create", "").is_err());
    assert!(ok("event.create", &format!("{t}{m}")).is_err());
    assert!(ok("event.update", t).is_err());
    assert!(ok("event.update", m).is_err());
    // `-` = no assertion; anything else that is not a snowflake is refused
    assert!(matches!(
        parse_outbound_entry(
            &base("event.delete", m).replace("\"111\"", "\"-\""),
            "discord"
        )
        .unwrap()
        .action,
        OutboundAction::EventOp {
            expect_guild: None,
            ..
        }
    ));
    assert!(parse_outbound_entry(
        &base("event.delete", m).replace("\"111\"", "\"12ab\""),
        "discord"
    )
    .is_err());
    let no_guild = r#"{"v":1,"op":"event.delete","platform":"discord","message_id":"42"}"#;
    assert!(parse_outbound_entry(no_guild, "discord").is_err());
    // never defaulted: an unknown event op is an unknown op
    assert!(ok("event.explode", m).is_err());
}

#[test]
fn only_the_event_kinds_declared_in_policy_are_executable() {
    let p = PlatformPolicy::discord();
    for (k, method) in [
        ("event.create", "POST"),
        ("event.update", "PATCH"),
        ("event.cancel", "PATCH"),
        ("event.delete", "DELETE"),
    ] {
        let kp = p.kind(k).unwrap();
        assert_eq!(kp.method, method);
        assert!(kp.needs_duty && kp.slots == vec!["target.guild_id"]);
    }
    assert!(p.kind("event.fetch").is_none());
}

#[tokio::test]
async fn guild_scoped_slot_requests_skip_the_channel_proof_and_nothing_else_does() {
    let r = rig(|b| event_tpl(b, "POST", None)).await;
    let slots = DiscordSlots::with_endpoint(
        r.creds.clone(),
        guard(),
        format!("{}/api/v10", r.server.uri()),
    );
    let req = |pin: Option<&'static str>, guild_scoped: bool, community: i64| SlotRequest {
        guild_scoped,
        platform: "discord",
        community_id: Some(community),
        pinned_channel: pin,
        handles: &["discord-bot"],
    };
    // guild-scoped, no channel: the community's own token, no channel lookup.
    let ok = slots.resolve(&req(None, true, 1)).await.unwrap();
    assert_eq!(ok["discord-bot"].expose(), TOKEN);
    assert!(seen(&r.server).await.is_empty());
    // every mismatch between pin and scope is refused loudly
    for (pin, scoped) in [(None, false), (Some(CHANNEL), true)] {
        assert_eq!(
            slots.resolve(&req(pin, scoped, 1)).await.unwrap_err(),
            SlotError::Refused("no_origin_pin")
        );
    }
    // never another community's credential
    assert!(slots.resolve(&req(None, true, 99)).await.is_err());
}

#[tokio::test]
async fn an_event_with_no_guild_assertion_pins_from_the_binding_alone() {
    let r = rig(|b| event_tpl(b, "POST", None)).await;
    mock_events(
        &r.server,
        "POST",
        &format!("/api/v10/guilds/{GUILD}/scheduled-events"),
        200,
        json!({"id": EVENT_ID}),
    )
    .await;
    let out = r
        .exec
        .execute(&rq(&event(EventKind::Create, "-", Some(9)), 1))
        .await
        .unwrap();
    assert_eq!(out.result_id.as_deref(), Some(EVENT_ID));
}

#[test]
fn a_discord_404_on_an_event_op_acks_a_distinct_not_found_code() {
    use crate::outbound_ops::exec_to_sender_error;
    let nf = exec_to_sender_error("discord", "event.update", &ExecError::Upstream(404));
    assert_eq!(nf.ack_code(), "upstream_not_found");
    // other ops / statuses keep the generic failed code
    for (op, st) in [
        ("chat.delete", 404),
        ("event.update", 500),
        ("event.create", 403),
    ] {
        let e = exec_to_sender_error("discord", op, &ExecError::Upstream(st));
        assert_eq!(e.ack_code(), "failed", "{op} {st}");
    }
}
