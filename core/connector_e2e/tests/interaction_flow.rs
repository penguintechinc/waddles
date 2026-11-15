//! Real-path e2e of the slash-interaction flow against the REAL compiled Discord connector
//! (`discord_connector.wasm`) and a fake Discord API:
//!
//! gateway `INTERACTION_CREATE` -> host `GatewayTap` (token stored in a REAL Valkey, deferred
//! ack POSTed within the deadline) -> bundle-side reply (`interaction.followup`) built by the
//! real connector over the real executor wire -> host fills the application id / token from the
//! store -> followup lands on the fake Discord. Also: reconciler-only command registration
//! verified against the desired state, and cross-guild / expired handles refused.
//!
//! Doubles (and only these): the hub-api credential seam, the tokenizer seam, the hub-api
//! desired-state source, and a `TemplateBuilder` decorator rewriting scheme+host of the
//! connector's template to the local fake after asserting it named discord.com. Needs a real
//! Valkey: `VALKEY_URL` unset FAILS (no silent skip).

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
use svc_ingest::commands_reconcile::{
    canonical_text, BoxFut, Desired, DesiredCommand, DesiredLookup, RegistryError,
};
use svc_ingest::connector::sender_exec::discord_duties::{
    DiscordDuties, PlatformUserResolver, UserResolveError,
};
use svc_ingest::connector::sender_exec::discord_slots::DiscordSlots;
use svc_ingest::connector::sender_exec::{
    BuildError, BuiltTemplate, ExecError, ExecRequest, ExecutorConfig, PlatformPolicy,
    SenderAction, SenderExecutor, SlotValue, TemplateBuilder, WireTemplateBuilder,
};
use svc_ingest::interaction_store::{InteractionRecord, InteractionStore};
use svc_ingest::interaction_tap::{FrameTap, GatewayTap, InteractionAcker, TapVerdict};
use svc_ingest::outbound_boot::CachedFlagGate;
use svc_ingest::outbound_creds::CredError;
use svc_ingest::outbound_creds_discord::{DiscordBinding, DiscordCredentialSource};
use svc_ingest::outbound_ops::{OutboundAction, PolicyOp};
use wiremock::matchers::{body_json, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const DISCORD_WASM: &[u8] =
    include_bytes!("../../bundle_executor/tests/fixtures/discord_connector.wasm");
const COMMUNITY: i64 = 7;
const GUILD: &str = "111";
const APP: &str = "424242";
const BOT: &str = "DC-BOT-SECRET";
const IX_TOKEN: &str = "e2eTOKEN-secret_value.9";

#[derive(Clone)]
struct Creds;
impl DiscordCredentialSource for Creds {
    async fn binding(&self, community_id: Option<i64>) -> Result<DiscordBinding, CredError> {
        if community_id != Some(COMMUNITY) {
            return Err(CredError::NoBinding);
        }
        Ok(DiscordBinding {
            bot_token: SecretString::new(BOT.to_string()),
            guild_id: GUILD.to_string(),
            application_id: Some(APP.to_string()),
            managed_roles: BTreeMap::new(),
        })
    }
}

struct Users;
impl PlatformUserResolver for Users {
    async fn resolve(&self, _t: i64, _c: i64, _u: &str) -> Result<SlotValue, UserResolveError> {
        Err(UserResolveError::NotFound)
    }
}

struct Desire(Desired);
impl DesiredLookup for Desire {
    fn desired<'a>(
        &'a self,
        _g: &'a str,
        _v: Option<&'a str>,
    ) -> BoxFut<'a, Result<Desired, RegistryError>> {
        Box::pin(async move { Ok(self.0.clone()) })
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

type Exec =
    SenderExecutor<Rewrite, DiscordSlots<Creds>, CachedFlagGate, DiscordDuties<Creds, Users>>;

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

fn desired_set() -> Vec<DesiredCommand> {
    vec![
        DesiredCommand {
            name: "context".into(),
            description: "Switch community".into(),
        },
        DesiredCommand {
            name: "ping".into(),
            description: "Ping".into(),
        },
    ]
}

struct Rig {
    _wire: WireRig,
    exec: Exec,
    dc: MockServer,
    store: InteractionStore,
    tap: GatewayTap,
}

async fn rig() -> Rig {
    let url = std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: this e2e needs a REAL Valkey (no silent skip)");
    std::env::set_var("SECURITY_TRANSPORT_TLS", "false");
    std::env::set_var("SECURITY_TRANSPORT_AUTH", "false");
    let conn = redis::Client::open(url)
        .unwrap()
        .get_connection_manager()
        .await
        .unwrap();
    let store = InteractionStore::new(conn);
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
    let cfg = ExecutorConfig::new(
        vec![policy],
        HashMap::from([("discord".to_string(), digest_of(DISCORD_WASM))]),
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
    let api = format!("{}/api/v10", dc.uri());
    let duties = DiscordDuties::with_endpoint(Creds, Users, api.clone(), Duration::from_secs(30))
        .with_interactions(store.clone())
        .with_desired_commands(Arc::new(Desire(Desired {
            version: "v1".into(),
            changed: true,
            commands: desired_set(),
        })));
    let exec = SenderExecutor::with_duties(
        Rewrite {
            inner: WireTemplateBuilder::new(Arc::clone(&wire.link), Arc::clone(&wire.registry)),
            base: dc.uri(),
        },
        DiscordSlots::with_endpoint(Creds, guard(), api.clone()),
        CachedFlagGate(true),
        duties,
        cfg,
    );
    let tap = GatewayTap::new(
        store.clone(),
        Arc::new(InteractionAcker::with_endpoint(guard(), api)),
    );
    Rig {
        _wire: wire,
        exec,
        dc,
        store,
        tap,
    }
}

fn unique(seed: u64) -> String {
    let n = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{seed}{n}")
}

fn gateway_frame(id: &str, guild: &str) -> Vec<u8> {
    serde_json::to_vec(&json!({"op":0,"s":1,"t":"INTERACTION_CREATE","d":{
        "id": id, "application_id": APP, "type": 2, "token": IX_TOKEN, "guild_id": guild,
        "channel_id": "222", "member": {"user": {"id": "444"}, "permissions": "0"},
        "data": {"name": "ping", "options": [{"name":"args","type":3,"value":"x"}]}}}))
    .unwrap()
}

fn op(kind: &'static str, text: Option<String>, handle: Option<&str>) -> OutboundAction {
    OutboundAction::PolicyOp(PolicyOp {
        op: kind,
        channel: None,
        text,
        message_id: handle.map(str::to_string),
        user_id: None,
        origin_channel: Some(format!("guild:{GUILD}")),
        tenant_id: Some(3),
    })
}

async fn run(r: &Rig, a: &OutboundAction) -> Result<(), ExecError> {
    r.exec
        .execute(&ExecRequest {
            platform: "discord",
            action: a,
            community_id: Some(COMMUNITY),
        })
        .await
        .map(|_| ())
}

#[tokio::test(flavor = "multi_thread")]
async fn interaction_is_acked_stored_then_answered_through_the_real_connector() {
    let r = rig().await;
    let id = unique(8);
    Mock::given(method("POST"))
        .and(path(format!(
            "/api/v10/interactions/{id}/{IX_TOKEN}/callback"
        )))
        .and(body_json(json!({"type": 5})))
        .respond_with(ResponseTemplate::new(204))
        .expect(1)
        .mount(&r.dc)
        .await;
    // 1. gateway frame: stored + deferred-acked, then forwarded to the connector.
    assert_eq!(
        r.tap.on_frame("discord", &gateway_frame(&id, GUILD)).await,
        TapVerdict::Pass
    );
    assert_eq!(
        r.tap.on_frame("discord", &gateway_frame(&id, GUILD)).await,
        TapVerdict::Duplicate,
        "redelivery is neither re-acked nor re-routed"
    );
    // Active-context routing resolved the community (host-side); only then may it be answered.
    assert!(r.store.set_routed(&id, COMMUNITY).await.unwrap());
    // 2. the bundle replies with only the handle + a logical reply (no token anywhere).
    Mock::given(method("POST"))
        .and(path(format!("/api/v10/webhooks/{APP}/{IX_TOKEN}")))
        .and(body_json(json!({
            "content": "pong", "flags": 64, "allowed_mentions": {"parse": ["users"]}
        })))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": "1"})))
        .expect(1)
        .mount(&r.dc)
        .await;
    let reply = json!({"content": "pong", "ephemeral": true}).to_string();
    run(&r, &op("interaction.followup", Some(reply), Some(&id)))
        .await
        .unwrap();
    for req in r.dc.received_requests().await.unwrap() {
        assert!(
            req.headers.get("authorization").is_none(),
            "interaction replies carry no bot credential"
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn another_guilds_or_an_expired_interaction_cannot_be_answered() {
    let r = rig().await;
    let foreign = unique(7);
    assert!(r
        .store
        .put_new(
            &foreign,
            &InteractionRecord {
                application_id: APP.into(),
                token: IX_TOKEN.into(),
                guild_id: Some("222000".into()),
                routed_community_id: Some(COMMUNITY),
                invoker_user_id: None,
            }
        )
        .await
        .unwrap());
    let reply = json!({"content": "x"}).to_string();
    for (handle, code) in [
        (foreign.as_str(), "interaction_not_owned"),
        ("999000000000000001", "interaction_expired"),
    ] {
        let e = run(
            &r,
            &op("interaction.followup", Some(reply.clone()), Some(handle)),
        )
        .await
        .unwrap_err();
        assert_eq!(e.code(), code);
    }
    assert!(r.dc.received_requests().await.unwrap().is_empty());
}

#[tokio::test(flavor = "multi_thread")]
async fn command_registration_is_reconciler_only_and_exactly_the_desired_set() {
    let r = rig().await;
    Mock::given(method("PUT"))
        .and(path(format!(
            "/api/v10/applications/{APP}/guilds/{GUILD}/commands"
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([])))
        .expect(1)
        .mount(&r.dc)
        .await;
    // Anything but the desired set is refused before the connector or Discord is touched.
    let mut tampered = desired_set();
    tampered.push(DesiredCommand {
        name: "evil".into(),
        description: "x".into(),
    });
    let e = run(
        &r,
        &op(
            "commands.register.guild",
            Some(canonical_text(&tampered)),
            None,
        ),
    )
    .await
    .unwrap_err();
    assert_eq!(e.code(), "commands_mismatch");
    assert!(r.dc.received_requests().await.unwrap().is_empty());
    // The reconciler's payload (exactly the desired set) registers through the real connector.
    run(
        &r,
        &op(
            "commands.register.guild",
            Some(canonical_text(&desired_set())),
            None,
        ),
    )
    .await
    .unwrap();
    let reqs = r.dc.received_requests().await.unwrap();
    assert_eq!(reqs.len(), 1);
    assert_eq!(
        reqs[0].headers.get("authorization").unwrap(),
        &format!("Bot {BOT}")
    );
    let body: Value = serde_json::from_slice(&reqs[0].body).unwrap();
    let names: Vec<&str> = body
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, ["context", "ping"]);
}
