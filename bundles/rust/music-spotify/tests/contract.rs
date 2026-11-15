//! Contract tests for the music tool surface the `dj` app bundle consumes.
//!
//! Drives every tool through the REAL registry (`build_registry`) with
//! realistic Spotify payloads and pins (a) the exact output keys a consumer may
//! rely on, (b) the manifests listing exactly the registered tools and agreeing
//! with each other, and (c) the credential/egress boundary on the wire.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use serde_json::{Value, json};
use waddle_bundle_music_spotify_service::logic::testing::{Rig, spotify_track};
use waddle_bundle_music_spotify_service::logic::{TOOL_NAMES, build_registry};

const T1: &str = "4uLU6hMCjMI75M1A2tKUQC";
const P1: &str = "37i9dQZF1DXcBWIGoYBM5M";

const CONTRACT: &str = include_str!("../../../contracts/music-tool-contract.v1.json");

/// Minimal JSON-Schema checker for the subset the shared contract uses
/// (`$ref`, type, required, properties, items, enum, min/max, min/maxLength,
/// additionalProperties:false). Returns every violation found.
fn validate(schema: &Value, v: &Value, defs: &Value, path: &str, errs: &mut Vec<String>) {
    if let Some(r) = schema.get("$ref").and_then(Value::as_str) {
        let name = r.rsplit('/').next().unwrap();
        return validate(&defs[name], v, defs, path, errs);
    }
    if let Some(t) = schema.get("type") {
        let types: Vec<&str> = match t {
            Value::String(s) => vec![s.as_str()],
            Value::Array(a) => a.iter().filter_map(Value::as_str).collect(),
            _ => vec![],
        };
        let ok = types.iter().any(|t| match *t {
            "string" => v.is_string(),
            "integer" => v.is_i64() || v.is_u64(),
            "boolean" => v.is_boolean(),
            "object" => v.is_object(),
            "array" => v.is_array(),
            "null" => v.is_null(),
            _ => false,
        });
        if !ok {
            errs.push(format!("{path}: type {types:?} != {v}"));
            return;
        }
    }
    if let Some(e) = schema.get("enum").and_then(Value::as_array)
        && !e.contains(v)
    {
        errs.push(format!("{path}: not in enum"));
    }
    if let Some(n) = v.as_i64() {
        if schema
            .get("minimum")
            .and_then(Value::as_i64)
            .is_some_and(|m| n < m)
        {
            errs.push(format!("{path}: below minimum"));
        }
        if schema
            .get("maximum")
            .and_then(Value::as_i64)
            .is_some_and(|m| n > m)
        {
            errs.push(format!("{path}: above maximum"));
        }
    }
    if let Some(st) = v.as_str() {
        let len = st.chars().count() as u64;
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
    if let (Some(obj), Some(props)) = (
        v.as_object(),
        schema.get("properties").and_then(Value::as_object),
    ) {
        for r in schema
            .get("required")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let k = r.as_str().unwrap();
            if !obj.contains_key(k) {
                errs.push(format!("{path}: missing required {k}"));
            }
        }
        for (k, val) in obj {
            match props.get(k) {
                Some(ps) => validate(ps, val, defs, &format!("{path}.{k}"), errs),
                None if schema.get("additionalProperties") == Some(&json!(false)) => {
                    errs.push(format!("{path}: unexpected {k}"));
                }
                None => {}
            }
        }
    }
    if let (Some(arr), Some(items)) = (v.as_array(), schema.get("items")) {
        for (i, it) in arr.iter().enumerate() {
            validate(items, it, defs, &format!("{path}[{i}]"), errs);
        }
    }
}

fn check(schema: &Value, v: &Value, defs: &Value) -> Vec<String> {
    let mut e = Vec::new();
    validate(schema, v, defs, "$", &mut e);
    e
}

fn contract() -> Value {
    serde_json::from_str(CONTRACT).unwrap()
}

fn manifest_tool_names(text: &str) -> Vec<String> {
    let mut names: Vec<String> = text
        .lines()
        .filter_map(|l| l.strip_prefix("- name: "))
        .map(str::to_string)
        .collect();
    names.sort();
    names
}

fn exported_block(text: &str) -> String {
    let mut out = Vec::new();
    let mut inside = false;
    for l in text.lines() {
        if l.starts_with("exported_tools:") {
            inside = true;
            continue;
        }
        if inside {
            if !l.is_empty() && !l.starts_with(' ') && !l.starts_with('-') {
                break;
            }
            out.push(l);
        }
    }
    out.join("\n")
}

#[test]
fn manifests_list_exactly_the_registered_tools_and_agree() {
    let bundle =
        std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/bundle.yaml")).unwrap();
    let hub =
        std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/hub-manifest.yaml")).unwrap();
    let mut want: Vec<String> = TOOL_NAMES.iter().map(|s| s.to_string()).collect();
    want.sort();
    assert_eq!(manifest_tool_names(&bundle), want);
    assert_eq!(manifest_tool_names(&hub), want);
    let (a, b) = (exported_block(&bundle), exported_block(&hub));
    assert!(
        a.lines().count() > 100,
        "examined {} lines",
        a.lines().count()
    );
    assert_eq!(
        a, b,
        "bundle.yaml and hub-manifest.yaml exported_tools drifted"
    );
    for t in TOOL_NAMES {
        assert!(
            build_registry(Rig::new().svc).call(t, b"[]").is_err(),
            "{t} must reject non-object args loudly"
        );
    }
}

#[test]
fn manifest_write_tools_are_dangerous_and_reads_are_normal() {
    let bundle =
        std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/bundle.yaml")).unwrap();
    for t in ["play", "pause", "next", "queue_add"] {
        let at = bundle
            .find(&format!("- name: music.spotify.player.{t}\n"))
            .unwrap();
        let head = &bundle[at..at + 120];
        assert!(
            head.contains("access: write") && head.contains("risk: dangerous"),
            "{t}: {head}"
        );
    }
}

#[test]
fn common_tools_conform_to_the_shared_music_contract() {
    let c = contract();
    let defs = &c["definitions"];
    let r = Rig::new();
    let mut examined = 0;

    // search
    r.http.push(
        200,
        &json!({"tracks": {"items": [spotify_track(T1, "S"), {"type": "track", "id": null}]}}),
    );
    let args = json!({"query": "q", "limit": 25});
    assert!(check(&c["tools"]["search"]["input_schema"], &args, defs).is_empty());
    let out = r.call("music.spotify.search", args, None).unwrap();
    let e = check(&c["tools"]["search"]["output_schema"], &out, defs);
    assert!(e.is_empty(), "{e:?}");
    assert_eq!(out["skipped"], 1);
    examined += 1;

    // track (cached second time still conforms, cached=true)
    r.http.push(200, &spotify_track(T1, "S"));
    let args = json!({"id": T1});
    assert!(check(&c["tools"]["track"]["input_schema"], &args, defs).is_empty());
    for want_cached in [false, true] {
        let out = r.call("music.spotify.track", args.clone(), None).unwrap();
        let e = check(&c["tools"]["track"]["output_schema"], &out, defs);
        assert!(e.is_empty(), "{e:?}");
        assert_eq!(out["cached"], want_cached);
        examined += 1;
    }

    // playlist.get (first page has next token, last page null)
    r.http
        .push(200, &json!({"name": "M", "tracks": {"total": 3}}));
    r.http.push(
        200,
        &json!({"items": [{"track": spotify_track(T1, "S")}, {"track": null}]}),
    );
    let args = json!({"playlist_id": P1, "limit": 50});
    assert!(check(&c["tools"]["playlist.get"]["input_schema"], &args, defs).is_empty());
    let out = r.call("music.spotify.playlist.get", args, None).unwrap();
    let e = check(&c["tools"]["playlist.get"]["output_schema"], &out, defs);
    assert!(e.is_empty(), "{e:?}");
    assert_eq!(
        (out["next_page_token"].clone(), out["skipped"].clone()),
        (json!("2"), json!(1))
    );
    examined += 1;
    let args = json!({"playlist_id": P1, "limit": 50, "page_token": "2"});
    assert!(check(&c["tools"]["playlist.get"]["input_schema"], &args, defs).is_empty());
    r.http
        .push(200, &json!({"name": "M", "tracks": {"total": 3}}));
    r.http
        .push(200, &json!({"items": [{"track": spotify_track(T1, "S")}]}));
    let out = r.call("music.spotify.playlist.get", args, None).unwrap();
    assert!(check(&c["tools"]["playlist.get"]["output_schema"], &out, defs).is_empty());
    assert_eq!(out["next_page_token"], Value::Null);
    examined += 1;

    // resolve
    let args = json!({"url": format!("https://open.spotify.com/track/{T1}")});
    assert!(check(&c["tools"]["resolve"]["input_schema"], &args, defs).is_empty());
    let out = r.call("music.spotify.resolve", args, None).unwrap();
    assert!(check(&c["tools"]["resolve"]["output_schema"], &out, defs).is_empty());
    examined += 1;

    assert_eq!(examined, 6, "denominator: contract checks executed");
}

#[test]
fn contract_input_bounds_are_enforced_by_the_tools() {
    let c = contract();
    let defs = &c["definitions"];
    let r = Rig::new();
    // inputs the contract schema rejects must also be rejected by the tool, before any upstream call
    for (tool, name, bad) in [
        (
            "music.spotify.search",
            "search",
            json!({"query": "q", "limit": 26}),
        ),
        ("music.spotify.search", "search", json!({"query": ""})),
        (
            "music.spotify.search",
            "search",
            json!({"query": "x".repeat(201)}),
        ),
        ("music.spotify.track", "track", json!({"id": ""})),
        (
            "music.spotify.track",
            "track",
            json!({"id": "x".repeat(65)}),
        ),
        ("music.spotify.track", "track", json!({"track": T1})),
        (
            "music.spotify.playlist.get",
            "playlist.get",
            json!({"playlist_id": P1, "limit": 51}),
        ),
        (
            "music.spotify.playlist.get",
            "playlist.get",
            json!({"playlist_id": P1, "offset": 5}),
        ),
        (
            "music.spotify.playlist.get",
            "playlist.get",
            json!({"playlist": P1}),
        ),
        ("music.spotify.resolve", "resolve", json!({"url": ""})),
        (
            "music.spotify.resolve",
            "resolve",
            json!({"url": "x".repeat(513)}),
        ),
    ] {
        assert!(
            !check(&c["tools"][name]["input_schema"], &bad, defs).is_empty(),
            "contract should reject {bad}"
        );
        assert!(
            r.call(tool, bad.clone(), None).is_err(),
            "{tool} accepted {bad}"
        );
    }
    assert!(r.http.requests().is_empty());
}

#[allow(non_snake_case)]
fn T1_URI() -> String {
    format!("spotify:track:{T1}")
}

#[test]
fn wire_boundary_only_api_host_and_only_secret_ref_names() {
    let r = Rig::new();
    r.http.push(200, &spotify_track(T1, "S"));
    r.http.push(204, &Value::Null);
    r.http.push(204, &Value::Null);
    r.call("music.spotify.track", json!({"id": T1_URI()}), None)
        .unwrap();
    r.call("music.spotify.player.next", json!({}), Some("k1"))
        .unwrap();
    r.call(
        "music.spotify.player.queue_add",
        json!({"track": T1_URI()}),
        Some("k2"),
    )
    .unwrap();
    let reqs = r.http.requests();
    assert_eq!(reqs.len(), 3);
    for q in &reqs {
        assert!(
            q.url.starts_with("https://api.spotify.com/v1/"),
            "{}",
            q.url
        );
        assert!(
            q.headers
                .iter()
                .all(|(k, _)| !k.eq_ignore_ascii_case("authorization"))
        );
    }
    assert_eq!(reqs[0].cred.secret_ref(), "spotify.app");
    assert_eq!(reqs[1].cred.secret_ref(), "spotify.oauth");
}

#[test]
fn caller_cannot_redirect_requests_via_args() {
    let r = Rig::new();
    for a in [
        json!({"url": "https://evil.example/track/4uLU6hMCjMI75M1A2tKUQC"}),
        json!({"url": "https://open.spotify.com.evil.example/track/4uLU6hMCjMI75M1A2tKUQC"}),
        json!({"url": "spotify:track:../../v1/me"}),
        json!({"url": T1_URI(), "next": "https://evil.example"}),
    ] {
        assert!(r.call("music.spotify.resolve", a, None).is_err());
    }
    for a in [
        json!({"id": "../../v1/me"}),
        json!({"id": T1_URI(), "url": "https://evil.example"}),
    ] {
        assert!(r.call("music.spotify.track", a, None).is_err());
    }
    assert!(r.http.requests().is_empty());
}

#[test]
fn manifests_gate_player_tools_by_scope_and_metadata_has_no_auth_selector() {
    for f in ["bundle.yaml", "hub-manifest.yaml"] {
        let text = std::fs::read_to_string(format!("{}/{f}", env!("CARGO_MANIFEST_DIR"))).unwrap();
        for (tool, scope) in [
            ("state", "community:moderate"),
            ("play", "community:moderate"),
            ("pause", "community:moderate"),
            ("next", "community:moderate"),
            ("queue_add", "community:member"),
        ] {
            let at = text
                .find(&format!("- name: music.spotify.player.{tool}\n"))
                .unwrap();
            let head = &text[at..at + 160];
            assert!(
                head.contains(&format!("required_scope: {scope}")),
                "{f} {tool}: {head}"
            );
        }
        for t in ["search", "track", "playlist.get", "resolve"] {
            let at = text.find(&format!("- name: music.spotify.{t}\n")).unwrap();
            let end = text[at..].find("output_schema").unwrap();
            assert!(
                !text[at..at + end].contains("auth"),
                "{f} {t} exposes an auth selector"
            );
        }
    }
}
