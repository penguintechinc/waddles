//! Example Tier-1 Rust bundle: a `!welcome`-style process-stage handler
//! that counts how many events it has greeted (via the `kv` import) and
//! rewrites the event's payload with a greeting. Exists to prove
//! `waddle-sdk-rs` against the normative WIT world end to end -- not
//! shipped to tenants.
//!
//! Implements only [`waddle_sdk::ProcessStage`]; the action-stage export
//! is still present on the compiled component, answering with the SDK's
//! automatic `UNSUPPORTED_STAGE` stub (spec SS6.5), which is itself part
//! of what this example demonstrates.
//!
//! The business logic ([`build_reply`], [`log_fields`], [`SEEN_KEY`]) is
//! target-independent so `cargo test` covers it on the host; only the
//! host-call glue (`kv`, `log`, `context`, the stage impls and the
//! component export) is `#[cfg(target_arch = "wasm32")]`-gated, because
//! the SDK's capability functions only exist on `wasm32` -- the same
//! split `bundles/rust/ping` uses. That glue is verified by the CI
//! `cargo component build` + `wasm-tools component wit` job instead.
//!
//! Hygiene: the `kv` key is colon-free (the host reserves `:`, gh-631) and
//! carries no actor identity, and no log line carries the actor or any
//! message text (gh-674) -- a bundle runs outside the PII boundary.

use serde::{Deserialize, Serialize};
use waddle_sdk::log::Fields;
#[cfg(target_arch = "wasm32")]
use waddle_sdk::log::Level;
#[cfg(target_arch = "wasm32")]
use waddle_sdk::{ActionStage, ProcessStage, UnsupportedStage};
use waddle_sdk::{PlatformEvent, SdkError};

/// `kv` key counting the events this bundle has greeted. Scoped per
/// (tenant, community, app) by the host; colon-free (gh-631) and carries
/// no actor identity (gh-674).
#[cfg_attr(not(any(test, target_arch = "wasm32")), allow(dead_code))]
const SEEN_KEY: &str = "welcome.seen";

#[derive(Debug, Deserialize)]
struct InboundPayload {
    message: String,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
struct OutboundPayload {
    message: String,
    greeting: String,
    seen_count: i64,
}

/// The greeting text for `actor` after `seen_count` greeted events. The
/// actor appears only in the reply that goes back to the same channel --
/// never in a `kv` key or a log line.
#[cfg_attr(not(any(test, target_arch = "wasm32")), allow(dead_code))]
fn greeting(actor: &str, seen_count: i64) -> String {
    format!("welcome back, {actor}! (seen {seen_count} events)")
}

/// Rewrites `event` into the greeting reply: the inbound `message` is
/// echoed (an unparseable payload degrades to an empty message rather than
/// failing the pipeline) alongside the greeting and the post-increment
/// `seen_count`.
///
/// # Errors
///
/// Returns [`SdkError`] if the outbound payload cannot be serialized as a
/// JSON object.
#[cfg_attr(not(any(test, target_arch = "wasm32")), allow(dead_code))]
fn build_reply(event: PlatformEvent, seen_count: i64) -> Result<PlatformEvent, SdkError> {
    let payload: InboundPayload = event.payload().unwrap_or_else(|_| InboundPayload {
        message: String::new(),
    });
    let actor = event.actor.clone().unwrap_or_else(|| "unknown".to_string());
    let outbound = OutboundPayload {
        message: payload.message,
        greeting: greeting(&actor, seen_count),
        seen_count,
    };
    PlatformEvent::with_payload(
        event.platform,
        event.event_type,
        event.actor,
        event.occurred_at,
        &outbound,
    )
}

/// Structured fields for the transform log line: tenant (a non-PII id) and
/// the running count only -- deliberately never the actor or the message.
///
/// # Errors
///
/// Returns [`SdkError`] if a field value cannot be serialized.
#[cfg_attr(not(any(test, target_arch = "wasm32")), allow(dead_code))]
fn log_fields(tenant: &str, seen_count: i64) -> Result<Fields, SdkError> {
    Fields::new()
        .with("tenant", tenant)
        .and_then(|f| f.with("seen_count", seen_count))
}

#[cfg(target_arch = "wasm32")]
struct WelcomeBundle;

#[cfg(target_arch = "wasm32")]
impl ProcessStage for WelcomeBundle {
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let ctx = waddle_sdk::context::get_context();

        let seen_count =
            waddle_sdk::kv::increment(SEEN_KEY, 1, waddle_sdk::kv::NO_EXPIRY).unwrap_or(0);

        if let Ok(fields) = log_fields(&ctx.tenant, seen_count) {
            let _ =
                waddle_sdk::log::write(Level::Info, "welcome bundle: transform called", &fields);
        }

        let updated = build_reply(event, seen_count).map_err(|_| UnsupportedStage::process())?;
        Ok(Some(updated))
    }
}

// This bundle only reacts to inbound events -- the action stage is left
// on its SDK-provided default, which answers with the `UNSUPPORTED_STAGE`
// stub the WIT world requires every component to export (spec SS6.5).
// The empty block is required even for the default: Rust only applies a
// trait's default method to a type that explicitly implements the trait.
#[cfg(target_arch = "wasm32")]
impl ActionStage for WelcomeBundle {}

#[cfg(target_arch = "wasm32")]
waddle_sdk::export_stage!(WelcomeBundle);

#[cfg(test)]
mod tests {
    use super::*;

    const PII_ACTOR: &str = "PIIACTOR_alice";
    const PII_TEXT: &str = "PIITEXT_secret_phrase";

    fn event(actor: Option<&str>, payload_json: &str) -> PlatformEvent {
        PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: actor.map(str::to_string),
            payload_json: payload_json.to_string(),
            occurred_at: "2026-10-09T00:00:00.000Z".to_string(),
        }
    }

    fn reply(event: PlatformEvent, seen_count: i64) -> (PlatformEvent, OutboundPayload) {
        let updated = build_reply(event, seen_count).expect("object payload serializes");
        let payload: OutboundPayload = updated.payload().expect("reply payload deserializes");
        (updated, payload)
    }

    #[test]
    fn reply_greets_the_actor_and_echoes_the_message_and_count() {
        let (updated, payload) = reply(event(Some("viewer-1"), r#"{"message":"hello"}"#), 3);
        assert_eq!(
            payload,
            OutboundPayload {
                message: "hello".to_string(),
                greeting: "welcome back, viewer-1! (seen 3 events)".to_string(),
                seen_count: 3,
            }
        );
        // Platform, event type, actor and timestamp carry through unchanged.
        assert_eq!(updated.platform, "twitch");
        assert_eq!(updated.event_type, "chat.message");
        assert_eq!(updated.actor.as_deref(), Some("viewer-1"));
        assert_eq!(updated.occurred_at, "2026-10-09T00:00:00.000Z");
    }

    #[test]
    fn missing_actor_is_greeted_as_unknown() {
        let (updated, payload) = reply(event(None, r#"{"message":"hi"}"#), 1);
        assert_eq!(payload.greeting, "welcome back, unknown! (seen 1 events)");
        assert_eq!(updated.actor, None);
    }

    #[test]
    fn unparseable_or_wrongly_shaped_payload_degrades_to_an_empty_message() {
        for bad in ["{not json", "{}", r#"{"message":42}"#, "[]", ""] {
            let (_, payload) = reply(event(Some("viewer-1"), bad), 2);
            assert_eq!(
                payload.message, "",
                "payload {bad:?} should degrade to empty"
            );
            assert_eq!(payload.seen_count, 2);
        }
    }

    #[test]
    fn greeting_formats_actor_and_count() {
        assert_eq!(greeting("a", 0), "welcome back, a! (seen 0 events)");
        assert_eq!(greeting("a", -1), "welcome back, a! (seen -1 events)");
    }

    // regression: gh-631 -- the `kv` host rejects any guest key byte outside ASCII
    // alnum + `_`/`-`/`.` (`:` is its reserved namespace separator). The key used to be
    // `welcome:seen:{actor}`, which every real host call rejected -- silently, because the
    // transform swallows a kv error with `unwrap_or(0)`.
    #[test]
    fn kv_key_satisfies_the_host_charset() {
        assert!(!SEEN_KEY.is_empty() && SEEN_KEY.len() <= 256);
        assert!(
            SEEN_KEY
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.')),
            "key {SEEN_KEY:?} has a byte the real kv host rejects"
        );
        assert!(!SEEN_KEY.contains(':'));
    }

    // regression: gh-674 -- the key used to embed the raw actor, and the log line carried
    // it; a bundle runs outside the PII boundary, so neither may hold identity or text.
    #[test]
    fn kv_key_and_log_fields_never_carry_the_actor_or_message_text() {
        assert!(!SEEN_KEY.contains(PII_ACTOR));
        let fields = log_fields("tenant-1", 7).expect("fields serialize");
        // `Fields` has no public serializer on the host build; its `Debug` form lists every
        // key and value, which is exactly what must be PII-free.
        let dump = format!("{fields:?}");
        assert!(dump.contains("tenant") && dump.contains("tenant-1"));
        assert!(dump.contains("seen_count") && dump.contains('7'));
        assert!(!dump.contains(PII_ACTOR));
        assert!(!dump.contains(PII_TEXT));
        assert!(!dump.contains("actor"));
        assert!(!dump.contains("message"));
    }

    #[test]
    fn the_reply_is_the_only_place_the_actor_appears() {
        let (updated, _) = reply(
            event(Some(PII_ACTOR), &format!(r#"{{"message":"{PII_TEXT}"}}"#)),
            1,
        );
        assert!(updated.payload_json.contains(PII_ACTOR));
        let fields = log_fields("tenant-1", 1).expect("fields serialize");
        let dump = format!("{fields:?}");
        assert!(!dump.contains(PII_ACTOR) && !dump.contains(PII_TEXT));
    }

    #[test]
    fn inbound_payload_requires_a_string_message() {
        let ok = event(None, r#"{"message":"x"}"#).payload::<InboundPayload>();
        assert_eq!(ok.expect("string message parses").message, "x");
        assert!(
            event(None, r#"{"message":1}"#)
                .payload::<InboundPayload>()
                .is_err()
        );
    }
}
