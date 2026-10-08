//! Runtime configuration, sourced entirely from environment variables (12
//! factor -- matches the env vars already wired into
//! `k8s/helm/waddlebot/templates/infrastructure/egress-proxy.yaml` by
//! feature/bundle-egress-gateway, plus the machine-JWT/limiter variables
//! this crate adds on top of that skeleton).

use std::net::IpAddr;
use std::time::Duration;

use ipnet::IpNet;

/// Every tunable this service reads at startup. `from_env` fails closed --
/// a missing required variable is a startup error, never a silent default
/// that would weaken the security posture (signing key path, JWKS URL).
#[derive(Debug, Clone)]
pub struct Config {
    pub listen_port: u16,
    pub metrics_port: u16,
    pub allowed_ports: Vec<u16>,
    pub deny_cidrs: Vec<IpNet>,
    pub deny_cluster_cidrs: Vec<IpNet>,
    pub machine_jwt_jwks_url: String,
    pub machine_jwt_audience: String,
    pub machine_jwt_trusted_issuers: Vec<String>,
    pub machine_jwt_required_scope: String,
    pub allowed_caller_services: Vec<String>,
    pub per_tenant_max_connections: u32,
    pub per_tenant_bandwidth_bytes_per_sec: u64,
    pub connect_timeout: Duration,
    pub assertion_max_ttl: Duration,
    /// Timeout for reading a client's request headers off the wire
    /// (`hyper::server::conn::http1::Builder::header_read_timeout`) --
    /// bounds a slow-loris-style caller that opens a connection and
    /// trickles headers in indefinitely.
    pub header_read_timeout: Duration,
    /// Inactivity timeout applied to each direction of a CONNECT tunnel's
    /// byte-copy loop: no bytes read within this window closes the
    /// tunnel, freeing the tenant's connection-limiter slot.
    pub tunnel_idle_timeout: Duration,
    /// Hard ceiling on a single CONNECT tunnel's total lifetime,
    /// regardless of activity -- bounds a connection an operator's
    /// destination keeps trickling just enough traffic through to dodge
    /// the idle timeout forever.
    pub tunnel_max_duration: Duration,
    /// Defense-in-depth, operator-controlled kill switch for the
    /// `PrivateIp` assertion category: even a validly-signed, correctly
    /// destination-matched private-IP grant is refused unless this
    /// deployment has explicitly opted in. Independent of (layered
    /// underneath) the assertion's own category -- a compromised/
    /// misconfigured signer minting private-IP grants is still contained
    /// by this being `false` by default. `EGRESS_PROXY_ALLOW_PRIVATE_IP`.
    pub allow_private_ip: bool,
}

#[derive(thiserror::Error, Debug)]
pub enum ConfigError {
    #[error("missing required env var {0}")]
    Missing(&'static str),
    #[error("invalid value for {0}: {1}")]
    Invalid(&'static str, String),
}

fn env_or(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

fn parse_ports(raw: &str) -> Result<Vec<u16>, ConfigError> {
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|s| {
            s.parse::<u16>()
                .map_err(|_| ConfigError::Invalid("ALLOWED_PORTS", s.to_string()))
        })
        .collect()
}

fn parse_cidrs(raw: &str, var: &'static str) -> Result<Vec<IpNet>, ConfigError> {
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|s| {
            // Bare IPs (no `/prefix`, e.g. a single node IP) are accepted as
            // a /32 or /128 host route.
            let net = if s.contains('/') {
                s.parse::<IpNet>()
                    .map_err(|_| ConfigError::Invalid(var, s.to_string()))?
            } else {
                s.parse::<IpAddr>()
                    .map(IpNet::from)
                    .map_err(|_| ConfigError::Invalid(var, s.to_string()))?
            };
            // regression: mapped-v6 cluster bypass -- `ip_policy::is_denied`
            // canonicalizes every checked address to its embedded-v4 form
            // (via `bundle_host_http::egress::canonicalize_ip`) before
            // comparing against this list, so a range configured in IPv4-
            // mapped/NAT64/IPv4-compatible IPv6 form could never match
            // anything post-canonicalization. Fail closed at startup rather
            // than silently shipping a deny entry that can never fire.
            if let IpAddr::V6(v6) = net.addr() {
                if bundle_host_http::egress::embedded_ipv4(v6).is_some() {
                    return Err(ConfigError::Invalid(
                        var,
                        format!(
                            "{s} is an IPv4-mapped/NAT64/IPv4-compatible IPv6 range -- write it \
                             in native IPv4 form instead (e.g. 10.0.0.0/8)"
                        ),
                    ));
                }
            }
            Ok(net)
        })
        .collect()
}

fn parse_list(raw: &str) -> Vec<String> {
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect()
}

impl Config {
    /// Loads config from the process environment. See module doc for the
    /// env-var/helm-skeleton correspondence.
    pub fn from_env() -> Result<Self, ConfigError> {
        let listen_port: u16 = env_or("PROXY_LISTEN_PORT", "8443")
            .parse()
            .map_err(|_| ConfigError::Invalid("PROXY_LISTEN_PORT", "not a u16".into()))?;
        let metrics_port: u16 = env_or("METRICS_PORT", "9090")
            .parse()
            .map_err(|_| ConfigError::Invalid("METRICS_PORT", "not a u16".into()))?;
        let allowed_ports = parse_ports(&env_or("ALLOWED_PORTS", "80,443,6697"))?;
        let deny_cidrs = parse_cidrs(&env_or("DENY_CIDRS", ""), "DENY_CIDRS")?;
        let deny_cluster_cidrs =
            parse_cidrs(&env_or("DENY_CLUSTER_CIDRS", ""), "DENY_CLUSTER_CIDRS")?;
        let machine_jwt_jwks_url = std::env::var("MACHINE_JWT_JWKS_URL")
            .map_err(|_| ConfigError::Missing("MACHINE_JWT_JWKS_URL"))?;
        let machine_jwt_audience = std::env::var("MACHINE_JWT_AUDIENCE")
            .map_err(|_| ConfigError::Missing("MACHINE_JWT_AUDIENCE"))?;
        let machine_jwt_trusted_issuers = parse_list(&env_or(
            "MACHINE_JWT_TRUSTED_ISSUERS",
            "spiffe://penguintech.io",
        ));
        let machine_jwt_required_scope = env_or("MACHINE_JWT_REQUIRED_SCOPE", "egress:connect");
        let allowed_caller_services = parse_list(&env_or(
            "ALLOWED_CALLER_SERVICES",
            "svc-ingest,svc-process,svc-action",
        ));
        let per_tenant_max_connections: u32 = env_or("PER_TENANT_MAX_CONNECTIONS", "50")
            .parse()
            .map_err(|_| ConfigError::Invalid("PER_TENANT_MAX_CONNECTIONS", "not a u32".into()))?;
        let per_tenant_bandwidth_bytes_per_sec: u64 =
            env_or("PER_TENANT_BANDWIDTH_BYTES_PER_SEC", "10485760")
                .parse()
                .map_err(|_| {
                    ConfigError::Invalid("PER_TENANT_BANDWIDTH_BYTES_PER_SEC", "not a u64".into())
                })?;
        let connect_timeout_secs: u64 = env_or("CONNECT_TIMEOUT_SECONDS", "10")
            .parse()
            .map_err(|_| ConfigError::Invalid("CONNECT_TIMEOUT_SECONDS", "not a u64".into()))?;
        let assertion_max_ttl_secs: u64 = env_or("ASSERTION_MAX_TTL_SECONDS", "60")
            .parse()
            .map_err(|_| ConfigError::Invalid("ASSERTION_MAX_TTL_SECONDS", "not a u64".into()))?;
        let header_read_timeout_secs: u64 = env_or("HEADER_READ_TIMEOUT_SECONDS", "10")
            .parse()
            .map_err(|_| ConfigError::Invalid("HEADER_READ_TIMEOUT_SECONDS", "not a u64".into()))?;
        let tunnel_idle_timeout_secs: u64 = env_or("TUNNEL_IDLE_TIMEOUT_SECONDS", "300")
            .parse()
            .map_err(|_| ConfigError::Invalid("TUNNEL_IDLE_TIMEOUT_SECONDS", "not a u64".into()))?;
        let tunnel_max_duration_secs: u64 = env_or("TUNNEL_MAX_DURATION_SECONDS", "3600")
            .parse()
            .map_err(|_| {
            ConfigError::Invalid("TUNNEL_MAX_DURATION_SECONDS", "not a u64".into())
        })?;
        let allow_private_ip: bool = env_or("EGRESS_PROXY_ALLOW_PRIVATE_IP", "false")
            .parse()
            .map_err(|_| {
                ConfigError::Invalid("EGRESS_PROXY_ALLOW_PRIVATE_IP", "not a bool".into())
            })?;

        Ok(Self {
            listen_port,
            metrics_port,
            allowed_ports,
            deny_cidrs,
            deny_cluster_cidrs,
            machine_jwt_jwks_url,
            machine_jwt_audience,
            machine_jwt_trusted_issuers,
            machine_jwt_required_scope,
            allowed_caller_services,
            per_tenant_max_connections,
            per_tenant_bandwidth_bytes_per_sec,
            connect_timeout: Duration::from_secs(connect_timeout_secs),
            assertion_max_ttl: Duration::from_secs(assertion_max_ttl_secs),
            header_read_timeout: Duration::from_secs(header_read_timeout_secs),
            tunnel_idle_timeout: Duration::from_secs(tunnel_idle_timeout_secs),
            tunnel_max_duration: Duration::from_secs(tunnel_max_duration_secs),
            allow_private_ip,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_cidrs_accepts_a_plain_v4_range_and_a_bare_ip() {
        let parsed = parse_cidrs("10.244.0.0/16, 1.2.3.4", "DENY_CIDRS").unwrap();
        assert_eq!(parsed.len(), 2);
    }

    /// regression: mapped-v6 cluster bypass -- a deny-CIDR entry written in
    /// IPv4-mapped-IPv6 form is rejected at config-parse time (fail closed)
    /// rather than silently accepted as an entry `ip_policy::is_denied`'s
    /// canonicalization would ensure can never match anything.
    #[test]
    fn parse_cidrs_rejects_a_mapped_ipv6_range() {
        let err = parse_cidrs("::ffff:10.244.0.0/120", "DENY_CLUSTER_CIDRS").unwrap_err();
        let message = err.to_string();
        assert!(
            message.contains("IPv4-mapped"),
            "expected a clear IPv4-mapped config error, got: {message}"
        );
    }

    /// regression: mapped-v6 cluster bypass -- same rejection for the
    /// NAT64-synthesized and IPv4-compatible encodings.
    #[test]
    fn parse_cidrs_rejects_nat64_and_ipv4_compatible_ranges() {
        for raw in ["64:ff9b::10.244.0.0/120", "::10.244.0.0/120"] {
            assert!(
                parse_cidrs(raw, "DENY_CIDRS").is_err(),
                "expected {raw} to be rejected"
            );
        }
    }

    #[test]
    fn parse_cidrs_accepts_a_native_v6_range() {
        let parsed = parse_cidrs("fd00::/8", "DENY_CIDRS").unwrap();
        assert_eq!(parsed.len(), 1);
    }

    #[test]
    fn parse_ports_accepts_a_comma_separated_list_with_whitespace() {
        let parsed = parse_ports("80, 443,6697").unwrap();
        assert_eq!(parsed, vec![80, 443, 6697]);
    }

    #[test]
    fn parse_ports_ignores_empty_segments() {
        let parsed = parse_ports("80,,443,").unwrap();
        assert_eq!(parsed, vec![80, 443]);
    }

    #[test]
    fn parse_ports_rejects_a_non_numeric_entry() {
        let err = parse_ports("80,not-a-port").unwrap_err();
        assert!(matches!(err, ConfigError::Invalid("ALLOWED_PORTS", _)));
    }

    #[test]
    fn parse_list_trims_and_drops_empty_entries() {
        let parsed = parse_list(" svc-ingest ,svc-process,,svc-action ");
        assert_eq!(parsed, vec!["svc-ingest", "svc-process", "svc-action"]);
    }

    #[test]
    fn config_error_display_messages() {
        assert_eq!(
            ConfigError::Missing("MACHINE_JWT_JWKS_URL").to_string(),
            "missing required env var MACHINE_JWT_JWKS_URL"
        );
        assert_eq!(
            ConfigError::Invalid("PROXY_LISTEN_PORT", "abc".into()).to_string(),
            "invalid value for PROXY_LISTEN_PORT: abc"
        );
    }

    /// Every env var `Config::from_env` reads. Shared with
    /// `crate::tests::CONFIG_ENV_VARS` (`lib.rs`) -- both modules serialize
    /// on the single crate-wide `crate::ENV_LOCK` so these tests and
    /// `lib.rs`'s `build_state` tests (which also drive `from_env`
    /// indirectly) never race on the same process-global variables.
    const CONFIG_ENV_VARS: &[&str] = &[
        "PROXY_LISTEN_PORT",
        "METRICS_PORT",
        "ALLOWED_PORTS",
        "DENY_CIDRS",
        "DENY_CLUSTER_CIDRS",
        "MACHINE_JWT_JWKS_URL",
        "MACHINE_JWT_AUDIENCE",
        "MACHINE_JWT_TRUSTED_ISSUERS",
        "MACHINE_JWT_REQUIRED_SCOPE",
        "ALLOWED_CALLER_SERVICES",
        "PER_TENANT_MAX_CONNECTIONS",
        "PER_TENANT_BANDWIDTH_BYTES_PER_SEC",
        "CONNECT_TIMEOUT_SECONDS",
        "ASSERTION_MAX_TTL_SECONDS",
        "HEADER_READ_TIMEOUT_SECONDS",
        "TUNNEL_IDLE_TIMEOUT_SECONDS",
        "TUNNEL_MAX_DURATION_SECONDS",
        "EGRESS_PROXY_ALLOW_PRIVATE_IP",
    ];

    fn clear_env() {
        for var in CONFIG_ENV_VARS {
            // SAFETY: serialized by ENV_LOCK, held by every caller.
            unsafe { std::env::remove_var(var) };
        }
    }

    #[test]
    fn from_env_uses_documented_defaults_when_only_required_vars_are_set() {
        let _guard = crate::ENV_LOCK.blocking_lock();
        clear_env();
        // SAFETY: serialized by ENV_LOCK.
        unsafe {
            std::env::set_var("MACHINE_JWT_JWKS_URL", "https://hub-api.example/jwks.json");
            std::env::set_var("MACHINE_JWT_AUDIENCE", "egress-proxy");
        }

        let cfg = Config::from_env().expect("required vars are set");
        assert_eq!(cfg.listen_port, 8443);
        assert_eq!(cfg.metrics_port, 9090);
        assert_eq!(cfg.allowed_ports, vec![80, 443, 6697]);
        assert!(cfg.deny_cidrs.is_empty());
        assert!(cfg.deny_cluster_cidrs.is_empty());
        assert_eq!(
            cfg.machine_jwt_trusted_issuers,
            vec!["spiffe://penguintech.io".to_string()]
        );
        assert_eq!(cfg.machine_jwt_required_scope, "egress:connect");
        assert_eq!(
            cfg.allowed_caller_services,
            vec!["svc-ingest", "svc-process", "svc-action"]
        );
        assert_eq!(cfg.per_tenant_max_connections, 50);
        assert_eq!(cfg.per_tenant_bandwidth_bytes_per_sec, 10_485_760);
        assert_eq!(cfg.connect_timeout, Duration::from_secs(10));
        assert_eq!(cfg.assertion_max_ttl, Duration::from_secs(60));
        assert_eq!(cfg.header_read_timeout, Duration::from_secs(10));
        assert_eq!(cfg.tunnel_idle_timeout, Duration::from_secs(300));
        assert_eq!(cfg.tunnel_max_duration, Duration::from_secs(3600));
        assert!(!cfg.allow_private_ip);

        clear_env();
    }

    #[test]
    fn from_env_overrides_every_tunable_when_all_vars_are_set() {
        let _guard = crate::ENV_LOCK.blocking_lock();
        clear_env();
        // SAFETY: serialized by ENV_LOCK.
        unsafe {
            std::env::set_var("PROXY_LISTEN_PORT", "9443");
            std::env::set_var("METRICS_PORT", "9091");
            std::env::set_var("ALLOWED_PORTS", "22");
            std::env::set_var("DENY_CIDRS", "10.0.0.0/8");
            std::env::set_var("DENY_CLUSTER_CIDRS", "10.244.0.0/16");
            std::env::set_var("MACHINE_JWT_JWKS_URL", "https://hub-api.example/jwks.json");
            std::env::set_var("MACHINE_JWT_AUDIENCE", "egress-proxy");
            std::env::set_var("MACHINE_JWT_TRUSTED_ISSUERS", "hub-api,hub-api-2");
            std::env::set_var("MACHINE_JWT_REQUIRED_SCOPE", "egress:custom");
            std::env::set_var("ALLOWED_CALLER_SERVICES", "svc-ingest");
            std::env::set_var("PER_TENANT_MAX_CONNECTIONS", "5");
            std::env::set_var("PER_TENANT_BANDWIDTH_BYTES_PER_SEC", "1024");
            std::env::set_var("CONNECT_TIMEOUT_SECONDS", "1");
            std::env::set_var("ASSERTION_MAX_TTL_SECONDS", "30");
            std::env::set_var("HEADER_READ_TIMEOUT_SECONDS", "2");
            std::env::set_var("TUNNEL_IDLE_TIMEOUT_SECONDS", "60");
            std::env::set_var("TUNNEL_MAX_DURATION_SECONDS", "120");
            std::env::set_var("EGRESS_PROXY_ALLOW_PRIVATE_IP", "true");
        }

        let cfg = Config::from_env().expect("all vars are valid");
        assert_eq!(cfg.listen_port, 9443);
        assert_eq!(cfg.metrics_port, 9091);
        assert_eq!(cfg.allowed_ports, vec![22]);
        assert_eq!(cfg.deny_cidrs.len(), 1);
        assert_eq!(cfg.deny_cluster_cidrs.len(), 1);
        assert_eq!(
            cfg.machine_jwt_trusted_issuers,
            vec!["hub-api".to_string(), "hub-api-2".to_string()]
        );
        assert_eq!(cfg.machine_jwt_required_scope, "egress:custom");
        assert_eq!(cfg.allowed_caller_services, vec!["svc-ingest"]);
        assert_eq!(cfg.per_tenant_max_connections, 5);
        assert_eq!(cfg.per_tenant_bandwidth_bytes_per_sec, 1024);
        assert_eq!(cfg.connect_timeout, Duration::from_secs(1));
        assert_eq!(cfg.assertion_max_ttl, Duration::from_secs(30));
        assert_eq!(cfg.header_read_timeout, Duration::from_secs(2));
        assert_eq!(cfg.tunnel_idle_timeout, Duration::from_secs(60));
        assert_eq!(cfg.tunnel_max_duration, Duration::from_secs(120));
        assert!(cfg.allow_private_ip);

        clear_env();
    }

    #[test]
    fn from_env_fails_closed_when_jwks_url_is_missing() {
        let _guard = crate::ENV_LOCK.blocking_lock();
        clear_env();
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::set_var("MACHINE_JWT_AUDIENCE", "egress-proxy") };

        let err = Config::from_env().unwrap_err();
        assert!(matches!(err, ConfigError::Missing("MACHINE_JWT_JWKS_URL")));

        clear_env();
    }

    #[test]
    fn from_env_fails_closed_when_audience_is_missing() {
        let _guard = crate::ENV_LOCK.blocking_lock();
        clear_env();
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::set_var("MACHINE_JWT_JWKS_URL", "https://hub-api.example/jwks.json") };

        let err = Config::from_env().unwrap_err();
        assert!(matches!(err, ConfigError::Missing("MACHINE_JWT_AUDIENCE")));

        clear_env();
    }

    #[test]
    fn from_env_rejects_an_invalid_listen_port() {
        let _guard = crate::ENV_LOCK.blocking_lock();
        clear_env();
        // SAFETY: serialized by ENV_LOCK.
        unsafe {
            std::env::set_var("PROXY_LISTEN_PORT", "not-a-port");
            std::env::set_var("MACHINE_JWT_JWKS_URL", "https://hub-api.example/jwks.json");
            std::env::set_var("MACHINE_JWT_AUDIENCE", "egress-proxy");
        }

        let err = Config::from_env().unwrap_err();
        assert!(matches!(err, ConfigError::Invalid("PROXY_LISTEN_PORT", _)));

        clear_env();
    }

    #[test]
    fn from_env_rejects_a_non_bool_private_ip_flag() {
        let _guard = crate::ENV_LOCK.blocking_lock();
        clear_env();
        // SAFETY: serialized by ENV_LOCK.
        unsafe {
            std::env::set_var("MACHINE_JWT_JWKS_URL", "https://hub-api.example/jwks.json");
            std::env::set_var("MACHINE_JWT_AUDIENCE", "egress-proxy");
            std::env::set_var("EGRESS_PROXY_ALLOW_PRIVATE_IP", "not-a-bool");
        }

        let err = Config::from_env().unwrap_err();
        assert!(matches!(
            err,
            ConfigError::Invalid("EGRESS_PROXY_ALLOW_PRIVATE_IP", _)
        ));

        clear_env();
    }
}
