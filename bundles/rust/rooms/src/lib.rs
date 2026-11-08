//! Rooms APP bundle (`world stage-next-tools`).
//!
//! Chat commands and gateway admin actions over community rooms; every media or
//! token operation is a gated `tool.invoke` against the `webrtc` SERVICE bundle.
//! All behavior lives in [`logic`] behind three seams; this file supplies the
//! real wasm implementations (kv, tool, identity) and the stage exports:
//! process-stage does the work (identity + tools run there, as in the other
//! tool-calling apps) and emits a reply event; action-stage only relays it.
#[allow(warnings)]
mod bindings;
mod logic;

use bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
use bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
use bindings::exports::waddle::bundle::streaming_lifecycle::{
    Guest as LifecycleGuest, RecordingInfo, SegmentInfo, StreamInfo,
};
use bindings::waddle::bundle::types::{
    PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
};
use bindings::waddle::bundle::{clock, context, identity, kv, log, relay, tool};
use logic::{Ident, Store, ToolFail, Tools};
use serde_json::{Value, json};

struct Component;

/// Real `tool.invoke` against the webrtc service bundle.
struct WasmTools;

impl Tools for WasmTools {
    fn invoke(&self, name: &str, args: &Value, idem: Option<&str>) -> Result<Value, ToolFail> {
        let body = serde_json::to_vec(args).map_err(|e| ToolFail::InvalidArgs(e.to_string()))?;
        match tool::invoke(logic::SERVICE, name, &body, idem) {
            Ok(bytes) => serde_json::from_slice(&bytes).map_err(|_| ToolFail::Unavailable),
            Err(tool::ToolError::NotGranted) => Err(ToolFail::NotGranted),
            Err(tool::ToolError::Forbidden) => Err(ToolFail::Forbidden),
            Err(tool::ToolError::InvalidArgs(m)) => Err(ToolFail::InvalidArgs(m)),
            Err(_) => Err(ToolFail::Unavailable),
        }
    }
}

/// The bundle's own community-scoped kv.
struct WasmKv;

impl Store for WasmKv {
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
        kv::get(key).map_err(|_| "kv unavailable".to_string())
    }
    fn set(&self, key: &str, value: &[u8]) -> Result<(), String> {
        kv::set(key, value, 0).map_err(|_| "kv unavailable".to_string())
    }
    fn delete(&self, key: &str) -> Result<(), String> {
        kv::delete(key).map_err(|_| "kv unavailable".to_string())
    }
}

/// The host-verified actor (community `user-uuid`).
struct WasmIdent;

impl Ident for WasmIdent {
    fn actor(&self) -> Result<String, String> {
        identity::resolve_actor().map_err(|_| "actor unresolved".to_string())
    }
}

/// One PII-free log line: op label + outcome only.
fn log_outcome(op: &str, outcome: &str) {
    let fields = json!({ "op": op, "outcome": outcome }).to_string();
    log::write(log::Level::Debug, "rooms op", &fields);
}

fn reply_event(src: &PlatformEvent, payload: Value) -> PlatformEvent {
    PlatformEvent {
        platform: src.platform.clone(),
        event_type: src.event_type.clone(),
        actor: src.actor.clone(),
        payload_json: payload.to_string(),
        occurred_at: src.occurred_at.clone(),
    }
}

impl ProcessGuest for Component {
    /// Handles a `!room` chat message or a gateway `admin.action` event.
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let payload: Value = serde_json::from_str(&event.payload_json).unwrap_or(Value::Null);
        let ctx = context::get_context();
        if event.event_type == "admin.action" {
            // The event type and payload authorize NOTHING: the actor is the host-
            // verified one, `args.actor` is rejected, and `logic` requires the
            // host-enforced community:admin check (admin.verify) per mutating op.
            // Results are emitted as an event, so credentials are never produced here.
            let op = payload.get("op").and_then(Value::as_str).unwrap_or("");
            let args = payload.get("args").cloned().unwrap_or(Value::Null);
            let now = (clock::now_millis() / 1000) as i64;
            let out = match WasmIdent.actor() {
                Ok(actor) => logic::admin_for_event(
                    op,
                    &args,
                    &WasmTools,
                    &WasmKv,
                    &ctx.message_id,
                    now,
                    &actor,
                ),
                Err(_) => Err(logic::RoomsError::Denied(
                    "could not verify you as a member".into(),
                )),
            };
            let label = if op.starts_with("room.") || op.starts_with("acl.") || op == "join_token" {
                op
            } else {
                "unknown"
            };
            log_outcome(label, if out.is_ok() { "ok" } else { "error" });
            let body = match out {
                Ok(v) => json!({"ok": true, "result": v}),
                Err(e) => json!({"ok": false, "error": logic::user_message(&e)}),
            };
            return Ok(Some(reply_event(&event, json!({"result": body}))));
        }
        let Some(text) = payload.get("text").and_then(Value::as_str) else {
            return Ok(None);
        };
        let Some(channel) = payload.get("channel_id").and_then(Value::as_str) else {
            return Ok(None);
        };
        let base: Option<String> = serde_json::from_str::<Value>(&ctx.config_json)
            .ok()
            .and_then(|c| {
                c.get("join_base_url")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            });
        let Some(reply) = logic::handle_chat(
            text,
            &ctx.message_id,
            &WasmTools,
            &WasmKv,
            &WasmIdent,
            base.as_deref(),
        ) else {
            return Ok(None);
        };
        log_outcome("chat", "replied");
        Ok(Some(reply_event(
            &event,
            json!({"channel_id": channel, "reply": reply}),
        )))
    }
}

impl ActionGuest for Component {
    /// Relays the already-computed reply; never touches room state.
    fn dispatch(
        envelope: StageEnvelope,
        _config: String,
    ) -> Result<TransportResult, TransportError> {
        let payload: Value =
            serde_json::from_str(&envelope.event.payload_json).unwrap_or(Value::Null);
        let fail = |code: &str, msg: &str| TransportError {
            retryable: false,
            code: code.to_string(),
            message: msg.to_string(),
            retry_after_ms: None,
        };
        // Admin results ride back on the event; only chat replies are relayed.
        let (Some(channel), Some(text)) = (
            payload.get("channel_id").and_then(Value::as_str),
            payload.get("reply").and_then(Value::as_str),
        ) else {
            return Ok(TransportResult {
                ok: true,
                status: None,
                detail: Some("rooms_no_relay".into()),
                provider_message_id: None,
            });
        };
        if envelope.community.is_none() {
            return Err(fail("no_community", "rooms reply requires a community"));
        }
        let msg = json!({"channel": channel, "text": text}).to_string();
        relay::push(&envelope.event.platform, &msg)
            .map_err(|_| fail("relay_failed", "relay push failed"))?;
        Ok(TransportResult {
            ok: true,
            status: None,
            detail: Some("rooms_reply".into()),
            provider_message_id: None,
        })
    }
}

impl LifecycleGuest for Component {
    fn on_start(_: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        Err(UnsupportedStage {
            stage: "streaming-lifecycle".into(),
        })
    }
    fn on_stop(_: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        Err(UnsupportedStage {
            stage: "streaming-lifecycle".into(),
        })
    }
    fn on_segment(_: SegmentInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        Err(UnsupportedStage {
            stage: "streaming-lifecycle".into(),
        })
    }
    fn on_recording_ready(_: RecordingInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        Err(UnsupportedStage {
            stage: "streaming-lifecycle".into(),
        })
    }
}

bindings::export!(Component with_types_in bindings);
