//! Twitch platform overlay (bundle-MCP-tools P6b T1) on the `connector-tools`
//! world. Pure, host-testable logic lives in the modules below; the wasm
//! component glue at the bottom only maps it onto the generated WIT types.
//! The overlay holds no secret, no raw target and no transport code.
pub mod common;
pub mod eventsub;
pub mod irc;
pub mod sender;
pub mod tools;

#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;

#[cfg(target_arch = "wasm32")]
mod component {
    use std::sync::{Arc, OnceLock};

    use super::bindings::exports::waddle::bundle::tool_provider::Guest as ToolGuest;
    use super::bindings::exports::waddle::connector::receiver::Guest as ReceiverGuest;
    use super::bindings::exports::waddle::connector::sender::{
        Action, Guest as SenderGuest, Header, HttpRequestTpl,
    };
    use super::bindings::waddle::bundle::tool::ToolError as WitError;
    use super::bindings::waddle::bundle::{flags, http, log};
    use super::bindings::waddle::connector::identity;
    use super::bindings::waddle::connector::types::{
        ConnectionCtx, FrameError, HandshakePayload, NormalizedEvent, RawFrame,
    };
    use super::common::{
        FrameErr, OVERLAY_FLAG, OVERLAY_FLAG_DEFAULT, PLATFORM, gate_decision, resolve_actors,
    };
    use super::tools::{HelixHost, IdentityHost, SECRET_REFS, build_registry};
    use super::{eventsub, irc, sender};
    use waddle_sdk::tool::{ToolError, ToolRegistry};

    pub struct Component;

    fn fe(e: FrameErr) -> FrameError {
        match e {
            FrameErr::Unsupported(m) => FrameError::Unsupported(m),
            FrameErr::Malformed(m) => FrameError::Malformed(m),
            FrameErr::Backend(m) => FrameError::Backend(m),
        }
    }

    /// Defence in depth behind the host's own cutover: flag OFF (default) =
    /// the overlay refuses to run.
    fn gate() -> Result<(), FrameError> {
        gate_decision(flags::enabled(OVERLAY_FLAG, OVERLAY_FLAG_DEFAULT)).map_err(fe)
    }

    /// One PII-free line: op + counts only.
    fn log_op(op: &str, count: usize) {
        let f = serde_json::json!({ "op": op, "count": count }).to_string();
        log::write(log::Level::Debug, "twitch overlay", &f);
    }

    /// Resolves actors through `identity.lookup`; fail closed (drop + count,
    /// loud frame error when nothing survives). Never logs a key or handle.
    fn to_events(evs: Vec<super::common::NormEvent>) -> Result<Vec<NormalizedEvent>, FrameError> {
        let total = evs.len();
        let ident = WasmIdent;
        let (resolved, dropped) = resolve_actors(evs, &|key| ident.uuid_for(key).map_err(|_| ()));
        if dropped > 0 {
            log_op("actor_lookup_dropped", dropped);
            if resolved.is_empty() && total > 0 {
                return Err(FrameError::Backend(
                    "identity lookup failed; event dropped".to_string(),
                ));
            }
        }
        Ok(resolved
            .into_iter()
            .map(|e| NormalizedEvent {
                platform: PLATFORM.to_string(),
                payload_json: e.payload_json(),
                event_type: e.event_type,
                actor: e.actor,
                occurred_at: e.occurred_at,
            })
            .collect())
    }

    impl ReceiverGuest for Component {
        fn on_connect(_ctx: ConnectionCtx) -> Result<HandshakePayload, FrameError> {
            gate()?;
            let (bytes, refs) = irc::handshake();
            Ok(HandshakePayload {
                bytes,
                secret_refs: refs,
            })
        }

        /// Routes by body shape: a JSON object is an EventSub delivery, anything
        /// else is IRC text.
        fn on_frame(frame: RawFrame) -> Result<Vec<NormalizedEvent>, FrameError> {
            gate()?;
            let is_json = frame.bytes.iter().find(|b| !b.is_ascii_whitespace()) == Some(&b'{');
            let res = if is_json {
                eventsub::normalize_eventsub(&frame.bytes, &frame.received_at)
            } else {
                irc::normalize_frame(&frame.bytes, &frame.received_at, irc::GUEST_SELF_NICK)
            };
            let evs = res.map_err(fe)?;
            log_op(if is_json { "eventsub" } else { "irc" }, evs.len());
            to_events(evs)
        }

        fn on_heartbeat_due(_ctx: ConnectionCtx) -> Result<Option<Vec<u8>>, FrameError> {
            gate()?;
            Ok(Some(irc::HEARTBEAT.to_vec()))
        }

        fn on_disconnect(_ctx: ConnectionCtx, _reason: String) {
            log_op("disconnect", 0);
        }
    }

    impl SenderGuest for Component {
        fn build_request(action: Action) -> Result<HttpRequestTpl, FrameError> {
            gate()?;
            let t =
                sender::build_request(&action.kind, &action.target_source_id, &action.payload_json)
                    .map_err(fe)?;
            log_op(&action.kind, 1);
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

    struct WasmHelix;
    impl HelixHost for WasmHelix {
        fn get(&self, url: &str) -> Result<(u16, Vec<u8>), ToolError> {
            let req = http::Request {
                method: "GET".to_string(),
                url: url.to_string(),
                headers: Vec::new(),
                body: None,
                secret_refs: SECRET_REFS
                    .iter()
                    .map(|(h, s)| (h.to_string(), s.to_string()))
                    .collect(),
            };
            match http::send(&req) {
                Ok(r) if r.truncated => Err(ToolError::TargetUnavailable),
                Ok(r) => Ok((r.status, r.body)),
                Err(http::Error::Denied(_)) => Err(ToolError::NotGranted),
                Err(http::Error::Timeout) => Err(ToolError::Timeout),
                Err(http::Error::RateLimited(_)) => Err(ToolError::PoolExhausted),
                Err(_) => Err(ToolError::TargetUnavailable),
            }
        }
    }

    struct WasmIdent;
    impl IdentityHost for WasmIdent {
        fn uuid_for(&self, id: &str) -> Result<String, ToolError> {
            identity::lookup(&identity::IdentityKey::PlatformIdentity((
                PLATFORM.to_string(),
                id.to_string(),
            )))
            .map(|r| r.uuid)
            .map_err(|e| match e {
                identity::Error::Denied(_) => ToolError::NotGranted,
                identity::Error::RateLimited(_) => ToolError::PoolExhausted,
                _ => ToolError::TargetUnavailable,
            })
        }
    }

    static REGISTRY: OnceLock<ToolRegistry> = OnceLock::new();

    impl ToolGuest for Component {
        fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
            gate().map_err(|_| WitError::NotGranted)?;
            let reg =
                REGISTRY.get_or_init(|| build_registry(Arc::new(WasmHelix), Arc::new(WasmIdent)));
            let res = reg.call(&name, &args);
            log_op(&name, usize::from(res.is_ok()));
            res.map_err(|e| match e {
                ToolError::NotGranted => WitError::NotGranted,
                ToolError::Forbidden => WitError::Forbidden,
                ToolError::Timeout => WitError::Timeout,
                ToolError::DepthExceeded => WitError::DepthExceeded,
                ToolError::PoolExhausted => WitError::PoolExhausted,
                ToolError::TargetUnavailable => WitError::TargetUnavailable,
                ToolError::InvalidArgs(m) => WitError::InvalidArgs(m),
                ToolError::NoTransport => WitError::InvalidArgs("no tool transport".to_string()),
            })
        }
    }
}

#[cfg(target_arch = "wasm32")]
use component::Component;
#[cfg(target_arch = "wasm32")]
bindings::export!(Component with_types_in bindings);
