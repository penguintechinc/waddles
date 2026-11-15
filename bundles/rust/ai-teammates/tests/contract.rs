//! Contract-fixture tests for every tool this bundle consumes. The fixtures in `contracts/` are
//! generated from the authoritative contracts (`ai-tools.v1.json`, PR #1109/#1120;
//! `live-channels-contract.v1.json`, PR #1119/#1120); a mismatch fails here and in the flow
//! tests, which validate every live call (request and response).
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
    let max_sys = "s".repeat(ai::MAX_SYSTEM_CHARS);
    let max_prompt = "p".repeat(ai::MAX_PROMPT_CHARS);
    validate(
        &c["request"],
        &ai::complete_args(&max_sys, &max_prompt, 512, 2.0),
        "r",
    )
    .unwrap();
    validate(&c["request"], &ai::complete_args("", "p", 1, 0.0), "r").unwrap();
    let c = contract("ai.classify");
    validate(
        &c["request"],
        &ai::classify_args("t", &["safe", "unsafe"], "decide"),
        "r",
    )
    .unwrap();
    let c = contract("ai.rag.query");
    validate(&c["request"], &ai::rag_args("q", 4, "knowledge"), "r").unwrap();
    validate(&c["request"], &ai::rag_args(&"q".repeat(1000), 8, "a"), "r").unwrap();
    for p in ["twitch", "discord", "youtube"] {
        validate(
            &contract(&format!("{p}.live.channels"))["request"],
            &ai::live_args(),
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
    assert!(validate(&c["request"], &ai::complete_args("s", "p", 513, 0.5), "r").is_err());
    assert!(validate(&c["request"], &ai::complete_args("s", "", 10, 0.5), "r").is_err());
    assert!(
        validate(
            &c["request"],
            &ai::complete_args(&"s".repeat(1201), "p", 10, 0.5),
            "r"
        )
        .is_err()
    );
    assert!(
        validate(
            &c["request"],
            &ai::complete_args("s", &"p".repeat(3501), 10, 0.5),
            "r"
        )
        .is_err()
    );
    let mut extra = ai::complete_args("s", "p", 10, 0.5);
    extra["purpose"] = json!("researcher");
    assert!(
        validate(&c["request"], &extra, "r").is_err(),
        "purpose is host-side, never an argument"
    );
    extra = ai::complete_args("s", "p", 10, 0.5);
    extra["tools"] = json!(["economy.credit"]);
    assert!(validate(&c["request"], &extra, "r").is_err());
    let rag = contract("ai.rag.query");
    assert!(validate(&rag["request"], &ai::rag_args("q", 9, "kb"), "r").is_err());
    assert!(
        validate(
            &rag["request"],
            &json!({"query": "q", "collection": "kb"}),
            "r"
        )
        .is_err()
    );
    assert!(
        validate(&rag["request"], &json!({"question": "q"}), "r").is_err(),
        "collection required"
    );
    assert!(validate(&rag["request"], &ai::rag_args("q", 4, "Bad Id"), "r").is_err());
    assert!(validate(&rag["response"], &json!({"matches": []}), "r").is_err());
    assert!(
        validate(
            &contract("ai.classify")["response"],
            &json!({"label": "safe", "score": 0.9}),
            "r"
        )
        .is_err()
    );
    assert!(
        validate(
            &contract("ai.classify")["response"],
            &json!({"label": "safe", "confidence": 1.5, "reason": "r"}),
            "r"
        )
        .is_err()
    );
    assert!(
        validate(
            &contract("ai.classify")["request"],
            &ai::classify_args("", &["a", "b"], "i"),
            "r"
        )
        .is_err()
    );
    let live = contract("twitch.live.channels");
    assert!(
        validate(&live["request"], &json!({}), "r").is_err(),
        "scope is required"
    );
    assert!(validate(&live["request"], &json!({"scope": "everything"}), "r").is_err());
    assert!(
        validate(
            &live["response"],
            &json!({"channels": [{"channel_id": "c", "live": true}], "as_of": "t"}),
            "r"
        )
        .is_err(),
        "old field names rejected"
    );
    assert!(
        validate(&live["response"], &json!({"channels": []}), "r").is_err(),
        "as_of required"
    );
}

#[test]
fn response_shapes_parse_into_the_typed_clients() {
    use waddle_bundle_ai_teammates::ai::{Classification, LiveChannel, RagAnswer};
    let r: RagAnswer =
        serde_json::from_value(json!({"answer": "a", "citations": [{"doc_id": "d", "chunk": 2}]}))
            .unwrap();
    assert_eq!(r.citations[0].score, None);
    let c: Classification =
        serde_json::from_value(json!({"label": "safe", "confidence": 0.9, "reason": "ok"}))
            .unwrap();
    assert_eq!(c.label, "safe");
    let l: LiveChannel = serde_json::from_value(
        json!({"channel_ref": "x", "display_name": "X", "is_live": true, "title": null, "viewers": 3}),
    )
    .unwrap();
    assert!(l.is_live && l.title.is_none() && l.category.is_none());
    assert!(serde_json::from_value::<RagAnswer>(json!({"answer": "t"})).is_err());
}

#[test]
fn client_side_bounds_fail_loud_before_any_call() {
    use common::FakeHost;
    let h = FakeHost::new(); // no handlers: any call would panic
    assert!(ai::complete(&h, "s", "", 10, 0.5).is_err());
    assert!(ai::complete(&h, "s", &"p".repeat(3501), 10, 0.5).is_err());
    assert!(ai::complete(&h, &"s".repeat(1201), "p", 10, 0.5).is_err());
    assert!(ai::classify(&h, "", &["a", "b"], "i").is_err());
    assert!(ai::rag_query(&h, "", 4, "kb").is_err());
    assert!(ai::rag_query(&h, "q", 4, "Bad Id").is_err());
    assert!(h.calls.borrow().is_empty());
    assert!(
        ai::collection_ok("kb")
            && ai::collection_ok("a_b.c-d")
            && !ai::collection_ok("_x")
            && !ai::collection_ok(&"a".repeat(41))
    );
}
