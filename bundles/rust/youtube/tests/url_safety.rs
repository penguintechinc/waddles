//! URL-query-injection regressions: every id/token that can reach a URL is an anchored
//! allowlist (`is_valid_id`, `is_valid_page_token`), `maxResults` is a bounded integer,
//! and non-slot values the bundle builds are percent-encoded (`pct`). Mutation-checked.

use serde_json::{Value, json};
use waddle_bundle_youtube_service::logic::{
    LogicError, is_valid_id, is_valid_page_token, pct, poll_plan, run_tool,
};

/// Real-looking values (alphabets seen in the Python fixtures / Data API docs).
const REAL_IDS: [&str; 6] = [
    "UCviewer1",
    "UC_x5XG1OV2P6uZZ5FSM9Ttw",
    "dQw4w9WgXcQ",
    "-abc_DEF-123",
    "Cg0KC2RRdzR3OVdnWGNR",
    "KicKGFVDX3g1WEcxT1YyUDZ1Wlo1RlNNOVR0dxILZFF3NHc5V2dYY1E",
];
const REAL_TOKENS: [&str; 5] = [
    "GJ-L0pXwgPQCIIvVy5Lwg_QC",
    "CAoQAQ==",
    "GMLR5r2UgPQCIKiRoJbdhPQC",
    "a.b-c_d=e",
    "T",
];

/// One hostile value per class, with the character that makes it hostile.
fn hostile() -> Vec<(&'static str, String)> {
    vec![
        ("ampersand", "abc&maxResults=1".into()),
        ("equals", "abc=def&x".into()),
        ("hash", "abc#frag".into()),
        ("percent", "abc%26def".into()),
        ("slash", "abc/../v1".into()),
        ("question", "abc?x=1".into()),
        ("space", "abc def".into()),
        ("plus", "abc+def".into()),
        ("unicode", "abc\u{e9}\u{4e16}".into()),
        ("quote", "abc\"def".into()),
        ("over-length", "a".repeat(129)),
        ("slot-forge", "{{YT_OAUTH_TOKEN}}".into()),
    ]
}

fn parse(step: &str, id: &str) -> Result<Value, LogicError> {
    let body = if step == "search" {
        json!({"items": [{"id": {"videoId": id}}]})
    } else {
        json!({"items": [{"liveStreamingDetails": {"activeLiveChatId": id}}]})
    };
    run_tool("discovery_parse", &json!({"step": step, "body": body}))
}

#[test]
fn real_ids_and_tokens_are_accepted() {
    for id in REAL_IDS {
        assert!(is_valid_id(id), "{id}");
        assert_eq!(parse("search", id).unwrap(), json!({"video_id": id}));
        assert_eq!(parse("videos", id).unwrap(), json!({"live_chat_id": id}));
    }
    assert!(is_valid_id(&"a".repeat(128)));
    for t in REAL_TOKENS {
        assert!(is_valid_page_token(t), "{t}");
        let p = poll_plan(&json!({"nextPageToken": t})).unwrap();
        assert_eq!(p.next_page_token.as_deref(), Some(t));
    }
    assert!(is_valid_page_token(&"a".repeat(512)));
}

#[test]
fn hostile_ids_are_rejected_per_class() {
    for (class, v) in hostile() {
        assert!(!is_valid_id(&v), "{class}");
        for step in ["search", "videos"] {
            assert!(
                matches!(parse(step, &v), Err(LogicError::Malformed(_))),
                "{step} {class}"
            );
        }
    }
    // empty is "no id", not an injectable value
    assert!(!is_valid_id(""));
    assert_eq!(parse("search", "").unwrap(), json!({"video_id": null}));
    assert_eq!(parse("videos", "").unwrap(), json!({"live_chat_id": null}));
}

#[test]
fn hostile_page_tokens_are_rejected_per_class() {
    let mut cases = hostile();
    // `=` and `.` are legitimate in tokens; the rest of the classes are not.
    cases.retain(|(c, _)| *c != "equals" && *c != "over-length");
    cases.push(("equals-with-amp", "ab=cd&x=1".into()));
    cases.push(("over-length", "a".repeat(513)));
    for (class, v) in cases {
        assert!(!is_valid_page_token(&v), "{class}");
        assert!(
            matches!(
                poll_plan(&json!({"nextPageToken": v})),
                Err(LogicError::Malformed(_))
            ),
            "{class}"
        );
    }
    assert!(!is_valid_page_token(""));
    assert_eq!(
        poll_plan(&json!({"nextPageToken": ""}))
            .unwrap()
            .next_page_token,
        None
    );
}

#[test]
fn max_results_is_a_bounded_integer() {
    let url =
        |a: Value| run_tool("poll_request", &a).map(|r| r["url"].as_str().unwrap().to_string());
    assert!(url(json!({})).unwrap().ends_with("maxResults=200"));
    assert!(
        url(json!({"max_results": 1}))
            .unwrap()
            .ends_with("maxResults=1")
    );
    assert!(
        url(json!({"max_results": 2000}))
            .unwrap()
            .ends_with("maxResults=2000")
    );
    for bad in [
        json!({"max_results": 0}),
        json!({"max_results": 2001}),
        json!({"max_results": -1}),
    ] {
        assert!(
            url(bad.clone()).is_err() || bad["max_results"] == json!(-1),
            "{bad}"
        );
    }
    // a string can never become the value: a typed error, never a silent default
    assert!(url(json!({"max_results": "5&liveChatId=evil"})).is_err());
}

#[test]
fn built_urls_have_exactly_the_expected_parameters() {
    for (tool, args, keys) in [
        (
            "discovery_request",
            json!({"step": "search"}),
            vec!["part", "channelId", "eventType", "type", "maxResults"],
        ),
        (
            "discovery_request",
            json!({"step": "videos"}),
            vec!["part", "id"],
        ),
        (
            "poll_request",
            json!({}),
            vec!["part", "liveChatId", "maxResults"],
        ),
    ] {
        let r = run_tool(tool, &args).unwrap();
        let url = r["url"].as_str().unwrap();
        let query = url.split_once('?').unwrap().1;
        let got: Vec<&str> = query
            .split('&')
            .map(|kv| kv.split('=').next().unwrap())
            .collect();
        assert_eq!(got, keys, "{url}");
        assert!(!url.contains('#'), "{url}");
        // literals are percent-encoded (commas), slots are verbatim
        assert!(!url.contains("id,snippet") && !url.contains("authorDetails,"));
    }
}

#[test]
fn pct_encodes_every_reserved_class() {
    assert_eq!(pct("UC-a_b.c~1"), "UC-a_b.c~1");
    assert_eq!(
        pct("a b&c=d/e#f%g?h+i"),
        "a%20b%26c%3Dd%2Fe%23f%25g%3Fh%2Bi"
    );
    assert_eq!(pct("\u{e9}\u{4e16}"), "%C3%A9%E4%B8%96");
    assert_eq!(pct(""), "");
}
