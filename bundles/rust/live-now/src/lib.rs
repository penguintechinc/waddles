//! `live-now` app bundle: community "who's live" across platforms.
//!
//! A chat-stage APP on `world stage-next-tools`. The process stage consumes `!live`, Twitch
//! `stream.online`/`stream.offline` and `live.refresh` events, calls each platform SERVICE
//! bundle's `<platform>.live.channels` tool (common contract, `bundles/contracts/
//! live-channels-contract.v1.json`) and decides which messages to send; the action stage
//! relays them. All logic lives in [`logic`] (host-testable behind the `Host` seam); this file
//! supplies the real wasm seam and the WIT exports. Secrets never enter the guest: the
//! platform tools resolve connections host-side.
pub mod logic;

#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;

#[cfg(target_arch = "wasm32")]
mod component {
    use serde_json::Value;
    use waddle_sdk::tool::{ToolError, ToolTransport, invoke_with};

    use super::bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
    use super::bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
    use super::bindings::exports::waddle::bundle::streaming_lifecycle::{
        Guest as StreamingGuest, RecordingInfo, SegmentInfo, StreamInfo,
    };
    use super::bindings::waddle::bundle::tool::{self, ToolError as WitError};
    use super::bindings::waddle::bundle::types::{
        PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
    };
    use super::bindings::waddle::bundle::{clock, context, flags, kv, log, relay};
    use super::logic::{self, Config, Host, Level};

    struct Component;

    /// Adapts the generated `tool.invoke` import to the SDK transport seam.
    struct HostTransport;

    impl ToolTransport for HostTransport {
        fn invoke(&self, target: &str, name: &str, args: &[u8], key: Option<&str>) -> Result<Vec<u8>, ToolError> {
            tool::invoke(target, name, args, key).map_err(|e| match e {
                WitError::NotGranted => ToolError::NotGranted,
                WitError::Forbidden => ToolError::Forbidden,
                WitError::Timeout => ToolError::Timeout,
                WitError::DepthExceeded => ToolError::DepthExceeded,
                WitError::PoolExhausted => ToolError::PoolExhausted,
                WitError::TargetUnavailable => ToolError::TargetUnavailable,
                WitError::InvalidArgs(m) => ToolError::InvalidArgs(m),
            })
        }
    }

    /// Real host capabilities.
    struct WasmHost;

    impl Host for WasmHost {
        fn invoke(&self, target: &str, name: &str, args: &Value) -> Result<Vec<u8>, ToolError> {
            invoke_with(&HostTransport, target, name, args, None)
        }
        fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            kv::get(key).map_err(|_| "kv unavailable".to_string())
        }
        fn kv_set(&self, key: &str, value: &[u8], ttl: u32) -> Result<(), String> {
            kv::set(key, value, ttl).map_err(|_| "kv unavailable".to_string())
        }
        fn kv_increment(&self, key: &str, delta: i64, ttl: u32) -> Result<i64, String> {
            kv::increment(key, delta, ttl).map_err(|_| "kv unavailable".to_string())
        }
        fn now_millis(&self) -> u64 {
            clock::now_millis()
        }
        fn flag(&self, key: &str, default: bool) -> bool {
            flags::enabled(key, default)
        }
        fn log(&self, level: Level, message: &str, fields: Value) {
            let lvl = match level {
                Level::Debug => log::Level::Debug,
                Level::Info => log::Level::Info,
                Level::Warn => log::Level::Warn,
                Level::Error => log::Level::Error,
            };
            log::write(lvl, message, &fields.to_string());
        }
    }

    impl ProcessGuest for Component {
        /// Maps one inbound event to an outbound `{messages:[...]}` event (or none).
        fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            let payload: Value = serde_json::from_str(&event.payload_json).unwrap_or(Value::Null);
            let cfg = Config::parse(&context::get_context().config_json);
            match logic::handle_event(&WasmHost, &cfg, &event.platform, &event.event_type, &payload) {
                Ok(messages) if messages.is_empty() => Ok(None),
                Ok(messages) => {
                    let body = serde_json::json!({ "messages": messages }).to_string();
                    Ok(Some(PlatformEvent {
                        platform: event.platform,
                        event_type: event.event_type,
                        // The invoking actor is dropped: replies never need it.
                        actor: None,
                        payload_json: body,
                        occurred_at: event.occurred_at,
                    }))
                }
                Err(e) => {
                    WasmHost.log(Level::Error, "live-now event failed", serde_json::json!({"error": e}));
                    Ok(None)
                }
            }
        }
    }

    impl ActionGuest for Component {
        /// Relays every message to its platform's outbound queue. A failure before anything was
        /// sent is retryable; a failure after a partial send is fatal (a retry would duplicate).
        fn dispatch(envelope: StageEnvelope, _config: String) -> Result<TransportResult, TransportError> {
            let messages = logic::parse_messages(&envelope.event.payload_json).map_err(|m| TransportError {
                retryable: false,
                code: "BAD_PAYLOAD".to_string(),
                message: m,
                retry_after_ms: None,
            })?;
            let mut sent = 0usize;
            for m in &messages {
                let body = serde_json::json!({"channel": m.channel, "text": m.text}).to_string();
                if let Err(e) = relay::push(&m.platform, &body) {
                    let detail = match e {
                        relay::Error::Denied(d) | relay::Error::Backend(d) => d,
                    };
                    return Err(TransportError {
                        retryable: sent == 0,
                        code: if sent == 0 { "RELAY_PUSH_FAILED" } else { "RELAY_PARTIAL" }.to_string(),
                        message: format!("relay failed after {sent} of {} messages: {detail}", messages.len()),
                        retry_after_ms: None,
                    });
                }
                sent += 1;
            }
            Ok(TransportResult { ok: true, status: None, detail: Some(format!("relayed {sent}")), provider_message_id: None })
        }
    }

    fn unsupported() -> UnsupportedStage {
        UnsupportedStage { stage: "streaming-lifecycle".to_string() }
    }

    impl StreamingGuest for Component {
        fn on_start(_info: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported())
        }
        fn on_stop(_info: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported())
        }
        fn on_segment(_s: SegmentInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported())
        }
        fn on_recording_ready(_r: RecordingInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported())
        }
    }

    super::bindings::export!(Component with_types_in super::bindings);
}
