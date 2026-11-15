//! `galactic-avatars` app bundle: anonymous faction avatars on the stage strip.
//!
//! A chat-stage APP on `world stage-next-tools`. The process stage records chat activity in a
//! community-scoped kv roster (hashed keys only) and emits a strip frame when it changed; the
//! action stage re-validates it and calls `overlay.publish-surface` on this bundle's own
//! namespaced surface (`waddles.core.app.galactic-avatars.stage_strip`). All logic lives in
//! [`logic`]; this file supplies the wasm seam and the WIT exports.
pub mod logic;

#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;

#[cfg(target_arch = "wasm32")]
mod component {
    use super::bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
    use super::bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
    use super::bindings::exports::waddle::bundle::streaming_lifecycle::{
        Guest as StreamingGuest, RecordingInfo, SegmentInfo, StreamInfo,
    };
    use super::bindings::waddle::bundle::types::{
        PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
    };
    use super::bindings::waddle::bundle::{clock, kv, log, overlay};
    use super::logic::{self, Kv, Publisher};

    struct Component;

    /// The bundle's own kv (host-scoped to this community + app); errors carry no detail.
    struct WasmKv;

    impl Kv for WasmKv {
        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            kv::get(key).map_err(|_| "kv unavailable".to_string())
        }
        fn set(&self, key: &str, value: &[u8]) -> Result<(), String> {
            kv::set(key, value, 0).map_err(|_| "kv unavailable".to_string())
        }
    }

    /// Real host publisher over `overlay.publish-surface`.
    struct WasmPublisher;

    impl Publisher for WasmPublisher {
        fn publish(&self, surface: &str, payload_json: &str) -> Result<(), (bool, String)> {
            overlay::publish_surface(surface, payload_json)
                .map(|_| ())
                .map_err(|e| (e.retryable, e.code))
        }
    }

    impl ProcessGuest for Component {
        /// Records one chat message; emits a strip frame event only when it should publish.
        fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            if event.event_type != "chat.message" {
                return Ok(None);
            }
            match logic::on_chat(&WasmKv, event.actor.as_deref(), clock::now_millis()) {
                Ok(Some(strip)) => Ok(Some(PlatformEvent {
                    platform: event.platform,
                    event_type: event.event_type,
                    // The actor is dropped: the frame never carries a user reference.
                    actor: None,
                    payload_json: serde_json::to_string(&strip).unwrap_or_default(),
                    occurred_at: event.occurred_at,
                })),
                Ok(None) => Ok(None),
                Err(e) => {
                    let fields = serde_json::json!({ "error": e }).to_string();
                    log::write(log::Level::Error, "galactic-avatars roster update failed", &fields);
                    Ok(None)
                }
            }
        }
    }

    impl ActionGuest for Component {
        /// Publishes the strip; refusals keep the host's retryable flag and code verbatim.
        fn dispatch(envelope: StageEnvelope, _config: String) -> Result<TransportResult, TransportError> {
            logic::dispatch(&WasmPublisher, &envelope.event.payload_json)
                .map(|()| TransportResult { ok: true, status: None, detail: None, provider_message_id: None })
                .map_err(|(retryable, code)| TransportError {
                    retryable,
                    message: format!("publish refused: {code}"),
                    code,
                    retry_after_ms: None,
                })
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
