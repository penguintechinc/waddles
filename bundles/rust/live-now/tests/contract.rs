//! Contract-fixture tests for the `<platform>.live.channels` tools this app consumes.
//!
//! Pins, against `bundles/contracts/live-channels-contract.v1.json`: (a) every request the app
//! sends validates against the contract's input schema, (b) provider output the app accepts is
//! exactly output the contract schema accepts (and contract-invalid output is rejected loudly),
//! (c) both manifests request exactly the contract tools from the right service bundles and
//! agree with each other.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use serde_json::{Value, json};
use waddle_bundle_live_now::logic::testing::FakeHost;
use waddle_bundle_live_now::logic::{
    CONTRACT_PLATFORMS, Config, SERVICE_PREFIX, parse_rfc3339_millis, refresh,
};
use waddle_sdk::tool::ToolError;

const CONTRACT: &str = include_str!("../../../contracts/live-channels-contract.v1.json");
const HUB: &str = include_str!("../hub-manifest.yaml");
const BUNDLE: &str = include_str!("../bundle.yaml");

fn contract() -> Value {
    serde_json::from_str(CONTRACT).unwrap()
}

/// Minimal JSON-schema validator for the subset the contract uses.
fn validate(schema: &Value, defs: &Value, v: &Value, path: &str) -> Result<(), String> {
    if let Some(r) = schema.get("$ref").and_then(Value::as_str) {
        let name = r.rsplit('/').next().unwrap();
        return validate(&defs[name], defs, v, path);
    }
    if let Some(t) = schema.get("type") {
        let types: Vec<&str> = match t {
            Value::String(s) => vec![s.as_str()],
            Value::Array(a) => a.iter().filter_map(Value::as_str).collect(),
            _ => return Err(format!("{path}: bad type decl")),
        };
        let ok = types.iter().any(|t| match *t {
            "object" => v.is_object(),
            "array" => v.is_array(),
            "string" => v.is_string(),
            "integer" => v.is_i64() || v.is_u64(),
            "boolean" => v.is_boolean(),
            "null" => v.is_null(),
            _ => false,
        });
        if !ok {
            return Err(format!("{path}: expected {types:?}, got {v}"));
        }
    }
    if let Some(e) = schema.get("enum").and_then(Value::as_array)
        && !e.contains(v)
    {
        return Err(format!("{path}: {v} not in enum"));
    }
    if let Some(s) = v.as_str() {
        let n = s.chars().count() as u64;
        if schema
            .get("minLength")
            .and_then(Value::as_u64)
            .is_some_and(|m| n < m)
        {
            return Err(format!("{path}: too short"));
        }
        if schema
            .get("maxLength")
            .and_then(Value::as_u64)
            .is_some_and(|m| n > m)
        {
            return Err(format!("{path}: too long"));
        }
    }
    if let Some(n) = v.as_i64() {
        if schema
            .get("minimum")
            .and_then(Value::as_i64)
            .is_some_and(|m| n < m)
        {
            return Err(format!("{path}: below minimum"));
        }
        if schema
            .get("maximum")
            .and_then(Value::as_i64)
            .is_some_and(|m| n > m)
        {
            return Err(format!("{path}: above maximum"));
        }
    }
    if let Some(o) = v.as_object() {
        for r in schema
            .get("required")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let k = r.as_str().unwrap();
            if !o.contains_key(k) {
                return Err(format!("{path}: missing {k}"));
            }
        }
        let props = schema.get("properties").and_then(Value::as_object);
        for (k, val) in o {
            match props.and_then(|p| p.get(k)) {
                Some(s) => validate(s, defs, val, &format!("{path}.{k}"))?,
                None if schema.get("additionalProperties") == Some(&json!(false)) => {
                    return Err(format!("{path}: unexpected {k}"));
                }
                None => {}
            }
        }
    }
    if let (Some(a), Some(items)) = (v.as_array(), schema.get("items")) {
        for (i, x) in a.iter().enumerate() {
            validate(items, defs, x, &format!("{path}[{i}]"))?;
        }
    }
    Ok(())
}

fn output_schema() -> (Value, Value) {
    let c = contract();
    (
        c["tools"]["channels"]["output_schema"].clone(),
        c["definitions"].clone(),
    )
}

#[test]
fn contract_is_well_formed_and_lists_every_platform() {
    let c = contract();
    assert_eq!(c["version"], 1);
    assert_eq!(c["tool_name_template"], "{platform}.live.channels");
    let platforms: Vec<&str> = c["platforms"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(Value::as_str)
        .collect();
    assert_eq!(platforms, CONTRACT_PLATFORMS);
    assert_eq!(c["tools"]["channels"]["access"], "read");
}

fn good_output() -> Value {
    json!({"channels": [
        {"channel_ref": "123", "display_name": "Pen", "is_live": true, "title": "T", "category": "G",
         "viewers": 4, "started_at": "2026-11-07T11:59:00Z", "thumbnail_ref": "thumb/1"},
        {"channel_ref": "456", "display_name": "Off", "is_live": false}
    ], "cursor": null, "as_of": "2026-11-07T12:00:00Z"})
}

#[test]
fn good_output_is_valid_under_the_contract_and_accepted_by_the_app() {
    let (schema, defs) = output_schema();
    validate(&schema, &defs, &good_output(), "$").unwrap();
    for p in CONTRACT_PLATFORMS {
        let h = FakeHost {
            now: parse_rfc3339_millis("2026-11-07T12:00:00Z").unwrap(),
            ..Default::default()
        };
        h.script(p, "community", Ok(good_output()));
        let cfg = Config::parse(&format!(r#"{{"platforms":["{p}"]}}"#)).unwrap();
        let snap = refresh(&h, &cfg).unwrap();
        assert_eq!(snap.streams.len(), 2, "{p}");
        // the request the app sent is contract-valid, names the contract tool, targets the service
        let calls = h.calls.borrow();
        assert_eq!(calls[0].0, format!("{SERVICE_PREFIX}{p}"));
        assert_eq!(calls[0].1, format!("{p}.live.channels"));
        let c = contract();
        validate(
            &c["tools"]["channels"]["input_schema"],
            &c["definitions"],
            &calls[0].2,
            "$in",
        )
        .unwrap();
    }
}

#[test]
fn linked_scope_and_cursor_requests_are_contract_valid() {
    let c = contract();
    let h = FakeHost::default();
    h.script(
        "twitch",
        "community",
        Ok(json!({"channels": [], "cursor": "c1", "as_of": "x"})),
    );
    h.script_page(
        "twitch",
        "community",
        "c1",
        Ok(json!({"channels": [], "as_of": "x"})),
    );
    h.script(
        "twitch",
        "linked",
        Ok(json!({"channels": [], "as_of": "x"})),
    );
    let cfg = Config::parse(r#"{"platforms":["twitch"],"include_linked":true}"#).unwrap();
    refresh(&h, &cfg).unwrap();
    let calls = h.calls.borrow();
    assert_eq!(calls.len(), 3);
    for (_, _, args) in calls.iter() {
        validate(
            &c["tools"]["channels"]["input_schema"],
            &c["definitions"],
            args,
            "$in",
        )
        .unwrap();
    }
}

#[test]
fn contract_invalid_output_is_rejected_loudly_by_both_validator_and_app() {
    let (schema, defs) = output_schema();
    let bad_outputs = [
        json!({"channels": [{"display_name": "x", "is_live": true}], "as_of": "t"}),
        json!({"channels": [{"channel_ref": "1", "display_name": "x", "is_live": "yes"}], "as_of": "t"}),
        json!({"channels": [{"channel_ref": "1", "display_name": "x", "is_live": true, "viewers": -1}], "as_of": "t"}),
        json!({"channels": []}),
    ];
    for bad in bad_outputs {
        assert!(validate(&schema, &defs, &bad, "$").is_err(), "{bad}");
        let h = FakeHost::default();
        h.script("twitch", "community", Ok(bad.clone()));
        let cfg = Config::parse(r#"{"platforms":["twitch"]}"#).unwrap();
        // viewers:-1 and missing as_of are caught by serde (u64 / required); all must error
        let err = refresh(&h, &cfg).unwrap_err();
        assert!(err.contains("contract_violation"), "{bad} -> {err}");
    }
}

#[test]
fn provider_error_codes_surface_per_platform() {
    let c = contract();
    for code in c["error_codes"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(Value::as_str)
    {
        let h = FakeHost::default();
        h.script(
            "twitch",
            "community",
            Err(ToolError::InvalidArgs(format!("{code}: detail"))),
        );
        let cfg = Config::parse(r#"{"platforms":["twitch"]}"#).unwrap();
        assert!(
            refresh(&h, &cfg)
                .unwrap_err()
                .contains(&format!("twitch={code}"))
        );
    }
}

fn requested_tools(manifest: &str) -> Vec<String> {
    manifest
        .lines()
        .filter_map(|l| l.trim().strip_prefix("- tool: "))
        .map(str::to_string)
        .collect()
}

fn permission_ids(manifest: &str) -> Vec<String> {
    let mut v: Vec<String> = manifest
        .lines()
        .filter_map(|l| l.trim().strip_prefix("- id: "))
        .map(|s| s.trim_matches('"').to_string())
        .collect();
    v.sort();
    v
}

#[test]
fn manifests_request_exactly_the_contract_tools_and_agree() {
    let want: Vec<String> = ["twitch", "youtube", "discord"]
        .iter()
        .map(|p| format!("{p}.live.channels"))
        .collect();
    assert_eq!(requested_tools(HUB), want);
    assert_eq!(requested_tools(BUNDLE), want);
    assert_eq!(permission_ids(HUB), permission_ids(BUNDLE));
    for p in ["twitch", "youtube", "discord"] {
        let id = format!("tool.call:{SERVICE_PREFIX}{p}:{p}.live.channels");
        assert!(permission_ids(HUB).contains(&id), "{id}");
        assert!(HUB.contains(&format!("target_app_id: {SERVICE_PREFIX}{p}\n")));
    }
    // kick ships later: not yet requested, but the contract lists it
    assert!(!HUB.contains("kick.live.channels"));
}

#[test]
fn manifest_declares_no_egress_and_no_secrets() {
    assert!(HUB.contains("egress: []"));
    for banned in ["token", "secret", "api_key", "password"] {
        assert!(
            !HUB.to_lowercase().contains(&format!("{banned}:")),
            "{banned}"
        );
    }
}
