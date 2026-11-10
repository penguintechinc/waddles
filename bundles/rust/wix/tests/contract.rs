//! Contract fixture: the published `wix.event.*` tool schemas (`tests/fixtures/wix_event_tools.json`,
//! mirrored into `bundle.yaml`/`hub-manifest.yaml` `exported_tools` -- enforced by
//! `scripts/ci/tests/test_wix_tool_contract.py`) must agree with what the sender accepts: every schema
//! property is accepted, every required property is needed, and unknown properties are refused.

use serde_json::{Map, Value, json};
use waddle_bundle_wix_service::sender::{Action, SendError, build_request};

const FIXTURE: &str = include_str!("fixtures/wix_event_tools.json");
const ID: &str = "75d64b92-4078-457a-9259-fef2948459f7";

fn sample(prop: &str) -> Value {
    match prop {
        "event_id" => json!(ID),
        "revision" | "limit" => json!(2),
        "offset" => json!(0),
        "starts_at" | "ends_at" | "starts_after" | "starts_before" => json!("2026-12-01T19:00:00Z"),
        "timezone" => json!("Europe/Vilnius"),
        "exclude_canceled" => json!(true),
        "location_type" => json!("VENUE"),
        _ => json!("x"),
    }
}

fn run(tool: &str, payload: &Map<String, Value>) -> Result<(), SendError> {
    build_request(&Action {
        kind: tool.strip_prefix("wix.").unwrap().to_string(),
        target_source_id: "binding".into(),
        payload_json: Value::Object(payload.clone()).to_string(),
    })
    .map(|_| ())
}

#[test]
fn schema_and_sender_agree_for_every_tool() {
    let fx: Value = serde_json::from_str(FIXTURE).unwrap();
    let tools = fx["tools"].as_array().unwrap();
    assert_eq!(tools.len(), 5, "exactly the five wix.event.* tools");
    let mut checked = 0;
    for t in tools {
        let name = t["name"].as_str().unwrap();
        let props = t["input_schema"]["properties"].as_object().unwrap();
        let required: Vec<&str> = t["input_schema"]["required"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert_eq!(t["input_schema"]["additionalProperties"], json!(false));
        let mut full = Map::new();
        for p in props.keys() {
            full.insert(p.clone(), sample(p));
        }
        run(name, &full).unwrap_or_else(|e| panic!("{name}: full payload refused: {e}"));
        for r in &required {
            let mut missing = full.clone();
            missing.remove(*r);
            assert!(
                run(name, &missing).is_err(),
                "{name}: missing `{r}` accepted"
            );
        }
        let mut extra = full.clone();
        extra.insert("site_id".into(), json!("x"));
        assert!(
            matches!(run(name, &extra), Err(SendError::Malformed(_))),
            "{name}: unknown field accepted"
        );
        checked += 1;
    }
    assert_eq!(checked, 5);
}
