//! Address-category policy: layers this proxy's two extra checks --
//! deployment-configured cluster CIDRs and the three-permission-family
//! category-crossing rule -- on top of `bundle_host_http::egress::
//! is_forbidden_address`, the shared SSRF-hardened check (loopback,
//! link-local, unspecified, multicast, broadcast, cloud metadata,
//! RFC1918/ULA private, CGNAT) already exercised by the in-process guard.
//! See that crate's `lib.rs` module doc, "Explicitly deferred" paragraph --
//! this module is exactly the deferred follow-up it names.

use std::net::IpAddr;

use bundle_host_http::egress::{canonicalize_ip, is_forbidden_address};
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
    // regression: mapped-v6 cluster bypass -- `IpNet::contains` requires an
    // exact address-family match, so an IPv4-mapped/NAT64/IPv4-compatible
    // IPv6 encoding of a denied address (e.g. `::ffff:10.42.0.5` against a
    // configured `10.42.0.0/16`) never matched either CIDR list below, even
    // though `is_forbidden_address` already canonicalizes the same address
    // for its own checks. Canonicalize once, up front, and use that value
    // for every check in this function -- shares `bundle_host_http::
    // egress::canonicalize_ip` so this proxy can never diverge from the
    // in-process guard's `ClusterCidrDenylist` on what counts as "the same
    // address".
    let ip = canonicalize_ip(ip);
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

    /// regression: mapped-v6 cluster bypass -- the exact SSRF this module
    /// was written to close: `::ffff:10.244.1.5` (a resolver/attacker
    /// encoding of the same cluster-pod address `cluster_pod_cidr_denied_
    /// even_with_private_ip_category` proves is denied in plain v4 form)
    /// must be denied too, even with `PrivateIp` category and the
    /// deployment-wide private-IP gate enabled. Before `is_denied`
    /// canonicalized `ip` up front, `IpNet::contains`'s exact-family
    /// requirement let this straight through: `cluster_cidrs`/`deny_cidrs`
    /// never matched (v4 net vs v6 ip), and `is_forbidden_address`'s own
    /// canonicalization only ever gated the private-range check, not the
    /// cluster/operator deny lists.
    #[test]
    fn cluster_pod_cidr_denies_an_ipv4_mapped_ipv6_target() {
        let ip: IpAddr = "::ffff:10.244.1.5".parse().unwrap();
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

    /// regression: mapped-v6 cluster bypass -- same bypass via the NAT64-
    /// synthesized and deprecated IPv4-compatible encodings of the same
    /// cluster address.
    #[test]
    fn cluster_pod_cidr_denies_nat64_and_ipv4_compatible_encodings() {
        for addr in ["64:ff9b::10.244.1.5", "::10.244.1.5"] {
            let ip: IpAddr = addr.parse().unwrap();
            assert_eq!(
                is_denied(
                    ip,
                    DestinationCategory::PrivateIp,
                    &cluster_cidrs(),
                    &[],
                    true
                ),
                Some("cluster_cidr"),
                "expected {addr} to be denied by the cluster CIDR list"
            );
        }
    }

    /// regression: mapped-v6 cluster bypass -- the operator `DENY_CIDRS`
    /// list (`deny_cidrs`) must canonicalize the same way `cluster_cidrs`
    /// does; it is a second, independent `IpNet::contains` call site in the
    /// same function.
    #[test]
    fn operator_deny_cidr_denies_an_ipv4_mapped_ipv6_target() {
        let ip: IpAddr = "::ffff:203.0.113.5".parse().unwrap();
        let deny_cidrs: Vec<IpNet> = vec!["203.0.113.0/24".parse().unwrap()];
        assert_eq!(
            is_denied(ip, DestinationCategory::PublicIp, &[], &deny_cidrs, true),
            Some("operator_deny_cidr")
        );
    }

    /// regression: mapped-v6 cluster bypass -- mapped/NAT64/compatible
    /// encodings of cloud metadata and loopback must still be denied
    /// through this proxy's own `is_denied` entry point (not just the
    /// shared `is_forbidden_address` this delegates to), proving the
    /// canonicalization added here doesn't accidentally skip that delegate
    /// call.
    #[test]
    fn ipv4_mapped_metadata_and_loopback_are_denied_through_is_denied() {
        let metadata: IpAddr = "::ffff:169.254.169.254".parse().unwrap();
        assert_eq!(
            is_denied(metadata, DestinationCategory::PrivateIp, &[], &[], true),
            Some("cloud_metadata")
        );
        let loopback: IpAddr = "::ffff:127.0.0.1".parse().unwrap();
        assert_eq!(
            is_denied(loopback, DestinationCategory::PrivateIp, &[], &[], true),
            Some("loopback")
        );
    }
}
