//! Event-sync producer tests: request authentication, validation, single use,
//! throttle, drain readiness, the signed outbound envelope, and ack mapping.

use std::sync::Arc;

use super::super::tests::{caps, test_pin_keys, FakeRelayQueue};
use super::*;
use crate::pin_sig::PinKeys;

const REQ_KEYS: &str = "r1:a0a1a2a3a4a5a6a7a8a9aaabacadaeaf";
const GUILD: &str = "555000111";

fn req_keys() -> PinKeys {
    PinKeys::parse(REQ_KEYS, Some("r1")).unwrap()
}

fn caps_with_keys(q: FakeRelayQueue) -> StageCapabilities<FakeRelayQueue> {
    caps(q)
        .with_event_sync_request_keys(PinKeys::parse(REQ_KEYS, None).unwrap())
        .with_discord_ack_timeout(Duration::from_millis(200))
}

fn fields_json() -> String {
    r#"{"name":"Movie night","description":"snacks","scheduled_start_time":"2026-12-01T19:00:00+00:00","scheduled_end_time":"2026-12-01T21:00:00+00:00","location":"Online"}"#.to_string()
}

struct Spec {
    request_id: String,
    action: &'static str,
    event_id: Option<&'static str>,
    event_json: Option<String>,
    exp_ms: u64,
    guild: &'static str,
    community: i64,
    tenant: i64,
}

impl Spec {
    fn create(id: &str) -> Self {
        Self {
            request_id: id.to_string(),
            action: "create",
            event_id: None,
            event_json: Some(fields_json()),
            exp_ms: now_epoch_ms() + 60_000,
            guild: GUILD,
            community: 7,
            tenant: 3,
        }
    }
    fn with(mut self, action: &'static str, id: Option<&'static str>, json: bool) -> Self {
        self.action = action;
        self.event_id = id;
        self.event_json = json.then(fields_json);
        self
    }
    fn raw(&self) -> String {
        let r = Request {
            v: 1,
            request_id: self.request_id.clone(),
            tenant_id: self.tenant,
            community_id: self.community,
            action: self.action.to_string(),
            discord_event_id: self.event_id.map(str::to_string),
            expect_guild_id: self.guild.to_string(),
            event_json: self.event_json.clone(),
            exp_ms: self.exp_ms,
            sig: String::new(),
        };
        let sig = req_keys().sign(&mac_fields(&r)).unwrap();
        serde_json::json!({
            "v": 1, "request_id": r.request_id, "tenant_id": r.tenant_id,
            "community_id": r.community_id, "action": r.action,
            "discord_event_id": r.discord_event_id, "expect_guild_id": r.expect_guild_id,
            "event_json": r.event_json, "exp_ms": r.exp_ms, "sig": sig,
        })
        .to_string()
    }
}

async fn run(c: &StageCapabilities<FakeRelayQueue>, raw: &str) -> Option<EventSyncOutcome> {
    c.handle_event_sync_request(raw).await.map(|(_, o)| o)
}

fn refused(code: &str) -> Option<EventSyncOutcome> {
    Some(EventSyncOutcome::Refused(code.to_string()))
}

#[tokio::test]
async fn create_queues_a_signed_envelope_and_returns_the_new_event_id() {
    let c = caps_with_keys(FakeRelayQueue::with_drain_result(
        r#"{"ok":true,"id":"987654321"}"#,
    ));
    let out = run(&c, &Spec::create("req-1").raw()).await;
    assert_eq!(
        out,
        Some(EventSyncOutcome::Done {
            id: Some("987654321".into())
        })
    );
    let envs = c.relay_queue.envelopes();
    assert_eq!(envs.len(), 1);
    let e = &envs[0];
    assert_eq!(e["op"], "event.create");
    assert_eq!(e["platform"], "discord");
    assert_eq!(e["origin_channel"], GUILD);
    assert_eq!(
        (e["community_id"].as_i64(), e["tenant_id"].as_i64()),
        (Some(7), Some(3))
    );
    assert!(
        e.get("message_id").is_none() && e.get("channel").is_none() && e.get("user_id").is_none()
    );
    let text: EventFields = serde_json::from_str(e["text"].as_str().unwrap()).unwrap();
    assert_eq!(text.name, "Movie night");
    // The drain's verifier accepts the stamp: MAC covers every producer field.
    let sig = e["pin_sig"].as_str().unwrap();
    let f = |k: &str| e.get(k).and_then(|v| v.as_str()).unwrap_or("");
    let fields = PinFields {
        platform: "discord",
        op: "event.create",
        channel: "",
        origin_channel: f("origin_channel"),
        message_id: "",
        text: f("text"),
        user_id: "",
        op_id: f("op_id"),
        exp_ms: e["exp_ms"].as_u64().unwrap(),
        community_id: 7,
        tenant_id: 3,
    };
    assert_eq!(test_pin_keys().verify(&fields, sig), Ok(()));
    let tampered = PinFields {
        community_id: 8,
        ..fields
    };
    assert!(test_pin_keys().verify(&tampered, sig).is_err());
}

#[tokio::test]
async fn update_cancel_delete_carry_the_event_id_and_only_update_carries_fields() {
    for (action, op, json) in [
        ("update", "event.update", true),
        ("cancel", "event.cancel", false),
        ("delete", "event.delete", false),
    ] {
        let c = caps_with_keys(FakeRelayQueue::with_live_drain());
        let out = run(
            &c,
            &Spec::create("req-u").with(action, Some("4242"), json).raw(),
        )
        .await;
        assert_eq!(out, Some(EventSyncOutcome::Done { id: None }), "{action}");
        let e = &c.relay_queue.envelopes()[0];
        assert_eq!(e["op"], op);
        assert_eq!(e["message_id"], "4242");
        assert_eq!(e.get("text").is_some(), json, "{action}");
    }
}

#[tokio::test]
async fn unauthentic_requests_are_dropped_unanswered_and_queue_nothing() {
    let c = caps_with_keys(FakeRelayQueue::with_live_drain());
    let good = Spec::create("req-a").raw();
    let mut v: serde_json::Value = serde_json::from_str(&good).unwrap();
    let tamper = |k: &str, val: serde_json::Value| {
        let mut t = v.clone();
        t[k] = val;
        t.to_string()
    };
    let cases = vec![
        "not json".to_string(),
        "{}".to_string(),
        "x".repeat(MAX_REQUEST_BYTES + 1),
        tamper("community_id", 8.into()),
        tamper("tenant_id", 4.into()),
        tamper("expect_guild_id", "999".into()),
        tamper("event_json", r#"{"name":"x"}"#.into()),
        tamper("action", "delete".into()),
        tamper("sig", "r1:00".into()),
        tamper("sig", "nokid:00".into()),
        tamper("request_id", "../evil".into()),
        tamper("extra", 1.into()),
    ];
    v["sig"] = "".into();
    for raw in cases {
        assert_eq!(
            c.handle_event_sync_request(&raw).await,
            None,
            "{}",
            &raw[..raw.len().min(60)]
        );
    }
    // Wrong key ring (outbound ring) -> unauthentic.
    let outbound_ring =
        caps(FakeRelayQueue::with_live_drain()).with_event_sync_request_keys(test_pin_keys());
    assert_eq!(outbound_ring.handle_event_sync_request(&good).await, None);
    // No ring configured -> dropped, never executed.
    let none = caps(FakeRelayQueue::with_live_drain());
    assert_eq!(none.handle_event_sync_request(&good).await, None);
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn shape_and_freshness_violations_are_refused_after_authentication() {
    let now = now_epoch_ms();
    let mut cases: Vec<(&str, Spec)> = Vec::new();
    let mut s = Spec::create("r-exp");
    s.exp_ms = now - 1;
    cases.push(("expired", s));
    let mut s = Spec::create("r-far");
    s.exp_ms = now + REQUEST_MAX_AHEAD_MS + 60_000;
    cases.push(("bad_request", s));
    cases.push((
        "bad_request",
        Spec::create("r-evid").with("create", Some("1"), true),
    ));
    cases.push((
        "bad_request",
        Spec::create("r-noid").with("update", None, true),
    ));
    cases.push((
        "bad_request",
        Spec::create("r-badid").with("update", Some("../1"), true),
    ));
    cases.push((
        "bad_request",
        Spec::create("r-nofields").with("create", None, false),
    ));
    cases.push((
        "bad_request",
        Spec::create("r-cancelfields").with("cancel", Some("1"), true),
    ));
    cases.push((
        "bad_request",
        Spec::create("r-action").with("explode", None, true),
    ));
    let mut s = Spec::create("r-guild");
    s.guild = "not-a-guild";
    cases.push(("bad_request", s));
    let mut s = Spec::create("r-com");
    s.community = 0;
    cases.push(("bad_request", s));
    let mut s = Spec::create("r-ten");
    s.tenant = 0;
    cases.push(("bad_request", s));
    let mut s = Spec::create("r-json");
    s.event_json = Some("[]".into());
    cases.push(("bad_request", s));
    let mut s = Spec::create("r-unk");
    s.event_json = Some(r#"{"name":"a","scheduled_start_time":"x","scheduled_end_time":"y","location":"l","guild_id":"9"}"#.into());
    cases.push(("bad_request", s));
    let mut s = Spec::create("r-name");
    s.event_json = Some(fields_json().replace("Movie night", &"n".repeat(101)));
    cases.push(("bad_event_name", s));
    let mut s = Spec::create("r-loc");
    s.event_json = Some(fields_json().replace("Online", " "));
    cases.push(("bad_event_location", s));
    let mut s = Spec::create("r-desc");
    s.event_json = Some(fields_json().replace("snacks", &"d".repeat(1001)));
    cases.push(("bad_event_description", s));
    let mut s = Spec::create("r-time");
    s.event_json = Some(fields_json().replace("2026-12-01T19:00:00+00:00", ""));
    cases.push(("bad_event_time", s));
    for (code, spec) in cases {
        let c = caps_with_keys(FakeRelayQueue::with_live_drain());
        assert_eq!(
            run(&c, &spec.raw()).await,
            refused(code),
            "{}",
            spec.request_id
        );
        assert!(c.relay_queue.envelopes().is_empty(), "{}", spec.request_id);
    }
}

#[tokio::test]
async fn a_request_id_is_single_use() {
    let c = caps_with_keys(FakeRelayQueue::with_live_drain());
    let raw = Spec::create("once").raw();
    assert!(matches!(
        run(&c, &raw).await,
        Some(EventSyncOutcome::Done { .. })
    ));
    assert_eq!(run(&c, &raw).await, refused("replayed"));
    assert_eq!(c.relay_queue.envelopes().len(), 1);
}

#[tokio::test]
async fn each_community_is_throttled_and_limiter_outage_fails_closed() {
    let c = caps_with_keys(FakeRelayQueue::with_live_drain());
    for i in 0..COMMUNITY_RATE_LIMIT {
        let out = run(&c, &Spec::create(&format!("rl-{i}")).raw()).await;
        assert!(matches!(out, Some(EventSyncOutcome::Done { .. })), "{i}");
    }
    assert_eq!(
        run(&c, &Spec::create("rl-over").raw()).await,
        refused("rate_limited")
    );
    // Another community has its own budget.
    let mut other = Spec::create("rl-other");
    other.community = 8;
    assert!(matches!(
        run(&c, &other.raw()).await,
        Some(EventSyncOutcome::Done { .. })
    ));

    let mut q = FakeRelayQueue::with_live_drain();
    q.fail_incr = true;
    let c = caps_with_keys(q);
    assert_eq!(
        run(&c, &Spec::create("inc").raw()).await,
        refused("relay_unavailable")
    );
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn a_missing_or_unreadable_drain_refuses_without_queueing() {
    let c = caps_with_keys(FakeRelayQueue::default());
    assert_eq!(
        run(&c, &Spec::create("nd").raw()).await,
        refused("relay_unavailable")
    );
    let mut q = FakeRelayQueue::with_live_drain();
    q.fail_key_exists = true;
    let c = caps_with_keys(q);
    assert_eq!(
        run(&c, &Spec::create("nd2").raw()).await,
        refused("relay_unavailable")
    );
    let mut q = FakeRelayQueue::with_live_drain();
    q.fail = true;
    let c = caps_with_keys(q);
    assert_eq!(
        run(&c, &Spec::create("nd3").raw()).await,
        refused("relay_unavailable")
    );
    // No outbound signing key -> refused, never queued unsigned.
    let unsigned = StageCapabilities::<_, redis::aio::MultiplexedConnection>::new(
        FakeRelayQueue::with_live_drain(),
        super::super::tests::test_egress(),
        Arc::new(std::sync::Mutex::new(crate::usage::UsageBatcher::new())),
        super::super::tests::permissive_gate(),
    )
    .with_event_sync_request_keys(PinKeys::parse(REQ_KEYS, None).unwrap());
    assert_eq!(
        unsigned
            .handle_event_sync_request(&Spec::create("ns").raw())
            .await
            .map(|(_, o)| o),
        refused("relay_unavailable")
    );
}

#[tokio::test]
async fn drain_results_map_to_closed_outcomes() {
    for (ack, want) in [
        (
            r#"{"ok":false,"code":"guild_mismatch"}"#,
            refused("guild_mismatch"),
        ),
        (r#"{"ok":false,"code":"Evil Code!"}"#, refused("failed")),
        (r#"{"ok":false}"#, refused("failed")),
        (
            r#"{"ok":true,"id":"../x"}"#,
            Some(EventSyncOutcome::Done { id: None }),
        ),
        (r#"{"nope":1}"#, refused("unconfirmed")),
        ("garbage", refused("unconfirmed")),
    ] {
        let c = caps_with_keys(FakeRelayQueue::with_drain_result(ack));
        assert_eq!(run(&c, &Spec::create("ack").raw()).await, want, "{ack}");
    }
    // Ready drain that never answers -> not confirmed (treat as NOT done).
    let q = FakeRelayQueue {
        ready: true,
        ..FakeRelayQueue::default()
    };
    let c = caps_with_keys(q);
    assert_eq!(
        run(&c, &Spec::create("silent").raw()).await,
        refused("not_confirmed")
    );
    let mut q = FakeRelayQueue::with_live_drain();
    q.fail_take = true;
    let c = caps_with_keys(q);
    assert_eq!(
        run(&c, &Spec::create("take").raw()).await,
        refused("relay_unavailable")
    );
}

#[test]
fn result_json_shapes_are_fixed() {
    assert_eq!(
        EventSyncOutcome::Done { id: None }.to_json(),
        r#"{"ok":true}"#
    );
    assert_eq!(
        EventSyncOutcome::Done {
            id: Some("1".into())
        }
        .to_json(),
        r#"{"ok":true,"id":"1"}"#
    );
    assert_eq!(
        EventSyncOutcome::Refused("rate_limited".into()).to_json(),
        r#"{"ok":false,"code":"rate_limited"}"#
    );
    assert!(is_safe_request_id("a-1") && !is_safe_request_id("") && !is_safe_request_id("a b"));
    assert!(!is_safe_request_id(&"a".repeat(65)));
    assert_eq!(
        EVENT_SYNC_REQUEST_QUEUE,
        "waddles:event-sync:discord:requests"
    );
    assert_eq!(
        EVENT_SYNC_RESULT_PREFIX,
        "waddles:event-sync:discord:result:"
    );
}

// ---- discord.scheduled_event.* tools (host-executed, binding-pinned) ----

use super::super::tests::scope;
use bundle_tool_call::ToolRefusal;
use serde_json::json;

fn tool_args(extra: serde_json::Value) -> serde_json::Value {
    let mut v = json!({
        "name": "Movie night", "scheduled_start_time": "2026-12-01T19:00:00Z",
        "scheduled_end_time": "2026-12-01T20:00:00Z", "location": "Online",
    });
    for (k, val) in extra.as_object().unwrap() {
        v[k] = val.clone();
    }
    v
}

#[tokio::test]
async fn tool_create_queues_a_binding_pinned_envelope_and_returns_the_event_id() {
    let c = caps(FakeRelayQueue::with_drain_result(
        r#"{"ok":true,"id":"4242"}"#,
    ));
    let out = c
        .run_discord_event_tool(
            &scope(),
            "discord.scheduled_event.create",
            &tool_args(json!({})),
        )
        .await
        .unwrap()
        .unwrap();
    assert_eq!(out, json!({"ok": true, "event_id": "4242"}));
    let e = &c.relay_queue.envelopes()[0];
    assert_eq!(e["op"], "event.create");
    // community/tenant come from the verified scope; no guild is asserted or named.
    assert_eq!(
        (e["community_id"].as_i64(), e["tenant_id"].as_i64()),
        (Some(3), Some(7))
    );
    assert_eq!(e["origin_channel"], NO_GUILD_ASSERTION);
    assert!(e["pin_sig"].as_str().is_some());
}

#[tokio::test]
async fn tool_update_cancel_delete_carry_the_event_id() {
    for (tool, args, op) in [
        (
            "update",
            tool_args(json!({"event_id": "55"})),
            "event.update",
        ),
        ("cancel", json!({"event_id": "55"}), "event.cancel"),
        ("delete", json!({"event_id": "55"}), "event.delete"),
    ] {
        let c = caps(FakeRelayQueue::with_live_drain());
        let out = c
            .run_discord_event_tool(&scope(), &format!("discord.scheduled_event.{tool}"), &args)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(out["ok"], true);
        let e = &c.relay_queue.envelopes()[0];
        assert_eq!(
            (e["op"].as_str(), e["message_id"].as_str()),
            (Some(op), Some("55"))
        );
    }
}

#[tokio::test]
async fn tool_arguments_can_never_name_a_guild_channel_or_extra_field() {
    let c = caps(FakeRelayQueue::with_live_drain());
    let cases = vec![
        ("create", tool_args(json!({"guild_id": "9"}))),
        ("create", tool_args(json!({"channel_id": "9"}))),
        ("create", tool_args(json!({"event_id": "9"}))),
        ("update", tool_args(json!({}))),
        ("update", tool_args(json!({"event_id": "../x"}))),
        ("cancel", json!({"event_id": "5", "guild_id": "9"})),
        ("cancel", json!({"event_id": "5", "name": "x"})),
        ("delete", json!({})),
        ("create", json!({"name": "x"})),
        ("create", json!([])),
    ];
    for (t, a) in cases {
        let r = c
            .run_discord_event_tool(&scope(), &format!("discord.scheduled_event.{t}"), &a)
            .await
            .unwrap();
        assert!(matches!(r, Err(ToolRefusal::InvalidArgs(_))), "{t} {a}");
    }
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn tool_path_refuses_unscoped_invokes_other_tools_and_maps_refusals() {
    let c = caps(FakeRelayQueue::with_live_drain());
    let mut tenantless = scope();
    tenantless.tenant_id = 0;
    let r = c
        .run_discord_event_tool(
            &tenantless,
            "discord.scheduled_event.create",
            &tool_args(json!({})),
        )
        .await
        .unwrap();
    assert!(matches!(r, Err(ToolRefusal::Forbidden(_))));
    // Not an event tool -> not handled here (the caller does the normal invoke).
    for t in [
        "classify_frame",
        "discord.scheduled_event.explode",
        "discord.scheduled_event.",
    ] {
        assert!(
            c.run_discord_event_tool(&scope(), t, &json!({}))
                .await
                .is_none(),
            "{t}"
        );
    }
    // Drain refusals map onto the framework's taxonomy.
    let c = caps(FakeRelayQueue::with_drain_result(
        r#"{"ok":false,"code":"guild_mismatch"}"#,
    ));
    let r = c
        .run_discord_event_tool(
            &scope(),
            "discord.scheduled_event.create",
            &tool_args(json!({})),
        )
        .await
        .unwrap();
    assert!(matches!(r, Err(ToolRefusal::Forbidden(_))));
    let c = caps(FakeRelayQueue::default());
    let r = c
        .run_discord_event_tool(
            &scope(),
            "discord.scheduled_event.create",
            &tool_args(json!({})),
        )
        .await
        .unwrap();
    assert!(matches!(r, Err(ToolRefusal::TargetUnavailable(_))));
    for (code, want_invalid) in [
        ("bad_event_name", true),
        ("build_refused", true),
        ("x", false),
    ] {
        assert_eq!(
            matches!(refusal_for(code), ToolRefusal::InvalidArgs(_)),
            want_invalid,
            "{code}"
        );
    }
}

/// Cross-language golden: hub-api's Python signer (`event_sync_connector_client.sign_request`)
/// produces this exact MAC for these fields; the same vector is asserted in
/// `hub_api/tests/test_event_sync_connector_client.py`.
#[test]
fn request_mac_golden_vector_matches_the_python_signer() {
    let r = Request {
        v: 1,
        request_id: "req-1".into(),
        tenant_id: 3,
        community_id: 7,
        action: "update".into(),
        discord_event_id: Some("4242".into()),
        expect_guild_id: "555000111".into(),
        event_json: Some(r#"{"name":"n"}"#.into()),
        exp_ms: 1_700_000_000_000,
        sig: String::new(),
    };
    assert_eq!(
        req_keys().sign(&mac_fields(&r)).unwrap(),
        "r1:14780dbf17556ab585cf5a61f7ae172b67055b06c8d0923e912b4f8eeb52e7ea"
    );
}
