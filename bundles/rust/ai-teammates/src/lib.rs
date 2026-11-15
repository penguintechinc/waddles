//! AI Teammates APP bundle: Researcher, Receptionist and Producer, each enabled per community.
//!
//! Stage split (host rule): identity, tool calls and kv run in `process-stage` (`transform`);
//! `action-stage` (`dispatch`) only relays the finished, already-sanitized reply. The model never
//! selects tools -- every tool call is made from code in [`ai`]. Chat is untrusted data
//! ([`guard`]). The logic is host-independent ([`host::Host`]) so the real code paths run in
//! native tests; the WASM component below is a thin adapter over the WIT imports.

#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;

pub mod ai;
pub mod command;
pub mod config;
pub mod engine;
pub mod guard;
pub mod host;
pub mod producer;
pub mod receptionist;
pub mod researcher;

use serde_json::{Value, json};
use waddle_sdk::{PlatformEvent, StageEnvelope, TransportError};

use engine::Reply;

/// Longest reply the action stage will relay (chars).
pub const MAX_RELAY_CHARS: usize = 500;

/// Wraps a finished [`Reply`] as the event the action stage reads back.
pub fn reply_event(reply: &Reply, occurred_at: &str) -> PlatformEvent {
    PlatformEvent {
        platform: reply.platform.clone(),
        event_type: "chat.message".to_string(),
        actor: None,
        payload_json: json!({"channel_id": reply.channel_id, "reply": reply.text}).to_string(),
        occurred_at: occurred_at.to_string(),
    }
}

/// Validates the reply event and builds the `(provider, {channel, text})` relay message.
/// Re-validates the text (defense in depth) before anything leaves.
pub fn build_relay(envelope: &StageEnvelope) -> Result<(String, Value), TransportError> {
    let payload: Value = envelope
        .event
        .payload()
        .map_err(|e| TransportError::fatal("BAD_PAYLOAD", e.to_string()))?;
    let channel = payload
        .get("channel_id")
        .and_then(Value::as_str)
        .filter(|c| !c.is_empty())
        .ok_or_else(|| TransportError::fatal("MISSING_CHANNEL", "reply requires a channel_id"))?;
    let reply = payload
        .get("reply")
        .and_then(Value::as_str)
        .ok_or_else(|| TransportError::fatal("MISSING_REPLY", "reply text missing"))?;
    let text = guard::validate_relay_text(reply, MAX_RELAY_CHARS)
        .map_err(|why| TransportError::fatal("UNSAFE_REPLY", why))?;
    Ok((
        envelope.event.platform.clone(),
        json!({"channel": channel, "text": text}),
    ))
}

#[cfg(target_arch = "wasm32")]
mod component {
    use super::bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
    use super::bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
    use super::bindings::exports::waddle::bundle::streaming_lifecycle::{
        Guest as LifecycleGuest, RecordingInfo, SegmentInfo, StreamInfo,
    };
    use super::bindings::waddle::bundle::types::{
        PlatformEvent as WitEvent, StageEnvelope as WitEnvelope,
        TransportError as WitTransportError, TransportResult as WitTransportResult,
        UnsupportedStage,
    };
    use super::bindings::waddle::bundle::{clock, context, identity, kv, log, relay, tool};
    use super::host::{Ctx, Host, HostError, Level};
    use waddle_sdk::tool::ToolError;

    pub struct Component;
    struct Wasm;

    fn kv_err(e: kv::Error) -> HostError {
        match e {
            kv::Error::TooLarge(_) => HostError::Backend("kv value too large".into()),
            kv::Error::Backend(_) => HostError::Backend("kv backend".into()),
        }
    }

    impl Host for Wasm {
        fn now_millis(&self) -> u64 {
            clock::now_millis()
        }
        fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, HostError> {
            kv::get(key).map_err(kv_err)
        }
        fn kv_set(&self, key: &str, value: &[u8], ttl: u32) -> Result<(), HostError> {
            kv::set(key, value, ttl).map_err(kv_err)
        }
        fn kv_incr(&self, key: &str, delta: i64, ttl: u32) -> Result<i64, HostError> {
            kv::increment(key, delta, ttl).map_err(kv_err)
        }
        fn resolve_actor(&self) -> Result<String, HostError> {
            identity::resolve_actor().map_err(|e| match e {
                identity::Error::NotLinked => HostError::Identity("not_linked".into()),
                identity::Error::NotAMember => HostError::Identity("not_a_member".into()),
                identity::Error::Denied(_) => HostError::Identity("denied".into()),
                _ => HostError::Identity("unavailable".into()),
            })
        }
        fn invoke(
            &self,
            target: &str,
            name: &str,
            args: &[u8],
            idem: Option<&str>,
        ) -> Result<Vec<u8>, ToolError> {
            tool::invoke(target, name, args, idem).map_err(|e| match e {
                tool::ToolError::NotGranted => ToolError::NotGranted,
                tool::ToolError::Forbidden => ToolError::Forbidden,
                tool::ToolError::Timeout => ToolError::Timeout,
                tool::ToolError::DepthExceeded => ToolError::DepthExceeded,
                tool::ToolError::PoolExhausted => ToolError::PoolExhausted,
                tool::ToolError::TargetUnavailable => ToolError::TargetUnavailable,
                tool::ToolError::InvalidArgs(m) => ToolError::InvalidArgs(m),
            })
        }
        fn log(&self, level: Level, msg: &str, fields_json: &str) {
            let lvl = match level {
                Level::Debug => log::Level::Debug,
                Level::Info => log::Level::Info,
                Level::Warn => log::Level::Warn,
                Level::Error => log::Level::Error,
            };
            log::write(lvl, msg, fields_json);
        }
    }

    fn ctx() -> Ctx {
        let c = context::get_context();
        Ctx {
            community: c.community,
            message_id: c.message_id,
            config_json: c.config_json,
        }
    }

    fn from_wit(e: WitEvent) -> waddle_sdk::PlatformEvent {
        waddle_sdk::PlatformEvent {
            platform: e.platform,
            event_type: e.event_type,
            actor: e.actor,
            payload_json: e.payload_json,
            occurred_at: e.occurred_at,
        }
    }

    fn to_wit(e: waddle_sdk::PlatformEvent) -> WitEvent {
        WitEvent {
            platform: e.platform,
            event_type: e.event_type,
            actor: e.actor,
            payload_json: e.payload_json,
            occurred_at: e.occurred_at,
        }
    }

    fn reply_for(reply: Option<super::engine::Reply>) -> Option<WitEvent> {
        reply.map(|r| to_wit(super::reply_event(&r, &clock::now_rfc3339())))
    }

    impl ProcessGuest for Component {
        /// Dispatches one event to the teammate logic; at most one reply event comes back.
        fn transform(event: WitEvent) -> Result<Option<WitEvent>, UnsupportedStage> {
            Ok(reply_for(super::engine::handle_event(
                &Wasm,
                &ctx(),
                &from_wit(event),
            )))
        }
    }

    impl ActionGuest for Component {
        /// Relays the finished reply to the event's own platform and channel.
        fn dispatch(
            envelope: WitEnvelope,
            _config: String,
        ) -> Result<WitTransportResult, WitTransportError> {
            let env = waddle_sdk::StageEnvelope {
                tenant: envelope.tenant,
                community: envelope.community,
                app_id: envelope.app_id,
                stage: envelope.stage,
                event: from_wit(envelope.event),
                ts: envelope.ts,
                target_app_id: envelope.target_app_id,
                trace_context: envelope.trace_context,
            };
            let to_err = |e: waddle_sdk::TransportError| WitTransportError {
                retryable: e.retryable,
                code: e.code,
                message: e.message,
                retry_after_ms: e.retry_after_ms,
            };
            let (provider, message) = super::build_relay(&env).map_err(to_err)?;
            relay::push(&provider, &message.to_string()).map_err(|e| {
                let (code, msg) = match e {
                    relay::Error::Denied(m) => ("RELAY_DENIED", m),
                    relay::Error::Backend(m) => ("RELAY_BACKEND", m),
                };
                WitTransportError {
                    retryable: code == "RELAY_BACKEND",
                    code: code.to_string(),
                    message: msg,
                    retry_after_ms: None,
                }
            })?;
            Ok(WitTransportResult {
                ok: true,
                status: None,
                detail: Some("teammate_reply".to_string()),
                provider_message_id: None,
            })
        }
    }

    impl LifecycleGuest for Component {
        /// A stream going live is a Producer tick opportunity (the tick is internally throttled).
        fn on_start(_info: StreamInfo) -> Result<Option<WitEvent>, UnsupportedStage> {
            Ok(reply_for(super::engine::run_tick(&Wasm, &ctx())))
        }
        fn on_stop(_info: StreamInfo) -> Result<Option<WitEvent>, UnsupportedStage> {
            Ok(None)
        }
        /// Segments arrive continuously while live: the Producer's heartbeat on svc-streaming.
        fn on_segment(_segment: SegmentInfo) -> Result<Option<WitEvent>, UnsupportedStage> {
            Ok(reply_for(super::engine::run_tick(&Wasm, &ctx())))
        }
        fn on_recording_ready(
            _recording: RecordingInfo,
        ) -> Result<Option<WitEvent>, UnsupportedStage> {
            Ok(None)
        }
    }
}

#[cfg(target_arch = "wasm32")]
use component::Component;
#[cfg(target_arch = "wasm32")]
bindings::export!(Component with_types_in bindings);
