//! Twitch clips APP bundle on `world stage-next-tools`. Process-stage parses `!clip`/`!clips`,
//! enforces gates and cooldowns, and calls the Twitch service bundle's `twitch.clip.*` tools
//! (all identity and tool calls happen here, where the host wires them). Action-stage only
//! relays the finished messages. Policy lives in [`logic`]; this file is the thin WASM seam.
#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;
pub mod logic;

#[cfg(target_arch = "wasm32")]
mod component {
    use super::bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
    use super::bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
    use super::bindings::exports::waddle::bundle::streaming_lifecycle::{
        Guest as StreamGuest, RecordingInfo, SegmentInfo, StreamInfo,
    };
    use super::bindings::waddle::bundle::types::{
        PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
    };
    use super::bindings::waddle::bundle::{clock, context, identity, kv, log, relay, tool};
    use super::logic::{self, EventIn, Host, HostError, IdentityError, Output};
    use serde_json::{Value, json};
    use waddle_sdk::tool::ToolError;

    pub struct Component;
    struct WasmHost;

    fn herr(e: kv::Error) -> HostError {
        match e {
            kv::Error::TooLarge(_) => HostError("kv value too large".into()),
            kv::Error::Backend(_) => HostError("kv backend error".into()),
        }
    }

    impl Host for WasmHost {
        fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, HostError> {
            kv::get(key).map_err(herr)
        }
        fn kv_set(&self, key: &str, value: &[u8], ttl_s: u32) -> Result<(), HostError> {
            kv::set(key, value, ttl_s).map_err(herr)
        }
        fn kv_delete(&self, key: &str) -> Result<(), HostError> {
            kv::delete(key).map_err(herr)
        }
        fn kv_increment(&self, key: &str, delta: i64, ttl_s: u32) -> Result<i64, HostError> {
            kv::increment(key, delta, ttl_s).map_err(herr)
        }
        fn now_millis(&self) -> u64 {
            clock::now_millis()
        }
        fn sleep_ms(&self, ms: u64) {
            std::thread::sleep(std::time::Duration::from_millis(ms));
        }
        fn resolve_actor(&self) -> Result<String, IdentityError> {
            identity::resolve_actor().map_err(|e| match e {
                identity::Error::NotLinked => IdentityError::NotLinked,
                _ => IdentityError::Other("identity unavailable".into()),
            })
        }
        fn invoke_tool(
            &self,
            name: &str,
            args: &Value,
            idem: Option<&str>,
        ) -> Result<Value, ToolError> {
            let bytes =
                serde_json::to_vec(args).map_err(|e| ToolError::InvalidArgs(e.to_string()))?;
            let out =
                tool::invoke(logic::TWITCH_SERVICE, name, &bytes, idem).map_err(|e| match e {
                    tool::ToolError::NotGranted => ToolError::NotGranted,
                    tool::ToolError::Forbidden => ToolError::Forbidden,
                    tool::ToolError::Timeout => ToolError::Timeout,
                    tool::ToolError::DepthExceeded => ToolError::DepthExceeded,
                    tool::ToolError::PoolExhausted => ToolError::PoolExhausted,
                    tool::ToolError::TargetUnavailable => ToolError::TargetUnavailable,
                    tool::ToolError::InvalidArgs(m) => ToolError::InvalidArgs(m),
                })?;
            serde_json::from_slice(&out).map_err(|e| ToolError::InvalidArgs(e.to_string()))
        }
        fn log(&self, level: &str, op: &str, outcome: &str) {
            let lv = match level {
                "error" => log::Level::Error,
                "warn" => log::Level::Warn,
                "info" => log::Level::Info,
                _ => log::Level::Debug,
            };
            let fields = json!({ "op": op, "outcome": outcome }).to_string();
            log::write(lv, "twitch-clips", &fields);
        }
    }

    fn unsupported(stage: &str) -> UnsupportedStage {
        UnsupportedStage {
            stage: stage.to_string(),
        }
    }

    impl ProcessGuest for Component {
        fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            let payload: Value = serde_json::from_str(&event.payload_json).unwrap_or(Value::Null);
            let ctx = context::get_context();
            let ev = EventIn {
                platform: event.platform.clone(),
                event_type: event.event_type.clone(),
                actor: event.actor.clone(),
                payload,
                message_id: ctx.message_id,
                config_json: ctx.config_json,
            };
            let out_payload = match logic::handle_event(&WasmHost, &ev) {
                Ok(Output::None) => return Ok(None),
                Ok(Output::Messages(m)) => json!({ "messages": m }),
                Ok(Output::Admin(v)) => json!({ "admin_result": v }),
                Err(e) => {
                    WasmHost.log("error", "transform", e.code);
                    if event.platform == "hub" {
                        json!({ "admin_result": { "ok": false, "error": e.code, "message": e.message } })
                    } else {
                        // Loud: an unexpected internal failure is dropped from chat but logged.
                        return Err(unsupported(&format!("process:{}", e.code)));
                    }
                }
            };
            let event_type = if event.platform == "hub" {
                "bundle.admin.result"
            } else {
                "chat.message"
            };
            Ok(Some(PlatformEvent {
                platform: event.platform,
                event_type: event_type.to_string(),
                actor: event.actor,
                payload_json: out_payload.to_string(),
                occurred_at: event.occurred_at,
            }))
        }
    }

    impl ActionGuest for Component {
        fn dispatch(
            envelope: StageEnvelope,
            _config: String,
        ) -> Result<TransportResult, TransportError> {
            let payload: Value =
                serde_json::from_str(&envelope.event.payload_json).map_err(|_| TransportError {
                    retryable: false,
                    code: "BAD_PAYLOAD".into(),
                    message: "payload is not JSON".into(),
                    retry_after_ms: None,
                })?;
            let msgs = logic::relay_plan(&payload).map_err(|e| TransportError {
                retryable: false,
                code: e.code.into(),
                message: e.message,
                retry_after_ms: None,
            })?;
            let mut failed = false;
            for m in &msgs {
                let body = json!({ "channel": m.channel, "text": m.text }).to_string();
                if relay::push(&m.provider, &body).is_err() {
                    WasmHost.log("warn", "relay", "push_failed");
                    failed = true;
                }
            }
            if failed {
                return Err(TransportError {
                    retryable: true,
                    code: "RELAY_PUSH_FAILED".into(),
                    message: "one or more relay pushes failed".into(),
                    retry_after_ms: None,
                });
            }
            Ok(TransportResult {
                ok: true,
                status: None,
                detail: Some(format!("relayed:{}", msgs.len())),
                provider_message_id: None,
            })
        }
    }

    impl StreamGuest for Component {
        fn on_start(_i: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported("streaming-lifecycle"))
        }
        fn on_stop(_i: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported("streaming-lifecycle"))
        }
        fn on_segment(_s: SegmentInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported("streaming-lifecycle"))
        }
        fn on_recording_ready(
            _r: RecordingInfo,
        ) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported("streaming-lifecycle"))
        }
    }
}

#[cfg(target_arch = "wasm32")]
use component::Component;
#[cfg(target_arch = "wasm32")]
bindings::export!(Component with_types_in bindings);
