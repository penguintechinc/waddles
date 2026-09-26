//! Minimal, real `!ping` -> `pong` chat bundle: the e2e test's deployable
//! process/action-stage fixture (the "currently-missing deployable test
//! bundle that gates the whole e2e"). Implements both WIT exports against
//! `wit/waddle-bundle/stage.wit` -- [`waddle_sdk::ProcessStage::transform`]
//! recognizes the exact command `!ping` in an inbound `chat.message` and
//! rewrites the event into a `pong` reply on the same platform/channel;
//! [`waddle_sdk::ActionStage::dispatch`] takes that reply and relays it
//! back via the `relay` host import (granted only to action-stage bundles,
//! `wit/waddle-bundle/stage.wit` `interface relay`) to the event's own
//! origin platform (`envelope.event.platform` -- never a fixed provider,
//! so a Discord `!ping`'s pong relays to Discord, not Twitch). Non-matching
//! text produces no reply and no action.
//!
//! Deliberately tiny: this exists to give the executor a small, correct,
//! real component to load in the e2e test, not to demonstrate SDK surface
//! area -- see `bundles/rust/example` for that. Unlike that example, this
//! crate's `dispatch` and component-export wiring are `#[cfg(target_arch
//! = "wasm32")]`-gated so `cargo test` also runs natively on the host
//! (`ProcessStage::transform` is plain, target-independent Rust) --
//! that's what makes the "at the SDK/host level" tests below possible
//! without a WASI test runner.

use serde::{Deserialize, Serialize};
#[cfg(target_arch = "wasm32")]
use waddle_sdk::TransportResult;
use waddle_sdk::{
    ActionStage, PlatformEvent, ProcessStage, StageEnvelope, TransportError, UnsupportedStage,
};

/// The exact command text this bundle reacts to, matched against the
/// trimmed `text` field only -- no prefix/argument parsing, this is a
/// test fixture, not a command framework.
const PING_COMMAND: &str = "!ping";
/// The reply body sent back for a matching command.
const PONG_REPLY: &str = "pong";

/// Inbound Twitch `chat.message` payload shape (spec
/// `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS6.1.1).
#[derive(Debug, Serialize, Deserialize)]
struct ChatMessagePayload {
    text: String,
    #[serde(default)]
    channel_id: Option<String>,
}

/// Outbound reply payload -- carried on the `PlatformEvent` `transform`
/// produces, and read back by `dispatch` to build the relay message.
#[derive(Debug, Serialize, Deserialize)]
struct PongPayload {
    text: String,
    channel_id: Option<String>,
}

/// The `{channel, text}` shape the provider-scoped outbound relay
/// transport (e.g. `waddle_transports.transports.irc_relay.
/// RelayOutboundIrcTransport.send` for Twitch) LPUSHes onto its queue --
/// the wire contract this bundle's `dispatch` must match exactly for
/// svc-ingest's drain loop to deliver the reply, regardless of which
/// provider queue it lands on.
#[derive(Debug, Serialize, PartialEq, Eq)]
struct RelayMessage {
    channel: String,
    text: String,
}

struct PingBundle;

impl ProcessStage for PingBundle {
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let payload: ChatMessagePayload = match event.payload() {
            Ok(payload) => payload,
            // Not a chat.message-shaped payload (or missing `text`): not
            // our command, drop silently rather than erroring the whole
            // pipeline over an event this bundle was never meant to react
            // to.
            Err(_) => return Ok(None),
        };

        if payload.text.trim() != PING_COMMAND {
            return Ok(None);
        }

        let reply = PongPayload {
            text: PONG_REPLY.to_string(),
            channel_id: payload.channel_id,
        };
        let updated = PlatformEvent::with_payload(
            event.platform,
            event.event_type,
            event.actor,
            event.occurred_at,
            &reply,
        )
        .map_err(|_| UnsupportedStage::process())?;

        Ok(Some(updated))
    }
}

/// Builds the outbound provider name and relay message body for a pong
/// reply, target-independent so it is unit-testable on the host without a
/// WASI runner. The provider is always the inbound event's own
/// `platform` (`envelope.event.platform`) -- **never** a fixed constant --
/// so a Discord-origin `!ping` relays its pong to Discord, a Twitch-origin
/// one to Twitch, etc.
fn build_relay(envelope: &StageEnvelope) -> Result<(String, RelayMessage), TransportError> {
    let reply: PongPayload = envelope
        .event
        .payload()
        .map_err(|err| TransportError::fatal("BAD_PAYLOAD", err.to_string()))?;
    let channel = reply.channel_id.ok_or_else(|| {
        TransportError::fatal(
            "MISSING_CHANNEL",
            "pong reply requires a channel_id from the inbound chat.message",
        )
    })?;

    Ok((
        envelope.event.platform.clone(),
        RelayMessage {
            channel,
            text: reply.text,
        },
    ))
}

impl ActionStage for PingBundle {
    /// Only compiled for `wasm32` -- [`waddle_sdk::relay::push`] is a
    /// wasm32-only host call (`sdk/waddle-sdk-rs/src/relay.rs`). On a host
    /// build this impl block is empty, so [`ActionStage`]'s default
    /// `dispatch` (the `UNSUPPORTED_STAGE` stub) applies there; the
    /// executor only ever loads the wasm32 component, so this is the path
    /// that actually runs in production and in the e2e test.
    #[cfg(target_arch = "wasm32")]
    fn dispatch(envelope: StageEnvelope, _config: &str) -> Result<TransportResult, TransportError> {
        let (provider, message) = build_relay(&envelope)?;

        waddle_sdk::relay::push(&provider, &message)
            .map(|()| TransportResult::ok())
            .map_err(|err| TransportError::retryable("RELAY_PUSH_FAILED", err.to_string(), None))
    }
}

#[cfg(target_arch = "wasm32")]
waddle_sdk::export_stage!(PingBundle);

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_event(text: &str) -> PlatformEvent {
        PlatformEvent::with_payload(
            "twitch",
            "chat.message",
            Some("viewer-1".to_string()),
            "2026-09-23T00:00:00.000Z",
            &ChatMessagePayload {
                text: text.to_string(),
                channel_id: Some("12345".to_string()),
            },
        )
        .expect("valid object payload serializes")
    }

    #[test]
    fn ping_produces_a_pong_reply_on_the_same_platform_and_channel() {
        let event = sample_event("!ping");
        let result = PingBundle::transform(event.clone())
            .expect("implemented, not an error")
            .expect("!ping matches, a reply is produced");

        assert_eq!(result.platform, event.platform);
        assert_eq!(result.event_type, event.event_type);

        let reply: PongPayload = result.payload().expect("reply payload deserializes");
        assert_eq!(reply.text, "pong");
        assert_eq!(reply.channel_id.as_deref(), Some("12345"));
    }

    #[test]
    fn ping_with_surrounding_whitespace_still_matches() {
        let result =
            PingBundle::transform(sample_event("  !ping  ")).expect("implemented, not an error");
        assert!(result.is_some());
    }

    #[test]
    fn non_matching_text_produces_no_reply() {
        for text in ["!pingpong", "ping", "!ping extra", "hello", ""] {
            let result =
                PingBundle::transform(sample_event(text)).expect("implemented, not an error");
            assert_eq!(result, None, "unexpected reply for input {text:?}");
        }
    }

    #[test]
    fn non_chat_payload_is_ignored_rather_than_erroring() {
        let event = PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "channel.follow".to_string(),
            actor: None,
            payload_json: "{}".to_string(),
            occurred_at: "2026-09-23T00:00:00.000Z".to_string(),
        };
        let result = PingBundle::transform(event).expect("implemented, not an error");
        assert_eq!(result, None);
    }

    /// Builds a `dispatch`-shaped `StageEnvelope` whose inbound event
    /// originates on `platform`, carrying a pong reply payload -- exactly
    /// what `ProcessStage::transform` would have produced for that
    /// platform's `!ping`.
    fn sample_envelope(platform: &str) -> StageEnvelope {
        StageEnvelope {
            tenant: "tenant-1".to_string(),
            community: None,
            app_id: "waddles.core.example.ping".to_string(),
            stage: "action".to_string(),
            event: PlatformEvent::with_payload(
                platform,
                "chat.message",
                Some("viewer-1".to_string()),
                "2026-09-23T00:00:00.000Z",
                &PongPayload {
                    text: PONG_REPLY.to_string(),
                    channel_id: Some("12345".to_string()),
                },
            )
            .expect("valid object payload serializes"),
            ts: "2026-09-23T00:00:00.000Z".to_string(),
            target_app_id: None,
            trace_context: None,
        }
    }

    #[test]
    fn dispatch_relays_to_the_events_own_origin_platform_not_a_hardcoded_one() {
        // Regression: the bundle used to hardcode a Twitch provider
        // constant, so a Discord-origin `!ping`'s pong was relayed onto
        // the Twitch queue instead of Discord's. The provider must always
        // track `envelope.event.platform`.
        for platform in ["twitch", "discord"] {
            let envelope = sample_envelope(platform);
            let (provider, message) =
                build_relay(&envelope).expect("payload has a channel_id, so this succeeds");

            assert_eq!(
                provider, platform,
                "relay provider must match the inbound event's own platform"
            );
            assert_eq!(
                message,
                RelayMessage {
                    channel: "12345".to_string(),
                    text: "pong".to_string(),
                }
            );
        }
    }

    #[test]
    fn build_relay_errors_when_channel_id_is_missing() {
        let mut envelope = sample_envelope("twitch");
        envelope.event = PlatformEvent::with_payload(
            "twitch",
            "chat.message",
            Some("viewer-1".to_string()),
            "2026-09-23T00:00:00.000Z",
            &PongPayload {
                text: PONG_REPLY.to_string(),
                channel_id: None,
            },
        )
        .expect("valid object payload serializes");

        let err = build_relay(&envelope).expect_err("missing channel_id must fail");
        assert_eq!(err.code, "MISSING_CHANNEL");
        assert!(!err.retryable);
    }

    #[test]
    fn build_relay_errors_on_a_non_pong_payload() {
        let mut envelope = sample_envelope("twitch");
        envelope.event = PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: None,
            payload_json: "{}".to_string(),
            occurred_at: "2026-09-23T00:00:00.000Z".to_string(),
        };

        let err = build_relay(&envelope).expect_err("payload missing `text` must fail");
        assert_eq!(err.code, "BAD_PAYLOAD");
        assert!(!err.retryable);
    }
}
