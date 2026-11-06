//! Egress SSRF guard for relay (RTMP/SRT push) destinations, applied at dial time.
//!
//! A destination is validated once when an admin adds it, but DNS can change afterwards
//! (rebinding / TOCTOU). [`EgressGuard::pin`] therefore re-resolves the host immediately before
//! the target is handed to ffmpeg, refuses it if ANY resolved address is non-public, and --
//! for `rtmp://` and `srt://`, where no TLS server name is involved -- rewrites the URL to the
//! validated IP so ffmpeg cannot be steered by a second, different lookup. `rtmps://` keeps its
//! hostname (the certificate must still verify against it); it is validated at dial time but
//! cannot be pinned without breaking TLS -- a documented residual.

use std::future::Future;
use std::io;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use std::pin::Pin;
use std::sync::Arc;

use super::error::RelayError;
use super::target::RelayTargetKind;
use crate::redact::redact_url;

type LookupFuture = Pin<Box<dyn Future<Output = io::Result<Vec<IpAddr>>> + Send>>;
type LookupFn = Arc<dyn Fn(String, u16) -> LookupFuture + Send + Sync>;

/// Resolves and validates relay destinations just before dial; see the module docs.
#[derive(Clone)]
pub struct EgressGuard {
    enabled: bool,
    lookup: LookupFn,
}

impl EgressGuard {
    /// Production guard: system DNS, enforcing.
    pub fn enforcing() -> Self {
        Self {
            enabled: true,
            lookup: Arc::new(|host, port| {
                Box::pin(async move {
                    let addrs = tokio::net::lookup_host((host.as_str(), port)).await?;
                    Ok(addrs.map(|a| a.ip()).collect())
                })
            }),
        }
    }

    /// Enforcing guard with an injected resolver (tests: simulate DNS rebinding).
    pub fn enforcing_with_lookup<F>(lookup: F) -> Self
    where
        F: Fn(String, u16) -> LookupFuture + Send + Sync + 'static,
    {
        Self {
            enabled: true,
            lookup: Arc::new(lookup),
        }
    }

    /// No-op guard -- tests with fake hostnames only; never constructed by production wiring.
    pub fn disabled() -> Self {
        let mut guard = Self::enforcing();
        guard.enabled = false;
        guard
    }

    /// Validates `raw` and returns the URL ffmpeg must dial (IP-pinned for rtmp/srt).
    pub async fn pin(&self, kind: RelayTargetKind, raw: &str) -> Result<String, RelayError> {
        if !self.enabled {
            return Ok(raw.to_string());
        }
        let reject = |reason: String| RelayError::InvalidUrl {
            redacted: redact_url(raw),
            reason,
        };
        if let Some(why) = forbidden_chars(raw) {
            return Err(reject(format!("relay url rejected: {why}")));
        }
        let parts = split_url(raw).ok_or_else(|| reject("unparseable relay url".into()))?;
        // Parser-differential defense: a second, independent parser must agree on host + port.
        let strict = url::Url::parse(raw).map_err(|_| reject("unparseable relay url".into()))?;
        if !parser_agrees(&strict, &parts) {
            return Err(reject("relay url is parsed ambiguously".into()));
        }
        let port = match (parts.port, parts.scheme.as_str()) {
            (Some(p), _) => p,
            (None, "rtmps") => 443,
            (None, "rtmp") => 1935,
            _ => return Err(reject("relay url needs an explicit port".into())),
        };
        let ip = if let Ok(literal) = parts.host.parse::<IpAddr>() {
            if is_blocked_ip(literal) {
                return Err(reject("destination is a non-public address".into()));
            }
            literal
        } else {
            let addrs = (self.lookup)(parts.host.clone(), port)
                .await
                .map_err(|_| reject("destination host did not resolve".into()))?;
            if addrs.is_empty() {
                return Err(reject("destination host did not resolve".into()));
            }
            if addrs.iter().any(|ip| is_blocked_ip(*ip)) {
                return Err(reject(
                    "destination resolves to a non-public address".into(),
                ));
            }
            addrs[0]
        };
        if parts.scheme == "rtmps" || matches!(kind, RelayTargetKind::Srt) && parts.scheme != "srt"
        {
            return Ok(raw.to_string());
        }
        let host = match ip {
            IpAddr::V4(v4) => v4.to_string(),
            IpAddr::V6(v6) => format!("[{v6}]"),
        };
        let port_part = parts.port.map_or_else(String::new, |p| format!(":{p}"));
        let pinned = format!("{}://{}{}{}", parts.scheme, host, port_part, parts.rest);
        // Re-parse what ffmpeg will actually receive: it must name exactly the validated IP.
        let reparsed = url::Url::parse(&pinned).map_err(|_| reject("pinned url invalid".into()))?;
        let again = split_url(&pinned).ok_or_else(|| reject("pinned url invalid".into()))?;
        let want = ip.to_string();
        let host_ok = |h: Option<url::Host<&str>>| match h {
            // Non-special schemes (rtmp/srt) parse an IPv4 literal as an opaque domain.
            Some(url::Host::Domain(d)) => d == want,
            Some(url::Host::Ipv4(v)) => v.to_string() == want,
            Some(url::Host::Ipv6(v)) => v.to_string() == want,
            None => false,
        };
        if !host_ok(reparsed.host()) || again.host != want || reparsed.port() != parts.port {
            return Err(reject(
                "pinned url does not name the validated address".into(),
            ));
        }
        Ok(pinned)
    }
}

/// Characters that make parsers (this one, `url`, ffmpeg's `av_url_split`) disagree or that
/// terminate/extend an ffmpeg `-f tee` slave (`|` separates slaves, `[`/`]` open option blocks,
/// `;`/`\\` are option/escape syntax, `#` starts a fragment ffmpeg strips, `@` userinfo).
/// An IPv6 literal's own brackets are allowed only as the very first authority character.
fn forbidden_chars(raw: &str) -> Option<&'static str> {
    if raw.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Some("whitespace or control character");
    }
    if raw.contains(['#', '\\', '|', ';', '"', '\'', '@']) {
        return Some("reserved character (# \\ | ; quote @)");
    }
    let after = raw.split_once("://").map_or("", |(_, a)| a);
    let end = after.find(['/', '?']).unwrap_or(after.len());
    let (authority, rest) = after.split_at(end);
    let v6 = authority.starts_with('[');
    let bracket_in_authority = authority.char_indices().any(|(i, c)| {
        (c == '[' && i != 0) || (c == ']' && !(v6 && authority[i..].starts_with(']')))
    });
    if bracket_in_authority || rest.contains(['[', ']']) {
        return Some("square bracket outside an IPv6 literal");
    }
    None
}

/// `true` when the `url` crate and the hand-rolled splitter name the same host and port.
fn parser_agrees(strict: &url::Url, parts: &UrlParts) -> bool {
    let host_matches = match strict.host() {
        Some(url::Host::Domain(d)) => d.eq_ignore_ascii_case(&parts.host),
        Some(url::Host::Ipv4(v)) => v.to_string() == parts.host,
        Some(url::Host::Ipv6(v)) => v.to_string() == parts.host.to_ascii_lowercase(),
        None => false,
    };
    host_matches && strict.port() == parts.port && strict.scheme() == parts.scheme
}

struct UrlParts {
    scheme: String,
    host: String,
    port: Option<u16>,
    /// Everything after the authority (path/query), starting at `/` or `?` -- may carry the key.
    rest: String,
}

fn split_url(raw: &str) -> Option<UrlParts> {
    let (scheme, after) = raw.split_once("://")?;
    let end = after.find(['/', '?']).unwrap_or(after.len());
    let (authority, rest) = after.split_at(end);
    // Userinfo is dropped: ffmpeg gets host:port only, and `a@b` tricks cannot smuggle a host.
    let hostport = authority.rsplit_once('@').map_or(authority, |(_, h)| h);
    let (host, port) = if let Some(stripped) = hostport.strip_prefix('[') {
        let (h, tail) = stripped.split_once(']')?;
        (h.to_string(), tail.strip_prefix(':'))
    } else {
        match hostport.rsplit_once(':') {
            Some((h, p)) => (h.to_string(), Some(p)),
            None => (hostport.to_string(), None),
        }
    };
    if host.is_empty() {
        return None;
    }
    let port = match port {
        Some(p) => Some(p.parse::<u16>().ok()?),
        None => None,
    };
    Some(UrlParts {
        scheme: scheme.to_ascii_lowercase(),
        host,
        port,
        rest: rest.to_string(),
    })
}

/// `true` for any address a relay must never dial: loopback, private, link-local (incl. cloud
/// metadata), CGNAT, unspecified, multicast, broadcast, reserved/documentation, IPv6 ULA, and
/// IPv4-mapped / NAT64-embedded forms of the above.
pub fn is_blocked_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => blocked_v4(v4),
        // Default-deny: embedded-v4 forms (mapped, NAT64, IPv4-compatible) all lie outside
        // 2000::/3 and are refused without extracting the v4.
        IpAddr::V6(v6) => blocked_v6(v6),
    }
}

fn blocked_v4(ip: Ipv4Addr) -> bool {
    let o = ip.octets();
    ip.is_loopback()
        || ip.is_private()
        || ip.is_link_local()
        || ip.is_broadcast()
        || ip.is_multicast()
        || ip.is_documentation()
        || o[0] == 0 // 0.0.0.0/8 "this network"
        || (o[0] == 100 && (64..=127).contains(&o[1])) // CGNAT 100.64/10
        || (o[0] == 198 && (18..=19).contains(&o[1])) // benchmarking 198.18/15
        || (o[0] == 192 && o[1] == 0 && o[2] == 0) // IETF protocol assignments
        || o[0] >= 240 // reserved
}

/// Default-deny: only global unicast 2000::/3 is dialable, minus the special-purpose carve-outs
/// inside it (6to4, Teredo/IETF 2001::/23, documentation, ORCHID/benchmarking-adjacent ranges).
fn blocked_v6(ip: Ipv6Addr) -> bool {
    let seg = ip.segments();
    if (seg[0] & 0xe000) != 0x2000 {
        return true; // loopback, unspecified, multicast, ULA, link-local, site-local fec0::/10,
                     // 64:ff9b:1::/48 local NAT64, mapped, ... everything outside 2000::/3
    }
    seg[0] == 0x2002 // 6to4 2002::/16 (embeds an arbitrary v4)
        || (seg[0] == 0x2001 && seg[1] < 0x0200) // 2001::/23 incl. Teredo 2001::/32
        || (seg[0] == 0x2001 && seg[1] == 0x0db8) // documentation 2001:db8::/32
        || (seg[0] == 0x3fff && seg[1] < 0x1000) // documentation 3fff::/20
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lookup_to(ip: &'static str) -> EgressGuard {
        EgressGuard::enforcing_with_lookup(move |_, _| {
            Box::pin(async move { Ok(vec![ip.parse().unwrap()]) })
        })
    }

    #[tokio::test]
    async fn rtmp_host_is_pinned_to_the_validated_ip_keeping_path_and_key() {
        let url = lookup_to("8.8.8.8")
            .pin(
                RelayTargetKind::Rtmp,
                "rtmp://live.example:1935/app/KEY?x=1",
            )
            .await
            .unwrap();
        assert_eq!(url, "rtmp://8.8.8.8:1935/app/KEY?x=1");
    }

    #[tokio::test]
    async fn dns_rebinding_to_private_or_metadata_is_refused_at_dial_time() {
        for bad in [
            "127.0.0.1",
            "10.0.0.5",
            "169.254.169.254",
            "::1",
            "fd00::1",
            "::ffff:10.0.0.1",
        ] {
            let err = lookup_to(bad)
                .pin(RelayTargetKind::Rtmp, "rtmp://live.example/app/KEY")
                .await
                .unwrap_err();
            assert!(err.to_string().contains("non-public"), "{bad}: {err}");
            assert!(!err.to_string().contains("KEY"), "error leaked the key");
        }
    }

    #[tokio::test]
    async fn any_private_answer_among_several_refuses_the_target() {
        let guard = EgressGuard::enforcing_with_lookup(|_, _| {
            Box::pin(async {
                Ok(vec![
                    "8.8.8.8".parse().unwrap(),
                    "10.1.1.1".parse().unwrap(),
                ])
            })
        });
        assert!(guard
            .pin(RelayTargetKind::Rtmp, "rtmp://live.example/app/KEY")
            .await
            .is_err());
    }

    #[tokio::test]
    async fn literal_private_ip_and_userinfo_tricks_are_refused() {
        let guard = lookup_to("8.8.8.8");
        for url in [
            "rtmp://127.0.0.1/app/KEY",
            "rtmp://8.8.8.8@127.0.0.1/app/KEY",
            "rtmp://[::1]:1935/app/KEY",
            "srt://192.168.1.10:9000?streamid=KEY",
        ] {
            let kind = if url.starts_with("srt") {
                RelayTargetKind::Srt
            } else {
                RelayTargetKind::Rtmp
            };
            assert!(guard.pin(kind, url).await.is_err(), "{url}");
        }
    }

    #[tokio::test]
    async fn rtmps_is_validated_but_keeps_its_hostname_for_tls() {
        let url = lookup_to("8.8.8.8")
            .pin(RelayTargetKind::Rtmp, "rtmps://live.example/app/KEY")
            .await
            .unwrap();
        assert_eq!(url, "rtmps://live.example/app/KEY");
        assert!(lookup_to("10.0.0.1")
            .pin(RelayTargetKind::Rtmp, "rtmps://live.example/app/KEY")
            .await
            .is_err());
    }

    #[tokio::test]
    async fn srt_requires_a_port_and_is_pinned() {
        let g = lookup_to("1.1.1.1");
        assert!(g
            .pin(RelayTargetKind::Srt, "srt://ingest.example?streamid=K")
            .await
            .is_err());
        assert_eq!(
            g.pin(RelayTargetKind::Srt, "srt://ingest.example:9000?streamid=K")
                .await
                .unwrap(),
            "srt://1.1.1.1:9000?streamid=K"
        );
    }

    #[tokio::test]
    async fn unresolvable_host_fails_closed_and_disabled_guard_passes_through() {
        let fail = EgressGuard::enforcing_with_lookup(|_, _| {
            Box::pin(async { Err(io::Error::other("nxdomain")) })
        });
        assert!(fail
            .pin(RelayTargetKind::Rtmp, "rtmp://nx.example/a/K")
            .await
            .is_err());
        assert_eq!(
            EgressGuard::disabled()
                .pin(RelayTargetKind::Rtmp, "rtmp://127.0.0.1/a/K")
                .await
                .unwrap(),
            "rtmp://127.0.0.1/a/K"
        );
    }

    #[tokio::test]
    async fn parser_differential_and_tee_injection_urls_are_refused() {
        let g = lookup_to("8.8.8.8");
        for url in [
            "rtmp://8.8.8.8#@127.0.0.1/app/KEY",
            "rtmp://8.8.8.8/app/KEY#frag",
            "rtmp://127.0.0.1\\@8.8.8.8/app/KEY",
            "rtmp://8.8.8.8@127.0.0.1/app/KEY",
            "rtmp://live.example/app/KEY|[f=flv]rtmp://127.0.0.1/x",
            "rtmp://live.example/app/KEY|rtmp://127.0.0.1/x",
            "rtmp://live.example/app/KEY;onfail=abort",
            "rtmp://live.example/app/KEY[f=flv]",
            "rtmp://live.example/app/KEY]",
            "rtmp://live.example/app/KE Y",
            "rtmp://live.example/app/KEY\n|x",
            "rtmp://live.example/app/KEY\t",
            "rtmp://[8.8.8.8/app/KEY",
            "rtmp://li[ve.example/app/KEY",
        ] {
            assert!(g.pin(RelayTargetKind::Rtmp, url).await.is_err(), "{url:?}");
        }
        // a legitimate IPv6 literal authority still works
        let ok = lookup_to("2606:4700:4700::1111")
            .pin(
                RelayTargetKind::Rtmp,
                "rtmp://[2606:4700:4700::1111]:1935/app/KEY",
            )
            .await
            .unwrap();
        assert_eq!(ok, "rtmp://[2606:4700:4700::1111]:1935/app/KEY");
    }

    #[test]
    fn ipv6_default_deny_table() {
        let blocked = [
            "::",
            "::1",
            "::127.0.0.1",
            "::10.0.0.1",
            "::ffff:10.0.0.1",
            "::ffff:8.8.8.8",
            "64:ff9b::7f00:1",
            "64:ff9b::808:808",
            "64:ff9b:1::1",
            "fc00::1",
            "fd00::1",
            "fe80::1",
            "fec0::1",
            "ff02::1",
            "2002::1",
            "2002:0a00:0001::1",
            "2001::1",
            "2001:0:4136:e378:8000:63bf:3fff:fdd2",
            "2001:1::1",
            "2001:db8::1",
            "3fff::1",
            "4000::1",
            "8000::1",
            "100::1",
        ];
        for ip in blocked {
            assert!(is_blocked_ip(ip.parse().unwrap()), "{ip} must be blocked");
        }
        for ip in [
            "2606:4700:4700::1111",
            "2001:4860:4860::8888",
            "2a00:1450:4001::1",
            "2001:200::1",
        ] {
            assert!(!is_blocked_ip(ip.parse().unwrap()), "{ip} must be allowed");
        }
    }

    #[test]
    fn ipv4_zero_network_and_special_ranges_blocked() {
        for ip in [
            "0.0.0.0",
            "0.1.2.3",
            "0.255.255.255",
            "192.0.2.1",
            "240.0.0.1",
        ] {
            assert!(is_blocked_ip(ip.parse().unwrap()), "{ip}");
        }
        assert!(!is_blocked_ip("1.0.0.1".parse().unwrap()));
    }

    #[test]
    fn blocked_ranges() {
        for ip in [
            "100.64.0.1",
            "198.18.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "255.255.255.255",
            "fe80::1",
            "64:ff9b::7f00:1",
        ] {
            assert!(is_blocked_ip(ip.parse().unwrap()), "{ip}");
        }
        for ip in ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"] {
            assert!(!is_blocked_ip(ip.parse().unwrap()), "{ip}");
        }
    }
}
