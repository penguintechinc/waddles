#![allow(clippy::unwrap_used, clippy::expect_used)]
//! Real-path executor tests: a local HTTP server stands in for Twitch /
//! Discord / YouTube behind an egress allowlist (the policy's host list +
//! the guarded client's exact-ip allowance), the production verifier,
//! substituter and `GuardedClient` run unmodified, and the templates are the
//! overlay-accurate fixtures from `verify::tests`. Only the component loader
//! ([`TemplateBuilder`]) is a double -- see the module doc for why.

use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr};
use std::sync::{Arc, Mutex};

use wiremock::matchers::any;
use wiremock::{Mock, MockServer, ResponseTemplate};

use super::policy::{KindPolicy, PathSeg, Position, SlotDecl, SlotShape, SlotSyntax};
use super::verify::tests::{
    discord_delete, discord_send, twitch_delete, twitch_send, youtube_send,
};
use super::*;

const DIGEST: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const TOKEN: &str = "SECRET-TOKEN-XYZ";

#[derive(Clone)]
pub(crate) struct FixedBuilder {
    tpl: Arc<Mutex<RequestTpl>>,
    digest: String,
    pub(crate) calls: Arc<Mutex<Vec<SenderAction>>>,
    fail: Option<BuildError>,
}

impl FixedBuilder {
    fn new(tpl: RequestTpl) -> Self {
        Self {
            tpl: Arc::new(Mutex::new(tpl)),
            digest: DIGEST.to_string(),
            calls: Arc::new(Mutex::new(Vec::new())),
            fail: None,
        }
    }
    fn calls(&self) -> usize {
        self.calls.lock().unwrap().len()
    }
}

impl TemplateBuilder for FixedBuilder {
    async fn build_request(
        &self,
        _platform: &str,
        action: &SenderAction,
    ) -> Result<BuiltTemplate, BuildError> {
        self.calls.lock().unwrap().push(action.clone());
        if let Some(e) = self.fail {
            return Err(e);
        }
        Ok(BuiltTemplate {
            tpl: self.tpl.lock().unwrap().clone(),
            component_digest: self.digest.clone(),
        })
    }
}

/// What the resolver was asked: community, pinned channel, handles.
pub(crate) type SeenReq = (Option<i64>, Option<String>, Vec<&'static str>);

#[derive(Clone, Default)]
pub(crate) struct MapSlots {
    pub(crate) map: HashMap<String, String>,
    pub(crate) err: Option<SlotError>,
    pub(crate) seen: Arc<Mutex<Vec<SeenReq>>>,
}

impl MapSlots {
    pub(crate) fn with(pairs: &[(&str, &str)]) -> Self {
        Self {
            map: pairs
                .iter()
                .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
                .collect(),
            ..Self::default()
        }
    }
    pub(crate) fn twitch() -> Self {
        Self::with(&[
            ("token", TOKEN),
            ("client_id", "cid123"),
            ("broadcaster_id", "111"),
            ("sender_id", "222"),
        ])
    }
}

impl SlotSource for MapSlots {
    async fn resolve(
        &self,
        req: &SlotRequest<'_>,
    ) -> Result<HashMap<String, SlotValue>, SlotError> {
        self.seen.lock().unwrap().push((
            req.community_id,
            req.pinned_channel.map(str::to_string),
            req.handles.to_vec(),
        ));
        if let Some(e) = self.err {
            return Err(e);
        }
        Ok(self
            .map
            .iter()
            .map(|(k, v)| (k.clone(), SlotValue::new(v.clone())))
            .collect())
    }
}

#[derive(Clone, Copy)]
pub(crate) struct Flag(pub(crate) bool);

impl FlagGate for Flag {
    async fn enabled(&self, flag: &str) -> bool {
        assert_eq!(flag, CONNECTOR_TOOLS_FLAG);
        self.0
    }
}

fn local(mut p: PlatformPolicy, server: &MockServer) -> PlatformPolicy {
    p.hosts = vec![server.address().to_string()];
    p.allow_http = true;
    p
}

fn retarget(mut t: RequestTpl, server: &MockServer) -> RequestTpl {
    let base = server.uri();
    for host in [
        "https://api.twitch.tv",
        "https://discord.com",
        "https://www.googleapis.com",
    ] {
        t.url = t.url.replace(host, &base);
    }
    t
}

fn cfg(policy: PlatformPolicy) -> ExecutorConfig {
    let mut digests = HashMap::new();
    digests.insert(policy.platform.to_string(), DIGEST.to_string());
    ExecutorConfig::new(vec![policy], digests)
        .unwrap()
        .with_client(
            GuardedLimits {
                max_redirects: 0,
                timeout: Duration::from_secs(5),
                max_response_bytes: 4096,
            },
            vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        )
}

async fn ok_server() -> MockServer {
    let s = MockServer::start().await;
    Mock::given(any())
        .respond_with(ResponseTemplate::new(200))
        .mount(&s)
        .await;
    s
}

fn chat_send(platform_channel: &str, origin: Option<&str>, text: &str) -> OutboundAction {
    OutboundAction::ChatSend {
        channel: platform_channel.to_string(),
        text: text.to_string(),
        origin_channel: origin.map(str::to_string),
    }
}

fn chat_delete(channel: &str, origin: Option<&str>, id: &str) -> OutboundAction {
    OutboundAction::ChatDelete {
        channel: channel.to_string(),
        message_id: id.to_string(),
        origin_channel: origin.map(str::to_string),
    }
}

fn req<'a>(
    platform: &'a str,
    action: &'a OutboundAction,
    community: Option<i64>,
) -> ExecRequest<'a> {
    ExecRequest {
        platform,
        action,
        community_id: community,
    }
}

pub(crate) struct Rig {
    pub(crate) server: MockServer,
    pub(crate) builder: FixedBuilder,
    pub(crate) slots: MapSlots,
    pub(crate) exec: SenderExecutor<FixedBuilder, MapSlots, Flag, NoDuties>,
}

pub(crate) async fn rig(
    policy: PlatformPolicy,
    tpl: RequestTpl,
    slots: MapSlots,
    flag: bool,
) -> Rig {
    let server = ok_server().await;
    rig_on(server, policy, tpl, slots, flag)
}

fn rig_on(
    server: MockServer,
    policy: PlatformPolicy,
    tpl: RequestTpl,
    slots: MapSlots,
    flag: bool,
) -> Rig {
    let policy = local(policy, &server);
    let builder = FixedBuilder::new(retarget(tpl, &server));
    let exec = SenderExecutor::new(builder.clone(), slots.clone(), Flag(flag), cfg(policy));
    Rig {
        server,
        builder,
        slots,
        exec,
    }
}

pub(crate) async fn sent(r: &Rig) -> Vec<wiremock::Request> {
    r.server.received_requests().await.unwrap()
}

#[tokio::test]
async fn twitch_chat_send_substitutes_pinned_ids_and_credential_and_sends_once() {
    let r = rig(
        PlatformPolicy::twitch(),
        twitch_send(),
        MapSlots::twitch(),
        true,
    )
    .await;
    let a = chat_send("#chan", Some("#chan"), "hello");
    let out = r.exec.execute(&req("twitch", &a, Some(7))).await.unwrap();
    assert_eq!(out.status, 200);
    let got = sent(&r).await;
    assert_eq!(got.len(), 1);
    let g = &got[0];
    assert_eq!(g.method.as_str(), "POST");
    assert_eq!(g.url.path(), "/helix/chat/messages");
    assert_eq!(
        g.headers.get("authorization").unwrap().to_str().unwrap(),
        format!("Bearer {TOKEN}")
    );
    assert_eq!(
        g.headers.get("client-id").unwrap().to_str().unwrap(),
        "cid123"
    );
    let body: serde_json::Value = serde_json::from_slice(&g.body).unwrap();
    assert_eq!(body["broadcaster_id"], "111");
    assert_eq!(body["sender_id"], "222");
    // The build call carried ONLY text + an opaque numeric-community target.
    let calls = r.builder.calls.lock().unwrap();
    assert_eq!(calls[0].kind, "chat.send");
    assert_eq!(calls[0].target_source_id, "community-7");
    assert_eq!(calls[0].payload_json, r#"{"text":"hello"}"#);
    // The resolver saw the numeric community and the canonical pinned channel.
    let seen = r.slots.seen.lock().unwrap();
    assert_eq!(seen[0].0, Some(7));
    assert_eq!(seen[0].1.as_deref(), Some("#chan"));
    assert_eq!(seen[0].2.len(), 4);
}

#[tokio::test]
async fn twitch_chat_delete_is_channel_scoped_through_pinned_slots() {
    let r = rig(
        PlatformPolicy::twitch(),
        twitch_delete(),
        MapSlots::twitch(),
        true,
    )
    .await;
    let a = chat_delete("#chan", Some("#chan"), "abc-123");
    r.exec.execute(&req("twitch", &a, Some(7))).await.unwrap();
    let got = sent(&r).await;
    let g = &got[0];
    assert_eq!(g.method.as_str(), "DELETE");
    let q: HashMap<_, _> = g.url.query_pairs().into_owned().collect();
    assert_eq!(q["broadcaster_id"], "111");
    assert_eq!(q["moderator_id"], "222");
    assert_eq!(q["message_id"], "abc-123");
    assert!(g.body.is_empty());
    assert_eq!(
        r.builder.calls.lock().unwrap()[0].payload_json,
        r#"{"message_id":"abc-123"}"#
    );
}

#[tokio::test]
async fn discord_send_uses_the_stamp_channel_and_the_header_credential() {
    let slots = MapSlots::with(&[("discord-bot", TOKEN)]);
    let r = rig(PlatformPolicy::discord(), discord_send(), slots, true).await;
    let a = chat_send("123456789", Some("123456789"), "hi");
    r.exec.execute(&req("discord", &a, Some(3))).await.unwrap();
    let g = &sent(&r).await[0];
    assert_eq!(g.url.path(), "/api/v10/channels/123456789/messages");
    assert_eq!(
        g.headers.get("authorization").unwrap().to_str().unwrap(),
        format!("Bot {TOKEN}")
    );
    // The resolver was asked ONLY for the credential handle (the channel is
    // the stamp's, never a resolver answer).
    assert_eq!(r.slots.seen.lock().unwrap()[0].2, vec!["discord-bot"]);
    assert_eq!(
        r.builder.calls.lock().unwrap()[0].payload_json,
        r#"{"content":"hi"}"#
    );
}

#[tokio::test]
async fn discord_delete_is_scoped_to_the_pinned_channel() {
    let slots = MapSlots::with(&[("discord-bot", TOKEN)]);
    let r = rig(PlatformPolicy::discord(), discord_delete(), slots, true).await;
    let a = chat_delete("123456789", Some("123456789"), "555");
    r.exec.execute(&req("discord", &a, Some(3))).await.unwrap();
    let g = &sent(&r).await[0];
    assert_eq!(g.method.as_str(), "DELETE");
    assert_eq!(
        g.url.path(),
        "/api/v10/channels/123456789/messages/123456789"
    );
}

#[tokio::test]
async fn youtube_chat_send_needs_no_stamp_and_fills_the_live_chat_id_in_the_body() {
    let slots = MapSlots::with(&[
        ("youtube.oauth", TOKEN),
        ("youtube.live_chat_id", "Cg0KC-live_ID"),
    ]);
    let r = rig(PlatformPolicy::youtube(), youtube_send(), slots, true).await;
    let a = chat_send("ignored", None, "yo");
    r.exec.execute(&req("youtube", &a, Some(9))).await.unwrap();
    let g = &sent(&r).await[0];
    assert_eq!(g.url.path(), "/youtube/v3/liveChat/messages");
    assert_eq!(g.url.query(), Some("part=snippet"));
    let body: serde_json::Value = serde_json::from_slice(&g.body).unwrap();
    assert_eq!(body["snippet"]["liveChatId"], "Cg0KC-live_ID");
    assert_eq!(body["snippet"]["textMessageDetails"]["messageText"], "yo");
}

#[tokio::test]
async fn flag_off_refuses_before_anything_runs() {
    let r = rig(
        PlatformPolicy::twitch(),
        twitch_send(),
        MapSlots::twitch(),
        false,
    )
    .await;
    let a = chat_send("#chan", Some("#chan"), "hello");
    let before = exec_count("twitch", "chat.send", "flag_off");
    assert_eq!(
        r.exec
            .execute(&req("twitch", &a, Some(7)))
            .await
            .unwrap_err(),
        ExecError::FlagOff
    );
    assert_eq!(r.builder.calls(), 0);
    assert!(r.slots.seen.lock().unwrap().is_empty());
    assert!(sent(&r).await.is_empty());
    assert!(exec_count("twitch", "chat.send", "flag_off") > before);
}

#[tokio::test]
async fn unpinned_missing_or_wrong_digest_never_sends() {
    let server = ok_server().await;
    let policy = local(PlatformPolicy::twitch(), &server);
    let builder = FixedBuilder::new(retarget(twitch_send(), &server));
    // No digest pinned for the platform.
    let exec = SenderExecutor::new(
        builder.clone(),
        MapSlots::twitch(),
        Flag(true),
        ExecutorConfig::new(vec![policy.clone()], HashMap::new()).unwrap(),
    );
    let a = chat_send("#chan", Some("#chan"), "hello");
    assert_eq!(
        exec.execute(&req("twitch", &a, Some(7))).await.unwrap_err(),
        ExecError::NotPinned
    );
    // Template built by a different component than the pinned one.
    let mut other = builder.clone();
    other.digest = format!("sha256:{}", "b".repeat(64));
    let exec = SenderExecutor::new(other, MapSlots::twitch(), Flag(true), cfg(policy));
    assert_eq!(
        exec.execute(&req("twitch", &a, Some(7))).await.unwrap_err(),
        ExecError::DigestMismatch
    );
    assert!(server.received_requests().await.unwrap().is_empty());
}

#[tokio::test]
async fn wrong_channel_stamp_is_refused_and_nothing_is_built_or_sent() {
    let r = rig(
        PlatformPolicy::twitch(),
        twitch_send(),
        MapSlots::twitch(),
        true,
    )
    .await;
    let d = rig(
        PlatformPolicy::discord(),
        discord_send(),
        MapSlots::with(&[("discord-bot", TOKEN)]),
        true,
    )
    .await;
    let cases = [
        (
            &r,
            "twitch",
            chat_send("#victim", Some("#chan"), "x"),
            PinError::ChannelMismatch,
        ),
        (
            &r,
            "twitch",
            chat_send("#chan", None, "x"),
            PinError::MissingOrigin,
        ),
        (
            &r,
            "twitch",
            chat_delete("#a,#b", Some("#a,#b"), "m"),
            PinError::InvalidChannel,
        ),
        (
            &d,
            "discord",
            chat_send("111", Some("222"), "x"),
            PinError::ChannelMismatch,
        ),
        (
            &d,
            "discord",
            chat_send("not-a-flake", Some("not-a-flake"), "x"),
            PinError::InvalidChannel,
        ),
        (
            &d,
            "discord",
            chat_delete("111", None, "5"),
            PinError::MissingOrigin,
        ),
    ];
    for (rig, platform, action, want) in cases {
        let got = rig
            .exec
            .execute(&req(platform, &action, Some(1)))
            .await
            .unwrap_err();
        assert_eq!(got, ExecError::Pin(want), "{platform} {action:?}");
        assert!(got.is_policy_refusal());
        assert_eq!(rig.builder.calls(), 0);
        assert!(sent(rig).await.is_empty());
    }
}

#[tokio::test]
async fn absent_or_refused_credentials_are_typed_and_nothing_is_sent() {
    for (slots, want) in [
        (
            MapSlots {
                err: Some(SlotError::Refused("no_connection")),
                ..MapSlots::twitch()
            },
            "no_connection",
        ),
        (
            MapSlots {
                err: Some(SlotError::Refused("channel_not_owned")),
                ..MapSlots::twitch()
            },
            "channel_not_owned",
        ),
        (
            MapSlots {
                err: Some(SlotError::Unavailable("credential_unavailable")),
                ..MapSlots::twitch()
            },
            "credential_unavailable",
        ),
        // Resolver answered without the token handle: never a fallback.
        (
            MapSlots::with(&[
                ("client_id", "c"),
                ("broadcaster_id", "1"),
                ("sender_id", "2"),
            ]),
            "slot_unresolved",
        ),
    ] {
        let r = rig(PlatformPolicy::twitch(), twitch_send(), slots, true).await;
        let a = chat_send("#chan", Some("#chan"), "hello");
        let e = r
            .exec
            .execute(&req("twitch", &a, Some(7)))
            .await
            .unwrap_err();
        assert_eq!(e.code(), want);
        assert!(matches!(e, ExecError::Slot(_)));
        assert!(sent(&r).await.is_empty(), "{want}");
    }
    let r = rig(
        PlatformPolicy::twitch(),
        twitch_send(),
        MapSlots {
            err: Some(SlotError::Unavailable("credential_unavailable")),
            ..MapSlots::twitch()
        },
        true,
    )
    .await;
    let a = chat_send("#chan", Some("#chan"), "hello");
    let e = r
        .exec
        .execute(&req("twitch", &a, Some(7)))
        .await
        .unwrap_err();
    assert!(
        !e.is_policy_refusal(),
        "an unavailable resolver is retryable, not a policy refusal"
    );
}

#[tokio::test]
async fn community_scope_is_passed_through_so_the_resolver_can_refuse_none() {
    let slots = MapSlots {
        err: Some(SlotError::Refused("no_community_scope")),
        ..MapSlots::twitch()
    };
    let r = rig(PlatformPolicy::twitch(), twitch_send(), slots, true).await;
    let a = chat_send("#chan", Some("#chan"), "hello");
    let e = r.exec.execute(&req("twitch", &a, None)).await.unwrap_err();
    assert_eq!(e.code(), "no_community_scope");
    assert_eq!(r.slots.seen.lock().unwrap()[0].0, None);
    assert_eq!(
        r.builder.calls.lock().unwrap()[0].target_source_id,
        "unscoped"
    );
}

#[tokio::test]
async fn a_template_that_fails_verification_is_never_sent() {
    let base = twitch_send();
    let mutants: Vec<(&str, RequestTpl, &str)> = vec![
        (
            "authorization from template",
            {
                let mut t = base.clone();
                t.headers[0].1 = "Bearer stolen".into();
                t
            },
            "authorization_from_template",
        ),
        (
            "undeclared slot",
            {
                let mut t = base.clone();
                t.headers.push(("Accept".into(), "{{EVIL}}".into()));
                t
            },
            "undeclared_slot",
        ),
        (
            "host not allowlisted",
            {
                let mut t = base.clone();
                t.url = "https://evil.example/helix/chat/messages".into();
                t
            },
            "host_not_allowed",
        ),
        (
            "userinfo",
            {
                let mut t = base.clone();
                t.url = t.url.replace("https://", "https://a:b@");
                t
            },
            "userinfo",
        ),
        (
            "scheme",
            {
                let mut t = base.clone();
                t.url = t.url.replace("https://", "ws://");
                t
            },
            "bad_scheme",
        ),
        (
            "forbidden header",
            {
                let mut t = base.clone();
                t.headers.push(("Host".into(), "evil".into()));
                t
            },
            "header_not_allowed",
        ),
        (
            "zero width",
            {
                let mut t = base.clone();
                t.headers.push(("Accept".into(), "a\u{200b}b".into()));
                t
            },
            "invisible_char",
        ),
        (
            "oversize body",
            {
                let mut t = base.clone();
                t.body = Some(vec![b' '; 9000]);
                t
            },
            "body_too_large",
        ),
    ];
    for (name, tpl, code) in mutants {
        let r = rig_on(
            MockServer::start().await,
            PlatformPolicy::twitch(),
            tpl,
            MapSlots::twitch(),
            true,
        );
        let a = chat_send("#chan", Some("#chan"), "hello");
        let e = r
            .exec
            .execute(&req("twitch", &a, Some(7)))
            .await
            .unwrap_err();
        assert_eq!(e.code(), code, "{name}");
        assert!(matches!(e, ExecError::Template(_)), "{name}");
        assert!(e.is_policy_refusal());
        assert!(sent(&r).await.is_empty(), "{name}");
        // Resolution never ran for a refused template: no credential fetched.
        assert!(r.slots.seen.lock().unwrap().is_empty(), "{name}");
    }
}

fn hostile_policy() -> PlatformPolicy {
    PlatformPolicy {
        platform: "twitch",
        syntax: SlotSyntax::Double,
        hosts: vec![],
        slots: vec![SlotDecl {
            name: "X_ID",
            handle: "x",
            origin: SlotOrigin::Resolver,
            shape: SlotShape::Opaque,
            positions: vec![Position::Path, Position::Query("q"), Position::Body("/b")],
        }],
        kinds: vec![KindPolicy {
            kind: "chat.send",
            method: "POST",
            path: vec![PathSeg::Lit("p"), PathSeg::Slot("X_ID")],
            query: vec!["q"],
            slots: vec!["X_ID"],
            header_creds: vec![],
            max_body: Some(1024),
            text_key: "text",
            needs_duty: false,
        }],
        auth_prefixes: vec![],
        allowed_headers: vec!["content-type"],
        allow_http: true,
    }
}

fn hostile_tpl() -> RequestTpl {
    RequestTpl {
        method: "POST".into(),
        url: "https://api.twitch.tv/p/{{X_ID}}?q={{X_ID}}".into(),
        headers: vec![("Content-Type".into(), "application/json".into())],
        body: Some(br#"{"b":"{{X_ID}}"}"#.to_vec()),
        secret_refs: vec![("{{X_ID}}".into(), "x".into())],
    }
}

#[tokio::test]
async fn hostile_slot_values_are_encoded_per_position_and_cannot_break_out() {
    let hostile = [
        "a&b=c#d",
        "100%",
        "%2e%2e/x",
        "a/b",
        "../x",
        "q\"uote",
        "line\nbreak\r\n",
        "back\\slash",
        "ünï☃😀",
        "{{TWITCH_TOKEN}}",
        "a b+c?d",
        "'; DROP--",
        "}}{{",
    ];
    for v in hostile {
        let r = rig(
            hostile_policy(),
            hostile_tpl(),
            MapSlots::with(&[("x", v)]),
            true,
        )
        .await;
        let a = chat_send("#chan", Some("#chan"), "t");
        let res = r.exec.execute(&req("twitch", &a, Some(1))).await;
        assert!(res.is_ok(), "{v:?}: {res:?}");
        let got = sent(&r).await;
        assert_eq!(got.len(), 1, "{v:?}");
        let g = &got[0];
        // Path: exactly two segments, the second round-trips to the value.
        let segs: Vec<_> = g.url.path_segments().unwrap().collect();
        assert_eq!(segs.len(), 2, "{v:?} {segs:?}");
        let seg = percent_decode(segs[1]);
        assert_eq!(seg, v, "path {v:?}");
        // Query: exactly one pair, no injected parameter, no fragment.
        let pairs: Vec<_> = g.url.query_pairs().into_owned().collect();
        assert_eq!(pairs, vec![("q".to_string(), v.to_string())], "query {v:?}");
        // Body: still the single field, value byte-exact.
        let body: serde_json::Value = serde_json::from_slice(&g.body).unwrap();
        assert_eq!(body.as_object().unwrap().len(), 1, "{v:?}");
        assert_eq!(body["b"], v, "body {v:?}");
        // The raw request line carries no unencoded delimiter from the value.
        let raw = g.url.as_str();
        assert!(raw.matches('?').count() == 1 && !raw.contains('#'), "{raw}");
    }
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            out.push(u8::from_str_radix(&s[i + 1..i + 3], 16).unwrap());
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    String::from_utf8(out).unwrap()
}

#[tokio::test]
async fn dot_only_values_and_oversize_bodies_are_refused_after_substitution() {
    for v in ["..", ".", "..."] {
        let r = rig(
            hostile_policy(),
            hostile_tpl(),
            MapSlots::with(&[("x", v)]),
            true,
        )
        .await;
        let a = chat_send("#chan", Some("#chan"), "t");
        let e = r
            .exec
            .execute(&req("twitch", &a, Some(1)))
            .await
            .unwrap_err();
        assert_eq!(e, ExecError::Subst(SubstError::UrlInvalid), "{v}");
        assert!(sent(&r).await.is_empty());
    }
    let big = "x".repeat(256);
    let mut p = hostile_policy();
    // Exactly the template's own size: it verifies, the substituted value does not fit.
    p.kinds[0].max_body = Some(hostile_tpl().body.unwrap().len());
    let r = rig(p, hostile_tpl(), MapSlots::with(&[("x", &big)]), true).await;
    let a = chat_send("#chan", Some("#chan"), "t");
    let e = r
        .exec
        .execute(&req("twitch", &a, Some(1)))
        .await
        .unwrap_err();
    assert_eq!(e, ExecError::Subst(SubstError::BodyTooLarge));
    assert!(sent(&r).await.is_empty());
}

#[tokio::test]
async fn production_id_and_secret_shapes_refuse_hostile_resolver_values() {
    for (handle, bad) in [
        ("broadcaster_id", "1&moderator_id=9"),
        ("broadcaster_id", "1/../2"),
        ("sender_id", "12 34"),
        ("sender_id", ""),
        ("client_id", "c\r\nX: y"),
        ("token", "tok en"),
        ("token", "tok\nen"),
        ("token", "tök"),
        ("token", ""),
    ] {
        let mut m = MapSlots::twitch();
        m.map.insert(handle.to_string(), bad.to_string());
        let r = rig(PlatformPolicy::twitch(), twitch_send(), m, true).await;
        let a = chat_send("#chan", Some("#chan"), "hello");
        let e = r
            .exec
            .execute(&req("twitch", &a, Some(7)))
            .await
            .unwrap_err();
        assert_eq!(
            e,
            ExecError::Subst(SubstError::ShapeInvalid),
            "{handle}={bad:?}"
        );
        assert!(sent(&r).await.is_empty(), "{handle}={bad:?}");
        assert!(!format!("{e} {e:?}").contains(bad) || bad.is_empty());
    }
}

#[tokio::test]
async fn redirect_is_never_followed_by_default() {
    let server = MockServer::start().await;
    Mock::given(any())
        .respond_with(
            ResponseTemplate::new(302)
                .insert_header("Location", "https://169.254.169.254/latest/meta-data"),
        )
        .mount(&server)
        .await;
    let r = rig_on(
        server,
        PlatformPolicy::twitch(),
        twitch_delete(),
        MapSlots::twitch(),
        true,
    );
    let a = chat_delete("#chan", Some("#chan"), "abc-123");
    assert_eq!(
        r.exec
            .execute(&req("twitch", &a, Some(7)))
            .await
            .unwrap_err(),
        ExecError::Redirect
    );
    assert_eq!(
        sent(&r).await.len(),
        1,
        "exactly one request: the redirect was not followed"
    );
}

#[tokio::test]
async fn redirect_to_the_metadata_ip_is_refused_by_the_guard_when_hops_are_enabled() {
    let server = MockServer::start().await;
    Mock::given(any())
        .respond_with(
            ResponseTemplate::new(302)
                .insert_header("Location", "https://169.254.169.254/latest/meta-data"),
        )
        .mount(&server)
        .await;
    let policy = local(PlatformPolicy::twitch(), &server);
    let builder = FixedBuilder::new(retarget(twitch_delete(), &server));
    let config = cfg(policy).with_client(
        GuardedLimits {
            max_redirects: 3,
            timeout: Duration::from_secs(5),
            max_response_bytes: 4096,
        },
        vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
    );
    let exec = SenderExecutor::new(builder, MapSlots::twitch(), Flag(true), config);
    let a = chat_delete("#chan", Some("#chan"), "abc-123");
    let e = exec.execute(&req("twitch", &a, Some(7))).await.unwrap_err();
    assert_eq!(e, ExecError::Egress("forbidden_address"));
    assert!(!format!("{e} {e:?}").contains("169.254"));
}

#[tokio::test]
async fn the_guarded_client_is_the_only_egress_so_plain_http_to_an_unlisted_ip_is_refused() {
    let server = ok_server().await;
    let policy = local(PlatformPolicy::twitch(), &server);
    let builder = FixedBuilder::new(retarget(twitch_send(), &server));
    // Same verified template, but the guard's exact-ip allowance is empty (the
    // production setting): the verifier passes, the guard refuses.
    let mut digests = HashMap::new();
    digests.insert("twitch".to_string(), DIGEST.to_string());
    let config = ExecutorConfig::new(vec![policy], digests).unwrap();
    let exec = SenderExecutor::new(builder, MapSlots::twitch(), Flag(true), config);
    let a = chat_send("#chan", Some("#chan"), "hello");
    let e = exec.execute(&req("twitch", &a, Some(7))).await.unwrap_err();
    // The verifier passed; the guard (no exact-ip allowance) refuses loopback.
    assert_eq!(e, ExecError::Egress("forbidden_address"));
    assert!(server.received_requests().await.unwrap().is_empty());
}

#[tokio::test]
async fn upstream_statuses_map_to_typed_failures() {
    for (status, retry, want) in [
        (
            429u16,
            Some("7"),
            ExecError::RateLimited {
                retry_after_ms: Some(7000),
            },
        ),
        (
            429,
            None,
            ExecError::RateLimited {
                retry_after_ms: None,
            },
        ),
        (
            429,
            Some("Wed, 21 Oct 2026 07:28:00 GMT"),
            ExecError::RateLimited {
                retry_after_ms: None,
            },
        ),
        (
            429,
            Some("999999"),
            ExecError::RateLimited {
                retry_after_ms: Some(3_600_000),
            },
        ),
        (401, None, ExecError::Upstream(401)),
        (404, None, ExecError::Upstream(404)),
        (500, None, ExecError::Upstream(500)),
    ] {
        let server = MockServer::start().await;
        let mut resp = ResponseTemplate::new(status);
        if let Some(r) = retry {
            resp = resp.insert_header("Retry-After", r);
        }
        Mock::given(any()).respond_with(resp).mount(&server).await;
        let r = rig_on(
            server,
            PlatformPolicy::twitch(),
            twitch_send(),
            MapSlots::twitch(),
            true,
        );
        let a = chat_send("#chan", Some("#chan"), "hello");
        let e = r
            .exec
            .execute(&req("twitch", &a, Some(7)))
            .await
            .unwrap_err();
        assert_eq!(e, want, "{status}");
        assert!(!e.is_policy_refusal());
        assert!(!format!("{e} {e:?}").contains(TOKEN));
    }
}

#[tokio::test]
async fn oversize_response_is_bounded_by_the_guard() {
    let server = MockServer::start().await;
    Mock::given(any())
        .respond_with(ResponseTemplate::new(200).set_body_bytes(vec![b'x'; 10_000]))
        .mount(&server)
        .await;
    let r = rig_on(
        server,
        PlatformPolicy::twitch(),
        twitch_send(),
        MapSlots::twitch(),
        true,
    );
    let a = chat_send("#chan", Some("#chan"), "hello");
    assert_eq!(
        r.exec
            .execute(&req("twitch", &a, Some(7)))
            .await
            .unwrap_err(),
        ExecError::Egress("too_large")
    );
}

#[tokio::test]
async fn unsupported_platform_kind_and_missing_host_duty_fail_loud() {
    let r = rig(
        PlatformPolicy::twitch(),
        twitch_send(),
        MapSlots::twitch(),
        true,
    )
    .await;
    let a = chat_send("#chan", Some("#chan"), "x");
    assert_eq!(
        r.exec
            .execute(&req("slack", &a, Some(1)))
            .await
            .unwrap_err(),
        ExecError::UnsupportedPlatform
    );
    // youtube has no chat.delete.
    let y = rig(
        PlatformPolicy::youtube(),
        youtube_send(),
        MapSlots::default(),
        true,
    )
    .await;
    let d = chat_delete("x", None, "m");
    assert_eq!(
        y.exec
            .execute(&req("youtube", &d, Some(1)))
            .await
            .unwrap_err(),
        ExecError::UnsupportedKind
    );
    // dm.send is declared but needs a host duty (T6d-2): refused before build.
    let dm = OutboundAction::DmSend {
        user_id: "u".into(),
        text: "t".into(),
        origin_channel: "#chan".into(),
    };
    assert_eq!(
        r.exec
            .execute(&req("twitch", &dm, Some(1)))
            .await
            .unwrap_err(),
        ExecError::HostDutyMissing
    );
    assert_eq!(r.builder.calls(), 0);
    assert!(sent(&r).await.is_empty());
}

#[tokio::test]
async fn builder_failures_are_typed_and_nothing_is_sent() {
    for (fail, code, policy_refusal) in [
        (BuildError::Guest("malformed"), "build_refused", true),
        (
            BuildError::Sandbox("fuel_exhausted"),
            "build_sandbox",
            false,
        ),
        (BuildError::Unavailable, "build_unavailable", false),
    ] {
        let server = ok_server().await;
        let policy = local(PlatformPolicy::twitch(), &server);
        let mut builder = FixedBuilder::new(retarget(twitch_send(), &server));
        builder.fail = Some(fail);
        let exec = SenderExecutor::new(builder, MapSlots::twitch(), Flag(true), cfg(policy));
        let a = chat_send("#chan", Some("#chan"), "hello");
        let e = exec.execute(&req("twitch", &a, Some(7))).await.unwrap_err();
        assert_eq!(e.code(), code);
        assert_eq!(e.is_policy_refusal(), policy_refusal);
        assert!(server.received_requests().await.unwrap().is_empty());
    }
}

/// Test hook standing in for T6d-2's Discord DM duty.
struct DmDuty {
    gate_ok: bool,
    log: Arc<Mutex<Vec<&'static str>>>,
}

impl DutyHook for DmDuty {
    fn claims(&self, platform: &str, kind: &str) -> bool {
        platform == "discord" && kind == "dm.send"
    }
    async fn gate(&self, cx: &DutyCx<'_>) -> Result<(), ExecError> {
        self.log.lock().unwrap().push("gate");
        assert_eq!(cx.pinned_channel, Some("123456789"));
        if self.gate_ok {
            Ok(())
        } else {
            Err(ExecError::Slot(SlotError::Refused("not_in_community")))
        }
    }
    async fn payload(&self, _cx: &DutyCx<'_>) -> Result<String, ExecError> {
        self.log.lock().unwrap().push("payload");
        Ok(r#"{"recipient_id":"{user:x}","content":"dm"}"#.to_string())
    }
    async fn dynamic_slot(
        &self,
        _cx: &DutyCx<'_>,
        slot: &str,
        _c: &GuardedClient,
    ) -> Result<SlotValue, ExecError> {
        self.log.lock().unwrap().push("dynamic_slot");
        assert_eq!(slot, "dm.channel_id");
        Ok(SlotValue::new("999".to_string()))
    }
}

fn discord_dm_policy() -> PlatformPolicy {
    let mut p = PlatformPolicy::discord();
    p.slots.push(SlotDecl {
        name: "dm.channel_id",
        handle: "",
        origin: SlotOrigin::Duty,
        shape: SlotShape::Id,
        positions: vec![Position::Path],
    });
    let dm = p.kinds.iter_mut().find(|k| k.kind == "dm.send").unwrap();
    dm.path = vec![
        PathSeg::Lit("api"),
        PathSeg::Lit("v10"),
        PathSeg::Lit("channels"),
        PathSeg::Slot("dm.channel_id"),
        PathSeg::Lit("messages"),
    ];
    dm.slots = vec!["dm.channel_id"];
    p
}

#[tokio::test]
async fn a_registered_duty_hook_gates_shapes_the_payload_and_fills_its_slot() {
    for gate_ok in [true, false] {
        let server = ok_server().await;
        let policy = local(discord_dm_policy(), &server);
        let mut tpl = retarget(discord_send(), &server);
        tpl.url = tpl.url.replace("{target.channel_id}", "{dm.channel_id}");
        let builder = FixedBuilder::new(tpl);
        let log = Arc::new(Mutex::new(Vec::new()));
        let exec = SenderExecutor::with_duties(
            builder.clone(),
            MapSlots::with(&[("discord-bot", TOKEN)]),
            Flag(true),
            DmDuty {
                gate_ok,
                log: log.clone(),
            },
            cfg(policy),
        );
        let dm = OutboundAction::DmSend {
            user_id: "u".into(),
            text: "ignored".into(),
            origin_channel: "123456789".into(),
        };
        let res = exec.execute(&req("discord", &dm, Some(3))).await;
        let got = server.received_requests().await.unwrap();
        if gate_ok {
            res.unwrap();
            assert_eq!(got[0].url.path(), "/api/v10/channels/999/messages");
            assert_eq!(
                *log.lock().unwrap(),
                vec!["gate", "payload", "dynamic_slot"]
            );
            assert_eq!(
                builder.calls.lock().unwrap()[0].payload_json,
                r#"{"recipient_id":"{user:x}","content":"dm"}"#
            );
        } else {
            assert_eq!(res.unwrap_err().code(), "not_in_community");
            assert!(got.is_empty());
            assert_eq!(builder.calls(), 0);
        }
    }
}

#[tokio::test]
async fn metrics_count_outcomes_with_bounded_labels() {
    let r = rig(
        PlatformPolicy::twitch(),
        twitch_send(),
        MapSlots::twitch(),
        true,
    )
    .await;
    let a = chat_send("#chan", Some("#chan"), "hello");
    let ok = exec_count("twitch", "chat.send", "ok");
    let pin = exec_count("twitch", "chat.send", "pin_refused");
    r.exec.execute(&req("twitch", &a, Some(7))).await.unwrap();
    let bad = chat_send("#other", Some("#chan"), "x");
    r.exec
        .execute(&req("twitch", &bad, Some(7)))
        .await
        .unwrap_err();
    assert!(exec_count("twitch", "chat.send", "ok") > ok);
    assert!(exec_count("twitch", "chat.send", "pin_refused") > pin);
    // Unknown platform / kind collapse into "other" rather than minting series.
    let before = exec_count("other", "chat.send", "unsupported");
    r.exec
        .execute(&req("made-up", &a, Some(1)))
        .await
        .unwrap_err();
    assert!(exec_count("other", "chat.send", "unsupported") > before);
    let registry = prometheus::Registry::new();
    register_metrics(&registry).unwrap();
    assert!(register_metrics(&registry).is_err());
    assert!(!registry.gather().is_empty());
}

#[test]
fn config_validates_digests_and_reads_them_from_env() {
    let mut bad = HashMap::new();
    bad.insert("twitch".to_string(), "sha256:XYZ".to_string());
    assert_eq!(
        ExecutorConfig::production(bad).unwrap_err(),
        ConfigError::BadDigest
    );
    let mut upper = HashMap::new();
    upper.insert("twitch".to_string(), format!("sha256:{}", "A".repeat(64)));
    assert!(ExecutorConfig::production(upper).is_err());
    let mut bare = HashMap::new();
    bare.insert("twitch".to_string(), "a".repeat(64));
    assert!(ExecutorConfig::production(bare).is_err());
    let cfg = ExecutorConfig::from_env_with(|k| {
        (k == "SENDER_EXEC_DIGEST_DISCORD").then(|| DIGEST.to_string())
    })
    .unwrap();
    assert_eq!(cfg.digests.len(), 1);
    assert_eq!(cfg.digests["discord"], DIGEST);
    assert!(ExecutorConfig::from_env_with(|_| Some("nope".to_string())).is_err());
    assert_eq!(cfg.policies.len(), 3);
    assert!(cfg.policies.iter().all(|p| !p.allow_http));
    assert_eq!(cfg.limits.max_redirects, 0);
    assert!(cfg.allow_ips.is_empty());
}

#[test]
fn production_policies_never_allow_http_or_other_hosts() {
    for p in PlatformPolicy::production_set() {
        assert!(!p.allow_http);
        assert_eq!(p.hosts.len(), 1);
        assert!(!p.hosts[0].contains(':'));
        // every Resolver slot's handle is non-empty and unique
        let mut seen = std::collections::BTreeSet::new();
        for s in p.slots.iter().filter(|s| s.origin == SlotOrigin::Resolver) {
            assert!(!s.handle.is_empty() && seen.insert(s.handle));
        }
        // every kind's slots are declared
        for k in &p.kinds {
            assert!(k.slots.iter().all(|n| p.slot_index(n).is_some()));
        }
    }
}

#[tokio::test]
async fn license_flag_gate_fails_closed_without_a_client() {
    assert!(
        !LicenseFlagGate::new(None)
            .enabled(CONNECTOR_TOOLS_FLAG)
            .await
    );
}

#[tokio::test]
async fn no_duties_claims_nothing_and_refuses_every_hook() {
    let d = NoDuties;
    assert!(!d.claims("discord", "dm.send"));
    let a = chat_send("1", Some("1"), "x");
    let c = GuardedClient::new(GuardedLimits::default(), vec![]);
    let cx = DutyCx {
        platform: "discord",
        action: &a,
        community_id: None,
        pinned_channel: None,
        client: &c,
    };
    assert_eq!(d.gate(&cx).await.unwrap_err(), ExecError::HostDutyMissing);
    assert_eq!(
        d.payload(&cx).await.unwrap_err(),
        ExecError::HostDutyMissing
    );
    assert_eq!(
        d.dynamic_slot(&cx, "x", &c).await.unwrap_err(),
        ExecError::HostDutyMissing
    );
}

#[test]
fn exec_error_codes_and_refusal_classification_are_total() {
    let all = [
        ExecError::FlagOff,
        ExecError::UnsupportedPlatform,
        ExecError::UnsupportedKind,
        ExecError::HostDutyMissing,
        ExecError::NotPinned,
        ExecError::DigestMismatch,
        ExecError::Pin(PinError::MissingOrigin),
        ExecError::BadAction,
        ExecError::Build(BuildError::Guest("g")),
        ExecError::Build(BuildError::Sandbox("s")),
        ExecError::Build(BuildError::Unavailable),
        ExecError::Template(TemplateError::BadUrl),
        ExecError::Slot(SlotError::Refused("r")),
        ExecError::Slot(SlotError::Unavailable("u")),
        ExecError::Subst(SubstError::UrlInvalid),
        ExecError::Egress("e"),
        ExecError::Redirect,
        ExecError::RateLimited {
            retry_after_ms: None,
        },
        ExecError::Upstream(500),
    ];
    for e in &all {
        assert!(!e.code().is_empty() && !e.to_string().is_empty() && !e.outcome().is_empty());
    }
    let refusals = all.iter().filter(|e| e.is_policy_refusal()).count();
    // Not policy refusals: sandbox trip, build unavailable, resolver
    // unavailable, egress failure, redirect, rate limit, upstream status.
    assert_eq!(refusals, all.len() - 7);
}

#[test]
fn a_dm_entry_cannot_become_a_plain_payload() {
    let k = PlatformPolicy::twitch();
    let kind = k.kind("chat.send").unwrap();
    let dm = OutboundAction::DmSend {
        user_id: "u".into(),
        text: "t".into(),
        origin_channel: "#c".into(),
    };
    assert_eq!(
        default_payload(kind, &dm).unwrap_err(),
        ExecError::BadAction
    );
}

#[tokio::test]
async fn a_malformed_header_credential_is_refused_and_never_sent() {
    for bad in ["tok en", "tok\nen", "t\u{f6}k", ""] {
        let slots = MapSlots::with(&[("discord-bot", bad)]);
        let r = rig(PlatformPolicy::discord(), discord_send(), slots, true).await;
        let a = chat_send("123456789", Some("123456789"), "hi");
        let e = r
            .exec
            .execute(&req("discord", &a, Some(3)))
            .await
            .unwrap_err();
        assert_eq!(e, ExecError::Subst(SubstError::ShapeInvalid), "{bad:?}");
        assert!(sent(&r).await.is_empty());
    }
}
