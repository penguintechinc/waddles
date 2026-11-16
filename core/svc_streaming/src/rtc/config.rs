//! [`RtcConfig`]: derives WebRTC transport settings from [`crate::config::Config`]
//! without adding any new env vars -- `src/config.rs` is owned by a
//! different chunk, so this reuses `WEBRTC_UDP_RANGE`, `BIND_ADDR`, and
//! `PUBLIC_BASE_URL` exactly as already declared there.

use std::net::IpAddr;

use thiserror::Error;

use crate::config::{parse_udp_range, Config};

/// Errors building an [`RtcConfig`] from [`Config`].
#[derive(Debug, Error, PartialEq, Eq)]
pub enum RtcConfigError {
    #[error("invalid WEBRTC_UDP_RANGE: {0}")]
    InvalidUdpRange(String),
}

/// WebRTC transport settings shared by WHIP ingest and WHEP egress
/// [`crate::rtc::pc_factory::PeerConnectionFactory`] instances.
///
/// **No STUN/TURN** (`docs/plans/2026-09-11-svc-streaming-pipeline-matrix.md`
/// §8: "host ICE candidates ... no TURN") -- every gathered candidate is a
/// host candidate on `bind_ip`, optionally rewritten to `nat_1to1_ip` via
/// `SettingEngine::set_nat_1to1_ips` for a pod behind a 1:1 NAT/LB.
#[derive(Debug, Clone)]
pub struct RtcConfig {
    /// Local interface each `PeerConnection`'s single UDP socket binds to
    /// (`BIND_ADDR`). A concrete address, never a wildcard -- see
    /// [`crate::rtc::pc_factory::PeerConnectionFactory`] for why a specific
    /// bind address is required to pin the port to `udp_port_range`.
    pub bind_ip: IpAddr,
    /// Inclusive UDP port range ICE candidates are drawn from
    /// (`WEBRTC_UDP_RANGE`). Enforced by
    /// [`crate::rtc::pc_factory::PortAllocator`], not by
    /// `SettingEngine::set_udp_network` -- see that module's docs for why.
    pub udp_port_range: (u16, u16),
    /// Public IP host candidates are rewritten to, parsed from
    /// `PUBLIC_BASE_URL` when its host is a literal IP address (a DNS name
    /// is left unmapped -- NAT-mapping a name has no single answer and
    /// `SettingEngine::set_nat_1to1_ips` takes IPs, not names).
    pub nat_1to1_ip: Option<IpAddr>,
}

impl RtcConfig {
    /// Builds an [`RtcConfig`] from the service's already-loaded [`Config`].
    pub fn from_config(config: &Config) -> Result<Self, RtcConfigError> {
        let udp_port_range = parse_udp_range(&config.cli.webrtc_udp_range)
            .map_err(|err| RtcConfigError::InvalidUdpRange(err.to_string()))?;
        Ok(Self {
            bind_ip: config.cli.bind_addr,
            udp_port_range,
            nat_1to1_ip: nat_1to1_ip_from_public_base_url(&config.cli.public_base_url),
        })
    }
}

/// Extracts a literal IP host from `PUBLIC_BASE_URL`, e.g.
/// `https://203.0.113.10:8208` -> `Some(203.0.113.10)`. Returns `None` for a
/// DNS name (`localhost`, `stream.penguintech.cloud`) or an unparseable
/// URL -- both leave host candidates unmapped rather than guessing.
fn nat_1to1_ip_from_public_base_url(url: &str) -> Option<IpAddr> {
    let without_scheme = url.split_once("://").map_or(url, |(_, rest)| rest);
    let host_port = without_scheme
        .split_once('/')
        .map_or(without_scheme, |(host, _)| host);
    let host = if let Some(bracketed) = host_port.strip_prefix('[') {
        // IPv6 literal in bracket notation, e.g. [::1]:8208.
        bracketed.split(']').next().unwrap_or(bracketed)
    } else {
        host_port.split_once(':').map_or(host_port, |(h, _)| h)
    };
    host.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CliConfig, Config, Secret};
    use clap::Parser;

    fn config_with(webrtc_udp_range: &str, public_base_url: &str) -> Config {
        let mut cli = CliConfig::parse_from(["svc-streaming"]);
        cli.webrtc_udp_range = webrtc_udp_range.to_string();
        cli.public_base_url = public_base_url.to_string();
        Config {
            cli,
            db_password: Secret::new("db-pass"),
            cache_password: None,
            service_api_key: Secret::new("service-key"),
            jwt_keys: crate::jwt_keys::JwtKeys::None,
        }
    }

    #[test]
    fn parses_udp_range_from_shared_config() {
        let config = config_with("40000-40100", "http://localhost:8208");
        let rtc = RtcConfig::from_config(&config).expect("valid range");
        assert_eq!(rtc.udp_port_range, (40000, 40100));
    }

    #[test]
    fn rejects_invalid_udp_range() {
        let config = config_with("not-a-range", "http://localhost:8208");
        let err = RtcConfig::from_config(&config).unwrap_err();
        assert!(matches!(err, RtcConfigError::InvalidUdpRange(_)));
    }

    #[test]
    fn nat_1to1_ip_none_for_dns_name() {
        let config = config_with("40000-40100", "https://stream.penguintech.cloud");
        let rtc = RtcConfig::from_config(&config).unwrap();
        assert_eq!(rtc.nat_1to1_ip, None);
    }

    #[test]
    fn nat_1to1_ip_parses_literal_ipv4_host() {
        let config = config_with("40000-40100", "https://203.0.113.10:8208");
        let rtc = RtcConfig::from_config(&config).unwrap();
        assert_eq!(rtc.nat_1to1_ip, Some("203.0.113.10".parse().unwrap()));
    }

    #[test]
    fn nat_1to1_ip_parses_bracketed_ipv6_host() {
        let config = config_with("40000-40100", "https://[2001:db8::1]:8208");
        let rtc = RtcConfig::from_config(&config).unwrap();
        assert_eq!(rtc.nat_1to1_ip, Some("2001:db8::1".parse().unwrap()));
    }

    #[test]
    fn nat_1to1_ip_none_for_localhost() {
        let config = config_with("40000-40100", "http://localhost:8208");
        let rtc = RtcConfig::from_config(&config).unwrap();
        assert_eq!(rtc.nat_1to1_ip, None);
    }
}
