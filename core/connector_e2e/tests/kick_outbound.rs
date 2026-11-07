//! Real-path e2e of Kick outbound through the generic executor: the REAL compiled Kick bundle
//! builds the request template over the REAL executor wire (`WireTemplateBuilder` ->
//! `ExecutorLink` -> `run_connection` -> `ConnectorHost` -> wasm), the PRODUCTION Kick policy
//! (`PlatformPolicy::kick`), verifier, substituter and `GuardedClient` run unmodified, and the
//! request is asserted on the wire at a local fake Kick endpoint.
//!
//! Doubles (and only these): the per-community slot source (the `KickSlots` seam belongs to the
//! host seams, see `bundles/rust/kick/README.md` "Needs from host seams") and a
//! `TemplateBuilder` decorator that rewrites ONLY the scheme+host of the bundle's template to the
//! local fake after asserting the bundle named the real Kick API host (https cannot be terminated
//! by a local plain-http fake). No Valkey is needed: this drives `SenderExecutor::execute`
//! directly (the Kick queue drain is part of the host seams).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_executor::manifest::WitWorld;
use bundle_host_http::guarded::GuardedLimits;
use connector_e2e::{digest_of, RigConnector, WireRig};
use svc_ingest::connector::sender_exec::policy::registered_op;
use svc_ingest::connector::sender_exec::{
    BuildError, BuiltTemplate, ExecError, ExecRequest, ExecutorConfig, PlatformPolicy,
    SenderAction, SenderExecutor, SlotError, SlotRequest, SlotSource, SlotValue, TemplateBuilder,
    WireTemplateBuilder,
};
use svc_ingest::outbound_boot::CachedFlagGate;
use svc_ingest::outbound_ops::OutboundAction;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const WASM: &[u8] = include_bytes!("fixtures/kick_connector.wasm");
const COMMUNITY: i64 = 7;
const TOKEN: &str = "KICK-OAUTH-SECRET";
const CHAN: &str = "kick-chan";
const MSG_ID: &str = "828f83bb-e391-4c78-9e91-af35a172840e";

/// Test double for the (host-seam) Kick slot source: one community, one token, the pinned
/// channel must be the one it owns. Records every request it was asked to resolve.
/// `(community_id, handles)` of every resolve call.
type AskLog = Arc<Mutex<Vec<(Option<i64>, Vec<String>)>>>;

struct KickSlots {
    asked: AskLog,
}

impl SlotSource for KickSlots {
    async fn resolve(
        &self,
        req: &SlotRequest<'_>,
    ) -> Result<HashMap<String, SlotValue>, SlotError> {
        self.asked.lock().unwrap().push((
            req.community_id,
            req.handles.iter().map(|h| (*h).to_string()).collect(),
        ));
        if req.platform != "kick" || req.community_id != Some(COMMUNITY) {
            return Err(SlotError::Refused("no_binding"));
        }
        if req.handles.iter().any(|h| *h != "token") {
            return Err(SlotError::Refused("unknown_handle"));
        }
        if req.pinned_channel.is_some_and(|c| c != CHAN) {
            return Err(SlotError::Refused("channel_not_owned"));
        }
        Ok(HashMap::from([(
            "token".to_string(),
            SlotValue::new(TOKEN.to_string()),
        )]))
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
            .strip_prefix("https://api.kick.com")
            .unwrap_or_else(|| panic!("bundle did not name api.kick.com: {}", built.tpl.url))
            .to_string();
        built.tpl.url = format!("{}{rest}", self.base);
        Ok(built)
    }
}

type Exec = SenderExecutor<
    Rewrite,
    KickSlots,
    CachedFlagGate,
    svc_ingest::connector::sender_exec::NoDuties,
>;

struct Rig {
    exec: Exec,
    server: MockServer,
    asked: AskLog,
    _wire: WireRig,
}

async fn rig() -> Rig {
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
    let asked = Arc::new(Mutex::new(Vec::new()));
    let exec = SenderExecutor::new(
        Rewrite {
            inner: WireTemplateBuilder::new(Arc::clone(&wire.link), Arc::clone(&wire.registry)),
            base: server.uri(),
        },
        KickSlots {
            asked: Arc::clone(&asked),
        },
        CachedFlagGate(true),
        cfg,
    );
    Rig {
        exec,
        server,
        asked,
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
    // The token was resolved by the host, per send, only under the `token` handle.
    assert_eq!(
        *rig.asked.lock().unwrap(),
        vec![(Some(COMMUNITY), vec!["token".to_string()])]
    );
}

/// `chat.delete` is deliberately unregistered on Kick: the message id is global and the executor's
/// `pin_channel` cannot pin a Kick channel (it ignores `requires_pin` for platforms other than
/// twitch|discord|youtube), so the community token could delete messages in any channel its owner
/// moderates. The executor refuses it (`UnsupportedKind`) and nothing is sent; the bundle still
/// builds the template, so enabling it later is a policy + pin change only.
#[tokio::test(flavor = "multi_thread")]
async fn chat_delete_is_refused_until_the_executor_can_pin_a_kick_channel() {
    let rig = rig().await;
    Mock::given(wiremock::matchers::any())
        .respond_with(ResponseTemplate::new(204))
        .mount(&rig.server)
        .await;
    assert!(registered_op("kick", "chat.delete").is_none());
    let action = OutboundAction::ChatDelete {
        channel: CHAN.into(),
        message_id: MSG_ID.into(),
        origin_channel: Some(CHAN.into()),
    };
    let err = rig.exec.execute(&req(&action)).await.unwrap_err();
    assert!(matches!(err, ExecError::UnsupportedKind), "got {err:?}");
    assert!(rig.server.received_requests().await.unwrap().is_empty());

    let builder = WireTemplateBuilder::new(rig._wire.link.clone(), Arc::clone(&rig._wire.registry));
    let built = builder
        .build_request(
            "kick",
            &SenderAction {
                kind: "chat.delete".into(),
                target_source_id: "src".into(),
                payload_json: format!(r#"{{"message_id":"{MSG_ID}"}}"#),
            },
        )
        .await
        .expect("the bundle builds chat.delete");
    assert_eq!(built.tpl.method, "DELETE");
    assert_eq!(
        built.tpl.url,
        format!("https://api.kick.com/public/v1/chat/{MSG_ID}")
    );
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
}

/// Moderation fails loud at every layer until the host has integer body slots: the policy table
/// never registers the op (parse-time `UnknownOp`) and the bundle itself answers `unsupported`.
#[tokio::test(flavor = "multi_thread")]
async fn moderation_is_refused_loudly_never_sent() {
    for op in ["mod.ban", "mod.timeout", "mod.unban"] {
        assert!(registered_op("kick", op).is_none(), "{op}");
    }
    let rig = rig().await;
    let wire = rig._wire.link.clone();
    let builder = WireTemplateBuilder::new(wire, Arc::clone(&rig._wire.registry));
    let err = builder
        .build_request(
            "kick",
            &SenderAction {
                kind: "mod.ban".into(),
                target_source_id: "src".into(),
                payload_json: r#"{"user_id":"5"}"#.into(),
            },
        )
        .await
        .unwrap_err();
    assert_eq!(err, BuildError::Guest("unsupported"));
    assert!(rig.server.received_requests().await.unwrap().is_empty());
}
