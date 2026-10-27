#[allow(warnings)]
mod bindings;

use bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
use bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
use bindings::exports::waddle::bundle::streaming_lifecycle::{
    Guest as StreamingGuest, RecordingInfo, SegmentInfo, StreamInfo,
};
use bindings::waddle::bundle::ai;
use bindings::waddle::bundle::types::{
    PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
};

struct Component;

/// Renders an `ai.error` as a stable short tag the integration test asserts on
/// (the variant payload, where any, follows a colon).
fn tag(err: ai::Error) -> String {
    match err {
        ai::Error::Denied(code) => format!("denied:{code}"),
        ai::Error::TooManyTokens(bound) => format!("too-many-tokens:{bound}"),
        ai::Error::ContentFiltered => "content-filtered".to_string(),
        ai::Error::Invalid(m) => format!("invalid:{m}"),
        ai::Error::Unavailable(m) => format!("unavailable:{m}"),
        ai::Error::Backend(m) => format!("backend:{m}"),
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

/// Extracts a top-level unsigned-integer field, `None` when absent.
fn uint_field(json: &str, key: &str) -> Option<u64> {
    let needle = format!("\"{key}\":");
    json.find(&needle).map(|i| {
        let rest = &json[i + needle.len()..];
        let end = rest
            .find(|c: char| !c.is_ascii_digit())
            .unwrap_or(rest.len());
        rest[..end].parse().unwrap_or(0)
    })
}

/// `true` when the flat object carries `"key":true`.
fn bool_field(json: &str, key: &str) -> bool {
    json.contains(&format!("\"{key}\":true"))
}

impl ProcessGuest for Component {
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let p = &event.payload_json;
        let out = match event.event_type.as_str() {
            "ai-generate" => {
                let opts = ai::Options {
                    max_tokens: uint_field(p, "max_tokens").map(|v| v as u32),
                    temperature: uint_field(p, "temperature_x100").map(|v| v as f32 / 100.0),
                    json: bool_field(p, "json"),
                };
                match ai::generate(&str_field(p, "prompt"), opts) {
                    Ok(text) => format!("ok:{text}"),
                    Err(e) => tag(e),
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
    fn dispatch(
        _envelope: StageEnvelope,
        _config: String,
    ) -> Result<TransportResult, TransportError> {
        Err(TransportError {
            retryable: false,
            code: "unsupported".to_string(),
            message: "ai fixture has no action stage".to_string(),
            retry_after_ms: None,
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
