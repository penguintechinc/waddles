//! RSI Spectrum platform connector (gh #486): a one-way INBOUND `kind: service`
//! connector on the `connector` world. The host owns the poll transport, the
//! RSI-host egress allowlist and the session credential; this component supplies
//! the platform knowledge (frame -> normalized events, author tokenization).
//! There is deliberately no outbound path: `sender.build-request` always fails.
#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;
pub mod logic;

#[cfg(target_arch = "wasm32")]
mod component {
    use super::bindings::exports::waddle::connector::receiver::Guest as ReceiverGuest;
    use super::bindings::exports::waddle::connector::sender::{
        Action, Guest as SenderGuest, HttpRequestTpl,
    };
    use super::bindings::waddle::bundle::log;
    use super::bindings::waddle::connector::identity::{self, Error as IdError, IdentityKey};
    use super::bindings::waddle::connector::types::{
        ConnectionCtx, FrameError, HandshakePayload, NormalizedEvent, RawFrame,
    };
    use super::logic::{self, LogicError};
    use std::collections::HashMap;

    pub struct Component;

    /// PII-free log line: operation + counts only, never frame content.
    fn log_counts(level: log::Level, op: &str, events: usize, skipped: usize) {
        let fields = serde_json::json!({"op": op, "events": events, "skipped": skipped});
        log::write(level, "spectrum connector", &fields.to_string());
    }

    fn frame_err(e: LogicError) -> FrameError {
        match e {
            LogicError::Malformed(m) => FrameError::Malformed(m),
            LogicError::Unsupported(m) => FrameError::Unsupported(m),
        }
    }

    /// Resolution of one RSI member id: a canonical UUID, or erased (drop).
    enum Resolved {
        Uuid(String),
        Erased,
    }

    /// Tokenizes `member_id` via `identity.lookup`; typed failures fail the frame loud.
    fn resolve(member_id: &str) -> Result<Resolved, FrameError> {
        match identity::lookup(&IdentityKey::PlatformIdentity((
            "spectrum".to_string(),
            member_id.to_string(),
        ))) {
            Ok(rec) => Ok(Resolved::Uuid(rec.uuid)),
            Err(IdError::NotFound) => Ok(Resolved::Erased),
            Err(IdError::RateLimited(_)) => {
                Err(FrameError::Backend("identity rate-limited".into()))
            }
            Err(IdError::Denied(_)) => Err(FrameError::Backend("identity denied".into())),
            Err(IdError::Backend(_)) => Err(FrameError::Backend("identity backend error".into())),
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

        /// One poll response (or raw change event) in, zero or more events out.
        fn on_frame(frame: RawFrame) -> Result<Vec<NormalizedEvent>, FrameError> {
            let parsed =
                match logic::parse_frame(&frame.bytes, &frame.received_at, &frame.ctx.source_id) {
                    Ok(p) => p,
                    Err(e) => {
                        log_counts(log::Level::Warn, "on_frame_malformed", 0, 0);
                        return Err(frame_err(e));
                    }
                };
            // Tokenize actors: one lookup per distinct author per frame. An erased
            // (unknown/forgotten) author's event is dropped and counted.
            let mut cache: HashMap<String, Resolved> = HashMap::new();
            let mut erased = 0usize;
            let mut out = Vec::with_capacity(parsed.events.len());
            for e in parsed.events {
                let actor = match e.actor.as_deref() {
                    None => None,
                    Some(id) => {
                        if !cache.contains_key(id) {
                            cache.insert(id.to_string(), resolve(id)?);
                        }
                        match cache.get(id) {
                            Some(Resolved::Uuid(u)) => Some(u.clone()),
                            _ => {
                                erased += 1;
                                continue;
                            }
                        }
                    }
                };
                out.push(NormalizedEvent {
                    platform: e.platform,
                    event_type: e.event_type,
                    actor,
                    payload_json: e.payload_json,
                    occurred_at: e.occurred_at,
                });
            }
            let skipped = parsed.skipped + erased;
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

        /// Poll transports have no keepalive.
        fn on_heartbeat_due(_ctx: ConnectionCtx) -> Result<Option<Vec<u8>>, FrameError> {
            Ok(None)
        }

        fn on_disconnect(_ctx: ConnectionCtx, _reason: String) {}
    }

    impl SenderGuest for Component {
        /// Spectrum is inbound-only (#101): automating writes against RSI's
        /// undocumented API risks banning the org. Always refused, loudly.
        fn build_request(_action: Action) -> Result<HttpRequestTpl, FrameError> {
            Err(FrameError::Unsupported(
                "spectrum is one-way inbound; no outbound path exists".into(),
            ))
        }
    }
}

#[cfg(target_arch = "wasm32")]
use component::Component;
#[cfg(target_arch = "wasm32")]
bindings::export!(Component with_types_in bindings);
