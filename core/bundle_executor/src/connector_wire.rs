//! Host-API wire face of the connector world (issue #840 follow-up to B0;
//! `penguin-bundle-host` 0.3.0 `ExportKind::Connector*`).
//!
//! A stage (svc_ingest) asks this process to run one of the four
//! `waddle:connector/receiver` exports with an `invoke` frame whose `export`
//! is a connector [`ExportKind`]. [`ConnectorRouter`] is the registry of
//! always-pinned [`ConnectorHost`]s those frames are routed to, and
//! [`ConnectorRouter::invoke`] is the translation layer: canonical-JSON
//! payload in, a typed `result`/`error` frame out.
//!
//! # Wire contract (canonical JSON, snake_case, unknown fields rejected)
//!
//! | `export` | `payload` | `result.payload` |
//! |---|---|---|
//! | `connector_on_connect` | `{ctx}` | `{bytes, secret_refs:[[slot,handle],..]}` |
//! | `connector_on_frame` | `{ctx, bytes, received_at}` | `{events:[{platform,event_type,actor,payload_json,occurred_at},..]}` |
//! | `connector_on_heartbeat_due` | `{ctx}` | `{bytes}` (`null` = no heartbeat body) |
//! | `connector_on_disconnect` | `{ctx, reason}` | `{}` |
//!
//! `ctx` = `{platform, source_id, connection_id, session_token}`; every
//! `bytes` is standard base64 (frames are binary-safe, never UTF-8 assumed).
//! A guest `frame-error` is a normal guest outcome, not a protocol failure, so
//! it rides a `result` as `{"frame_error":{"kind","message"}}` (same shape of
//! convention as `unsupported_stage` / `transport_error`). Every sandbox
//! containment is a typed `error` frame (`EXECUTOR_DEADLINE` with detail
//! `fuel_exhausted`/`pool_exhausted`, `MEMORY_LIMIT`, `WASM_TRAP`).
//!
//! # Pinning, not `load`
//!
//! Connectors are core-namespaced and always pinned for the process lifetime
//! (spec S2.3), unlike stage bundles which are scope-refcounted by `load`/
//! `unload` and wiped on reconnect. They are registered with
//! [`ConnectorRouter::pin`] (via `Executor::pin_connector`), never over the
//! wire, so a stage can neither load nor evict one. An `invoke` naming a
//! digest/app_id that is not pinned is `UNKNOWN_BUNDLE` -- fail closed, never
//! a fallback to "some other connector".
//!
//! Pinned hosts run with no host-call bridge today: a connector import that
//! needs one (`http`, ...) fails typed `ConnectionUnavailable`. Wiring a
//! per-connection bridge is a later step; Echo-class connectors need none.
//!
//! # Logging
//!
//! Only the app id, export label and error class are logged -- never frame
//! bytes, actors, payloads or guest-supplied error text.

use std::collections::HashMap;
use std::time::Instant;

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine as _;
use penguin_bundle_host::wire::{ErrorBody, ErrorCode, ExportKind, InvokeBody, ResultBody};
use serde::{Deserialize, Serialize};
use tokio::sync::RwLock;
use tracing::{debug, warn};

use crate::connector::{
    CallReply, ConnectionCtx, ConnectorCallError, ConnectorHost, FrameError, HandshakePayload,
    NormalizedEvent, RawFrame,
};
use crate::error::ExecutorError;

/// `ErrorBody::detail` for a connector pool with no free slot (shared with
/// the stage path's `pool_exhausted`).
const DETAIL_POOL_EXHAUSTED: &str = "pool_exhausted";
/// `ErrorBody::detail` for a spent fuel budget (shared with the stage path).
const DETAIL_FUEL_EXHAUSTED: &str = "fuel_exhausted";

/// The always-pinned connector registry, keyed by content digest.
#[derive(Default)]
pub struct ConnectorRouter {
    hosts: RwLock<HashMap<String, ConnectorHost>>,
}

impl ConnectorRouter {
    /// An empty router: every connector invoke is `UNKNOWN_BUNDLE` until a
    /// host is pinned.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Pins `host` for the process lifetime. Re-pinning a digest is an error
    /// (a pinned connector is never replaced or evicted, spec S2.3).
    pub async fn pin(&self, host: ConnectorHost) -> Result<(), ExecutorError> {
        let mut hosts = self.hosts.write().await;
        if hosts.contains_key(host.digest()) {
            return Err(ExecutorError::ConnectorLoad(format!(
                "connector digest {} is already pinned",
                host.digest()
            )));
        }
        hosts.insert(host.digest().to_string(), host);
        Ok(())
    }

    /// Number of pinned connectors.
    pub async fn len(&self) -> usize {
        self.hosts.read().await.len()
    }

    /// True when no connector is pinned.
    pub async fn is_empty(&self) -> bool {
        self.hosts.read().await.is_empty()
    }

    /// Runs one connector export for an `invoke` frame, translating the
    /// canonical-JSON payload to the typed WIT call and the outcome back to a
    /// `result` body or a typed `error` body. Fails closed: an unpinned
    /// digest, an `app_id` that does not match the pinned one, or a
    /// non-connector export are all refused before any guest runs.
    pub async fn invoke(&self, body: InvokeBody) -> Result<ResultBody, ErrorBody> {
        if !body.export.is_connector() {
            return Err(error_body(
                ErrorCode::ExportMissing,
                format!("{} is not a connector export", body.export.as_str()),
            ));
        }
        let host = {
            let hosts = self.hosts.read().await;
            hosts.get(&body.digest).cloned()
        }
        .ok_or_else(|| error_body(ErrorCode::UnknownBundle, body.digest.clone()))?;
        if host.app_id() != body.app_id {
            // The digest is the identity; an app_id that disagrees means the
            // stage is confused about which connector it is talking to.
            return Err(error_body(
                ErrorCode::UnknownBundle,
                format!("digest {} is pinned under a different app_id", body.digest),
            ));
        }

        let export = body.export;
        let app_id = body.app_id.clone();
        let started = Instant::now();
        let outcome = dispatch(&host, export, body.payload).await;
        match &outcome {
            Ok(_) => debug!(app_id = %app_id, export = export.as_str(), "connector invoke ok"),
            Err(e) => warn!(
                app_id = %app_id,
                export = export.as_str(),
                code = ?e.code,
                "connector invoke failed"
            ),
        }
        let (payload, fuel_used) = outcome?;
        Ok(ResultBody {
            payload,
            duration_ms: u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX),
            fuel_used,
        })
    }
}

/// `{platform, source_id, connection_id, session_token}` on the wire.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WireCtx {
    platform: String,
    source_id: String,
    connection_id: String,
    #[serde(default)]
    session_token: Option<String>,
}

impl From<WireCtx> for ConnectionCtx {
    fn from(c: WireCtx) -> Self {
        Self {
            platform: c.platform,
            source_id: c.source_id,
            connection_id: c.connection_id,
            session_token: c.session_token,
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CtxArgs {
    ctx: WireCtx,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FrameArgs {
    ctx: WireCtx,
    bytes: String,
    received_at: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct DisconnectArgs {
    ctx: WireCtx,
    reason: String,
}

#[derive(Debug, Serialize)]
struct HandshakeReply {
    bytes: String,
    secret_refs: Vec<(String, String)>,
}

#[derive(Debug, Serialize)]
struct EventReply {
    platform: String,
    event_type: String,
    actor: Option<String>,
    payload_json: String,
    occurred_at: String,
}

#[derive(Debug, Serialize)]
struct FramesReply {
    events: Vec<EventReply>,
}

#[derive(Debug, Serialize)]
struct HeartbeatReply {
    bytes: Option<String>,
}

#[derive(Debug, Serialize)]
struct FrameErrorReply {
    kind: &'static str,
    message: String,
}

fn error_body(code: ErrorCode, message: impl Into<String>) -> ErrorBody {
    ErrorBody {
        code,
        message: message.into(),
        detail: None,
    }
}

fn malformed(e: impl std::fmt::Display) -> ErrorBody {
    error_body(ErrorCode::MalformedFrame, e.to_string())
}

fn decode<T: for<'de> Deserialize<'de>>(payload: serde_json::Value) -> Result<T, ErrorBody> {
    serde_json::from_value(payload).map_err(malformed)
}

fn decode_bytes(b64: &str) -> Result<Vec<u8>, ErrorBody> {
    BASE64.decode(b64).map_err(malformed)
}

fn encode_reply<T: Serialize>(value: &T) -> Result<serde_json::Value, ErrorBody> {
    serde_json::to_value(value).map_err(|e| {
        // The executor's existing "internal, not a guest fault" bucket.
        error_body(ErrorCode::LoadFailed, format!("result encode failed: {e}"))
    })
}

/// A guest `frame-error` as a normal `result` payload.
fn frame_error_payload(e: FrameError) -> Result<serde_json::Value, ErrorBody> {
    let (kind, message) = match e {
        FrameError::Unsupported(m) => ("unsupported", m),
        FrameError::Malformed(m) => ("malformed", m),
        FrameError::Backend(m) => ("backend", m),
    };
    encode_reply(&serde_json::json!({
        "frame_error": FrameErrorReply { kind, message }
    }))
}

/// Maps a sandbox/host containment onto the typed wire error a stage keys its
/// circuit breaker and DLQ-kind off (never by message text).
fn call_error_to_body(e: ConnectorCallError) -> ErrorBody {
    let message = e.to_string();
    match e {
        // Handled before this is reached; kept total so a future caller
        // cannot silently lose it.
        ConnectorCallError::Guest(_) | ConnectorCallError::Internal(_) => {
            error_body(ErrorCode::LoadFailed, message)
        }
        ConnectorCallError::FuelExhausted(_) => ErrorBody {
            code: ErrorCode::ExecutorDeadline,
            message,
            detail: Some(DETAIL_FUEL_EXHAUSTED.to_string()),
        },
        ConnectorCallError::DeadlineExceeded(_) => error_body(ErrorCode::ExecutorDeadline, message),
        ConnectorCallError::PoolExhausted { .. } => ErrorBody {
            code: ErrorCode::ExecutorDeadline,
            message,
            detail: Some(DETAIL_POOL_EXHAUSTED.to_string()),
        },
        ConnectorCallError::MemoryLimit(_) => error_body(ErrorCode::MemoryLimit, message),
        ConnectorCallError::Trap(_) => error_body(ErrorCode::WasmTrap, message),
        ConnectorCallError::InstantiateFailed(_) => error_body(ErrorCode::LoadFailed, message),
    }
}

/// Settles one call: a guest `frame-error` becomes a `result` payload, any
/// other failure a typed error, success the encoded reply plus fuel used.
fn settle<T>(
    outcome: Result<CallReply<T>, ConnectorCallError>,
    encode: impl FnOnce(T) -> Result<serde_json::Value, ErrorBody>,
) -> Result<(serde_json::Value, u64), ErrorBody> {
    match outcome {
        Ok(reply) => Ok((encode(reply.value)?, reply.fuel_used)),
        Err(ConnectorCallError::Guest(fe)) => Ok((frame_error_payload(fe)?, 0)),
        Err(other) => Err(call_error_to_body(other)),
    }
}

fn encode_handshake(h: HandshakePayload) -> Result<serde_json::Value, ErrorBody> {
    encode_reply(&HandshakeReply {
        bytes: BASE64.encode(h.bytes),
        secret_refs: h.secret_refs,
    })
}

fn encode_events(events: Vec<NormalizedEvent>) -> Result<serde_json::Value, ErrorBody> {
    encode_reply(&FramesReply {
        events: events
            .into_iter()
            .map(|e| EventReply {
                platform: e.platform,
                event_type: e.event_type,
                actor: e.actor,
                payload_json: e.payload_json,
                occurred_at: e.occurred_at,
            })
            .collect(),
    })
}

/// Decodes the payload for `export`, runs the matching `ConnectorHost` call
/// and encodes the outcome.
async fn dispatch(
    host: &ConnectorHost,
    export: ExportKind,
    payload: serde_json::Value,
) -> Result<(serde_json::Value, u64), ErrorBody> {
    match export {
        ExportKind::ConnectorOnConnect => {
            let args: CtxArgs = decode(payload)?;
            settle(host.on_connect(args.ctx.into()).await, encode_handshake)
        }
        ExportKind::ConnectorOnFrame => {
            let args: FrameArgs = decode(payload)?;
            let frame = RawFrame {
                ctx: args.ctx.into(),
                bytes: decode_bytes(&args.bytes)?,
                received_at: args.received_at,
            };
            settle(host.on_frame(frame).await, encode_events)
        }
        ExportKind::ConnectorOnHeartbeatDue => {
            let args: CtxArgs = decode(payload)?;
            settle(host.on_heartbeat_due(args.ctx.into()).await, |bytes| {
                encode_reply(&HeartbeatReply {
                    bytes: bytes.map(|b| BASE64.encode(b)),
                })
            })
        }
        ExportKind::ConnectorOnDisconnect => {
            let args: DisconnectArgs = decode(payload)?;
            settle(
                host.on_disconnect(args.ctx.into(), args.reason).await,
                |()| encode_reply(&serde_json::json!({})),
            )
        }
        // Rejected by `invoke` before dispatch; kept total so adding an
        // export is a compile error here, never a silent drop.
        ExportKind::Transform | ExportKind::Dispatch | ExportKind::ToolCall => Err(error_body(
            ErrorCode::ExportMissing,
            format!("{} is not a connector export", export.as_str()),
        )),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    fn body(export: ExportKind, payload: serde_json::Value) -> InvokeBody {
        InvokeBody {
            app_id: "waddles.core.example.echo".to_string(),
            digest: "sha256:nope".to_string(),
            export,
            payload,
            deadline_ms: 1000,
            trace: None,
        }
    }

    #[tokio::test]
    async fn an_unpinned_digest_is_unknown_bundle_for_every_connector_export() {
        let router = ConnectorRouter::new();
        assert!(router.is_empty().await);
        for export in ExportKind::ALL.into_iter().filter(|e| e.is_connector()) {
            let err = router
                .invoke(body(export, serde_json::json!({})))
                .await
                .unwrap_err();
            assert_eq!(err.code, ErrorCode::UnknownBundle, "{export:?}");
        }
    }

    #[tokio::test]
    async fn a_stage_export_is_refused_by_the_connector_router() {
        let router = ConnectorRouter::new();
        for export in [
            ExportKind::Transform,
            ExportKind::Dispatch,
            ExportKind::ToolCall,
        ] {
            let err = router
                .invoke(body(export, serde_json::json!({})))
                .await
                .unwrap_err();
            assert_eq!(err.code, ErrorCode::ExportMissing);
        }
    }

    #[test]
    fn call_errors_map_to_the_typed_wire_codes() {
        let cases = [
            (
                ConnectorCallError::FuelExhausted("f".into()),
                ErrorCode::ExecutorDeadline,
                Some("fuel_exhausted"),
            ),
            (
                ConnectorCallError::DeadlineExceeded("d".into()),
                ErrorCode::ExecutorDeadline,
                None,
            ),
            (
                ConnectorCallError::PoolExhausted { in_flight: 2 },
                ErrorCode::ExecutorDeadline,
                Some("pool_exhausted"),
            ),
            (
                ConnectorCallError::MemoryLimit("m".into()),
                ErrorCode::MemoryLimit,
                None,
            ),
            (
                ConnectorCallError::Trap("t".into()),
                ErrorCode::WasmTrap,
                None,
            ),
            (
                ConnectorCallError::InstantiateFailed("i".into()),
                ErrorCode::LoadFailed,
                None,
            ),
            (
                ConnectorCallError::Internal("n".into()),
                ErrorCode::LoadFailed,
                None,
            ),
        ];
        for (err, code, detail) in cases {
            let body = call_error_to_body(err);
            assert_eq!(body.code, code);
            assert_eq!(body.detail.as_deref(), detail);
        }
    }

    #[test]
    fn a_guest_frame_error_is_a_result_payload_not_an_error_frame() {
        for (fe, kind) in [
            (FrameError::Unsupported("u".into()), "unsupported"),
            (FrameError::Malformed("m".into()), "malformed"),
            (FrameError::Backend("b".into()), "backend"),
        ] {
            let (payload, fuel) = settle::<()>(Err(ConnectorCallError::Guest(fe)), |()| {
                Ok(serde_json::json!({}))
            })
            .unwrap();
            assert_eq!(fuel, 0);
            assert_eq!(payload["frame_error"]["kind"], kind);
        }
    }

    #[test]
    fn payload_decoding_rejects_unknown_fields_and_bad_base64() {
        let bad = serde_json::json!({
            "ctx": {"platform":"p","source_id":"s","connection_id":"c","extra":1}
        });
        assert!(decode::<CtxArgs>(bad).is_err());
        assert!(decode_bytes("!!not-base64!!").is_err());
        let ok = serde_json::json!({
            "ctx": {"platform":"p","source_id":"s","connection_id":"c"}
        });
        let args: CtxArgs = decode(ok).unwrap();
        assert!(args.ctx.session_token.is_none());
    }

    #[test]
    fn replies_encode_with_base64_bytes_and_the_documented_field_names() {
        let h = encode_handshake(HandshakePayload {
            bytes: vec![0, 255, 7],
            secret_refs: vec![("slot".into(), "handle".into())],
        })
        .unwrap();
        assert_eq!(h["bytes"], BASE64.encode([0u8, 255, 7]));
        assert_eq!(h["secret_refs"][0][0], "slot");
        let e = encode_events(vec![NormalizedEvent {
            platform: "echo".into(),
            event_type: "message".into(),
            actor: None,
            payload_json: "{}".into(),
            occurred_at: "2026-10-13T00:00:00.000Z".into(),
        }])
        .unwrap();
        assert_eq!(e["events"][0]["event_type"], "message");
        assert!(e["events"][0]["actor"].is_null());
    }
}
