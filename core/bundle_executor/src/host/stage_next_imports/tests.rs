#![allow(clippy::unwrap_used, clippy::expect_used)]

use super::*;
use crate::host::imports::tests::{one_shot_bridge_capturing, state_with};
use penguin_bundle_host::wire::HostResultError;
use serde_json::json;

fn denied(code: &str, message: &str) -> HostResultError {
    HostResultError {
        code: code.to_string(),
        message: message.to_string(),
    }
}

fn exec_denied(code: &str, message: &str) -> ExecutorError {
    ExecutorError::HostCallDenied {
        capability: "db",
        op: "reputation.adjust",
        code: code.to_string(),
        message: message.to_string(),
    }
}

/// Every stage error code lands on its documented WIT `reputation.error` variant; an unknown
/// (gate) code is `denied(<code>)` so a bundle can branch on it, and a transport failure (no
/// bridge, wire error) is `backend`, never a success.
#[test]
fn stage_error_codes_map_onto_the_wit_variants() {
    use reputation::Error as E;
    assert!(matches!(
        reputation_error_from(exec_denied("user_not_in_scope", "")),
        E::NotAMember
    ));
    assert!(matches!(
        reputation_error_from(exec_denied("not_a_member", "")),
        E::NotAMember
    ));
    assert!(matches!(
        reputation_error_from(exec_denied("daily_cap_exceeded", "")),
        E::DailyCapExceeded
    ));
    assert!(matches!(
        reputation_error_from(exec_denied("invalid_args", "bad delta")),
        E::Invalid(m) if m == "bad delta"
    ));
    assert!(matches!(
        reputation_error_from(exec_denied("not_implemented", "off")),
        E::Unavailable(m) if m == "off"
    ));
    assert!(matches!(
        reputation_error_from(exec_denied("feature_disabled", "flag")),
        E::Unavailable(_)
    ));
    assert!(matches!(
        reputation_error_from(exec_denied("backend", "db down")),
        E::Backend(m) if m == "db down"
    ));
    assert!(matches!(
        reputation_error_from(exec_denied("delta_out_of_bounds", "")),
        E::Denied(c) if c == "delta_out_of_bounds"
    ));
    assert!(matches!(
        reputation_error_from(ExecutorError::ConnectionUnavailable),
        E::Backend(_)
    ));
}

#[test]
fn malformed_host_results_are_backend_errors_never_defaults() {
    assert!(matches!(
        decode_balance(json!({"balance": "x"})),
        Err(reputation::Error::Backend(_))
    ));
    assert!(matches!(
        decode_apply(json!({"new_score": 1})),
        Err(reputation::Error::Backend(_))
    ));
    assert!(matches!(
        decode_flag(json!({})),
        Err(reputation::Error::Backend(_))
    ));
    assert!(matches!(
        decode_entries(json!({"entries": [{"user": "u"}]})),
        Err(reputation::Error::Backend(_))
    ));
    assert_eq!(decode_balance(json!({"balance": 7})).unwrap(), 7);
    assert!(decode_flag(json!({"flag": true})).unwrap());
}

#[tokio::test]
async fn get_adjust_and_override_round_trip_and_pin_the_wire_args() {
    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"balance": 650})));
    let mut st = state_with(bridge);
    assert_eq!(
        reputation::Host::get(&mut st, "u1".into()).await.unwrap(),
        650
    );
    assert_eq!(args.await.unwrap(), json!({"user": "u1"}));

    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"balance": 655})));
    let mut st = state_with(bridge);
    let out = reputation::Host::adjust(&mut st, "u1".into(), 5, "game.win".into()).await;
    assert_eq!(out.unwrap(), 655);
    assert_eq!(
        args.await.unwrap(),
        json!({"user": "u1", "delta": 5, "reason": "game.win"})
    );

    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"balance": 700})));
    let mut st = state_with(bridge);
    let out = reputation::Host::override_score(&mut st, "u1".into(), 700, "admin".into()).await;
    assert_eq!(out.unwrap(), 700);
    assert_eq!(
        args.await.unwrap(),
        json!({"user": "u1", "absolute_score": 700, "reason": "admin"})
    );
}

#[tokio::test]
async fn a_denied_write_surfaces_the_mapped_variant() {
    let (bridge, _args) = one_shot_bridge_capturing(Err(denied("daily_cap_exceeded", "cap")));
    let mut st = state_with(bridge);
    let out = reputation::Host::adjust(&mut st, "u".into(), 1, "r".into()).await;
    assert!(matches!(out, Err(reputation::Error::DailyCapExceeded)));

    let mut no_bridge = ExecState::new(None, "waddles.test.app".to_string(), 1);
    let out = reputation::Host::get(&mut no_bridge, "u".into()).await;
    assert!(matches!(out, Err(reputation::Error::Backend(_))));
}

#[tokio::test]
async fn adjust_many_get_many_leaderboard_and_below_threshold_round_trip() {
    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({})));
    let mut st = state_with(bridge);
    let items = vec![
        reputation::Adjustment {
            user: "a".into(),
            delta: 2,
        },
        reputation::Adjustment {
            user: "b".into(),
            delta: -1,
        },
    ];
    reputation::Host::adjust_many(&mut st, items, "batch".into())
        .await
        .unwrap();
    assert_eq!(
        args.await.unwrap(),
        json!({"adjustments": [{"user": "a", "delta": 2}, {"user": "b", "delta": -1}],
               "reason": "batch"})
    );

    let entries = json!({"entries": [{"user": "a", "score": 610}, {"user": "b", "score": 590}]});
    let (bridge, args) = one_shot_bridge_capturing(Ok(entries.clone()));
    let mut st = state_with(bridge);
    let got = reputation::Host::get_many(&mut st, vec!["a".into(), "b".into()])
        .await
        .unwrap();
    assert_eq!(got.len(), 2);
    assert_eq!((got[0].user.as_str(), got[0].score), ("a", 610));
    assert_eq!(args.await.unwrap(), json!({"users": ["a", "b"]}));

    let (bridge, args) = one_shot_bridge_capturing(Ok(entries.clone()));
    let mut st = state_with(bridge);
    let got = reputation::Host::leaderboard(&mut st, 10).await.unwrap();
    assert_eq!(got.len(), 2);
    assert_eq!(args.await.unwrap(), json!({"limit": 10}));

    let (bridge, args) = one_shot_bridge_capturing(Ok(entries));
    let mut st = state_with(bridge);
    let got = reputation::Host::below_threshold(&mut st, 600, 5)
        .await
        .unwrap();
    assert_eq!(got[1].score, 590);
    assert_eq!(args.await.unwrap(), json!({"threshold": 600, "limit": 5}));
}

#[tokio::test]
async fn apply_and_apply_moderation_decode_the_outcome() {
    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"new_score": 605, "applied": true})));
    let mut st = state_with(bridge);
    let out = reputation::Host::apply(&mut st, "u".into(), 5, "r".into())
        .await
        .unwrap();
    assert_eq!((out.new_score, out.applied), (605, true));
    assert_eq!(
        args.await.unwrap(),
        json!({"user": "u", "delta": 5, "reason": "r"})
    );

    let (bridge, _args) =
        one_shot_bridge_capturing(Ok(json!({"new_score": 590, "applied": false})));
    let mut st = state_with(bridge);
    let out = reputation::Host::apply_moderation(&mut st, "u".into(), -10, "spam".into())
        .await
        .unwrap();
    assert_eq!((out.new_score, out.applied), (590, false));
}

#[tokio::test]
async fn tenant_score_policy_and_history_decode() {
    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"score": 480, "communities": 3})));
    let mut st = state_with(bridge);
    let t = reputation::Host::get_tenant(&mut st, "u".into())
        .await
        .unwrap();
    assert_eq!((t.score, t.communities), (480, 3));

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({
        "policy": {"auto_ban_enabled": true, "auto_ban_floor": 350, "at_risk_threshold": 450}
    })));
    let mut st = state_with(bridge);
    let p = reputation::Host::get_tenant_policy(&mut st)
        .await
        .unwrap()
        .expect("policy present");
    assert!(p.auto_ban_enabled);
    assert_eq!((p.auto_ban_floor, p.at_risk_threshold), (350, 450));

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"policy": null})));
    let mut st = state_with(bridge);
    assert!(reputation::Host::get_tenant_policy(&mut st)
        .await
        .unwrap()
        .is_none());

    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"entries": [{
        "id": 9, "delta": 5, "reason_code": "game.win", "kind": "adjust",
        "balance_after": 605, "occurred_at": "2026-11-14T00:00:00Z"
    }]})));
    let mut st = state_with(bridge);
    let h = reputation::Host::history(&mut st, "u".into(), 20, Some(100))
        .await
        .unwrap();
    assert_eq!(h.len(), 1);
    assert_eq!((h[0].id, h[0].delta, h[0].balance_after), (9, 5, Some(605)));
    assert_eq!(h[0].kind, "adjust");
    assert_eq!(
        args.await.unwrap(),
        json!({"user": "u", "limit": 20, "before": 100})
    );
}

#[tokio::test]
async fn soft_ban_ops_decode_the_flag() {
    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"flag": true})));
    let mut st = state_with(bridge);
    assert!(reputation::Host::is_soft_banned(&mut st, "u".into())
        .await
        .unwrap());

    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"flag": true})));
    let mut st = state_with(bridge);
    assert!(
        reputation::Host::soft_ban(&mut st, "u".into(), "floor".into())
            .await
            .unwrap()
    );
    assert_eq!(args.await.unwrap(), json!({"user": "u", "reason": "floor"}));

    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"flag": false})));
    let mut st = state_with(bridge);
    assert!(
        !reputation::Host::lift_soft_ban(&mut st, "u".into(), "floor".into())
            .await
            .unwrap()
    );
    assert_eq!(
        args.await.unwrap(),
        json!({"user": "u", "only_reason": "floor"})
    );
}

/// `overlay` is linked but has no stage handler: every call is a non-retryable
/// `not_implemented` transport error (fail-loud, never a fabricated success).
#[tokio::test]
async fn overlay_calls_fail_loud_with_not_implemented() {
    let mut st = ExecState::new(None, "waddles.test.app".to_string(), 1);
    let widget = overlay::WidgetDescriptor {
        widget_id: "w".into(),
        display_name: "W".into(),
        surface: overlay::Surface::Chat,
        descriptor_json: "{}".into(),
    };
    let e = overlay::Host::register_widget(&mut st, widget)
        .await
        .unwrap_err();
    assert_eq!(e.code, "not_implemented");
    assert!(!e.retryable);
    let e = overlay::Host::push(&mut st, "w".into(), overlay::Surface::Chat, "{}".into())
        .await
        .unwrap_err();
    assert_eq!(e.code, "not_implemented");
}
