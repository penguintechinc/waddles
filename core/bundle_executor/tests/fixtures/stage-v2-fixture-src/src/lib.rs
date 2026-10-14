//! `world stage-v2` reference component (numeric-scope contract proof).
//!
//! `transform` branches on `event.event-type`:
//! - `scope`: reads `context-v2.get-context` and echoes the AUTHORITATIVE
//!   numeric scope (`tenant-id:community-id`) plus the display strings, or
//!   `no-scope` when the host reports `tenant-id == 0` (the fail-closed
//!   sentinel a bundle must refuse to act on).
//!
//! `dispatch` (`action-stage-v2`) echoes the envelope's numeric scope (and
//! its display strings) back in `provider-message-id`, proving the v2
//! envelope's ids cross the host boundary intact.
#[allow(warnings)]
mod bindings;

use bindings::exports::waddle::bundle::action_stage_v2::Guest as ActionGuest;
use bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
use bindings::exports::waddle::bundle::streaming_lifecycle::{
    Guest as StreamingGuest, RecordingInfo, SegmentInfo, StreamInfo,
};
use bindings::waddle::bundle::context_v2;
use bindings::waddle::bundle::types::{
    PlatformEvent, TransportError, TransportResult, UnsupportedStage,
};
use bindings::waddle::bundle::types_v2::StageEnvelope;

struct Component;

impl ProcessGuest for Component {
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let out = match event.event_type.as_str() {
            "scope" => {
                let ctx = context_v2::get_context();
                if ctx.tenant_id == 0 {
                    "no-scope".to_string()
                } else {
                    format!(
                        "{}:{}:{}:{}",
                        ctx.tenant_id,
                        ctx.community_id,
                        ctx.tenant,
                        ctx.community.unwrap_or_default()
                    )
                }
            }
            _ => {
                return Err(UnsupportedStage {
                    stage: "process".to_string(),
                })
            }
        };
        Ok(Some(PlatformEvent {
            payload_json: format!("{{\"result\":\"{out}\"}}"),
            ..event
        }))
    }
}

impl ActionGuest for Component {
    fn dispatch(envelope: StageEnvelope, _config: String) -> Result<TransportResult, TransportError> {
        Ok(TransportResult {
            ok: true,
            status: None,
            detail: None,
            provider_message_id: Some(format!(
                "{}:{}:{}:{}",
                envelope.tenant_id,
                envelope.community_id,
                envelope.tenant,
                envelope.community.unwrap_or_default()
            )),
        })
    }
}

impl StreamingGuest for Component {
    fn on_start(_info: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        Err(UnsupportedStage {
            stage: "on-start".to_string(),
        })
    }
    fn on_stop(_info: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        Err(UnsupportedStage {
            stage: "on-stop".to_string(),
        })
    }
    fn on_segment(_segment: SegmentInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        Err(UnsupportedStage {
            stage: "on-segment".to_string(),
        })
    }
    fn on_recording_ready(
        _recording: RecordingInfo,
    ) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        Err(UnsupportedStage {
            stage: "on-recording-ready".to_string(),
        })
    }
}

bindings::export!(Component with_types_in bindings);
