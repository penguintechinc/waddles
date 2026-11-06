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
        let parts = split_url(raw).ok_or_else(|| reject("unparseable relay url".into()))?;
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
        let port_part = parts.port.map(|p| format!(":{p}")).unwrap_or_default();
        Ok(format!(
            "{}://{}{}{}",
            parts.scheme, host, port_part, parts.rest
        ))
    }
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
        IpAddr::V6(v6) => {
            if let Some(mapped) = v6.to_ipv4_mapped() {
                return blocked_v4(mapped);
            }
            let seg = v6.segments();
            if seg[0] == 0x0064 && seg[1] == 0xff9b && seg[2..6].iter().all(|s| *s == 0) {
                let o = v6.octets();
                return blocked_v4(Ipv4Addr::new(o[12], o[13], o[14], o[15]));
            }
            blocked_v6(v6)
        }
    }
}

fn blocked_v4(ip: Ipv4Addr) -> bool {
    let o = ip.octets();
    ip.is_loopback()
        || ip.is_private()
        || ip.is_link_local()
        || ip.is_unspecified()
        || ip.is_broadcast()
        || ip.is_multicast()
        || ip.is_documentation()
        || (o[0] == 100 && (64..=127).contains(&o[1])) // CGNAT 100.64/10
        || (o[0] == 198 && (18..=19).contains(&o[1])) // benchmarking 198.18/15
        || (o[0] == 192 && o[1] == 0 && o[2] == 0) // IETF protocol assignments
        || o[0] >= 240 // reserved
}

fn blocked_v6(ip: Ipv6Addr) -> bool {
    let seg = ip.segments();
    ip.is_loopback()
        || ip.is_unspecified()
        || ip.is_multicast()
        || (seg[0] & 0xfe00) == 0xfc00 // ULA fc00::/7
        || (seg[0] & 0xffc0) == 0xfe80 // link-local fe80::/10
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
