//! Tests of the action-stage floor-ban emission (`floor_ban::emit_floor_ban`): through the REAL
//! stage handler (service grant, drain readiness, throttle, host-side detokenize, protected-target
//! check, `pin_sig` signing) down to the signed `mod.ban` envelope on the outbound queue, and the
//! confirmed-ack rule (only the sender's ack is success).

use serde_json::Value;

use super::super::super::tests::{test_pin_keys, FakeRelayQueue};
use super::super::super::{outbound_relay_queue_key, InvokeScope, StageCapabilities};
use super::super::super::{FloorBanCommand, FloorSendError, FloorSendFailure};
use super::{build, mod_tools, rig_for, Rig, COMMUNITY_ID, TENANT_ID, UUID_A, UUID_B, UUID_C};
use crate::pin_sig::PinFields;

fn cmd(user: &str) -> FloorBanCommand {
    FloorBanCommand {
        id: 42,
        tenant_id: TENANT_ID,
        community_id: COMMUNITY_ID,
        user: user.parse().unwrap(),
        platform: "discord".to_string(),
        attempt: 2,
        unban: false,
    }
}

fn caps_with(rig: Rig, queue: FakeRelayQueue) -> StageCapabilities<FakeRelayQueue> {
    build(rig, mod_tools("discord", "mod.ban"), queue)
        .with_discord_ack_timeout(std::time::Duration::from_millis(60))
}

fn failure(r: Result<String, FloorSendFailure>) -> FloorSendError {
    r.expect_err("the delivery must fail").error
}

/// The fixture envelope of one floor ban, with the volatile fields masked, as the sender's
/// real-path e2e (`core/connector_e2e/tests/floor_ban_outbound.rs`) consumes it.
const GOLDEN: &str = include_str!("../../tests/fixtures/floor_ban_mod_ban.json");

#[tokio::test]
async fn a_leased_row_becomes_a_signed_mod_ban_for_the_communitys_guild_and_the_user_token() {
    let c = caps_with(rig_for("discord"), FakeRelayQueue::with_live_drain());
    let op_id = c.emit_floor_ban(&cmd(UUID_B)).await.expect("confirmed");

    let pushed = c.relay_queue.pushed.lock().unwrap().clone();
    assert_eq!(pushed.len(), 1, "exactly one outbound command");
    assert_eq!(pushed[0].0, outbound_relay_queue_key("discord"));
    let env: Value = serde_json::from_str(&pushed[0].1).unwrap();
    assert_eq!(env["op"], "mod.ban");
    assert_eq!(env["platform"], "discord");
    assert_eq!(env["community_id"], i64::from(COMMUNITY_ID));
    assert_eq!(env["tenant_id"], i64::from(TENANT_ID));
    // The guild is pinned from the community's binding by the sender; the target is the
    // tokenized user placeholder -- the raw platform id never rides the wire.
    assert_eq!(env["origin_channel"], "guild:-");
    assert_eq!(env["user_id"], format!("{{user:{UUID_B}}}"));
    assert!(
        !pushed[0].1.contains("999000111"),
        "the raw platform id must never be queued"
    );
    // The op id carries the outbox row id and attempt (the correlation) and is the one acked.
    assert_eq!(env["op_id"], op_id);
    assert!(op_id.starts_with("fb-42-2-"), "{op_id}");
    assert!(
        op_id.len() <= 64
            && op_id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-')
    );
    // The MAC binds op, ids, user and deadline: the sender's verifier accepts it.
    let sig = env["pin_sig"].as_str().expect("signed");
    let fields = PinFields {
        platform: "discord",
        op: "mod.ban",
        channel: "",
        origin_channel: "guild:-",
        message_id: "",
        text: "",
        user_id: &format!("{{user:{UUID_B}}}"),
        op_id: &op_id,
        exp_ms: env["exp_ms"].as_u64().unwrap(),
        community_id: i64::from(COMMUNITY_ID),
        tenant_id: i64::from(TENANT_ID),
    };
    assert_eq!(test_pin_keys().verify(&fields, sig), Ok(()));
    let other_user = PinFields {
        user_id: &format!("{{user:{UUID_A}}}"),
        ..fields
    };
    assert!(test_pin_keys().verify(&other_user, sig).is_err());
}

#[tokio::test]
async fn the_emitted_envelope_matches_the_golden_fixture_the_sender_e2e_consumes() {
    let c = caps_with(rig_for("discord"), FakeRelayQueue::with_live_drain());
    c.emit_floor_ban(&cmd(UUID_B)).await.expect("confirmed");
    let mut env: Value = serde_json::from_str(&c.relay_queue.pushed.lock().unwrap()[0].1).unwrap();
    for volatile in ["op_id", "exp_ms", "pin_sig"] {
        assert!(env.get(volatile).is_some(), "{volatile}");
        env.as_object_mut().unwrap().remove(volatile);
    }
    let golden: Value = serde_json::from_str(GOLDEN).unwrap();
    assert_eq!(
        env, golden,
        "the producer's envelope drifted from tests/fixtures/floor_ban_mod_ban.json (the sender \
         e2e would then test a shape the producer no longer emits)"
    );
}

#[tokio::test]
async fn only_the_senders_confirmed_ack_is_success() {
    // No ack at all: the command was queued, but silence is NOT success.
    let queue = FakeRelayQueue {
        ready: true,
        ..FakeRelayQueue::default()
    };
    let c = caps_with(rig_for("discord"), queue);
    let f = c.emit_floor_ban(&cmd(UUID_B)).await.unwrap_err();
    assert_eq!(f.error, FloorSendError::Retry("relay_unconfirmed".into()));
    assert!(
        f.op_id.is_some(),
        "the minted op id is reported for correlation"
    );
    assert_eq!(c.relay_queue.pushed.lock().unwrap().len(), 1);

    // A sender refusal is a typed retry, never success.
    let c = caps_with(
        rig_for("discord"),
        FakeRelayQueue::with_drain_result(r#"{"ok":false,"code":"quota_exhausted"}"#),
    );
    assert_eq!(
        failure(c.emit_floor_ban(&cmd(UUID_B)).await),
        FloorSendError::Retry("quota_exhausted".into())
    );
}

#[tokio::test]
async fn a_protected_target_or_an_unlinked_user_is_closed_for_good_and_nothing_is_queued() {
    // UUID_C maps to platform id 600 whose member role is community-admin.
    let c = caps_with(rig_for("discord"), FakeRelayQueue::with_live_drain());
    assert_eq!(
        failure(c.emit_floor_ban(&cmd(UUID_C)).await),
        FloorSendError::Permanent("protected_target".into())
    );
    // A user with no linked platform account.
    let unlinked = "11111111-2222-4333-8444-555555555555";
    assert_eq!(
        failure(c.emit_floor_ban(&cmd(unlinked)).await),
        FloorSendError::Permanent("no_linked_account".into())
    );
    assert!(c.relay_queue.pushed.lock().unwrap().is_empty());
}

#[tokio::test]
async fn every_unready_dependency_is_a_retry_that_queues_nothing() {
    // Drain not running.
    let c = caps_with(rig_for("discord"), FakeRelayQueue::default());
    assert_eq!(
        failure(c.emit_floor_ban(&cmd(UUID_B)).await),
        FloorSendError::Retry("relay_unavailable".into())
    );
    // The platform service bundle lacks its moderation grant.
    let rig = Rig {
        svc_grants: vec![],
        ..rig_for("discord")
    };
    let c = caps_with(rig, FakeRelayQueue::with_live_drain());
    assert_eq!(
        failure(c.emit_floor_ban(&cmd(UUID_B)).await),
        FloorSendError::Retry("scope_denied".into())
    );
    // The service is not active in the community's tenant scope.
    let c = caps_with(rig_for("discord"), FakeRelayQueue::with_live_drain());
    let mut elsewhere = cmd(UUID_B);
    elsewhere.tenant_id = 99;
    assert_eq!(
        failure(c.emit_floor_ban(&elsewhere).await),
        FloorSendError::Retry("service_unavailable".into())
    );
    // An outbound drain outage on push.
    let mut queue = FakeRelayQueue::with_live_drain();
    queue.fail = true;
    let c = caps_with(rig_for("discord"), queue);
    assert_eq!(
        failure(c.emit_floor_ban(&cmd(UUID_B)).await),
        FloorSendError::Retry("relay_unavailable".into())
    );
    assert!(c.relay_queue.pushed.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_platform_with_no_outbound_ban_path_is_closed_for_good() {
    let c = caps_with(rig_for("discord"), FakeRelayQueue::with_live_drain());
    let mut unsupported = cmd(UUID_B);
    unsupported.platform = "youtube".to_string();
    assert_eq!(
        failure(c.emit_floor_ban(&unsupported).await),
        FloorSendError::Permanent("platform_not_supported".into())
    );
    let mut bad_scope = cmd(UUID_B);
    bad_scope.community_id = 0;
    assert_eq!(
        failure(c.emit_floor_ban(&bad_scope).await),
        FloorSendError::Permanent("invalid_scope".into())
    );
    assert!(c.relay_queue.pushed.lock().unwrap().is_empty());
}

/// `(tenant, community, platform, user, calling app)` of one platform-originated lift.
type LiftCall = (i32, i32, String, uuid::Uuid, String);

/// Records every platform-originated lift the hook asks for.
#[derive(Default)]
struct FakeUnbanSync {
    calls: std::sync::Mutex<Vec<LiftCall>>,
    fail: bool,
}

impl crate::floor_ban_dispatch::UnbanSync for FakeUnbanSync {
    fn lift_from_platform<'a>(
        &'a self,
        tenant_id: i32,
        community_id: i32,
        platform: &'a str,
        user: uuid::Uuid,
        source_app: &'a str,
    ) -> crate::floor_ban_dispatch::StoreFuture<'a, (i32, i32)> {
        Box::pin(async move {
            self.calls.lock().unwrap().push((
                tenant_id,
                community_id,
                platform.to_string(),
                user,
                source_app.to_string(),
            ));
            if self.fail {
                Err("db down".to_string())
            } else {
                Ok((1, 0))
            }
        })
    }
}

#[tokio::test]
async fn an_unban_row_becomes_a_signed_mod_unban_with_its_own_op_id_prefix() {
    let c = build(
        rig_for("discord"),
        mod_tools("discord", "mod.unban"),
        FakeRelayQueue::with_live_drain(),
    )
    .with_discord_ack_timeout(std::time::Duration::from_millis(60));
    let mut unban = cmd(UUID_B);
    unban.unban = true;
    let op_id = c.emit_floor_ban(&unban).await.expect("confirmed");
    let pushed = c.relay_queue.pushed.lock().unwrap().clone();
    assert_eq!(pushed.len(), 1, "exactly one outbound command");
    let env: Value = serde_json::from_str(&pushed[0].1).unwrap();
    assert_eq!(env["op"], "mod.unban");
    assert_eq!(env["user_id"], format!("{{user:{UUID_B}}}"));
    assert!(op_id.starts_with("fu-42-2-"), "{op_id}");
}

fn caller_scope(app_id: &str) -> InvokeScope {
    InvokeScope {
        tenant: TENANT_ID.to_string(),
        community: Some(COMMUNITY_ID.to_string()),
        app_id: app_id.to_string(),
        tenant_id: TENANT_ID,
        community_id: COMMUNITY_ID,
        app_version: 1,
        ..super::super::super::tests::scope()
    }
}

const CHATGUARD: &str = "penguintechinc.waddles.chatguard";

#[tokio::test]
async fn an_allowlisted_core_bundles_confirmed_unban_lifts_only_its_own_tenant_and_community() {
    let c = caps_with(rig_for("discord"), FakeRelayQueue::with_live_drain());
    let sync = std::sync::Arc::new(FakeUnbanSync::default());
    c.set_unban_sync(sync.clone());
    c.sync_confirmed_unban(&caller_scope(CHATGUARD), "discord", UUID_B)
        .await;
    assert_eq!(
        sync.calls.lock().unwrap().clone(),
        vec![(
            TENANT_ID,
            COMMUNITY_ID,
            "discord".to_string(),
            UUID_B.parse().unwrap(),
            CHATGUARD.to_string()
        )],
        "tenant and community come from the invoke scope, the calling app is recorded"
    );
    assert!(
        c.relay_queue.pushed.lock().unwrap().is_empty(),
        "a sync-originated lift must not send a platform unban back"
    );
    // A malformed user token never reaches the store.
    c.sync_confirmed_unban(&caller_scope(CHATGUARD), "discord", "not-a-uuid")
        .await;
    assert_eq!(sync.calls.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn a_vendor_bundles_confirmed_unban_lifts_nothing_in_waddles() {
    let c = caps_with(rig_for("discord"), FakeRelayQueue::with_live_drain());
    let sync = std::sync::Arc::new(FakeUnbanSync::default());
    c.set_unban_sync(sync.clone());
    // A third-party (non-core) bundle, and a core-namespace bundle that is NOT allowlisted: the
    // platform unban already went out, but neither may lift a Waddles ban without the permission.
    for app in [
        "acme.vendor.modbot",
        "penguintechinc.waddles.fixture-test-app-caller",
    ] {
        assert!(!c.may_sync_lift(&caller_scope(app)), "{app}");
        c.sync_confirmed_unban(&caller_scope(app), "discord", UUID_B)
            .await;
    }
    assert!(sync.calls.lock().unwrap().is_empty(), "nothing was lifted");
}

#[tokio::test]
async fn a_bundle_holding_the_per_action_permission_may_lift() {
    let mut c = caps_with(rig_for("discord"), FakeRelayQueue::with_live_drain());
    c.gate = super::gate(&[(
        "acme.vendor.modbot",
        1,
        vec!["reputation.floor_ban.lift".to_string()],
    )]);
    let sync = std::sync::Arc::new(FakeUnbanSync::default());
    c.set_unban_sync(sync.clone());
    assert!(c.may_sync_lift(&caller_scope("acme.vendor.modbot")));
    c.sync_confirmed_unban(&caller_scope("acme.vendor.modbot"), "discord", UUID_B)
        .await;
    assert_eq!(sync.calls.lock().unwrap().len(), 1);
    // The grant is per app: another vendor bundle in the same community still lifts nothing.
    assert!(!c.may_sync_lift(&caller_scope("acme.vendor.other")));
}

#[tokio::test]
async fn a_failed_platform_lift_is_logged_loudly_and_never_panics() {
    let c = caps_with(rig_for("discord"), FakeRelayQueue::with_live_drain());
    c.set_unban_sync(std::sync::Arc::new(FakeUnbanSync {
        fail: true,
        ..FakeUnbanSync::default()
    }));
    c.sync_confirmed_unban(&caller_scope(CHATGUARD), "discord", UUID_B)
        .await;
}

#[tokio::test]
async fn without_an_installed_sync_a_confirmed_unban_changes_nothing() {
    let c = caps_with(rig_for("discord"), FakeRelayQueue::with_live_drain());
    c.sync_confirmed_unban(&caller_scope(CHATGUARD), "discord", UUID_B)
        .await;
}

fn twitch_caps(op: &str, queue: FakeRelayQueue) -> StageCapabilities<FakeRelayQueue> {
    build(rig_for("twitch"), mod_tools("twitch", op), queue)
        .with_discord_ack_timeout(std::time::Duration::from_millis(60))
}

fn twitch_cmd(user: &str, unban: bool) -> FloorBanCommand {
    FloorBanCommand {
        platform: "twitch".to_string(),
        unban,
        ..cmd(user)
    }
}

#[tokio::test]
async fn a_twitch_floor_ban_names_no_channel_and_carries_the_resolved_id_and_a_fixed_reason() {
    let c = twitch_caps("mod.ban", FakeRelayQueue::with_live_drain());
    let op_id = c
        .emit_floor_ban(&twitch_cmd(UUID_B, false))
        .await
        .expect("confirmed");
    let pushed = c.relay_queue.pushed.lock().unwrap().clone();
    assert_eq!(pushed.len(), 1);
    assert_eq!(pushed[0].0, outbound_relay_queue_key("twitch"));
    let env: Value = serde_json::from_str(&pushed[0].1).unwrap();
    assert_eq!(env["op"], "mod.ban");
    assert_eq!(env["platform"], "twitch");
    // The channel is NEVER named by the producer: the consumer pins it from the DB binding.
    assert!(env.get("channel").is_none(), "{env}");
    assert_eq!(env["origin_channel"], "channel:-");
    // Helix addresses the user by numeric id, carried only inside the signed envelope.
    assert_eq!(env["user_id"], "999000111");
    let text = env["text"].as_str().expect("fixed reason text");
    assert_eq!(
        serde_json::from_str::<Value>(text).unwrap(),
        serde_json::json!({"reason": "Waddles reputation floor"})
    );
    assert!(op_id.starts_with("fb-42-2-"), "{op_id}");
    // The MAC binds the scope, the user id and the reason text.
    let sig = env["pin_sig"].as_str().expect("signed");
    let fields = PinFields {
        platform: "twitch",
        op: "mod.ban",
        channel: "",
        origin_channel: "channel:-",
        message_id: "",
        text,
        user_id: "999000111",
        op_id: &op_id,
        exp_ms: env["exp_ms"].as_u64().unwrap(),
        community_id: i64::from(COMMUNITY_ID),
        tenant_id: i64::from(TENANT_ID),
    };
    assert_eq!(test_pin_keys().verify(&fields, sig), Ok(()));
    let other_scope = PinFields {
        origin_channel: "#someoneelse",
        ..fields
    };
    assert!(test_pin_keys().verify(&other_scope, sig).is_err());
}

#[tokio::test]
async fn a_twitch_unban_carries_no_text_and_does_not_need_the_discord_drain_flag() {
    // Twitch has no readiness key: only the ack matters.
    let mut queue = FakeRelayQueue::with_live_drain();
    queue.ready = false;
    let c = twitch_caps("mod.unban", queue);
    let op_id = c
        .emit_floor_ban(&twitch_cmd(UUID_B, true))
        .await
        .expect("confirmed");
    let env = &c.relay_queue.envelopes()[0];
    assert_eq!(env["op"], "mod.unban");
    assert_eq!(env["origin_channel"], "channel:-");
    assert_eq!(env["user_id"], "999000111");
    assert!(env.get("text").is_none() && env.get("channel").is_none());
    assert!(op_id.starts_with("fu-42-2-"), "{op_id}");
}

#[tokio::test]
async fn a_twitch_ban_of_a_protected_target_is_closed_and_a_failed_ack_retries() {
    let c = twitch_caps("mod.ban", FakeRelayQueue::with_live_drain());
    assert_eq!(
        failure(c.emit_floor_ban(&twitch_cmd(UUID_C, false)).await),
        FloorSendError::Permanent("protected_target".into())
    );
    assert!(c.relay_queue.pushed.lock().unwrap().is_empty());
    let c = twitch_caps(
        "mod.ban",
        FakeRelayQueue::with_drain_result(r#"{"ok":false,"code":"quota_exhausted"}"#),
    );
    assert_eq!(
        failure(c.emit_floor_ban(&twitch_cmd(UUID_B, false)).await),
        FloorSendError::Retry("quota_exhausted".into())
    );
}
