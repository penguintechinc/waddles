//! ELEVATED app-caller primitives (`transfer_from` + app mint / non-actor debit).
//!
//! The economy SERVICE bundle holds the broad money grants; an APP bundle reaching
//! it through `tool.invoke` additionally needs ITS OWN admin-approved elevated
//! grant. These run the real gate + the real `handle` dispatch against the
//! recording store double of the parent module; the store's SQL (grant row, caps,
//! audit, atomicity) is proven against real Postgres in
//! `core/bundle_host_economy/tests/postgres_integration.rs`.

use super::*;

type Grants = [(&'static str, serde_json::Value)];

/// The service bundle's own grants (everything it exposes as tools).
fn service_grants() -> Vec<(&'static str, serde_json::Value)> {
    vec![
        ("economy.read", serde_json::json!({})),
        ("economy.credit", serde_json::json!({"max_amount": 500})),
        ("economy.debit", serde_json::json!({"max_amount": 500})),
        ("economy.admin_adjust", serde_json::json!({})),
        ("economy.pot.pay", serde_json::json!({})),
        ("economy.transfer_from", serde_json::json!({})),
    ]
}

/// A fixture whose economy calls arrive as the service, invoked by `CALLER_APP`
/// (holding `caller` grants; `None` = a top-level invocation, no calling app).
fn elevated_fixture(service: &Grants, caller: Option<&Grants>) -> Fixture {
    let mut f = fixture_full(
        grants(service),
        true,
        true,
        Some(("main", COMMUNITY_ID)),
        Facts::Alice,
        caller.map(|c| (CALLER_APP, grants(c))),
    );
    if caller.is_some() {
        f.caps.tool_caller = Some(ToolCaller {
            app_id: CALLER_APP.to_string(),
            app_version: CALLER_VERSION,
        });
    }
    f
}

fn steal_args(f: &Fixture) -> serde_json::Value {
    serde_json::json!({
        "from": f.bob.to_string(), "to": f.alice.to_string(),
        "amount": 25, "reason": "steal"
    })
}

fn ops_of(f: &Fixture) -> Vec<String> {
    f.store
        .calls
        .lock()
        .unwrap()
        .iter()
        .map(|c| c.0.clone())
        .collect()
}

#[tokio::test]
async fn transfer_from_without_a_calling_app_is_an_explicit_denial() {
    // A top-level invocation has no calling app, hence no grant to check.
    let f = elevated_fixture(&service_grants(), None);
    assert_eq!(
        code_of(&f, "economy.transfer_from", steal_args(&f)).await,
        "elevated_not_granted"
    );
    assert_eq!(f.store.call_count(), 0, "nothing reached the store");
}

#[tokio::test]
async fn transfer_from_without_the_callers_grant_is_denied_even_if_the_service_holds_it() {
    // The caller holds only other money grants -- none implies transfer_from.
    let f = elevated_fixture(
        &service_grants(),
        Some(&[
            ("economy.transfer", serde_json::json!({"max_amount": 200})),
            ("economy.credit", serde_json::json!({})),
            ("economy.admin_adjust", serde_json::json!({})),
            ("economy.app_mint", serde_json::json!({})),
        ]),
    );
    assert_eq!(
        code_of(&f, "economy.transfer_from", steal_args(&f)).await,
        "not_granted"
    );
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn transfer_from_needs_the_services_own_grant_too() {
    let service: Vec<_> = service_grants()
        .into_iter()
        .filter(|(id, _)| *id != "economy.transfer_from")
        .collect();
    let f = elevated_fixture(
        &service,
        Some(&[("economy.transfer_from", serde_json::json!({}))]),
    );
    assert_eq!(
        code_of(&f, "economy.transfer_from", steal_args(&f)).await,
        "not_granted"
    );
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn transfer_from_with_both_grants_reaches_the_store_with_host_derived_parties() {
    let f = elevated_fixture(
        &service_grants(),
        Some(&[(
            "economy.transfer_from",
            serde_json::json!({"max_per_op": 50}),
        )]),
    );
    f.store
        .next_transfer_from
        .lock()
        .unwrap()
        .replace(Ok(TransferFromOutcome {
            from_balance: 75,
            to_balance: 125,
            audit_id: 3,
            grant_id: 8,
        }));
    let out = f
        .caps
        .handle(eco_call("economy.transfer_from", steal_args(&f)))
        .await
        .unwrap();
    assert_eq!(out["from_balance"], 75);
    assert_eq!(out["to_balance"], 125);
    assert_eq!(
        f.store.elevated_log(),
        vec![format!("transfer_from:{CALLER_APP}:25:steal")]
    );
    let calls = f.store.calls.lock().unwrap();
    let (op, scope, users, nums) = &calls[0];
    assert_eq!(op, "transfer_from");
    // The ledger scope is the SERVICE's; the calling app travels in the request.
    assert_eq!(*scope, expected_scope());
    assert_eq!(users, &vec![f.bob, f.alice]);
    assert_eq!(nums, &vec![25]);
    drop(calls);
    assert_eq!(
        f.store.keys().len(),
        1,
        "keyed for replay-stable idempotency"
    );
}

#[tokio::test]
async fn transfer_from_refuses_malformed_arguments_before_the_gate() {
    let f = elevated_fixture(
        &service_grants(),
        Some(&[("economy.transfer_from", serde_json::json!({}))]),
    );
    let (bob, alice) = (f.bob.to_string(), f.alice.to_string());
    for args in [
        serde_json::json!({"from": bob, "to": bob, "amount": 5, "reason": "steal"}),
        serde_json::json!({"from": bob, "to": alice, "amount": 0, "reason": "steal"}),
        serde_json::json!({"from": bob, "to": alice, "amount": -1, "reason": "steal"}),
        serde_json::json!({"from": bob, "to": alice, "amount": 5}),
        serde_json::json!({"from": bob, "to": alice, "amount": 5, "reason": ""}),
        serde_json::json!({"from": bob, "to": alice, "amount": 5, "reason": "Free Text PII"}),
        serde_json::json!({"from": "bob", "to": alice, "amount": 5, "reason": "steal"}),
    ] {
        assert_eq!(
            code_of(&f, "economy.transfer_from", args.clone()).await,
            "invalid_args",
            "{args}"
        );
    }
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn transfer_from_non_member_party_is_refused_at_the_gate() {
    let f = elevated_fixture(
        &service_grants(),
        Some(&[("economy.transfer_from", serde_json::json!({}))]),
    );
    let stranger = Uuid::new_v4().to_string();
    let args = serde_json::json!({
        "from": stranger, "to": f.alice.to_string(), "amount": 5, "reason": "steal"
    });
    assert_eq!(
        code_of(&f, "economy.transfer_from", args).await,
        "user_not_in_scope"
    );
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn transfer_from_store_refusals_surface_with_their_stable_codes() {
    let f = elevated_fixture(
        &service_grants(),
        Some(&[("economy.transfer_from", serde_json::json!({}))]),
    );
    for (err, code) in [
        (EconomyError::ElevatedNotGranted, "elevated_not_granted"),
        (EconomyError::ElevatedOverCap { cap: 10 }, "over_cap"),
        (
            EconomyError::ElevatedQuotaExceeded { cap: 10 },
            "quota_exceeded",
        ),
        (EconomyError::IdempotencyConflict, "idempotency_conflict"),
        (
            EconomyError::InsufficientFunds { balance: 3 },
            "insufficient_funds",
        ),
        (EconomyError::NotAMember, "not_a_member"),
    ] {
        f.store.next_transfer_from.lock().unwrap().replace(Err(err));
        assert_eq!(
            code_of(&f, "economy.transfer_from", steal_args(&f)).await,
            code
        );
    }
}

#[tokio::test]
async fn app_credit_without_app_mint_is_denied_and_never_mints() {
    // Regression (security review): tool.call:...adjust_balance(+) alone must not
    // let an app mint.
    let f = elevated_fixture(
        &service_grants(),
        Some(&[("economy.transfer_from", serde_json::json!({}))]),
    );
    assert_eq!(
        code_of(
            &f,
            "economy.credit",
            serde_json::json!({"user": f.alice.to_string(), "amount": 10})
        )
        .await,
        "not_granted"
    );
    assert_eq!(f.store.call_count(), 0, "no mint was attempted");
}

#[tokio::test]
async fn app_debit_of_a_non_actor_without_transfer_from_is_denied_not_actor_mismatch() {
    // Regression (security review): adjust_balance(-) on bob while alice
    // triggered the invocation is an explicit grant denial.
    let f = elevated_fixture(
        &service_grants(),
        Some(&[("economy.app_mint", serde_json::json!({}))]),
    );
    assert_eq!(
        code_of(
            &f,
            "economy.debit",
            serde_json::json!({"user": f.bob.to_string(), "amount": 10})
        )
        .await,
        "not_granted"
    );
    assert_eq!(
        code_of(
            &f,
            "economy.conditional_debit",
            serde_json::json!({"user": f.bob.to_string(), "amount": 10, "min_balance": 0})
        )
        .await,
        "not_granted"
    );
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn app_debit_of_the_actor_stays_on_the_light_actor_bound_path() {
    // No elevated grant at all: debiting the invocation's own actor is the
    // existing, actor-bound behaviour (the user's own wager / spend).
    let f = elevated_fixture(&service_grants(), Some(&[]));
    f.store.answer(Ok(40));
    let out = f
        .caps
        .handle(eco_call(
            "economy.debit",
            serde_json::json!({"user": f.alice.to_string(), "amount": 10}),
        ))
        .await
        .unwrap();
    assert_eq!(out["balance"], 40);
    assert!(
        f.store.elevated_log().is_empty(),
        "no elevated reservation for an actor-bound debit"
    );
}

#[tokio::test]
async fn app_elevated_ops_are_reserved_executed_and_settled() {
    let f = elevated_fixture(
        &service_grants(),
        Some(&[
            ("economy.app_mint", serde_json::json!({})),
            ("economy.transfer_from", serde_json::json!({})),
        ]),
    );
    // Mint: credit -> reserve(AppMint) -> store.credit -> settle(applied).
    f.store.answer(Ok(110));
    let out = f
        .caps
        .handle(eco_call(
            "economy.credit",
            serde_json::json!({"user": f.alice.to_string(), "amount": 10}),
        ))
        .await
        .unwrap();
    assert_eq!(out["balance"], 110);
    // Non-actor debit -> reserve(TransferFrom) -> store.debit -> settle(applied).
    f.store.answer(Ok(5));
    f.caps
        .handle(eco_call(
            "economy.debit",
            serde_json::json!({"user": f.bob.to_string(), "amount": 7}),
        ))
        .await
        .unwrap();
    assert_eq!(
        f.store.elevated_log(),
        vec![
            format!("reserve:{CALLER_APP}:economy.app_mint:credit:10"),
            "settle:77:true".to_string(),
            format!("reserve:{CALLER_APP}:economy.transfer_from:debit:7"),
            "settle:77:true".to_string(),
        ]
    );
    assert_eq!(
        ops_of(&f),
        ["elevated_reserve", "credit", "elevated_reserve", "debit"]
    );
}

#[tokio::test]
async fn every_mint_class_op_needs_app_mint_when_called_by_an_app() {
    let f = elevated_fixture(
        &service_grants(),
        Some(&[("economy.transfer_from", serde_json::json!({}))]),
    );
    let alice = f.alice.to_string();
    for (op, args) in [
        (
            "economy.credit",
            serde_json::json!({"user": alice, "amount": 5}),
        ),
        (
            "economy.batch_credit",
            serde_json::json!({"entries": [{"user": alice, "amount": 5}]}),
        ),
        (
            "economy.admin_adjust",
            serde_json::json!({"user": alice, "mode": "set", "value": 99}),
        ),
        (
            "economy.admin_adjust",
            serde_json::json!({"user": alice, "mode": "delta", "value": -5}),
        ),
        (
            "economy.pay_pot",
            serde_json::json!({"winners": [{"user": alice, "amount": 5}]}),
        ),
        (
            "economy.exchange",
            serde_json::json!({"user": alice, "from_amount": 5, "rate_num": 2, "rate_den": 1}),
        ),
    ] {
        assert_eq!(
            code_of(&f, op, args.clone()).await,
            "not_granted",
            "{op} {args}"
        );
    }
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn a_failed_elevated_op_frees_its_reservation_but_a_backend_failure_keeps_it() {
    let f = elevated_fixture(
        &service_grants(),
        Some(&[("economy.app_mint", serde_json::json!({}))]),
    );
    let args = serde_json::json!({"user": f.alice.to_string(), "amount": 10});
    // Definitive refusal -> settle(false): the budget is released.
    f.store
        .answer(Err(EconomyError::SupplyCapReached { cap: 9 }));
    assert_eq!(
        code_of(&f, "economy.credit", args.clone()).await,
        "over_cap"
    );
    // Indeterminate backend failure -> NOT settled: stays counted (conservative).
    f.store.answer(Err(EconomyError::Backend("boom".into())));
    assert_eq!(code_of(&f, "economy.credit", args).await, "backend");
    assert_eq!(
        f.store.elevated_log(),
        vec![
            format!("reserve:{CALLER_APP}:economy.app_mint:credit:10"),
            "settle:77:false".to_string(),
            format!("reserve:{CALLER_APP}:economy.app_mint:credit:10"),
        ]
    );
}

#[tokio::test]
async fn a_refused_reservation_stops_the_primitive_from_running() {
    let f = elevated_fixture(
        &service_grants(),
        Some(&[("economy.app_mint", serde_json::json!({}))]),
    );
    f.store
        .next_reserve
        .lock()
        .unwrap()
        .replace(Err(EconomyError::ElevatedQuotaExceeded { cap: 1000 }));
    assert_eq!(
        code_of(
            &f,
            "economy.credit",
            serde_json::json!({"user": f.alice.to_string(), "amount": 10})
        )
        .await,
        "quota_exceeded"
    );
    assert_eq!(ops_of(&f), ["elevated_reserve"], "the mint never ran");
}

#[tokio::test]
async fn a_top_level_invocation_keeps_the_existing_unelevated_behaviour() {
    // No calling app (an app calling the host capability directly under its own
    // grants): unchanged -- no reservation, the bundle's own grant governs.
    let f = elevated_fixture(&service_grants(), None);
    f.store.answer(Ok(60));
    f.caps
        .handle(eco_call(
            "economy.credit",
            serde_json::json!({"user": f.alice.to_string(), "amount": 10}),
        ))
        .await
        .unwrap();
    assert!(f.store.elevated_log().is_empty());
}
