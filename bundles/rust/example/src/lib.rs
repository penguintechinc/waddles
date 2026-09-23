//! Example Tier-1 Rust bundle: a `!welcome`-style process-stage handler
//! that counts how many times each actor has been seen (via the `kv`
//! import) and rewrites the event's payload with a greeting. Exists to
//! prove `waddle-sdk-rs` against the normative WIT world end to end --
//! not shipped to tenants.
//!
//! Implements only [`waddle_sdk::ProcessStage`]; the action-stage export
//! is still present on the compiled component, answering with the SDK's
//! automatic `UNSUPPORTED_STAGE` stub (spec SS6.5), which is itself part
//! of what this example demonstrates.

use serde::{Deserialize, Serialize};
use waddle_sdk::log::{Fields, Level};
use waddle_sdk::{ActionStage, PlatformEvent, ProcessStage, UnsupportedStage};

#[derive(Debug, Deserialize)]
struct InboundPayload {
    message: String,
}

#[derive(Debug, Serialize)]
struct OutboundPayload {
    message: String,
    greeting: String,
    seen_count: i64,
}

struct WelcomeBundle;

impl ProcessStage for WelcomeBundle {
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let ctx = waddle_sdk::context::get_context();
        let payload: InboundPayload = event.payload().unwrap_or_else(|_| InboundPayload {
            message: String::new(),
        });

        let actor = event.actor.clone().unwrap_or_else(|| "unknown".to_string());
        let kv_key = format!("welcome:seen:{actor}");
        let seen_count =
            waddle_sdk::kv::increment(&kv_key, 1, waddle_sdk::kv::NO_EXPIRY).unwrap_or(0);

        if let Ok(fields) = Fields::new()
            .with("actor", &actor)
            .and_then(|f| f.with("tenant", &ctx.tenant))
            .and_then(|f| f.with("seen_count", seen_count))
        {
            let _ =
                waddle_sdk::log::write(Level::Info, "welcome bundle: transform called", &fields);
        }

        let greeting = format!("welcome back, {actor}! (seen {seen_count} times)");
        let outbound = OutboundPayload {
            message: payload.message,
            greeting,
            seen_count,
        };

        let updated = PlatformEvent::with_payload(
            event.platform,
            event.event_type,
            event.actor,
            event.occurred_at,
            &outbound,
        )
        .map_err(|_| UnsupportedStage::process())?;

        Ok(Some(updated))
    }
}

// This bundle only reacts to inbound events -- the action stage is left
// on its SDK-provided default, which answers with the `UNSUPPORTED_STAGE`
// stub the WIT world requires every component to export (spec SS6.5).
// The empty block is required even for the default: Rust only applies a
// trait's default method to a type that explicitly implements the trait.
impl ActionStage for WelcomeBundle {}

waddle_sdk::export_stage!(WelcomeBundle);
