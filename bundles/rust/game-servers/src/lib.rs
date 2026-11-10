//! Game-servers APP bundle (`world stage-next-tools`).
//!
//! Process-stage turns a chat command (or a scheduled tick) into a reply by
//! calling the `gameserver` SERVICE bundle's tools; action-stage only relays
//! the finished text. All decisions live in [`logic`] (host-testable); this
//! file is the thin wasm glue over the generated bindings.
#[allow(warnings)]
mod bindings;
mod logic;

use bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
use bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
use bindings::exports::waddle::bundle::streaming_lifecycle::{
    Guest as LifecycleGuest, RecordingInfo, SegmentInfo, StreamInfo,
};
use bindings::waddle::bundle::tool::{self, ToolError as WitToolError};
use bindings::waddle::bundle::types::{
    PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
};
use bindings::waddle::bundle::{context, log, relay};
use logic::{Host, ToolFail};
use serde_json::{Value, json};

struct Component;

/// Event type the host's scheduled-trigger delivery uses (README "Needs from S0").
const EVENT_TICK: &str = "schedule.tick";

/// Real host: tool calls + logging through the generated imports.
struct WasmHost;

impl Host for WasmHost {
    fn invoke(&self, name: &str, args: &Value, idem: Option<&str>) -> Result<Value, ToolFail> {
        let bytes = serde_json::to_vec(args).map_err(|e| ToolFail::Invalid(e.to_string()))?;
        match tool::invoke(logic::SERVICE, name, &bytes, idem) {
            Ok(out) => serde_json::from_slice(&out)
                .map_err(|_| ToolFail::Invalid("bad_server_response".into())),
            Err(WitToolError::NotGranted) => Err(ToolFail::NotGranted),
            Err(WitToolError::Forbidden) => Err(ToolFail::Forbidden),
            Err(WitToolError::Timeout) => Err(ToolFail::Timeout),
            Err(WitToolError::InvalidArgs(m)) => Err(ToolFail::Invalid(m)),
            Err(
                WitToolError::DepthExceeded
                | WitToolError::PoolExhausted
                | WitToolError::TargetUnavailable,
            ) => Err(ToolFail::Unavailable),
        }
    }

    fn log(&self, message: &str, fields: &Value) {
        log::write(log::Level::Info, message, &fields.to_string());
    }
}

fn reply_event(src: &PlatformEvent, platform: &str, channel: &str, reply: &str) -> PlatformEvent {
    PlatformEvent {
        platform: platform.to_string(),
        event_type: src.event_type.clone(),
        actor: src.actor.clone(),
        payload_json: json!({"channel_id": channel, "reply": reply}).to_string(),
        occurred_at: src.occurred_at.clone(),
    }
}

impl ProcessGuest for Component {
    /// Chat commands and scheduled status ticks; anything else is dropped.
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let payload: Value = serde_json::from_str(&event.payload_json).unwrap_or(Value::Null);
        let host = WasmHost;
        if event.event_type == EVENT_TICK {
            let ctx = context::get_context();
            let config: Value = serde_json::from_str(&ctx.config_json).unwrap_or(Value::Null);
            return Ok(match logic::post_config(&config) {
                Ok(None) => None,
                Ok(Some(cfg)) => {
                    let text = match logic::scheduled_status(&host, &cfg) {
                        Ok(t) => t,
                        Err(f) => logic::fail_text(&f),
                    };
                    Some(reply_event(&event, &cfg.platform, &cfg.channel_id, &text))
                }
                Err(msg) => {
                    host.log(
                        "game-servers status post misconfigured",
                        &json!({"error": msg}),
                    );
                    None
                }
            });
        }
        let Some(text) = payload.get("text").and_then(Value::as_str) else {
            return Ok(None);
        };
        let Some(channel) = payload.get("channel_id").and_then(Value::as_str) else {
            return Ok(None);
        };
        let message_id = context::get_context().message_id;
        Ok(logic::handle_chat(&host, text, &message_id)
            .map(|reply| reply_event(&event, &event.platform, channel, &reply)))
    }
}

impl ActionGuest for Component {
    /// Relays the already-computed reply to the originating platform/channel.
    fn dispatch(
        envelope: StageEnvelope,
        _config: String,
    ) -> Result<TransportResult, TransportError> {
        let fail = |code: &str, msg: &str| TransportError {
            retryable: false,
            code: code.to_string(),
            message: msg.to_string(),
            retry_after_ms: None,
        };
        let payload: Value = serde_json::from_str(&envelope.event.payload_json)
            .map_err(|_| fail("BAD_PAYLOAD", "payload is not JSON"))?;
        let channel = payload
            .get("channel_id")
            .and_then(Value::as_str)
            .filter(|c| !c.is_empty())
            .ok_or_else(|| fail("MISSING_CHANNEL", "reply needs channel_id"))?;
        let reply = payload
            .get("reply")
            .and_then(Value::as_str)
            .filter(|r| !r.is_empty())
            .ok_or_else(|| fail("MISSING_REPLY", "reply text missing"))?;
        let msg = json!({"channel": channel, "text": reply}).to_string();
        relay::push(&envelope.event.platform, &msg).map_err(|e| TransportError {
            retryable: matches!(e, relay::Error::Backend(_)),
            code: "RELAY_FAILED".to_string(),
            message: "relay push failed".to_string(),
            retry_after_ms: None,
        })?;
        Ok(TransportResult {
            ok: true,
            status: None,
            detail: Some("game_servers_reply".to_string()),
            provider_message_id: None,
        })
    }
}

fn unsupported() -> UnsupportedStage {
    UnsupportedStage {
        stage: "streaming-lifecycle".to_string(),
    }
}

impl LifecycleGuest for Component {
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

bindings::export!(Component with_types_in bindings);
