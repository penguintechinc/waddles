//! Host side of the `stage-tools` world's `tool` import (P3b). The guest-facing
//! `tool.invoke` is ENCODED and FORWARDED to the stage as a host call
//! (`capability = db`, `op = "tool.invoke"`, the op-prefix pattern of
//! [`super::stage_next_economy`]); the stage is the sole gate/resolve
//! authority. Nothing here decides authority, and any failure surfaces as a
//! typed `tool-error` -- never a fabricated success.
//!
//! Wire contract (pinned): the host-call args are canonical JSON
//! `{"target", "tool", "args": <json>, "idempotency_key": <string|null>}`; the
//! stage answers `{"ok": <json>}` or `{"tool_error": <ToolError>}`.

use penguin_bundle_host::wire::CapabilityKind;

use crate::engine::stage_tools_world::waddle::bundle::tool;
use crate::error::ExecutorError;
use crate::host::imports::call;
use crate::host::ExecState;

/// Maps a stage refusal code onto the WIT `tool-error`. Unknown codes fail
/// closed as `not-granted`; transport failures are `target-unavailable`.
fn tool_error_from(err: ExecutorError) -> tool::ToolError {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "forbidden" => tool::ToolError::Forbidden,
            "timeout" => tool::ToolError::Timeout,
            "depth_exceeded" => tool::ToolError::DepthExceeded,
            "pool_exhausted" => tool::ToolError::PoolExhausted,
            "target_unavailable" | "not_implemented" | "unavailable" => {
                tool::ToolError::TargetUnavailable
            }
            "invalid_args" => tool::ToolError::InvalidArgs(message.clone()),
            _ => tool::ToolError::NotGranted,
        },
        _ => tool::ToolError::TargetUnavailable,
    }
}

/// Decodes the stage's `{ok}` / `{tool_error}` envelope into the guest's
/// canonical-JSON result bytes or a typed `tool-error`.
fn decode_envelope(mut value: serde_json::Value) -> Result<Vec<u8>, tool::ToolError> {
    if let Some(ok) = value.get_mut("ok").map(serde_json::Value::take) {
        return serde_json::to_vec(&ok)
            .map_err(|e| tool::ToolError::InvalidArgs(format!("unencodable tool result: {e}")));
    }
    if let Some(te) = value.get_mut("tool_error").map(serde_json::Value::take) {
        return Err(serde_json::from_value::<tool::ToolError>(te)
            .unwrap_or(tool::ToolError::TargetUnavailable));
    }
    tracing::warn!("tool.invoke: malformed host-result envelope");
    Err(tool::ToolError::TargetUnavailable)
}

impl tool::Host for ExecState {
    /// Forwards `tool.invoke` to the stage as `Db:"tool.invoke"`. Args must be
    /// UTF-8 JSON bytes (else `invalid-args`); logs are PII-free (target/tool
    /// only, never args).
    async fn invoke(
        &mut self,
        target: String,
        tool_name: String,
        args: Vec<u8>,
        idempotency_key: Option<String>,
    ) -> Result<Vec<u8>, tool::ToolError> {
        let args_json: serde_json::Value = serde_json::from_slice(&args)
            .map_err(|e| tool::ToolError::InvalidArgs(format!("args are not JSON: {e}")))?;
        tracing::debug!(target = %target, tool = %tool_name, "tool.invoke forwarded to stage");
        let payload = serde_json::json!({
            "target": target,
            "tool": tool_name,
            "args": args_json,
            "idempotency_key": idempotency_key,
        });
        match call(self, CapabilityKind::Db, "tool.invoke", payload).await {
            Ok(value) => decode_envelope(value),
            Err(e) => Err(tool_error_from(e)),
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use std::sync::Arc;

    use penguin_bundle_host::wire::{Frame, HostResultBody, HostResultError, Message};
    use tokio::sync::{mpsc, oneshot};

    use super::*;
    use crate::host::HostBridge;
    use crate::wire::Connection;

    /// An `ExecState` whose stage answers the next host call with `answer`,
    /// plus a receiver for the `(capability, op, args)` it observed.
    fn state_with_stage(
        answer: Result<serde_json::Value, (&'static str, &'static str)>,
    ) -> (
        ExecState,
        oneshot::Receiver<(String, String, serde_json::Value)>,
    ) {
        let (tx, mut rx) = mpsc::channel::<Frame>(8);
        let connection = Connection::new(tx);
        let stage = Arc::clone(&connection);
        let (seen_tx, seen_rx) = oneshot::channel();
        tokio::spawn(async move {
            if let Some(frame) = rx.recv().await {
                if let Message::HostCall(body) = &frame.message {
                    let _ = seen_tx.send((
                        format!("{:?}", body.capability),
                        body.op.clone(),
                        body.args.clone(),
                    ));
                }
                let reply = match answer {
                    Ok(v) => HostResultBody {
                        result: Some(v),
                        error: None,
                    },
                    Err((code, message)) => HostResultBody {
                        result: None,
                        error: Some(HostResultError {
                            code: code.to_string(),
                            message: message.to_string(),
                        }),
                    },
                };
                let _ = stage.deliver(Frame::new(frame.id, Message::HostResult(reply)));
            }
        });
        let state = ExecState::new(
            Some(HostBridge::new(connection)),
            "waddles.test.caller".to_string(),
            7,
        );
        (state, seen_rx)
    }

    #[tokio::test]
    async fn invoke_forwards_encoded_payload_as_db_tool_invoke() {
        let (mut st, seen) = state_with_stage(Ok(serde_json::json!({"ok": {"balance": 5}})));
        let out = tool::Host::invoke(
            &mut st,
            "waddles.core.economy".to_string(),
            "get_balance".to_string(),
            br#"{"user":"u1"}"#.to_vec(),
            Some("k".to_string()),
        )
        .await
        .unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&out).unwrap(),
            serde_json::json!({"balance": 5})
        );
        let (cap, op, args) = seen.await.unwrap();
        assert_eq!((cap.as_str(), op.as_str()), ("Db", "tool.invoke"));
        assert_eq!(
            args,
            serde_json::json!({
                "target": "waddles.core.economy",
                "tool": "get_balance",
                "args": {"user": "u1"},
                "idempotency_key": "k",
            })
        );
    }

    #[tokio::test]
    async fn missing_idempotency_key_encodes_as_null() {
        let (mut st, seen) = state_with_stage(Ok(serde_json::json!({"ok": null})));
        tool::Host::invoke(&mut st, "t".into(), "x".into(), b"{}".to_vec(), None)
            .await
            .unwrap();
        let (_, _, args) = seen.await.unwrap();
        assert_eq!(args["idempotency_key"], serde_json::Value::Null);
    }

    #[tokio::test]
    async fn non_json_args_are_invalid_args_and_never_reach_the_stage() {
        let (mut st, mut seen) = state_with_stage(Ok(serde_json::json!({"ok": 1})));
        let err = tool::Host::invoke(&mut st, "t".into(), "x".into(), b"not json".to_vec(), None)
            .await
            .unwrap_err();
        assert!(matches!(err, tool::ToolError::InvalidArgs(_)));
        assert!(seen.try_recv().is_err(), "stage must not be called");
    }

    #[tokio::test]
    async fn stage_tool_error_envelope_maps_to_typed_error() {
        let (mut st, _s) = state_with_stage(Ok(serde_json::json!({"tool_error": "Forbidden"})));
        let err = tool::Host::invoke(&mut st, "t".into(), "x".into(), b"{}".to_vec(), None)
            .await
            .unwrap_err();
        assert!(matches!(err, tool::ToolError::Forbidden));
    }

    #[tokio::test]
    async fn stage_refusal_codes_map_and_unknown_fails_closed() {
        for (code, want) in [
            ("depth_exceeded", "ToolError::DepthExceeded"),
            ("pool_exhausted", "ToolError::PoolExhausted"),
            ("something_new", "ToolError::NotGranted"),
        ] {
            let (mut st, _s) = state_with_stage(Err((code, "m")));
            let err = tool::Host::invoke(&mut st, "t".into(), "x".into(), b"{}".to_vec(), None)
                .await
                .unwrap_err();
            assert_eq!(format!("{err:?}"), want);
        }
    }

    #[tokio::test]
    async fn malformed_envelope_and_no_bridge_fail_closed() {
        let (mut st, _s) = state_with_stage(Ok(serde_json::json!({"weird": 1})));
        let err = tool::Host::invoke(&mut st, "t".into(), "x".into(), b"{}".to_vec(), None)
            .await
            .unwrap_err();
        assert!(matches!(err, tool::ToolError::TargetUnavailable));
        let mut bare = ExecState::new(None, "a".into(), 1);
        let err = tool::Host::invoke(&mut bare, "t".into(), "x".into(), b"{}".to_vec(), None)
            .await
            .unwrap_err();
        assert!(matches!(err, tool::ToolError::TargetUnavailable));
    }
}
