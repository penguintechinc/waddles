//! Real-path proof of the generic self-echo filter on EVERY chat platform: the real profile of
//! each platform plans a real binding, the real `HubBindingFeed` records the binding's
//! `self_user_id` into the registry, and the real `ScopedSinkFactory`/`ConnectionSink` publishes
//! onto a recording spine appender. Only the hub-api binding list, the scope authority and the
//! appender are fakes (the boundaries, not the logic under test).
//!
//! Asserts per platform: the bot's own `message` is dropped, another author's message, a
//! non-message event of the bot and an unconfigured connection all publish.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use connection_resolver::{
    BindingPage, BindingSummary, ConnectionKind, ConnectionRef, ConnectionResolve, Endpoint,
    ResolveError, ResolvedConnection, SecretString,
};
use penguin_spine::{KeyRing, NoopMetrics, SpineError, StageEnvelope};
use svc_ingest::connector::webhook::BoxFut;
use svc_ingest::connector::{ConnectionCtx, ConnectorEvent, EventSink};
use svc_ingest::connector_runtime::profiles::{discord, kick, twitch, youtube};
use svc_ingest::connector_runtime::scoped::{ScopeLookup, ScopedSinkFactory};
use svc_ingest::connector_runtime::self_echo::{dropped_count, SelfEchoRegistry};
use svc_ingest::connector_runtime::{
    BindingFeed, ConnectorProfile, HubBindingFeed, RuntimeEnv, SinkFactory,
};
use svc_ingest::identity_crypto::{Dek, DekProvider, DekUnavailableError};
use svc_ingest::publish::EventAppender;
use svc_ingest::scope_resolver::{ResolvedScope, Unresolved};

const COMMUNITY: i64 = 7;
const TENANT: i64 = 5;
const BOT: &str = "4242";

/// The hub-api binding list: one fixed set, no secrets.
struct Hub(Vec<BindingSummary>);

impl ConnectionResolve for Hub {
    async fn resolve(&self, _r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
        Err(ResolveError::Malformed("not used by the feed"))
    }

    async fn list_bindings(
        &self,
        provider: &str,
        _c: Option<&str>,
        _after: i64,
        _limit: u32,
    ) -> Result<BindingPage, ResolveError> {
        Ok(BindingPage {
            items: self
                .0
                .iter()
                .filter(|b| b.provider == provider)
                .cloned()
                .collect(),
            next_after: None,
        })
    }
}

#[allow(dead_code)]
fn _assert_resolved_shape(_: ResolvedConnection, _: Endpoint, _: SecretString) {}

/// The scope authority: every lookup resolves to the one community.
struct Scope;

impl ScopeLookup for Scope {
    fn lookup<'a>(
        &'a self,
        _platform: &'a str,
        _source: &'a str,
    ) -> BoxFut<'a, Result<ResolvedScope, Unresolved>> {
        Box::pin(async {
            Ok(ResolvedScope::validated(TENANT, COMMUNITY, "acme", Some("main")).unwrap())
        })
    }
}

#[derive(Default)]
struct Rec(Mutex<Vec<StageEnvelope>>);

impl EventAppender for Rec {
    async fn append(&self, _stream: &str, env: &StageEnvelope) -> Result<String, SpineError> {
        self.0.lock().unwrap().push(env.clone());
        Ok("1-0".into())
    }
}

struct FakeDek;

impl DekProvider for FakeDek {
    async fn get_dek(&self, _tenant_id: i64) -> Result<(Dek, u32), DekUnavailableError> {
        Ok((zeroize::Zeroizing::new([9u8; 32]), 1))
    }
}

fn binding(provider: &str, connection_id: i64, meta: &[(&str, &str)]) -> BindingSummary {
    BindingSummary {
        tenant_id: TENANT,
        secret_version: 1,
        community_id: COMMUNITY,
        connection_id,
        provider: provider.into(),
        kind: ConnectionKind::PlatformOauth,
        metadata: meta
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect::<BTreeMap<_, _>>(),
    }
}

fn env() -> RuntimeEnv {
    RuntimeEnv {
        environment: "test".into(),
        max_frame_bytes: 65_536,
        egress_allow: vec![],
        heartbeat_enabled: true,
    }
}

fn event(platform: &str, event_type: &str, payload: serde_json::Value) -> ConnectorEvent {
    ConnectorEvent {
        platform: platform.into(),
        event_type: event_type.into(),
        actor: Some("0b9f6a2c-aaaa-bbbb-cccc-111122223333".into()),
        payload_json: payload.to_string(),
        occurred_at: "2026-11-07T00:00:00.000Z".into(),
    }
}

/// Plans `bindings` through `profile` via the real feed (recording self identities into
/// `registry`), builds each connection's real sink, and returns the sinks by connection id.
async fn sinks<P: ConnectorProfile + 'static>(
    profile: P,
    bindings: Vec<BindingSummary>,
    registry: &SelfEchoRegistry,
    rec: &Arc<Rec>,
) -> BTreeMap<i64, svc_ingest::connector_runtime::scoped::ConnectionSink<Rec, FakeDek>> {
    let platform = profile.platform();
    let feed = HubBindingFeed::new(
        Arc::new(Hub(bindings)),
        profile,
        env(),
        Duration::from_secs(5),
    )
    .with_self_echo(registry.clone());
    let desired = feed.desired(platform).await.expect("discovery");
    let factory = ScopedSinkFactory::new(
        Arc::clone(rec),
        Arc::new(NoopMetrics),
        Arc::new(KeyRing::new(vec![("k1".to_string(), vec![7u8; 32])])),
        "k1".into(),
        Arc::new(FakeDek),
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("self_echo_real_identity_total", "test"),
            &["tenant", "status"],
        )
        .unwrap(),
        Arc::new(Scope),
    )
    .with_self_echo(registry.clone());
    let mut out = BTreeMap::new();
    for d in &desired {
        let sink = factory.build(d, TENANT).await.expect("sink builds");
        out.insert(d.key.connection_id, sink);
    }
    assert!(!out.is_empty(), "the feed planned no connection");
    out
}

fn ctx(platform: &str) -> ConnectionCtx {
    ConnectionCtx {
        platform: platform.into(),
        source_id: "ws-0123abcd".into(),
        connection_id: "conn-1".into(),
        session_token: None,
    }
}

/// Publishes the four probe events and returns which of them reached the spine, by label.
async fn probe<S: EventSink>(sink: &S, rec: &Rec, platform: &str, key: &str) -> Vec<&'static str> {
    let own = serde_json::json!({ key: BOT, "text": "bot reply" });
    let viewer = serde_json::json!({ key: "9999", "text": "hello" });
    let mut reached = Vec::new();
    for (label, ev) in [
        ("own_message", event(platform, "message", own.clone())),
        ("viewer_message", event(platform, "message", viewer)),
        ("own_follow", event(platform, "channel.follow", own)),
    ] {
        let before = rec.0.lock().unwrap().len();
        sink.publish(&ctx(platform), ev).await.unwrap();
        if rec.0.lock().unwrap().len() > before {
            reached.push(label);
        }
    }
    reached
}

#[tokio::test(flavor = "multi_thread")]
async fn twitch_discord_youtube_and_kick_all_drop_the_bots_own_messages() {
    let registry = SelfEchoRegistry::new();
    let cases: [(&str, &str, Vec<BindingSummary>); 4] = [
        (
            "twitch",
            "user_id",
            vec![binding(
                "twitch",
                11,
                &[
                    ("channel", "somechan"),
                    ("bot_login", "penguinbot"),
                    ("self_user_id", BOT),
                ],
            )],
        ),
        (
            "discord",
            "author_id",
            vec![binding(
                "discord",
                12,
                &[("guild_id", "123456789012345678"), ("self_user_id", BOT)],
            )],
        ),
        (
            "youtube",
            "author_id",
            vec![binding(
                "youtube",
                13,
                &[("live_chat_id", "Cg0KC-live_ID"), ("self_user_id", BOT)],
            )],
        ),
        (
            "kick",
            "user_id",
            vec![binding(
                "kick",
                14,
                &[("chatroom_id", "12345"), ("self_user_id", BOT)],
            )],
        ),
    ];
    for (platform, key, bindings) in cases {
        let rec = Arc::new(Rec::default());
        let before = dropped_count(platform);
        let built = match platform {
            "twitch" => {
                sinks(
                    twitch::TwitchProfile::from_feed(),
                    bindings,
                    &registry,
                    &rec,
                )
                .await
            }
            "discord" => {
                sinks(
                    discord::DiscordProfile::new().with_budget_ready(true),
                    bindings,
                    &registry,
                    &rec,
                )
                .await
            }
            "youtube" => sinks(youtube::YoutubeProfile::new(), bindings, &registry, &rec).await,
            _ => sinks(kick::KickProfile::from_feed(), bindings, &registry, &rec).await,
        };
        let sink = built.values().next().unwrap();
        let reached = probe(sink, &rec, platform, key).await;
        assert_eq!(
            reached,
            ["viewer_message", "own_follow"],
            "{platform}: only the bot's own chat message may be dropped"
        );
        assert_eq!(dropped_count(platform), before + 1, "{platform}");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_binding_without_self_user_id_is_never_filtered_and_a_rediscovery_updates_live() {
    let registry = SelfEchoRegistry::new();
    let rec = Arc::new(Rec::default());
    // No self_user_id: nothing dropped.
    let built = sinks(
        kick::KickProfile::from_feed(),
        vec![binding("kick", 14, &[("chatroom_id", "12345")])],
        &registry,
        &rec,
    )
    .await;
    let sink = built.values().next().unwrap();
    assert_eq!(
        probe(sink, &rec, "kick", "user_id").await,
        ["own_message", "viewer_message", "own_follow"]
    );

    // The same live sink starts dropping once a COMPLETE rediscovery carries the id.
    let feed = HubBindingFeed::new(
        Arc::new(Hub(vec![binding(
            "kick",
            14,
            &[("chatroom_id", "12345"), ("self_user_id", BOT)],
        )])),
        kick::KickProfile::from_feed(),
        env(),
        Duration::from_secs(5),
    )
    .with_self_echo(registry.clone());
    feed.desired("kick").await.expect("discovery");
    assert_eq!(
        probe(sink, &rec, "kick", "user_id").await,
        ["viewer_message", "own_follow"]
    );

    // And stops again when the binding loses it (or leaves the list).
    let feed = HubBindingFeed::new(
        Arc::new(Hub(vec![])),
        kick::KickProfile::from_feed(),
        env(),
        Duration::from_secs(5),
    )
    .with_self_echo(registry);
    feed.desired("kick").await.expect("discovery");
    assert_eq!(
        probe(sink, &rec, "kick", "user_id").await,
        ["own_message", "viewer_message", "own_follow"]
    );
}
