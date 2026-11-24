#![allow(clippy::unwrap_used, clippy::expect_used)]
//! Real-path tests for the YouTube slot source: a local fake YouTube
//! (wiremock) behind the guard's exact-ip test allowance, the production
//! verifier / substituter / `GuardedClient` / `YoutubeSlots` running
//! unmodified. Only the component loader ([`TemplateBuilder`]) and the
//! hub-api credential seam are doubles.

use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_host_http::guarded::GuardedLimits;
use connection_resolver::SecretString;
use wiremock::matchers::any;
use wiremock::{Mock, MockServer, ResponseTemplate};

use super::tests::Flag;
use super::verify::tests::youtube_send;
use super::youtube_slots::YoutubeSlots;
use super::*;
use crate::outbound_creds::CredError;
use crate::outbound_creds_youtube::{YoutubeBinding, YoutubeCredentialSource};

const DIGEST: &str = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
const TOKEN1: &str = "YT-TOKEN-ONE";
const TOKEN2: &str = "YT-TOKEN-TWO";
const CHAT1: &str = "Cg0KC-chat_ONE";
const CHAT2: &str = "Cg0KC-chat_TWO";

/// `community -> (token, chat)`; `chat` doubles as the ownership truth.
#[derive(Clone)]
struct Creds {
    map: Arc<HashMap<i64, (String, String)>>,
    calls: Arc<AtomicUsize>,
    err: Option<CredError>,
    /// Hostile token to hand out regardless of the map (defense in depth).
    token_override: Option<String>,
}

impl Creds {
    fn new() -> Self {
        let mut m = HashMap::new();
        m.insert(1, (TOKEN1.to_string(), CHAT1.to_string()));
        m.insert(2, (TOKEN2.to_string(), CHAT2.to_string()));
        Self {
            map: Arc::new(m),
            calls: Arc::new(AtomicUsize::new(0)),
            err: None,
            token_override: None,
        }
    }
}

impl YoutubeCredentialSource for Creds {
    async fn binding(
        &self,
        community_id: Option<i64>,
        live_chat_id: &str,
    ) -> Result<YoutubeBinding, CredError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if let Some(e) = self.err {
            return Err(e);
        }
        let c = community_id
            .filter(|c| *c > 0)
            .ok_or(CredError::NoCommunityScope)?;
        let (t, chat) = self.map.get(&c).ok_or(CredError::NoBinding)?;
        if chat != live_chat_id {
            return Err(CredError::ChannelNotOwned);
        }
        Ok(YoutubeBinding {
            oauth_token: SecretString::new(
                self.token_override.clone().unwrap_or_else(|| t.clone()),
            ),
            live_chat_id: chat.clone(),
        })
    }
}

#[derive(Clone)]
struct Tpl {
    tpl: Arc<Mutex<RequestTpl>>,
}

impl TemplateBuilder for Tpl {
    async fn build_request(
        &self,
        _platform: &str,
        _action: &SenderAction,
    ) -> Result<BuiltTemplate, BuildError> {
        Ok(BuiltTemplate {
            tpl: self.tpl.lock().unwrap().clone(),
            component_digest: DIGEST.to_string(),
        })
    }
}

struct Rig {
    server: MockServer,
    exec: SenderExecutor<
        Tpl,
        YoutubeSlots<Creds>,
        Flag,
        super::youtube_duties::YoutubeDuties<Creds, super::discord_duties::NoUserResolver>,
    >,
    creds: Creds,
}

async fn rig_with(creds: Creds, edit: impl FnOnce(&mut RequestTpl)) -> Rig {
    let server = MockServer::start().await;
    Mock::given(any())
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;
    let mut t = youtube_send();
    t.url = t.url.replace("https://www.googleapis.com", &server.uri());
    edit(&mut t);
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
                max_response_bytes: 4096,
            },
            vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        );
    let duties = super::youtube_duties::YoutubeDuties::with_endpoint(
        creds.clone(),
        super::discord_duties::NoUserResolver,
        server.uri(),
        super::youtube_duties::QuotaLedger::new(1_000_000),
    );
    let exec = SenderExecutor::with_duties(
        Tpl {
            tpl: Arc::new(Mutex::new(t)),
        },
        YoutubeSlots::new(creds.clone()),
        Flag(true),
        duties,
        cfg,
    );
    Rig {
        server,
        exec,
        creds,
    }
}

async fn rig() -> Rig {
    rig_with(Creds::new(), |_| {}).await
}

fn send(channel: &str, origin: Option<&str>) -> OutboundAction {
    OutboundAction::ChatSend {
        channel: channel.to_string(),
        text: "hello".to_string(),
        origin_channel: origin.map(str::to_string),
        tenant_id: None,
    }
}

async fn run(
    r: &Rig,
    a: &OutboundAction,
    community: Option<i64>,
) -> Result<ExecOutcome, ExecError> {
    r.exec
        .execute(&ExecRequest {
            platform: "youtube",
            action: a,
            community_id: community,
        })
        .await
}

async fn nothing_sent(r: &Rig) {
    assert!(
        r.server.received_requests().await.unwrap().is_empty(),
        "a refused send must not reach the network"
    );
}

#[tokio::test]
async fn chat_send_uses_the_communitys_token_and_pinned_chat_through_the_guard() {
    let r = rig().await;
    let out = run(&r, &send(CHAT1, Some(CHAT1)), Some(1)).await.unwrap();
    assert_eq!(out.status, 200);
    let got = r.server.received_requests().await.unwrap();
    assert_eq!(got.len(), 1);
    let g = &got[0];
    assert_eq!(g.method.as_str(), "POST");
    assert_eq!(g.url.path(), "/youtube/v3/liveChat/messages");
    assert_eq!(g.url.query(), Some("part=snippet"));
    assert_eq!(
        g.headers.get("authorization").unwrap().to_str().unwrap(),
        format!("Bearer {TOKEN1}")
    );
    let body: serde_json::Value = serde_json::from_slice(&g.body).unwrap();
    assert_eq!(body["snippet"]["liveChatId"], CHAT1);
    // Per call, uncached.
    run(&r, &send(CHAT1, Some(CHAT1)), Some(1)).await.unwrap();
    assert_eq!(r.creds.calls.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn a_live_chat_from_the_payload_is_never_trusted() {
    // Community 1 asked to post into community 2's chat: the stamp is
    // self-consistent but the community's connection does not own it.
    let r = rig().await;
    assert_eq!(
        run(&r, &send(CHAT2, Some(CHAT2)), Some(1))
            .await
            .unwrap_err(),
        ExecError::Slot(SlotError::Refused("channel_not_owned"))
    );
    nothing_sent(&r).await;
    // Payload channel differs from the stamp: refused at the pin gate, before
    // any credential is resolved.
    assert_eq!(
        run(&r, &send(CHAT2, Some(CHAT1)), Some(1))
            .await
            .unwrap_err(),
        ExecError::Pin(PinError::ChannelMismatch)
    );
    assert_eq!(r.creds.calls.load(Ordering::SeqCst), 1);
    nothing_sent(&r).await;
}

#[tokio::test]
async fn an_unstamped_or_hostile_chat_id_is_refused_before_resolution() {
    let r = rig().await;
    assert_eq!(
        run(&r, &send(CHAT1, None), Some(1)).await.unwrap_err(),
        ExecError::Pin(PinError::MissingOrigin)
    );
    for hostile in [
        "x&liveChatId=evil",
        "a b",
        "a=b",
        "a/../b",
        "a%26b",
        "a\"}",
        "",
        &"a".repeat(129),
    ] {
        assert_eq!(
            run(&r, &send(hostile, Some(hostile)), Some(1))
                .await
                .unwrap_err(),
            ExecError::Pin(if hostile.is_empty() {
                PinError::MissingOrigin
            } else {
                PinError::InvalidChannel
            }),
            "{hostile:?}"
        );
    }
    assert_eq!(r.creds.calls.load(Ordering::SeqCst), 0);
    nothing_sent(&r).await;
}

#[tokio::test]
async fn the_wrong_community_never_gets_another_communitys_credential() {
    let r = rig().await;
    // Community 2 against community 1's chat.
    assert_eq!(
        run(&r, &send(CHAT1, Some(CHAT1)), Some(2))
            .await
            .unwrap_err(),
        ExecError::Slot(SlotError::Refused("channel_not_owned"))
    );
    nothing_sent(&r).await;
    // The right pairing sends community 2's own token, never community 1's.
    run(&r, &send(CHAT2, Some(CHAT2)), Some(2)).await.unwrap();
    let got = r.server.received_requests().await.unwrap();
    assert_eq!(
        got[0]
            .headers
            .get("authorization")
            .unwrap()
            .to_str()
            .unwrap(),
        format!("Bearer {TOKEN2}")
    );
}

#[tokio::test]
async fn missing_credential_is_typed_and_nothing_is_sent() {
    let r = rig().await;
    assert_eq!(
        run(&r, &send(CHAT1, Some(CHAT1)), Some(9))
            .await
            .unwrap_err(),
        ExecError::Slot(SlotError::Refused("no_connection"))
    );
    assert_eq!(
        run(&r, &send(CHAT1, Some(CHAT1)), None).await.unwrap_err(),
        ExecError::Slot(SlotError::Refused("no_community_scope"))
    );
    nothing_sent(&r).await;
}

#[tokio::test]
async fn an_expired_token_is_a_retryable_typed_refusal_and_nothing_is_sent() {
    let mut c = Creds::new();
    c.err = Some(CredError::TokenExpired);
    let r = rig_with(c, |_| {}).await;
    assert_eq!(
        run(&r, &send(CHAT1, Some(CHAT1)), Some(1))
            .await
            .unwrap_err(),
        ExecError::Slot(SlotError::Unavailable("token_expired"))
    );
    nothing_sent(&r).await;
}

#[tokio::test]
async fn hostile_values_from_a_misbehaving_source_never_reach_the_wire() {
    for tok in ["tok\r\nX-Evil: 1", "tok en", "tok\u{e9}", ""] {
        let mut c = Creds::new();
        c.token_override = Some(tok.to_string());
        let r = rig_with(c, |_| {}).await;
        let err = run(&r, &send(CHAT1, Some(CHAT1)), Some(1))
            .await
            .unwrap_err();
        assert!(matches!(err, ExecError::Subst(_)), "{tok:?}: {err:?}");
        nothing_sent(&r).await;
    }
}

#[tokio::test]
async fn templates_cannot_set_authorization_or_read_path_slots() {
    let literal_auth = |t: &mut RequestTpl| {
        for h in &mut t.headers {
            if h.0 == "Authorization" {
                h.1 = "Bearer stolen-token".to_string();
            }
        }
        t.secret_refs.retain(|(s, _)| s != "{{YT_OAUTH_TOKEN}}");
    };
    let extra_header = |t: &mut RequestTpl| t.headers.push(("X-Goog-Api-Key".into(), "k".into()));
    let video_slot = |t: &mut RequestTpl| {
        t.url.push_str("&id={{YT_VIDEO_ID}}");
        t.secret_refs
            .push(("{{YT_VIDEO_ID}}".into(), "youtube.video_id".into()));
    };
    let channel_slot = |t: &mut RequestTpl| {
        t.headers
            .push(("Accept".into(), "{{YT_CHANNEL_ID}}".into()));
        t.secret_refs
            .push(("{{YT_CHANNEL_ID}}".into(), "youtube.channel_id".into()));
    };
    let foreign_host = |t: &mut RequestTpl| {
        t.url = t.url.replace("127.0.0.1", "localhost");
    };
    type Edit = Box<dyn Fn(&mut RequestTpl)>;
    let cases: Vec<Edit> = vec![
        Box::new(literal_auth),
        Box::new(extra_header),
        Box::new(video_slot),
        Box::new(channel_slot),
        Box::new(foreign_host),
    ];
    for (i, edit) in cases.into_iter().enumerate() {
        let r = rig_with(Creds::new(), |t| edit(t)).await;
        let err = run(&r, &send(CHAT1, Some(CHAT1)), Some(1))
            .await
            .unwrap_err();
        assert!(matches!(err, ExecError::Template(_)), "case {i}: {err:?}");
        // The credential is never even resolved for a refused template.
        assert_eq!(r.creds.calls.load(Ordering::SeqCst), 0, "case {i}");
        nothing_sent(&r).await;
    }
}

#[tokio::test]
async fn upstream_errors_do_not_leak_the_token() {
    let r = rig().await;
    r.server.reset().await;
    Mock::given(any())
        .respond_with(ResponseTemplate::new(403).set_body_string(TOKEN1))
        .mount(&r.server)
        .await;
    let err = run(&r, &send(CHAT1, Some(CHAT1)), Some(1))
        .await
        .unwrap_err();
    assert_eq!(err, ExecError::Upstream(403));
    assert!(!format!("{err} {err:?}").contains(TOKEN1));
}
