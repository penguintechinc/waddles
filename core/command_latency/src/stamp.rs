//! The non-PII latency stamps carried in the W3C `tracestate` `waddles` member.
//!
//! The spine envelope (`penguin_spine::StageEnvelope`, an external crate) cannot take a new field
//! without a library release, but it already propagates `trace.tracestate` verbatim through every
//! hop (ingest mints it, process copies it onto the action envelope, action forwards it into the
//! executor invoke scope). The stamps ride that string as one vendor member:
//!
//! ```text
//! tracestate: waddles=t0:1731575000123;d:g;ps:1731575000140;pc:1731575000152;pd:1731575000190;ac:...;iv:...;c:ping
//! ```
//!
//! | key | meaning | written by |
//! |---|---|---|
//! | `t0` | first-door epoch ms | svc-ingest |
//! | `d`  | door kind: `g` gateway frame, `s` connector sink (lower bound), `h` http request-start header, `p` publish time (no door info) | svc-ingest |
//! | `ps` | ingest-stream XADD time (Valkey entry-id ms) | svc-process |
//! | `pc` | process consumed | svc-process |
//! | `pd` | process handed to the action stream | svc-process |
//! | `ac` | action consumed | svc-action |
//! | `iv` | executor invoke sent | svc-action |
//! | `c`  | registered command name (never user text) | svc-process |
//!
//! Security: the member is metadata only and is never used for authorization. The envelope MAC covers
//! the trace id, not the tracestate, so a producer that can already write the stream could forge it;
//! the only producers are the trusted stage services (a bundle never writes a stream or the trace of
//! an envelope), and a consumer additionally refuses implausible values ([`elapsed_ms`]) so a forged
//! stamp can at worst skip or skew its own latency sample, never reach a decision.

/// The tracestate member key owned by this crate.
pub const MEMBER_KEY: &str = "waddles";

/// W3C limit on the whole tracestate value is 512 chars for practical propagation; keep the member
/// well under the per-value limit (256).
const MAX_VALUE_LEN: usize = 200;
const MAX_TRACESTATE_LEN: usize = 512;
const MAX_MEMBERS: usize = 32;

/// How far in the future a stamp may be relative to the reader's clock before it is refused (NTP
/// drift across pods; anything beyond this is a broken or forged stamp).
pub const MAX_CLOCK_SKEW_MS: u64 = 5_000;
/// Oldest stamp still counted: a command older than this has been redelivered or abandoned and
/// would poison the histogram's upper tail.
pub const MAX_AGE_MS: u64 = 600_000;

/// Where the first-door timestamp was taken. A closed set (it becomes a metric label).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Door {
    /// The Discord gateway websocket frame was received by svc-ingest (the true first door).
    Gateway,
    /// The connector sink received the event from the connector guest: a LOWER BOUND on the first
    /// door (the frame read and the guest's `on-frame` time before it are not included).
    Sink,
    /// A trusted proxy's request-start header on an HTTP interaction/webhook (the gateway leg).
    Http,
    /// Stamped at publish time because the receiver supplied no earlier instant: a LOWER BOUND.
    Publish,
}

impl Door {
    fn code(self) -> char {
        match self {
            Door::Gateway => 'g',
            Door::Sink => 's',
            Door::Http => 'h',
            Door::Publish => 'p',
        }
    }

    fn from_code(c: &str) -> Option<Self> {
        match c {
            "g" => Some(Door::Gateway),
            "s" => Some(Door::Sink),
            "h" => Some(Door::Http),
            "p" => Some(Door::Publish),
            _ => None,
        }
    }

    /// The bounded metric label value.
    pub fn label(self) -> &'static str {
        match self {
            Door::Gateway => "gateway",
            Door::Sink => "sink",
            Door::Http => "http",
            Door::Publish => "publish",
        }
    }
}

/// Every stamp the `waddles` member can carry. All optional: a missing stamp only means that hop is
/// not measured, never an error.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Stamps {
    /// First-door epoch ms.
    pub t0_ms: Option<u64>,
    /// Door kind of `t0`.
    pub door: Option<Door>,
    /// Ingest-stream XADD (publish) epoch ms.
    pub publish_ms: Option<u64>,
    /// Process-stage consume epoch ms.
    pub consume_ms: Option<u64>,
    /// Process-stage hand-off (action XADD) epoch ms.
    pub handoff_ms: Option<u64>,
    /// Action-stage consume epoch ms.
    pub action_consume_ms: Option<u64>,
    /// Executor invoke-sent epoch ms.
    pub invoke_ms: Option<u64>,
    /// Registered command name (see [`is_valid_command`]); never user text.
    pub command: Option<String>,
}

/// `true` for a syntactically safe command label: 1..=32 of `[a-z0-9_-]`. Registry membership is a
/// separate check ([`crate::registry`]); this only keeps a hostile value out of the tracestate.
pub fn is_valid_command(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 32
        && name
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-')
}

impl Stamps {
    /// Renders the member VALUE (`t0:..;d:g;...`), skipping absent fields.
    pub fn render_value(&self) -> String {
        let mut parts: Vec<String> = Vec::with_capacity(8);
        if let Some(v) = self.t0_ms {
            parts.push(format!("t0:{v}"));
        }
        if let Some(d) = self.door {
            parts.push(format!("d:{}", d.code()));
        }
        for (key, v) in [
            ("ps", self.publish_ms),
            ("pc", self.consume_ms),
            ("pd", self.handoff_ms),
            ("ac", self.action_consume_ms),
            ("iv", self.invoke_ms),
        ] {
            if let Some(v) = v {
                parts.push(format!("{key}:{v}"));
            }
        }
        if let Some(c) = self.command.as_deref().filter(|c| is_valid_command(c)) {
            parts.push(format!("c:{c}"));
        }
        parts.join(";")
    }

    /// Parses a member value tolerantly: an unknown key, a malformed number or an oversized value is
    /// ignored field by field, never an error.
    pub fn parse_value(value: &str) -> Stamps {
        let mut s = Stamps::default();
        if value.len() > MAX_VALUE_LEN {
            return s;
        }
        for part in value.split(';') {
            let Some((k, v)) = part.split_once(':') else {
                continue;
            };
            let num = || v.parse::<u64>().ok();
            match k {
                "t0" => s.t0_ms = num(),
                "d" => s.door = Door::from_code(v),
                "ps" => s.publish_ms = num(),
                "pc" => s.consume_ms = num(),
                "pd" => s.handoff_ms = num(),
                "ac" => s.action_consume_ms = num(),
                "iv" => s.invoke_ms = num(),
                "c" if is_valid_command(v) => s.command = Some(v.to_string()),
                _ => {}
            }
        }
        s
    }

    /// Extracts the stamps from a whole `tracestate` string (empty when the member is absent).
    pub fn from_tracestate(tracestate: Option<&str>) -> Stamps {
        let Some(ts) = tracestate else {
            return Stamps::default();
        };
        for member in ts.split(',') {
            let member = member.trim();
            if let Some(value) = member
                .strip_prefix(MEMBER_KEY)
                .and_then(|r| r.strip_prefix('='))
            {
                return Stamps::parse_value(value);
            }
        }
        Stamps::default()
    }

    /// Returns `existing` with the `waddles` member replaced by these stamps, every other vendor's
    /// member preserved after it. Past the W3C member/length caps the foreign members are dropped
    /// (ours is the one this crate's SLO depends on).
    pub fn merge_into_tracestate(&self, existing: Option<&str>) -> String {
        let ours = format!("{MEMBER_KEY}={}", self.render_value());
        let mut members: Vec<&str> = vec![];
        if let Some(ts) = existing {
            for m in ts.split(',') {
                let m = m.trim();
                if m.is_empty() || m.starts_with(&format!("{MEMBER_KEY}=")) {
                    continue;
                }
                members.push(m);
            }
        }
        let mut out = ours;
        for m in members.into_iter().take(MAX_MEMBERS - 1) {
            if out.len() + 1 + m.len() > MAX_TRACESTATE_LEN {
                break;
            }
            out.push(',');
            out.push_str(m);
        }
        out
    }
}

/// Why a stamp was refused as an elapsed-time source.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Implausible {
    /// Further in the future than [`MAX_CLOCK_SKEW_MS`].
    Skewed,
    /// Older than [`MAX_AGE_MS`].
    Stale,
}

impl Implausible {
    /// The bounded metric label value.
    pub fn label(self) -> &'static str {
        match self {
            Implausible::Skewed => "skewed",
            Implausible::Stale => "stale",
        }
    }
}

/// `now - stamp` in ms, refusing implausible stamps. A stamp slightly ahead of `now` (within
/// [`MAX_CLOCK_SKEW_MS`], cross-pod clock drift) reads as `0` rather than negative.
pub fn elapsed_ms(stamp_ms: u64, now_ms: u64) -> Result<u64, Implausible> {
    if stamp_ms > now_ms {
        return if stamp_ms - now_ms > MAX_CLOCK_SKEW_MS {
            Err(Implausible::Skewed)
        } else {
            Ok(0)
        };
    }
    let age = now_ms - stamp_ms;
    if age > MAX_AGE_MS {
        Err(Implausible::Stale)
    } else {
        Ok(age)
    }
}

/// Wall-clock now, epoch milliseconds (`0` before the epoch, which every consumer then refuses as
/// stale rather than recording a bogus sample).
pub fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
}

/// The epoch-ms prefix of a Valkey stream entry id (`<ms>-<seq>`): the Valkey server's own XADD time.
pub fn entry_id_ms(entry_id: &str) -> Option<u64> {
    entry_id.split('-').next()?.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn full() -> Stamps {
        Stamps {
            t0_ms: Some(1_731_575_000_123),
            door: Some(Door::Gateway),
            publish_ms: Some(1_731_575_000_140),
            consume_ms: Some(1_731_575_000_152),
            handoff_ms: Some(1_731_575_000_190),
            action_consume_ms: Some(1_731_575_000_200),
            invoke_ms: Some(1_731_575_000_210),
            command: Some("ping".into()),
        }
    }

    #[test]
    fn render_parse_round_trips_every_field() {
        let s = full();
        assert_eq!(Stamps::parse_value(&s.render_value()), s);
        assert!(s.render_value().len() < MAX_VALUE_LEN);
    }

    #[test]
    fn tracestate_merge_preserves_foreign_members_and_replaces_ours() {
        let merged = full().merge_into_tracestate(Some("vendor=abc,waddles=t0:1,other=zz"));
        assert!(merged.starts_with("waddles=t0:1731575000123;"));
        assert!(merged.ends_with(",vendor=abc,other=zz"));
        assert_eq!(merged.matches("waddles=").count(), 1);
        assert_eq!(Stamps::from_tracestate(Some(&merged)), full());
    }

    #[test]
    fn missing_or_garbled_member_parses_to_empty_never_errors() {
        assert_eq!(Stamps::from_tracestate(None), Stamps::default());
        assert_eq!(Stamps::from_tracestate(Some("vendor=1")), Stamps::default());
        let s = Stamps::parse_value("t0:notanumber;d:zz;zz:5;c:BAD NAME;ps:7");
        assert_eq!(s.t0_ms, None);
        assert_eq!(s.door, None);
        assert_eq!(s.command, None);
        assert_eq!(s.publish_ms, Some(7));
        assert_eq!(
            Stamps::parse_value(&"t0:1;".repeat(100)),
            Stamps::default(),
            "an oversized value is ignored wholesale"
        );
    }

    #[test]
    fn hostile_command_never_reaches_the_rendered_member() {
        let s = Stamps {
            command: Some("a;b=c,d".into()),
            ..Stamps::default()
        };
        assert_eq!(s.render_value(), "");
        for bad in [
            "",
            "UPPER",
            "has space",
            "semi;colon",
            "comma,x",
            &"a".repeat(33),
        ] {
            assert!(!is_valid_command(bad), "{bad}");
        }
        assert!(is_valid_command("ping"));
        assert!(is_valid_command("a-b_9"));
    }

    #[test]
    fn merge_drops_foreign_members_past_the_length_cap() {
        let huge = format!("a={}", "x".repeat(600));
        let merged = full().merge_into_tracestate(Some(&huge));
        assert!(merged.len() <= MAX_TRACESTATE_LEN);
        assert!(merged.starts_with("waddles="));
        assert!(!merged.contains("a=x"));
    }

    #[test]
    fn elapsed_refuses_forged_future_and_ancient_stamps() {
        let now = 1_000_000_000_000;
        assert_eq!(elapsed_ms(now - 250, now), Ok(250));
        assert_eq!(elapsed_ms(now + 100, now), Ok(0), "small drift reads 0");
        assert_eq!(
            elapsed_ms(now + MAX_CLOCK_SKEW_MS + 1, now),
            Err(Implausible::Skewed)
        );
        assert_eq!(
            elapsed_ms(now - MAX_AGE_MS - 1, now),
            Err(Implausible::Stale)
        );
        assert_eq!(elapsed_ms(0, now), Err(Implausible::Stale));
    }

    #[test]
    fn entry_id_prefix_is_the_publish_time() {
        assert_eq!(entry_id_ms("1731575000140-3"), Some(1_731_575_000_140));
        assert_eq!(entry_id_ms("garbage"), None);
        assert_eq!(entry_id_ms(""), None);
    }
}
