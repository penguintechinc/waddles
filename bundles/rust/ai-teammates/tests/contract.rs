//! Contract-fixture tests for every tool this bundle consumes. The fixtures in `contracts/`
//! are the agreed wire schema with the services being built in parallel (AI service, platform
//! `live.channels`); a mismatch fails here (and in the flow tests, which validate live calls).
mod common;

use common::{contract, validate};
use serde_json::json;
use waddle_bundle_ai_teammates::ai::{self, CONSUMED};

#[test]
fn every_consumed_tool_has_a_fixture_naming_the_same_target() {
    let dir = format!("{}/contracts", env!("CARGO_MANIFEST_DIR"));
    let mut files: Vec<String> = std::fs::read_dir(dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| {
            e.file_name()
                .to_string_lossy()
                .trim_end_matches(".json")
                .to_string()
        })
        .collect();
    files.sort();
    let mut tools: Vec<&str> = CONSUMED.iter().map(|(_, t)| *t).collect();
    tools.sort();
    assert_eq!(
        files, tools,
        "contracts/ and ai::CONSUMED must list the same tools"
    );
    assert_eq!(files.len(), 6);
    for (target, tool) in CONSUMED {
        let c = contract(tool);
        assert_eq!(c["tool"], json!(tool));
        assert_eq!(c["target"], json!(target));
        assert_eq!(
            c["access"],
            json!("read"),
            "this bundle only consumes read tools"
        );
    }
}

#[test]
fn request_builders_satisfy_their_contracts_including_bounds() {
    let c = contract("ai.complete");
    validate(
        &c["request"],
        &ai::complete_args("sys", "prompt", 512, 2.0, "researcher"),
        "r",
    )
    .unwrap();
    validate(
        &c["request"],
        &ai::complete_args("sys", "prompt", 1, 0.0, "producer"),
        "r",
    )
    .unwrap();
    let c = contract("ai.classify");
    validate(
        &c["request"],
        &ai::classify_args("t", &["safe", "unsafe"], "producer_moderation"),
        "r",
    )
    .unwrap();
    let c = contract("ai.rag.query");
    validate(&c["request"], &ai::rag_args("q", 4, None), "r").unwrap();
    validate(&c["request"], &ai::rag_args("q", 8, Some("kb")), "r").unwrap();
    for p in ["twitch", "discord", "youtube"] {
        validate(
            &contract(&format!("{p}.live.channels"))["request"],
            &json!({}),
            "r",
        )
        .unwrap();
        let (target, tool) = ai::live_tool(p).unwrap();
        assert!(CONSUMED.contains(&(target, tool)));
    }
    assert!(ai::live_tool("slack").is_none());
}

#[test]
fn fixtures_actually_reject_violations() {
    let c = contract("ai.complete");
    assert!(
        validate(
            &c["request"],
            &ai::complete_args("s", "p", 513, 0.5, "researcher"),
            "r"
        )
        .is_err()
    );
    assert!(
        validate(
            &c["request"],
            &ai::complete_args("s", "p", 10, 0.5, "other"),
            "r"
        )
        .is_err()
    );
    let mut extra = ai::complete_args("s", "p", 10, 0.5, "researcher");
    extra["tools"] = json!(["economy.credit"]);
    assert!(
        validate(&c["request"], &extra, "r").is_err(),
        "no model-selectable tools field can ride along"
    );
    assert!(
        validate(
            &contract("ai.rag.query")["request"],
            &ai::rag_args("q", 9, None),
            "r"
        )
        .is_err()
    );
    assert!(
        validate(
            &contract("ai.rag.query")["response"],
            &json!({"matches": [{"score": 1.0}]}),
            "r"
        )
        .is_err()
    );
    assert!(
        validate(
            &contract("ai.classify")["response"],
            &json!({"label": "safe", "score": 1.5}),
            "r"
        )
        .is_err()
    );
    assert!(
        validate(
            &contract("twitch.live.channels")["response"],
            &json!({"channels": [{"channel_id": "c"}]}),
            "r"
        )
        .is_err()
    );
}

#[test]
fn response_shapes_parse_into_the_typed_clients() {
    use waddle_bundle_ai_teammates::ai::{Classification, LiveChannel, RagMatch};
    let m: Vec<RagMatch> = serde_json::from_value(json!([{"text": "t", "score": 0.5}])).unwrap();
    assert_eq!(m[0].source, None);
    let c: Classification = serde_json::from_value(json!({"label": "safe", "score": 0.9})).unwrap();
    assert_eq!(c.label, "safe");
    let l: LiveChannel = serde_json::from_value(json!({"channel_id": "x", "live": true})).unwrap();
    assert!(l.live && l.title.is_none());
    assert!(serde_json::from_value::<RagMatch>(json!({"text": "t"})).is_err());
}
