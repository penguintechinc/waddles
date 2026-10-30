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

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::time::Duration;

/// DNS lookup ceiling.
const RESOLVE_TIMEOUT: Duration = Duration::from_secs(5);

/// Unwraps IPv4 embedded in v6 forms (mapped, compatible, NAT64, 6to4) so a
/// v6 spelling cannot smuggle a forbidden v4 target.
fn canonical(ip: IpAddr) -> IpAddr {
    let IpAddr::V6(v6) = ip else { return ip };
    let s = v6.segments();
    if let Some(v4) = v6.to_ipv4_mapped() {
        return IpAddr::V4(v4);
    }
    let v4 = |hi: u16, lo: u16| {
        let [a, b] = hi.to_be_bytes();
        let [c, d] = lo.to_be_bytes();
        IpAddr::V4(Ipv4Addr::new(a, b, c, d))
    };
    if s[..6] == [0, 0, 0, 0, 0, 0] && !v6.is_unspecified() && !v6.is_loopback() {
        return v4(s[6], s[7]);
    }
    if s[..6] == [0x64, 0xff9b, 0, 0, 0, 0] {
        return v4(s[6], s[7]);
    }
    if s[0] == 0x2002 {
        return v4(s[1], s[2]);
    }
    ip
}

/// Why `ip` may not be dialed, or `None` when it is a public address.
#[must_use]
pub(crate) fn forbidden_reason(ip: IpAddr) -> Option<&'static str> {
    match canonical(ip) {
        IpAddr::V4(v) => {
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
                || (o[0] == 198 && (o[1] & 0xFE) == 18)
                || v.is_documentation()
            {
                Some("special-use")
            } else {
                None
            }
        }
        IpAddr::V6(v) => {
            let s = v.segments();
            if v.is_loopback() {
                Some("loopback")
            } else if v.is_unspecified() {
                Some("unspecified")
            } else if v.is_multicast() {
                Some("multicast")
            } else if (s[0] & 0xFFC0) == 0xFE80 {
                Some("link-local")
            } else if (s[0] & 0xFE00) == 0xFC00 {
                Some("unique-local")
            } else if s[0] == 0x2001 && (s[1] == 0 || s[1] == 0xDB8) {
                Some("special-use")
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
