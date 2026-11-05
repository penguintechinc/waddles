//! Poll plan, quota/backoff classification, discovery, tools and chat-send template.

use serde_json::{Value, json};
use waddle_bundle_youtube_service::logic::{
    LogicError, build_chat_send, classify_error, describe_403_reason, poll_plan, run_tool,
};

#[test]
fn poll_plan_honors_token_interval_and_floor() {
    let p = poll_plan(&json!({"nextPageToken": "T", "pollingIntervalMillis": 7000})).unwrap();
    assert_eq!(
        (p.next_page_token.as_deref(), p.interval_ms),
        (Some("T"), 7000)
    );
    // floor: never a tight loop
    assert_eq!(
        poll_plan(&json!({"pollingIntervalMillis": 100}))
            .unwrap()
            .interval_ms,
        2000
    );
    assert_eq!(
        poll_plan(&json!({"pollingIntervalMillis": -5}))
            .unwrap()
            .interval_ms,
        2000
    );
    assert_eq!(
        poll_plan(&json!({"pollingIntervalMillis": 3500.9}))
            .unwrap()
            .interval_ms,
        3500
    );
    // default when absent / wrong type; empty token is no token
    let p = poll_plan(&json!({"pollingIntervalMillis": "x", "nextPageToken": ""})).unwrap();
    assert_eq!((p.next_page_token, p.interval_ms), (None, 5000));
}

#[test]
fn reason_extraction_falls_back_like_python() {
    assert_eq!(
        describe_403_reason(&json!({"error": {"errors": [{"reason": "quotaExceeded"}]}})),
        "quotaExceeded"
    );
    assert_eq!(
        describe_403_reason(&json!({"error": {"errors": [], "status": "PERMISSION_DENIED"}})),
        "PERMISSION_DENIED"
    );
    assert_eq!(
        describe_403_reason(&json!({"error": {"message": "nope"}})),
        "nope"
    );
    assert_eq!(describe_403_reason(&json!({"error": {}})), "forbidden");
    assert_eq!(describe_403_reason(&json!({"error": 7})), "forbidden");
    assert_eq!(describe_403_reason(&Value::Null), "forbidden");
}

#[test]
fn quota_backs_off_then_stops_at_ceiling() {
    let q = json!({"error": {"errors": [{"reason": "quotaExceeded"}]}});
    let v = classify_error(403, &q, 0, 30.0, 5);
    assert_eq!(
        (v.class, v.action, v.backoff_s, v.consecutive_quota_errors),
        ("quota", "backoff", 30.0, 1)
    );
    let v = classify_error(403, &q, 3, 30.0, 5);
    assert_eq!((v.action, v.consecutive_quota_errors), ("backoff", 4));
    let v = classify_error(403, &q, 4, 30.0, 5);
    assert_eq!((v.action, v.consecutive_quota_errors), ("stop", 5));
    for r in [
        "dailyLimitExceeded",
        "rateLimitExceeded",
        "userRateLimitExceeded",
    ] {
        let b = json!({"error": {"errors": [{"reason": r}]}});
        assert_eq!(classify_error(403, &b, 0, 1.0, 5).class, "quota");
    }
}

#[test]
fn chat_ended_resets_without_touching_quota_counter() {
    let b = json!({"error": {"errors": [{"reason": "liveChatEnded"}]}});
    let v = classify_error(403, &b, 2, 30.0, 5);
    assert_eq!(
        (v.class, v.action, v.consecutive_quota_errors),
        ("chat_unavailable", "rediscover", 2)
    );
    assert_eq!(v.reason.as_deref(), Some("liveChatEnded"));
}

#[test]
fn other_statuses_classify() {
    let n = Value::Null;
    assert_eq!(
        classify_error(401, &n, 0, 30.0, 5).action,
        "refresh_credentials"
    );
    for s in [429u16, 500, 503] {
        assert_eq!(classify_error(s, &n, 0, 30.0, 5).class, "retryable");
    }
    for s in [400u16, 404, 302] {
        assert_eq!(classify_error(s, &n, 0, 30.0, 5).class, "fatal");
    }
}

fn tool(name: &str, args: Value) -> Result<Value, LogicError> {
    run_tool(name, &args)
}

const TOKEN_SLOT: &str = "{{YT_OAUTH_TOKEN}}";

fn slots(t: &Value) -> Vec<String> {
    t["secret_refs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p[1].as_str().unwrap().to_string())
        .collect()
}

#[test]
fn discovery_templates_use_host_pinned_slots_not_caller_ids() {
    let r = tool("discovery_request", json!({"step": "search"})).unwrap();
    let url = r["url"].as_str().unwrap();
    assert!(url.starts_with("https://www.googleapis.com/youtube/v3/search?"));
    assert!(url.contains("channelId={{YT_CHANNEL_ID}}"));
    assert!(url.contains("eventType=live") && url.contains("maxResults=5"));
    assert_eq!(r["method"], "GET");
    assert_eq!(slots(&r), ["youtube.oauth", "youtube.channel_id"]);
    assert_eq!(r["headers"][0]["value"], format!("Bearer {TOKEN_SLOT}"));

    let r = tool("discovery_request", json!({"step": "videos"})).unwrap();
    assert!(
        r["url"]
            .as_str()
            .unwrap()
            .ends_with("videos?part=liveStreamingDetails&id={{YT_VIDEO_ID}}")
    );
    assert_eq!(slots(&r), ["youtube.oauth", "youtube.video_id"]);

    // A caller-supplied id is rejected by schema, never templated.
    let r = tool(
        "discovery_request",
        json!({"step": "search", "channel_id": "evil"}),
    )
    .unwrap();
    assert!(!r.to_string().contains("evil"));
}

#[test]
fn discovery_parse_round_trip_and_unsafe_ids_rejected() {
    let s = json!({"items": [{"id": {}}, {"id": {"videoId": "vid123"}}]});
    assert_eq!(
        tool("discovery_parse", json!({"step": "search", "body": s})).unwrap(),
        json!({"video_id": "vid123"})
    );
    let none = json!({"items": []});
    assert_eq!(
        tool(
            "discovery_parse",
            json!({"step": "search", "body": none.clone()})
        )
        .unwrap(),
        json!({"video_id": null})
    );
    assert_eq!(
        tool("discovery_parse", json!({"step": "search", "body": {}})).unwrap(),
        json!({"video_id": null})
    );
    let v = json!({"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat123"}}]});
    assert_eq!(
        tool("discovery_parse", json!({"step": "videos", "body": v})).unwrap(),
        json!({"live_chat_id": "chat123"})
    );
    for body in [
        none,
        json!({"items": [{}]}),
        json!({"items": [{"liveStreamingDetails": {"activeLiveChatId": ""}}]}),
    ] {
        assert_eq!(
            tool("discovery_parse", json!({"step": "videos", "body": body})).unwrap(),
            json!({"live_chat_id": null})
        );
    }
    // ids from the API that could forge a slot are a typed error
    for bad in ["a{{YT_OAUTH_TOKEN}}b", "x\u{0}y", "x\u{200B}y"] {
        let v = json!({"items": [{"liveStreamingDetails": {"activeLiveChatId": bad}}]});
        assert!(matches!(
            tool("discovery_parse", json!({"step": "videos", "body": v})),
            Err(LogicError::Malformed(_))
        ));
        let s = json!({"items": [{"id": {"videoId": bad}}]});
        assert!(matches!(
            tool("discovery_parse", json!({"step": "search", "body": s})),
            Err(LogicError::Malformed(_))
        ));
    }
}

#[test]
fn tool_argument_errors_fail_loud() {
    for (n, a) in [
        ("discovery_request", json!({"step": "nope"})),
        ("discovery_request", json!({})),
        ("discovery_parse", json!({"step": "nope", "body": {}})),
        ("discovery_parse", json!({"step": "search"})),
        ("poll_request", json!({"max_results": 0})),
        ("poll_request", json!({"max_results": 99999})),
        ("poll_plan", json!({})),
        ("classify_error", json!({})),
        ("classify_error", json!({"status": 70000})),
        ("build_send_chat_request", json!({"payload": {"text": "x"}})),
        ("does_not_exist", json!({})),
    ] {
        assert!(
            matches!(tool(n, a.clone()), Err(LogicError::Malformed(_))),
            "{n} {a}"
        );
    }
}

#[test]
fn poll_request_plan_and_classify_tools() {
    let r = tool("poll_request", json!({})).unwrap();
    assert!(
        r["url"]
            .as_str()
            .unwrap()
            .contains("liveChatId={{YT_LIVE_CHAT_ID}}&maxResults=200")
    );
    assert_eq!(slots(&r), ["youtube.oauth", "youtube.live_chat_id"]);
    let r = tool("poll_request", json!({"max_results": 50})).unwrap();
    assert!(r["url"].as_str().unwrap().ends_with("maxResults=50"));
    let p = tool(
        "poll_plan",
        json!({"body": {"nextPageToken": "N", "pollingIntervalMillis": 4000}}),
    )
    .unwrap();
    assert_eq!(p, json!({"next_page_token": "N", "interval_ms": 4000}));
    let c = tool(
        "classify_error",
        json!({"status": 403, "body": {"error": {"errors": [{"reason": "quotaExceeded"}]}},
               "consecutive_quota_errors": 4, "max_consecutive_quota_errors": 5,
               "no_broadcast_backoff_s": 10.0}),
    )
    .unwrap();
    assert_eq!(
        (c["action"].clone(), c["consecutive_quota_errors"].clone()),
        (json!("stop"), json!(5))
    );
    let c = tool("classify_error", json!({"status": 500})).unwrap();
    assert_eq!(
        (c["action"].clone(), c["backoff_s"].clone()),
        (json!("retry"), json!(30.0))
    );
}

#[test]
fn chat_send_template_pins_chat_and_never_holds_token() {
    let t = build_chat_send("chat.send", "target-1", r#"{"text": "  hi chat  "}"#).unwrap();
    assert_eq!(t.method, "POST");
    assert_eq!(
        t.url,
        "https://www.googleapis.com/youtube/v3/liveChat/messages?part=snippet"
    );
    let body: Value = serde_json::from_slice(t.body.as_ref().unwrap()).unwrap();
    assert_eq!(
        body,
        json!({"snippet": {"liveChatId": "{{YT_LIVE_CHAT_ID}}", "type": "textMessageEvent",
                           "textMessageDetails": {"messageText": "hi chat"}}})
    );
    assert_eq!(
        t.secret_refs,
        vec![
            (TOKEN_SLOT.to_string(), "youtube.oauth".to_string()),
            (
                "{{YT_LIVE_CHAT_ID}}".to_string(),
                "youtube.live_chat_id".to_string()
            ),
        ]
    );
    assert!(format!("{:?}", t.headers).contains(&format!("Bearer {TOKEN_SLOT}")));
    // neither the target nor a payload liveChatId can redirect the message
    let t = build_chat_send(
        "chat.send",
        "target-1",
        r#"{"text": "x", "liveChatId": "evil"}"#,
    )
    .unwrap();
    let b = String::from_utf8_lossy(t.body.as_ref().unwrap()).into_owned();
    assert!(!b.contains("evil") && !b.contains("target-1"));
}

#[test]
fn chat_send_rejects_bad_input() {
    let ok = |t: &str| build_chat_send("chat.send", "c", &json!({"text": t}).to_string());
    assert!(matches!(
        build_chat_send("moderation.ban", "c", "{}"),
        Err(LogicError::Unsupported(_))
    ));
    assert!(matches!(
        build_chat_send("chat.send", "", r#"{"text":"x"}"#),
        Err(LogicError::Malformed(_))
    ));
    assert!(matches!(
        build_chat_send("chat.send", "c", "nope"),
        Err(LogicError::Malformed(_))
    ));
    assert!(matches!(
        build_chat_send("chat.send", "c", "{}"),
        Err(LogicError::Malformed(_))
    ));
    assert!(matches!(ok("   "), Err(LogicError::Malformed(_))));
    assert!(matches!(
        ok(&"a".repeat(201)),
        Err(LogicError::Malformed(_))
    ));
    assert!(ok(&"a".repeat(200)).is_ok());
}

/// Regression (Twitch overlay review, HIGH): checking `{{` BEFORE stripping control
/// characters let `{<ctrl>{SECRET}}` become a live slot. We REJECT (never strip) any
/// control/invisible/format char, then `{{`/`}}` on the raw and cleaned strings.
#[test]
fn chat_send_rejects_slot_smuggling_and_invisible_chars() {
    let send = |t: &str| build_chat_send("chat.send", "c", &json!({"text": t}).to_string());
    let bad = [
        // control char between braces (the HIGH bug) -- C0, DEL, C1, NUL, newline, tab
        "{\u{1}{YT_OAUTH_TOKEN}}",
        "{\u{0}{YT_OAUTH_TOKEN}}",
        "{\u{7f}{YT_OAUTH_TOKEN}}",
        "{\u{85}{YT_OAUTH_TOKEN}}",
        "{\n{YT_OAUTH_TOKEN}}",
        "{\t{YT_OAUTH_TOKEN}}",
        "hello\u{1b}[31m",
        // zero-width / format / bidi between or around braces
        "{\u{200B}{YT_OAUTH_TOKEN}}",
        "{\u{200D}{YT_OAUTH_TOKEN}}",
        "{\u{2060}{YT_OAUTH_TOKEN}}",
        "{\u{FEFF}{YT_OAUTH_TOKEN}}",
        "{\u{202E}{YT_OAUTH_TOKEN}}",
        "{\u{2066}{YT_OAUTH_TOKEN}}",
        "{\u{00AD}{YT_OAUTH_TOKEN}}",
        "{\u{2028}{YT_OAUTH_TOKEN}}",
        "plain \u{200B}text",
        // lookalike braces
        "\u{FF5B}\u{FF5B}YT_OAUTH_TOKEN\u{FF5D}\u{FF5D}",
        "\u{2774}\u{2774}x\u{2775}\u{2775}",
        // plain markers (raw)
        "{{YT_OAUTH_TOKEN}}",
        "a {{ b",
        "a }} b",
        "{{YT_LIVE_CHAT_ID}}",
    ];
    for t in bad {
        let r = send(t);
        assert!(
            matches!(r, Err(LogicError::Malformed(_))),
            "must reject {t:?}, got {r:?}"
        );
    }
    // single braces and ordinary unicode are fine
    assert!(send("{ok} caf\u{e9} \u{1f389}").is_ok());
    // target-source-id is guarded the same way
    for t in ["{\u{1}{X}}", "a\u{200B}b", "{{X}}"] {
        assert!(matches!(
            build_chat_send("chat.send", t, r#"{"text":"x"}"#),
            Err(LogicError::Malformed(_))
        ));
    }
    // a hostile text never reaches the emitted body
    let t = send("{ok}").unwrap();
    assert!(!String::from_utf8_lossy(t.body.as_ref().unwrap()).contains("{{YT_OAUTH"));
}

#[test]
fn poll_plan_rejects_hostile_page_tokens() {
    for bad in ["{{YT_OAUTH_TOKEN}}", "a\u{0}b", "a\u{200B}b", "{\u{1}{x}}"] {
        assert!(
            matches!(
                poll_plan(&json!({"nextPageToken": bad})),
                Err(LogicError::Malformed(_))
            ),
            "{bad:?}"
        );
    }
    assert!(tool("poll_plan", json!({"body": {"nextPageToken": "a\u{1}b"}})).is_err());
}

#[test]
fn send_chat_tool_returns_template() {
    let r = tool(
        "build_send_chat_request",
        json!({"target_source_id": "chat1", "payload": {"text": "yo"}}),
    )
    .unwrap();
    assert_eq!(r["method"], "POST");
    assert!(r["body"].as_str().unwrap().contains("{{YT_LIVE_CHAT_ID}}"));
    assert!(
        tool(
            "build_send_chat_request",
            json!({"target_source_id": "chat1", "payload": {"text": "{\u{1}{X}}"}})
        )
        .is_err()
    );
}

/// Known gap pinned by test (coordinator item 3): the generic host PollTransport treats
/// EVERY 403 as fatal, so a quota 403 kills the poller instead of backing off. The overlay
/// classifies correctly (below); wiring it needs a host hook (see README "Gaps").
#[test]
fn known_gap_quota_403_is_backoff_here_but_fatal_in_host_poll_transport() {
    let q = json!({"error": {"errors": [{"reason": "quotaExceeded"}]}});
    assert_eq!(classify_error(403, &q, 0, 30.0, 5).action, "backoff");
}
