//! Wix connector (`kind: service`, `connector-tools` world): inbound webhooks + outbound Wix Events templates.
//! The host owns the verified webhook ingress and every credential; this
//! component maps verified Wix calendar events to normalized records (events only:
//! member webhooks are ignored, no identity lookup). Outbound event tools are host-executed request templates.
#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;
pub mod logic;
pub mod sender;

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
    use super::sender;

    pub struct Component;

    /// PII-free log line: operation + counts only, never frame content.
    fn log_counts(level: log::Level, op: &str, events: usize, skipped: usize) {
        let fields = serde_json::json!({"op": op, "events": events, "skipped": skipped});
        log::write(level, "wix connector", &fields.to_string());
    }

    impl ReceiverGuest for Component {
        /// Webhook ingress has no handshake: empty payload, no secret refs.
        fn on_connect(_ctx: ConnectionCtx) -> Result<HandshakePayload, FrameError> {
            Ok(HandshakePayload {
                bytes: Vec::new(),
                secret_refs: Vec::new(),
            })
        }

        /// One host-verified webhook JWT in, zero or one event out.
        fn on_frame(frame: RawFrame) -> Result<Vec<NormalizedEvent>, FrameError> {
            let parsed = match logic::parse_frame(&frame.bytes, &frame.received_at) {
                Ok(p) => p,
                Err(LogicError::Malformed(m)) => {
                    log_counts(log::Level::Warn, "on_frame_malformed", 0, 0);
                    return Err(FrameError::Malformed(m));
                }
            };
            let skipped = parsed.skipped;
            let out: Vec<NormalizedEvent> = parsed
                .events
                .into_iter()
                .map(|e| NormalizedEvent {
                    platform: e.platform,
                    event_type: e.event_type,
                    actor: None,
                    payload_json: e.payload_json,
                    occurred_at: e.occurred_at,
                })
                .collect();
            log_counts(
                if skipped > 0 {
                    log::Level::Warn
                } else {
                    log::Level::Debug
                },
                "on_frame",
                out.len(),
                skipped,
            );
            Ok(out)
        }

        /// Webhooks have no keepalive.
        fn on_heartbeat_due(_ctx: ConnectionCtx) -> Result<Option<Vec<u8>>, FrameError> {
            Ok(None)
        }

        fn on_disconnect(_ctx: ConnectionCtx, _reason: String) {}
    }

    impl SenderGuest for Component {
        /// Wix Events REST templates (`event.{list,get,create,update,cancel}`); host fills creds.
        fn build_request(action: Action) -> Result<HttpRequestTpl, FrameError> {
            let act = sender::Action {
                kind: action.kind,
                target_source_id: action.target_source_id,
                payload_json: action.payload_json,
            };
            match sender::build_request(&act) {
                Ok(t) => Ok(HttpRequestTpl {
                    method: t.method,
                    url: t.url,
                    headers: t
                        .headers
                        .into_iter()
                        .map(|(name, value)| Header { name, value })
                        .collect(),
                    body: t.body,
                    secret_refs: t.secret_refs,
                }),
                Err(e) => {
                    log_counts(log::Level::Warn, "build_request_refused", 0, 0);
                    Err(match e {
                        sender::SendError::Unsupported(m) => FrameError::Unsupported(m),
                        sender::SendError::Malformed(m) => FrameError::Malformed(m),
                    })
                }
            }
        }
    }

    impl ToolGuest for Component {
        /// The connector exports no tools; the world requires the interface.
        fn call(name: String, _args: Vec<u8>) -> Result<Vec<u8>, WitError> {
            Err(WitError::InvalidArgs(format!("unknown tool `{name}`")))
        }
    }
}

#[cfg(target_arch = "wasm32")]
use component::Component;
#[cfg(target_arch = "wasm32")]
bindings::export!(Component with_types_in bindings);
