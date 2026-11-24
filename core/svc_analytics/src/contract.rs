//! Strict validation of one `waddles:analytics` stream entry against the wire
//! contract in `docs/analytics-foundation.md`. Nothing from the stream is trusted:
//! unknown version, missing/duplicate/extra fields, non-canonical numbers, bad codes,
//! a non-UUID `user_uuid` and an implausible timestamp all reject with a PII-free
//! reason code (never the offending value).

use uuid::Uuid;

/// Highest member timestamp skew into the future that is tolerated (ms).
pub const MAX_FUTURE_MS: i64 = 300_000;
/// Oldest event accepted (ms): the longest retention any row can have.
pub const MAX_AGE_MS: i64 = 400 * 86_400_000;

const FIELDS: [&str; 8] = [
    "v",
    "tenant_id",
    "community_id",
    "user_uuid",
    "platform_code",
    "kind_code",
    "ts_ms",
    "provenance_code",
];

/// What happened (`kind_code`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Kind {
    /// 1 -- a chat message.
    Message,
    /// 2 -- a command invocation.
    Command,
    /// 3 -- a member joined.
    Join,
    /// 4 -- a member left.
    Leave,
    /// 5 -- presence started.
    PresenceStart,
    /// 6 -- presence ended.
    PresenceEnd,
}

impl Kind {
    fn from_code(c: i64) -> Option<Self> {
        Some(match c {
            1 => Self::Message,
            2 => Self::Command,
            3 => Self::Join,
            4 => Self::Leave,
            5 => Self::PresenceStart,
            6 => Self::PresenceEnd,
            _ => return None,
        })
    }

    /// The wire code.
    pub fn code(self) -> i16 {
        match self {
            Self::Message => 1,
            Self::Command => 2,
            Self::Join => 3,
            Self::Leave => 4,
            Self::PresenceStart => 5,
            Self::PresenceEnd => 6,
        }
    }
}

/// A fully validated analytics event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    /// Numeric tenant id (0 = default tenant).
    pub tenant_id: i32,
    /// Numeric community id (>= 1).
    pub community_id: i32,
    /// Member UUID token.
    pub user_uuid: Uuid,
    /// Platform 1..=5.
    pub platform_code: i16,
    /// Event kind.
    pub kind: Kind,
    /// Event time, epoch ms.
    pub ts_ms: i64,
    /// Provenance 1..=5.
    pub provenance_code: i16,
}

/// Why an entry was rejected. Codes are a fixed, bounded set (safe as metric labels).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reject {
    /// A field value was not valid UTF-8.
    NonUtf8,
    /// `v` missing or not `1`.
    UnknownVersion,
    /// A contract field is absent.
    MissingField,
    /// A field appears twice.
    DuplicateField,
    /// A field outside the contract (free text is never accepted).
    UnexpectedField,
    /// `tenant_id` invalid.
    BadTenantId,
    /// `community_id` invalid.
    BadCommunityId,
    /// `user_uuid` not a canonical non-nil UUID.
    BadUserUuid,
    /// `platform_code` outside 1..=5.
    BadPlatformCode,
    /// `kind_code` outside 1..=6.
    BadKindCode,
    /// `ts_ms` not a canonical integer.
    BadTs,
    /// `ts_ms` too far in the future or older than any retention.
    TsOutOfRange,
    /// `provenance_code` outside 1..=5.
    BadProvenanceCode,
}

impl Reject {
    /// Stable machine-readable, PII-free reason code.
    pub fn code(self) -> &'static str {
        match self {
            Self::NonUtf8 => "non_utf8",
            Self::UnknownVersion => "unknown_version",
            Self::MissingField => "missing_field",
            Self::DuplicateField => "duplicate_field",
            Self::UnexpectedField => "unexpected_field",
            Self::BadTenantId => "bad_tenant_id",
            Self::BadCommunityId => "bad_community_id",
            Self::BadUserUuid => "bad_user_uuid",
            Self::BadPlatformCode => "bad_platform_code",
            Self::BadKindCode => "bad_kind_code",
            Self::BadTs => "bad_ts",
            Self::TsOutOfRange => "ts_out_of_range",
            Self::BadProvenanceCode => "bad_provenance_code",
        }
    }
}

/// Parses a canonical unsigned decimal (ASCII digits only, no sign/space, no leading
/// zeros except "0", at most 15 digits).
fn canonical_int(s: &str) -> Option<i64> {
    let b = s.as_bytes();
    if b.is_empty() || b.len() > 15 || !b.iter().all(u8::is_ascii_digit) {
        return None;
    }
    if b.len() > 1 && b[0] == b'0' {
        return None;
    }
    s.parse().ok()
}

fn canonical_uuid(s: &str) -> Option<Uuid> {
    if s.len() != 36 {
        return None;
    }
    let u = Uuid::try_parse(s).ok()?;
    (!u.is_nil()).then_some(u)
}

/// Validates `fields` (raw name/value pairs of one stream entry) against the contract.
/// `now_ms` is injected so range checks are deterministic.
///
/// # Errors
/// The [`Reject`] reason for the first violation found.
pub fn parse(fields: &[(String, Vec<u8>)], now_ms: i64) -> Result<Event, Reject> {
    let mut slots: [Option<&str>; 8] = [None; 8];
    for (name, raw) in fields {
        let idx = FIELDS
            .iter()
            .position(|f| f == name)
            .ok_or(Reject::UnexpectedField)?;
        if slots[idx].is_some() {
            return Err(Reject::DuplicateField);
        }
        slots[idx] = Some(std::str::from_utf8(raw).map_err(|_| Reject::NonUtf8)?);
    }
    if slots[0] != Some("1") && slots[0].is_some() {
        return Err(Reject::UnknownVersion);
    }
    if slots[0].is_none() {
        return Err(Reject::MissingField);
    }
    let get = |i: usize| slots[i].ok_or(Reject::MissingField);
    let (tenant, community, user, platform, kind, ts, prov) = (
        get(1)?,
        get(2)?,
        get(3)?,
        get(4)?,
        get(5)?,
        get(6)?,
        get(7)?,
    );

    let tenant_id = canonical_int(tenant)
        .filter(|n| *n <= i64::from(i32::MAX))
        .ok_or(Reject::BadTenantId)? as i32;
    let community_id = canonical_int(community)
        .filter(|n| (1..=i64::from(i32::MAX)).contains(n))
        .ok_or(Reject::BadCommunityId)? as i32;
    let user_uuid = canonical_uuid(user).ok_or(Reject::BadUserUuid)?;
    let platform_code = canonical_int(platform)
        .filter(|n| (1..=5).contains(n))
        .ok_or(Reject::BadPlatformCode)? as i16;
    let kind = canonical_int(kind)
        .and_then(Kind::from_code)
        .ok_or(Reject::BadKindCode)?;
    let ts_ms = canonical_int(ts).ok_or(Reject::BadTs)?;
    if ts_ms > now_ms + MAX_FUTURE_MS || ts_ms < now_ms - MAX_AGE_MS {
        return Err(Reject::TsOutOfRange);
    }
    let provenance_code = canonical_int(prov)
        .filter(|n| (1..=5).contains(n))
        .ok_or(Reject::BadProvenanceCode)? as i16;
    Ok(Event {
        tenant_id,
        community_id,
        user_uuid,
        platform_code,
        kind,
        ts_ms,
        provenance_code,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: i64 = 1_800_000_000_000;
    const U: &str = "0b9f2c1e-7d3a-4c58-9a1e-5f6d7e8a9b0c";

    fn base() -> Vec<(String, String)> {
        [
            ("v", "1"),
            ("tenant_id", "7"),
            ("community_id", "70"),
            ("user_uuid", U),
            ("platform_code", "2"),
            ("kind_code", "1"),
            ("ts_ms", "1800000000000"),
            ("provenance_code", "2"),
        ]
        .iter()
        .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
        .collect()
    }

    fn run(f: Vec<(String, String)>) -> Result<Event, Reject> {
        let raw: Vec<(String, Vec<u8>)> = f.into_iter().map(|(k, v)| (k, v.into_bytes())).collect();
        parse(&raw, NOW)
    }

    fn with(k: &str, v: &str) -> Vec<(String, String)> {
        let mut f = base();
        for e in &mut f {
            if e.0 == k {
                e.1 = v.to_string();
            }
        }
        f
    }

    #[test]
    fn valid_event_parses() {
        let e = run(base()).unwrap();
        assert_eq!((e.tenant_id, e.community_id, e.platform_code), (7, 70, 2));
        assert_eq!(e.kind, Kind::Message);
        assert_eq!(e.user_uuid.to_string(), U);
    }

    #[test]
    fn every_kind_code_roundtrips() {
        for c in 1..=6 {
            let e = run(with("kind_code", &c.to_string())).unwrap();
            assert_eq!(i64::from(e.kind.code()), c);
        }
    }

    #[test]
    fn rejection_table() {
        let nil = "00000000-0000-0000-0000-000000000000";
        let cases: Vec<(Vec<(String, String)>, Reject)> = vec![
            (with("v", "2"), Reject::UnknownVersion),
            (with("v", "01"), Reject::UnknownVersion),
            (with("v", ""), Reject::UnknownVersion),
            (with("tenant_id", "-1"), Reject::BadTenantId),
            (with("tenant_id", "2147483648"), Reject::BadTenantId),
            (with("tenant_id", "1x"), Reject::BadTenantId),
            (with("tenant_id", "07"), Reject::BadTenantId),
            (with("community_id", "0"), Reject::BadCommunityId),
            (with("community_id", "abc"), Reject::BadCommunityId),
            (with("community_id", " 5"), Reject::BadCommunityId),
            (with("user_uuid", "alice"), Reject::BadUserUuid),
            (with("user_uuid", nil), Reject::BadUserUuid),
            (
                with("user_uuid", "0b9f2c1e7d3a4c589a1e5f6d7e8a9b0c"),
                Reject::BadUserUuid,
            ),
            (
                with("user_uuid", "{0b9f2c1e-7d3a-4c58-9a1e-5f6d7e8a9b0c}"),
                Reject::BadUserUuid,
            ),
            (with("platform_code", "0"), Reject::BadPlatformCode),
            (with("platform_code", "6"), Reject::BadPlatformCode),
            (with("kind_code", "0"), Reject::BadKindCode),
            (with("kind_code", "7"), Reject::BadKindCode),
            (with("ts_ms", "now"), Reject::BadTs),
            (with("ts_ms", "1800000300001"), Reject::TsOutOfRange),
            (with("ts_ms", "1"), Reject::TsOutOfRange),
            (with("provenance_code", "0"), Reject::BadProvenanceCode),
            (with("provenance_code", "9"), Reject::BadProvenanceCode),
        ];
        for (fields, want) in cases {
            assert_eq!(run(fields.clone()).unwrap_err(), want, "{fields:?}");
        }
    }

    #[test]
    fn structural_rejections() {
        for name in FIELDS {
            let f: Vec<_> = base().into_iter().filter(|(k, _)| k != name).collect();
            assert_eq!(run(f).unwrap_err(), Reject::MissingField, "missing {name}");
        }
        let mut f = base();
        f.push(("text".into(), "hello".into()));
        assert_eq!(run(f).unwrap_err(), Reject::UnexpectedField);
        let mut f = base();
        f.push(("kind_code".into(), "2".into()));
        assert_eq!(run(f).unwrap_err(), Reject::DuplicateField);
        let raw = vec![("v".to_string(), vec![0xff, 0xfe])];
        assert_eq!(parse(&raw, NOW).unwrap_err(), Reject::NonUtf8);
        assert_eq!(parse(&[], NOW).unwrap_err(), Reject::MissingField);
    }

    #[test]
    fn reason_codes_are_unique_and_pii_free() {
        let all = [
            Reject::NonUtf8,
            Reject::UnknownVersion,
            Reject::MissingField,
            Reject::DuplicateField,
            Reject::UnexpectedField,
            Reject::BadTenantId,
            Reject::BadCommunityId,
            Reject::BadUserUuid,
            Reject::BadPlatformCode,
            Reject::BadKindCode,
            Reject::BadTs,
            Reject::TsOutOfRange,
            Reject::BadProvenanceCode,
        ];
        let mut codes: Vec<_> = all.iter().map(|r| r.code()).collect();
        codes.sort_unstable();
        codes.dedup();
        assert_eq!(codes.len(), all.len());
        assert!(codes.iter().all(|c| c
            .chars()
            .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '_')));
    }
}
