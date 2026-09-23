//! Built-in platform senders (spec §4.3/§16 M3 row): "Platform senders
//! become Rust built-ins the bundles reach through the host API (`relay`
//! for Twitch, `http` with guarded egress for REST platforms) -- a bundle
//! never holds a platform credential."
//!
//! This M3 landing implements the Valkey `LPUSH` half of the Twitch relay
//! path in `crate::capabilities::StageCapabilities::handle_relay` (the
//! platform explicitly called out as the relay-based one, and the simplest
//! to land completely: no SSRF-guarded `http` capability, no per-platform
//! credential injection). Discord/Slack/YouTube/Kick all route through the
//! REST `http` capability, which is itself a documented `TODO(M3+)` seam in
//! `crate::capabilities` -- rather than reimplement connector logic here,
//! this module leaves an explicit seam per platform naming the
//! `penguin-connectors` crate that owns it, per the M3 task's own
//! instruction ("USE penguin-connectors' senders where they exist ... do
//! NOT reimplement connector logic in svc_action").
//!
//! **Not yet wired end to end (post-M3 review correction).** This module's
//! own `Platform`/`sender_status`/`is_retryable`/`twitch_relay_args` are
//! not referenced from `crate::dispatch` or anywhere else in this crate
//! outside their own tests below -- `dispatch::invoke_dispatch` hardcodes
//! `"irc_relay"` as its `target_type_hint` rather than calling
//! `Platform::Twitch.as_str()` (`"twitch"`), and nothing builds a `relay`
//! host-call from `twitch_relay_args` today (that call, per the doc below,
//! is issued by the *bundle* via the wire protocol, not by this stage).
//! More fundamentally, `crate::lib::try_start_host_api` always installs
//! `DenyAllCapabilities`, never `StageCapabilities`, as the live
//! connection's handler, so even a bundle-issued `relay` call is denied in
//! every build shipped so far -- see that function's module-level doc
//! correction. Twitch sending is therefore **not functional end to end**
//! in this build; these functions document the intended shape for the
//! caller that will use them once both gaps close, not code `dispatch`
//! currently exercises.

use penguin_bundle_host::wire::HostResultError;

/// The platform a dispatch outcome targeted -- becomes
/// `action_dispatch_log.target_type` (spec: mirrors the Python runner's
/// `result.transport`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Platform {
    Twitch,
    Discord,
    Slack,
    Youtube,
    Kick,
}

impl Platform {
    pub fn as_str(&self) -> &'static str {
        match self {
            Platform::Twitch => "twitch",
            Platform::Discord => "discord",
            Platform::Slack => "slack",
            Platform::Youtube => "youtube",
            Platform::Kick => "kick",
        }
    }
}

/// Whether a sender path is implemented in this build, or is a documented
/// `TODO(M3+)` seam awaiting a `penguin-connectors` REST sender.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SenderStatus {
    /// Fully wired end to end (Twitch, via the `relay` host capability).
    Implemented,
    /// `TODO(M3+): {platform} sender -- pending penguin-connectors
    /// {platform}` -- names exactly which upstream crate/module will back
    /// it once the `http` capability (`crate::capabilities`) lands.
    PendingSeam { reason: &'static str },
}

/// Reports which of the five built-in senders spec §16 M3 lists are
/// implemented in this build vs. an honest seam -- used by `crate::dispatch`
/// to fail a dispatch attempt with a clear, non-retryable error rather than
/// silently no-op for a platform this build doesn't yet send to.
pub fn sender_status(platform: Platform) -> SenderStatus {
    match platform {
        Platform::Twitch => SenderStatus::Implemented,
        Platform::Discord => SenderStatus::PendingSeam {
            reason: "TODO(M3+): discord sender -- pending the http host capability \
                     (crate::capabilities) and penguin-connector-discord's REST sender",
        },
        Platform::Slack => SenderStatus::PendingSeam {
            reason: "TODO(M3+): slack sender -- pending the http host capability \
                     (crate::capabilities) and penguin-connectors' Slack scaffold",
        },
        Platform::Youtube => SenderStatus::PendingSeam {
            reason: "TODO(M3+): youtube sender -- pending the http host capability \
                     (crate::capabilities) and penguin-connectors' YouTube scaffold",
        },
        Platform::Kick => SenderStatus::PendingSeam {
            reason: "TODO(M3+): kick sender -- pending the http host capability \
                     (crate::capabilities) and penguin-connectors' Kick scaffold",
        },
    }
}

/// Builds the `relay` host-call args for a Twitch chat send -- the shape
/// `crate::capabilities::StageCapabilities::handle_relay` expects.
/// `crate::dispatch` issues this over the live host-API `Connection` as a
/// `host-call` frame the executor's `dispatch` export would normally
/// trigger; a bundle never constructs this JSON itself (spec §4.3: "a
/// bundle never holds a platform credential" -- there is no credential
/// here at all, which is exactly why Twitch is relay-based).
pub fn twitch_relay_args(channel: &str, text: &str) -> serde_json::Value {
    serde_json::json!({
        "provider": "twitch",
        "channel": channel,
        "text": text,
    })
}

/// Classifies a `relay` capability's `HostResultError` into whether the
/// dispatch attempt should retry (spec §4.3: `transport-error.retryable`).
/// `invalid_args`/`unknown_provider` are caller bugs -- never retryable;
/// `relay_unavailable` is a transient Valkey outage -- retryable.
pub fn is_retryable(err: &HostResultError) -> bool {
    matches!(err.code.as_str(), "relay_unavailable")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn twitch_is_implemented_every_other_platform_is_a_seam() {
        assert_eq!(sender_status(Platform::Twitch), SenderStatus::Implemented);
        for platform in [
            Platform::Discord,
            Platform::Slack,
            Platform::Youtube,
            Platform::Kick,
        ] {
            assert!(matches!(
                sender_status(platform),
                SenderStatus::PendingSeam { .. }
            ));
        }
    }

    #[test]
    fn platform_as_str_matches_dispatch_log_target_type_convention() {
        assert_eq!(Platform::Twitch.as_str(), "twitch");
        assert_eq!(Platform::Discord.as_str(), "discord");
        assert_eq!(Platform::Slack.as_str(), "slack");
        assert_eq!(Platform::Youtube.as_str(), "youtube");
        assert_eq!(Platform::Kick.as_str(), "kick");
    }

    #[test]
    fn twitch_relay_args_shape_matches_the_relay_capability() {
        let args = twitch_relay_args("#somechannel", "hello");
        assert_eq!(args["provider"], "twitch");
        assert_eq!(args["channel"], "#somechannel");
        assert_eq!(args["text"], "hello");
    }

    #[test]
    fn relay_unavailable_is_retryable_other_codes_are_not() {
        assert!(is_retryable(&HostResultError {
            code: "relay_unavailable".to_string(),
            message: "x".to_string()
        }));
        assert!(!is_retryable(&HostResultError {
            code: "invalid_args".to_string(),
            message: "x".to_string()
        }));
        assert!(!is_retryable(&HostResultError {
            code: "unknown_provider".to_string(),
            message: "x".to_string()
        }));
    }
}
