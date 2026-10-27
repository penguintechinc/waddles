#[allow(warnings)]
mod bindings;

use bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
use bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
use bindings::exports::waddle::bundle::streaming_lifecycle::{
    Guest as StreamingGuest, RecordingInfo, SegmentInfo, StreamInfo,
};
use bindings::waddle::bundle::reputation;
use bindings::waddle::bundle::types::{
    PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
};

struct Component;

/// Renders a `reputation.error` as a stable short tag the integration test
/// asserts on (the variant payload, where any, follows a colon).
fn tag(err: reputation::Error) -> String {
    match err {
        reputation::Error::Denied(code) => format!("denied:{code}"),
        reputation::Error::NotAMember => "not-a-member".to_string(),
        reputation::Error::DailyCapExceeded => "daily-cap-exceeded".to_string(),
        reputation::Error::Invalid(m) => format!("invalid:{m}"),
        reputation::Error::Unavailable(m) => format!("unavailable:{m}"),
        reputation::Error::Backend(m) => format!("backend:{m}"),
    }
}

/// Extracts a top-level JSON string field from a flat object without a JSON
/// dependency (the fixture's payloads are simple, machine-built objects).
fn str_field(json: &str, key: &str) -> String {
    let needle = format!("\"{key}\":\"");
    json.find(&needle)
        .map(|i| {
            let rest = &json[i + needle.len()..];
            rest[..rest.find('"').unwrap_or(rest.len())].to_string()
        })
        .unwrap_or_default()
}

fn int_field(json: &str, key: &str) -> i32 {
    let needle = format!("\"{key}\":");
    json.find(&needle)
        .map(|i| {
            let rest = &json[i + needle.len()..];
            let end = rest
                .find(|c: char| c != '-' && !c.is_ascii_digit())
                .unwrap_or(rest.len());
            rest[..end].parse().unwrap_or(0)
        })
        .unwrap_or(0)
}

impl ProcessGuest for Component {
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let user = str_field(&event.payload_json, "user");
        let out = match event.event_type.as_str() {
            "rep-get" => match reputation::get(&user) {
                Ok(v) => format!("ok:{v}"),
                Err(e) => tag(e),
            },
            "rep-adjust" => {
                let delta = int_field(&event.payload_json, "delta");
                let reason = str_field(&event.payload_json, "reason");
                match reputation::adjust(&user, delta, &reason) {
                    Ok(v) => format!("ok:{v}"),
                    Err(e) => tag(e),
                }
            }
            _ => return Err(UnsupportedStage { stage: "process".to_string() }),
        };
        Ok(Some(PlatformEvent {
            payload_json: format!("{{\"result\":\"{out}\"}}"),
            ..event
        }))
    }
}

impl ActionGuest for Component {
    fn dispatch(_envelope: StageEnvelope, _config: String) -> Result<TransportResult, TransportError> {
        Err(TransportError {
            retryable: false,
            code: "unsupported".to_string(),
            message: "reputation fixture has no action stage".to_string(),
            retry_after_ms: None,
        })
    }
}

impl StreamingGuest for Component {
    fn on_start(_info: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        Err(UnsupportedStage { stage: "on-start".to_string() })
    }
    fn on_stop(_info: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        Err(UnsupportedStage { stage: "on-stop".to_string() })
    }
    fn on_segment(_segment: SegmentInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        Err(UnsupportedStage { stage: "on-segment".to_string() })
    }
    fn on_recording_ready(
        _recording: RecordingInfo,
    ) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        Err(UnsupportedStage { stage: "on-recording-ready".to_string() })
    }
}

bindings::export!(Component with_types_in bindings);
