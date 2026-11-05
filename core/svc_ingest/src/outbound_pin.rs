//! Host-side destination pinning for the outbound relay drain.
//!
//! The relay producer (`svc_action`) pins every channel-targeted op to the
//! triggering event's own channel (`origin_channel`) and stamps both fields
//! on the queue envelope. This module is the consumer-side second lock: the
//! drain refuses to send unless the target `channel` equals the stamped
//! `origin_channel` AND is a well-formed platform channel id, so a message
//! that carries a different (bundle-chosen, tampered, or legacy un-pinned)
//! destination is refused loudly with a typed [`PinError`] instead of being
//! delivered. Bundles only ever supply message content.
//!
//! Logs and metrics here are PII-free: only platform/op/reason are emitted,
//! never a channel name, id, or message text.

use std::sync::LazyLock;

/// Why a queued target failed the host-pin check. Each maps to a stable
/// machine `reason` code ([`PinError::reason`]).
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum PinError {
    /// The entry carries no `origin_channel` stamp (legacy/un-pinned or
    /// forged producer) -- never guessed at, always refused.
    #[error("outbound entry has no host-pinned origin channel")]
    MissingOrigin,
    /// `channel` or `origin_channel` is not a valid id for the platform
    /// (also rejects IRC multi-target lists such as `#a,#b`).
    #[error("outbound target is not a valid channel id")]
    InvalidChannel,
    /// `channel` differs from the host-pinned `origin_channel`.
    #[error("outbound target channel is not the host-pinned origin channel")]
    ChannelMismatch,
    /// The platform has no pin policy (fail closed, not open).
    #[error("no pin policy for platform")]
    UnsupportedPlatform,
}

impl PinError {
    /// Stable machine code, used as the `reason` metric label and as the
    /// `SenderError::Rejected` reason the producer maps to its own error.
    #[must_use]
    pub fn reason(self) -> &'static str {
        match self {
            Self::MissingOrigin => "no_origin_pin",
            Self::InvalidChannel => "invalid_channel",
            Self::ChannelMismatch => "channel_not_pinned",
            Self::UnsupportedPlatform => "unsupported_platform",
        }
    }
}

/// Canonical Twitch login: strip control chars and a leading `#`, lowercase.
fn canonical_twitch(raw: &str) -> String {
    raw.chars()
        .filter(|c| !c.is_control())
        .collect::<String>()
        .trim_start_matches('#')
        .to_ascii_lowercase()
}

/// A valid Twitch login: `^[a-z0-9_]{1,25}$`, not all digits (a numeric
/// broadcaster id is not an IRC channel name).
fn is_twitch_login(c: &str) -> bool {
    (1..=25).contains(&c.len())
        && c.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
        && !c.bytes().all(|b| b.is_ascii_digit())
}

/// A Discord snowflake: 1..=20 ASCII digits (unsigned 64-bit decimal).
fn is_snowflake(id: &str) -> bool {
    (1..=20).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_digit())
}

/// Verifies `channel` is exactly the host-pinned `origin` for `platform`
/// and returns the canonical destination to send to (`#login` for Twitch,
/// the snowflake for Discord).
///
/// # Errors
/// [`PinError`] -- fail closed on a missing origin, a malformed id, a
/// mismatch, or an unknown platform.
pub fn pin_target(platform: &str, channel: &str, origin: Option<&str>) -> Result<String, PinError> {
    let origin = origin
        .filter(|o| !o.is_empty())
        .ok_or(PinError::MissingOrigin)?;
    match platform {
        "twitch" => {
            let (c, o) = (canonical_twitch(channel), canonical_twitch(origin));
            if !is_twitch_login(&c) || !is_twitch_login(&o) {
                return Err(PinError::InvalidChannel);
            }
            if c != o {
                return Err(PinError::ChannelMismatch);
            }
            Ok(format!("#{o}"))
        }
        "discord" => {
            if !is_snowflake(channel) || !is_snowflake(origin) {
                return Err(PinError::InvalidChannel);
            }
            if channel != origin {
                return Err(PinError::ChannelMismatch);
            }
            Ok(origin.to_string())
        }
        _ => Err(PinError::UnsupportedPlatform),
    }
}

static PIN_REFUSED: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_outbound_pin_refused_total",
            "Outbound relay ops refused by the host-pin check, labeled by platform/op/reason",
        ),
        &["platform", "op", "reason"],
    )
    .expect("valid metric definition")
});

/// Registers the pin-refusal counter on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(PIN_REFUSED.clone()))
}

/// Records one refusal: a warn log + counter, both PII-free.
pub fn record_refusal(platform: &str, op: &'static str, err: PinError) {
    tracing::warn!(
        platform,
        op,
        reason = err.reason(),
        "outbound refused: target is not the host-pinned origin channel"
    );
    PIN_REFUSED
        .with_label_values(&[platform, op, err.reason()])
        .inc();
}

/// Current refusal count for a label set (test/observability helper).
#[must_use]
pub fn refusal_count(platform: &str, op: &str, reason: &str) -> u64 {
    PIN_REFUSED.with_label_values(&[platform, op, reason]).get()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn twitch_matching_origin_pins_canonical() {
        assert_eq!(
            pin_target("twitch", "#Foo", Some("foo")).as_deref(),
            Ok("#foo")
        );
    }

    #[test]
    fn twitch_other_channel_refused_regression_p0() {
        // regression: bundle-supplied channel differing from origin (original bug)
        assert_eq!(
            pin_target("twitch", "#victim", Some("#mine")),
            Err(PinError::ChannelMismatch)
        );
    }

    #[test]
    fn missing_or_empty_origin_refused() {
        assert_eq!(
            pin_target("twitch", "#a", None),
            Err(PinError::MissingOrigin)
        );
        assert_eq!(
            pin_target("discord", "1", Some("")),
            Err(PinError::MissingOrigin)
        );
    }

    #[test]
    fn twitch_invalid_ids_refused() {
        for bad in [
            "#a,#b",
            "#a b",
            "#",
            "#123456",
            "#toolongtoolongtoolongtoolong1",
            "#a\r\nJOIN #b",
        ] {
            assert_eq!(
                pin_target("twitch", bad, Some(bad)),
                Err(PinError::InvalidChannel),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn discord_snowflake_rules() {
        assert_eq!(
            pin_target("discord", "123", Some("123")).as_deref(),
            Ok("123")
        );
        assert_eq!(
            pin_target("discord", "123", Some("124")),
            Err(PinError::ChannelMismatch)
        );
        assert_eq!(
            pin_target("discord", "../x", Some("../x")),
            Err(PinError::InvalidChannel)
        );
        let long = "123456789012345678901";
        assert_eq!(
            pin_target("discord", long, Some(long)),
            Err(PinError::InvalidChannel)
        );
    }

    #[test]
    fn unknown_platform_fails_closed() {
        assert_eq!(
            pin_target("slack", "1", Some("1")),
            Err(PinError::UnsupportedPlatform)
        );
    }

    #[test]
    fn reasons_are_stable_codes() {
        let all = [
            (PinError::MissingOrigin, "no_origin_pin"),
            (PinError::InvalidChannel, "invalid_channel"),
            (PinError::ChannelMismatch, "channel_not_pinned"),
            (PinError::UnsupportedPlatform, "unsupported_platform"),
        ];
        for (e, r) in all {
            assert_eq!(e.reason(), r);
            assert!(!e.to_string().is_empty());
        }
    }

    #[test]
    fn refusal_counts_and_registers() {
        let before = refusal_count("twitch", "chat.send", "channel_not_pinned");
        record_refusal("twitch", "chat.send", PinError::ChannelMismatch);
        assert_eq!(
            refusal_count("twitch", "chat.send", "channel_not_pinned"),
            before + 1
        );
        let reg = prometheus::Registry::new();
        register_metrics(&reg).expect("register");
        assert!(register_metrics(&reg).is_err());
        let names: Vec<String> = reg.gather().iter().map(|f| f.name().to_string()).collect();
        assert!(names.contains(&"svc_ingest_outbound_pin_refused_total".to_string()));
    }
}
