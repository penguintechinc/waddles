//! Behavioural tests of the clips policy against an in-memory host, with every tool call checked
//! against the contract fixture (see `common`).
mod common;

use common::*;
use serde_json::{Value, json};
use waddle_bundle_twitch_clips::logic::*;
use waddle_sdk::tool::ToolError;

fn msgs(o: Output) -> Vec<OutMsg> {
    match o {
        Output::Messages(m) => m,
        other => panic!("expected messages, got {other:?}"),
    }
}

fn texts(o: Output) -> Vec<String> {
    msgs(o).into_iter().map(|m| m.text).collect()
}

fn happy_host() -> FakeHost {
    let h = FakeHost::new();
    h.push_reply(TOOL_CREATE, Ok(json!({"clip_id": "Abc-1_x"})));
    h.push_reply(TOOL_GET, Ok(ready("Abc-1_x")));
    h
}

#[test]
fn parse_command_table() {
    let cases: Vec<(&str, Option<Command>)> = vec![
        ("!clip", Some(Command::Clip { title: None })),
        ("  !CLIP  ", Some(Command::Clip { title: None })),
        (
            "!clip big play",
            Some(Command::Clip {
                title: Some("big play".into()),
            }),
        ),
        ("!clips", Some(Command::Clips { limit: None })),
        ("!clips 3", Some(Command::Clips { limit: Some(3) })),
        ("!clips abc", Some(Command::ClipsUsage)),
        ("!clipper", None),
        ("clip", None),
        ("hello !clip", None),
    ];
    for (t, want) in cases {
        assert_eq!(parse_command(t), want, "{t:?}");
    }
}

#[test]
fn sanitize_title_rules() {
    assert_eq!(
        sanitize_title("  hi\u{0007} there ").unwrap(),
        Some("hi there".into())
    );
    assert_eq!(sanitize_title("   ").unwrap(), None);
    assert!(sanitize_title("look {user:abc}").is_err());
    assert_eq!(
        sanitize_title(&"x".repeat(300))
            .unwrap()
            .unwrap()
            .chars()
            .count(),
        100
    );
}

#[test]
fn happy_path_creates_polls_announces_and_indexes() {
    let h = happy_host();
    let out = handle_event(&h, &mod_chat("!clip big play", "1-0")).unwrap();
    let m = msgs(out);
    assert_eq!(m.len(), 1);
    assert_eq!(
        (m[0].provider.as_str(), m[0].channel.as_str()),
        ("twitch", CHANNEL)
    );
    assert_eq!(m[0].text, "Clip ready: https://clips.twitch.tv/Abc-1_x");
    let calls = h.calls.borrow();
    assert_eq!(calls[0].0, TOOL_CREATE);
    assert_eq!(calls[0].1, json!({"title": "big play"}));
    assert_eq!(calls[0].2.as_deref(), Some("clip-1-0"));
    assert_eq!(h.slept.borrow().as_slice(), &[1000]);
    drop(calls);
    let items = handle_event(&h, &admin("list", json!({}), "{}")).unwrap();
    let Output::Admin(v) = items else { panic!() };
    assert_eq!(v["data"]["items"][0]["clip_id"], "Abc-1_x");
    assert_eq!(v["data"]["pending"], 0);
    assert!(h.kv.borrow().get("clip.lock").is_none(), "lock released");
}

#[test]
fn default_title_applies_when_none_given() {
    let h = happy_host();
    let mut ev = mod_chat("!clip", "2-0");
    ev.config_json = r#"{"default_title":"Stream moment"}"#.into();
    handle_event(&h, &ev).unwrap();
    assert_eq!(
        h.calls_to(TOOL_CREATE)[0],
        json!({"title": "Stream moment"})
    );
}

#[test]
fn no_title_sends_empty_object() {
    let h = happy_host();
    handle_event(&h, &mod_chat("!clip", "3-0")).unwrap();
    assert_eq!(h.calls_to(TOOL_CREATE)[0], json!({}));
}

#[test]
fn mention_title_is_rejected_without_a_tool_call() {
    let h = FakeHost::new();
    let t = texts(handle_event(&h, &mod_chat("!clip hi {user:abc}", "4-0")).unwrap());
    assert_eq!(t, vec!["Clip titles can't contain mentions."]);
    assert!(h.calls.borrow().is_empty());
}

#[test]
fn gating_follows_community_config() {
    let viewer = chat("!clip", "5-0", json!({}));
    let h = FakeHost::new();
    let t = texts(handle_event(&h, &viewer).unwrap());
    assert_eq!(t, vec!["Clips are limited to moderators."]);
    assert!(h.calls.borrow().is_empty());

    let h = happy_host();
    let mut ev = chat("!clip", "5-1", json!({}));
    ev.config_json = r#"{"who_can_clip":"everyone"}"#.into();
    assert!(texts(handle_event(&h, &ev).unwrap())[0].starts_with("Clip ready"));

    for (role, badges, allowed) in [
        (Role::Subscribers, json!({"is_subscriber": true}), true),
        (Role::Vip, json!({"is_subscriber": true}), false),
        (Role::Vip, json!({"is_vip": true}), true),
        (Role::Mods, json!({"is_vip": true}), false),
        (Role::Mods, json!({"is_mod": true}), true),
        (Role::Broadcaster, json!({"is_mod": true}), false),
        (Role::Broadcaster, json!({"is_broadcaster": true}), true),
    ] {
        let p = chat("!clip", "x", badges).payload;
        assert_eq!(role_allows(role, &p), allowed, "{role:?} {p}");
    }
}

#[test]
fn user_and_channel_cooldowns_then_expiry() {
    let h = happy_host();
    handle_event(&h, &mod_chat("!clip", "6-0")).unwrap();

    // Same user: per-user cooldown (60s default).
    let t = texts(handle_event(&h, &mod_chat("!clip", "6-1")).unwrap());
    assert!(t[0].starts_with("You can clip again in "), "{t:?}");
    assert_eq!(h.calls_to(TOOL_CREATE).len(), 1);

    // Different user: channel cooldown (30s default).
    *h.identity.borrow_mut() = Ok("11111111-2222-3333-4444-555555555555".into());
    let t = texts(handle_event(&h, &mod_chat("!clip", "6-2")).unwrap());
    assert!(t[0].starts_with("Clip cooldown: try again in "), "{t:?}");

    // Past both: allowed again.
    h.now.set(h.now.get() + 61_000);
    h.push_reply(TOOL_CREATE, Ok(json!({"clip_id": "Second1"})));
    h.push_reply(TOOL_GET, Ok(ready("Second1")));
    let t = texts(handle_event(&h, &mod_chat("!clip", "6-3")).unwrap());
    assert!(t[0].contains("Second1"));
}

#[test]
fn create_failures_map_to_messages_and_do_not_burn_cooldown() {
    for (err, want) in [
        (
            ToolError::InvalidArgs("not_live: offline".into()),
            "The channel isn't live, so clips can't be created.",
        ),
        (
            ToolError::InvalidArgs("rate_limited".into()),
            "Twitch is rate limiting clips right now; try again shortly.",
        ),
        (
            ToolError::InvalidArgs("not_connected".into()),
            "Twitch isn't connected for this community.",
        ),
        (
            ToolError::InvalidArgs("weird".into()),
            "Twitch rejected the clip request.",
        ),
        (
            ToolError::NotGranted,
            "Clips aren't enabled for this community yet.",
        ),
        (
            ToolError::Timeout,
            "Twitch took too long to answer; try again shortly.",
        ),
        (
            ToolError::TargetUnavailable,
            "Twitch is unavailable right now; try again shortly.",
        ),
    ] {
        let h = FakeHost::new();
        h.push_reply(TOOL_CREATE, Err(err.clone()));
        let t = texts(handle_event(&h, &mod_chat("!clip", "7-0")).unwrap());
        assert_eq!(t, vec![want.to_string()], "{err:?}");
        assert!(h.kv.borrow().get("clip.lock").is_none());
        assert!(
            !h.kv.borrow().keys().any(|k| k.starts_with("clip.cd.")),
            "cooldown burned on failure"
        );
    }
}

#[test]
fn not_ready_polls_with_backoff_then_goes_pending_and_sweep_announces() {
    let h = FakeHost::new();
    h.push_reply(TOOL_CREATE, Ok(json!({"clip_id": "Slow1"})));
    for _ in 0..4 {
        h.push_reply(TOOL_GET, Ok(not_ready()));
    }
    let t = texts(handle_event(&h, &mod_chat("!clip", "8-0")).unwrap());
    assert_eq!(
        t,
        vec!["Clip created, still processing - I'll post the link when it's ready."]
    );
    let slept = h.slept.borrow().clone();
    assert_eq!(slept[..3], [1000, 1500, 2000], "backoff schedule");
    assert!(
        slept.iter().sum::<u64>() <= 6000,
        "poll budget respected: {slept:?}"
    );
    assert!(h.kv.borrow().keys().any(|k| k.starts_with("clip.pend.")));

    // Later the reconcile/sweep trigger finds it ready.
    h.push_reply(TOOL_GET, Ok(ready("Slow1")));
    let rec = EventIn {
        platform: "hub".into(),
        event_type: "bundle.reconcile".into(),
        ..mod_chat("", "r")
    };
    let m = msgs(handle_event(&h, &rec).unwrap());
    assert_eq!(m[0].text, "Clip ready: https://clips.twitch.tv/Slow1");
    assert_eq!(m[0].channel, CHANNEL);
    assert!(
        !h.kv
            .borrow()
            .keys()
            .any(|k| k.starts_with("clip.pend.") && !k.ends_with("seq"))
    );
}

#[test]
fn pending_clip_times_out_explicitly_after_15s() {
    let h = FakeHost::new();
    h.push_reply(TOOL_CREATE, Ok(json!({"clip_id": "Never1"})));
    for _ in 0..4 {
        h.push_reply(TOOL_GET, Ok(not_ready()));
    }
    handle_event(&h, &mod_chat("!clip", "9-0")).unwrap();
    h.now.set(h.now.get() + 16_000);
    h.push_reply(TOOL_GET, Ok(not_ready()));
    let rec = EventIn {
        platform: "hub".into(),
        event_type: "bundle.reconcile".into(),
        ..mod_chat("", "r")
    };
    let t = texts(handle_event(&h, &rec).unwrap());
    assert!(t[0].contains("timed out after 15s"), "{t:?}");
}

#[test]
fn chat_command_sweeps_pending_and_prepends_announcement() {
    let h = FakeHost::new();
    h.push_reply(TOOL_CREATE, Ok(json!({"clip_id": "Slow2"})));
    for _ in 0..4 {
        h.push_reply(TOOL_GET, Ok(not_ready()));
    }
    handle_event(&h, &mod_chat("!clip", "10-0")).unwrap();
    h.push_reply(TOOL_GET, Ok(ready("Slow2")));
    h.push_reply(TOOL_LIST, Ok(json!({"clips": []})));
    let m = msgs(handle_event(&h, &chat("!clips", "10-1", json!({}))).unwrap());
    assert_eq!(m.len(), 2);
    assert!(m[0].text.contains("Slow2"));
    assert_eq!(m[1].text, "No clips yet.");
}

#[test]
fn redelivery_is_dropped_and_concurrent_create_is_refused() {
    let h = happy_host();
    handle_event(&h, &mod_chat("!clip", "11-0")).unwrap();
    assert_eq!(
        handle_event(&h, &mod_chat("!clip", "11-0")).unwrap(),
        Output::None
    );
    assert_eq!(h.calls_to(TOOL_CREATE).len(), 1);

    let h = FakeHost::new();
    h.kv.borrow_mut().insert("clip.lock".into(), b"1".to_vec());
    let t = texts(handle_event(&h, &mod_chat("!clip", "11-1")).unwrap());
    assert_eq!(
        t,
        vec!["A clip is already being created; try again in a moment."]
    );
    assert!(h.calls.borrow().is_empty());
}

#[test]
fn identity_failures_are_explicit() {
    let h = FakeHost::new();
    *h.identity.borrow_mut() = Err(IdentityError::NotLinked);
    assert_eq!(
        texts(handle_event(&h, &mod_chat("!clip", "12-0")).unwrap()),
        vec!["Link your account before creating clips."]
    );
    *h.identity.borrow_mut() = Err(IdentityError::Other("x".into()));
    assert_eq!(
        texts(handle_event(&h, &mod_chat("!clip", "12-1")).unwrap()),
        vec!["Couldn't verify you right now; try again shortly."]
    );
    *h.identity.borrow_mut() = Ok("not a uuid!".into());
    assert_eq!(
        handle_event(&h, &mod_chat("!clip", "12-2"))
            .unwrap_err()
            .code,
        "BAD_IDENTITY"
    );
}

#[test]
fn malformed_tool_responses_fail_loud() {
    let h = FakeHost::new();
    h.push_raw(TOOL_CREATE, Ok(json!({"clip_id": "bad:id"})));
    assert_eq!(
        handle_event(&h, &mod_chat("!clip", "13-0"))
            .unwrap_err()
            .code,
        "BAD_TOOL_RESPONSE"
    );
    assert!(
        h.kv.borrow().get("clip.lock").is_none(),
        "lock released on error"
    );

    let h = FakeHost::new();
    h.push_reply(TOOL_CREATE, Ok(json!({"clip_id": "Okay1"})));
    // Contract-violating URL host must be rejected by the bundle even though the fake would flag it:
    h.push_raw(
        TOOL_GET,
        Ok(json!({"ready": true, "clip": {"clip_id": "Okay1", "url": "https://evil.example/x"}})),
    );
    assert_eq!(
        handle_event(&h, &mod_chat("!clip", "13-1"))
            .unwrap_err()
            .code,
        "BAD_TOOL_RESPONSE"
    );
}

#[test]
fn kv_outage_is_an_error_not_a_silent_pass() {
    let h = FakeHost::new();
    h.fail_kv.set(true);
    assert_eq!(
        handle_event(&h, &mod_chat("!clip", "14-0"))
            .unwrap_err()
            .code,
        "KV_UNAVAILABLE"
    );
}

#[test]
fn non_matching_events_are_ignored_and_missing_channel_fails() {
    let h = FakeHost::new();
    assert_eq!(
        handle_event(&h, &mod_chat("hello", "15-0")).unwrap(),
        Output::None
    );
    let mut ev = mod_chat("!clip", "15-1");
    ev.platform = "discord".into();
    assert_eq!(handle_event(&h, &ev).unwrap(), Output::None);
    let mut ev = mod_chat("!clip", "15-2");
    ev.payload.as_object_mut().unwrap().remove("channel_id");
    assert_eq!(handle_event(&h, &ev).unwrap_err().code, "MISSING_CHANNEL");
}

#[test]
fn invalid_config_replies_misconfigured() {
    let h = FakeHost::new();
    let mut ev = mod_chat("!clip", "16-0");
    ev.config_json = r#"{"list_limit": 99}"#.into();
    assert_eq!(
        texts(handle_event(&h, &ev).unwrap()),
        vec!["Clips are misconfigured; ask a community admin."]
    );
}

#[test]
fn discord_share_posts_to_configured_channel() {
    let h = happy_host();
    let mut ev = mod_chat("!clip", "17-0");
    ev.config_json =
        format!(r#"{{"discord_share_enabled":true,"discord_share_channel_id":"{DISCORD_CH}"}}"#);
    let m = msgs(handle_event(&h, &ev).unwrap());
    assert_eq!(m.len(), 2);
    assert_eq!(
        (m[1].provider.as_str(), m[1].channel.as_str()),
        ("discord", DISCORD_CH)
    );
    assert!(m[1].text.contains("https://clips.twitch.tv/Abc-1_x"));
    // Enabled without a channel is a config error, never a silent no-share.
    assert!(Config::parse(r#"{"discord_share_enabled":true}"#).is_err());
}

#[test]
fn clips_lists_recent_filters_hidden_and_has_cooldown() {
    let h = FakeHost::new();
    h.push_reply(
        TOOL_LIST,
        Ok(json!({"clips": [clip("A1"), clip("B2"), clip("C3")]})),
    );
    h.kv.borrow_mut()
        .insert("clip.hidden.B2".into(), b"1".to_vec());
    let t = texts(handle_event(&h, &chat("!clips 2", "18-0", json!({}))).unwrap());
    assert!(
        t[0].starts_with(
            "Recent clips: 1. Nice https://clips.twitch.tv/A1 | 2. Nice https://clips.twitch.tv/C3"
        ),
        "{t:?}"
    );
    assert_eq!(h.calls_to(TOOL_LIST)[0], json!({"limit": 4}));
    let t = texts(handle_event(&h, &chat("!clips", "18-1", json!({}))).unwrap());
    assert!(t[0].starts_with("Clip list cooldown"), "{t:?}");
    assert_eq!(
        texts(handle_event(&h, &chat("!clips 0", "18-2", json!({}))).unwrap()),
        vec!["Usage: !clips [1-10]"]
    );
    assert_eq!(
        texts(handle_event(&h, &chat("!clips x", "18-3", json!({}))).unwrap()),
        vec!["Usage: !clips [1-10]"]
    );
}

#[test]
fn clips_list_errors_and_role_gate() {
    let h = FakeHost::new();
    h.push_reply(
        TOOL_LIST,
        Err(ToolError::InvalidArgs("not_connected".into())),
    );
    assert_eq!(
        texts(handle_event(&h, &chat("!clips", "19-0", json!({}))).unwrap()),
        vec!["Twitch isn't connected for this community."]
    );
    let mut ev = chat("!clips", "19-1", json!({}));
    ev.config_json = r#"{"who_can_list":"mods"}"#.into();
    assert_eq!(
        texts(handle_event(&h, &ev).unwrap()),
        vec!["Clip listing is limited to moderators."]
    );
    let h = FakeHost::new();
    h.push_raw(TOOL_LIST, Ok(json!({"nope": 1})));
    assert_eq!(
        handle_event(&h, &chat("!clips", "19-2", json!({})))
            .unwrap_err()
            .code,
        "BAD_TOOL_RESPONSE"
    );
}

#[test]
fn admin_list_delete_config_sweep() {
    let h = happy_host();
    handle_event(&h, &mod_chat("!clip", "20-0")).unwrap();

    let Output::Admin(v) =
        handle_event(&h, &admin("delete", json!({"clip_id": "Abc-1_x"}), "{}")).unwrap()
    else {
        panic!()
    };
    assert_eq!(v["data"]["removed_index_entries"], 1);
    assert_eq!(v["data"]["hidden_from_chat"], true);
    let Output::Admin(v) = handle_event(&h, &admin("list", json!({}), "{}")).unwrap() else {
        panic!()
    };
    assert_eq!(v["data"]["items"].as_array().unwrap().len(), 0);

    // deleted clip is hidden from !clips
    h.push_reply(TOOL_LIST, Ok(json!({"clips": [clip("Abc-1_x")]})));
    let t = texts(handle_event(&h, &chat("!clips", "20-1", json!({}))).unwrap());
    assert_eq!(t, vec!["No clips yet."]);

    let Output::Admin(v) = handle_event(
        &h,
        &admin("config", json!({"config": {"list_limit": 50}}), "{}"),
    )
    .unwrap() else {
        panic!()
    };
    assert_eq!(v["data"]["candidate_valid"], false);
    assert_eq!(v["data"]["effective"]["who_can_clip"], "mods");
    let Output::Admin(v) = handle_event(
        &h,
        &admin("config", json!({"config": {"list_limit": 3}}), "{}"),
    )
    .unwrap() else {
        panic!()
    };
    assert_eq!(v["data"]["candidate_valid"], true);

    let Output::Admin(v) = handle_event(&h, &admin("sweep", json!({}), "{}")).unwrap() else {
        panic!()
    };
    assert_eq!(v["data"]["resolved_messages"], json!([]));

    assert_eq!(
        handle_event(&h, &admin("nope", json!({}), "{}"))
            .unwrap_err()
            .code,
        "UNKNOWN_ADMIN_ACTION"
    );
    assert_eq!(
        handle_event(&h, &admin("delete", json!({"clip_id": "bad:id"}), "{}"))
            .unwrap_err()
            .code,
        "BAD_ADMIN"
    );
    let mut no_action = admin("list", json!({}), "{}");
    no_action.payload = json!({});
    assert_eq!(handle_event(&h, &no_action).unwrap_err().code, "BAD_ADMIN");
}

#[test]
fn admin_delete_removes_pending_too() {
    let h = FakeHost::new();
    h.push_reply(TOOL_CREATE, Ok(json!({"clip_id": "Slow3"})));
    for _ in 0..4 {
        h.push_reply(TOOL_GET, Ok(not_ready()));
    }
    handle_event(&h, &mod_chat("!clip", "21-0")).unwrap();
    let Output::Admin(v) = handle_event(&h, &admin("list", json!({}), "{}")).unwrap() else {
        panic!()
    };
    assert_eq!(v["data"]["pending"], 1);
    let Output::Admin(v) =
        handle_event(&h, &admin("delete", json!({"clip_id": "Slow3"}), "{}")).unwrap()
    else {
        panic!()
    };
    assert_eq!(v["data"]["removed_index_entries"], 1);
}

#[test]
fn index_ring_wraps_without_growing() {
    let h = FakeHost::new();
    for i in 0..(RING_SLOTS + 3) {
        let id = format!("Ring{i}");
        h.now.set(h.now.get() + 120_000);
        h.push_reply(TOOL_CREATE, Ok(json!({"clip_id": id})));
        h.push_reply(TOOL_GET, Ok(ready(&id)));
        handle_event(&h, &mod_chat("!clip", &format!("30-{i}"))).unwrap();
    }
    let Output::Admin(v) = handle_event(&h, &admin("list", json!({}), "{}")).unwrap() else {
        panic!()
    };
    let items = v["data"]["items"].as_array().unwrap();
    assert_eq!(items.len() as u64, RING_SLOTS);
    assert_eq!(
        items[0]["clip_id"],
        format!("Ring{}", RING_SLOTS + 2),
        "newest first"
    );
}

#[test]
fn relay_plan_validates_messages() {
    let ok = json!({"messages": [{"provider": "twitch", "channel": "1", "text": "hi"}]});
    assert_eq!(relay_plan(&ok).unwrap().len(), 1);
    assert!(relay_plan(&json!({})).unwrap().is_empty());
    assert!(relay_plan(&json!({"admin_result": {}})).unwrap().is_empty());
    for bad in [
        json!({"messages": [{"provider": "slack", "channel": "1", "text": "hi"}]}),
        json!({"messages": [{"provider": "twitch", "channel": "", "text": "hi"}]}),
        json!({"messages": [{"provider": "twitch", "channel": "1", "text": ""}]}),
        json!({"messages": "nope"}),
    ] {
        assert!(relay_plan(&bad).is_err(), "{bad}");
    }
}

#[test]
fn config_defaults_and_validation() {
    let d = Config::parse("").unwrap();
    assert_eq!(d, Config::default());
    assert_eq!(d.who_can_clip, Role::Mods);
    for bad in [
        r#"{"user_cooldown_secs": 4000}"#,
        r#"{"list_limit": 0}"#,
        r#"{"poll_budget_ms": 100}"#,
        r#"{"announce_template": "no url"}"#,
        r#"{"who_can_clip": "robots"}"#,
        r#"{"default_title": "x\ny"}"#,
        "not json",
    ] {
        assert!(Config::parse(bad).is_err(), "{bad}");
    }
}

#[test]
fn tool_error_text_never_echoes_backend_detail() {
    let (_, t) = tool_error_text(&ToolError::InvalidArgs("not_live: secret-token-abc".into()));
    assert!(!t.contains("secret"));
    let _: Value = json!(null);
}
