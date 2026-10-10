//! Proof (bundle `identity` capability): a REAL compiled `world stage-next`
//! component (`tests/fixtures/identity_fixture.wasm`, a reference points-game
//! bundle) loads into the executor's production `Linker`, CALLS the `identity`
//! host imports, and each call crosses the real wire bridge to the stage as
//! `capability = db`, `op = "identity.resolve_actor" | "identity.resolve_mention"`
//! with the exact args shape `core/svc_process` decodes. Only the stage half of
//! the connection is simulated (it answers with a canned host-result or a
//! gate/store-style refusal) -- see `common`; the real stage handler, gate and
//! Postgres directory are `core/svc_process/tests/identity_pg_e2e.rs`.
//!
//! What this file pins:
//!
//! * the bundle supplies NO argument that selects whose identity is resolved
//!   (`resolve_actor` crosses with `{}`; `resolve_mention` with only the opaque
//!   `token`);
//! * a resolved UUID flows from `identity` straight into `economy` on the wire
//!   (the `!steal` composition), and every refusal short-circuits loudly --
//!   an unlinked actor/target never reaches `economy.transfer`;
//! * the success position can only ever carry a canonical UUID: a stage that
//!   leaked a platform id / handle / placeholder is a loud `backend` error,
//!   never forwarded to the bundle;
//! * the same fixture is refused by a `stage` (1.0.0) linker -- the import
//!   exists only in `stage-next`.
//!
//! Each test loads the component ONCE and runs every scenario as its own invoke
//! on that connection (a wasm compile per scenario would dominate the runtime).

#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use bundle_executor::engine::{build_engine, build_linker_for};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use common::{run_many_detailed, test_config, Scenario, StageAnswer};

const FIXTURE_WASM: &[u8] = include_bytes!("fixtures/identity_fixture.wasm");
const APP_ID: &str = "waddles.test.identity-fixture";

const ALICE: &str = "aaaaaaaa-1111-4222-8333-444444444444";
const BOB: &str = "bbbbbbbb-1111-4222-8333-444444444444";
/// The opaque pseudonym token a bundle was shown in `{user:<token>}` -- NOT a
/// community uuid.
const TOKEN: &str = "cccccccc-5555-4666-8777-888888888888";

fn scenario(event_type: &'static str, payload_json: String, answer: StageAnswer) -> Scenario {
    Scenario {
        event_type,
        payload_json,
        answer,
    }
}

fn token_payload() -> String {
    format!(r#"{{"token":"{TOKEN}"}}"#)
}

#[tokio::test]
async fn resolve_actor_and_mention_cross_the_bridge_with_only_what_the_bundle_may_supply() {
    let observed = run_many_detailed(
        FIXTURE_WASM,
        APP_ID,
        64,
        vec![
            scenario(
                "id-actor",
                "{}".to_string(),
                StageAnswer::Ok(serde_json::json!({ "user": ALICE })),
            ),
            scenario(
                "id-mention",
                token_payload(),
                StageAnswer::Ok(serde_json::json!({ "user": BOB })),
            ),
        ],
    )
    .await;

    // resolve_actor: NO argument -- nothing the bundle could supply selects
    // whose identity is resolved.
    let (calls, result) = &observed[0];
    assert_eq!(
        calls,
        &vec![("identity.resolve_actor".to_string(), serde_json::json!({}))]
    );
    assert_eq!(result, &format!("ok:{ALICE}"));

    // resolve_mention: only the opaque token.
    let (calls, result) = &observed[1];
    assert_eq!(
        calls,
        &vec![(
            "identity.resolve_mention".to_string(),
            serde_json::json!({ "token": TOKEN })
        )]
    );
    assert_eq!(result, &format!("ok:{BOB}"));
}

/// Every refusal the stage can answer reaches the guest as its own typed
/// variant -- never a swallowed default identity.
#[tokio::test]
async fn stage_refusals_map_to_their_typed_variants() {
    let cases: [(&'static str, &'static str, &'static str, &str); 14] = [
        ("not_linked", "x", "id-actor", "not-linked"),
        ("not_a_member", "x", "id-actor", "not-a-member"),
        ("not_found", "x", "id-mention", "not-found"),
        ("ambiguous", "x", "id-mention", "ambiguous"),
        (
            "invalid_args",
            "bad token",
            "id-mention",
            "invalid:bad token",
        ),
        (
            "not_implemented",
            "wiring",
            "id-actor",
            "unavailable:wiring",
        ),
        (
            "feature_disabled",
            "flag off",
            "id-actor",
            "unavailable:flag off",
        ),
        (
            "unavailable",
            "hub down",
            "id-mention",
            "unavailable:hub down",
        ),
        ("backend", "boom", "id-actor", "backend:boom"),
        ("not_granted", "m", "id-actor", "denied:not_granted"),
        ("rate_limited", "m", "id-mention", "denied:rate_limited"),
        ("instance_denied", "m", "id-actor", "denied:instance_denied"),
        (
            "resource_scope_mismatch",
            "m",
            "id-actor",
            "denied:resource_scope_mismatch",
        ),
        (
            "some_future_code",
            "m",
            "id-actor",
            "denied:some_future_code",
        ),
    ];
    let scenarios = cases
        .iter()
        .map(|(code, message, event, _)| {
            scenario(event, token_payload(), StageAnswer::Err(code, message))
        })
        .collect();
    let observed = run_many_detailed(FIXTURE_WASM, APP_ID, 64, scenarios).await;
    for ((code, _, _, expected), (_, result)) in cases.iter().zip(observed) {
        assert_eq!(&result, expected, "stage code {code}");
    }
}

/// PII-safe in the success position: a stage that put a raw platform id, a
/// handle, or a `{user:..}` placeholder where the uuid belongs is surfaced as a
/// loud `backend` error -- the offending value is NOT forwarded to the bundle
/// and NOT echoed in the error. (A missing / non-string `user` is the same loud
/// error, never a default identity.)
#[tokio::test]
async fn a_non_uuid_or_malformed_success_payload_is_never_forwarded_to_the_bundle() {
    let leaked_values = [
        "1001",
        "secret_handle_bob",
        "<@2002>",
        "{user:cccccccc-5555-4666-8777-888888888888}",
        "AAAAAAAA-1111-4222-8333-444444444444", // not canonical (upper-case)
    ];
    let mut scenarios = Vec::new();
    for leaked in leaked_values {
        for (event, payload) in [
            ("id-actor", "{}".to_string()),
            ("id-mention", token_payload()),
        ] {
            scenarios.push(scenario(
                event,
                payload,
                StageAnswer::Ok(serde_json::json!({ "user": leaked })),
            ));
        }
    }
    let leak_scenarios = scenarios.len();
    for bad in [
        serde_json::json!({}),
        serde_json::json!({ "unexpected": true }),
        serde_json::json!({ "user": 7 }),
        serde_json::json!([]),
    ] {
        scenarios.push(scenario("id-actor", "{}".to_string(), StageAnswer::Ok(bad)));
    }

    let observed = run_many_detailed(FIXTURE_WASM, APP_ID, 64, scenarios).await;
    assert_eq!(observed.len(), leak_scenarios + 4);
    for (i, (_, result)) in observed.iter().enumerate() {
        assert!(
            result.starts_with("backend:malformed host-result"),
            "scenario {i}: {result}"
        );
        if i < leak_scenarios {
            let leaked = leaked_values[i / 2];
            assert!(
                !result.contains(leaked),
                "the value must not be echoed: {result}"
            );
        }
    }
}

/// The reference points-game bundle: `!steal <@mention> <amount>`. The UUIDs it
/// moves currency between are EXACTLY the two the stage resolved -- proven on the
/// wire by the `economy.transfer` args -- and the actor/target never come from
/// anything the bundle typed.
#[tokio::test]
async fn steal_flow_feeds_the_resolved_uuids_into_economy_transfer() {
    let observed = run_many_detailed(
        FIXTURE_WASM,
        APP_ID,
        64,
        vec![scenario(
            "id-steal",
            format!(r#"{{"token":"{TOKEN}","amount":30}}"#),
            StageAnswer::PerOp(vec![
                (
                    "identity.resolve_actor",
                    Ok(serde_json::json!({ "user": ALICE })),
                ),
                (
                    "identity.resolve_mention",
                    Ok(serde_json::json!({ "user": BOB })),
                ),
                ("economy.transfer", Ok(serde_json::json!({}))),
            ]),
        )],
    )
    .await;
    let (calls, result) = &observed[0];
    assert_eq!(result, "steal:ok");
    assert_eq!(
        calls,
        &vec![
            ("identity.resolve_actor".to_string(), serde_json::json!({})),
            (
                "identity.resolve_mention".to_string(),
                serde_json::json!({ "token": TOKEN })
            ),
            (
                "economy.transfer".to_string(),
                serde_json::json!({ "from": ALICE, "to": BOB, "amount": 30 })
            ),
        ]
    );
}

/// An unresolved identity is a clear, surfaced "not linked" -- and the bundle
/// stops there: the currency call is never attempted.
#[tokio::test]
async fn an_unlinked_actor_or_target_stops_the_flow_before_any_currency_moves() {
    let payload = || format!(r#"{{"token":"{TOKEN}","amount":30}}"#);
    let actor_ok = || {
        (
            "identity.resolve_actor",
            Ok(serde_json::json!({ "user": ALICE })),
        )
    };
    let scenarios = vec![
        // Unlinked ACTOR: only the first call is made.
        scenario(
            "id-steal",
            payload(),
            StageAnswer::PerOp(vec![(
                "identity.resolve_actor",
                Err(("not_linked", "identity is not linked")),
            )]),
        ),
        // Unlinked TARGET: actor resolved, mention refused, transfer never attempted.
        scenario(
            "id-steal",
            payload(),
            StageAnswer::PerOp(vec![
                actor_ok(),
                (
                    "identity.resolve_mention",
                    Err(("not_linked", "identity is not linked")),
                ),
            ]),
        ),
        // Ambiguous / unknown mentions likewise stop the flow with their own tag.
        scenario(
            "id-steal",
            payload(),
            StageAnswer::PerOp(vec![
                actor_ok(),
                ("identity.resolve_mention", Err(("ambiguous", "m"))),
            ]),
        ),
        scenario(
            "id-steal",
            payload(),
            StageAnswer::PerOp(vec![
                actor_ok(),
                ("identity.resolve_mention", Err(("not_found", "m"))),
            ]),
        ),
    ];
    let observed = run_many_detailed(FIXTURE_WASM, APP_ID, 64, scenarios).await;

    let (calls, result) = &observed[0];
    assert_eq!(result, "actor:not-linked");
    assert_eq!(calls.len(), 1, "no further call after a refusal: {calls:?}");

    for (i, tag) in [
        (1, "target:not-linked"),
        (2, "target:ambiguous"),
        (3, "target:not-found"),
    ] {
        let (calls, result) = &observed[i];
        assert_eq!(result, tag);
        assert_eq!(calls.len(), 2, "transfer must not be attempted: {calls:?}");
        assert!(calls.iter().all(|(op, _)| op != "economy.transfer"));
    }
}

/// Isolation direction: a linker built for `WitWorld::Stage` does not register
/// `identity`, so the stage-next fixture fails to instantiate ("unknown
/// import") before any guest code runs; the `StageNext` linker accepts it.
#[tokio::test]
async fn stage_world_linker_refuses_a_component_importing_identity() {
    let engine = build_engine(&test_config()).expect("engine");
    let component = wasmtime::component::Component::new(&engine, FIXTURE_WASM).expect("component");

    let stage_manifest =
        VerifiedManifest::new(APP_ID, "sha256:aa", WitWorld::Stage, Default::default());
    let stage_linker = build_linker_for(&engine, &stage_manifest).expect("stage linker");
    assert!(
        stage_linker.instantiate_pre(&component).is_err(),
        "a stage-1.0.0 linker must not satisfy the identity import"
    );

    let next_manifest =
        VerifiedManifest::new(APP_ID, "sha256:bb", WitWorld::StageNext, Default::default());
    let next_linker = build_linker_for(&engine, &next_manifest).expect("stage-next linker");
    assert!(
        next_linker.instantiate_pre(&component).is_ok(),
        "a stage-next linker must satisfy the identity import"
    );
}
