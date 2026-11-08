//! Real-path e2e of Kick outbound through the generic executor: the REAL compiled Kick bundle
//! builds the request template over the REAL executor wire (`WireTemplateBuilder` ->
//! `ExecutorLink` -> `run_connection` -> `ConnectorHost` -> wasm), the PRODUCTION Kick policy
//! (`PlatformPolicy::kick`), verifier, substituter and `GuardedClient` run unmodified, and the
//! request is asserted on the wire at a local fake Kick endpoint.
//!
//! The per-community slot source is the PRODUCTION `KickSlots` over `ResolverKickCredentials`
//! over the real `ConnectionResolver`, talking to a local fake hub-api. The only double besides
//! the fake endpoints is a `TemplateBuilder` decorator that rewrites ONLY the scheme+host of the
//! bundle's template to the local fake after asserting the bundle named the real Kick API host
//! (https cannot be terminated by a local plain-http fake). No Valkey is needed: this drives
//! `SenderExecutor::execute` directly.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr};
use std::sync::Arc;
use std::time::Duration;

use bundle_executor::manifest::WitWorld;
use bundle_host_http::guarded::GuardedLimits;
use connection_resolver::{ConnectionKind, ConnectionResolver, ResolverConfig, ResolverMetrics};
use connector_e2e::{digest_of, RigConnector, WireRig};
use svc_ingest::connector::sender_exec::kick_slots::KickSlots;
use svc_ingest::connector::sender_exec::policy::registered_op;
use svc_ingest::connector::sender_exec::subst::SubstError;
use svc_ingest::connector::sender_exec::{
    BuildError, BuiltTemplate, ExecError, ExecRequest, ExecutorConfig, PlatformPolicy,
    SenderAction, SenderExecutor, TemplateBuilder, WireTemplateBuilder,
};
use svc_ingest::outbound_boot::CachedFlagGate;
use svc_ingest::outbound_creds_kick::ResolverKickCredentials;
use svc_ingest::outbound_ops::{OutboundAction, PolicyOp};
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const WASM: &[u8] = include_bytes!("fixtures/kick_connector.wasm");
const COMMUNITY: i64 = 7;
const TOKEN: &str = "KICK-OAUTH-SECRET";
const CHAN: &str = "kick-chan";
const BROADCASTER: &str = "777";
const MSG_ID: &str = "828f83bb-e391-4c78-9e91-af35a172840e";

struct NoMetrics;

impl ResolverMetrics for NoMetrics {
    fn resolve(&self, _: ConnectionKind, _: &'static str, _: Duration) {}
    fn list(&self, _: &'static str) {}
}

/// Synthetic service-account token file, removed on drop.
struct TestSa(std::path::PathBuf);

impl Drop for TestSa {
    fn drop(&mut self) {
        if let Err(e) = std::fs::remove_file(&self.0) {
            eprintln!("synthetic sa token cleanup failed: {e}");
        }
    }
}

type Slots = KickSlots<ResolverKickCredentials<ConnectionResolver>>;

/// A fake hub-api serving ONE Kick connection (community 7, connection 12, kind-1 token).
/// Returns the server (request counts are read back from it) and the production slot source.
async fn hub(broadcaster: Option<&str>) -> (MockServer, Slots, TestSa) {
    let metadata = broadcaster.map_or_else(
        || serde_json::json!({}),
        |b| serde_json::json!({ "broadcaster_user_id": b }),
    );
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
            "items": [{"communityId": COMMUNITY, "connectionId": 12, "tenantId": 3,
                       "secretVersion": 1, "provider": "kick", "kind": 1, "metadata": metadata}],
            "nextAfter": null
        })))
        .mount(&server)
        .await;
    Mock::given(method("POST"))
        .and(path(format!(
            "/api/v1/internal/communities/{COMMUNITY}/provider-connections/12/resolve"
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "communityId": COMMUNITY, "connectionId": 12, "provider": "kick", "kind": 1,
            "accessToken": TOKEN, "expiresAt": null, "metadata": metadata
        })))
        .mount(&server)
        .await;
    // One file per rig: tests run in parallel inside this process and each removes its own on drop.
    static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let sa = std::env::temp_dir().join(format!("kick-sa-{}-{n}", std::process::id()));
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
    (
        server,
        KickSlots::new(ResolverKickCredentials::new(resolver)),
        TestSa(sa),
    )
}

async fn resolves(hub: &MockServer) -> usize {
    hub.received_requests()
        .await
        .unwrap()
        .iter()
        .filter(|r| r.url.path().ends_with("/resolve"))
        .count()
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
            .strip_prefix("https://api.kick.com")
            .unwrap_or_else(|| panic!("bundle did not name api.kick.com: {}", built.tpl.url))
            .to_string();
        built.tpl.url = format!("{}{rest}", self.base);
        Ok(built)
    }
}

type Exec =
    SenderExecutor<Rewrite, Slots, CachedFlagGate, svc_ingest::connector::sender_exec::NoDuties>;

struct Rig {
    exec: Exec,
    server: MockServer,
    hub: MockServer,
    _sa: TestSa,
    _wire: WireRig,
}

async fn rig() -> Rig {
    rig_with(Some(BROADCASTER)).await
}

async fn rig_with(broadcaster: Option<&str>) -> Rig {
    let wire = WireRig::start_with_permissions(
        vec![RigConnector {
            platform: "kick".into(),
            app_id: "waddles.core.service.kick".into(),
            wasm: WASM,
            world: WitWorld::ConnectorTools,
        }],
        &["connector.pii.read"],
        &["waddles.connector-kick"],
        64 * 1024,
        |_| {},
    )
    .await;
    let server = MockServer::start().await;
    let mut policy = PlatformPolicy::kick();
    policy.hosts = vec![server.address().to_string()];
    policy.allow_http = true;
    let cfg = ExecutorConfig::new(
        vec![policy],
        HashMap::from([("kick".to_string(), digest_of(WASM))]),
    )
    .unwrap()
    .with_client(
        GuardedLimits {
            max_redirects: 0,
            timeout: Duration::from_secs(5),
            max_response_bytes: 64 * 1024,
        },
        vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
    );
    let (hub, slots, sa) = hub(broadcaster).await;
    let exec = SenderExecutor::new(
        Rewrite {
            inner: WireTemplateBuilder::new(Arc::clone(&wire.link), Arc::clone(&wire.registry)),
            base: server.uri(),
        },
        slots,
        CachedFlagGate(true),
        cfg,
    );
    Rig {
        exec,
        server,
        hub,
        _sa: sa,
        _wire: wire,
    }
}

fn send(text: &str) -> OutboundAction {
    OutboundAction::ChatSend {
        channel: CHAN.into(),
        text: text.into(),
        origin_channel: Some(CHAN.into()),
    }
}

fn req(action: &OutboundAction) -> ExecRequest<'_> {
    ExecRequest {
        platform: "kick",
        action,
        community_id: Some(COMMUNITY),
    }
}

fn header(r: &wiremock::Request, name: &str) -> String {
    r.headers
        .get(name)
        .map(|v| v.to_str().unwrap().to_string())
        .unwrap_or_default()
}

#[tokio::test(flavor = "multi_thread")]
async fn chat_send_reaches_the_kick_api_with_the_host_resolved_token_and_no_destination() {
    let rig = rig().await;
    Mock::given(method("POST"))
        .and(path("/public/v1/chat"))
        .respond_with(ResponseTemplate::new(200).set_body_json(
            serde_json::json!({"data": {"is_sent": true, "message_id": MSG_ID}, "message": "ok"}),
        ))
        .mount(&rig.server)
        .await;

    let action = send("hello kick");
    rig.exec.execute(&req(&action)).await.expect("chat.send");

    let reqs = rig.server.received_requests().await.unwrap();
    assert_eq!(reqs.len(), 1);
    let r = &reqs[0];
    assert_eq!(header(r, "authorization"), format!("Bearer {TOKEN}"));
    let body: serde_json::Value = serde_json::from_slice(&r.body).unwrap();
    assert_eq!(
        body,
        serde_json::json!({"content": "hello kick", "type": "bot"}),
        "no channel/broadcaster field is ever sent"
    );
    // The token was resolved by the host from hub-api, once for this one send.
    assert_eq!(resolves(&rig.hub).await, 1);
    rig.exec.execute(&req(&action)).await.expect("second send");
    assert_eq!(
        resolves(&rig.hub).await,
        2,
        "resolved fresh per send, never cached"
    );
}

/// `chat.delete` is registered and PINNED: the stamped channel must equal the origin, the
/// community token rides the request, and a mismatched channel never reaches Kick.
#[tokio::test(flavor = "multi_thread")]
async fn chat_delete_reaches_kick_only_for_the_pinned_channel() {
    let rig = rig().await;
    Mock::given(method("DELETE"))
        .and(path(format!("/public/v1/chat/{MSG_ID}")))
        .respond_with(ResponseTemplate::new(204))
        .mount(&rig.server)
        .await;
    assert_eq!(registered_op("kick", "chat.delete"), Some("chat.delete"));
    let action = OutboundAction::ChatDelete {
        channel: CHAN.into(),
        message_id: MSG_ID.into(),
        origin_channel: Some(CHAN.into()),
    };
    rig.exec.execute(&req(&action)).await.expect("chat.delete");
    let reqs = rig.server.received_requests().await.unwrap();
    assert_eq!(reqs.len(), 1);
    assert_eq!(header(&reqs[0], "authorization"), format!("Bearer {TOKEN}"));
    assert!(reqs[0].body.is_empty());

    // A channel that is not the stamped origin is refused before anything is resolved or sent.
    let before = resolves(&rig.hub).await;
    let other = OutboundAction::ChatDelete {
        channel: "someone-else".into(),
        message_id: MSG_ID.into(),
        origin_channel: Some(CHAN.into()),
    };
    let err = rig.exec.execute(&req(&other)).await.unwrap_err();
    assert!(matches!(err, ExecError::Pin(_)), "got {err:?}");
    // No origin at all is refused too (a pinned kind never runs unpinned).
    let none = OutboundAction::ChatDelete {
        channel: CHAN.into(),
        message_id: MSG_ID.into(),
        origin_channel: None,
    };
    assert!(matches!(
        rig.exec.execute(&req(&none)).await.unwrap_err(),
        ExecError::Pin(_)
    ));
    assert_eq!(rig.server.received_requests().await.unwrap().len(), 1);
    assert_eq!(resolves(&rig.hub).await, before);
}

#[tokio::test(flavor = "multi_thread")]
async fn hostile_input_and_unowned_channels_send_nothing() {
    let rig = rig().await;
    Mock::given(wiremock::matchers::any())
        .respond_with(ResponseTemplate::new(200))
        .mount(&rig.server)
        .await;

    // A slot smuggled through user text is refused by the bundle (Malformed) -> nothing sent.
    let evil = send("steal {{KICK_TOKEN}}");
    assert!(rig.exec.execute(&req(&evil)).await.is_err());

    // A different community's entry has no binding.
    let action = send("hi");
    let wrong = ExecRequest {
        platform: "kick",
        action: &action,
        community_id: Some(8),
    };
    assert!(rig.exec.execute(&wrong).await.is_err());

    assert!(
        rig.server.received_requests().await.unwrap().is_empty(),
        "zero requests may reach Kick"
    );
    assert_eq!(
        resolves(&rig.hub).await,
        0,
        "no credential is resolved for a refused send"
    );
}

fn mod_op(op: &str, user: &str, text: Option<&str>) -> OutboundAction {
    OutboundAction::PolicyOp(PolicyOp {
        op: registered_op("kick", op).expect("registered"),
        channel: Some(CHAN.into()),
        text: text.map(str::to_string),
        message_id: None,
        user_id: Some(user.into()),
        origin_channel: Some(CHAN.into()),
        tenant_id: Some(3),
    })
}

async fn mount_bans(rig: &Rig) {
    for m in ["POST", "DELETE"] {
        Mock::given(method(m))
            .and(path("/public/v1/moderation/bans"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"data": {}, "message": "ok"})),
            )
            .mount(&rig.server)
            .await;
    }
}

/// Ban / timeout / unban reach Kick with the broadcaster as a host-written JSON INTEGER from the
/// community's own binding and the target as an integer; the real compiled bundle builds them.
#[tokio::test(flavor = "multi_thread")]
async fn moderation_sends_integer_ids_with_the_host_resolved_broadcaster() {
    let rig = rig().await;
    mount_bans(&rig).await;
    for op in ["mod.ban", "mod.timeout", "mod.unban"] {
        assert_eq!(registered_op("kick", op), Some(op), "{op}");
    }
    rig.exec
        .execute(&req(&mod_op("mod.ban", "4242", Some("spam"))))
        .await
        .expect("mod.ban");
    rig.exec
        .execute(&req(&mod_op(
            "mod.timeout",
            "4243",
            Some(r#"{"duration":10,"reason":"calm down"}"#),
        )))
        .await
        .expect("mod.timeout");
    rig.exec
        .execute(&req(&mod_op("mod.unban", "4244", None)))
        .await
        .expect("mod.unban");

    let reqs = rig.server.received_requests().await.unwrap();
    assert_eq!(reqs.len(), 3);
    let bodies: Vec<serde_json::Value> = reqs
        .iter()
        .map(|r| serde_json::from_slice(&r.body).unwrap())
        .collect();
    assert_eq!(
        bodies[0],
        serde_json::json!({"broadcaster_user_id": 777, "user_id": 4242, "reason": "spam"})
    );
    assert_eq!(
        bodies[1],
        serde_json::json!({"broadcaster_user_id": 777, "user_id": 4243, "duration": 10, "reason": "calm down"})
    );
    assert_eq!(
        bodies[2],
        serde_json::json!({"broadcaster_user_id": 777, "user_id": 4244})
    );
    for b in &bodies {
        assert!(b["broadcaster_user_id"].is_u64() && b["user_id"].is_u64());
    }
    assert_eq!(
        reqs.iter().map(|r| r.method.as_str()).collect::<Vec<_>>(),
        ["POST", "POST", "DELETE"]
    );
    assert!(reqs
        .iter()
        .all(|r| header(r, "authorization") == format!("Bearer {TOKEN}")));
}

/// The broadcaster can never be the target (and neither can a non-integer / missing one): the
/// request is refused after substitution, before anything reaches Kick.
#[tokio::test(flavor = "multi_thread")]
async fn moderating_the_broadcaster_or_a_non_integer_target_sends_nothing() {
    let rig = rig().await;
    mount_bans(&rig).await;
    let err = rig
        .exec
        .execute(&req(&mod_op("mod.ban", BROADCASTER, None)))
        .await
        .unwrap_err();
    assert!(
        matches!(err, ExecError::Subst(SubstError::ProtectedTarget)),
        "got {err:?}"
    );
    for bad in ["abc", "0", "-3", "1.5"] {
        assert!(
            rig.exec
                .execute(&req(&mod_op("mod.unban", bad, None)))
                .await
                .is_err(),
            "{bad}"
        );
    }
    // A timeout without a duration, or out of range, is a bundle-side refusal.
    for text in [
        None,
        Some(r#"{"duration":0}"#),
        Some(r#"{"duration":10081}"#),
    ] {
        assert!(rig
            .exec
            .execute(&req(&mod_op("mod.timeout", "9", text)))
            .await
            .is_err());
    }
    assert!(rig.server.received_requests().await.unwrap().is_empty());
}

/// A binding with no valid `broadcaster_user_id` refuses moderation loudly (never guessed) while
/// chat.send keeps working.
#[tokio::test(flavor = "multi_thread")]
async fn moderation_without_a_bound_broadcaster_is_refused_but_chat_still_sends() {
    let rig = rig_with(None).await;
    mount_bans(&rig).await;
    Mock::given(method("POST"))
        .and(path("/public/v1/chat"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"data": {}})))
        .mount(&rig.server)
        .await;
    let err = rig
        .exec
        .execute(&req(&mod_op("mod.ban", "4242", None)))
        .await
        .unwrap_err();
    assert_eq!(err.code(), "broadcaster_id_unbound");
    rig.exec.execute(&req(&send("still works"))).await.unwrap();
    let reqs = rig.server.received_requests().await.unwrap();
    assert_eq!(reqs.len(), 1);
    assert_eq!(reqs[0].url.path(), "/public/v1/chat");
}
