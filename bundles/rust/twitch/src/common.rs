//! Shared plain-Rust types for the host-testable connector logic. The wasm glue
//! in `lib.rs` maps these onto the generated WIT records.

use serde_json::{Map, Value};

/// The PostHog flag gating the whole connector cutover (default OFF; while OFF
/// the hardcoded `svc_ingest` Twitch path runs unchanged).
pub const CONNECTOR_FLAG: &str = "waddles.connector-twitch";

/// Platform slug every emitted event carries (the host rejects any other).
pub const PLATFORM: &str = "twitch";

/// A connector-reported frame failure (mirrors WIT `frame-error`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FrameErr {
    /// The connector does not handle this input kind.
    Unsupported(String),
    /// The input is structurally invalid.
    Malformed(String),
    /// A backend/protocol-level problem the host should react to.
    Backend(String),
}

/// One normalized event (mirrors WIT `normalized-event`). `source` is NOT
/// carried: the host derives it from the pinned connection (never the connector).
#[derive(Debug, Clone, PartialEq)]
pub struct NormEvent {
    /// Dotted lowercase event type (`message`, `channel.follow`, ...).
    pub event_type: String,
    /// Platform user id the host-side `identity.lookup` resolves to the
    /// canonical UUID actor (login only when the wire carries no id). NEVER
    /// published as-is: [`resolve_actors`] replaces it, or drops the event.
    pub actor_key: Option<String>,
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

impl ResolvedEvent {
    /// Serializes the payload to the canonical `payload-json` string.
    pub fn payload_json(&self) -> String {
        Value::Object(self.payload.clone()).to_string()
    }
}

/// An event whose actor is the canonical identity UUID (fail-closed output).
#[derive(Debug, Clone, PartialEq)]
pub struct ResolvedEvent {
    /// Dotted lowercase event type.
    pub event_type: String,
    /// `identity.lookup` UUID, `None` for a system event. Never a raw handle.
    pub actor: Option<String>,
    /// JSON object payload.
    pub payload: Map<String, Value>,
    /// Host-stamped arrival time.
    pub occurred_at: String,
}

/// Resolves every event's actor key through `lookup` (identity.lookup). There
/// is NO raw-handle fallback: an event whose lookup fails is dropped and
/// counted; the caller fails the frame loudly if that leaves nothing to publish.
/// Returns `(resolved, dropped)`.
pub fn resolve_actors(
    events: Vec<NormEvent>,
    lookup: &dyn Fn(&str) -> Result<String, ()>,
) -> (Vec<ResolvedEvent>, usize) {
    let mut out = Vec::with_capacity(events.len());
    let mut dropped = 0;
    for e in events {
        let actor = match &e.actor_key {
            None => None,
            Some(key) => match lookup(key) {
                Ok(uuid) => Some(uuid),
                Err(()) => {
                    dropped += 1;
                    continue;
                }
            },
        };
        out.push(ResolvedEvent {
            event_type: e.event_type,
            actor,
            payload: e.payload,
            occurred_at: e.occurred_at,
        });
    }
    (out, dropped)
}

/// Cutover gate decision, split out of the wasm glue so it is host-testable:
/// flag OFF (the default; an unseen/unreachable flag reads OFF) refuses every
/// connector entry point so the hardcoded path stays the only live one.
pub fn gate_decision(flag_enabled: bool) -> Result<(), FrameErr> {
    if flag_enabled {
        Ok(())
    } else {
        Err(FrameErr::Unsupported(
            "connector-twitch flag is off".to_string(),
        ))
    }
}

/// Default the glue passes to `flags::enabled(CONNECTOR_FLAG, ..)`: OFF.
pub const CONNECTOR_FLAG_DEFAULT: bool = false;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flag_off_refuses_and_default_is_off() {
        assert_eq!(CONNECTOR_FLAG, "waddles.connector-twitch");
        assert!(!std::hint::black_box(CONNECTOR_FLAG_DEFAULT));
        assert!(matches!(
            gate_decision(CONNECTOR_FLAG_DEFAULT),
            Err(FrameErr::Unsupported(_))
        ));
        assert!(gate_decision(true).is_ok());
    }

    fn ev(key: Option<&str>) -> NormEvent {
        NormEvent {
            event_type: "message".into(),
            actor_key: key.map(str::to_string),
            payload: Map::new(),
            occurred_at: "t".into(),
        }
    }

    #[test]
    fn actors_resolve_to_uuid_never_the_key() {
        let (out, dropped) = resolve_actors(vec![ev(Some("123")), ev(None)], &|k| {
            Ok(format!("uuid-{k}"))
        });
        assert_eq!(dropped, 0);
        assert_eq!(out[0].actor.as_deref(), Some("uuid-123"));
        assert_eq!(out[1].actor, None);
    }

    #[test]
    fn failed_lookup_drops_the_event_with_no_raw_fallback() {
        let (out, dropped) = resolve_actors(vec![ev(Some("secrethandle"))], &|_| Err(()));
        assert!(out.is_empty());
        assert_eq!(dropped, 1);
        assert!(!format!("{out:?}").contains("secrethandle"));
    }

    #[test]
    fn payload_json_round_trips() {
        let e = NormEvent {
            event_type: "message".into(),
            actor_key: None,
            payload: Map::new(),
            occurred_at: "t".into(),
        };
        assert_eq!(e.payload_json(), "{}");
    }
}
