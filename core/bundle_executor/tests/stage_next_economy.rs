//! Issue #714 proof: a REAL compiled `world stage-next` component
//! (`tests/fixtures/economy_fixture.wasm`) loads into the executor's
//! production `Linker`, CALLS every `economy` host import, and each call
//! crosses the real wire bridge to the stage as `capability = db`,
//! `op = "economy.*"` with the exact args shape `core/svc_process` decodes.
//! Only the stage half of the connection is simulated (it answers with a
//! canned host-result or a gate/store-style refusal) -- see `common`.
//!
//! The same fixture is also loaded against a linker built for
//! `WitWorld::Stage` to prove the isolation direction: a `stage` linker
//! refuses a component importing `economy` ("unknown import") before any
//! guest code runs.

#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use bundle_executor::engine::{build_engine, build_linker_for};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use common::{run_one, test_config, StageAnswer, TARGET_USER};

const FIXTURE_WASM: &[u8] = include_bytes!("fixtures/economy_fixture.wasm");
const APP_ID: &str = "waddles.test.economy-fixture";
const OTHER_USER: &str = "66666666-7777-4888-8999-aaaaaaaaaaaa";

async fn run(
    event_type: &'static str,
    payload_json: String,
    answer: StageAnswer,
) -> (String, serde_json::Value, String) {
    run_one(FIXTURE_WASM, APP_ID, 64, event_type, payload_json, answer).await
}

#[tokio::test]
async fn balance_crosses_the_bridge_and_returns_the_balance() {
    let (op, args, result) = run(
        "eco-balance",
        format!(r#"{{"user":"{TARGET_USER}"}}"#),
        StageAnswer::Ok(serde_json::json!({ "balance": 420 })),
    )
    .await;
    assert_eq!(op, "economy.balance");
    assert_eq!(args, serde_json::json!({ "user": TARGET_USER }));
    assert_eq!(result, "ok:420");
}

#[tokio::test]
async fn wager_crosses_the_bridge_with_user_stake_payout_and_returns_the_new_balance() {
    let (op, args, result) = run(
        "eco-wager",
        format!(r#"{{"user":"{TARGET_USER}","stake":10,"payout":25}}"#),
        StageAnswer::Ok(serde_json::json!({ "balance": 115 })),
    )
    .await;
    assert_eq!(op, "economy.wager");
    assert_eq!(
        args,
        serde_json::json!({ "user": TARGET_USER, "stake": 10, "payout": 25 })
    );
    assert_eq!(result, "ok:115");
}

#[tokio::test]
async fn transfer_crosses_the_bridge_with_both_users_and_the_amount() {
    let (op, args, result) = run(
        "eco-transfer",
        format!(r#"{{"from":"{TARGET_USER}","to":"{OTHER_USER}","amount":30}}"#),
        StageAnswer::Ok(serde_json::json!({})),
    )
    .await;
    assert_eq!(op, "economy.transfer");
    assert_eq!(
        args,
        serde_json::json!({ "from": TARGET_USER, "to": OTHER_USER, "amount": 30 })
    );
    assert_eq!(result, "ok");
}

#[tokio::test]
async fn max_bet_and_leaderboard_cross_the_bridge_and_decode_their_payloads() {
    let (op, args, result) = run(
        "eco-max-bet",
        format!(r#"{{"user":"{TARGET_USER}"}}"#),
        StageAnswer::Ok(serde_json::json!({ "max_bet": 50 })),
    )
    .await;
    assert_eq!(op, "economy.max_bet");
    assert_eq!(args, serde_json::json!({ "user": TARGET_USER }));
    assert_eq!(result, "ok:50");

    let (op, args, result) = run(
        "eco-board",
        r#"{"limit":2}"#.to_string(),
        StageAnswer::Ok(serde_json::json!({ "entries": [
            { "user": TARGET_USER, "balance": 300 },
            { "user": OTHER_USER, "balance": 50 },
        ]})),
    )
    .await;
    assert_eq!(op, "economy.leaderboard");
    assert_eq!(args, serde_json::json!({ "limit": 2 }));
    assert_eq!(result, format!("ok:{TARGET_USER}=300,{OTHER_USER}=50"));
}

/// Fail-loud on gate denial: the stage's gate code reaches the guest as a
/// typed `denied(<code>)`, never a swallowed default balance.
#[tokio::test]
async fn gate_denial_surfaces_as_a_typed_denied_error() {
    for (event, payload, code) in [
        (
            "eco-wager",
            format!(r#"{{"user":"{TARGET_USER}","stake":9999,"payout":0}}"#),
            "amount_out_of_bounds",
        ),
        (
            "eco-balance",
            format!(r#"{{"user":"{TARGET_USER}"}}"#),
            "not_granted",
        ),
        (
            "eco-transfer",
            format!(r#"{{"from":"{TARGET_USER}","to":"{OTHER_USER}","amount":1}}"#),
            "quota_exceeded",
        ),
    ] {
        let (_, _, result) = run(event, payload, StageAnswer::Err(code, "refused")).await;
        assert_eq!(result, format!("denied:{code}"));
    }
}

#[tokio::test]
async fn store_refusals_map_to_their_typed_variants_with_their_numbers() {
    let cases: [(&'static str, &'static str, &'static str, &str); 7] = [
        (
            "insufficient_funds",
            "7",
            "eco-wager",
            "insufficient-funds:7",
        ),
        ("over_cap", "50", "eco-wager", "over-cap:50"),
        ("user_not_in_scope", "gate", "eco-wager", "not-a-member"),
        ("not_a_member", "store", "eco-transfer", "not-a-member"),
        (
            "invalid_args",
            "bad amount",
            "eco-wager",
            "invalid:bad amount",
        ),
        (
            "not_implemented",
            "wiring",
            "eco-wager",
            "unavailable:wiring",
        ),
        ("backend", "boom", "eco-wager", "backend:boom"),
    ];
    for (code, message, event, expected) in cases {
        let payload = format!(
            r#"{{"user":"{TARGET_USER}","stake":1,"payout":0,"from":"{TARGET_USER}","to":"{OTHER_USER}","amount":1}}"#
        );
        let (_, _, result) = run(event, payload, StageAnswer::Err(code, message)).await;
        assert_eq!(result, expected, "stage code {code}");
    }
}

#[tokio::test]
async fn a_non_numeric_numeric_refusal_is_a_loud_backend_error() {
    let (_, _, result) = run(
        "eco-wager",
        format!(r#"{{"user":"{TARGET_USER}","stake":1,"payout":0}}"#),
        StageAnswer::Err("insufficient_funds", "lots"),
    )
    .await;
    assert!(
        result.starts_with("backend:malformed insufficient_funds"),
        "{result}"
    );
}

#[tokio::test]
async fn a_malformed_success_payload_is_a_loud_backend_error_not_a_default() {
    let (_, _, result) = run(
        "eco-balance",
        format!(r#"{{"user":"{TARGET_USER}"}}"#),
        StageAnswer::Ok(serde_json::json!({ "unexpected": true })),
    )
    .await;
    assert!(
        result.starts_with("backend:malformed host-result"),
        "{result}"
    );
}

/// Isolation direction: a linker built for `WitWorld::Stage` does not
/// register `economy`, so the stage-next fixture fails to instantiate
/// ("unknown import") before any guest code runs; the `StageNext` linker
/// accepts it.
#[tokio::test]
async fn stage_world_linker_refuses_a_component_importing_economy() {
    let engine = build_engine(&test_config()).expect("engine");
    let component = wasmtime::component::Component::new(&engine, FIXTURE_WASM).expect("component");

    let stage_manifest =
        VerifiedManifest::new(APP_ID, "sha256:aa", WitWorld::Stage, Default::default());
    let stage_linker = build_linker_for(&engine, &stage_manifest).expect("stage linker");
    assert!(
        stage_linker.instantiate_pre(&component).is_err(),
        "a stage-1.0.0 linker must not satisfy the economy import"
    );

    let next_manifest =
        VerifiedManifest::new(APP_ID, "sha256:bb", WitWorld::StageNext, Default::default());
    let next_linker = build_linker_for(&engine, &next_manifest).expect("stage-next linker");
    assert!(
        next_linker.instantiate_pre(&component).is_ok(),
        "a stage-next linker must satisfy the economy import"
    );
}
