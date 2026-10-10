//! Issue #726 foundation proof: a REAL compiled `world stage-next` component
//! (`tests/fixtures/reputation_fixture.wasm`) loads into the executor's
//! production `Linker`, CALLS the new `reputation` host import, and the call
//! crosses the real wire bridge to the stage as `capability = db`,
//! `op = "reputation.*"` with the exact args shape `core/svc_process`
//! decodes. Only the stage half of the connection is simulated (it answers
//! with a canned host-result or a gate-style denial) -- see `common`.
//!
//! The same fixture is also loaded against a linker built for
//! `WitWorld::Stage` to prove the isolation direction: a `stage` linker
//! (`build_linker_for` + `WitWorld::Stage`) refuses a component importing
//! `reputation` ("unknown import") before any guest code runs.

#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use bundle_executor::engine::{build_engine, build_linker_for};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use common::{run_one, test_config, StageAnswer, TARGET_USER};

const FIXTURE_WASM: &[u8] = include_bytes!("fixtures/reputation_fixture.wasm");
const APP_ID: &str = "waddles.test.reputation-fixture";

async fn run(
    event_type: &'static str,
    payload_json: String,
    answer: StageAnswer,
) -> (String, serde_json::Value, String) {
    run_one(FIXTURE_WASM, APP_ID, 64, event_type, payload_json, answer).await
}

#[tokio::test]
async fn get_crosses_the_bridge_and_returns_the_balance() {
    let (op, args, result) = run(
        "rep-get",
        format!(r#"{{"user":"{TARGET_USER}"}}"#),
        StageAnswer::Ok(serde_json::json!({ "balance": 42 })),
    )
    .await;
    assert_eq!(op, "reputation.get");
    assert_eq!(args, serde_json::json!({ "user": TARGET_USER }));
    assert_eq!(result, "ok:42");
}

#[tokio::test]
async fn adjust_crosses_the_bridge_with_user_delta_reason_and_returns_new_balance() {
    let (op, args, result) = run(
        "rep-adjust",
        format!(r#"{{"user":"{TARGET_USER}","delta":-5,"reason":"game.loss"}}"#),
        StageAnswer::Ok(serde_json::json!({ "balance": 37 })),
    )
    .await;
    assert_eq!(op, "reputation.adjust");
    assert_eq!(
        args,
        serde_json::json!({ "user": TARGET_USER, "delta": -5, "reason": "game.loss" })
    );
    assert_eq!(result, "ok:37");
}

/// Fail-loud on gate denial: the stage's gate code reaches the guest as a
/// typed `denied(<code>)`, never a swallowed default balance.
#[tokio::test]
async fn gate_denial_surfaces_as_a_typed_denied_error() {
    let (_, _, result) = run(
        "rep-adjust",
        format!(r#"{{"user":"{TARGET_USER}","delta":999,"reason":"x"}}"#),
        StageAnswer::Err("delta_out_of_bounds", "delta outside declared bounds"),
    )
    .await;
    assert_eq!(result, "denied:delta_out_of_bounds");

    let (_, _, result) = run(
        "rep-get",
        format!(r#"{{"user":"{TARGET_USER}"}}"#),
        StageAnswer::Err("not_granted", "reputation.read not granted"),
    )
    .await;
    assert_eq!(result, "denied:not_granted");
}

#[tokio::test]
async fn membership_and_cap_and_unavailable_errors_map_to_their_variants() {
    let cases: [(&'static str, &'static str, &str); 4] = [
        ("user_not_in_scope", "gate", "not-a-member"),
        ("not_a_member", "store", "not-a-member"),
        ("daily_cap_exceeded", "store", "daily-cap-exceeded"),
        ("not_implemented", "wiring", "unavailable:wiring"),
    ];
    for (code, message, expected) in cases {
        let (_, _, result) = run(
            "rep-adjust",
            format!(r#"{{"user":"{TARGET_USER}","delta":1,"reason":"r"}}"#),
            StageAnswer::Err(code, message),
        )
        .await;
        assert_eq!(result, expected, "stage code {code}");
    }
}

/// Isolation direction: a linker built for `WitWorld::Stage` does not
/// register `reputation`, so the stage-next fixture fails to instantiate
/// ("unknown import") before any guest code runs; the `StageNext` linker
/// accepts it.
#[tokio::test]
async fn stage_world_linker_refuses_a_component_importing_reputation() {
    let engine = build_engine(&test_config()).expect("engine");
    let component = wasmtime::component::Component::new(&engine, FIXTURE_WASM).expect("component");

    let stage_manifest =
        VerifiedManifest::new(APP_ID, "sha256:aa", WitWorld::Stage, Default::default());
    let stage_linker = build_linker_for(&engine, &stage_manifest).expect("stage linker");
    assert!(
        stage_linker.instantiate_pre(&component).is_err(),
        "a stage-1.0.0 linker must not satisfy the reputation import"
    );

    let next_manifest =
        VerifiedManifest::new(APP_ID, "sha256:bb", WitWorld::StageNext, Default::default());
    let next_linker = build_linker_for(&engine, &next_manifest).expect("stage-next linker");
    assert!(
        next_linker.instantiate_pre(&component).is_ok(),
        "a stage-next linker must satisfy the reputation import"
    );
}
