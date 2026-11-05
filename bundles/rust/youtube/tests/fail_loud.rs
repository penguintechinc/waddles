//! Fail-loud regressions (check-no-stubs): a present-but-invalid or missing argument is a
//! typed error, never silently defaulted into an empty payload / default integer. Defaults
//! apply ONLY when a documented optional field is absent or null. Mutation-checked.

use serde_json::{Value, json};
use waddle_bundle_youtube_service::logic::{LogicError, parse_frame, poll_plan, run_tool};

fn is_malformed<T: std::fmt::Debug>(r: Result<T, LogicError>) -> bool {
    matches!(r, Err(LogicError::Malformed(_)))
}

#[test]
fn missing_payload_is_not_defaulted_to_empty() {
    // Previously `unwrap_or_default()` turned this into "" and failed later as "not JSON".
    let r = run_tool("build_send_chat_request", &json!({"target_source_id": "t"}));
    let Err(LogicError::Malformed(m)) = r else {
        panic!("expected Malformed");
    };
    assert_eq!(m, "missing payload");
}

#[test]
fn present_wrong_typed_options_are_errors_not_defaults() {
    for (tool, args) in [
        ("poll_request", json!({"max_results": "5"})),
        ("poll_request", json!({"max_results": 1.5})),
        ("poll_request", json!({"max_results": -3})),
        ("poll_request", json!({"max_results": 4294967296u64})),
        ("poll_request", json!({"max_results": true})),
        (
            "classify_error",
            json!({"status": 403, "consecutive_quota_errors": "x"}),
        ),
        (
            "classify_error",
            json!({"status": 403, "consecutive_quota_errors": -1}),
        ),
        (
            "classify_error",
            json!({"status": 403, "max_consecutive_quota_errors": 0}),
        ),
        (
            "classify_error",
            json!({"status": 403, "max_consecutive_quota_errors": "5"}),
        ),
        (
            "classify_error",
            json!({"status": 403, "no_broadcast_backoff_s": "30"}),
        ),
        (
            "classify_error",
            json!({"status": 403, "no_broadcast_backoff_s": -1}),
        ),
        ("classify_error", json!({"status": "403"})),
        ("classify_error", json!({"status": 65536})),
    ] {
        assert!(is_malformed(run_tool(tool, &args)), "{tool} {args}");
    }
}

#[test]
fn absent_or_null_optionals_use_the_documented_defaults() {
    let r = run_tool("poll_request", &json!({"max_results": null})).unwrap();
    assert!(r["url"].as_str().unwrap().ends_with("maxResults=200"));
    let c = run_tool("classify_error", &json!({"status": 500})).unwrap();
    assert_eq!(c["backoff_s"], json!(30.0));
    let c = run_tool(
        "classify_error",
        &json!({"status": 500, "no_broadcast_backoff_s": null, "consecutive_quota_errors": null}),
    )
    .unwrap();
    assert_eq!(c["consecutive_quota_errors"], json!(0));
}

#[test]
fn polling_interval_wrong_type_is_an_error() {
    for bad in [json!("x"), json!(true), json!([1]), json!({"a": 1})] {
        assert!(
            is_malformed(poll_plan(&json!({"pollingIntervalMillis": bad}))),
            "{bad}"
        );
    }
    assert_eq!(poll_plan(&json!({})).unwrap().interval_ms, 5000);
}

#[test]
fn items_without_text_are_counted_not_silently_lost() {
    let body = json!({"items": [
        {"id": "a", "snippet": {"liveChatId": "c"}},
        {"id": "b", "snippet": {"displayMessage": "ok", "liveChatId": "c"}},
        {"id": "c", "snippet": {"displayMessage": "no chat"}}
    ]});
    let p = parse_frame(&serde_json::to_vec(&body).unwrap(), "2026-11-05T00:00:00Z").unwrap();
    assert_eq!((p.events.len(), p.skipped), (1, 2));
    let none: Value = json!({"items": []});
    let p = parse_frame(&serde_json::to_vec(&none).unwrap(), "t").unwrap();
    assert_eq!((p.events.len(), p.skipped), (0, 0));
}
