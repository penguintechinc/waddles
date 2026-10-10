#[allow(warnings)]
mod bindings;

use bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
use bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
use bindings::exports::waddle::bundle::streaming_lifecycle::{
    Guest as StreamingGuest, RecordingInfo, SegmentInfo, StreamInfo,
};
use bindings::waddle::bundle::types::{
    PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
};
use bindings::waddle::bundle::{economy, identity};

struct Component;

/// Renders an `identity.error` as a stable short tag the integration test
/// asserts on (the variant payload, where any, follows a colon).
fn tag(err: identity::Error) -> String {
    match err {
        identity::Error::Denied(code) => format!("denied:{code}"),
        identity::Error::NotLinked => "not-linked".to_string(),
        identity::Error::NotAMember => "not-a-member".to_string(),
        identity::Error::NotFound => "not-found".to_string(),
        identity::Error::Ambiguous => "ambiguous".to_string(),
        identity::Error::Invalid(m) => format!("invalid:{m}"),
        identity::Error::Unavailable(m) => format!("unavailable:{m}"),
        identity::Error::Backend(m) => format!("backend:{m}"),
    }
}

/// Renders an `economy.error` the same way (the steal flow's last hop).
fn eco_tag(err: economy::Error) -> String {
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

/// A points-game `!steal @target <amount>`: resolve the actor, resolve the
/// mention, then move currency between the two RESOLVED uuids. Every hop
/// fails loud and short-circuits -- an unlinked actor/target never reaches
/// `economy.transfer`, and nothing here guesses a uuid.
fn steal(token: &str, amount: u64) -> String {
    let actor = match identity::resolve_actor() {
        Ok(u) => u,
        Err(e) => return format!("actor:{}", tag(e)),
    };
    let target = match identity::resolve_mention(token) {
        Ok(u) => u,
        Err(e) => return format!("target:{}", tag(e)),
    };
    match economy::transfer(&actor, &target, amount) {
        Ok(()) => "steal:ok".to_string(),
        Err(e) => format!("transfer:{}", eco_tag(e)),
    }
}

impl ProcessGuest for Component {
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let p = &event.payload_json;
        let out = match event.event_type.as_str() {
            "id-actor" => match identity::resolve_actor() {
                Ok(u) => format!("ok:{u}"),
                Err(e) => tag(e),
            },
            "id-mention" => match identity::resolve_mention(&str_field(p, "token")) {
                Ok(u) => format!("ok:{u}"),
                Err(e) => tag(e),
            },
            "id-steal" => steal(&str_field(p, "token"), uint_field(p, "amount")),
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
            message: "identity fixture has no action stage".to_string(),
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
