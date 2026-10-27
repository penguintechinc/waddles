// Echo connector (`waddles.core.example.echo`-style) -- the B0 end-to-end
// smoke connector (issue #840, spec
// `docs/superpowers/specs/2026-09-28-connector-bundles.md` S5.1 row 1 / S6
// phase 2). A synthetic, zero-real-traffic `receiver`: its `on-frame` is a
// faithful port of the legacy demo ingest handler
// `core/svc_ingest/builtin_handlers/echo_ingest.py::normalize`.
//
// Frame contract (JSON object in `raw-frame.bytes`):
//   source      (required, non-empty string)  -> normalized-event.platform
//   text        (required, non-empty string)  -> payload {"text": <trimmed>}
//   event_type  (optional, default "message")
//   actor       (optional, default "unknown")
//   occurred_at (optional, default: the host-stamped `raw-frame.received-at`;
//                the Python original used wall-clock now(), but a connector
//                holds no clock import -- the host stamps arrival time)
//
// A missing/empty/non-string `source`/`text` is `frame-error::malformed`,
// never a trap and never a silent default -- the ingest runner treats it as a
// per-frame failure so one bad frame never kills the connection.
//
// The `sender` export is `frame-error::unsupported`: sender wiring is a later
// phase (spec S6 phase 6), and an unwired capability must say so loudly.
#[allow(warnings)]
mod bindings;

use bindings::exports::waddle::connector::receiver::Guest as ReceiverGuest;
use bindings::exports::waddle::connector::sender::{
    Action, Guest as SenderGuest, HttpRequestTpl,
};
use bindings::waddle::connector::types::{
    ConnectionCtx, FrameError, HandshakePayload, NormalizedEvent, RawFrame,
};
use serde::Deserialize;

struct Component;

/// The raw inbound event shape `echo_ingest.normalize` accepts.
#[derive(Deserialize)]
struct RawEcho {
    source: Option<String>,
    text: Option<String>,
    event_type: Option<String>,
    actor: Option<String>,
    occurred_at: Option<String>,
}

impl ReceiverGuest for Component {
    fn on_connect(_ctx: ConnectionCtx) -> Result<HandshakePayload, FrameError> {
        // A synthetic in-memory transport has no wire handshake; a fixed
        // marker lets the host test prove the bytes crossed the boundary.
        Ok(HandshakePayload {
            bytes: b"echo-hello".to_vec(),
            secret_refs: Vec::new(),
        })
    }

    fn on_frame(frame: RawFrame) -> Result<Vec<NormalizedEvent>, FrameError> {
        let raw: RawEcho = serde_json::from_slice(&frame.bytes)
            .map_err(|e| FrameError::Malformed(format!("not a JSON echo event: {e}")))?;
        let source = raw
            .source
            .filter(|s| !s.is_empty())
            .ok_or_else(|| FrameError::Malformed("missing required 'source' string field".into()))?;
        let text = raw
            .text
            .filter(|s| !s.is_empty())
            .ok_or_else(|| FrameError::Malformed("missing required 'text' string field".into()))?;
        let payload_json = serde_json::json!({ "text": text.trim() }).to_string();
        Ok(vec![NormalizedEvent {
            platform: source,
            event_type: raw.event_type.unwrap_or_else(|| "message".to_string()),
            actor: Some(raw.actor.unwrap_or_else(|| "unknown".to_string())),
            payload_json,
            occurred_at: raw.occurred_at.unwrap_or(frame.received_at),
        }])
    }

    fn on_heartbeat_due(_ctx: ConnectionCtx) -> Result<Option<Vec<u8>>, FrameError> {
        // Platform-default heartbeat: no bundle-supplied body needed.
        Ok(None)
    }

    fn on_disconnect(_ctx: ConnectionCtx, _reason: String) {}
}

impl SenderGuest for Component {
    fn build_request(_action: Action) -> Result<HttpRequestTpl, FrameError> {
        Err(FrameError::Unsupported(
            "echo connector has no sender (sender wiring is a later phase)".to_string(),
        ))
    }
}

bindings::export!(Component with_types_in bindings);
