//! Poll plan, quota/backoff classification, discovery, tools and chat-send template.

use serde_json::{Value, json};
use waddle_bundle_youtube_service::logic::{
    LogicError, build_chat_send, classify_error, describe_403_reason, pct, poll_plan, run_tool,
};

#[test]
fn poll_plan_honors_token_interval_and_floor() {
    let p = poll_plan(&json!({"nextPageToken": "T", "pollingIntervalMillis": 7000}));
    assert_eq!(
        (p.next_page_token.as_deref(), p.interval_ms),
        (Some("T"), 7000)
    );
    // floor: never a tight loop
    assert_eq!(
        poll_plan(&json!({"pollingIntervalMillis": 100})).interval_ms,
        2000
    );
    assert_eq!(
        poll_plan(&json!({"pollingIntervalMillis": -5})).interval_ms,
        2000
    );
    assert_eq!(
        poll_plan(&json!({"pollingIntervalMillis": 3500.9})).interval_ms,
        3500
    );
    // default when absent / wrong type; empty token is no token
    let p = poll_plan(&json!({"pollingIntervalMillis": "x", "nextPageToken": ""}));
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

#[test]
fn percent_encoding() {
    assert_eq!(pct("UC-a_b.c~1"), "UC-a_b.c~1");
    assert_eq!(pct("a b&c=d/é"), "a%20b%26c%3Dd%2F%C3%A9");
}

fn tool(name: &str, args: Value) -> Result<Value, LogicError> {
    run_tool(name, &args)
}

#[test]
fn discovery_tools_round_trip() {
    let r = tool(
        "discovery_request",
        json!({"step": "search", "channel_id": "UC abc"}),
    )
    .unwrap();
    let url = r["url"].as_str().unwrap();
    assert!(url.starts_with("https://www.googleapis.com/youtube/v3/search?"));
    assert!(
        url.contains("channelId=UC%20abc")
            && url.contains("eventType=live")
            && url.contains("maxResults=5")
    );
    assert_eq!(r["method"], "GET");
    // credential only ever as a slot, never a literal
    assert_eq!(
        r["secret_refs"],
        json!([["{{YT_OAUTH_TOKEN}}", "youtube.oauth"]])
    );
    assert_eq!(r["headers"][0]["value"], "Bearer {{YT_OAUTH_TOKEN}}");

    let r = tool(
        "discovery_request",
        json!({"step": "videos", "video_id": "vid1"}),
    )
    .unwrap();
    assert!(
        r["url"]
            .as_str()
            .unwrap()
            .ends_with("videos?part=liveStreamingDetails&id=vid1")
    );

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
    let v = json!({"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat123"}}]});
    assert_eq!(
        tool("discovery_parse", json!({"step": "videos", "body": v})).unwrap(),
        json!({"live_chat_id": "chat123"})
    );
    assert_eq!(
        tool("discovery_parse", json!({"step": "videos", "body": none})).unwrap(),
        json!({"live_chat_id": null})
    );
    assert_eq!(
        tool(
            "discovery_parse",
            json!({"step": "videos", "body": {"items": [{}]}})
        )
        .unwrap(),
        json!({"live_chat_id": null})
    );
}

#[test]
fn tool_argument_errors_fail_loud() {
    for (n, a) in [
        ("discovery_request", json!({"step": "nope"})),
        ("discovery_request", json!({"step": "search"})),
        ("discovery_parse", json!({"step": "nope", "body": {}})),
        ("discovery_parse", json!({"step": "search"})),
        ("poll_request", json!({})),
        (
            "poll_request",
            json!({"live_chat_id": "c", "max_results": 0}),
        ),
        (
            "poll_request",
            json!({"live_chat_id": "c", "max_results": 99999}),
        ),
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
fn poll_request_and_plan_and_classify_tools() {
    let r = tool("poll_request", json!({"live_chat_id": "chat123"})).unwrap();
    assert!(
        r["url"]
            .as_str()
            .unwrap()
            .contains("liveChatId=chat123&maxResults=200")
    );
    let r = tool(
        "poll_request",
        json!({"live_chat_id": "c", "max_results": 50}),
    )
    .unwrap();
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
fn chat_send_template_pins_target_and_never_holds_token() {
    let t = build_chat_send("chat.send", "chatABC", r#"{"text": "  hi chat  "}"#).unwrap();
    assert_eq!(t.method, "POST");
    assert_eq!(
        t.url,
        "https://www.googleapis.com/youtube/v3/liveChat/messages?part=snippet"
    );
    let body: Value = serde_json::from_slice(t.body.as_ref().unwrap()).unwrap();
    assert_eq!(
        body,
        json!({"snippet": {"liveChatId": "chatABC", "type": "textMessageEvent",
                           "textMessageDetails": {"messageText": "hi chat"}}})
    );
    assert_eq!(
        t.secret_refs,
        vec![(
            "{{YT_OAUTH_TOKEN}}".to_string(),
            "youtube.oauth".to_string()
        )]
    );
    let all = format!("{:?}", t.headers);
    assert!(all.contains("Bearer {{YT_OAUTH_TOKEN}}"));
    // the payload cannot redirect the message: a payload liveChatId is ignored
    let t = build_chat_send(
        "chat.send",
        "chatABC",
        r#"{"text": "x", "liveChatId": "evil"}"#,
    )
    .unwrap();
    assert!(!String::from_utf8_lossy(t.body.as_ref().unwrap()).contains("evil"));
}

#[test]
fn chat_send_rejects_bad_input() {
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
    assert!(matches!(
        build_chat_send("chat.send", "c", r#"{"text":"   "}"#),
        Err(LogicError::Malformed(_))
    ));
    let long = format!(r#"{{"text":"{}"}}"#, "a".repeat(201));
    assert!(matches!(
        build_chat_send("chat.send", "c", &long),
        Err(LogicError::Malformed(_))
    ));
    let ok = format!(r#"{{"text":"{}"}}"#, "a".repeat(200));
    assert!(build_chat_send("chat.send", "c", &ok).is_ok());
}

#[test]
fn send_chat_tool_returns_template() {
    let r = tool(
        "build_send_chat_request",
        json!({"target_source_id": "chat1", "payload": {"text": "yo"}}),
    )
    .unwrap();
    assert_eq!(r["method"], "POST");
    assert!(r["body"].as_str().unwrap().contains("chat1"));
}
