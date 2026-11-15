#![allow(clippy::unwrap_used, clippy::expect_used)]
//! Real-path tests for the YouTube host duties: a local fake YouTube (wiremock) behind the
//! guard's exact-ip test allowance with the production verifier / substituter /
//! `GuardedClient` / `YoutubeSlots` / `YoutubeDuties` running unmodified. Only the component
//! loader, the hub-api credential seam and the tokenization seam are doubles.

use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_host_http::guarded::GuardedLimits;
use connection_resolver::SecretString;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use super::super::tests::Flag;
use super::super::youtube_slots::YoutubeSlots;
use super::*;
use crate::connector::sender_exec::{
    BuildError, BuiltTemplate, ExecRequest, ExecutorConfig, RequestTpl, SenderAction,
    SenderExecutor, TemplateBuilder,
};
use crate::outbound_creds::CredError;
use crate::outbound_creds_youtube::YoutubeBinding;

const DIGEST: &str = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
const CHAT: &str = "Cg0KC-chat_ONE";
const OTHER_CHAT: &str = "Cg0KC-chat_TWO";
const USER: &str = "123e4567-e89b-12d3-a456-426614174000";

#[derive(Clone)]
struct Creds;

impl YoutubeCredentialSource for Creds {
    async fn binding(&self, c: Option<i64>, chat: &str) -> Result<YoutubeBinding, CredError> {
        if c != Some(1) {
            return Err(CredError::NoBinding);
        }
        if chat != CHAT {
            return Err(CredError::ChannelNotOwned);
        }
        Ok(YoutubeBinding {
            oauth_token: SecretString::new("YT-TOKEN".to_string()),
            live_chat_id: chat.to_string(),
        })
    }
}

struct Users;

impl PlatformUserResolver for Users {
    async fn resolve(&self, t: i64, c: i64, u: &str) -> Result<SlotValue, UserResolveError> {
        if (t, c, u) == (7, 1, USER) {
            Ok(SlotValue::new("UCtargetChannel_1".to_string()))
        } else {
            Err(UserResolveError::NotFound)
        }
    }
}

#[derive(Clone)]
struct Tpl(Arc<Mutex<RequestTpl>>);

impl TemplateBuilder for Tpl {
    async fn build_request(
        &self,
        _platform: &str,
        _action: &SenderAction,
    ) -> Result<BuiltTemplate, BuildError> {
        Ok(BuiltTemplate {
            tpl: self.0.lock().unwrap().clone(),
            component_digest: DIGEST.to_string(),
        })
    }
}

fn refs(r: &[(&str, &str)]) -> Vec<(String, String)> {
    r.iter()
        .map(|(a, b)| ((*a).to_string(), (*b).to_string()))
        .collect()
}

/// The template the connector emits for a delete-style kind.
fn delete_tpl(resource: &str, id: &str) -> RequestTpl {
    RequestTpl {
        method: "DELETE".into(),
        url: format!("https://www.googleapis.com/youtube/v3/liveChat/{resource}?id={id}"),
        headers: vec![("Authorization".into(), "Bearer {{YT_OAUTH_TOKEN}}".into())],
        body: None,
        secret_refs: refs(&[("{{YT_OAUTH_TOKEN}}", "youtube.oauth")]),
    }
}

/// The template the connector emits for `mod.ban` (permanent).
fn ban_tpl() -> RequestTpl {
    RequestTpl {
        method: "POST".into(),
        url: "https://www.googleapis.com/youtube/v3/liveChat/bans?part=snippet".into(),
        headers: vec![
            ("Authorization".into(), "Bearer {{YT_OAUTH_TOKEN}}".into()),
            ("Content-Type".into(), "application/json".into()),
        ],
        body: Some(
            serde_json::json!({"snippet": {
                "liveChatId": "{{YT_LIVE_CHAT_ID}}",
                "type": "permanent",
                "bannedUserDetails": {"channelId": "{{YT_TARGET_USER}}"},
            }})
            .to_string()
            .into_bytes(),
        ),
        secret_refs: refs(&[
            ("{{YT_OAUTH_TOKEN}}", "youtube.oauth"),
            ("{{YT_LIVE_CHAT_ID}}", "youtube.live_chat_id"),
        ]),
    }
}

struct Rig {
    server: MockServer,
    tpl: Arc<Mutex<RequestTpl>>,
    exec: SenderExecutor<Tpl, YoutubeSlots<Creds>, Flag, YoutubeDuties<Creds, Users>>,
}

async fn rig(tpl: RequestTpl, budget: u32) -> Rig {
    let server = MockServer::start().await;
    let mut policy = PlatformPolicy::youtube();
    policy.hosts = vec![server.address().to_string()];
    policy.allow_http = true;
    let mut digests = HashMap::new();
    digests.insert("youtube".to_string(), DIGEST.to_string());
    let cfg = ExecutorConfig::new(vec![policy], digests)
        .unwrap()
        .with_client(
            GuardedLimits {
                max_redirects: 0,
                timeout: Duration::from_secs(5),
                max_response_bytes: 65_536,
            },
            vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        );
    let duties = YoutubeDuties::with_endpoint(Creds, Users, server.uri(), QuotaLedger::new(budget));
    let tpl = Arc::new(Mutex::new(rewrite(tpl, &server)));
    let exec = SenderExecutor::with_duties(
        Tpl(Arc::clone(&tpl)),
        YoutubeSlots::new(Creds),
        Flag(true),
        duties,
        cfg,
    );
    Rig { server, tpl, exec }
}

fn rewrite(mut t: RequestTpl, server: &MockServer) -> RequestTpl {
    t.url = t.url.replace("https://www.googleapis.com", &server.uri());
    t
}

fn delete(op: &'static str, id: &str) -> OutboundAction {
    if op == "chat.delete" {
        OutboundAction::ChatDelete {
            channel: CHAT.into(),
            message_id: id.into(),
            origin_channel: Some(CHAT.into()),
        }
    } else {
        policy_op(op, Some(id), None)
    }
}

fn policy_op(op: &'static str, id: Option<&str>, user: Option<&str>) -> OutboundAction {
    OutboundAction::PolicyOp(PolicyOp {
        op,
        channel: Some(CHAT.into()),
        text: None,
        message_id: id.map(str::to_string),
        user_id: user.map(str::to_string),
        origin_channel: Some(CHAT.into()),
        tenant_id: Some(7),
    })
}

async fn run(
    r: &Rig,
    a: &OutboundAction,
) -> Result<crate::connector::sender_exec::ExecOutcome, ExecError> {
    r.exec
        .execute(&ExecRequest {
            platform: "youtube",
            action: a,
            community_id: Some(1),
        })
        .await
}

async fn mount_messages(server: &MockServer, ids: &[&str]) {
    let items: Vec<_> = ids.iter().map(|i| serde_json::json!({"id": i})).collect();
    Mock::given(method("GET"))
        .and(path("/youtube/v3/liveChat/messages"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"items": items})))
        .mount(server)
        .await;
    Mock::given(method("DELETE"))
        .respond_with(ResponseTemplate::new(204))
        .mount(server)
        .await;
}

async fn methods(server: &MockServer) -> Vec<String> {
    server
        .received_requests()
        .await
        .unwrap()
        .iter()
        .map(|r| r.method.to_string())
        .collect()
}

#[tokio::test]
async fn a_foreign_message_id_is_refused_before_any_delete_reaches_youtube() {
    let r = rig(delete_tpl("messages", "foreignMsg1"), 10_000).await;
    mount_messages(&r.server, &["ownMsg1", "ownMsg2"]).await;
    assert_eq!(
        run(&r, &delete("chat.delete", "foreignMsg1"))
            .await
            .unwrap_err(),
        ExecError::Slot(SlotError::Refused("resource_not_in_chat"))
    );
    assert_eq!(
        methods(&r.server).await,
        vec!["GET"],
        "only the host lookup ran"
    );
}

#[tokio::test]
async fn an_own_message_id_is_proven_by_lookup_then_cached() {
    let r = rig(delete_tpl("messages", "ownMsg1"), 10_000).await;
    mount_messages(&r.server, &["ownMsg1"]).await;
    // Cache miss -> list lookup -> delete.
    assert_eq!(
        run(&r, &delete("chat.delete", "ownMsg1"))
            .await
            .unwrap()
            .status,
        204
    );
    assert_eq!(methods(&r.server).await, vec!["GET", "DELETE"]);
    // Cache hit -> no second lookup.
    run(&r, &delete("chat.delete", "ownMsg1")).await.unwrap();
    assert_eq!(methods(&r.server).await, vec!["GET", "DELETE", "DELETE"]);
}

#[tokio::test]
async fn the_lookup_is_scoped_to_the_pinned_chat_never_a_bundle_named_one() {
    let r = rig(delete_tpl("messages", "ownMsg1"), 10_000).await;
    mount_messages(&r.server, &["ownMsg1"]).await;
    // A producer-named channel that differs from the stamp dies at the pin gate.
    let a = OutboundAction::ChatDelete {
        channel: OTHER_CHAT.into(),
        message_id: "ownMsg1".into(),
        origin_channel: Some(CHAT.into()),
    };
    assert!(run(&r, &a).await.is_err());
    assert!(r.server.received_requests().await.unwrap().is_empty());
    run(&r, &delete("chat.delete", "ownMsg1")).await.unwrap();
    let got = r.server.received_requests().await.unwrap();
    assert_eq!(
        got[0]
            .url
            .query_pairs()
            .find(|(k, _)| k == "liveChatId")
            .unwrap()
            .1,
        CHAT
    );
}

#[tokio::test]
async fn a_moderator_id_is_proven_by_the_pinned_chats_moderator_list() {
    let r = rig(delete_tpl("moderators", "modOwn1"), 10_000).await;
    Mock::given(method("GET"))
        .and(path("/youtube/v3/liveChat/moderators"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(serde_json::json!({"items": [{"id": "modOwn1"}]})),
        )
        .mount(&r.server)
        .await;
    Mock::given(method("DELETE"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&r.server)
        .await;
    assert!(run(&r, &delete("mod.remove", "modOwn1")).await.is_ok());
    *r.tpl.lock().unwrap() = rewrite(delete_tpl("moderators", "modForeign"), &r.server);
    assert_eq!(
        run(&r, &delete("mod.remove", "modForeign"))
            .await
            .unwrap_err(),
        ExecError::Slot(SlotError::Refused("resource_not_in_chat"))
    );
}

#[tokio::test]
async fn an_unknown_ban_id_is_refused_but_a_host_created_one_is_accepted() {
    let r = rig(ban_tpl(), 10_000).await;
    Mock::given(method("POST"))
        .and(path("/youtube/v3/liveChat/bans"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(serde_json::json!({"id": "banNew1"})),
        )
        .mount(&r.server)
        .await;
    Mock::given(method("DELETE"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&r.server)
        .await;
    *r.tpl.lock().unwrap() = rewrite(delete_tpl("bans", "banForeign"), &r.server);
    assert_eq!(
        run(&r, &delete("mod.unban", "banForeign"))
            .await
            .unwrap_err(),
        ExecError::Slot(SlotError::Refused("ban_not_host_created"))
    );
    // The ban itself: target user is detokenized host-side into the body.
    *r.tpl.lock().unwrap() = rewrite(ban_tpl(), &r.server);
    let out = run(
        &r,
        &policy_op("mod.ban", None, Some(&format!("{{user:{USER}}}"))),
    )
    .await
    .unwrap();
    assert!(out.result_id.unwrap().contains("banNew1"));
    let got = r.server.received_requests().await.unwrap();
    let body: serde_json::Value = serde_json::from_slice(&got[0].body).unwrap();
    assert_eq!(body["snippet"]["liveChatId"], CHAT);
    assert_eq!(
        body["snippet"]["bannedUserDetails"]["channelId"],
        "UCtargetChannel_1"
    );
    // The id the host created is now provable.
    *r.tpl.lock().unwrap() = rewrite(delete_tpl("bans", "banNew1"), &r.server);
    assert!(run(&r, &delete("mod.unban", "banNew1")).await.is_ok());
}

#[tokio::test]
async fn a_raw_channel_id_or_foreign_uuid_is_never_a_ban_target() {
    let r = rig(ban_tpl(), 10_000).await;
    for user in ["UCrawChannelId", "00000000-0000-0000-0000-000000000000"] {
        let err = run(&r, &policy_op("mod.ban", None, Some(user)))
            .await
            .unwrap_err();
        assert!(
            matches!(err, ExecError::Slot(SlotError::Refused(_))),
            "{err:?}"
        );
    }
    assert!(r.server.received_requests().await.unwrap().is_empty());
}

#[tokio::test]
async fn a_literal_live_chat_id_in_an_insert_body_is_refused_by_the_verifier() {
    let mut t = ban_tpl();
    t.body = Some(
        serde_json::json!({"snippet": {
            "liveChatId": OTHER_CHAT,
            "type": "permanent",
            "bannedUserDetails": {"channelId": "{{YT_TARGET_USER}}"},
        }})
        .to_string()
        .into_bytes(),
    );
    let r = rig(t, 10_000).await;
    let err = run(&r, &policy_op("mod.ban", None, Some(USER)))
        .await
        .unwrap_err();
    assert!(matches!(err, ExecError::Template(_)), "{err:?}");
    assert!(r.server.received_requests().await.unwrap().is_empty());
}

#[tokio::test]
async fn an_exhausted_community_budget_is_the_explicit_quota_refusal() {
    // chat.delete (20) + lookup (5) fits 25; the second distinct delete needs 25 more.
    let r = rig(delete_tpl("messages", "ownMsg1"), 45).await;
    mount_messages(&r.server, &["ownMsg1", "ownMsg2"]).await;
    run(&r, &delete("chat.delete", "ownMsg1")).await.unwrap();
    // Cached id: only the op cost (20) -> 45 used.
    run(&r, &delete("chat.delete", "ownMsg1")).await.unwrap();
    let before = r.server.received_requests().await.unwrap().len();
    assert_eq!(
        run(&r, &delete("chat.delete", "ownMsg1"))
            .await
            .unwrap_err(),
        ExecError::Slot(SlotError::Refused("quota_exhausted"))
    );
    assert_eq!(r.server.received_requests().await.unwrap().len(), before);
    assert!(
        refusal_count("chat.delete", "slot_refused")
            + refusal_count("chat.delete", "quota_exhausted")
            >= 1
    );
}

#[test]
fn the_ledger_is_per_community_and_resets_each_utc_day() {
    use std::sync::atomic::{AtomicU64, Ordering};
    static NOW: AtomicU64 = AtomicU64::new(0);
    let l = QuotaLedger::with_clock(100, || NOW.load(Ordering::SeqCst));
    assert!(l.charge(1, 60));
    assert!(!l.charge(1, 60), "over budget charges nothing");
    assert_eq!(l.used(1), 60);
    assert!(l.charge(2, 100), "another community has its own budget");
    NOW.store(86_400, Ordering::SeqCst);
    assert_eq!(l.used(1), 0);
    assert!(l.charge(1, 100));
}

#[test]
fn costs_follow_the_documented_unit_table() {
    assert_eq!(unit_cost("chat.send"), 20);
    assert_eq!(unit_cost("chat.delete"), 20);
    for k in ["mod.ban", "mod.unban", "mod.add", "mod.remove"] {
        assert_eq!(unit_cost(k), 50);
    }
}

#[test]
fn the_owned_cache_flushes_instead_of_growing_without_bound() {
    let mut c = OwnedCache::default();
    for i in 0..(OWNED_MAX + 5) {
        c.insert(CHAT, Class::Message, &format!("m{i}"));
        assert!(c.map.len() <= OWNED_MAX);
    }
    assert!(c.contains(CHAT, Class::Message, &format!("m{}", OWNED_MAX + 4)));
    assert!(!c.contains(OTHER_CHAT, Class::Message, &format!("m{}", OWNED_MAX + 4)));
}
