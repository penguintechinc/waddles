//! Pure aggregation: turns accepted events into per-key deltas for layer A. No IO.
//! Tier gating happens here: Free communities produce basic counts only
//! (`community_hourly` messages/commands/joins/leaves, `member_daily` msgs/cmds and
//! first/last activity); Premium additionally yields session starts, presence
//! sessionization input and the hourly `sessions_started` metric.

use std::collections::BTreeMap;

use uuid::Uuid;

use crate::config::{FREE_RETENTION_DAYS, PREMIUM_RETENTION_DAYS};
use crate::contract::{Event, Kind};

/// `community_hourly.metric_code` values.
pub mod metric {
    /// Chat messages (Free).
    pub const MESSAGES: i16 = 1;
    /// Command invocations (Free).
    pub const COMMANDS: i16 = 2;
    /// Member joins (Free).
    pub const JOINS: i16 = 3;
    /// Member leaves (Free).
    pub const LEAVES: i16 = 4;
    /// Presence sessions started (Premium).
    pub const SESSIONS_STARTED: i16 = 5;
}

/// A stream entry id (`<ms>-<seq>`), the idempotency key of one event.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct EntryId {
    /// Millisecond part.
    pub ms: i64,
    /// Sequence part.
    pub seq: i64,
}

impl EntryId {
    /// Parses `<ms>-<seq>`.
    pub fn parse(s: &str) -> Option<Self> {
        let (a, b) = s.split_once('-')?;
        Some(Self {
            ms: a.parse().ok().filter(|n: &i64| *n >= 0)?,
            seq: b.parse().ok().filter(|n: &i64| *n >= 0)?,
        })
    }
}

/// A validated event plus the tier decision made for it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Accepted {
    /// Idempotency key.
    pub entry: EntryId,
    /// The event.
    pub event: Event,
    /// Whether the community is Premium (in-depth tables allowed).
    pub premium: bool,
}

impl Accepted {
    /// Retention (days) to stamp on rows this event touches.
    pub fn retention_days(&self) -> i32 {
        if self.premium {
            PREMIUM_RETENTION_DAYS
        } else {
            FREE_RETENTION_DAYS
        }
    }
}

/// Key of one hourly counter row.
pub type HourlyKey = (i32, i32, i64, i16, i16);
/// Key of one daily member row: community, member, days since epoch.
pub type DailyKey = (i32, Uuid, i32);

/// Hourly counter delta.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HourlyDelta {
    /// Count to add.
    pub value: i64,
    /// Retention days to stamp.
    pub retention: i32,
    /// Provenance code.
    pub provenance: i16,
}

/// Daily member delta.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DailyDelta {
    /// Messages to add.
    pub msgs: i32,
    /// Commands to add.
    pub cmds: i32,
    /// Earliest event, epoch ms.
    pub first_ms: i64,
    /// Latest event, epoch ms.
    pub last_ms: i64,
    /// Retention days to stamp.
    pub retention: i32,
    /// Provenance code.
    pub provenance: i16,
}

/// One presence transition (Premium only).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Presence {
    /// Community.
    pub community_id: i32,
    /// Member.
    pub user_uuid: Uuid,
    /// Platform.
    pub platform_code: i16,
    /// Epoch seconds.
    pub ts_s: i64,
    /// `true` start, `false` end.
    pub start: bool,
    /// Retention days to stamp.
    pub retention: i32,
    /// Provenance code.
    pub provenance: i16,
}

/// All deltas for one batch.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct WriteBatch {
    /// Hourly counters.
    pub hourly: BTreeMap<HourlyKey, HourlyDelta>,
    /// Daily member rows.
    pub daily: BTreeMap<DailyKey, DailyDelta>,
    /// Presence transitions sorted by time (start before end on ties).
    pub presence: Vec<Presence>,
}

/// Epoch ms -> epoch seconds (floor).
pub fn secs(ms: i64) -> i64 {
    ms.div_euclid(1000)
}

/// Start of the UTC hour containing `ts_s`.
pub fn hour_start(ts_s: i64) -> i64 {
    ts_s - ts_s.rem_euclid(3600)
}

/// Days since the epoch (UTC) containing `ts_s`.
pub fn day_index(ts_s: i64) -> i32 {
    (ts_s.div_euclid(86_400)) as i32
}

/// Builds the deltas for `events`. Deterministic; order of `events` does not matter
/// except that presence transitions are sorted by time.
pub fn build_batch(events: &[&Accepted]) -> WriteBatch {
    let mut b = WriteBatch::default();
    for a in events {
        let e = &a.event;
        let ts_s = secs(e.ts_ms);
        let ret = a.retention_days();
        let metric = match e.kind {
            Kind::Message => Some(metric::MESSAGES),
            Kind::Command => Some(metric::COMMANDS),
            Kind::Join => Some(metric::JOINS),
            Kind::Leave => Some(metric::LEAVES),
            Kind::PresenceStart if a.premium => Some(metric::SESSIONS_STARTED),
            Kind::PresenceStart | Kind::PresenceEnd => None,
        };
        if let Some(m) = metric {
            let key = (
                e.tenant_id,
                e.community_id,
                hour_start(ts_s),
                m,
                e.platform_code,
            );
            let d = b.hourly.entry(key).or_insert(HourlyDelta {
                value: 0,
                retention: ret,
                provenance: e.provenance_code,
            });
            d.value += 1;
        }
        let activity = matches!(e.kind, Kind::Message | Kind::Command | Kind::PresenceStart);
        if activity {
            let key = (e.community_id, e.user_uuid, day_index(ts_s));
            let d = b.daily.entry(key).or_insert(DailyDelta {
                msgs: 0,
                cmds: 0,
                first_ms: e.ts_ms,
                last_ms: e.ts_ms,
                retention: ret,
                provenance: e.provenance_code,
            });
            match e.kind {
                Kind::Message => d.msgs += 1,
                Kind::Command => d.cmds += 1,
                _ => {}
            }
            d.first_ms = d.first_ms.min(e.ts_ms);
            d.last_ms = d.last_ms.max(e.ts_ms);
        }
        if a.premium && matches!(e.kind, Kind::PresenceStart | Kind::PresenceEnd) {
            b.presence.push(Presence {
                community_id: e.community_id,
                user_uuid: e.user_uuid,
                platform_code: e.platform_code,
                ts_s,
                start: e.kind == Kind::PresenceStart,
                retention: ret,
                provenance: e.provenance_code,
            });
        }
    }
    b.presence.sort_by_key(|p| (p.ts_s, !p.start));
    b
}

/// Splits the span `[start_s, end_s]` at UTC midnights, returning
/// `(day_index, seconds)` pieces. An empty/negative span yields nothing.
pub fn split_by_day(start_s: i64, end_s: i64) -> Vec<(i32, i32)> {
    let mut out = Vec::new();
    let mut cur = start_s;
    while cur < end_s {
        let next_midnight = (cur.div_euclid(86_400) + 1) * 86_400;
        let stop = next_midnight.min(end_s);
        out.push((day_index(cur), (stop - cur) as i32));
        cur = stop;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const U1: u128 = 0x0b9f2c1e7d3a4c589a1e5f6d7e8a9b0c;
    const U2: u128 = 0x1c8e3d2f6e4b4d698b2f6a7e8f9a0b1d;

    fn acc(kind: Kind, user: u128, ts_ms: i64, premium: bool, seq: i64) -> Accepted {
        Accepted {
            entry: EntryId { ms: ts_ms, seq },
            event: Event {
                tenant_id: 1,
                community_id: 10,
                user_uuid: Uuid::from_u128(user),
                platform_code: 2,
                kind,
                ts_ms,
                provenance_code: 2,
            },
            premium,
        }
    }

    #[test]
    fn entry_id_parse() {
        assert_eq!(EntryId::parse("12-3"), Some(EntryId { ms: 12, seq: 3 }));
        for bad in ["", "12", "a-1", "1-b", "-1-2", "1-2-3x", "1--2"] {
            assert_eq!(EntryId::parse(bad), None, "{bad}");
        }
    }

    #[test]
    fn free_gets_basic_counts_only() {
        let evs = [
            acc(Kind::Message, U1, 3_600_000, false, 0),
            acc(Kind::Message, U1, 3_700_000, false, 1),
            acc(Kind::Command, U2, 3_800_000, false, 2),
            acc(Kind::Join, U2, 3_900_000, false, 3),
            acc(Kind::Leave, U2, 4_000_000, false, 4),
            acc(Kind::PresenceStart, U1, 4_100_000, false, 5),
            acc(Kind::PresenceEnd, U1, 4_200_000, false, 6),
        ];
        let refs: Vec<&Accepted> = evs.iter().collect();
        let b = build_batch(&refs);
        let by = |m| {
            b.hourly
                .iter()
                .find(|(k, _)| k.3 == m)
                .map(|(_, v)| v.value)
        };
        assert_eq!(by(metric::MESSAGES), Some(2));
        assert_eq!(by(metric::COMMANDS), Some(1));
        assert_eq!(by(metric::JOINS), Some(1));
        assert_eq!(by(metric::LEAVES), Some(1));
        assert_eq!(by(metric::SESSIONS_STARTED), None, "premium-only metric");
        assert!(b.presence.is_empty(), "no sessionization for Free");
        assert!(b.hourly.values().all(|d| d.retention == 90));
        assert!(b.daily.values().all(|d| d.retention == 90));
        let d1 = b.daily.get(&(10, Uuid::from_u128(U1), 0)).unwrap();
        assert_eq!(
            (d1.msgs, d1.cmds, d1.first_ms, d1.last_ms),
            (2, 0, 3_600_000, 4_100_000)
        );
        assert!(!b.daily.contains_key(&(10, Uuid::from_u128(U2), 5)));
    }

    #[test]
    fn premium_adds_sessions_and_400_day_retention_sorted() {
        let evs = [
            acc(Kind::PresenceEnd, U1, 9_000_000, true, 0),
            acc(Kind::PresenceStart, U1, 5_000_000, true, 1),
            acc(Kind::PresenceEnd, U1, 5_000_000, true, 2),
            acc(Kind::PresenceStart, U1, 5_000_000, true, 3),
        ];
        let refs: Vec<&Accepted> = evs.iter().collect();
        let b = build_batch(&refs);
        assert_eq!(b.presence.len(), 4);
        let order: Vec<(i64, bool)> = b.presence.iter().map(|p| (p.ts_s, p.start)).collect();
        assert_eq!(
            order,
            vec![(5000, true), (5000, true), (5000, false), (9000, false)]
        );
        let ss = b
            .hourly
            .iter()
            .find(|(k, _)| k.3 == metric::SESSIONS_STARTED)
            .unwrap()
            .1;
        assert_eq!((ss.value, ss.retention), (2, 400));
    }

    #[test]
    fn split_by_day_midnight_and_empty() {
        assert_eq!(split_by_day(100, 100), vec![]);
        assert_eq!(split_by_day(200, 100), vec![]);
        assert_eq!(
            split_by_day(86_400 - 60, 86_400 + 120),
            vec![(0, 60), (1, 120)]
        );
        assert_eq!(
            split_by_day(0, 86_400 * 2 + 5),
            vec![(0, 86_400), (1, 86_400), (2, 5)]
        );
        assert_eq!(split_by_day(10, 20), vec![(0, 10)]);
    }

    #[test]
    fn time_helpers() {
        assert_eq!(secs(1999), 1);
        assert_eq!(hour_start(7199), 3600);
        assert_eq!(day_index(86_399), 0);
        assert_eq!(day_index(86_400), 1);
    }
}
