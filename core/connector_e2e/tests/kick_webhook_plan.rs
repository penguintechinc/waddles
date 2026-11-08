//! Real-path e2e of Kick webhook route PLANNING and SUBSCRIPTION: the real `KickProfile` plans a
//! binding served by a fake hub-api through the real `HubBindingFeed` and `ConnectionResolver`;
//! the webhook lane is activated on a real `WebhookIngress` by the production `IngressAttach`
//! (`activate_route`), which also starts the production `KickSubscriber` (real
//! `ResolverKickCredentials`, real HTTP client) against a fake Kick events endpoint.
//!
//! Doubles (and only these): the hub-api and Kick HTTP endpoints (wiremock) and an in-memory
//! dedup store / inert webhook secret source (no delivery is verified here; that is
//! `kick_webhook.rs`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use connection_resolver::{
    ConnectionKind, ConnectionRef, ConnectionResolver, ResolverConfig, ResolverMetrics,
    SecretString,
};
use svc_ingest::connector::kick_subscribe::{
    BackgroundSubscriber, HttpKickEvents, KickSubscriber, SharedResolver, EVENTS,
};
use svc_ingest::connector::webhook::{
    BoxFut, DedupToken, SecretError, WebhookDedup, WebhookIngress, WebhookSecretSource,
};
use svc_ingest::connector::webhook_verify::Scheme;
use svc_ingest::connector_runtime::boot::IngressAttach;
use svc_ingest::connector_runtime::feed::Lane;
use svc_ingest::connector_runtime::profiles::kick::KickProfile;
use svc_ingest::connector_runtime::webhook_boot::{RouteKeyDeriver, WebhookEnv};
use svc_ingest::connector_runtime::{
    BindingFeed, HubBindingFeed, RuntimeEnv, TransportPlan, WebhookAttach,
};
use svc_ingest::outbound_creds_kick::ResolverKickCredentials;
use wiremock::matchers::{method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

const COMMUNITY: i64 = 7;
const CONNECTION: i64 = 12;
const TOKEN: &str = "KICK-OAUTH-SECRET";
const SECRET: &str = "0123456789abcdef0123456789abcdef-route-key-secret";

struct NoMetrics;

impl ResolverMetrics for NoMetrics {
    fn resolve(&self, _: ConnectionKind, _: &'static str, _: Duration) {}
    fn list(&self, _: &'static str) {}
}

struct NoInner;

impl WebhookSecretSource for NoInner {
    fn resolve(&self, _r: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
        Box::pin(async { Err(SecretError::Missing) })
    }
}

#[derive(Default)]
struct MemDedup(Mutex<HashSet<String>>);

impl WebhookDedup for MemDedup {
    fn first_seen<'a>(&'a self, key: &'a str) -> BoxFut<'a, Result<Option<DedupToken>, ()>> {
        let fresh = self.0.lock().unwrap().insert(key.to_string());
        Box::pin(async move { Ok(fresh.then(|| DedupToken("t".into()))) })
    }
    fn release<'a>(&'a self, key: &'a str, _t: &'a DedupToken) -> BoxFut<'a, Result<(), ()>> {
        self.0.lock().unwrap().remove(key);
        Box::pin(async { Ok(()) })
    }
}

struct TestSa(std::path::PathBuf);

impl Drop for TestSa {
    fn drop(&mut self) {
        if let Err(e) = std::fs::remove_file(&self.0) {
            eprintln!("synthetic sa token cleanup failed: {e}");
        }
    }
}

/// A fake hub-api serving one Kick connection with the given metadata.
async fn hub(
    metadata: serde_json::Value,
    tag: &str,
) -> (MockServer, Arc<ConnectionResolver>, TestSa) {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/service-token"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(serde_json::json!({"token": "machine-tok", "expires_in": 3600})),
        )
        .mount(&server)
        .await;
    Mock::given(method("GET"))
        .and(path("/api/v1/internal/provider-connections"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "success": true,
            "items": [{"communityId": COMMUNITY, "connectionId": CONNECTION, "tenantId": 3,
                       "secretVersion": 1, "provider": "kick", "kind": 1, "metadata": metadata}],
            "nextAfter": null
        })))
        .mount(&server)
        .await;
    Mock::given(method("POST"))
        .and(path(format!(
            "/api/v1/internal/communities/{COMMUNITY}/provider-connections/{CONNECTION}/resolve"
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "communityId": COMMUNITY, "connectionId": CONNECTION, "provider": "kick", "kind": 1,
            "accessToken": TOKEN, "expiresAt": null, "metadata": metadata
        })))
        .mount(&server)
        .await;
    let sa = std::env::temp_dir().join(format!("kick-plan-sa-{}-{tag}", std::process::id()));
    std::fs::write(&sa, "synthetic-sa").unwrap();
    let resolver = ConnectionResolver::new(
        ResolverConfig::new(
            server.uri(),
            "alpha",
            format!("{}/internal/service-token", server.uri()),
            sa.to_string_lossy().to_string(),
        ),
        Arc::new(NoMetrics),
    )
    .unwrap();
    (server, Arc::new(resolver), TestSa(sa))
}

fn env() -> RuntimeEnv {
    RuntimeEnv {
        environment: "alpha".into(),
        max_frame_bytes: 65_536,
        egress_allow: vec![],
        heartbeat_enabled: false,
    }
}

fn webhook_env() -> Arc<WebhookEnv> {
    let lookup = |k: &str| match k {
        "CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS" => Some("kick".to_owned()),
        "CONNECTOR_ROUTE_KEY_SECRET" => Some(SECRET.to_owned()),
        _ => None,
    };
    Arc::new(WebhookEnv::from_lookup(&lookup).unwrap())
}

fn deriver() -> RouteKeyDeriver {
    RouteKeyDeriver::new(SecretString::new(SECRET.to_owned())).unwrap()
}

async fn desired(
    resolver: &Arc<ConnectionResolver>,
    webhook: bool,
) -> Result<
    Vec<svc_ingest::connector_runtime::DesiredConnection>,
    svc_ingest::connector_runtime::FeedError,
> {
    let profile = KickProfile::from_feed().with_webhook(webhook.then(deriver));
    HubBindingFeed::new(Arc::clone(resolver), profile, env(), Duration::from_secs(5))
        .desired("kick")
        .await
}

#[tokio::test(flavor = "multi_thread")]
async fn the_feed_plans_two_lanes_and_the_webhook_route_activates_and_subscribes() {
    let (_hub, resolver, _sa) = hub(
        serde_json::json!({"chatroom_id": "4242", "broadcaster_user_id": "555"}),
        "ok",
    )
    .await;

    // Without the webhook deriver only the socket lane exists.
    let chat_only = desired(&resolver, false).await.unwrap();
    assert_eq!(chat_only.len(), 1);
    assert_eq!(chat_only[0].key.lane, Lane::Primary);

    let both = desired(&resolver, true).await.unwrap();
    assert_eq!(both.len(), 2, "pusher socket + webhook lane");
    assert!(both.iter().any(|d| d.key.lane == Lane::Primary));
    let hook = both.iter().find(|d| d.key.lane == Lane::Webhook).unwrap();
    assert_eq!(hook.key.connection_id, CONNECTION);
    assert_eq!(hook.tenant_id, 3);
    let TransportPlan::Webhook(plan) = &hook.plan else {
        panic!("webhook lane must plan a webhook transport");
    };
    let Scheme::Kick(spec) = &plan.route.scheme else {
        panic!("kick scheme");
    };
    assert_eq!(spec.broadcaster_id, "555");

    // A fake Kick events endpoint: nothing subscribed yet, then the create succeeds.
    let kick = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/public/v1/events/subscriptions"))
        .and(query_param("broadcaster_user_id", "555"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"data": []})))
        .mount(&kick)
        .await;
    let created: Vec<serde_json::Value> = EVENTS
        .iter()
        .map(|(n, _)| serde_json::json!({"name": n, "subscription_id": "s"}))
        .collect();
    Mock::given(method("POST"))
        .and(path("/public/v1/events/subscriptions"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(serde_json::json!({ "data": created })),
        )
        .mount(&kick)
        .await;

    let subscriber = KickSubscriber::new(
        ResolverKickCredentials::new(SharedResolver(Arc::clone(&resolver))),
        HttpKickEvents::insecure_for_tests().unwrap(),
    )
    .with_base(format!("{}/public/v1", kick.uri()));
    let background = Arc::new(BackgroundSubscriber::new(subscriber));
    let ingress = Arc::new(WebhookIngress::new(
        Arc::new(NoInner),
        Arc::new(MemDedup::default()),
        None,
    ));
    let attach = IngressAttach::new(Arc::clone(&ingress), webhook_env())
        .with_kick_subscribe(Arc::clone(&background) as _);

    // Production activation: the host-derived route key registers and a transport is bound.
    let _transport = attach.attach(plan, &hook.key).expect("route activates");
    assert_eq!(
        plan.route.route_key,
        deriver().derive("kick", CONNECTION).unwrap()
    );

    // The background subscription reaches Kick with the community's own token + integer id.
    let mut seen = vec![];
    for _ in 0..100 {
        seen = kick.received_requests().await.unwrap();
        if seen.len() >= 2 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(seen.len(), 2, "list + create");
    assert!(seen.iter().all(|r| r
        .headers
        .get("authorization")
        .is_some_and(|v| v.to_str().unwrap() == format!("Bearer {TOKEN}"))));
    let body: serde_json::Value = serde_json::from_slice(&seen[1].body).unwrap();
    assert_eq!(body["broadcaster_user_id"], 555);
    assert!(body["broadcaster_user_id"].is_u64());
    assert_eq!(body["method"], "webhook");
    assert_eq!(body["events"].as_array().unwrap().len(), EVENTS.len());
    assert_eq!(background.tracked(), 1);

    // Detach cancels tracking and removes the route (re-activation then succeeds again).
    attach.detach(plan);
    assert_eq!(background.tracked(), 0);
    let _again = attach.attach(plan, &hook.key).expect("re-activates");
    attach.detach(plan);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_binding_without_a_valid_broadcaster_is_refused_loudly_when_webhooks_are_on() {
    for (tag, meta) in [
        ("missing", serde_json::json!({"chatroom_id": "4242"})),
        (
            "invalid",
            serde_json::json!({"chatroom_id": "4242", "broadcaster_user_id": "abc"}),
        ),
    ] {
        let (_hub, resolver, _sa) = hub(meta, tag).await;
        // The feed skips the malformed binding entirely (no chat-only fallback) ...
        assert!(
            desired(&resolver, true).await.unwrap().is_empty(),
            "{tag}: refused"
        );
        // ... while a chat-only deployment (webhooks off) is unaffected.
        assert_eq!(desired(&resolver, false).await.unwrap().len(), 1, "{tag}");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn two_communities_binding_one_broadcaster_are_both_dropped() {
    // The same hub row served under two communities would claim one broadcaster twice.
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/service-token"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(serde_json::json!({"token": "machine-tok", "expires_in": 3600})),
        )
        .mount(&server)
        .await;
    let item = |community: i64, conn: i64, room: &str| {
        serde_json::json!({"communityId": community, "connectionId": conn, "tenantId": 3,
            "secretVersion": 1, "provider": "kick", "kind": 1,
            "metadata": {"chatroom_id": room, "broadcaster_user_id": "555"}})
    };
    Mock::given(method("GET"))
        .and(path("/api/v1/internal/provider-connections"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "success": true,
            "items": [item(7, 12, "1"), item(8, 13, "2")],
            "nextAfter": null
        })))
        .mount(&server)
        .await;
    let sa = std::env::temp_dir().join(format!("kick-plan-sa-{}-dup", std::process::id()));
    std::fs::write(&sa, "synthetic-sa").unwrap();
    let _sa = TestSa(sa.clone());
    let resolver = Arc::new(
        ConnectionResolver::new(
            ResolverConfig::new(
                server.uri(),
                "alpha",
                format!("{}/internal/service-token", server.uri()),
                sa.to_string_lossy().to_string(),
            ),
            Arc::new(NoMetrics),
        )
        .unwrap(),
    );
    let got = desired(&resolver, true).await.unwrap();
    // Distinct chatrooms keep both socket lanes; the shared broadcaster drops BOTH webhook lanes.
    assert_eq!(
        got.iter().filter(|d| d.key.lane == Lane::Primary).count(),
        2
    );
    assert_eq!(
        got.iter().filter(|d| d.key.lane == Lane::Webhook).count(),
        0
    );
}
