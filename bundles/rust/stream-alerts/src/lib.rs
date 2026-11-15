//! `stream-alerts` app bundle: follow/sub/cheer/raid/donation alerts on the overlay.
//!
//! A chat-stage APP on `world stage-next-tools`. The process stage turns an event into a
//! text-only alert; the action stage re-validates it and calls `overlay.publish-surface` on this
//! bundle's own namespaced surface (`waddles.core.app.stream-alerts.alerts`). All logic lives in
//! [`logic`]; this file supplies the wasm seam and the WIT exports.
pub mod logic;

#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;

#[cfg(target_arch = "wasm32")]
mod component {
    use serde_json::Value;

    use super::bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
    use super::bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
    use super::bindings::exports::waddle::bundle::streaming_lifecycle::{
        Guest as StreamingGuest, RecordingInfo, SegmentInfo, StreamInfo,
    };
    use super::bindings::waddle::bundle::overlay;
    use super::bindings::waddle::bundle::types::{
        PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
    };
    use super::logic::{self, Publisher};

    struct Component;

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
        /// Maps one inbound event to an alert frame event (or none).
        fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            let payload: Value = serde_json::from_str(&event.payload_json).unwrap_or(Value::Null);
            Ok(logic::build_alert(&event.event_type, &payload).map(|alert| PlatformEvent {
                platform: event.platform,
                event_type: event.event_type,
                // The actor is dropped: alerts never carry a user reference.
                actor: None,
                payload_json: serde_json::to_string(&alert).unwrap_or_default(),
                occurred_at: event.occurred_at,
            }))
        }
    }

    impl ActionGuest for Component {
        /// Publishes the alert; refusals keep the host's retryable flag and code verbatim.
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
