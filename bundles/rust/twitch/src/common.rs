//! Shared plain-Rust types for the host-testable overlay logic. The wasm glue
//! in `lib.rs` maps these onto the generated WIT records.

use serde_json::{Map, Value};

/// The PostHog flag gating the whole overlay cutover (default OFF; while OFF
/// the hardcoded `svc_ingest` Twitch path runs unchanged).
pub const OVERLAY_FLAG: &str = "waddles.overlay-twitch";

/// Platform slug every emitted event carries (the host rejects any other).
pub const PLATFORM: &str = "twitch";

/// A connector-reported frame failure (mirrors WIT `frame-error`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FrameErr {
    /// The overlay does not handle this input kind.
    Unsupported(String),
    /// The input is structurally invalid.
    Malformed(String),
    /// A backend/protocol-level problem the host should react to.
    Backend(String),
}

/// One normalized event (mirrors WIT `normalized-event`). `source` is NOT
/// carried: the host derives it from the pinned connection (never the overlay).
#[derive(Debug, Clone, PartialEq)]
pub struct NormEvent {
    /// Dotted lowercase event type (`message`, `channel.follow`, ...).
    pub event_type: String,
    /// Actor login, `None` for a system event (parity with the hardcoded path).
    pub actor: Option<String>,
    /// JSON object payload, identical to the hardcoded normalizers' payload.
    pub payload: Map<String, Value>,
    /// Host-stamped arrival time, passed through.
    pub occurred_at: String,
}

impl NormEvent {
    /// Serializes the payload to the canonical `payload-json` string.
    pub fn payload_json(&self) -> String {
        Value::Object(self.payload.clone()).to_string()
    }
}

/// Cutover gate decision, split out of the wasm glue so it is host-testable:
/// flag OFF (the default; an unseen/unreachable flag reads OFF) refuses every
/// overlay entry point so the hardcoded path stays the only live one.
pub fn gate_decision(flag_enabled: bool) -> Result<(), FrameErr> {
    if flag_enabled {
        Ok(())
    } else {
        Err(FrameErr::Unsupported(
            "overlay-twitch flag is off".to_string(),
        ))
    }
}

/// Default the glue passes to `flags::enabled(OVERLAY_FLAG, ..)`: OFF.
pub const OVERLAY_FLAG_DEFAULT: bool = false;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flag_off_refuses_and_default_is_off() {
        assert_eq!(OVERLAY_FLAG, "waddles.overlay-twitch");
        assert!(!std::hint::black_box(OVERLAY_FLAG_DEFAULT));
        assert!(matches!(
            gate_decision(OVERLAY_FLAG_DEFAULT),
            Err(FrameErr::Unsupported(_))
        ));
        assert!(gate_decision(true).is_ok());
    }

    #[test]
    fn payload_json_round_trips() {
        let e = NormEvent {
            event_type: "message".into(),
            actor: None,
            payload: Map::new(),
            occurred_at: "t".into(),
        };
        assert_eq!(e.payload_json(), "{}");
    }
}
