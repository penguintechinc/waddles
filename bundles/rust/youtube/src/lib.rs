//! YouTube platform overlay (bundle-MCP-tools P6b T3): a `kind: service`
//! connector on the `connector-tools` world. The host owns the poll transport
//! and every credential; this component supplies the platform knowledge:
//! frame -> normalized events (`receiver`), chat-send request template
//! (`sender`), and read-only helper tools (`tool-provider`).
#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;
pub mod logic;

#[cfg(target_arch = "wasm32")]
mod component {
    use super::bindings::exports::waddle::bundle::tool_provider::Guest as ToolGuest;
    use super::bindings::exports::waddle::connector::receiver::Guest as ReceiverGuest;
    use super::bindings::exports::waddle::connector::sender::{
        Action, Guest as SenderGuest, Header, HttpRequestTpl,
    };
    use super::bindings::waddle::bundle::log;
    use super::bindings::waddle::bundle::tool::ToolError as WitError;
    use super::bindings::waddle::connector::types::{
        ConnectionCtx, FrameError, HandshakePayload, NormalizedEvent, RawFrame,
    };
    use super::logic::{self, LogicError};

    pub struct Component;

    /// PII-free log line: operation + counts only, never frame content.
    fn log_counts(level: log::Level, op: &str, events: usize, skipped: usize) {
        let fields = serde_json::json!({"op": op, "events": events, "skipped": skipped});
        log::write(level, "youtube overlay", &fields.to_string());
    }

    fn frame_err(e: LogicError) -> FrameError {
        match e {
            LogicError::Malformed(m) => FrameError::Malformed(m),
            LogicError::Unsupported(m) => FrameError::Unsupported(m),
        }
    }

    impl ReceiverGuest for Component {
        /// Polling has no handshake: empty payload, no secret refs.
        fn on_connect(_ctx: ConnectionCtx) -> Result<HandshakePayload, FrameError> {
            Ok(HandshakePayload {
                bytes: Vec::new(),
                secret_refs: Vec::new(),
            })
        }

        /// One poll response (or raw event) in, zero or more events out.
        fn on_frame(frame: RawFrame) -> Result<Vec<NormalizedEvent>, FrameError> {
            let parsed = match logic::parse_frame(&frame.bytes, &frame.received_at) {
                Ok(p) => p,
                Err(e) => {
                    log_counts(log::Level::Warn, "on_frame_malformed", 0, 0);
                    return Err(frame_err(e));
                }
            };
            log_counts(
                if parsed.skipped > 0 {
                    log::Level::Warn
                } else {
                    log::Level::Debug
                },
                "on_frame",
                parsed.events.len(),
                parsed.skipped,
            );
            Ok(parsed
                .events
                .into_iter()
                .map(|e| NormalizedEvent {
                    platform: e.platform,
                    event_type: e.event_type,
                    actor: e.actor,
                    payload_json: e.payload_json,
                    occurred_at: e.occurred_at,
                })
                .collect())
        }

        /// Poll transports have no keepalive.
        fn on_heartbeat_due(_ctx: ConnectionCtx) -> Result<Option<Vec<u8>>, FrameError> {
            Ok(None)
        }

        fn on_disconnect(_ctx: ConnectionCtx, _reason: String) {}
    }

    impl SenderGuest for Component {
        /// `chat.send` -> `liveChatMessages.insert`; the host pins the target and
        /// substitutes the credential for the returned secret-ref slot.
        fn build_request(action: Action) -> Result<HttpRequestTpl, FrameError> {
            let t = logic::build_chat_send(
                &action.kind,
                &action.target_source_id,
                &action.payload_json,
            )
            .map_err(frame_err)?;
            Ok(HttpRequestTpl {
                method: t.method,
                url: t.url,
                headers: t
                    .headers
                    .into_iter()
                    .map(|(name, value)| Header { name, value })
                    .collect(),
                body: t.body,
                secret_refs: t.secret_refs,
            })
        }
    }

    impl ToolGuest for Component {
        /// Routes a tool name through the pure dispatcher.
        fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
            let value: serde_json::Value = serde_json::from_slice(&args)
                .map_err(|e| WitError::InvalidArgs(format!("args are not JSON: {e}")))?;
            let out = logic::run_tool(&name, &value).map_err(|e| match e {
                LogicError::Malformed(m) | LogicError::Unsupported(m) => WitError::InvalidArgs(m),
            })?;
            serde_json::to_vec(&out).map_err(|e| WitError::InvalidArgs(e.to_string()))
        }
    }
}

#[cfg(target_arch = "wasm32")]
use component::Component;
#[cfg(target_arch = "wasm32")]
bindings::export!(Component with_types_in bindings);
