//! Timestamp helpers built on the `time` crate.
//!
//! Replaces the former `chrono` usage. Every textual form that reaches a
//! consumer (HTTP JSON, object-store metadata, billing references) is kept
//! byte-identical to what `chrono` produced, so downstream parsers see no
//! change; the golden tests below pin that.

use serde::{Serialize, Serializer};
use time::macros::format_description;
use time::{OffsetDateTime, PrimitiveDateTime, UtcOffset};

/// Current time as a UTC `OffsetDateTime`.
pub fn now() -> OffsetDateTime {
    OffsetDateTime::now_utc()
}

/// Current Unix time in whole seconds (token `iat`/`exp` arithmetic).
pub fn unix_now() -> i64 {
    now().unix_timestamp()
}

/// Formats the date-time body shared by both output shapes:
/// `YYYY-MM-DDTHH:MM:SS` plus a `.fff`, `.ffffff` or `.fffffffff` fraction
/// only when non-zero (the shortest of 3/6/9 digits that is exact),
/// matching chrono's `SecondsFormat::AutoSi`.
fn format_body(dt: OffsetDateTime) -> String {
    let dt = dt.to_offset(UtcOffset::UTC);
    let mut out = format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
        dt.year(),
        u8::from(dt.month()),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    );
    let nanos = dt.nanosecond();
    if nanos != 0 {
        if nanos.is_multiple_of(1_000_000) {
            out.push_str(&format!(".{:03}", nanos / 1_000_000));
        } else if nanos.is_multiple_of(1_000) {
            out.push_str(&format!(".{:06}", nanos / 1_000));
        } else {
            out.push_str(&format!(".{nanos:09}"));
        }
    }
    out
}

/// RFC 3339 with a numeric `+00:00` offset -- byte-identical to
/// `chrono::DateTime<Utc>::to_rfc3339()`.
pub fn to_rfc3339_offset(dt: OffsetDateTime) -> String {
    format!("{}+00:00", format_body(dt))
}

/// RFC 3339 with a `Z` suffix -- byte-identical to chrono's serde
/// serialization of `DateTime<Utc>`.
pub fn to_rfc3339_z(dt: OffsetDateTime) -> String {
    format!("{}Z", format_body(dt))
}

/// `serde(serialize_with = ...)` adapter emitting the `Z` form.
pub fn serialize_z<S: Serializer>(dt: &OffsetDateTime, s: S) -> Result<S::Ok, S::Error> {
    to_rfc3339_z(*dt).serialize(s)
}

/// Parses the `%Y%m%d%H%M%S` stem ffmpeg's segment muxer writes into a UTC
/// time; `None` for anything that is not exactly 14 digits of a valid date.
pub fn parse_compact_utc(s: &str) -> Option<OffsetDateTime> {
    if s.len() != 14 || !s.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let fmt = format_description!("[year][month][day][hour][minute][second]");
    PrimitiveDateTime::parse(s, &fmt)
        .ok()
        .map(PrimitiveDateTime::assume_utc)
}

#[cfg(test)]
mod tests {
    use super::*;
    use time::Duration;

    fn at(unix: i64, nanos: i64) -> OffsetDateTime {
        OffsetDateTime::from_unix_timestamp(unix).unwrap() + Duration::nanoseconds(nanos)
    }

    // Golden strings produced by chrono 0.4.45 (`to_rfc3339` / serde_json)
    // for the same instants before the migration.
    #[test]
    fn golden_formats_match_chrono() {
        let cases: [(i64, i64, &str, &str); 7] = [
            (0, 0, "1970-01-01T00:00:00+00:00", "1970-01-01T00:00:00Z"),
            (
                1_789_128_000,
                0,
                "2026-09-11T12:00:00+00:00",
                "2026-09-11T12:00:00Z",
            ),
            (
                1_789_128_000,
                123_000_000,
                "2026-09-11T12:00:00.123+00:00",
                "2026-09-11T12:00:00.123Z",
            ),
            (
                1_789_128_000,
                123_456_000,
                "2026-09-11T12:00:00.123456+00:00",
                "2026-09-11T12:00:00.123456Z",
            ),
            (
                1_789_128_000,
                123_456_789,
                "2026-09-11T12:00:00.123456789+00:00",
                "2026-09-11T12:00:00.123456789Z",
            ),
            (
                1_789_128_000,
                500_000_000,
                "2026-09-11T12:00:00.500+00:00",
                "2026-09-11T12:00:00.500Z",
            ),
            (
                1_789_128_000,
                1_000,
                "2026-09-11T12:00:00.000001+00:00",
                "2026-09-11T12:00:00.000001Z",
            ),
        ];
        for (unix, nanos, off, z) in cases {
            let dt = at(unix, nanos);
            assert_eq!(to_rfc3339_offset(dt), off);
            assert_eq!(to_rfc3339_z(dt), z);
        }
    }

    #[test]
    fn non_utc_offsets_are_normalised_to_utc() {
        let dt = at(1_789_128_000, 0).to_offset(UtcOffset::from_hms(5, 30, 0).unwrap());
        assert_eq!(to_rfc3339_offset(dt), "2026-09-11T12:00:00+00:00");
        assert_eq!(to_rfc3339_z(dt), "2026-09-11T12:00:00Z");
    }

    #[test]
    fn serialize_z_matches_chrono_serde_shape() {
        #[derive(serde::Serialize)]
        struct W {
            #[serde(serialize_with = "serialize_z")]
            t: OffsetDateTime,
        }
        let json = serde_json::to_string(&W {
            t: at(1_789_128_000, 250_000_000),
        })
        .unwrap();
        assert_eq!(json, r#"{"t":"2026-09-11T12:00:00.250Z"}"#);
    }

    #[test]
    fn compact_parse_accepts_and_rejects_like_chrono() {
        let cases: [(&str, Option<i64>); 8] = [
            ("20260911120000", Some(1_789_128_000)),
            ("19700101000000", Some(0)),
            ("20261301120000", None),
            ("2026091112000", None),
            ("202609111200000", None),
            ("2026-09-11 12:00", None),
            ("abcdefghijklmn", None),
            ("", None),
        ];
        for (input, want) in cases {
            assert_eq!(
                parse_compact_utc(input).map(OffsetDateTime::unix_timestamp),
                want,
                "input {input:?}"
            );
        }
    }

    #[test]
    fn unix_now_tracks_the_clock() {
        let a = unix_now();
        let b = now().unix_timestamp();
        assert!((b - a).abs() <= 1);
    }
}
