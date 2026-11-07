//! Contract tests against the SHARED music contract
//! (`bundles/contracts/music-tool-contract.v1.json`, authoritative): every tool
//! output is validated against the contract's `output_schema`, inputs the
//! contract allows are accepted and inputs it forbids are rejected, and the
//! two shipped manifests agree. Drift fails here, loudly.

use std::fs;
use std::path::Path;
use std::sync::Arc;

use serde_json::{Value, json};
use waddle_bundle_music_soundcloud::logic::fakes::{FakeStore, FakeUpstream, ok, service, status};
use waddle_bundle_music_soundcloud::logic::{TOOL_NAMES, build_registry};

fn root(rel: &str) -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join(rel)
}

fn read(rel: &str) -> String {
    let p = root(rel);
    fs::read_to_string(&p).unwrap_or_else(|e| panic!("read {p:?}: {e}"))
}

fn contract() -> Value {
    serde_json::from_str(&read("../../contracts/music-tool-contract.v1.json")).unwrap()
}

/// Minimal JSON-schema checker for the keyword subset the contract uses.
fn validate(schema: &Value, v: &Value, defs: &Value, path: &str) -> Vec<String> {
    if let Some(r) = schema.get("$ref").and_then(Value::as_str) {
        let name = r.rsplit('/').next().unwrap();
        return validate(&defs[name], v, defs, path);
    }
    let mut errs = Vec::new();
    if let Some(t) = schema.get("type") {
        let types: Vec<&str> = match t {
            Value::String(s) => vec![s.as_str()],
            Value::Array(a) => a.iter().filter_map(Value::as_str).collect(),
            _ => vec![],
        };
        let ok = types.iter().any(|t| match *t {
            "object" => v.is_object(),
            "array" => v.is_array(),
            "string" => v.is_string(),
            "integer" => v.is_i64() || v.is_u64(),
            "boolean" => v.is_boolean(),
            "null" => v.is_null(),
            "number" => v.is_number(),
            _ => false,
        });
        if !ok {
            return vec![format!("{path}: expected {types:?}, got {v}")];
        }
    }
    if let Some(e) = schema.get("enum").and_then(Value::as_array)
        && !e.contains(v)
    {
        errs.push(format!("{path}: {v} not in enum"));
    }
    if let (Some(m), Some(n)) = (schema.get("minimum").and_then(Value::as_i64), v.as_i64())
        && n < m
    {
        errs.push(format!("{path}: {n} < minimum {m}"));
    }
    if let (Some(m), Some(n)) = (schema.get("maximum").and_then(Value::as_i64), v.as_i64())
        && n > m
    {
        errs.push(format!("{path}: {n} > maximum {m}"));
    }
    if let Some(s) = v.as_str() {
        let len = s.chars().count() as u64;
        if schema
            .get("minLength")
            .and_then(Value::as_u64)
            .is_some_and(|m| len < m)
        {
            errs.push(format!("{path}: too short"));
        }
        if schema
            .get("maxLength")
            .and_then(Value::as_u64)
            .is_some_and(|m| len > m)
        {
            errs.push(format!("{path}: too long"));
        }
    }
    if let Some(a) = v.as_array() {
        if schema
            .get("maxItems")
            .and_then(Value::as_u64)
            .is_some_and(|m| a.len() as u64 > m)
        {
            errs.push(format!("{path}: too many items"));
        }
        if let Some(items) = schema.get("items") {
            for (i, x) in a.iter().enumerate() {
                errs.extend(validate(items, x, defs, &format!("{path}[{i}]")));
            }
        }
    }
    if let Some(o) = v.as_object() {
        let props = schema.get("properties").and_then(Value::as_object);
        for r in schema
            .get("required")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let k = r.as_str().unwrap();
            if !o.contains_key(k) {
                errs.push(format!("{path}: missing required {k}"));
            }
        }
        for (k, x) in o {
            match props.and_then(|p| p.get(k)) {
                Some(s) => errs.extend(validate(s, x, defs, &format!("{path}.{k}"))),
                None if schema.get("additionalProperties") == Some(&json!(false)) => {
                    errs.push(format!("{path}: unexpected field {k}"));
                }
                None => {}
            }
        }
    }
    errs
}

/// The `exported_tools:` block (it runs to end-of-section in both manifests).
fn tools_block(manifest: &str) -> String {
    let start = manifest
        .find("exported_tools:")
        .expect("exported_tools block");
    let end = manifest
        .find("\nconfig_schema:")
        .expect("config_schema after tools");
    manifest[start..end].to_string()
}

#[test]
fn manifests_agree_and_declare_every_registered_tool() {
    let (b, h) = (read("bundle.yaml"), read("hub-manifest.yaml"));
    assert_eq!(
        tools_block(&b),
        tools_block(&h),
        "exported_tools drifted between manifests"
    );
    let block = tools_block(&b);
    assert_eq!(block.matches("\n- name: ").count(), TOOL_NAMES.len());
    for t in TOOL_NAMES {
        assert!(
            block.contains(&format!("- name: {t}\n")),
            "{t} missing from manifest"
        );
    }
    for m in [&b, &h] {
        assert!(m.contains("net.http.fqdn:api.soundcloud.com"));
        for forbidden in ["client_secret", "Bearer ", "access_token:"] {
            assert!(
                !m.contains(forbidden),
                "{forbidden} must not appear in a manifest"
            );
        }
    }
    // Contract-derived input bounds must be present verbatim (generator keeps them in sync).
    assert!(block.contains("maximum: 25") && block.contains("maxLength: 512"));
}

#[test]
fn tool_names_are_the_contract_tools_for_this_provider() {
    let c = contract();
    let mut want: Vec<String> = c["tools"]
        .as_object()
        .unwrap()
        .keys()
        .map(|k| format!("music.soundcloud.{k}"))
        .collect();
    want.sort();
    let mut got: Vec<String> = TOOL_NAMES.iter().map(|s| s.to_string()).collect();
    got.sort();
    assert_eq!(got, want);
}

fn track_fixture(id: u64) -> Value {
    json!({
        "urn": format!("soundcloud:tracks:{id}"), "title": "T", "duration": 61_000,
        "permalink_url": "https://soundcloud.com/a/t", "access": "playable", "streamable": true,
        "downloadable": true, "license": "cc-by-nc", "sharing": "public",
        "artwork_url": null, "user": {"username": "A", "urn": "soundcloud:users:1"}
    })
}

#[test]
fn outputs_satisfy_the_contract_output_schema() {
    let c = contract();
    let defs = &c["definitions"];
    let up = FakeUpstream::with(vec![
        ok(json!({"collection": [track_fixture(1)]})),
        ok(track_fixture(2)),
        ok(json!([track_fixture(4), track_fixture(5)])),
        status(302, Some("https://api.soundcloud.com/tracks/5"), None),
        ok(track_fixture(5)),
    ]);
    let reg = build_registry(service(up, Arc::new(FakeStore::default())));
    let call = |tool: &str, a: Value| -> Value {
        let name = format!("music.soundcloud.{tool}");
        let out: Value =
            serde_json::from_slice(&reg.call(&name, &serde_json::to_vec(&a).unwrap()).unwrap())
                .unwrap();
        let errs = validate(&c["tools"][tool]["output_schema"], &out, defs, tool);
        assert!(errs.is_empty(), "{tool} violates contract: {errs:?}\n{out}");
        out
    };
    let s = call("search", json!({"query": "x", "limit": 25}));
    assert_eq!(s["tracks"][0]["license"]["creative_commons"], true);
    call("track", json!({"id": "2"}));
    let p = call("playlist.get", json!({"playlist_id": "3", "limit": 1}));
    assert_eq!(p["next_page_token"], "o1");
    call("resolve", json!({"url": "https://soundcloud.com/a/t"}));
}

#[test]
fn contract_valid_inputs_are_accepted_and_invalid_ones_rejected() {
    let c = contract();
    let defs = &c["definitions"];
    let valid = [
        ("search", json!({"query": "x"})),
        ("search", json!({"query": "x", "limit": 25})),
        ("track", json!({"id": "1"})),
        (
            "playlist.get",
            json!({"playlist_id": "1", "limit": 50, "page_token": "o3"}),
        ),
        ("resolve", json!({"url": "https://soundcloud.com/a/b"})),
    ];
    let invalid = [
        ("search", json!({"query": "x", "limit": 26})),
        ("search", json!({"q": "x"})),
        ("search", json!({"query": "x", "offset": 1})),
        ("track", json!({"track": "1"})),
        ("playlist.get", json!({"id": "1"})),
        ("playlist.get", json!({"playlist_id": "1", "limit": 51})),
        ("resolve", json!({"url": ""})),
    ];
    for (tool, args) in &valid {
        assert!(
            validate(&c["tools"][tool]["input_schema"], args, defs, tool).is_empty(),
            "fixture {tool} {args} must be contract-valid"
        );
    }
    for (tool, args) in &invalid {
        assert!(
            !validate(&c["tools"][tool]["input_schema"], args, defs, tool).is_empty(),
            "fixture {tool} {args} must be contract-invalid"
        );
    }
    // Implementation parity: every contract-invalid input is refused by the tool
    // before any upstream call (empty upstream queue + zero calls recorded).
    let up = FakeUpstream::with(vec![]);
    let reg = build_registry(service(up.clone(), Arc::new(FakeStore::default())));
    for (tool, args) in &invalid {
        let name = format!("music.soundcloud.{tool}");
        let r = reg.call(&name, &serde_json::to_vec(args).unwrap());
        assert!(r.is_err(), "{tool} {args} must be rejected");
    }
    assert!(up.calls().is_empty());
    // And every contract-valid input passes argument validation (it may fail
    // later on the empty upstream, but never with invalid-args).
    for (tool, args) in &valid {
        let name = format!("music.soundcloud.{tool}");
        let r = reg.call(&name, &serde_json::to_vec(args).unwrap());
        assert!(
            !matches!(r, Err(waddle_sdk::tool::ToolError::InvalidArgs(_))),
            "{tool} {args} wrongly rejected: {r:?}"
        );
    }
}
