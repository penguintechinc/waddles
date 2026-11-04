//! SSRF egress address policy for the generic WebSocket / poll transports.
//!
//! The endpoint host comes from overlay config, so it is untrusted input:
//! it is resolved HERE, every resolved address is checked, and the connection
//! is made to the PINNED address (never re-resolved by the client), closing
//! DNS-rebind. Mirrors the ranges of `core/bundle_host_http`'s
//! `is_forbidden_address` (same denylist, replicated rather than depended on
//! so svc_ingest's build graph / Dockerfile COPY set stay unchanged).
//! Loopback, link-local, metadata, CGNAT, private, multicast, reserved and
//! documentation ranges are refused unless the EXACT ip is in the explicit
//! allowlist the host config grants.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::time::Duration;

/// DNS lookup ceiling.
const RESOLVE_TIMEOUT: Duration = Duration::from_secs(5);

/// IPv4 embedded in a v6 spelling that must be judged by the v4 deny set:
/// mapped `::ffff:0:0/96`, compatible `::/96`, NAT64 `64:ff9b::/96`. (6to4
/// `2002::/16` is NOT unwrapped: it is denied outright, as in the source.)
fn embedded_v4(v6: Ipv6Addr) -> Option<Ipv4Addr> {
    if let Some(v4) = v6.to_ipv4_mapped() {
        return Some(v4);
    }
    let s = v6.segments();
    let tail = || {
        let [a, b] = s[6].to_be_bytes();
        let [c, d] = s[7].to_be_bytes();
        Ipv4Addr::new(a, b, c, d)
    };
    if s[..6] == [0, 0, 0, 0, 0, 0] && !v6.is_unspecified() && !v6.is_loopback() {
        return Some(tail());
    }
    if s[..6] == [0x64, 0xff9b, 0, 0, 0, 0] {
        return Some(tail());
    }
    None
}

fn forbidden_v4(v: Ipv4Addr) -> Option<&'static str> {
    let o = v.octets();
    if v.is_loopback() {
        Some("loopback")
    } else if o[0] == 0 {
        Some("unspecified")
    } else if v.is_link_local() {
        Some("link-local/metadata")
    } else if v.is_private() {
        Some("private")
    } else if o[0] == 100 && (o[1] & 0xC0) == 64 {
        Some("cgnat")
    } else if v.is_multicast() || v == Ipv4Addr::BROADCAST || o[0] >= 240 {
        Some("multicast/reserved")
    } else if (o[0] == 192 && o[1] == 0 && o[2] == 0)
        || (o[0] == 192 && o[1] == 88 && o[2] == 99)
        || (o[0] == 198 && (o[1] & 0xFE) == 18)
        || v.is_documentation()
    {
        Some("special-use")
    } else {
        None
    }
}

/// Why `ip` may not be dialed, or `None` for a genuine global-unicast
/// address. MUST stay in sync with
/// `core/bundle_host_http::egress::is_forbidden_address` (same denylist;
/// replicated, not depended on, to keep svc_ingest's build graph unchanged --
/// factoring into one shared crate is a tracked follow-up). Where the two
/// differ this one is stricter, never looser.
#[must_use]
pub(crate) fn forbidden_reason(ip: IpAddr) -> Option<&'static str> {
    match ip {
        IpAddr::V4(v) => forbidden_v4(v),
        IpAddr::V6(v) => {
            let s = v.segments();
            let s0 = s[0];
            if v.is_unspecified() {
                Some("unspecified")
            } else if v.is_loopback() {
                Some("loopback")
            } else if (s0 & 0xFF00) == 0xFF00 {
                Some("multicast")
            } else if (s0 & 0xFFC0) == 0xFE80 {
                Some("link-local")
            } else if (s0 & 0xFFC0) == 0xFEC0 {
                Some("site-local")
            } else if (s0 & 0xFE00) == 0xFC00 {
                Some("unique-local")
            } else if s[..4] == [0x100, 0, 0, 0] {
                Some("discard")
            } else if s[..3] == [0x64, 0xff9b, 1] {
                Some("local-nat64")
            } else if s0 == 0x2001 && (s[1] < 0x200 || s[1] == 0x0db8) {
                Some("special-use") // 2001::/23 + 2001:db8::/32
            } else if s0 == 0x2002 {
                Some("6to4")
            } else if s0 == 0x3fff && s[1] < 0x1000 {
                Some("documentation")
            } else if s0 == 0x5f00 {
                Some("special-use")
            } else if let Some(v4) = embedded_v4(v) {
                forbidden_v4(v4)
            } else {
                None
            }
        }
    }
}

/// Resolves the URL's host, requires EVERY address to be permitted (or
/// exactly allowlisted), and returns the address to pin the connection to.
pub(crate) async fn resolve_pinned(
    url: &reqwest::Url,
    allow: &[IpAddr],
) -> Result<SocketAddr, String> {
    let port = url.port_or_known_default().ok_or("endpoint has no port")?;
    let host = url.host_str().ok_or("endpoint has no host")?;
    let literal = host.trim_start_matches('[').trim_end_matches(']');
    let addrs: Vec<IpAddr> = if let Ok(ip) = literal.parse::<IpAddr>() {
        vec![ip]
    } else {
        tokio::time::timeout(RESOLVE_TIMEOUT, tokio::net::lookup_host((host, port)))
            .await
            .map_err(|_| "dns lookup timed out".to_string())?
            .map_err(|_| "dns lookup failed".to_string())?
            .map(|sa| sa.ip())
            .collect()
    };
    let first = *addrs.first().ok_or("host resolved to no address")?;
    for ip in &addrs {
        if forbidden_reason(*ip).is_some() && !allow.contains(ip) {
            // Class only -- never the address or host.
            return Err("endpoint resolves to a forbidden address range".into());
        }
    }
    Ok(SocketAddr::new(first, port))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn forbidden_ranges() {
        for ip in [
            "127.0.0.1",
            "0.0.0.0",
            "169.254.169.254",
            "169.254.1.1",
            "10.1.2.3",
            "172.16.0.1",
            "192.168.1.1",
            "100.64.0.1",
            "100.127.255.255",
            "224.0.0.1",
            "255.255.255.255",
            "240.0.0.1",
            "198.18.0.1",
            "192.0.2.1",
            "::1",
            "::",
            "fe80::1",
            "fc00::1",
            "ff02::1",
            "::ffff:169.254.169.254",
            "::ffff:10.0.0.1",
            "64:ff9b::a00:1",
            "2002:a9fe:a9fe::1",
            "2001:db8::1",
            "::7f00:1",
        ] {
            assert!(forbidden_reason(ip.parse().unwrap()).is_some(), "{ip}");
        }
        for ip in [
            "8.8.8.8",
            "1.1.1.1",
            "100.128.0.1",
            "172.32.0.1",
            "2606:4700::1111",
        ] {
            assert!(forbidden_reason(ip.parse().unwrap()).is_none(), "{ip}");
        }
    }

    #[tokio::test]
    async fn pinned_resolution_refuses_and_allowlists() {
        let u = |s: &str| reqwest::Url::parse(s).unwrap();
        assert!(resolve_pinned(&u("https://169.254.169.254/x"), &[])
            .await
            .is_err());
        assert!(resolve_pinned(&u("https://10.0.0.5/x"), &[]).await.is_err());
        assert!(resolve_pinned(&u("https://[::ffff:10.0.0.5]/x"), &[])
            .await
            .is_err());
        assert!(resolve_pinned(&u("https://localhost/x"), &[])
            .await
            .is_err());
        let lo: IpAddr = "127.0.0.1".parse().unwrap();
        assert_eq!(
            resolve_pinned(&u("http://127.0.0.1:9/x"), &[lo])
                .await
                .unwrap()
                .port(),
            9
        );
        assert!(resolve_pinned(&u("https://8.8.8.8/x"), &[]).await.is_ok());
    }
}
