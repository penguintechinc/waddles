//! Wire-format timestamp helpers backed by the `time` crate.
//!
//! Replaces the former `chrono` calls (`to_rfc3339_opts(Millis, true)` and
//! `timestamp_millis()`) while keeping the emitted bytes identical: envelope `ts` fields, the
//! `clock` host capability's `now-rfc3339` op and the bundle-admin millisecond clock all cross
//! process boundaries (Valkey streams, JSON host-API payloads), so the format is a contract.

use time::OffsetDateTime;

/// Formats `t` as RFC 3339 in UTC with exactly three fractional digits and a `Z` suffix
/// (`2023-11-14T22:13:20.123Z`), truncating (never rounding) below a millisecond -- byte-identical
/// to chrono's `to_rfc3339_opts(SecondsFormat::Millis, true)` on a UTC value.
pub fn format_rfc3339_millis(t: OffsetDateTime) -> String {
    let t = t.to_offset(time::UtcOffset::UTC);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        t.year(),
        u8::from(t.month()),
        t.day(),
        t.hour(),
        t.minute(),
        t.second(),
        t.millisecond()
    )
}

/// The current instant formatted by [`format_rfc3339_millis`].
pub fn now_rfc3339_millis() -> String {
    format_rfc3339_millis(OffsetDateTime::now_utc())
}

/// Milliseconds since the Unix epoch, floored toward negative infinity -- the same value chrono's
/// `timestamp_millis()` returned.
pub fn unix_millis(t: OffsetDateTime) -> i64 {
    t.unix_timestamp_nanos().div_euclid(1_000_000) as i64
}

/// The current Unix-epoch milliseconds ([`unix_millis`] of now).
pub fn now_unix_millis() -> i64 {
    unix_millis(OffsetDateTime::now_utc())
}

#[cfg(test)]
mod tests {
    use super::*;
    use time::UtcOffset;

    fn at_nanos(n: i128) -> OffsetDateTime {
        OffsetDateTime::from_unix_timestamp_nanos(n).expect("in range")
    }

    // Golden strings are what chrono's `to_rfc3339_opts(Millis, true)` produced for the same
    // instants (captured before the migration).
    #[test]
    fn golden_epoch() {
        assert_eq!(
            format_rfc3339_millis(at_nanos(0)),
            "1970-01-01T00:00:00.000Z"
        );
    }

    #[test]
    fn golden_millisecond_precision() {
        let t = at_nanos(1_700_000_000_123_000_000);
        assert_eq!(format_rfc3339_millis(t), "2023-11-14T22:13:20.123Z");
    }

    #[test]
    fn golden_whole_second_keeps_zero_fraction() {
        let t = at_nanos(1_700_000_000_000_000_000);
        assert_eq!(format_rfc3339_millis(t), "2023-11-14T22:13:20.000Z");
    }

    #[test]
    fn sub_millisecond_is_truncated_not_rounded() {
        let t = at_nanos(1_700_000_000_123_999_999);
        assert_eq!(format_rfc3339_millis(t), "2023-11-14T22:13:20.123Z");
        assert_eq!(unix_millis(t), 1_700_000_000_123);
    }

    #[test]
    fn non_utc_offset_is_normalised_to_z() {
        let offset = UtcOffset::from_hms(5, 30, 0).expect("valid offset");
        let t = at_nanos(1_700_000_000_123_000_000).to_offset(offset);
        assert_eq!(t.hour(), 3);
        assert_eq!(format_rfc3339_millis(t), "2023-11-14T22:13:20.123Z");
    }

    #[test]
    fn pre_epoch_values_floor_and_pad() {
        let t = at_nanos(-1_000_000);
        assert_eq!(format_rfc3339_millis(t), "1969-12-31T23:59:59.999Z");
        assert_eq!(unix_millis(t), -1);
    }

    #[test]
    fn leap_day_and_year_padding() {
        // 2024-02-29T23:59:59.001Z
        let t = at_nanos(1_709_251_199_001_000_000);
        assert_eq!(format_rfc3339_millis(t), "2024-02-29T23:59:59.001Z");
    }

    #[test]
    fn now_helpers_are_well_formed_and_consistent() {
        let s = now_rfc3339_millis();
        assert_eq!(s.len(), 24, "{s}");
        assert!(s.ends_with('Z'));
        assert!(now_unix_millis() > 1_700_000_000_000);
    }

    #[test]
    fn output_round_trips_through_a_strict_rfc3339_parser() {
        let t = at_nanos(1_700_000_000_123_000_000);
        let parsed = OffsetDateTime::parse(
            &format_rfc3339_millis(t),
            &time::format_description::well_known::Rfc3339,
        )
        .expect("parse");
        assert_eq!(parsed, t);
    }
}
