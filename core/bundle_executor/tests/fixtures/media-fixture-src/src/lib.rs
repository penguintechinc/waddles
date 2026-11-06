#[allow(warnings)]
mod bindings;

use bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
use bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
use bindings::exports::waddle::bundle::streaming_lifecycle::{
    Guest as StreamingGuest, RecordingInfo, SegmentInfo, StreamInfo,
};
use bindings::waddle::bundle::media;
use bindings::waddle::bundle::types::{
    PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
};

struct Component;

/// Renders a `media.error` as a stable short tag the integration test asserts on.
fn tag(err: media::Error) -> String {
    match err {
        media::Error::TierRequired(t) => format!("tier-required:{t}"),
        media::Error::ByokMissing(p) => format!("byok-missing:{p}"),
        media::Error::ProviderRefused(r) => format!("provider-refused:{r}"),
        media::Error::Quota(w) => format!("quota:{w}"),
        media::Error::Timeout => "timeout".to_string(),
        media::Error::Denied(c) => format!("denied:{c}"),
        media::Error::Invalid(m) => format!("invalid:{m}"),
        media::Error::Unavailable(m) => format!("unavailable:{m}"),
        media::Error::NotFound => "not-found".to_string(),
        media::Error::Backend(m) => format!("backend:{m}"),
    }
}

/// Extracts a top-level JSON string field from a flat object without a JSON
/// dependency (the fixture's payloads are simple, machine-built objects).
fn str_field(json: &str, key: &str) -> String {
    let needle = format!("\"{key}\":\"");
    match json.find(&needle) {
        Some(i) => {
            let rest = &json[i + needle.len()..];
            match rest.find('"') {
                Some(end) => rest[..end].to_string(),
                None => rest.to_string(),
            }
        }
        None => String::new(),
    }
}

impl ProcessGuest for Component {
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let p = &event.payload_json;
        let out = match event.event_type.as_str() {
            "media-image" => match media::generate_image(&str_field(p, "prompt")) {
                Ok(h) => format!("handle:{h}"),
                Err(e) => tag(e),
            },
            "media-submit-music" => match media::submit_music(&str_field(p, "prompt")) {
                Ok(j) => format!("job:{j}"),
                Err(e) => tag(e),
            },
            "media-submit-video" => match media::submit_video(&str_field(p, "prompt")) {
                Ok(j) => format!("job:{j}"),
                Err(e) => tag(e),
            },
            "media-status" => match media::job_status(&str_field(p, "job_id")) {
                Ok(media::JobState::Pending) => "pending".to_string(),
                Ok(media::JobState::Done(h)) => format!("done:{h}"),
                Ok(media::JobState::Failed(e)) => format!("failed:{}", tag(e)),
                Err(e) => tag(e),
            },
            "media-url" => match media::object_url(&str_field(p, "handle")) {
                Ok(u) => format!("url:{u}"),
                Err(e) => tag(e),
            },
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
            message: "media fixture has no action stage".to_string(),
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
