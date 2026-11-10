//! Shared plain-Rust types for the host-testable connector logic. The wasm glue
//! in `lib.rs` maps these onto the generated WIT records.

use serde_json::{Map, Value};

/// The PostHog flag gating the whole connector cutover (default OFF; while OFF
/// the legacy Python `svc_ingest` Kick path runs unchanged).
pub const CONNECTOR_FLAG: &str = "waddles.connector-kick";

/// Platform slug every emitted event carries (the host rejects any other).
pub const PLATFORM: &str = "kick";

/// Prefix of the opaque source id the host gives a WEBHOOK session (`connector::webhook::
/// opaque_source_id`); such a session has no upstream socket, so it takes no handshake.
pub const WEBHOOK_SOURCE_PREFIX: &str = "wh-";

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
    /// JSON object payload, shaped like the Twitch normalizers' payload so downstream consumers need no Kick branch.
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

/// Renders a Kick numeric/string id as a non-empty string. Kick ids are JSON integers on the
/// public API and Pusher alike, strings on a few legacy frames; anything else (bool, float,
/// object, empty string, negative number) is not an id.
pub fn id_string(v: &Value) -> Option<String> {
    match v {
        Value::Number(n) => n.as_u64().map(|u| u.to_string()),
        Value::String(s) if !s.is_empty() => Some(s.clone()),
        _ => None,
    }
}

/// Badge `type` names from a Kick `identity.badges` array (`[{"type":"moderator",...}]`). Names
/// only (no counts/text); an absent or malformed list is empty, never an error.
pub fn badge_types(identity: Option<&Value>) -> Vec<String> {
    identity
        .and_then(|i| i.get("badges"))
        .and_then(Value::as_array)
        .map(|badges| {
            badges
                .iter()
                .filter_map(|b| b.get("type").and_then(Value::as_str))
                .filter(|t| !t.is_empty())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// Strict flag read: ONLY the real JSON boolean `true` grants a flag; a string, number or any
/// other type is `false` (fail closed), mirroring `receivers/badge_flags.strict_flag`.
pub fn strict_flag(v: Option<&Value>) -> bool {
    v.and_then(Value::as_bool) == Some(true)
}

/// Cutover gate decision, split out of the wasm glue so it is host-testable:
/// flag OFF (the default; an unseen/unreachable flag reads OFF) refuses every
/// connector entry point so the legacy Python path stays the only live one.
pub fn gate_decision(flag_enabled: bool) -> Result<(), FrameErr> {
    if flag_enabled {
        Ok(())
    } else {
        Err(FrameErr::Unsupported(
            "connector-kick flag is off".to_string(),
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
        assert_eq!(CONNECTOR_FLAG, "waddles.connector-kick");
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
    fn id_string_accepts_only_unsigned_ints_and_non_empty_strings() {
        use serde_json::json;
        assert_eq!(id_string(&json!(42)).as_deref(), Some("42"));
        assert_eq!(id_string(&json!("a7")).as_deref(), Some("a7"));
        for bad in [
            json!(-1),
            json!(1.5),
            json!(""),
            json!(true),
            json!(null),
            json!({}),
        ] {
            assert_eq!(id_string(&bad), None, "{bad}");
        }
    }

    #[test]
    fn strict_flag_grants_only_on_real_true() {
        use serde_json::json;
        assert!(strict_flag(Some(&json!(true))));
        for bad in [
            json!("true"),
            json!(1),
            json!("false"),
            json!(false),
            json!(null),
        ] {
            assert!(!strict_flag(Some(&bad)), "{bad}");
        }
        assert!(!strict_flag(None));
    }

    #[test]
    fn badge_types_reads_names_only_and_tolerates_junk() {
        use serde_json::json;
        let id = json!({"badges":[{"type":"moderator","text":"Moderator"},{"type":"sub_gifter","count":5},{"text":"x"},{"type":""}]});
        assert_eq!(badge_types(Some(&id)), vec!["moderator", "sub_gifter"]);
        assert!(badge_types(None).is_empty());
        assert!(badge_types(Some(&json!({"badges":"x"}))).is_empty());
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
