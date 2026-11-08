#![allow(clippy::unwrap_used, clippy::expect_used)]
//! Real-path tests for the interaction token store duties and the reconciler-only
//! `commands.register.guild` gate: the production executor / verifier / substituter /
//! `GuardedClient` / `DiscordSlots` / `DiscordDuties` against a local fake Discord and a
//! real Valkey (`VALKEY_URL`). Only the component loader, the credential seam, the tokenizer
//! and the hub-api desired-state source are doubles.

use super::*;
use crate::commands_reconcile::{
    canonical_text, BoxFut, Desired, DesiredCommand, DesiredLookup, RegistryError,
};
use crate::interaction_store::{InteractionRecord, InteractionStore};
use crate::outbound_ops::PolicyOp;

const IX_ID: &str = "888000000000000001";
const IX_TOKEN: &str = "ixTOKEN-secret_value.123";

type IxExec = SenderExecutor<Tpl, DiscordSlots<Creds>, Flag, DiscordDuties<Creds, Users>>;

struct FixedDesired(Desired);
impl DesiredLookup for FixedDesired {
    fn desired<'a>(
        &'a self,
        _guild: &'a str,
        if_version: Option<&'a str>,
    ) -> BoxFut<'a, Result<Desired, RegistryError>> {
        assert!(if_version.is_none(), "verification fetches the full set");
        Box::pin(async move { Ok(self.0.clone()) })
    }
}

async fn valkey() -> redis::aio::MultiplexedConnection {
    let url = std::env::var("VALKEY_URL").expect("VALKEY_URL (a local valkey) is required");
    redis::Client::open(url)
        .unwrap()
        .get_multiplexed_async_connection()
        .await
        .unwrap()
}

fn unique(prefix: u64) -> String {
    let n = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}{n}")
}

fn op(kind: &'static str, text: Option<&str>, handle: Option<&str>) -> OutboundAction {
    OutboundAction::PolicyOp(PolicyOp {
        op: kind,
        channel: None,
        text: text.map(str::to_string),
        message_id: handle.map(str::to_string),
        user_id: None,
        origin_channel: Some(format!("guild:{GUILD}")),
        tenant_id: Some(9),
    })
}

fn followup_tpl(base: &str) -> RequestTpl {
    RequestTpl {
        method: "POST".into(),
        url: format!(
            "{base}/api/v10/webhooks/{{interaction.application_id}}/{{interaction.token}}"
        ),
        headers: vec![("Content-Type".into(), "application/json".into())],
        body: Some(
            serde_json::to_vec(&json!({"content":"later","allowed_mentions":{"parse":["users"]}}))
                .unwrap(),
        ),
        secret_refs: vec![],
    }
}

fn callback_tpl(base: &str) -> RequestTpl {
    RequestTpl {
        method: "POST".into(),
        url: format!(
            "{base}/api/v10/interactions/{{interaction.id}}/{{interaction.token}}/callback"
        ),
        headers: vec![("Content-Type".into(), "application/json".into())],
        body: Some(serde_json::to_vec(&json!({"type":5})).unwrap()),
        secret_refs: vec![],
    }
}

fn register_tpl(base: &str, body: serde_json::Value) -> RequestTpl {
    RequestTpl {
        method: "PUT".into(),
        url: format!(
            "{base}/api/v10/applications/{{app.application_id}}/guilds/{{target.guild_id}}/commands"
        ),
        headers: vec![("Content-Type".into(), "application/json".into())],
        body: Some(serde_json::to_vec(&body).unwrap()),
        secret_refs: vec![
            ("Authorization".into(), "discord-bot".into()),
            ("{app.application_id}".into(), "app.application_id".into()),
        ],
    }
}

struct IxRig {
    server: MockServer,
    exec: IxExec,
    tpl: Tpl,
    store: InteractionStore,
}

async fn ix_rig(
    tpl: impl FnOnce(&str) -> RequestTpl,
    with_store: bool,
    desired: Option<Desired>,
) -> IxRig {
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
    let creds = Creds::new();
    let store = InteractionStore::new(valkey().await);
    let mut duties = DiscordDuties::with_endpoint(
        creds.clone(),
        Users::new(),
        api.clone(),
        Duration::from_secs(30),
    );
    if with_store {
        duties = duties.with_interactions(store.clone());
    }
    if let Some(d) = desired {
        duties = duties.with_desired_commands(Arc::new(FixedDesired(d)));
    }
    let exec = SenderExecutor::with_duties(
        tpl.clone(),
        DiscordSlots::with_endpoint(creds, guard(), api),
        Flag(true),
        duties,
        cfg,
    );
    IxRig {
        server,
        exec,
        tpl,
        store,
    }
}

fn rec(guild: Option<&str>) -> InteractionRecord {
    InteractionRecord {
        application_id: APP_ID.to_string(),
        token: IX_TOKEN.to_string(),
        guild_id: guild.map(str::to_string),
        // Routed to community 1 (the rig's primary community).
        routed_community_id: Some(1),
    }
}

async fn mock_post(s: &MockServer, p: String) {
    Mock::given(method("POST"))
        .and(path(p))
        .respond_with(ResponseTemplate::new(204))
        .mount(s)
        .await;
}

#[tokio::test]
async fn followup_and_callback_resolve_the_token_host_side_and_never_hand_it_to_the_bundle() {
    for (kind, tpl, want_path) in [
        (
            "interaction.followup",
            followup_tpl as fn(&str) -> RequestTpl,
            format!("/api/v10/webhooks/{APP_ID}/{IX_TOKEN}"),
        ),
        (
            "interaction.callback",
            callback_tpl,
            String::new(), // filled below (id is unique per run)
        ),
    ] {
        let r = ix_rig(tpl, true, None).await;
        let id = unique(8);
        assert!(r.store.put_new(&id, &rec(Some(GUILD))).await.unwrap());
        let want_path = if want_path.is_empty() {
            format!("/api/v10/interactions/{id}/{IX_TOKEN}/callback")
        } else {
            want_path
        };
        mock_post(&r.server, want_path.clone()).await;
        r.exec
            .execute(&rq(&op(kind, Some("{\"content\":\"x\"}"), Some(&id)), 1))
            .await
            .unwrap();
        let reqs = r.server.received_requests().await.unwrap();
        assert_eq!(reqs.len(), 1, "{kind}: no lookups, one reply");
        assert_eq!(reqs[0].url.path(), want_path, "{kind}");
        assert!(
            reqs[0].headers.get("authorization").is_none(),
            "{kind}: the URL is the credential; no bot token is sent"
        );
        // The connector (bundle) saw the handle and the reply, never the token.
        for c in r.tpl.calls.lock().unwrap().iter() {
            assert!(!c.payload_json.contains(IX_TOKEN), "{kind}");
            assert!(!c.payload_json.contains("token"), "{kind}");
            assert!(c.payload_json.contains(&id), "{kind}");
        }
    }
}

#[tokio::test]
async fn an_interaction_of_another_guild_or_a_dm_or_an_expired_one_is_refused_before_any_request() {
    let r = ix_rig(followup_tpl, true, None).await;
    let foreign = unique(7);
    let dm = unique(6);
    let missing = unique(5);
    r.store
        .put_new(&foreign, &rec(Some("222000")))
        .await
        .unwrap();
    r.store.put_new(&dm, &rec(None)).await.unwrap();
    for (handle, code) in [
        (foreign.as_str(), "interaction_not_owned"),
        (dm.as_str(), "interaction_not_owned"),
        (missing.as_str(), "interaction_expired"),
        ("not-a-snowflake", "interaction_handle_invalid"),
    ] {
        let before = refusal_count("interaction.followup", code);
        let e = r
            .exec
            .execute(&rq(
                &op("interaction.followup", Some("{}"), Some(handle)),
                1,
            ))
            .await
            .unwrap_err();
        assert_eq!(e, ExecError::Slot(SlotError::Refused(code)), "{handle}");
        assert!(e.is_policy_refusal());
        assert!(refusal_count("interaction.followup", code) > before);
    }
    // No handle at all.
    let e = r
        .exec
        .execute(&rq(&op("interaction.followup", Some("{}"), None), 1))
        .await
        .unwrap_err();
    assert_eq!(
        e,
        ExecError::Slot(SlotError::Refused("interaction_handle_invalid"))
    );
    assert_eq!(r.tpl.calls(), 0, "refused before the connector ran");
    assert!(seen(&r.server).await.is_empty());
}

#[tokio::test]
async fn community_two_cannot_answer_community_ones_interaction() {
    let r = ix_rig(followup_tpl, true, None).await;
    let id = unique(4);
    r.store.put_new(&id, &rec(Some(GUILD))).await.unwrap();
    // Community 2's binding is guild 222000: the guild-111 interaction is not its own.
    let e = r
        .exec
        .execute(&rq(&op("interaction.followup", Some("{}"), Some(&id)), 2))
        .await
        .unwrap_err();
    assert_eq!(
        e,
        ExecError::Slot(SlotError::Refused("interaction_not_owned"))
    );
    assert!(seen(&r.server).await.is_empty());
}

#[tokio::test]
async fn same_guild_other_community_other_application_or_unrouted_is_refused() {
    let r = ix_rig(followup_tpl, true, None).await;
    let routed_elsewhere = unique(3);
    let other_app = unique(2);
    let unrouted = unique(1);
    let owner = unique(9);
    let mut a = rec(Some(GUILD));
    a.routed_community_id = Some(2);
    let mut b = rec(Some(GUILD));
    b.application_id = "999".into();
    let mut c = rec(Some(GUILD));
    c.routed_community_id = None;
    for (id, rc) in [
        (&routed_elsewhere, a),
        (&other_app, b),
        (&unrouted, c),
        (&owner, rec(Some(GUILD))),
    ] {
        assert!(r.store.put_new(id, &rc).await.unwrap());
    }
    mock_post(&r.server, format!("/api/v10/webhooks/{APP_ID}/{IX_TOKEN}")).await;
    for id in [&routed_elsewhere, &other_app, &unrouted] {
        let e = r
            .exec
            .execute(&rq(&op("interaction.followup", Some("{}"), Some(id)), 1))
            .await
            .unwrap_err();
        assert_eq!(
            e,
            ExecError::Slot(SlotError::Refused("interaction_not_owned"))
        );
    }
    assert!(seen(&r.server).await.is_empty());
    // The owner (routed to community 1, same application) is answered.
    r.exec
        .execute(&rq(
            &op("interaction.followup", Some("{}"), Some(&owner)),
            1,
        ))
        .await
        .unwrap();
    // set_routed after the fact makes an unrouted record answerable by exactly that community.
    assert!(r.store.set_routed(&unrouted, 1).await.unwrap());
    r.exec
        .execute(&rq(
            &op("interaction.followup", Some("{}"), Some(&unrouted)),
            1,
        ))
        .await
        .unwrap();
}

struct FixedView(crate::active_context::ContextView);
impl crate::active_context::ContextSource for FixedView {
    fn get<'a>(
        &'a self,
        _s: &'a str,
        _u: &'a str,
    ) -> BoxFut<'a, Result<crate::active_context::ContextView, crate::active_context::ContextError>>
    {
        Box::pin(async move { Ok(self.0.clone()) })
    }
    fn put<'a>(
        &'a self,
        _s: &'a str,
        _u: &'a str,
        _c: &'a str,
    ) -> BoxFut<'a, Result<crate::active_context::ContextView, crate::active_context::ContextError>>
    {
        Box::pin(async move { Ok(self.0.clone()) })
    }
}

#[derive(Clone, Default)]
struct Null;
impl crate::connector::sink::EventSink for Null {
    async fn publish(
        &self,
        _c: &crate::connector::types::ConnectionCtx,
        _e: crate::connector::types::ConnectorEvent,
    ) -> Result<(), crate::connector::sink::SinkError> {
        Ok(())
    }
}

/// Routes `id` through the production `ContextSink` (hub view = `community`, sink bound to
/// `app`), against the rig's real store.
async fn route_via_sink(r: &IxRig, id: &str, community: i64, app: &str) {
    use crate::active_context::{
        register_routing_metrics, ContextSink, ContextView, InteractionReplier,
    };
    use crate::connector::sink::EventSink;
    let view = ContextView {
        community_public_id: "11111111-1111-4111-8111-111111111111".into(),
        community_id: community,
        community_name: "Alpha".into(),
        explicit: false,
        communities: vec![],
    };
    let replier = Arc::new(InteractionReplier::new(
        r.store.clone(),
        guard(),
        format!("{}/api/v10", r.server.uri()),
    ));
    let sink = ContextSink::new(
        Null,
        Arc::new(FixedView(view)),
        replier,
        r.store.clone(),
        app.to_string(),
        register_routing_metrics(&prometheus::Registry::new()).unwrap(),
    );
    let ctx = crate::connector::types::ConnectionCtx {
        platform: "discord".into(),
        source_id: GUILD.into(),
        connection_id: "c".into(),
        session_token: None,
    };
    let ev = crate::connector::types::ConnectorEvent {
        platform: "discord".into(),
        event_type: "interaction.command".into(),
        actor: Some("123e4567-e89b-12d3-a456-426614174000".into()),
        payload_json: json!({"interaction_id": id, "command": "ping", "args": ""}).to_string(),
        occurred_at: "t".into(),
    };
    sink.publish(&ctx, ev).await.unwrap();
}

#[tokio::test]
async fn context_sink_routing_makes_the_owner_answerable_and_only_the_owner() {
    let r = ix_rig(followup_tpl, true, None).await;
    let owned = unique(1);
    let unrouted = unique(2);
    let wrong_comm = unique(3);
    let wrong_app = unique(4);
    let mut fresh = rec(Some(GUILD));
    fresh.routed_community_id = None;
    for id in [&owned, &unrouted, &wrong_comm, &wrong_app] {
        assert!(r.store.put_new(id, &fresh).await.unwrap());
    }
    // A refused routing also posts the generic notice through the followup webhook.
    mock_post(&r.server, format!("/api/v10/webhooks/{APP_ID}/{IX_TOKEN}")).await;
    route_via_sink(&r, &owned, 1, APP_ID).await;
    route_via_sink(&r, &wrong_comm, 2, APP_ID).await;
    route_via_sink(&r, &wrong_app, 1, "999").await;
    for id in [&unrouted, &wrong_comm, &wrong_app] {
        let e = r
            .exec
            .execute(&rq(&op("interaction.followup", Some("{}"), Some(id)), 1))
            .await
            .unwrap_err();
        assert_eq!(
            e,
            ExecError::Slot(SlotError::Refused("interaction_not_owned")),
            "{id}"
        );
    }
    r.exec
        .execute(&rq(
            &op("interaction.followup", Some("{}"), Some(&owned)),
            1,
        ))
        .await
        .unwrap();
}

#[tokio::test]
async fn without_a_configured_store_interaction_ops_fail_loud() {
    let r = ix_rig(followup_tpl, false, None).await;
    let e = r
        .exec
        .execute(&rq(&op("interaction.followup", Some("{}"), Some(IX_ID)), 1))
        .await
        .unwrap_err();
    assert_eq!(
        e,
        ExecError::Slot(SlotError::Refused("interaction_store_unconfigured"))
    );
}

fn cmd(n: &str, d: &str) -> DesiredCommand {
    DesiredCommand {
        name: n.into(),
        description: d.into(),
    }
}

fn desired_set() -> Vec<DesiredCommand> {
    vec![cmd("context", "Switch community"), cmd("ping", "Ping")]
}

fn desired(changed: bool) -> Desired {
    Desired {
        version: "v1".into(),
        changed,
        commands: desired_set(),
    }
}

fn reg_body() -> serde_json::Value {
    json!([{"name":"context","description":"Switch community"},{"name":"ping","description":"Ping"}])
}

#[tokio::test]
async fn the_reconciler_path_registers_exactly_the_desired_set_in_the_binding_guild() {
    let r = ix_rig(|b| register_tpl(b, reg_body()), false, Some(desired(true))).await;
    Mock::given(method("PUT"))
        .and(path(format!(
            "/api/v10/applications/{APP_ID}/guilds/{GUILD}/commands"
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([])))
        .mount(&r.server)
        .await;
    r.exec
        .execute(&rq(
            &op(
                "commands.register.guild",
                Some(&canonical_text(&desired_set())),
                None,
            ),
            1,
        ))
        .await
        .unwrap();
    let reqs = r.server.received_requests().await.unwrap();
    assert_eq!(reqs.len(), 1);
    assert_eq!(
        reqs[0].headers.get("authorization").unwrap(),
        &format!("Bot {TOKEN}")
    );
    // The connector payload carries ONLY the commands array (no guild/app id).
    let calls = r.tpl.calls.lock().unwrap().clone();
    let p: serde_json::Value = serde_json::from_str(&calls[0].payload_json).unwrap();
    assert_eq!(p.as_object().unwrap().len(), 1);
    assert_eq!(p["commands"].as_array().unwrap().len(), 2);
}

#[tokio::test]
async fn a_payload_that_is_not_the_desired_version_is_rejected_before_the_connector() {
    let r = ix_rig(|b| register_tpl(b, reg_body()), false, Some(desired(true))).await;
    let wipe = canonical_text(&[]);
    let extra = canonical_text(&[
        cmd("context", "Switch community"),
        cmd("ping", "Ping"),
        cmd("evil", "x"),
    ]);
    let swapped = canonical_text(&[cmd("context", "Switch community"), cmd("ping", "Pong")]);
    for text in [
        Some(wipe),
        Some(extra),
        Some(swapped),
        Some("nope".to_string()),
        None,
    ] {
        let before = refusal_count("commands.register.guild", "commands_mismatch");
        let e = r
            .exec
            .execute(&rq(
                &op("commands.register.guild", text.as_deref(), None),
                1,
            ))
            .await
            .unwrap_err();
        assert_eq!(
            e,
            ExecError::Slot(SlotError::Refused("commands_mismatch")),
            "{text:?}"
        );
        assert!(refusal_count("commands.register.guild", "commands_mismatch") > before);
    }
    assert_eq!(r.tpl.calls(), 0);
    assert!(seen(&r.server).await.is_empty());
}

#[tokio::test]
async fn an_unchanged_desired_answer_authorizes_no_write_and_no_lookup_means_refused() {
    let text = canonical_text(&desired_set());
    let r = ix_rig(|b| register_tpl(b, reg_body()), false, Some(desired(false))).await;
    let e = r
        .exec
        .execute(&rq(&op("commands.register.guild", Some(&text), None), 1))
        .await
        .unwrap_err();
    assert_eq!(e, ExecError::Slot(SlotError::Refused("commands_mismatch")));
    let r = ix_rig(|b| register_tpl(b, reg_body()), false, None).await;
    let e = r
        .exec
        .execute(&rq(&op("commands.register.guild", Some(&text), None), 1))
        .await
        .unwrap_err();
    assert_eq!(
        e,
        ExecError::Slot(SlotError::Refused("registry_unconfigured"))
    );
    assert!(seen(&r.server).await.is_empty());
}
