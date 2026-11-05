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

pub use crate::pin_sig::PinKeys;
use crate::pin_sig::{PinFields, PinSigError};

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
    /// The entry has no `pin_sig` integrity stamp (un-stamped producer, or a
    /// writer that is not `svc_action`).
    #[error("outbound entry has no pin signature")]
    MissingSignature,
    /// `pin_sig` is malformed, names an unknown key, or does not match.
    #[error("outbound pin signature is invalid")]
    BadSignature,
    /// The entry is signed under the pre-community-binding (v1) MAC:
    /// authentic but unscoped, so refused. Distinct so a rolling upgrade
    /// (svc_action must be deployed first) shows up in the counter.
    #[error("outbound pin signature predates community binding")]
    LegacyStamp,
    /// The entry's `exp_ms` is in the past (stale or replayed).
    #[error("outbound entry is past its signed expiry")]
    Expired,
    /// `exp_ms` is missing or implausibly far in the future.
    #[error("outbound entry has no plausible signed expiry")]
    ExpiryInvalid,
    /// This process has no usable `OUTBOUND_PIN_KEYS`, so no entry can
    /// be verified -- every chat op is refused (fail closed, never open).
    #[error("outbound pin verification key is not configured")]
    KeyUnconfigured,
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
            Self::MissingSignature => "no_pin_sig",
            Self::BadSignature => "bad_pin_sig",
            Self::LegacyStamp => "pin_sig_legacy",
            Self::Expired => "pin_expired",
            Self::ExpiryInvalid => "pin_exp_invalid",
            Self::KeyUnconfigured => "pin_key_unconfigured",
        }
    }
}

/// Canonical Twitch login: at most ONE leading `#` removed, ASCII-lowercased.
/// No other normalization -- a stamp is machine-produced, so control chars,
/// whitespace, or repeated `#` stay in the string and fail [`is_twitch_login`].
fn canonical_twitch(raw: &str) -> String {
    raw.strip_prefix('#').unwrap_or(raw).to_ascii_lowercase()
}

/// A valid Twitch login: `^[a-z0-9_]{1,25}$`, not all digits (a numeric
/// broadcaster id is not an IRC channel name).
fn is_twitch_login(c: &str) -> bool {
    (1..=25).contains(&c.len())
        && c.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
        && !c.bytes().all(|b| b.is_ascii_digit())
}

/// A Discord snowflake: plain ASCII digits that parse as a non-zero `u64`
/// (the Discord adapter parses the same way), so 20-digit ids above
/// `u64::MAX` and `0` are refused.
fn is_snowflake(id: &str) -> bool {
    id.bytes().all(|b| b.is_ascii_digit()) && id.parse::<u64>().is_ok_and(|n| n != 0)
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

/// The destination-bearing wire fields the producer's `pin_sig` covers.
#[derive(serde::Deserialize)]
struct StampWire {
    #[serde(default)]
    op: Option<String>,
    #[serde(default)]
    channel: Option<String>,
    #[serde(default)]
    origin_channel: Option<String>,
    #[serde(default)]
    message_id: Option<String>,
    #[serde(default)]
    text: Option<String>,
    #[serde(default)]
    user_id: Option<String>,
    #[serde(default)]
    op_id: Option<String>,
    #[serde(default)]
    exp_ms: Option<u64>,
    #[serde(default)]
    pin_sig: Option<String>,
    /// Numeric community the credential is resolved for; MAC-bound (v2).
    #[serde(default)]
    community_id: Option<i64>,
    /// Numeric tenant id when present; MAC-bound (v2).
    #[serde(default)]
    tenant_id: Option<i64>,
}

/// Furthest-future `exp_ms` the drain accepts (producer TTL is 60s at most
/// for chat, the ack timeout for Discord); anything beyond is not a stamp
/// this system produced.
const MAX_EXP_AHEAD_MS: u64 = 300_000;

fn now_epoch_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
}

/// Verifies the producer's integrity stamp on a raw queue entry BEFORE it is
/// acted on. The Valkey ACL lets any app client write the outbound list, so
/// `origin_channel == channel` is only meaningful if the stamp was made by
/// `svc_action` (HMAC under `OUTBOUND_PIN_KEYS`, constant-time verify). EVERY
/// op is covered (`chat.send`, `chat.delete`, `dm.send`) and the MAC binds
/// `exp_ms`, which must be in the future and within [`MAX_EXP_AHEAD_MS`].
///
/// # Errors
/// [`PinError::MissingOrigin`], [`PinError::KeyUnconfigured`],
/// [`PinError::MissingSignature`], [`PinError::BadSignature`],
/// [`PinError::ExpiryInvalid`] or [`PinError::Expired`] -- fail closed.
pub fn verify_stamp(raw: &str, platform: &str, keys: Option<&PinKeys>) -> Result<(), PinError> {
    verify_stamp_at(raw, platform, keys, now_epoch_ms())
}

/// [`verify_stamp`] with an injectable clock (tests).
///
/// # Errors
/// As [`verify_stamp`].
pub fn verify_stamp_at(
    raw: &str,
    platform: &str,
    keys: Option<&PinKeys>,
    now_ms: u64,
) -> Result<(), PinError> {
    let wire: StampWire = serde_json::from_str(raw).map_err(|_| PinError::BadSignature)?;
    let op = wire.op.as_deref().unwrap_or("chat.send");
    let origin = wire
        .origin_channel
        .as_deref()
        .filter(|o| !o.is_empty())
        .ok_or(PinError::MissingOrigin)?;
    let keys = keys.ok_or(PinError::KeyUnconfigured)?;
    let sig = wire
        .pin_sig
        .as_deref()
        .filter(|s| !s.is_empty())
        .ok_or(PinError::MissingSignature)?;
    let exp_ms = wire.exp_ms.ok_or(PinError::ExpiryInvalid)?;
    let fields = PinFields {
        platform,
        op,
        channel: wire.channel.as_deref().unwrap_or(""),
        origin_channel: origin,
        message_id: wire.message_id.as_deref().unwrap_or(""),
        text: wire.text.as_deref().unwrap_or(""),
        user_id: wire.user_id.as_deref().unwrap_or(""),
        op_id: wire.op_id.as_deref().unwrap_or(""),
        exp_ms,
        community_id: wire.community_id.unwrap_or(0),
        tenant_id: wire.tenant_id.unwrap_or(0),
    };
    // Signature first: an attacker must not learn expiry rules from forgeries.
    keys.verify(&fields, sig).map_err(|e| match e {
        PinSigError::MissingSignature => PinError::MissingSignature,
        PinSigError::LegacyUnscoped => PinError::LegacyStamp,
        _ => PinError::BadSignature,
    })?;
    if exp_ms < now_ms {
        return Err(PinError::Expired);
    }
    if exp_ms > now_ms.saturating_add(MAX_EXP_AHEAD_MS) {
        return Err(PinError::ExpiryInvalid);
    }
    Ok(())
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

    /// regression: p987 review -- a 20-digit id above `u64::MAX` is not a
    /// snowflake (the Discord adapter parses u64); zero is not either.
    #[test]
    fn discord_snowflake_must_fit_u64_and_be_nonzero() {
        let max = u64::MAX.to_string();
        assert_eq!(
            pin_target("discord", &max, Some(&max)).as_deref(),
            Ok(max.as_str())
        );
        for bad in ["18446744073709551616", "99999999999999999999", "0", "00"] {
            assert_eq!(
                pin_target("discord", bad, Some(bad)),
                Err(PinError::InvalidChannel),
                "{bad}"
            );
        }
    }

    /// regression: p987 review -- a stamp is machine-produced, so malformed
    /// input is refused rather than normalized (no `##a`, control chars,
    /// uppercase without `#` prefix rules beyond one optional `#`).
    #[test]
    fn twitch_is_strict_not_normalizing() {
        for bad in ["##foo", "#fo\u{0}o", "#fo\ro", " #foo", "#foo ", "\n#foo"] {
            assert_eq!(
                pin_target("twitch", bad, Some("#foo")),
                Err(PinError::InvalidChannel),
                "channel {bad:?}"
            );
            assert_eq!(
                pin_target("twitch", "#foo", Some(bad)),
                Err(PinError::InvalidChannel),
                "origin {bad:?}"
            );
        }
        assert_eq!(
            pin_target("twitch", "foo", Some("#foo")).as_deref(),
            Ok("#foo")
        );
    }

    /// The producer's copy of the signing module must stay byte-identical.
    #[test]
    fn pin_sig_copies_do_not_drift() {
        assert_eq!(
            include_str!("pin_sig.rs"),
            include_str!("../../svc_action/src/pin_sig.rs")
        );
    }

    const NOW: u64 = 1_000_000;

    fn test_keys() -> PinKeys {
        PinKeys::parse("k1:000102030405060708090a0b0c0d0e0f", Some("k1")).unwrap()
    }

    /// Builds a signed entry for `op` with the given fields.
    fn signed_entry(op: &str, user_id: &str, channel: &str, exp_ms: u64) -> String {
        let sig = test_keys()
            .sign(&PinFields {
                platform: "discord",
                op,
                channel,
                origin_channel: "1",
                message_id: "",
                text: "x",
                user_id,
                op_id: "",
                exp_ms,
                community_id: 0,
                tenant_id: 0,
            })
            .unwrap();
        let mut v = serde_json::json!({
            "op": op, "origin_channel": "1", "text": "x", "exp_ms": exp_ms, "pin_sig": sig,
        });
        if !channel.is_empty() {
            v["channel"] = channel.into();
        }
        if !user_id.is_empty() {
            v["user_id"] = user_id.into();
        }
        v.to_string()
    }

    /// A discord chat.send entry signed for `community_id` (0 = none), as the
    /// v2 producer does; `community_id` is also written on the wire.
    fn community_entry(community_id: i64) -> String {
        let exp_ms = NOW + 60_000;
        let sig = test_keys()
            .sign(&PinFields {
                platform: "discord",
                op: "chat.send",
                channel: "1",
                origin_channel: "1",
                message_id: "",
                text: "x",
                user_id: "",
                op_id: "",
                exp_ms,
                community_id,
                tenant_id: 0,
            })
            .unwrap();
        let mut v = serde_json::json!({
            "op": "chat.send", "channel": "1", "origin_channel": "1", "text": "x",
            "exp_ms": exp_ms, "pin_sig": sig,
        });
        if community_id != 0 {
            v["community_id"] = community_id.into();
        }
        v.to_string()
    }

    /// regression: p6a-t6d1 -- a replayed, validly signed entry cannot be
    /// re-pointed at another community (nor given, nor stripped of one).
    #[test]
    fn community_id_on_a_signed_entry_cannot_be_changed_added_or_stripped() {
        let keys = test_keys();
        let signed = community_entry(7);
        assert_eq!(
            verify_stamp_at(&signed, "discord", Some(&keys), NOW),
            Ok(())
        );
        let mut v: serde_json::Value = serde_json::from_str(&signed).unwrap();
        // Another community's id swapped in.
        v["community_id"] = 8.into();
        assert_eq!(
            verify_stamp_at(&v.to_string(), "discord", Some(&keys), NOW),
            Err(PinError::BadSignature)
        );
        // Community stripped (tenant-wide scope).
        v.as_object_mut().unwrap().remove("community_id");
        assert_eq!(
            verify_stamp_at(&v.to_string(), "discord", Some(&keys), NOW),
            Err(PinError::BadSignature)
        );
        // Tenant id injected.
        let mut t: serde_json::Value = serde_json::from_str(&signed).unwrap();
        t["tenant_id"] = 1.into();
        assert_eq!(
            verify_stamp_at(&t.to_string(), "discord", Some(&keys), NOW),
            Err(PinError::BadSignature)
        );
        // Signed with no community: adding one is refused.
        let none = community_entry(0);
        assert_eq!(verify_stamp_at(&none, "discord", Some(&keys), NOW), Ok(()));
        let mut a: serde_json::Value = serde_json::from_str(&none).unwrap();
        a["community_id"] = 7.into();
        assert_eq!(
            verify_stamp_at(&a.to_string(), "discord", Some(&keys), NOW),
            Err(PinError::BadSignature)
        );
        // A non-integer community id is not a stamp this system produced.
        let mut bad: serde_json::Value = serde_json::from_str(&signed).unwrap();
        bad["community_id"] = "7".into();
        assert_eq!(
            verify_stamp_at(&bad.to_string(), "discord", Some(&keys), NOW),
            Err(PinError::BadSignature)
        );
    }

    /// An entry signed by a pre-community-binding (v1) producer is refused
    /// with its own counted reason, not as an anonymous bad signature -- the
    /// rolling-upgrade signal (deploy svc_action first).
    #[test]
    fn a_v1_signed_entry_is_refused_with_a_distinct_counted_reason() {
        let keys = test_keys();
        // The same v1 golden pin_sig.rs asserts; v1 covered these exact fields.
        let v1 = "k1:1ff2c00b8d4eea50b0371acd512329475b9b4397c49148092c8a34436f62100f";
        let raw = serde_json::json!({
            "op": "chat.send", "channel": "123", "origin_channel": "123", "text": "hi",
            "exp_ms": 1_700_000_000_000u64, "pin_sig": v1, "community_id": 7,
        })
        .to_string();
        // platform "discord" (the golden's), clock inside the window.
        let err = verify_stamp_at(&raw, "discord", Some(&keys), 1_700_000_000_000 - 1_000);
        assert_eq!(err, Err(PinError::LegacyStamp));
        assert_eq!(PinError::LegacyStamp.reason(), "pin_sig_legacy");
        let before = refusal_count("discord", "chat.send", "pin_sig_legacy");
        record_refusal("discord", "chat.send", PinError::LegacyStamp);
        assert_eq!(
            refusal_count("discord", "chat.send", "pin_sig_legacy"),
            before + 1
        );
    }

    #[test]
    fn verify_stamp_rules() {
        let keys = test_keys();
        let ok = signed_entry("chat.send", "", "1", NOW + 60_000);
        assert_eq!(verify_stamp_at(&ok, "discord", Some(&keys), NOW), Ok(()));
        // Same bytes under another platform's queue: not valid.
        assert_eq!(
            verify_stamp_at(&ok, "twitch", Some(&keys), NOW),
            Err(PinError::BadSignature)
        );
        assert_eq!(
            verify_stamp_at(&ok, "discord", None, NOW),
            Err(PinError::KeyUnconfigured)
        );
        let no_sig = r#"{"op":"chat.send","channel":"1","origin_channel":"1","text":"x"}"#;
        assert_eq!(
            verify_stamp_at(no_sig, "discord", Some(&keys), NOW),
            Err(PinError::MissingSignature)
        );
        let no_origin = r#"{"op":"chat.send","channel":"1","text":"x"}"#;
        assert_eq!(
            verify_stamp_at(no_origin, "discord", Some(&keys), NOW),
            Err(PinError::MissingOrigin)
        );
        assert_eq!(
            verify_stamp_at("not json", "discord", Some(&keys), NOW),
            Err(PinError::BadSignature)
        );
        for e in [
            PinError::MissingSignature,
            PinError::BadSignature,
            PinError::KeyUnconfigured,
            PinError::Expired,
            PinError::ExpiryInvalid,
        ] {
            assert!(!e.reason().is_empty() && !e.to_string().is_empty());
        }
    }

    /// regression: p987 review -- `dm.send` is signed and verified like every
    /// other op: an unsigned DM is refused (`no_pin_sig`) and a tampered
    /// `user_id` fails the MAC.
    #[test]
    fn dm_send_is_signed_and_verified() {
        let keys = test_keys();
        let dm = signed_entry("dm.send", "42", "", NOW + 60_000);
        assert_eq!(verify_stamp_at(&dm, "discord", Some(&keys), NOW), Ok(()));
        let unsigned =
            r#"{"op":"dm.send","user_id":"42","text":"x","origin_channel":"1","exp_ms":1000060}"#;
        assert_eq!(
            verify_stamp_at(unsigned, "discord", Some(&keys), NOW),
            Err(PinError::MissingSignature)
        );
        let tampered = dm.replace(r#""user_id":"42""#, r#""user_id":"43""#);
        assert_ne!(dm, tampered);
        assert_eq!(
            verify_stamp_at(&tampered, "discord", Some(&keys), NOW),
            Err(PinError::BadSignature)
        );
    }

    /// regression: p987 review -- `op_id` is MAC-bound: re-labelling a signed
    /// entry with another producer's op_id (to settle its ack) is refused.
    #[test]
    fn op_id_is_bound_into_the_mac() {
        let keys = test_keys();
        let signed = signed_entry("chat.send", "", "1", NOW + 60_000);
        let mut v: serde_json::Value = serde_json::from_str(&signed).unwrap();
        v["op_id"] = "victim-op-1".into();
        assert_eq!(
            verify_stamp_at(&v.to_string(), "discord", Some(&keys), NOW),
            Err(PinError::BadSignature)
        );
    }

    /// regression: p987 review -- freshness: expired, missing and
    /// implausibly-far-future `exp_ms` are refused (the MAC binds exp_ms, so
    /// extending it breaks the signature).
    #[test]
    fn exp_ms_is_bound_and_bounded() {
        let keys = test_keys();
        let expired = signed_entry("chat.send", "", "1", NOW - 1);
        assert_eq!(
            verify_stamp_at(&expired, "discord", Some(&keys), NOW),
            Err(PinError::Expired)
        );
        let far = signed_entry("chat.send", "", "1", NOW + MAX_EXP_AHEAD_MS + 1);
        assert_eq!(
            verify_stamp_at(&far, "discord", Some(&keys), NOW),
            Err(PinError::ExpiryInvalid)
        );
        let edge = signed_entry("chat.send", "", "1", NOW + MAX_EXP_AHEAD_MS);
        assert_eq!(verify_stamp_at(&edge, "discord", Some(&keys), NOW), Ok(()));
        let ok = signed_entry("chat.send", "", "1", NOW + 1000);
        let extended = ok.replace("1001000", "1090000");
        assert_ne!(ok, extended);
        assert_eq!(
            verify_stamp_at(&extended, "discord", Some(&keys), NOW),
            Err(PinError::BadSignature)
        );
        let missing = ok.replace(r#""exp_ms":1001000,"#, "");
        let missing = if missing == ok {
            ok.replace(r#","exp_ms":1001000"#, "")
        } else {
            missing
        };
        assert_ne!(ok, missing);
        assert_eq!(
            verify_stamp_at(&missing, "discord", Some(&keys), NOW),
            Err(PinError::ExpiryInvalid)
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
