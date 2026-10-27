//! `ai.generate` proof: a REAL compiled `world stage-next` component
//! (`tests/fixtures/ai_fixture.wasm`) loads into the executor's production
//! `Linker`, CALLS the `ai` host import, and the call crosses the real wire
//! bridge to the stage as `capability = db`, `op = "ai.generate"` with the exact
//! args shape `core/svc_process` decodes. Only the stage half of the connection
//! is simulated (it answers with a canned host-result or a gate/guard-style
//! refusal) -- see `common`. The executor does no network I/O and no prompt
//! inspection; this proves it is a pure, typed bridge.
//!
//! The same fixture is also loaded against a linker built for
//! `WitWorld::Stage` to prove the isolation direction: a `stage` linker
//! refuses a component importing `ai` ("unknown import") before any guest code
//! runs.

#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use bundle_executor::engine::{build_engine, build_linker_for};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use common::{run_one, test_config, StageAnswer};

const FIXTURE_WASM: &[u8] = include_bytes!("fixtures/ai_fixture.wasm");
const APP_ID: &str = "waddles.test.ai-fixture";

async fn run(payload_json: &str, answer: StageAnswer) -> (String, serde_json::Value, String) {
    run_one(
        FIXTURE_WASM,
        APP_ID,
        64,
        "ai-generate",
        payload_json.to_string(),
        answer,
    )
    .await
}

#[tokio::test]
async fn generate_crosses_the_bridge_with_only_the_four_guest_fields_and_returns_the_text() {
    let (op, args, result) = run(
        r#"{"prompt":"say hello","max_tokens":64,"temperature_x100":25,"json":true}"#,
        StageAnswer::Ok(serde_json::json!({ "text": "Hello, friend!" })),
    )
    .await;
    assert_eq!(op, "ai.generate");
    assert_eq!(
        args,
        serde_json::json!({
            "prompt": "say hello",
            "max_tokens": 64,
            "temperature": 0.25,
            "json": true,
        })
    );
    assert_eq!(result, "ok:Hello, friend!");
}

#[tokio::test]
async fn absent_options_cross_as_nulls_and_json_defaults_false() {
    let (_, args, result) = run(
        r#"{"prompt":"hi"}"#,
        StageAnswer::Ok(serde_json::json!({ "text": "yo" })),
    )
    .await;
    assert_eq!(
        args,
        serde_json::json!({
            "prompt": "hi",
            "max_tokens": null,
            "temperature": null,
            "json": false,
        })
    );
    assert_eq!(result, "ok:yo");
}

/// Fail-loud on gate / content denial: the stage's stable code reaches the guest
/// as a typed `denied(<code>)`, never a swallowed empty completion.
#[tokio::test]
async fn gate_and_content_denials_surface_as_typed_denied_errors() {
    for code in [
        "not_granted",
        "rate_limited",
        "quota_exceeded",
        "instance_denied",
        "contains_pii",
    ] {
        let (_, _, result) = run(r#"{"prompt":"x"}"#, StageAnswer::Err(code, "refused")).await;
        assert_eq!(result, format!("denied:{code}"));
    }
}

#[tokio::test]
async fn stage_refusals_map_to_their_typed_variants() {
    let cases: [(&'static str, &'static str, &str); 8] = [
        ("too_many_tokens", "512", "too-many-tokens:512"),
        ("too_many_tokens", "2000", "too-many-tokens:2000"),
        ("content_filtered", "nothing usable", "content-filtered"),
        ("invalid_args", "bad temperature", "invalid:bad temperature"),
        ("not_implemented", "wiring", "unavailable:wiring"),
        ("feature_disabled", "flag off", "unavailable:flag off"),
        (
            "unavailable",
            "model timed out",
            "unavailable:model timed out",
        ),
        ("backend", "boom", "backend:boom"),
    ];
    for (code, message, expected) in cases {
        let (_, _, result) = run(r#"{"prompt":"x"}"#, StageAnswer::Err(code, message)).await;
        assert_eq!(result, expected, "stage code {code}");
    }
}

#[tokio::test]
async fn a_non_numeric_bound_is_a_loud_backend_error() {
    let (_, _, result) = run(
        r#"{"prompt":"x"}"#,
        StageAnswer::Err("too_many_tokens", "lots"),
    )
    .await;
    assert!(
        result.starts_with("backend:malformed too_many_tokens"),
        "{result}"
    );
}

#[tokio::test]
async fn a_malformed_success_payload_is_a_loud_backend_error_not_an_empty_completion() {
    for bad in [
        serde_json::json!({ "unexpected": true }),
        serde_json::json!({ "text": 7 }),
    ] {
        let (_, _, result) = run(r#"{"prompt":"x"}"#, StageAnswer::Ok(bad)).await;
        assert!(
            result.starts_with("backend:malformed host-result"),
            "{result}"
        );
    }
}

/// Isolation direction: a linker built for `WitWorld::Stage` does not register
/// `ai`, so the stage-next fixture fails to instantiate ("unknown import")
/// before any guest code runs; the `StageNext` linker accepts it.
#[tokio::test]
async fn stage_world_linker_refuses_a_component_importing_ai() {
    let engine = build_engine(&test_config()).expect("engine");
    let component = wasmtime::component::Component::new(&engine, FIXTURE_WASM).expect("component");

    let stage_manifest =
        VerifiedManifest::new(APP_ID, "sha256:aa", WitWorld::Stage, Default::default());
    let stage_linker = build_linker_for(&engine, &stage_manifest).expect("stage linker");
    assert!(
        stage_linker.instantiate_pre(&component).is_err(),
        "a stage-1.0.0 linker must not satisfy the ai import"
    );

    let next_manifest =
        VerifiedManifest::new(APP_ID, "sha256:bb", WitWorld::StageNext, Default::default());
    let next_linker = build_linker_for(&engine, &next_manifest).expect("stage-next linker");
    assert!(
        next_linker.instantiate_pre(&component).is_ok(),
        "a stage-next linker must satisfy the ai import"
    );
}
