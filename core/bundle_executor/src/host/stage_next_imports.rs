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
