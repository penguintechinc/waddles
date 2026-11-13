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
