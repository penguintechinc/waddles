//! Host-owned gateway-session rules layered on [`super::lifecycle`]: a
//! validated reconnect-URL capture (Discord `resume_gateway_url`), a
//! heartbeat-ACK timeout (the zombie-connection rule) and the handshake
//! ordering option (`handshake_after: first_frame`). Same stance as the rest
//! of the lifecycle engine: the rules are HOST CONFIG, never supplied by a
//! guest, no platform is named here, and every misconfiguration is refused
//! at construction.
//!
//! # Reconnect URL
//!
//! A frame-supplied URL is attacker-influenced data (a compromised or
//! spoofed upstream, a hostile guest echoing text). It is therefore NEVER
//! used as-is: [`validate_reconnect_url`] accepts only `wss://` (plain `ws://`
//! solely to loopback in test harnesses), no userinfo, no explicit port, no
//! path/query/fragment, printable ASCII only (CR/LF/space refused), and a host
//! that matches the host-pinned allow-list ([`ReconnectUrlCapture::allowed_hosts`]).
//! The host-configured query string is appended afterwards. The SSRF guard
//! (`egress_guard`, the shared `bundle_host_http::guarded` resolver) then runs
//! on the connect itself, on the pinned address, so a DNS-rebinding or
//! private-address host is refused there as well.

use std::time::Duration;

use super::lifecycle::{match_ok, pointer_ok, FrameMatch, LifecycleError};
use super::transport_common::validate_endpoint;

/// Longest accepted captured reconnect URL.
pub const MAX_RECONNECT_URL_LEN: usize = 256;
/// Most allow-listed host patterns.
pub const MAX_ALLOWED_HOSTS: usize = 8;
/// Longest accepted host pattern / configured query string.
pub const MAX_PATTERN_LEN: usize = 128;
/// Most consecutive unacknowledged heartbeats a rule may tolerate.
pub const MAX_UNACKED: u8 = 3;
/// Shortest accepted first-frame wait.
pub const MIN_FIRST_FRAME_WAIT: Duration = Duration::from_millis(100);
/// Longest accepted first-frame wait.
pub const MAX_FIRST_FRAME_WAIT: Duration = Duration::from_secs(60);

/// Captures the URL the next reconnect must dial (Discord READY
/// `/d/resume_gateway_url`) from a matched frame, under strict validation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReconnectUrlCapture {
    /// Which frames the rule reads.
    pub when: FrameMatch,
    /// JSON pointer to the URL string; absent or `null` is skipped.
    pub pointer: String,
    /// Host-pinned hosts the URL may point at: exact (`gateway.example`) or
    /// `*.suffix` (one or more labels under a suffix of at least two labels
    /// whose last label is not numeric). 1..=[`MAX_ALLOWED_HOSTS`] entries.
    pub allowed_hosts: Vec<String>,
    /// Query string (without `?`) appended to the validated URL, e.g.
    /// `v=10&encoding=json`; charset `[A-Za-z0-9=&._-]`, may be empty.
    pub query: String,
}

/// Heartbeat-ACK timeout (the zombie-connection rule): when a heartbeat is
/// due while `max_unacked` earlier heartbeats are still unacknowledged, the
/// connection is dead and the transport ends it so the driver reconnects
/// (RESUME state is kept).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HeartbeatAck {
    /// Which frames are the acknowledgement (e.g. `op == 11`).
    pub when: FrameMatch,
    /// Unacknowledged heartbeats tolerated before the next one declares the
    /// connection dead; `1..=`[`MAX_UNACKED`]. `1` = Discord's rule.
    pub max_unacked: u8,
}

/// When the connector's on-connect handshake is sent.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum HandshakeAfter {
    /// Right after the socket opens (the historical behaviour, e.g. IRC).
    #[default]
    Connect,
    /// After the first frame is read and its captures (heartbeat interval,
    /// session state) are applied -- the gateway HELLO -> IDENTIFY/RESUME
    /// order. No first frame within `timeout` fails the connection loudly.
    FirstFrame {
        /// Bounded wait for the first frame.
        timeout: Duration,
    },
}

impl HandshakeAfter {
    /// `Some(wait)` when the handshake must wait for the first frame.
    #[must_use]
    pub fn first_frame_wait(self) -> Option<Duration> {
        match self {
            Self::Connect => None,
            Self::FirstFrame { timeout } => Some(timeout),
        }
    }

    /// Refuses an out-of-range wait.
    pub(super) fn validate(self) -> Result<(), LifecycleError> {
        match self {
            Self::FirstFrame { timeout }
                if !(MIN_FIRST_FRAME_WAIT..=MAX_FIRST_FRAME_WAIT).contains(&timeout) =>
            {
                Err(LifecycleError::Config("handshake_after wait out of range"))
            }
            _ => Ok(()),
        }
    }
}

impl HeartbeatAck {
    /// Refuses a malformed matcher or an unbounded tolerance.
    pub(super) fn validate(&self) -> Result<(), LifecycleError> {
        if !match_ok(&self.when) || !(1..=MAX_UNACKED).contains(&self.max_unacked) {
            return Err(LifecycleError::Config("invalid heartbeat_ack rule"));
        }
        Ok(())
    }
}

fn label_charset_ok(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= MAX_PATTERN_LEN
        && !s.starts_with(['.', '-'])
        && !s.ends_with(['.', '-'])
        && !s.contains("..")
        && s.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'.')
}

fn is_numeric_label(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

/// A valid allow-list entry: exact host, or `*.suffix` with a >= 2-label
/// suffix whose last label is non-numeric (so `*.0.1` can never match an IP).
fn pattern_ok(p: &str) -> bool {
    match p.strip_prefix("*.") {
        Some(suffix) => {
            label_charset_ok(suffix)
                && suffix.contains('.')
                && suffix
                    .rsplit('.')
                    .next()
                    .is_some_and(|l| !is_numeric_label(l))
        }
        None => label_charset_ok(p),
    }
}

impl ReconnectUrlCapture {
    /// Refuses a malformed rule (fail loud at construction).
    pub(super) fn validate(&self) -> Result<(), LifecycleError> {
        if !match_ok(&self.when) || !pointer_ok(&self.pointer) {
            return Err(LifecycleError::Config("invalid reconnect_url capture"));
        }
        if self.allowed_hosts.is_empty()
            || self.allowed_hosts.len() > MAX_ALLOWED_HOSTS
            || !self.allowed_hosts.iter().all(|p| pattern_ok(p))
        {
            return Err(LifecycleError::Config(
                "reconnect_url allowed_hosts empty, too many or malformed",
            ));
        }
        if self.query.len() > MAX_PATTERN_LEN
            || !self
                .query
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"=&._-".contains(&b))
        {
            return Err(LifecycleError::Config("reconnect_url query charset"));
        }
        Ok(())
    }

    fn host_allowed(&self, host: &str, is_ip: bool) -> bool {
        if host.is_empty() || host.ends_with('.') {
            return false;
        }
        self.allowed_hosts
            .iter()
            .any(|p| match p.strip_prefix("*.") {
                // An IP literal only ever matches an exact entry.
                Some(suffix) => {
                    !is_ip
                        && host
                            .strip_suffix(suffix)
                            .is_some_and(|rest| rest.len() > 1 && rest.ends_with('.'))
                }
                None => host == p,
            })
    }
}

/// Validates a frame-supplied reconnect URL against `rule` and returns the
/// normalised URL to dial (`scheme://host[:port]/` plus the configured
/// query). Every refusal is the same class-only error: no URL text leaves.
pub(super) fn validate_reconnect_url(
    raw: &str,
    rule: &ReconnectUrlCapture,
    allow_loopback_insecure: bool,
) -> Result<String, LifecycleError> {
    let bad = LifecycleError::BadReconnectUrl;
    if raw.is_empty()
        || raw.len() > MAX_RECONNECT_URL_LEN
        || !raw.bytes().all(|b| (0x21..=0x7e).contains(&b))
    {
        return Err(bad);
    }
    let url =
        validate_endpoint(raw, &["wss"], "ws", allow_loopback_insecure).map_err(|_| bad.clone())?;
    // `wss` on a non-default port, any path, query or fragment is refused:
    // the only legitimate shape is a bare gateway host.
    let plain_path = url.path() == "/";
    let no_extras = url.query().is_none() && url.fragment().is_none();
    let port_ok = url.scheme() != "wss" || url.port().is_none();
    if !(plain_path && no_extras && port_ok) {
        return Err(bad);
    }
    let host = url.host_str().ok_or_else(|| bad.clone())?;
    let is_ip = host.starts_with('[') || host.parse::<std::net::IpAddr>().is_ok();
    if !rule.host_allowed(host, is_ip) {
        return Err(bad);
    }
    let hostport = match url.port() {
        Some(p) => format!("{host}:{p}"),
        None => host.to_string(),
    };
    let query = if rule.query.is_empty() {
        String::new()
    } else {
        format!("?{}", rule.query)
    };
    Ok(format!("{}://{hostport}/{query}", url.scheme()))
}

#[cfg(test)]
mod tests {
    use super::super::lifecycle::Scalar;
    use super::*;

    fn rule(hosts: &[&str], query: &str) -> ReconnectUrlCapture {
        ReconnectUrlCapture {
            when: FrameMatch::Json(vec![("/op".into(), Scalar::Int(0))]),
            pointer: "/d/resume_gateway_url".into(),
            allowed_hosts: hosts.iter().map(|s| (*s).to_string()).collect(),
            query: query.into(),
        }
    }

    #[test]
    fn accepts_allowlisted_wss_host_and_appends_the_host_query() {
        let r = rule(&["*.discord.gg"], "v=10&encoding=json");
        assert_eq!(
            validate_reconnect_url("wss://gateway-us-east1-b.discord.gg", &r, false).unwrap(),
            "wss://gateway-us-east1-b.discord.gg/?v=10&encoding=json"
        );
        assert_eq!(
            validate_reconnect_url("wss://a.b.discord.gg/", &r, false).unwrap(),
            "wss://a.b.discord.gg/?v=10&encoding=json"
        );
        // Uppercase host is normalised by the URL parser before matching.
        assert!(validate_reconnect_url("wss://GATEWAY.Discord.GG", &r, false).is_ok());
        assert_eq!(
            validate_reconnect_url("wss://gw.discord.gg", &rule(&["*.discord.gg"], ""), false)
                .unwrap(),
            "wss://gw.discord.gg/"
        );
    }

    #[test]
    fn hostile_urls_are_refused() {
        let r = rule(&["*.discord.gg", "exact.example"], "");
        let bad = [
            "ws://gateway.discord.gg",                // plain ws
            "http://gateway.discord.gg",              // wrong scheme
            "wss://10.0.0.5",                         // private IP
            "wss://127.0.0.1",                        // loopback
            "wss://[::1]",                            // v6 loopback
            "wss://169.254.169.254",                  // metadata
            "wss://evil.example",                     // not allow-listed
            "wss://discord.gg",                       // the bare suffix
            "wss://evildiscord.gg",                   // suffix without a dot boundary
            "wss://gateway.discord.gg.evil.example",  // suffix as a prefix
            "wss://user@gateway.discord.gg",          // userinfo
            "wss://user:pw@gateway.discord.gg",       // userinfo
            "wss://gateway.discord.gg@evil.example",  // userinfo trick
            "wss://evil.example#.discord.gg",         // fragment trick
            "wss://gateway.discord.gg:8443",          // explicit port
            "wss://gateway.discord.gg/path",          // path
            "wss://gateway.discord.gg/?x=1",          // query
            "wss://gateway.discord.gg/#f",            // fragment
            "wss://gateway.discord.gg\r\nHost: evil", // CRLF
            "wss://gateway.discord.gg\n",             // LF
            "wss://gateway.discord.gg ",              // space
            "wss://gateway.discord.gg\u{0}",          // NUL
            "wss://gât.discord.gg",                   // non-ASCII
            "wss://gateway.discord.gg.",              // trailing dot
            "",
            "nonsense",
        ];
        for raw in bad {
            assert_eq!(
                validate_reconnect_url(raw, &r, false),
                Err(LifecycleError::BadReconnectUrl),
                "{raw:?} must be refused"
            );
        }
        assert!(validate_reconnect_url("wss://exact.example", &r, false).is_ok());
        let long = format!("wss://{}.discord.gg", "a".repeat(300));
        assert!(validate_reconnect_url(&long, &r, false).is_err());
    }

    #[test]
    fn plain_ws_is_loopback_only_even_with_the_test_switch() {
        let r = rule(&["127.0.0.1", "*.discord.gg"], "");
        assert!(validate_reconnect_url("ws://127.0.0.1:9", &r, false).is_err());
        assert_eq!(
            validate_reconnect_url("ws://127.0.0.1:9", &r, true).unwrap(),
            "ws://127.0.0.1:9/"
        );
        // Non-loopback ws:// is refused regardless of the switch.
        assert!(validate_reconnect_url("ws://gateway.discord.gg", &r, true).is_err());
    }

    #[test]
    fn ip_literals_only_match_exact_entries_never_wildcards() {
        // `*.0.1` is refused as a pattern, and a v4 literal can never satisfy
        // a wildcard even if a numeric-looking suffix slipped through.
        assert!(!pattern_ok("*.0.1"));
        assert!(!pattern_ok("*.gg"));
        assert!(!pattern_ok("*"));
        assert!(!pattern_ok("*.*.gg"));
        assert!(!pattern_ok("Discord.GG"));
        assert!(!pattern_ok(".discord.gg"));
        assert!(!pattern_ok("a..b"));
        let r = rule(&["*.discord.gg"], "");
        assert!(!r.host_allowed("10.0.0.5", true));
        assert!(!r.host_allowed("x.discord.gg", true));
        assert!(r.host_allowed("x.discord.gg", false));
    }

    #[test]
    fn config_validation_is_loud() {
        assert!(rule(&["*.discord.gg"], "v=10&encoding=json")
            .validate()
            .is_ok());
        assert!(rule(&[], "").validate().is_err(), "empty allow-list");
        assert!(rule(&["*.gg"], "").validate().is_err());
        assert!(rule(&["a.example"], "x y").validate().is_err());
        assert!(rule(&["a.example"], "a=1\r\nb").validate().is_err());
        let many: Vec<&str> = vec!["a.example"; MAX_ALLOWED_HOSTS + 1];
        assert!(rule(&many, "").validate().is_err());
        let mut r = rule(&["a.example"], "");
        r.pointer = "no-slash".into();
        assert!(r.validate().is_err());

        let ack = |n| HeartbeatAck {
            when: FrameMatch::Json(vec![("/op".into(), Scalar::Int(11))]),
            max_unacked: n,
        };
        assert!(ack(1).validate().is_ok() && ack(MAX_UNACKED).validate().is_ok());
        assert!(ack(0).validate().is_err() && ack(MAX_UNACKED + 1).validate().is_err());

        let first = |ms| HandshakeAfter::FirstFrame {
            timeout: Duration::from_millis(ms),
        };
        assert!(first(100).validate().is_ok() && first(60_000).validate().is_ok());
        assert!(first(99).validate().is_err() && first(60_001).validate().is_err());
        assert!(HandshakeAfter::Connect.validate().is_ok());
        assert_eq!(HandshakeAfter::Connect.first_frame_wait(), None);
        assert_eq!(
            first(500).first_frame_wait(),
            Some(Duration::from_millis(500))
        );
    }
}
