//! Address-category policy: layers this proxy's two extra checks --
//! deployment-configured cluster CIDRs and the three-permission-family
//! category-crossing rule -- on top of `bundle_host_http::egress::
//! is_forbidden_address`, the shared SSRF-hardened check (loopback,
//! link-local, unspecified, multicast, broadcast, cloud metadata,
//! RFC1918/ULA private, CGNAT) already exercised by the in-process guard.
//! See that crate's `lib.rs` module doc, "Explicitly deferred" paragraph --
//! this module is exactly the deferred follow-up it names.

use std::net::IpAddr;

use bundle_host_http::egress::is_forbidden_address;
use ipnet::IpNet;

/// The three `net.http.*` permission families (connector spec) -- the
/// canonical definition now lives in `egress_assertion` (shared with
/// `core/bundle_host_http`'s signer); re-exported here so existing
/// `crate::ip_policy::DestinationCategory` call sites keep working.
/// Category determines whether the shared SSRF check's `allow_private`
/// escape hatch applies -- only `PrivateIp` ever lifts it, and even then
/// loopback/link-local/metadata/CGNAT/cluster CIDRs are still always
/// denied (`is_forbidden_address` denies those unconditionally regardless
/// of the flag).
pub use egress_assertion::DestinationCategory;

/// Denies `ip` if it falls in any deployment-configured cluster CIDR
/// (pod/service/node -- `DENY_CLUSTER_CIDRS`) or an operator-configured
/// extra deny range (`DENY_CIDRS`), or if it fails the shared SSRF check
/// for `category`. Returns `None` when the address is permitted. Order
/// matters only for the reason string reported to the audit log, not for
/// correctness -- every branch is a hard deny.
///
/// `allow_private_ip_enabled` is this deployment's
/// `EGRESS_PROXY_ALLOW_PRIVATE_IP` config gate -- defense in depth,
/// independent of and layered underneath the assertion's own
/// `PrivateIp` category: the shared SSRF check's private-range escape
/// hatch only ever lifts when *both* the assertion says `PrivateIp` *and*
/// this deployment has opted in, so a compromised/misconfigured signer
/// minting private-IP grants is still contained on a deployment that
/// never enabled this gate.
pub fn is_denied(
    ip: IpAddr,
    category: DestinationCategory,
    cluster_cidrs: &[IpNet],
    deny_cidrs: &[IpNet],
    allow_private_ip_enabled: bool,
) -> Option<&'static str> {
    if cluster_cidrs.iter().any(|net| net.contains(&ip)) {
        return Some("cluster_cidr");
    }
    if deny_cidrs.iter().any(|net| net.contains(&ip)) {
        return Some("operator_deny_cidr");
    }
    let allow_private =
        allow_private_ip_enabled && matches!(category, DestinationCategory::PrivateIp);
    is_forbidden_address(ip, allow_private)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::Ipv4Addr;

    fn cluster_cidrs() -> Vec<IpNet> {
        vec![
            "10.244.0.0/16".parse().unwrap(),
            "10.96.0.0/12".parse().unwrap(),
        ]
    }

    #[test]
    fn cluster_pod_cidr_denied_even_with_private_ip_category() {
        let ip = IpAddr::V4(Ipv4Addr::new(10, 244, 1, 5));
        assert_eq!(
            is_denied(
                ip,
                DestinationCategory::PrivateIp,
                &cluster_cidrs(),
                &[],
                true
            ),
            Some("cluster_cidr")
        );
    }

    #[test]
    fn metadata_denied_regardless_of_category() {
        let ip = IpAddr::V4(Ipv4Addr::new(169, 254, 169, 254));
        assert_eq!(
            is_denied(ip, DestinationCategory::PrivateIp, &[], &[], true),
            Some("cloud_metadata")
        );
        assert_eq!(
            is_denied(ip, DestinationCategory::Fqdn, &[], &[], true),
            Some("cloud_metadata")
        );
    }

    #[test]
    fn loopback_denied_regardless_of_category() {
        let ip = IpAddr::V4(Ipv4Addr::LOCALHOST);
        assert_eq!(
            is_denied(ip, DestinationCategory::PrivateIp, &[], &[], true),
            Some("loopback")
        );
    }

    #[test]
    fn private_ip_denied_without_private_ip_category() {
        let ip = IpAddr::V4(Ipv4Addr::new(192, 168, 1, 10));
        assert_eq!(
            is_denied(ip, DestinationCategory::Fqdn, &[], &[], true),
            Some("private")
        );
        assert_eq!(
            is_denied(ip, DestinationCategory::PublicIp, &[], &[], true),
            Some("private")
        );
    }

    #[test]
    fn private_ip_permitted_with_private_ip_category_and_gate_enabled() {
        let ip = IpAddr::V4(Ipv4Addr::new(192, 168, 1, 10));
        assert_eq!(
            is_denied(ip, DestinationCategory::PrivateIp, &[], &[], true),
            None
        );
    }

    /// `EGRESS_PROXY_ALLOW_PRIVATE_IP` (`allow_private_ip_enabled`) is a
    /// deployment-wide gate independent of the assertion's own category --
    /// a `PrivateIp`-category assertion still denies every private address
    /// on a deployment that has not opted into the gate.
    #[test]
    fn private_ip_denied_with_private_ip_category_when_gate_disabled() {
        let ip = IpAddr::V4(Ipv4Addr::new(192, 168, 1, 10));
        assert_eq!(
            is_denied(ip, DestinationCategory::PrivateIp, &[], &[], false),
            Some("private")
        );
    }

    #[test]
    fn public_ip_permitted_for_fqdn_category() {
        let ip = IpAddr::V4(Ipv4Addr::new(93, 184, 216, 34));
        assert_eq!(
            is_denied(ip, DestinationCategory::Fqdn, &cluster_cidrs(), &[], true),
            None
        );
    }
}
