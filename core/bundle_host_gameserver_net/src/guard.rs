//! SSRF guard on the RESOLVED address. DNS is resolved exactly once; every answer must be
//! publicly routable; the caller connects to the returned [`SocketAddr`] and never re-resolves
//! (DNS rebinding defense). The classifier is `bundle_host_http`'s -- one denylist, never a copy --
//! plus the reserved ranges that classifier does not cover.

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::time::Duration;

use bundle_host_http::egress::{canonicalize_ip, is_forbidden_address};

use crate::error::NetError;

/// Reserved IPv4 ranges `is_forbidden_address` does not classify (this-network, IETF protocol
/// assignments, benchmarking, reserved/future-use).
fn extra_reserved_v4(ip: Ipv4Addr) -> Option<&'static str> {
    let o = ip.octets();
    if o[0] == 0 {
        return Some("this_network");
    }
    if o[0] == 192 && o[1] == 0 && o[2] == 0 {
        return Some("ietf_protocol");
    }
    if o[0] == 198 && (o[1] == 18 || o[1] == 19) {
        return Some("benchmarking");
    }
    if o[0] >= 240 {
        return Some("reserved");
    }
    None
}

/// Returns the refusal class for `ip`, or `None` when it may be dialed.
/// `allow_loopback` is the test-only escape hatch (loopback only; nothing else is lifted).
pub fn classify(ip: IpAddr, allow_loopback: bool) -> Option<&'static str> {
    let canonical = canonicalize_ip(ip);
    if allow_loopback && canonical.is_loopback() {
        return None;
    }
    if let Some(class) = is_forbidden_address(canonical, false) {
        return Some(class);
    }
    if let IpAddr::V4(v4) = canonical {
        return extra_reserved_v4(v4);
    }
    None
}

/// Validates a registered host string before any lookup (no scheme, userinfo, path, spaces).
fn host_is_well_formed(host: &str) -> bool {
    !host.is_empty()
        && host.len() <= 253
        && host
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | ':' | '_' | '[' | ']'))
}

/// Resolves `host:port` once and returns the single address to connect to. EVERY resolved
/// address must pass [`classify`]: a name with one public and one private answer is refused
/// outright rather than trusting the connect order. Errors carry no host/IP.
pub async fn resolve_pinned(
    host: &str,
    port: u16,
    allow_loopback: bool,
    deadline: Duration,
) -> Result<SocketAddr, NetError> {
    if !host_is_well_formed(host) || port == 0 {
        return Err(NetError::Denied("invalid_target"));
    }
    let bare = host.trim_start_matches('[').trim_end_matches(']');
    let addrs: Vec<SocketAddr> = if let Ok(ip) = bare.parse::<IpAddr>() {
        vec![SocketAddr::new(ip, port)]
    } else {
        match tokio::time::timeout(deadline, tokio::net::lookup_host((bare, port))).await {
            Err(_) => return Err(NetError::Timeout),
            Ok(Err(_)) => return Err(NetError::Unreachable),
            Ok(Ok(it)) => it.collect(),
        }
    };
    if addrs.is_empty() {
        return Err(NetError::Unreachable);
    }
    for addr in &addrs {
        if let Some(class) = classify(addr.ip(), allow_loopback) {
            tracing::warn!(
                class,
                "gameserver-net: resolved address refused by egress guard"
            );
            return Err(NetError::Denied("forbidden_address"));
        }
    }
    Ok(addrs[0])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blocks_every_non_public_class() {
        for ip in [
            "10.0.0.5",
            "172.16.4.4",
            "192.168.1.1",
            "127.0.0.1",
            "169.254.169.254",
            "169.254.1.1",
            "100.64.0.1",
            "100.100.100.200",
            "224.0.0.1",
            "0.0.0.0",
            "0.1.2.3",
            "240.0.0.1",
            "255.255.255.255",
            "198.18.0.1",
            "192.0.0.8",
            "::1",
            "::",
            "fc00::1",
            "fd12::1",
            "fe80::1",
            "ff02::1",
            "::ffff:10.0.0.1",
            "::ffff:127.0.0.1",
            "::ffff:169.254.169.254",
            "64:ff9b::a00:1",
            "2002::1",
            "2001::1",
        ] {
            let ip: IpAddr = ip.parse().unwrap();
            assert!(classify(ip, false).is_some(), "{ip} must be refused");
        }
    }

    #[test]
    fn allows_public_addresses() {
        for ip in ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"] {
            assert!(classify(ip.parse().unwrap(), false).is_none(), "{ip}");
        }
    }

    #[test]
    fn loopback_lift_is_loopback_only() {
        assert!(classify("127.0.0.1".parse().unwrap(), true).is_none());
        assert!(classify("::1".parse().unwrap(), true).is_none());
        assert!(classify("10.0.0.1".parse().unwrap(), true).is_some());
        assert!(classify("169.254.169.254".parse().unwrap(), true).is_some());
    }

    #[tokio::test]
    async fn literal_private_target_is_denied_without_leaking_it() {
        let err = resolve_pinned("10.1.2.3", 27015, false, Duration::from_secs(1))
            .await
            .unwrap_err();
        assert_eq!(err, NetError::Denied("forbidden_address"));
        assert!(!format!("{err:?}{err}").contains("10.1.2.3"));
    }

    #[tokio::test]
    async fn malformed_hosts_and_port_zero_are_invalid() {
        for host in ["", "a b", "user@host", "http://x", "x/y", "host\n"] {
            assert_eq!(
                resolve_pinned(host, 1, false, Duration::from_secs(1))
                    .await
                    .unwrap_err(),
                NetError::Denied("invalid_target"),
                "{host:?}"
            );
        }
        assert_eq!(
            resolve_pinned("8.8.8.8", 0, false, Duration::from_secs(1))
                .await
                .unwrap_err(),
            NetError::Denied("invalid_target")
        );
    }

    #[tokio::test]
    async fn hostname_resolving_to_loopback_is_denied() {
        // `localhost` resolves to loopback via the system resolver: the RESOLVED address is judged.
        let r = resolve_pinned("localhost", 27015, false, Duration::from_secs(2)).await;
        assert!(matches!(
            r,
            Err(NetError::Denied("forbidden_address")) | Err(NetError::Unreachable)
        ));
    }

    #[tokio::test]
    async fn public_literal_is_pinned_as_is() {
        let a = resolve_pinned("8.8.8.8", 27015, false, Duration::from_secs(1))
            .await
            .unwrap();
        assert_eq!(a, "8.8.8.8:27015".parse().unwrap());
    }
}
