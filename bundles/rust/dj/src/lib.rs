//! DJ APP bundle: playlists, viewer song requests, mod queue controls, a
//! deterministic song policy and an optional AI DJ.
//!
//! Layout: `model`/`policy` (pure data + rules), `music`/`ai` (typed clients
//! over gated tool calls), `engine`/`admin` (behaviour, generic over `Host`),
//! `chat` (event parsing), and `component` (the wasm-only WIT glue, the only
//! code that touches real host imports). Process-stage does all work (identity
//! + tool calls are wired there); action-stage only relays the finished reply.
#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;

pub mod admin;
pub mod ai;
pub mod chat;
pub mod engine;
pub mod host;
pub mod model;
pub mod music;
pub mod policy;

#[cfg(target_arch = "wasm32")]
mod component {
    use serde_json::Value;

    use super::bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
    use super::bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
    use super::bindings::exports::waddle::bundle::streaming_lifecycle::{
        Guest as StreamGuest, RecordingInfo, SegmentInfo, StreamInfo,
    };
    use super::bindings::waddle::bundle::tool::ToolError as WitToolError;
    use super::bindings::waddle::bundle::types::{
        PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
    };
    use super::bindings::waddle::bundle::{clock, context, flags, identity, kv, log, relay, tool};
    use super::chat;
    use super::engine::{Ctx, Engine};
    use super::host::{Host, Level, ToolErr};

    /// Feature flag gating every reply (defaults OFF until validated).
    const FLAG: &str = "waddles.command-dj";

    pub struct Component;
    struct WasmHost;

    impl Host for WasmHost {
        fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            kv::get(key).map_err(|e| format!("{e:?}"))
        }
        fn kv_set(&self, key: &str, value: &[u8], ttl_s: u32) -> Result<(), String> {
            kv::set(key, value, ttl_s).map_err(|e| format!("{e:?}"))
        }
        fn kv_delete(&self, key: &str) -> Result<(), String> {
            kv::delete(key).map_err(|e| format!("{e:?}"))
        }
        fn now_ms(&self) -> u64 {
            clock::now_millis()
        }
        fn invoke(
            &self,
            target: &str,
            tool_name: &str,
            args: &Value,
            idem: Option<&str>,
        ) -> Result<Value, ToolErr> {
            let bytes =
                serde_json::to_vec(args).map_err(|e| ToolErr::InvalidArgs(e.to_string()))?;
            let out = tool::invoke(target, tool_name, &bytes, idem).map_err(|e| match e {
                WitToolError::NotGranted => ToolErr::NotGranted,
                WitToolError::Forbidden => ToolErr::Forbidden,
                WitToolError::Timeout => ToolErr::Timeout,
                WitToolError::DepthExceeded => ToolErr::DepthExceeded,
                WitToolError::PoolExhausted => ToolErr::PoolExhausted,
                WitToolError::TargetUnavailable => ToolErr::TargetUnavailable,
                WitToolError::InvalidArgs(m) => ToolErr::InvalidArgs(m),
            })?;
            serde_json::from_slice(&out).map_err(|e| ToolErr::InvalidArgs(e.to_string()))
        }
        fn log(&self, level: Level, message: &str, fields: Value) {
            let lvl = match level {
                Level::Error => log::Level::Error,
                Level::Warn => log::Level::Warn,
                Level::Info => log::Level::Info,
                Level::Debug => log::Level::Debug,
            };
            log::write(lvl, message, &fields.to_string());
        }
    }

    impl ProcessGuest for Component {
        /// Runs a DJ command to completion and returns the chat reply event.
        fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            let Some(msg) = chat::parse(&event.payload_json) else {
                return Ok(None);
            };
            if !flags::enabled(FLAG, false) {
                return Ok(None);
            }
            let host = WasmHost;
            // An unlinked/unresolvable actor is None; the engine then asks the user to link.
            let actor = identity::resolve_actor().ok();
            let ctx = Ctx {
                actor,
                is_mod: msg.is_privileged(),
                message_id: context::get_context().message_id,
            };
            let text = match Engine::new(&host, &ctx).handle(&msg.text) {
                Ok(Some(t)) => t,
                Ok(None) => return Ok(None),
                Err(e) => {
                    host.log(
                        Level::Error,
                        "dj command failed",
                        serde_json::json!({"error": e.to_string()}),
                    );
                    "the DJ hit an error, please try again later".to_string()
                }
            };
            Ok(Some(PlatformEvent {
                platform: event.platform,
                event_type: event.event_type,
                actor: event.actor,
                payload_json: chat::reply_payload(&text, msg.channel_id.as_deref()),
                occurred_at: event.occurred_at,
            }))
        }
    }

    impl ActionGuest for Component {
        /// Relays the finished reply back to the event's own platform channel.
        fn dispatch(
            envelope: StageEnvelope,
            _config: String,
        ) -> Result<TransportResult, TransportError> {
            let (wire, _channel) =
                chat::relay_message(&envelope.event.payload_json).map_err(|m| TransportError {
                    retryable: false,
                    code: "BAD_PAYLOAD".to_string(),
                    message: m,
                    retry_after_ms: None,
                })?;
            relay::push(&envelope.event.platform, &wire).map_err(|e| TransportError {
                retryable: true,
                code: "RELAY_PUSH_FAILED".to_string(),
                message: format!("{e:?}"),
                retry_after_ms: None,
            })?;
            Ok(TransportResult {
                ok: true,
                status: None,
                detail: None,
                provider_message_id: None,
            })
        }
    }

    fn unsupported() -> UnsupportedStage {
        UnsupportedStage {
            stage: "streaming-lifecycle".to_string(),
        }
    }

    /// No lifecycle hooks: stream-driven playback needs a host periodic/lifecycle
    /// seam (see README "Needs from host seams"); until then these fail loud.
    impl StreamGuest for Component {
        fn on_start(_: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported())
        }
        fn on_stop(_: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported())
        }
        fn on_segment(_: SegmentInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported())
        }
        fn on_recording_ready(_: RecordingInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported())
        }
    }
}

#[cfg(target_arch = "wasm32")]
use component::Component;
#[cfg(target_arch = "wasm32")]
bindings::export!(Component with_types_in bindings);
