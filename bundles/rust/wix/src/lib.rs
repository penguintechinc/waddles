//! Wix webhook connector (inbound-only `kind: service`, `connector` world).
//! The host owns the verified webhook ingress and every credential; this
//! component maps verified Wix events to normalized records and tokenizes the
//! Wix member id through `identity.lookup`. There is no egress and no sender.
#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;
pub mod logic;

#[cfg(target_arch = "wasm32")]
mod component {
    use super::bindings::exports::waddle::bundle::tool_provider::Guest as ToolGuest;
    use super::bindings::exports::waddle::connector::receiver::Guest as ReceiverGuest;
    use super::bindings::exports::waddle::connector::sender::{
        Action, Guest as SenderGuest, HttpRequestTpl,
    };
    use super::bindings::waddle::bundle::log;
    use super::bindings::waddle::bundle::tool::ToolError as WitError;
    use super::bindings::waddle::connector::identity::{self, Error as IdError, IdentityKey};
    use super::bindings::waddle::connector::types::{
        ConnectionCtx, FrameError, HandshakePayload, NormalizedEvent, RawFrame,
    };
    use super::logic::{self, LogicError};

    pub struct Component;

    /// PII-free log line: operation + counts only, never frame content.
    fn log_counts(level: log::Level, op: &str, events: usize, skipped: usize) {
        let fields = serde_json::json!({"op": op, "events": events, "skipped": skipped});
        log::write(level, "wix connector", &fields.to_string());
    }

    /// Resolution of one Wix member id: a canonical UUID, or not linked (drop).
    enum Resolved {
        Uuid(String),
        Unlinked,
    }

    fn resolve(member_id: &str) -> Result<Resolved, FrameError> {
        match identity::lookup(&IdentityKey::PlatformIdentity((
            "wix".to_string(),
            member_id.to_string(),
        ))) {
            Ok(rec) => Ok(Resolved::Uuid(rec.uuid)),
            Err(IdError::NotFound) => Ok(Resolved::Unlinked),
            Err(IdError::RateLimited(_)) => {
                Err(FrameError::Backend("identity rate-limited".into()))
            }
            Err(IdError::Denied(_)) => Err(FrameError::Backend("identity denied".into())),
            Err(IdError::Backend(_)) => Err(FrameError::Backend("identity backend error".into())),
        }
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
            let mut out = Vec::with_capacity(parsed.events.len());
            let mut skipped = parsed.skipped;
            for e in parsed.events {
                let actor = match e.member.as_deref() {
                    None => None,
                    Some(id) => match resolve(id)? {
                        Resolved::Uuid(u) => Some(u),
                        Resolved::Unlinked => {
                            skipped += 1;
                            continue;
                        }
                    },
                };
                out.push(NormalizedEvent {
                    platform: e.platform,
                    event_type: e.event_type,
                    actor,
                    payload_json: e.payload_json,
                    occurred_at: e.occurred_at,
                });
            }
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
        /// Inbound-only: every outbound action is refused loudly.
        fn build_request(_action: Action) -> Result<HttpRequestTpl, FrameError> {
            Err(FrameError::Unsupported(
                "wix connector is inbound-only".into(),
            ))
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
