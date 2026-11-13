//! `Host` trait implementations for the imports `world stage-next` adds on
//! top of `world stage` (`wit/waddle-bundle/stage.wit`, issue #726):
//! `reputation` (the reference bundle-called host capability) and `overlay`
//! (linked, but its stage-side handler does not exist yet -- see below).
//!
//! `reputation` rides the same [`imports::call`](crate::host::imports::call)
//! round trip as every other capability. `penguin-bundle-host`'s closed
//! `CapabilityKind` has no `reputation` member, so calls are carried as
//! `capability = db`, `op = "reputation.get" | "reputation.adjust"`; the
//! stage (`core/svc_process::capabilities`) dispatches on that op prefix
//! BEFORE its `storage.tables` path and authorizes through the capability
//! gate with a `ReputationScoped` resource. Nothing here decides authority.
//!
//! `overlay` is linked so a `stage-next` component importing it instantiates
//! (instead of failing with an opaque "unknown import"), but no stage handler
//! exists for it (issue #716/#457 follow-up): every call returns a
//! non-retryable `not_implemented` transport error -- fail-loud, never a
//! fabricated success.

use penguin_bundle_host::wire::CapabilityKind;

use crate::engine::stage_next_world::waddle::bundle::{overlay, reputation};
use crate::engine::waddle::bundle::types;
use crate::error::ExecutorError;
use crate::host::imports::call;
use crate::host::ExecState;

/// Wire shape of a successful `reputation.*` host-result.
#[derive(Debug, serde::Deserialize)]
struct BalanceWire {
    balance: i64,
}

fn decode_balance(value: serde_json::Value) -> Result<i64, reputation::Error> {
    serde_json::from_value::<BalanceWire>(value)
        .map(|w| w.balance)
        .map_err(|e| reputation::Error::Backend(format!("malformed host-result: {e}")))
}

/// Maps a stage-side error code onto the WIT `reputation.error` variant.
/// Gate denial codes (`not_granted`, `delta_out_of_bounds`, `quota_exceeded`,
/// `rate_limited`, `instance_denied`, ...) all surface as `denied(<code>)` so
/// a bundle can branch on the stable code; `user_not_in_scope` (gate) and
/// `not_a_member` (store) both mean "target is not in this community".
fn reputation_error_from(err: ExecutorError) -> reputation::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "user_not_in_scope" | "not_a_member" => reputation::Error::NotAMember,
            "daily_cap_exceeded" => reputation::Error::DailyCapExceeded,
            "invalid_args" => reputation::Error::Invalid(message.clone()),
            "not_implemented" | "feature_disabled" => {
                reputation::Error::Unavailable(message.clone())
            }
            "backend" => reputation::Error::Backend(message.clone()),
            other => reputation::Error::Denied(other.to_string()),
        },
        other => reputation::Error::Backend(other.to_string()),
    }
}

impl ExecState {
    /// One `reputation.*` host round trip whose result is decoded by `decode`.
    async fn rep_call<T>(
        &mut self,
        op: &'static str,
        args: serde_json::Value,
        decode: impl FnOnce(serde_json::Value) -> Result<T, reputation::Error>,
    ) -> Result<T, reputation::Error> {
        match call(self, CapabilityKind::Db, op, args).await {
            Ok(value) => decode(value),
            Err(e) => Err(reputation_error_from(e)),
        }
    }
}

fn decode_wire<T: serde::de::DeserializeOwned>(
    value: serde_json::Value,
) -> Result<T, reputation::Error> {
    serde_json::from_value::<T>(value)
        .map_err(|e| reputation::Error::Backend(format!("malformed host-result: {e}")))
}

#[derive(Debug, serde::Deserialize)]
struct ApplyWire {
    new_score: i64,
    applied: bool,
}

#[derive(Debug, serde::Deserialize)]
struct HistoryRowWire {
    id: i64,
    delta: i32,
    reason_code: String,
    kind: String,
    balance_after: Option<i64>,
    occurred_at: String,
}

#[derive(Debug, serde::Deserialize)]
struct HistoryWire {
    entries: Vec<HistoryRowWire>,
}

#[derive(Debug, serde::Deserialize)]
struct TenantScoreWire {
    score: i64,
    communities: u32,
}

#[derive(Debug, serde::Deserialize)]
struct TenantPolicyRowWire {
    auto_ban_enabled: bool,
    auto_ban_floor: i64,
    at_risk_threshold: i64,
}

#[derive(Debug, serde::Deserialize)]
struct TenantPolicyWire {
    policy: Option<TenantPolicyRowWire>,
}

/// Wire shape of the boolean-result ops (`is_soft_banned`, `soft_ban`,
/// `lift_soft_ban`).
#[derive(Debug, serde::Deserialize)]
struct FlagWire {
    flag: bool,
}

fn decode_apply(value: serde_json::Value) -> Result<reputation::ApplyOutcome, reputation::Error> {
    decode_wire::<ApplyWire>(value).map(|w| reputation::ApplyOutcome {
        new_score: w.new_score,
        applied: w.applied,
    })
}

fn decode_flag(value: serde_json::Value) -> Result<bool, reputation::Error> {
    decode_wire::<FlagWire>(value).map(|w| w.flag)
}

impl reputation::Host for ExecState {
    async fn get(&mut self, user: String) -> Result<i64, reputation::Error> {
        let args = serde_json::json!({ "user": user });
        match call(self, CapabilityKind::Db, "reputation.get", args).await {
            Ok(value) => decode_balance(value),
            Err(e) => Err(reputation_error_from(e)),
        }
    }

    async fn adjust(
        &mut self,
        user: String,
        delta: i32,
        reason: String,
    ) -> Result<i64, reputation::Error> {
        let args = serde_json::json!({ "user": user, "delta": delta, "reason": reason });
        match call(self, CapabilityKind::Db, "reputation.adjust", args).await {
            Ok(value) => decode_balance(value),
            Err(e) => Err(reputation_error_from(e)),
        }
    }

    async fn override_score(
        &mut self,
        user: String,
        absolute_score: i64,
        reason: String,
    ) -> Result<i64, reputation::Error> {
        let args = serde_json::json!({
            "user": user, "absolute_score": absolute_score, "reason": reason,
        });
        match call(self, CapabilityKind::Db, "reputation.override", args).await {
            Ok(value) => decode_balance(value),
            Err(e) => Err(reputation_error_from(e)),
        }
    }

    async fn adjust_many(
        &mut self,
        adjustments: Vec<reputation::Adjustment>,
        reason: String,
    ) -> Result<(), reputation::Error> {
        let items: Vec<serde_json::Value> = adjustments
            .into_iter()
            .map(|a| serde_json::json!({ "user": a.user, "delta": a.delta }))
            .collect();
        let args = serde_json::json!({ "adjustments": items, "reason": reason });
        match call(self, CapabilityKind::Db, "reputation.adjust_many", args).await {
            Ok(_) => Ok(()),
            Err(e) => Err(reputation_error_from(e)),
        }
    }

    async fn get_many(
        &mut self,
        users: Vec<String>,
    ) -> Result<Vec<reputation::ScoreEntry>, reputation::Error> {
        let args = serde_json::json!({ "users": users });
        match call(self, CapabilityKind::Db, "reputation.get_many", args).await {
            Ok(value) => decode_entries(value),
            Err(e) => Err(reputation_error_from(e)),
        }
    }

    async fn leaderboard(
        &mut self,
        limit: u32,
    ) -> Result<Vec<reputation::ScoreEntry>, reputation::Error> {
        let args = serde_json::json!({ "limit": limit });
        match call(self, CapabilityKind::Db, "reputation.leaderboard", args).await {
            Ok(value) => decode_entries(value),
            Err(e) => Err(reputation_error_from(e)),
        }
    }

    async fn apply(
        &mut self,
        user: String,
        delta: i32,
        reason: String,
    ) -> Result<reputation::ApplyOutcome, reputation::Error> {
        let args = serde_json::json!({ "user": user, "delta": delta, "reason": reason });
        self.rep_call("reputation.apply", args, decode_apply).await
    }

    async fn apply_moderation(
        &mut self,
        user: String,
        delta: i32,
        reason: String,
    ) -> Result<reputation::ApplyOutcome, reputation::Error> {
        let args = serde_json::json!({ "user": user, "delta": delta, "reason": reason });
        self.rep_call("reputation.apply_moderation", args, decode_apply)
            .await
    }

    async fn get_tenant(
        &mut self,
        user: String,
    ) -> Result<reputation::TenantScore, reputation::Error> {
        let args = serde_json::json!({ "user": user });
        self.rep_call("reputation.get_tenant", args, |v| {
            decode_wire::<TenantScoreWire>(v).map(|w| reputation::TenantScore {
                score: w.score,
                communities: w.communities,
            })
        })
        .await
    }

    async fn get_tenant_policy(
        &mut self,
    ) -> Result<Option<reputation::TenantPolicy>, reputation::Error> {
        self.rep_call("reputation.get_tenant_policy", serde_json::json!({}), |v| {
            decode_wire::<TenantPolicyWire>(v).map(|w| {
                w.policy.map(|p| reputation::TenantPolicy {
                    auto_ban_enabled: p.auto_ban_enabled,
                    auto_ban_floor: p.auto_ban_floor,
                    at_risk_threshold: p.at_risk_threshold,
                })
            })
        })
        .await
    }

    async fn history(
        &mut self,
        user: String,
        limit: u32,
        before: Option<i64>,
    ) -> Result<Vec<reputation::HistoryEntry>, reputation::Error> {
        let args = serde_json::json!({ "user": user, "limit": limit, "before": before });
        self.rep_call("reputation.history", args, |v| {
            decode_wire::<HistoryWire>(v).map(|w| {
                w.entries
                    .into_iter()
                    .map(|e| reputation::HistoryEntry {
                        id: e.id,
                        delta: e.delta,
                        reason_code: e.reason_code,
                        kind: e.kind,
                        balance_after: e.balance_after,
                        occurred_at: e.occurred_at,
                    })
                    .collect()
            })
        })
        .await
    }

    async fn below_threshold(
        &mut self,
        threshold: i64,
        limit: u32,
    ) -> Result<Vec<reputation::ScoreEntry>, reputation::Error> {
        let args = serde_json::json!({ "threshold": threshold, "limit": limit });
        self.rep_call("reputation.below_threshold", args, decode_entries)
            .await
    }

    async fn is_soft_banned(&mut self, user: String) -> Result<bool, reputation::Error> {
        let args = serde_json::json!({ "user": user });
        self.rep_call("reputation.is_soft_banned", args, decode_flag)
            .await
    }

    async fn soft_ban(&mut self, user: String, reason: String) -> Result<bool, reputation::Error> {
        let args = serde_json::json!({ "user": user, "reason": reason });
        self.rep_call("reputation.soft_ban", args, decode_flag)
            .await
    }

    async fn lift_soft_ban(
        &mut self,
        user: String,
        only_reason: String,
    ) -> Result<bool, reputation::Error> {
        let args = serde_json::json!({ "user": user, "only_reason": only_reason });
        self.rep_call("reputation.lift_soft_ban", args, decode_flag)
            .await
    }
}

/// One score row on the wire (`reputation.get_many` / `reputation.leaderboard`).
#[derive(Debug, serde::Deserialize)]
struct EntryWire {
    user: String,
    score: i64,
}

/// Wire shape of a successful `reputation.get_many` / `leaderboard` result.
#[derive(Debug, serde::Deserialize)]
struct EntriesWire {
    entries: Vec<EntryWire>,
}

fn decode_entries(
    value: serde_json::Value,
) -> Result<Vec<reputation::ScoreEntry>, reputation::Error> {
    serde_json::from_value::<EntriesWire>(value)
        .map(|w| {
            w.entries
                .into_iter()
                .map(|e| reputation::ScoreEntry {
                    user: e.user,
                    score: e.score,
                })
                .collect()
        })
        .map_err(|e| reputation::Error::Backend(format!("malformed host-result: {e}")))
}

fn overlay_not_implemented() -> types::TransportError {
    types::TransportError {
        retryable: false,
        code: "not_implemented".to_string(),
        message: "overlay host capability has no stage-side handler yet".to_string(),
        retry_after_ms: None,
    }
}

impl overlay::Host for ExecState {
    async fn register_widget(
        &mut self,
        _widget: overlay::WidgetDescriptor,
    ) -> Result<types::TransportResult, types::TransportError> {
        Err(overlay_not_implemented())
    }

    async fn push(
        &mut self,
        _widget_id: String,
        _surface: overlay::Surface,
        _payload_json: String,
    ) -> Result<types::TransportResult, types::TransportError> {
        Err(overlay_not_implemented())
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;
    use crate::host::imports::tests::{one_shot_bridge_capturing, state_with};
    use penguin_bundle_host::wire::HostResultError;
    use serde_json::json;

    fn refusal(code: &str, message: &str) -> HostResultError {
        HostResultError {
            code: code.to_string(),
            message: message.to_string(),
        }
    }

    fn host_denied(code: &str) -> ExecutorError {
        ExecutorError::HostCallDenied {
            capability: "db",
            op: "reputation.get",
            code: code.to_string(),
            message: "msg".to_string(),
        }
    }

    /// Stage refusal codes map onto the typed WIT `reputation.error` variants.
    #[test]
    fn stage_codes_map_to_typed_reputation_errors() {
        assert!(matches!(
            reputation_error_from(host_denied("user_not_in_scope")),
            reputation::Error::NotAMember
        ));
        assert!(matches!(
            reputation_error_from(host_denied("not_a_member")),
            reputation::Error::NotAMember
        ));
        assert!(matches!(
            reputation_error_from(host_denied("daily_cap_exceeded")),
            reputation::Error::DailyCapExceeded
        ));
        assert!(matches!(
            reputation_error_from(host_denied("invalid_args")),
            reputation::Error::Invalid(m) if m == "msg"
        ));
        for unavailable in ["not_implemented", "feature_disabled"] {
            assert!(matches!(
                reputation_error_from(host_denied(unavailable)),
                reputation::Error::Unavailable(m) if m == "msg"
            ));
        }
        assert!(matches!(
            reputation_error_from(host_denied("backend")),
            reputation::Error::Backend(m) if m == "msg"
        ));
        assert!(matches!(
            reputation_error_from(host_denied("quota_exceeded")),
            reputation::Error::Denied(c) if c == "quota_exceeded"
        ));
        // A non-HostCallDenied failure (transport) is a backend error, never a denial.
        assert!(matches!(
            reputation_error_from(ExecutorError::Config("boom".to_string())),
            reputation::Error::Backend(_)
        ));
    }

    /// Malformed success payloads are a loud `backend` error, never a default.
    #[test]
    fn malformed_payloads_are_loud_backend_errors() {
        assert!(matches!(
            decode_balance(json!({"nope": 1})),
            Err(reputation::Error::Backend(m)) if m.contains("malformed host-result")
        ));
        assert!(matches!(
            decode_entries(json!({"entries": [{"user": 1}]})),
            Err(reputation::Error::Backend(m)) if m.contains("malformed host-result")
        ));
        assert!(matches!(
            decode_apply(json!({"new_score": "x"})),
            Err(reputation::Error::Backend(_))
        ));
        assert!(matches!(
            decode_flag(json!({})),
            Err(reputation::Error::Backend(_))
        ));
        assert_eq!(decode_balance(json!({"balance": 7})).unwrap(), 7);
        assert!(decode_flag(json!({"flag": true})).unwrap());
    }

    /// `get` / `adjust` / `override_score` send exactly the guest's words and
    /// decode the balance.
    #[tokio::test]
    async fn balance_ops_cross_the_bridge_with_their_args() {
        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"balance": 11})));
        let mut st = state_with(bridge);
        assert_eq!(
            reputation::Host::get(&mut st, "u1".into()).await.unwrap(),
            11
        );
        assert_eq!(args.await.unwrap(), json!({"user": "u1"}));

        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"balance": 12})));
        let mut st = state_with(bridge);
        let got = reputation::Host::adjust(&mut st, "u1".into(), -3, "game.loss".into())
            .await
            .unwrap();
        assert_eq!(got, 12);
        assert_eq!(
            args.await.unwrap(),
            json!({"user": "u1", "delta": -3, "reason": "game.loss"})
        );

        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"balance": 500})));
        let mut st = state_with(bridge);
        let got = reputation::Host::override_score(&mut st, "u1".into(), 500, "admin".into())
            .await
            .unwrap();
        assert_eq!(got, 500);
        assert_eq!(
            args.await.unwrap(),
            json!({"user": "u1", "absolute_score": 500, "reason": "admin"})
        );
    }

    /// Batch ops send the adjustments list / user list and decode entries.
    #[tokio::test]
    async fn batch_ops_cross_the_bridge_and_decode_entries() {
        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({})));
        let mut st = state_with(bridge);
        reputation::Host::adjust_many(
            &mut st,
            vec![
                reputation::Adjustment {
                    user: "a".into(),
                    delta: 2,
                },
                reputation::Adjustment {
                    user: "b".into(),
                    delta: -4,
                },
            ],
            "batch.reason".into(),
        )
        .await
        .unwrap();
        assert_eq!(
            args.await.unwrap(),
            json!({"adjustments": [{"user":"a","delta":2},{"user":"b","delta":-4}], "reason": "batch.reason"})
        );

        let rows = json!({"entries": [{"user": "a", "score": 600}, {"user": "b", "score": 590}]});
        let (bridge, args) = one_shot_bridge_capturing(Ok(rows.clone()));
        let mut st = state_with(bridge);
        let got = reputation::Host::get_many(&mut st, vec!["a".into(), "b".into()])
            .await
            .unwrap();
        assert_eq!(args.await.unwrap(), json!({"users": ["a", "b"]}));
        assert_eq!(got.len(), 2);
        assert_eq!((got[0].user.as_str(), got[0].score), ("a", 600));
        assert_eq!((got[1].user.as_str(), got[1].score), ("b", 590));

        let (bridge, args) = one_shot_bridge_capturing(Ok(rows.clone()));
        let mut st = state_with(bridge);
        let got = reputation::Host::leaderboard(&mut st, 10).await.unwrap();
        assert_eq!(args.await.unwrap(), json!({"limit": 10}));
        assert_eq!(got.len(), 2);

        let (bridge, args) = one_shot_bridge_capturing(Ok(rows));
        let mut st = state_with(bridge);
        let got = reputation::Host::below_threshold(&mut st, 400, 5)
            .await
            .unwrap();
        assert_eq!(args.await.unwrap(), json!({"threshold": 400, "limit": 5}));
        assert_eq!(got.len(), 2);
    }

    /// `apply` / `apply_moderation` decode `(new_score, applied)`.
    #[tokio::test]
    async fn apply_ops_decode_the_outcome() {
        for moderation in [false, true] {
            let (bridge, args) =
                one_shot_bridge_capturing(Ok(json!({"new_score": 610, "applied": !moderation})));
            let mut st = state_with(bridge);
            let out = if moderation {
                reputation::Host::apply_moderation(&mut st, "u".into(), 5, "r".into()).await
            } else {
                reputation::Host::apply(&mut st, "u".into(), 5, "r".into()).await
            }
            .unwrap();
            assert_eq!(out.new_score, 610);
            assert_eq!(out.applied, !moderation);
            assert_eq!(
                args.await.unwrap(),
                json!({"user": "u", "delta": 5, "reason": "r"})
            );
        }
    }

    /// Tenant aggregate, policy (present and absent) and history decode.
    #[tokio::test]
    async fn tenant_policy_and_history_decode() {
        let (bridge, _args) =
            one_shot_bridge_capturing(Ok(json!({"score": 640, "communities": 3})));
        let mut st = state_with(bridge);
        let t = reputation::Host::get_tenant(&mut st, "u".into())
            .await
            .unwrap();
        assert_eq!((t.score, t.communities), (640, 3));

        let (bridge, _args) = one_shot_bridge_capturing(Ok(json!({
            "policy": {"auto_ban_enabled": true, "auto_ban_floor": 350, "at_risk_threshold": 450}
        })));
        let mut st = state_with(bridge);
        let p = reputation::Host::get_tenant_policy(&mut st)
            .await
            .unwrap()
            .expect("policy present");
        assert!(p.auto_ban_enabled);
        assert_eq!((p.auto_ban_floor, p.at_risk_threshold), (350, 450));

        let (bridge, _args) = one_shot_bridge_capturing(Ok(json!({"policy": null})));
        let mut st = state_with(bridge);
        assert!(reputation::Host::get_tenant_policy(&mut st)
            .await
            .unwrap()
            .is_none());

        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"entries": [{
            "id": 9, "delta": -2, "reason_code": "game.loss", "kind": "adjust",
            "balance_after": 598, "occurred_at": "2026-11-12T00:00:00Z"
        }]})));
        let mut st = state_with(bridge);
        let h = reputation::Host::history(&mut st, "u".into(), 20, Some(50))
            .await
            .unwrap();
        assert_eq!(
            args.await.unwrap(),
            json!({"user": "u", "limit": 20, "before": 50})
        );
        assert_eq!(h.len(), 1);
        assert_eq!((h[0].id, h[0].delta), (9, -2));
        assert_eq!(h[0].reason_code, "game.loss");
        assert_eq!(h[0].kind, "adjust");
        assert_eq!(h[0].balance_after, Some(598));
        assert_eq!(h[0].occurred_at, "2026-11-12T00:00:00Z");
    }

    /// Soft-ban ops carry their args and decode the boolean flag.
    #[tokio::test]
    async fn soft_ban_ops_decode_the_flag() {
        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"flag": true})));
        let mut st = state_with(bridge);
        assert!(reputation::Host::is_soft_banned(&mut st, "u".into())
            .await
            .unwrap());
        assert_eq!(args.await.unwrap(), json!({"user": "u"}));

        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"flag": false})));
        let mut st = state_with(bridge);
        assert!(
            !reputation::Host::soft_ban(&mut st, "u".into(), "floor".into())
                .await
                .unwrap()
        );
        assert_eq!(args.await.unwrap(), json!({"user": "u", "reason": "floor"}));

        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"flag": true})));
        let mut st = state_with(bridge);
        assert!(
            reputation::Host::lift_soft_ban(&mut st, "u".into(), "floor".into())
                .await
                .unwrap()
        );
        assert_eq!(
            args.await.unwrap(),
            json!({"user": "u", "only_reason": "floor"})
        );
    }

    /// A stage refusal reaches every op as its typed variant, not a default.
    #[tokio::test]
    async fn stage_refusals_surface_through_the_host_methods() {
        let (bridge, _a) =
            one_shot_bridge_capturing(Err(refusal("not_a_member", "not in this community")));
        let mut st = state_with(bridge);
        assert!(matches!(
            reputation::Host::get(&mut st, "u".into()).await,
            Err(reputation::Error::NotAMember)
        ));

        let (bridge, _a) = one_shot_bridge_capturing(Err(refusal("rate_limited", "slow down")));
        let mut st = state_with(bridge);
        assert!(matches!(
            reputation::Host::apply(&mut st, "u".into(), 1, "r".into()).await,
            Err(reputation::Error::Denied(c)) if c == "rate_limited"
        ));

        let (bridge, _a) = one_shot_bridge_capturing(Err(refusal("feature_disabled", "flag off")));
        let mut st = state_with(bridge);
        assert!(matches!(
            reputation::Host::is_soft_banned(&mut st, "u".into()).await,
            Err(reputation::Error::Unavailable(m)) if m == "flag off"
        ));

        // A success body of the wrong shape is a loud backend error end to end.
        let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"unexpected": true})));
        let mut st = state_with(bridge);
        assert!(matches!(
            reputation::Host::get_tenant(&mut st, "u".into()).await,
            Err(reputation::Error::Backend(m)) if m.contains("malformed host-result")
        ));
    }

    /// The linked-but-unhandled `overlay` import fails loudly, never succeeds.
    #[tokio::test]
    async fn overlay_imports_fail_loudly_with_not_implemented() {
        let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({})));
        let mut st = state_with(bridge);
        let err = overlay::Host::push(&mut st, "w1".into(), overlay::Surface::Chat, "{}".into())
            .await
            .expect_err("overlay.push has no stage handler");
        assert_eq!(err.code, "not_implemented");
        assert!(!err.retryable);
        assert_eq!(err.retry_after_ms, None);
    }
}
