//! Calendar APP bundle (Rust, `stage-next-tools` world): community events, RSVP, approvals,
//! chat commands (`!calendar` / `!event` / `!rsvp`), hub-gateway admin actions, and event sync
//! to external targets by CALLING service-bundle tools (never another app bundle).
//!
//! Layout: `model` (types) | `timeutil` (validation, IANA/RFC 3339) | `store` (kv layout) |
//! `sync` (target adapters + idempotent mapping engine) | `service` (operations) | `cmd`
//! (chat grammar) | `app` (stage-level behaviour + admin actions) | `host` (seams). Everything
//! except the thin wasm glue below is host-testable.

#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;

pub mod app;
pub mod cmd;
pub mod host;
pub mod model;
pub mod service;
pub mod store;
pub mod sync;
pub mod timeutil;

#[cfg(target_arch = "wasm32")]
mod component {
    use serde_json::{Value, json};

    use super::app;
    use super::bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
    use super::bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
    use super::bindings::exports::waddle::bundle::streaming_lifecycle::{
        Guest as LifecycleGuest, RecordingInfo, SegmentInfo, StreamInfo,
    };
    use super::bindings::waddle::bundle::tool::ToolError as WitToolError;
    use super::bindings::waddle::bundle::types::{
        PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
    };
    use super::bindings::waddle::bundle::{clock, context, kv, log, relay, tool};
    use super::host::{CalError, Clock, Ctx, Kv, Logger, ToolInvoker};
    use super::store;
    use waddle_sdk::tool::ToolError;

    pub struct Component;

    struct WasmKv;
    impl Kv for WasmKv {
        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, CalError> {
            kv::get(key).map_err(|_| CalError::Storage("kv get failed".into()))
        }
        fn set(&self, key: &str, value: &[u8], ttl_seconds: u32) -> Result<(), CalError> {
            kv::set(key, value, ttl_seconds).map_err(|_| CalError::Storage("kv set failed".into()))
        }
        fn delete(&self, key: &str) -> Result<(), CalError> {
            kv::delete(key).map_err(|_| CalError::Storage("kv delete failed".into()))
        }
        fn increment(&self, key: &str, delta: i64) -> Result<i64, CalError> {
            kv::increment(key, delta, 0)
                .map_err(|_| CalError::Storage("kv increment failed".into()))
        }
    }

    struct WasmTools;
    impl ToolInvoker for WasmTools {
        fn invoke(
            &self,
            target: &str,
            name: &str,
            args: &Value,
            idempotency_key: &str,
        ) -> Result<Value, ToolError> {
            let bytes =
                serde_json::to_vec(args).map_err(|e| ToolError::InvalidArgs(e.to_string()))?;
            let out =
                tool::invoke(target, name, &bytes, Some(idempotency_key)).map_err(|e| match e {
                    WitToolError::NotGranted => ToolError::NotGranted,
                    WitToolError::Forbidden => ToolError::Forbidden,
                    WitToolError::Timeout => ToolError::Timeout,
                    WitToolError::DepthExceeded => ToolError::DepthExceeded,
                    WitToolError::PoolExhausted => ToolError::PoolExhausted,
                    WitToolError::TargetUnavailable => ToolError::TargetUnavailable,
                    WitToolError::InvalidArgs(m) => ToolError::InvalidArgs(m),
                })?;
            serde_json::from_slice(&out).map_err(|e| ToolError::InvalidArgs(e.to_string()))
        }
    }

    struct WasmClock;
    impl Clock for WasmClock {
        fn now_secs(&self) -> i64 {
            i64::try_from(clock::now_millis() / 1000).unwrap_or(i64::MAX)
        }
    }

    struct WasmLog;
    impl Logger for WasmLog {
        fn log(&self, level: &str, msg: &str, fields: Value) {
            let lvl = match level {
                "error" => log::Level::Error,
                "warn" => log::Level::Warn,
                "debug" => log::Level::Debug,
                _ => log::Level::Info,
            };
            log::write(lvl, msg, &fields.to_string());
        }
    }

    fn unsupported(stage: &str) -> UnsupportedStage {
        UnsupportedStage {
            stage: stage.to_string(),
        }
    }

    fn fatal(code: &str, message: &str) -> TransportError {
        TransportError {
            retryable: false,
            code: code.to_string(),
            message: message.to_string(),
            retry_after_ms: None,
        }
    }

    impl ProcessGuest for Component {
        fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            let payload: Value =
                serde_json::from_str(&event.payload_json).map_err(|_| unsupported("process"))?;
            let out = if event.platform == app::ADMIN_PLATFORM
                && event.event_type == app::ADMIN_EVENT_TYPE
            {
                Some(payload)
            } else {
                app::transform_chat(&payload)
            };
            Ok(out.map(|p| PlatformEvent {
                platform: event.platform,
                event_type: event.event_type,
                actor: event.actor,
                payload_json: p.to_string(),
                occurred_at: event.occurred_at,
            }))
        }
    }

    impl ActionGuest for Component {
        fn dispatch(
            envelope: StageEnvelope,
            _config: String,
        ) -> Result<TransportResult, TransportError> {
            let ev = &envelope.event;
            let payload: Value = serde_json::from_str(&ev.payload_json)
                .map_err(|_| fatal("BAD_PAYLOAD", "payload is not JSON"))?;
            let ctx_in = context::get_context();
            let community = ctx_in.community.clone().unwrap_or_else(|| "0".to_string());
            let kv_h = WasmKv;
            let tools_h = WasmTools;
            let clock_h = WasmClock;
            let log_h = WasmLog;
            let ctx = Ctx {
                kv: &kv_h,
                tools: &tools_h,
                clock: &clock_h,
                log: &log_h,
                tenant: &ctx_in.tenant,
                community: &community,
            };
            let cfg = store::effective_config(&ctx_in.config_json).map_err(|e| {
                log_h.log(
                    "error",
                    "calendar config unreadable",
                    json!({"error": e.to_string()}),
                );
                fatal("CONFIG_INVALID", "calendar configuration is invalid")
            })?;

            if ev.platform == app::ADMIN_PLATFORM && ev.event_type == app::ADMIN_EVENT_TYPE {
                let action = payload.get("action").and_then(Value::as_str).unwrap_or("");
                let args = payload.get("args").cloned().unwrap_or_else(|| json!({}));
                return match app::run_admin(&ctx, &cfg, ev.actor.as_deref(), action, &args) {
                    Ok(v) => {
                        log_h.log(
                            "info",
                            "calendar admin action",
                            json!({"action": action, "ok": true}),
                        );
                        Ok(TransportResult {
                            ok: true,
                            status: Some(200),
                            detail: Some(v.to_string()),
                            provider_message_id: None,
                        })
                    }
                    Err(e) => {
                        log_h.log(
                            "info",
                            "calendar admin action",
                            json!({"action": action, "ok": false}),
                        );
                        Err(fatal(admin_code(&e), &e.to_string()))
                    }
                };
            }

            let channel = payload
                .get("channel_id")
                .and_then(Value::as_str)
                .ok_or_else(|| fatal("MISSING_CHANNEL", "reply requires a channel_id"))?
                .to_string();
            let reply = app::run_chat(&ctx, &cfg, ev.actor.as_deref(), &payload);
            log_h.log(
                if reply.ok { "info" } else { "warn" },
                "calendar command",
                json!({"op": reply.op, "event_id": reply.event_id, "ok": reply.ok, "platform": ev.platform}),
            );
            let msg = json!({"channel": channel, "text": reply.text}).to_string();
            relay::push(&ev.platform, &msg).map_err(|_| TransportError {
                retryable: true,
                code: "RELAY_PUSH_FAILED".into(),
                message: "reply relay failed".into(),
                retry_after_ms: None,
            })?;
            Ok(TransportResult {
                ok: true,
                status: None,
                detail: Some(reply.op.to_string()),
                provider_message_id: None,
            })
        }
    }

    fn admin_code(e: &CalError) -> &'static str {
        match e {
            CalError::Validation(_) => "INVALID",
            CalError::NotFound => "NOT_FOUND",
            CalError::Forbidden => "FORBIDDEN",
            CalError::Conflict(_) => "CONFLICT",
            CalError::Storage(_) => "STORAGE",
        }
    }

    impl LifecycleGuest for Component {
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
