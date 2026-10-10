#[allow(warnings)]
mod bindings;

use bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
use bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
use bindings::exports::waddle::bundle::streaming_lifecycle::{
    Guest as StreamingGuest, RecordingInfo, SegmentInfo, StreamInfo,
};
use bindings::waddle::bundle::economy;
use bindings::waddle::bundle::types::{
    PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
};

struct Component;

/// Renders an `economy.error` as a stable short tag the integration test
/// asserts on (the variant payload, where any, follows a colon).
fn tag(err: economy::Error) -> String {
    match err {
        economy::Error::Denied(code) => format!("denied:{code}"),
        economy::Error::InsufficientFunds(balance) => format!("insufficient-funds:{balance}"),
        economy::Error::OverCap(cap) => format!("over-cap:{cap}"),
        economy::Error::NotAMember => "not-a-member".to_string(),
        economy::Error::Invalid(m) => format!("invalid:{m}"),
        economy::Error::Unavailable(m) => format!("unavailable:{m}"),
        economy::Error::Backend(m) => format!("backend:{m}"),
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

fn uint_field(json: &str, key: &str) -> u64 {
    let needle = format!("\"{key}\":");
    json.find(&needle)
        .map(|i| {
            let rest = &json[i + needle.len()..];
            let end = rest
                .find(|c: char| !c.is_ascii_digit())
                .unwrap_or(rest.len());
            rest[..end].parse().unwrap_or(0)
        })
        .unwrap_or(0)
}

impl ProcessGuest for Component {
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let p = &event.payload_json;
        let user = str_field(p, "user");
        let out = match event.event_type.as_str() {
            "eco-balance" => match economy::balance(&user) {
                Ok(v) => format!("ok:{v}"),
                Err(e) => tag(e),
            },
            "eco-wager" => {
                match economy::wager(&user, uint_field(p, "stake"), uint_field(p, "payout")) {
                    Ok(v) => format!("ok:{v}"),
                    Err(e) => tag(e),
                }
            }
            "eco-transfer" => {
                let from = str_field(p, "from");
                let to = str_field(p, "to");
                match economy::transfer(&from, &to, uint_field(p, "amount")) {
                    Ok(()) => "ok".to_string(),
                    Err(e) => tag(e),
                }
            }
            "eco-max-bet" => match economy::max_bet(&user) {
                Ok(v) => format!("ok:{v}"),
                Err(e) => tag(e),
            },
            "eco-board" => match economy::leaderboard(uint_field(p, "limit") as u32) {
                Ok(entries) => {
                    let rows: Vec<String> = entries
                        .iter()
                        .map(|e| format!("{}={}", e.user, e.balance))
                        .collect();
                    format!("ok:{}", rows.join(","))
                }
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
            message: "economy fixture has no action stage".to_string(),
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
